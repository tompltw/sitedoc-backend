"""
Pipeline routes — ticket transitions + site agent management.

Permitted transitions by actor:
  customer:   ready_for_uat_approval → todo (Approve & Start Work)
              ready_for_uat → done (UAT Pass)
              ready_for_uat → todo (UAT Fail, dev_fail_count++)
              any → dismissed

  pm_agent:   triage → ready_for_uat_approval
  dev_agent:  todo → in_progress; in_progress → ready_for_qa
  qa_agent:   ready_for_qa → in_qa; in_qa → ready_for_uat (pass); in_qa → todo (fail)
  tech_lead:  any → in_progress; in_progress → ready_for_qa
  system:     any transition (internal use)
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.api.schemas import (
    IssueResponse,
    IssueTransitionRequest,
    SiteAgentCreate,
    SiteAgentResponse,
    TicketTransitionResponse,
)
from src.db.models import (
    Customer,
    Issue,
    KanbanColumn,
    Site,
    SiteAgent,
    TicketTransition,
)
from src.db.session import get_db

router = APIRouter()

# ---------------------------------------------------------------------------
# Transition permission matrix
# ---------------------------------------------------------------------------

CUSTOMER_TRANSITIONS: dict[KanbanColumn, list[KanbanColumn]] = {
    KanbanColumn.ready_for_uat_approval: [KanbanColumn.todo, KanbanColumn.dismissed],
    KanbanColumn.ready_for_uat: [KanbanColumn.done, KanbanColumn.todo],
    # Customer can dismiss from any stage
}

# Stages that increment dev_fail_count when customer moves back to todo
UAT_FAIL_COLUMNS = {KanbanColumn.ready_for_uat}


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
    issue_id: uuid.UUID,
    from_col: Optional[KanbanColumn],
    to_col: KanbanColumn,
    actor_type: str,
    actor_id: Optional[uuid.UUID] = None,
    note: Optional[str] = None,
) -> None:
    transition = TicketTransition(
        issue_id=issue_id,
        from_col=from_col,
        to_col=to_col,
        actor_type=actor_type,
        actor_id=actor_id,
        note=note,
    )
    db.add(transition)
    await db.flush()  # ensure row is visible to the outer commit


# ---------------------------------------------------------------------------
# Customer-triggered transitions
# ---------------------------------------------------------------------------

@router.post("/issues/{issue_id}/transition", response_model=IssueResponse)
async def transition_issue(
    issue_id: uuid.UUID,
    body: IssueTransitionRequest,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Customer-triggered ticket stage transitions."""
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)

    current_col = issue.kanban_column
    target_col = body.to_col

    # Always allow dismiss
    if target_col == KanbanColumn.dismissed:
        pass
    else:
        allowed = CUSTOMER_TRANSITIONS.get(current_col, [])
        if target_col not in allowed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot move ticket from '{current_col.value}' to '{target_col.value}' as customer",
            )

    # Increment dev_fail_count on UAT fail (customer sends back to todo from ready_for_uat)
    if current_col in UAT_FAIL_COLUMNS and target_col == KanbanColumn.todo:
        issue.dev_fail_count += 1
        # Check if tech lead escalation needed
        if issue.dev_fail_count >= 3:
            _enqueue_tech_lead(str(issue_id), reason=f"dev_fail_count={issue.dev_fail_count}")

    # Apply transition
    old_col = issue.kanban_column
    issue.kanban_column = target_col

    # Sync legacy status field
    issue.status = _kanban_to_legacy_status(target_col)

    # Mark resolved_at if done
    if target_col == KanbanColumn.done:
        issue.resolved_at = datetime.now(timezone.utc)

    # If customer approved work, trigger dev agent
    if target_col == KanbanColumn.todo:
        _enqueue_dev_agent(str(issue_id))

    await db.flush()
    await _log_transition(
        db, issue_id, old_col, target_col,
        actor_type="customer",
        actor_id=current_customer.id,
        note=body.note,
    )
    await db.commit()
    await db.refresh(issue)

    try:
        from src.api.ws import publish_event
        issue_dict = IssueResponse.model_validate(issue).model_dump(mode='json')
        publish_event(str(issue_id), {"type": "issue_updated", "issue": issue_dict})
    except Exception as e:
        logger.warning("[pipeline] WS broadcast failed for %s: %s", issue_id, e)

    return issue


# ---------------------------------------------------------------------------
# Internal agent transition (called by Celery workers via internal API)
# ---------------------------------------------------------------------------

