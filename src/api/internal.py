"""
Internal HTTP endpoints for agent callbacks.

These endpoints are called by spawned OpenClaw sub-agents to report results
and advance ticket state — no customer auth required, protected by a shared
static token (AGENT_INTERNAL_TOKEN env var).
"""
import logging
import os
import uuid

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from src.tasks.base import post_chat_message, transition_issue

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

AGENT_INTERNAL_TOKEN = os.getenv("AGENT_INTERNAL_TOKEN", "")
DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")


def _verify_token(authorization: str | None) -> None:
    if not AGENT_INTERNAL_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AGENT_INTERNAL_TOKEN not configured",
        )
    if not authorization or authorization != f"Bearer {AGENT_INTERNAL_TOKEN}":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal token",
        )


class AgentResultBody(BaseModel):
    issue_id: str
    agent_role: str = "dev"           # "dev" | "qa" | "pm" | "tech_lead"
    status: str                        # "success" | "failure"
    message: str                       # Summary posted to chat
    transition_to: str | None = None  # kanban column to move to (None = no transition)


@router.post("/agent-result")
async def agent_result(
    body: AgentResultBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """
    Called by spawned sub-agents to post a result message and advance the ticket.

    Headers:
        Authorization: Bearer <AGENT_INTERNAL_TOKEN>

    Body:
        {
          "issue_id": "uuid",
          "agent_role": "dev",
          "status": "success" | "failure",
          "message": "What the agent did / what failed",
          "transition_to": "ready_for_qa" | null
        }
    """
    _verify_token(authorization)

    issue_id = body.issue_id
    logger.info(
        "[internal] agent-result: issue=%s role=%s status=%s",
        issue_id, body.agent_role, body.status,
    )

    # 1. Post the result message to the issue chat
    prefix = "✅" if body.status == "success" else "❌"
    chat_content = f"{prefix} {body.message}"

    try:
        post_chat_message(issue_id, chat_content, body.agent_role, DB_URL)
    except Exception as e:
        logger.error("[internal] Failed to post chat message for issue %s: %s", issue_id, e)
        raise HTTPException(status_code=500, detail=f"Failed to post chat message: {e}")

    # 2. Advance the ticket if a target column was specified
    if body.transition_to:
        try:
            transition_issue(
                issue_id=issue_id,
                to_col=body.transition_to,
                actor_type=f"{body.agent_role}_agent",
                note=f"Agent {body.status}: advanced to {body.transition_to}",
            )
        except Exception as e:
            logger.error("[internal] Failed to transition issue %s → %s: %s", issue_id, body.transition_to, e)
            # Don't fail the whole request — message was already posted
            return {"ok": True, "warning": f"Message posted but transition failed: {e}"}

        # 3. Enqueue next agent if moving to ready_for_qa
        if body.transition_to == "ready_for_qa" and body.status == "success":
            try:
                from src.tasks.base import celery_app
                celery_app.send_task("src.tasks.qa_agent.run", args=[issue_id], queue="backend")
                logger.info("[internal] QA agent enqueued for issue %s", issue_id)
            except Exception as e:
                logger.error("[internal] Failed to enqueue QA agent for issue %s: %s", issue_id, e)

    return {"ok": True}
