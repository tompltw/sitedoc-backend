"""
Shared helpers for SiteDoc Celery task workers.

Provides:
  - celery_app: shared Celery instance (imported by all task modules)
  - post_chat_message: inserts a ChatMessage + publishes WebSocket event
  - transition_issue: calls the internal HTTP transition endpoint
  - get_issue: fetches an Issue record from DB using sync SQLAlchemy
"""
import json
import logging
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Load .env from project root (two levels up from src/tasks/base.py)
_env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(_env_path, override=False)

import requests
from celery import Celery
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Celery application — shared across all task modules
# ---------------------------------------------------------------------------

celery_app = Celery(
    "sitedoc",
    broker=os.getenv("CELERY_BROKER_URL", os.getenv("REDIS_URL", "redis://localhost:6379/0")),
    backend=os.getenv("CELERY_RESULT_BACKEND", os.getenv("REDIS_URL", "redis://localhost:6379/0")),
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "stall-checker-every-5min": {
            "task": "src.tasks.stall_checker.check_stalled_tickets",
            "schedule": 5 * 60,  # every 5 minutes
        },
    },
)

# ---------------------------------------------------------------------------
# DB helpers — sync SQLAlchemy for use inside Celery tasks
# ---------------------------------------------------------------------------

def _sync_db_url(db_url: str) -> str:
    """Convert asyncpg/aioredis URL to a psycopg2 URL for sync access."""
    return (
        db_url
        .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        .replace("postgresql://", "postgresql+psycopg2://")
    )


@contextmanager
def get_db_session(db_url: str):
    """
    Context manager that yields a sync SQLAlchemy session.
    Commits on clean exit, rolls back on exception.
    """
    sync_url = _sync_db_url(db_url)
    engine = create_engine(sync_url, pool_pre_ping=True, pool_size=2, max_overflow=3)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Shared task helpers
# ---------------------------------------------------------------------------

def post_chat_message(
    issue_id: str,
    content: str,
    agent_role: str,
    db_url: str,
) -> str:
    """
    Insert a ChatMessage (sender_type='agent', agent_role=agent_role) and
    publish a WebSocket event so connected clients see it in real time.

    Returns the new message's UUID string.
    """
    from src.db.models import ChatMessage, SenderType
    from src.api.ws import publish_event

    with get_db_session(db_url) as session:
        msg = ChatMessage(
            issue_id=uuid.UUID(issue_id),
            sender_type=SenderType.agent,
            content=content,
            agent_role=agent_role,
        )
        session.add(msg)
        session.flush()
        msg_id = str(msg.id)

    # Publish WebSocket event (outside session — no DB dependency)
    try:
        publish_event(issue_id, {
            "type": "message",
            "role": "agent",
            "agent_role": agent_role,
            "content": content,
            "message_id": msg_id,
        })
    except Exception as e:
        logger.warning("[base] WS publish failed for issue %s: %s", issue_id, e)

    return msg_id


