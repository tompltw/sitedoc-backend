"""
Issues routes — CRUD + status management + kanban workflow.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.api.schemas import AgentActionResponse, IssueCreate, IssueResponse, IssueStatusUpdate
from src.db.models import (
    AgentAction, Customer, Issue, IssueStatus, IssuePriority,
    KanbanColumn, TicketTransition,
)
from src.db.session import get_db

router = APIRouter()

TECH_LEAD_FAIL_THRESHOLD = 3


async def _get_issue_for_customer(
    issue_id: uuid.UUID,
    customer_id: uuid.UUID,
    db: AsyncSession,
) -> Issue:
    result = await db.execute(
        select(Issue).where(Issue.id == issue_id, Issue.customer_id == customer_id)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    return issue


async def _log_transition(
    db: AsyncSession,
    issue: Issue,
    to_col: KanbanColumn,
    actor_type: str,
    actor_id: uuid.UUID | None = None,
    note: str | None = None,
) -> None:
    transition = TicketTransition(
        issue_id=issue.id,
        from_col=issue.kanban_column,
        to_col=to_col,
        actor_type=actor_type,
        actor_id=actor_id,
        note=note,
    )
    db.add(transition)
    issue.kanban_column = to_col


@router.get("/", response_model=list[IssueResponse])
async def list_issues(
    site_id: uuid.UUID | None = Query(None),
    issue_status: IssueStatus | None = Query(None, alias="status"),
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    q = select(Issue).where(Issue.customer_id == current_customer.id)
    if site_id:
        q = q.where(Issue.site_id == site_id)
    if issue_status:
        q = q.where(Issue.status == issue_status)
    q = q.order_by(Issue.created_at.desc())
    result = await db.execute(q)
    return result.scalars().all()


@router.post("/", response_model=IssueResponse, status_code=status.HTTP_201_CREATED)
async def create_issue(
    body: IssueCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    issue = Issue(
        site_id=body.site_id,
        customer_id=current_customer.id,
        title=body.title,
        description=body.description,
        priority=body.priority or IssuePriority.medium,
    )
    db.add(issue)
    await db.flush()
    await db.refresh(issue)

    # Auto-trigger diagnosis immediately on ticket creation
    _enqueue_diagnose_task(issue_id=str(issue.id))

    return issue


@router.get("/{issue_id}/actions", response_model=list[AgentActionResponse])
async def list_actions(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Return all agent actions for a given issue (owned by the current customer)."""
    # Verify ownership
    await _get_issue_for_customer(issue_id, current_customer.id, db)

    result = await db.execute(
        select(AgentAction)
        .where(AgentAction.issue_id == issue_id)
        .order_by(AgentAction.created_at.asc())
    )
    return result.scalars().all()


@router.get("/{issue_id}", response_model=IssueResponse)
async def get_issue(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    return await _get_issue_for_customer(issue_id, current_customer.id, db)


@router.patch("/{issue_id}/status", response_model=IssueResponse)
async def update_status(
    issue_id: uuid.UUID,
    body: IssueStatusUpdate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)
    issue.status = body.status
    if body.status == IssueStatus.resolved:
        issue.resolved_at = datetime.now(timezone.utc)
        # Keep kanban column in sync — resolved tickets belong in done
        if issue.kanban_column not in (KanbanColumn.done, KanbanColumn.dismissed):
            issue.kanban_column = KanbanColumn.done
    elif body.status == IssueStatus.dismissed:
        if issue.kanban_column != KanbanColumn.dismissed:
            issue.kanban_column = KanbanColumn.dismissed
    await db.flush()
    await db.refresh(issue)
    return issue


@router.post("/{issue_id}/approve-and-start", response_model=IssueResponse)
async def approve_and_start(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """
    Customer clicks "Approve & Start Work" on the ticket page.

    Gate: ticket must be in `ready_for_uat_approval`.
    Effect: transitions to `todo`, logs the transition, enqueues dev work.
    """
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)

    if issue.kanban_column != KanbanColumn.ready_for_uat_approval:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ticket is in '{issue.kanban_column.value}' — "
                "only 'ready_for_uat_approval' tickets can be approved."
            ),
        )

    await _log_transition(
        db, issue,
        to_col=KanbanColumn.todo,
        actor_type="customer",
        actor_id=current_customer.id,
        note="Customer approved via 'Approve & Start Work'",
    )

    await db.flush()
    await db.refresh(issue)

    _enqueue_fix_task(str(issue_id), tier="assisted")

    return issue


