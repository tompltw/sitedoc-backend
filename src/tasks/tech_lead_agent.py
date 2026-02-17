"""
Tech Lead Agent Celery task — routed via OpenClaw (CLAWBOT).

Trigger: dev_fail_count >= 3 OR dev stall > 4 hours.

Flow:
  1. Post "Tech Lead escalated — reviewing history..." message.
  2. Fetch full issue history (all chat messages + transitions).
  3. Call OpenClaw agent with full context for expert analysis.
  4. Post tech lead response (agent_role='tech_lead').
  5. Transition to in_progress, re-enqueue dev_agent with guidance.
"""
import logging
import os
import uuid


from src.tasks.llm import call_llm
from src.tasks.base import (
    celery_app,
    get_db_session,
    post_chat_message,
    transition_issue,
)

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")

TECH_LEAD_SYSTEM_PROMPT = """You are a senior tech lead and engineering manager at SiteDoc, \
a managed website maintenance service. You have been escalated on a ticket where the dev agent \
has failed to fix the issue multiple times or has stalled.

Your job is to:
1. Analyze the full ticket history (all previous dev attempts and QA failures)
2. Identify what went wrong with each attempt
3. Provide precise, actionable guidance for the next fix attempt
4. Be extremely specific: exact file paths, exact commands, exact code changes

Format your response as:
## Root Cause Analysis
[What is fundamentally wrong and why previous attempts failed]

## Corrected Fix Plan
[Step-by-step instructions the dev agent must follow exactly]

## Verification Checklist
[Specific checks the QA agent should perform after the fix]"""


def _fetch_full_history(issue_id: str, db_url: str) -> dict:
    """
    Fetch the full history of the issue: metadata, all chat messages, all transitions.
    Returns a structured dict for building the tech lead prompt.
    """
    from src.db.models import Issue, Site, ChatMessage, TicketTransition, SenderType

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")

        site = session.get(Site, issue.site_id)
        site_url = site.url if site else "unknown"
        site_name = site.name if site else "unknown"

        # All chat messages ordered chronologically
        messages = (
            session.query(ChatMessage)
            .filter(ChatMessage.issue_id == uuid.UUID(issue_id))
            .order_by(ChatMessage.created_at.asc())
            .all()
        )

        # All transitions for timeline
        transitions = (
            session.query(TicketTransition)
            .filter(TicketTransition.issue_id == uuid.UUID(issue_id))
            .order_by(TicketTransition.created_at.asc())
            .all()
        )

        # Build chat transcript
        transcript_parts = []
        for m in messages:
            if m.sender_type == SenderType.user:
                speaker = "Customer"
            elif m.agent_role:
                role_labels = {"pm": "PM Agent", "dev": "Dev Agent", "qa": "QA Agent", "tech_lead": "Tech Lead"}
                speaker = role_labels.get(m.agent_role, f"Agent ({m.agent_role})")
            else:
                speaker = "System"

            ts = m.created_at.strftime("%Y-%m-%d %H:%M UTC") if m.created_at else "unknown time"
            transcript_parts.append(f"[{ts}] {speaker}:\n{m.content}")

        # Build transition timeline
        transition_parts = []
        for t in transitions:
            from_col = t.from_col.value if t.from_col else "—"
            to_col = t.to_col.value if t.to_col else "—"
            ts = t.created_at.strftime("%Y-%m-%d %H:%M UTC") if t.created_at else "unknown time"
            note = f" ({t.note})" if t.note else ""
            transition_parts.append(f"[{ts}] {from_col} → {to_col} by {t.actor_type}{note}")

        return {
            "title": issue.title or "Untitled",
            "description": issue.description or "No description.",
            "site_url": site_url,
            "site_name": site_name,
            "dev_fail_count": issue.dev_fail_count,
            "transcript": "\n\n".join(transcript_parts),
            "transition_timeline": "\n".join(transition_parts),
        }


def _build_tech_lead_prompt(ctx: dict, reason: str) -> str:
    """Build the user prompt for the tech lead agent."""
    return (
        f"ESCALATION REASON: {reason}\n\n"
        f"Site: {ctx['site_name']} ({ctx['site_url']})\n"
        f"Dev Fail Count: {ctx['dev_fail_count']}\n\n"
        f"Issue Title: {ctx['title']}\n\n"
        f"Issue Description:\n{ctx['description']}\n\n"
        f"{'=' * 60}\n"
        f"TICKET TRANSITION TIMELINE:\n{ctx['transition_timeline']}\n\n"
        f"{'=' * 60}\n"
        f"FULL CHAT TRANSCRIPT:\n{ctx['transcript']}"
    )


@celery_app.task(name="src.tasks.tech_lead_agent.run")
def run(issue_id: str, reason: str = "") -> None:
    """
    Run the Tech Lead agent escalation for the given issue.

    Args:
        issue_id: UUID string of the issue.
        reason:   Human-readable reason for escalation (e.g. "dev_fail_count=3").
    """
    logger.info("[tech_lead] Escalation triggered for issue %s: %s", issue_id, reason)

    try:
        # 1. Post escalation announcement
        post_chat_message(
            issue_id,
            "👨‍💼 Tech Lead escalated. Reviewing history...",
            "tech_lead",
            DB_URL,
        )

        # 2. Fetch full issue history
        ctx = _fetch_full_history(issue_id, DB_URL)
        prompt = _build_tech_lead_prompt(ctx, reason or "Manual escalation")

        # 3. Call OpenClaw agent
        tl_response = call_llm(
            system_prompt=TECH_LEAD_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            timeout=180,
        ).strip()
        logger.info("[tech_lead] Got response for issue %s (%d chars)", issue_id, len(tl_response))

        # 4. Post tech lead analysis
        post_chat_message(issue_id, tl_response, "tech_lead", DB_URL)

        # 5. Transition to in_progress and re-enqueue dev agent
        try:
            transition_issue(
                issue_id=issue_id,
                to_col="in_progress",
                actor_type="tech_lead",
                note=f"Tech Lead reviewed and handed back to dev with corrected guidance. Reason: {reason}",
                db_url=DB_URL,
            )
        except Exception as e:
            logger.error("[tech_lead] Could not transition to in_progress: %s", e)

        _enqueue_dev_agent(issue_id)

    except Exception as e:
        logger.exception("[tech_lead] Unhandled error for issue %s: %s", issue_id, e)
        try:
            post_chat_message(
                issue_id,
                f"❌ Tech Lead agent encountered an error: {str(e)[:200]}. Manual review required.",
                "tech_lead",
                DB_URL,
            )
        except Exception:
            pass


def _enqueue_dev_agent(issue_id: str) -> None:
    """Enqueue dev agent to attempt fix with tech lead guidance in context."""
    try:
        celery_app.send_task(
            "src.tasks.dev_agent.run",
            args=[issue_id],
            queue="backend",
        )
        logger.info("[tech_lead] Dev agent re-enqueued for issue %s", issue_id)
    except Exception as e:
        logger.error("[tech_lead] Could not enqueue dev_agent for issue %s: %s", issue_id, e)
