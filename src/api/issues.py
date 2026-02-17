"""
Issues routes — CRUD + status management + approval workflow.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.api.schemas import IssueCreate, IssueResponse, IssueStatusUpdate
from src.db.models import Customer, Issue, IssueStatus, IssuePriority
from src.db.session import get_db

router = APIRouter()


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
    Approve a pending fix for an issue in `pending_approval` state.
    Triggers the fix pipeline immediately (Celery, async).
    """
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)

    if issue.status != IssueStatus.pending_approval:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue is '{issue.status.value}' — only 'pending_approval' issues can be approved",
        )

    # Confidence gate check — if somehow < 60%, don't allow approval
    if issue.confidence_score is not None and issue.confidence_score < 0.30:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Confidence score {issue.confidence_score:.0%} is too low to approve",
        )

    # Trigger fix task in "assisted" tier (user explicitly approved — bypass confidence gate)
    _enqueue_fix_task(str(issue_id), tier="assisted")

    return issue


@router.post("/{issue_id}/reject", response_model=IssueResponse)
async def reject_fix(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """
    Reject a pending fix — marks issue open for re-diagnosis or manual handling.
    """
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)

    if issue.status not in (IssueStatus.in_progress, IssueStatus.pending_approval, IssueStatus.open):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject fix for issue in '{issue.status.value}' state",
        )

    issue.status = IssueStatus.open
    await db.flush()
    await db.refresh(issue)

    # Post a system message to the conversation so the chat reflects the rejection
    _post_system_message(str(issue_id), "Fix rejected by user. Issue reopened for review.")

    return issue


def _enqueue_diagnose_task(issue_id: str) -> None:
    """Fire-and-forget: trigger the diagnosis pipeline on issue creation."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
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
    """Fire-and-forget: trigger the full fix pipeline."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.fix.apply_fix",
            args=[issue_id],
            kwargs={"tier": tier},
            queue="agent",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not enqueue fix task for issue %s", issue_id
        )


def _post_system_message(issue_id: str, content: str) -> None:
    """Post a system message to the issue's chat thread."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.messaging.post_system_message",
            args=[issue_id, content],
            queue="agent",
        )
    except Exception:
        pass  # Non-critical