@router.post("/{issue_id}/uat-reject", response_model=IssueResponse)
async def uat_reject(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """
    Customer rejects a fix during UAT (`ready_for_uat`).

    Rules (confirmed by Tom 2026-02-17):
    - dev_fail_count++ always — no reset, ever.
    - Customer catching it counts the same as QA catching it.
    - At >= 3 total failures → Tech Lead takes over, ticket → `dismissed` for triage.
    - Below threshold → ticket returns to `todo` for another dev cycle.
    """
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)

    if issue.kanban_column != KanbanColumn.ready_for_uat:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Ticket is in '{issue.kanban_column.value}' — "
                "only 'ready_for_uat' tickets can be UAT-rejected."
            ),
        )

    # Increment fail counter — never reset
    issue.dev_fail_count += 1
    new_count = issue.dev_fail_count

    if new_count >= TECH_LEAD_FAIL_THRESHOLD:
        # Tech Lead intervention
        await _log_transition(
            db, issue,
            to_col=KanbanColumn.triage,
            actor_type="customer",
            actor_id=current_customer.id,
            note=f"UAT rejected (failure #{new_count}) — escalated to Tech Lead",
        )
        _post_system_message(
            str(issue_id),
            f"🚨 Ticket failed UAT {new_count} time(s). Tech Lead has been notified and is taking over.",
        )
        _enqueue_tech_lead_task(str(issue_id), fail_count=new_count)
    else:
        # Return to dev for another cycle
        await _log_transition(
            db, issue,
            to_col=KanbanColumn.todo,
            actor_type="customer",
            actor_id=current_customer.id,
            note=f"UAT rejected (failure #{new_count}/{TECH_LEAD_FAIL_THRESHOLD}) — back to dev",
        )
        _post_system_message(
            str(issue_id),
            f"Fix rejected during UAT (attempt {new_count}). Returning to development queue.",
        )
        _enqueue_fix_task(str(issue_id), tier="assisted")

    await db.flush()
    await db.refresh(issue)
    return issue


@router.post("/{issue_id}/approve", response_model=IssueResponse)
async def approve_fix(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """
    Legacy approval endpoint — kept for backwards compatibility.
    Prefer /approve-and-start for new integrations.
    """
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)

    if issue.status != IssueStatus.pending_approval:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue is '{issue.status.value}' — only 'pending_approval' issues can be approved",
        )

    if issue.confidence_score is not None and issue.confidence_score < 0.30:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Confidence score {issue.confidence_score:.0%} is too low to approve",
        )

    _enqueue_fix_task(str(issue_id), tier="assisted")
    return issue


def _enqueue_diagnose_task(issue_id: str) -> None:
    """Fire-and-forget: trigger the diagnosis pipeline on issue creation."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.diagnose.diagnose_issue",
            args=[issue_id],
            queue="agent",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not enqueue diagnose task for issue %s", issue_id
        )


def _enqueue_fix_task(issue_id: str, tier: str = "autonomous") -> None:
    """Fire-and-forget: trigger the full fix pipeline via dev_agent."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.dev_agent.run",
            args=[issue_id],
            queue="backend",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not enqueue fix task for issue %s", issue_id
        )


def _enqueue_tech_lead_task(issue_id: str, fail_count: int) -> None:
    """Fire-and-forget: escalate to Tech Lead after repeated failures."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.tech_lead_agent.run",
            args=[issue_id],
            kwargs={"reason": f"dev_fail_count={fail_count}"},
            queue="backend",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not enqueue tech_lead task for issue %s", issue_id
        )


def _post_system_message(issue_id: str, content: str) -> None:
    """Post a system message to the issue's chat thread."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.messaging.post_system_message",
            args=[issue_id, content],
            queue="agent",
        )
    except Exception:
        pass  # Non-critical