def transition_issue(
    issue_id: str,
    to_col: str,
    actor_type: str,
    note: Optional[str] = None,
    db_url: Optional[str] = None,  # accepted for API compat but not used for HTTP call
) -> None:
    """
    Call the internal FastAPI transition endpoint to move a ticket to a new
    kanban column and log the transition in the audit trail.

    Endpoint: POST /api/v1/issues/{issue_id}/transition/internal
    """
    api_base = os.getenv("INTERNAL_API_URL", "http://localhost:5000")
    url = f"{api_base}/api/v1/issues/{issue_id}/transition/internal"

    payload: dict = {"to_col": to_col}
    if note:
        payload["note"] = note

    try:
        resp = requests.post(
            url,
            json=payload,
            params={"actor_type": actor_type},
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("[base] Transitioned issue %s → %s (%s)", issue_id, to_col, actor_type)
    except Exception as e:
        logger.error("[base] transition_issue failed for %s → %s: %s", issue_id, to_col, e)
        raise


def transition_issue_direct(
    issue_id: str,
    to_col: str,
    actor_type: str,
    note: Optional[str] = None,
    db_url: Optional[str] = None,
) -> None:
    """
    Transition a ticket directly via DB (sync SQLAlchemy) — no HTTP call.

    Use this when calling from within a FastAPI request handler to avoid
    the self-referential HTTP deadlock that transition_issue() would cause.
    """
    from datetime import datetime, timezone
    from src.db.models import Issue, KanbanColumn, IssueStatus, TicketTransition

    _db_url = db_url or os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")

    KANBAN_TO_STATUS = {
        "triage": "open",
        "ready_for_uat_approval": "open",
        "todo": "open",
        "in_progress": "in_progress",
        "ready_for_qa": "in_progress",
        "in_qa": "in_progress",
        "ready_for_uat": "pending_approval",
        "done": "resolved",
        "dismissed": "dismissed",
    }

    with get_db_session(_db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")

        old_col = issue.kanban_column
        issue.kanban_column = KanbanColumn(to_col)

        legacy_status = KANBAN_TO_STATUS.get(to_col, "in_progress")
        issue.status = IssueStatus(legacy_status)

        if to_col == "in_progress":
            issue.stall_check_at = datetime.now(timezone.utc)

        if to_col == "done":
            issue.resolved_at = datetime.now(timezone.utc)

        if to_col == "todo" and old_col and old_col.value == "in_qa":
            issue.dev_fail_count = (issue.dev_fail_count or 0) + 1

        should_enqueue_dev = to_col == "todo"
        should_enqueue_qa = to_col == "ready_for_qa"

        transition = TicketTransition(
            issue_id=uuid.UUID(issue_id),
            from_col=old_col,
            to_col=KanbanColumn(to_col),
            actor_type=actor_type,
            note=note,
        )
        session.add(transition)

    logger.info("[base] transition_direct issue %s → %s (%s)", issue_id, to_col, actor_type)

    # Auto-enqueue dev agent when ticket moves to todo
    if should_enqueue_dev:
        try:
            celery_app.send_task("src.tasks.dev_agent.run", args=[issue_id], queue="backend")
            logger.info("[base] dev_agent enqueued for issue %s (todo transition)", issue_id)
        except Exception as e:
            logger.error("[base] Could not enqueue dev_agent for %s: %s", issue_id, e)

    # Auto-enqueue qa agent when ticket moves to ready_for_qa
    if should_enqueue_qa:
        try:
            celery_app.send_task("src.tasks.qa_agent.run", args=[issue_id], queue="backend")
            logger.info("[base] qa_agent enqueued for issue %s (ready_for_qa transition)", issue_id)
        except Exception as e:
            logger.error("[base] Could not enqueue qa_agent for %s: %s", issue_id, e)


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def try_acquire_agent_lock(issue_id: str, agent_role: str, ttl_seconds: int = 900) -> bool:
    """
    Try to acquire a Redis lock for (agent_role, issue_id).

    Uses SET NX EX so only the first caller succeeds — any concurrent duplicate
    Celery task for the same issue will see the lock already held and abort.

    The lock expires automatically after ttl_seconds (default 15 min, matching
    the agent run timeout) so a crashed task can always be retried.

    Returns True if the lock was acquired, False if it was already held.
    """
    try:
        import redis as redis_lib
        r = redis_lib.from_url(REDIS_URL, decode_responses=True)
        key = f"agent_lock:{agent_role}:{issue_id}"
        return bool(r.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception as e:
        # If Redis is unavailable, log and allow the task to proceed so we
        # don't block all work — the pre-flight column check is still a backstop.
        logger.warning("[base] Could not check agent lock for %s/%s: %s — proceeding", agent_role, issue_id, e)
        return True


def release_agent_lock(issue_id: str, agent_role: str) -> None:
    """Release the agent lock early (e.g. called from the agent-result callback)."""
    try:
        import redis as redis_lib
        r = redis_lib.from_url(REDIS_URL, decode_responses=True)
        r.delete(f"agent_lock:{agent_role}:{issue_id}")
    except Exception as e:
        logger.warning("[base] Could not release agent lock for %s/%s: %s", agent_role, issue_id, e)


def get_issue(issue_id: str, db_url: str):
    """
    Fetch and return a detached Issue ORM object from the database.
    Relationships are NOT loaded (use explicit queries for related data).
    """
    from src.db.models import Issue

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")
        # Snapshot scalar columns before session closes
        return issue
