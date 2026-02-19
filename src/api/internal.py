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

from src.tasks.base import post_chat_message, release_agent_lock, transition_issue_direct

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


class SaveCredentialBody(BaseModel):
    site_id: str
    credential_type: str  # ssh | ftp | wp_admin | database | cpanel | wp_app_password | api_key
    value: dict           # JSON-serialisable credential object


class AgentResultBody(BaseModel):
    issue_id: str
    agent_role: str = "dev"           # "dev" | "qa" | "pm" | "tech_lead"
    status: str                        # "success" | "failure"
    message: str                       # Summary posted to chat
    transition_to: str | None = None  # kanban column to move to (None = no transition)


@router.post("/save-credential", status_code=201)
async def save_credential(
    body: SaveCredentialBody,
    authorization: str | None = Header(default=None),
) -> dict:
    """
    Called by the PM agent to store a credential the customer provided in chat.
    Authenticated by AGENT_INTERNAL_TOKEN.
    """
    import base64
    import json as _json
    from cryptography.fernet import Fernet
    from src.core.config import settings

    _verify_token(authorization)

    # Validate credential type
    from src.db.models import CredentialType
    try:
        ctype = CredentialType(body.credential_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown credential_type: {body.credential_type}")

    # Encrypt the credential
    raw = settings.CREDENTIAL_ENCRYPTION_KEY.encode()
    key = base64.urlsafe_b64encode(raw.ljust(32)[:32])
    fernet = Fernet(key)
    encrypted = fernet.encrypt(_json.dumps(body.value).encode()).decode()

    # Upsert: delete existing credential of same type for site, then insert new one
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
    from src.db.models import SiteCredential, Site

    engine = create_async_engine(DB_URL.replace("postgresql+psycopg2", "postgresql+asyncpg"))
    async_session = async_sessionmaker(engine, expire_on_commit=False)

    async with async_session() as session:
        async with session.begin():
            # Verify site exists
            site_result = await session.execute(
                select(Site).where(Site.id == uuid.UUID(body.site_id))
            )
            site = site_result.scalar_one_or_none()
            if not site:
                raise HTTPException(status_code=404, detail="Site not found")

            # Delete any existing credential of same type
            existing_result = await session.execute(
                select(SiteCredential).where(
                    SiteCredential.site_id == uuid.UUID(body.site_id),
                    SiteCredential.credential_type == ctype,
                )
            )
            for old_cred in existing_result.scalars().all():
                await session.delete(old_cred)

            # Insert new credential
            new_cred = SiteCredential(
                site_id=uuid.UUID(body.site_id),
                credential_type=ctype,
                encrypted_value=encrypted,
            )
            session.add(new_cred)

    await engine.dispose()

    logger.info("[internal] Saved %s credential for site %s", body.credential_type, body.site_id)
    return {"ok": True, "credential_type": body.credential_type}


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
        "[internal] agent-result: issue=%s role=%s status=%s transition_to=%s",
        issue_id, body.agent_role, body.status, body.transition_to,
    )

    # Idempotency guard: skip if the target column is same or earlier in the pipeline.
    # This prevents backwards transitions (e.g. ready_for_qa when already in_qa)
    # and duplicate callbacks from re-running completed stages.
    if body.transition_to:
        try:
            from src.db.models import Issue
            from src.tasks.base import get_db_session

            PIPELINE_ORDER = [
                "triage", "ready_for_uat_approval", "todo", "in_progress",
                "ready_for_qa", "in_qa", "ready_for_uat", "done", "dismissed",
            ]

            def _col_idx(val: str) -> int:
                try:
                    return PIPELINE_ORDER.index(val)
                except ValueError:
                    return -1

            with get_db_session(DB_URL) as session:
                issue = session.get(Issue, uuid.UUID(issue_id))
                if issue:
                    current_idx = _col_idx(issue.kanban_column.value if issue.kanban_column else "")
                    target_idx  = _col_idx(body.transition_to)
                    if target_idx >= 0 and current_idx >= target_idx:
                        logger.warning(
                            "[internal] Skipping callback for %s — already at '%s', target '%s' is same or earlier",
                            issue_id, issue.kanban_column, body.transition_to,
                        )
                        return {"ok": True, "skipped": "already_at_or_past_target"}
        except Exception as e:
            logger.error("[internal] Idempotency check failed for issue %s: %s", issue_id, e)

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
            transition_issue_direct(
                issue_id=issue_id,
                to_col=body.transition_to,
                actor_type=f"{body.agent_role}_agent",
                note=f"Agent {body.status}: advanced to {body.transition_to}",
                db_url=DB_URL,
            )
        except Exception as e:
            logger.error("[internal] Failed to transition issue %s → %s: %s", issue_id, body.transition_to, e)
            # Don't fail the whole request — message was already posted
            return {"ok": True, "warning": f"Message posted but transition failed: {e}"}

    # Release the agent lock so the next run (retry or new stage) can proceed immediately.
    # agent_role from the callback is "dev" or "qa" — matches the lock keys set in those tasks.
    release_agent_lock(issue_id, body.agent_role)

    # Note: dev/qa agent enqueuing is handled inside transition_issue_direct()
    # (base.py) to keep dispatch logic in one place and avoid double-enqueue.

    return {"ok": True}