@router.post("/issues/{issue_id}/transition/internal", response_model=IssueResponse)
async def transition_issue_internal(
    issue_id: uuid.UUID,
    body: IssueTransitionRequest,
    actor_type: str = "system",
    actor_id: Optional[uuid.UUID] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Internal transition endpoint for Celery workers.
    No customer auth — secured by network boundary (internal only).
    """
    result = await db.execute(select(Issue).where(Issue.id == issue_id))
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")

    old_col = issue.kanban_column
    issue.kanban_column = body.to_col
    issue.status = _kanban_to_legacy_status(body.to_col)

    if body.to_col == KanbanColumn.in_progress:
        # Set stall check timestamp when dev picks up
        issue.stall_check_at = datetime.now(timezone.utc)

    if body.to_col == KanbanColumn.done:
        issue.resolved_at = datetime.now(timezone.utc)

    # QA fail — increment dev_fail_count
    if body.to_col == KanbanColumn.todo and old_col == KanbanColumn.in_qa:
        issue.dev_fail_count += 1
        if issue.dev_fail_count >= 3:
            _enqueue_tech_lead(str(issue_id), reason=f"qa_fail_count={issue.dev_fail_count}")

    await db.flush()
    await _log_transition(
        db, issue_id, old_col, body.to_col,
        actor_type=actor_type,
        actor_id=actor_id,
        note=body.note,
    )
    await db.commit()
    await db.refresh(issue)

    # Broadcast status update via WebSocket
    try:
        from src.api.ws import publish_event
        from src.api.issues import IssueResponse
        
        issue_dict = IssueResponse.model_validate(issue).model_dump(mode='json')
        publish_event(str(issue_id), {
            "type": "issue_updated",
            "issue": issue_dict,
        })
    except Exception as e:
        logger.warning("[pipeline] WebSocket broadcast failed for %s: %s", issue_id, e)

    # Enqueue dev agent when any internal actor sends ticket to todo
    if body.to_col == KanbanColumn.todo:
        _enqueue_dev_agent(str(issue_id))

    # Enqueue QA agent when ticket moves to ready_for_qa
    if body.to_col == KanbanColumn.ready_for_qa:
        _enqueue_qa_agent(str(issue_id))

    return issue


@router.get("/issues/{issue_id}/transitions", response_model=list[TicketTransitionResponse])
async def list_transitions(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Full audit log of stage transitions for a ticket."""
    await _get_issue_for_customer(issue_id, current_customer.id, db)
    result = await db.execute(
        select(TicketTransition)
        .where(TicketTransition.issue_id == issue_id)
        .order_by(TicketTransition.created_at.asc())
    )
    return result.scalars().all()


# ---------------------------------------------------------------------------
# Site agents
# ---------------------------------------------------------------------------

@router.get("/sites/{site_id}/agents", response_model=list[SiteAgentResponse])
async def list_site_agents(
    site_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site or site.customer_id != current_customer.id:
        raise HTTPException(status_code=404, detail="Site not found")

    agents_result = await db.execute(
        select(SiteAgent).where(SiteAgent.site_id == site_id)
    )
    return agents_result.scalars().all()


@router.post("/sites/{site_id}/agents", response_model=SiteAgentResponse, status_code=201)
async def create_site_agent(
    site_id: uuid.UUID,
    body: SiteAgentCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site or site.customer_id != current_customer.id:
        raise HTTPException(status_code=404, detail="Site not found")

    agent = SiteAgent(
        site_id=site_id,
        agent_role=body.agent_role.value,
        model=body.model,
    )
    db.add(agent)
    await db.flush()
    await db.refresh(agent)
    await db.commit()
    return agent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kanban_to_legacy_status(col: KanbanColumn):
    """Map kanban column back to legacy IssueStatus for backwards compat."""
    from src.db.models import IssueStatus
    mapping = {
        KanbanColumn.triage: IssueStatus.open,
        KanbanColumn.ready_for_uat_approval: IssueStatus.open,
        KanbanColumn.todo: IssueStatus.open,
        KanbanColumn.in_progress: IssueStatus.in_progress,
        KanbanColumn.ready_for_qa: IssueStatus.in_progress,
        KanbanColumn.in_qa: IssueStatus.in_progress,
        KanbanColumn.ready_for_uat: IssueStatus.pending_approval,
        KanbanColumn.done: IssueStatus.resolved,
        KanbanColumn.dismissed: IssueStatus.dismissed,
    }
    return mapping.get(col, IssueStatus.open)


def _enqueue_dev_agent(issue_id: str) -> None:
    try:
        from src.tasks.base import celery_app
        celery_app.send_task("src.tasks.dev_agent.run", args=[issue_id], queue="backend")
    except Exception:
        logger.warning("Could not enqueue dev_agent for %s", issue_id)


def _enqueue_qa_agent(issue_id: str) -> None:
    try:
        from src.tasks.base import celery_app
        celery_app.send_task("src.tasks.qa_agent.run", args=[issue_id], queue="backend")
    except Exception:
        logger.warning("Could not enqueue qa_agent for %s", issue_id)


def _enqueue_tech_lead(issue_id: str, reason: str = "") -> None:
    try:
        from src.tasks.base import celery_app
        celery_app.send_task("src.tasks.tech_lead_agent.run", args=[issue_id, reason], queue="backend")
    except Exception:
        logger.warning("Could not enqueue tech_lead for %s", issue_id)
