"""
Stall checker — Celery Beat task, runs every 5 minutes.

Recovery tiers:
  1. `todo` tickets not picked up in >5 min     → re-enqueue dev agent
  2. `ready_for_qa` tickets not picked up in >5m → re-enqueue qa agent
  3. `in_progress` / `in_qa` stuck >4h          → escalate to tech lead
"""
import logging
import os
from datetime import datetime, timezone, timedelta

from sqlalchemy import text

from src.tasks.base import celery_app, get_db_session, post_chat_message

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "")
STALL_THRESHOLD_HOURS = int(os.getenv("STALL_THRESHOLD_HOURS", "4"))
TODO_PICKUP_MINUTES = int(os.getenv("TODO_PICKUP_MINUTES", "5"))


def _last_activity_query() -> str:
    """SQL fragment: most recent activity timestamp for an issue."""
    return """
        GREATEST(
            COALESCE((SELECT MAX(tt.created_at) FROM ticket_transitions tt WHERE tt.issue_id = i.id), i.created_at),
            COALESCE((SELECT MAX(cm.created_at) FROM chat_messages cm WHERE cm.issue_id = i.id), i.created_at)
        )
    """


@celery_app.task(name="src.tasks.stall_checker.check_stalled_tickets")
def check_stalled_tickets():
    """
    Self-healing pass over all active tickets.
    Runs every 5 minutes via Celery Beat.
    """
    if not DB_URL:
        logger.error("[stall_checker] DATABASE_URL not set")
        return

    now = datetime.now(timezone.utc)
    todo_cutoff = now - timedelta(minutes=TODO_PICKUP_MINUTES)
    stall_cutoff = now - timedelta(hours=STALL_THRESHOLD_HOURS)

    with get_db_session(DB_URL) as session:
        rows = session.execute(text(f"""
            SELECT
                i.id::text,
                i.kanban_column::text,
                i.dev_fail_count,
                {_last_activity_query()} AS last_activity_at
            FROM issues i
            WHERE i.kanban_column IN ('todo', 'ready_for_qa', 'in_progress', 'in_qa')
        """)).fetchall()

    if not rows:
        return

    for issue_id, kanban_col, dev_fail_count, last_activity in rows:
        last_activity = last_activity.replace(tzinfo=timezone.utc) if last_activity.tzinfo is None else last_activity

        # Tier 1: todo not picked up within TODO_PICKUP_MINUTES
        if kanban_col == "todo" and last_activity < todo_cutoff:
            logger.warning(
                "[stall_checker] issue %s stuck in 'todo' since %s — re-triggering dev agent",
                issue_id, last_activity.isoformat(),
            )
            try:
                celery_app.send_task("src.tasks.dev_agent.run", args=[issue_id], queue="backend")
                # Bump stall_check_at so we don't fire again for 15 min
                with get_db_session(DB_URL) as session:
                    session.execute(
                        text("UPDATE issues SET stall_check_at = now() + INTERVAL '15 minutes' WHERE id = :id"),
                        {"id": issue_id},
                    )
            except Exception as e:
                logger.error("[stall_checker] dev re-trigger failed for %s: %s", issue_id, e)
            continue

        # Tier 2: ready_for_qa not picked up within TODO_PICKUP_MINUTES
        if kanban_col == "ready_for_qa" and last_activity < todo_cutoff:
            logger.warning(
                "[stall_checker] issue %s stuck in 'ready_for_qa' since %s — re-triggering qa agent",
                issue_id, last_activity.isoformat(),
            )
            try:
                celery_app.send_task("src.tasks.qa_agent.run", args=[issue_id], queue="backend")
                with get_db_session(DB_URL) as session:
                    session.execute(
                        text("UPDATE issues SET stall_check_at = now() + INTERVAL '15 minutes' WHERE id = :id"),
                        {"id": issue_id},
                    )
            except Exception as e:
                logger.error("[stall_checker] qa re-trigger failed for %s: %s", issue_id, e)
            continue

        # Tier 3: long-running in_progress / in_qa → escalate
        if kanban_col in ("in_progress", "in_qa") and last_activity < stall_cutoff:
            reason = (
                f"Stall detected: ticket stuck in '{kanban_col}' for >{STALL_THRESHOLD_HOURS}h. "
                f"dev_fail_count={dev_fail_count}"
            )
            logger.warning("[stall_checker] Escalating %s — %s", issue_id, reason)
            try:
                post_chat_message(
                    issue_id=issue_id,
                    content=f"⚠️ No progress for over {STALL_THRESHOLD_HOURS} hours. Escalating to Tech Lead.",
                    agent_role="system",
                    db_url=DB_URL,
                )
                celery_app.send_task("src.tasks.tech_lead_agent.run", args=[issue_id, reason], queue="backend")
                with get_db_session(DB_URL) as session:
                    session.execute(
                        text("UPDATE issues SET stall_check_at = now() + INTERVAL '4 hours' WHERE id = :id"),
                        {"id": issue_id},
                    )
            except Exception as e:
                logger.error("[stall_checker] Escalation failed for %s: %s", issue_id, e)

    logger.info("[stall_checker] Pass complete — checked %d active ticket(s)", len(rows))
