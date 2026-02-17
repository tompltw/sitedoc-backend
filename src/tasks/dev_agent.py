"""
Dev Agent Celery task — routed via OpenClaw (CLAWBOT).

Trigger: ticket moves to 'todo' stage (customer approved work).

Flow:
  1. Transition issue to in_progress.
  2. Post "Starting diagnosis and fix..." message.
  3. Fetch issue details + site credentials from DB.
  4. Call OpenClaw agent with issue context for diagnosis and fix plan.
  5. Post dev agent diagnostic response (agent_role='dev').
  6. Transition issue to ready_for_qa.
  7. Post "Fix applied — sending to QA..." message.
  8. Enqueue QA agent task.
"""
import logging
import os
import uuid

import httpx

from src.tasks.base import (
    celery_app,
    get_db_session,
    post_chat_message,
    transition_issue,
)

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")
CLAWBOT_AGENT_ID = os.getenv("CLAWBOT_AGENT_ID", "main")
CLAWBOT_URL = os.getenv("CLAWBOT_BASE_URL", "http://127.0.0.1:18789/v1") + "/chat/completions"
CLAWBOT_HEADERS = lambda: {
    "Authorization": f"Bearer {os.getenv('CLAWBOT_TOKEN', '')}",
    "Content-Type": "application/json",
    "x-openclaw-agent-id": CLAWBOT_AGENT_ID,
}

DEV_SYSTEM_PROMPT = """You are an expert full-stack developer fixing a website issue for a \
managed website maintenance service called SiteDoc. You have been given an issue report from \
a customer. Analyze the issue thoroughly and describe exactly what you would do to fix it.

Be specific and technical:
- Identify the root cause
- List exact file paths that need to be changed
- Show specific code changes or commands to run
- Describe verification steps to confirm the fix worked

Format your response in clear sections: Root Cause, Fix Plan, Verification Steps."""


def _fetch_issue_context(issue_id: str, db_url: str) -> dict:
    """
    Fetch issue details and site credentials for the dev agent.
    Returns a dict with title, description, site_url, credentials summary.
    """
    from src.db.models import Issue, Site, SiteCredential, CredentialType

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")

        site = session.get(Site, issue.site_id)
        site_url = site.url if site else "unknown"
        site_name = site.name if site else "unknown"

        # Gather credential types (not values — values are encrypted)
        creds = (
            session.query(SiteCredential)
            .filter(SiteCredential.site_id == issue.site_id)
            .all()
        )
        cred_types = [c.credential_type.value for c in creds] if creds else []

        return {
            "issue_id": issue_id,
            "title": issue.title or "Untitled",
            "description": issue.description or "No description provided.",
            "site_url": site_url,
            "site_name": site_name,
            "dev_fail_count": issue.dev_fail_count,
            "credential_types": cred_types,
        }


def _build_dev_prompt(ctx: dict) -> str:
    """Build the user-facing prompt for the dev agent."""
    cred_info = (
        f"Available credential types: {', '.join(ctx['credential_types'])}"
        if ctx["credential_types"]
        else "No credentials on file."
    )
    fail_note = (
        f"\nNote: This issue has failed QA {ctx['dev_fail_count']} time(s) previously. "
        "Review carefully and try a different approach."
        if ctx["dev_fail_count"] > 0
        else ""
    )

    return (
        f"Site: {ctx['site_name']} ({ctx['site_url']})\n"
        f"{cred_info}\n\n"
        f"Issue Title: {ctx['title']}\n\n"
        f"Issue Description:\n{ctx['description']}"
        f"{fail_note}"
    )


@celery_app.task(name="src.tasks.dev_agent.run")
def run(issue_id: str) -> None:
    """
    Run the dev agent for the given issue.

    Args:
        issue_id: UUID string of the issue.
    """
    logger.info("[dev_agent] Starting work on issue %s", issue_id)

    try:
        # 1. Transition to in_progress
        try:
            transition_issue(
                issue_id=issue_id,
                to_col="in_progress",
                actor_type="dev_agent",
                note="Dev agent picking up ticket.",
                db_url=DB_URL,
            )
        except Exception as e:
            logger.warning("[dev_agent] Could not transition to in_progress: %s", e)

        # 2. Post starting message
        post_chat_message(
            issue_id,
            "🔧 Starting diagnosis and fix...",
            "dev",
            DB_URL,
        )

        # 3. Fetch issue context
        ctx = _fetch_issue_context(issue_id, DB_URL)
        user_prompt = _build_dev_prompt(ctx)

        # 4. Call OpenClaw agent
        resp = httpx.post(
            CLAWBOT_URL,
            headers=CLAWBOT_HEADERS(),
            json={
                "model": f"openclaw:{CLAWBOT_AGENT_ID}",
                "max_tokens": 4096,
                "messages": [
                    {"role": "system", "content": DEV_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        dev_response = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("[dev_agent] Got diagnostic response for issue %s (%d chars)", issue_id, len(dev_response))

        # 5. Post diagnostic response
        post_chat_message(issue_id, dev_response, "dev", DB_URL)

        # 6. Transition to ready_for_qa
        try:
            transition_issue(
                issue_id=issue_id,
                to_col="ready_for_qa",
                actor_type="dev_agent",
                note="Dev agent completed fix. Sending to QA.",
                db_url=DB_URL,
            )
        except Exception as e:
            logger.error("[dev_agent] Could not transition to ready_for_qa: %s", e)
            post_chat_message(
                issue_id,
                "⚠️ Fix was applied but I had trouble updating the ticket stage. Notifying the team.",
                "dev",
                DB_URL,
            )
            return

        # 7. Post completion message
        post_chat_message(
            issue_id,
            "✅ Fix applied. Sending to QA for verification...",
            "dev",
            DB_URL,
        )

        # 8. Enqueue QA agent
        _enqueue_qa_agent(issue_id)

    except Exception as e:
        logger.exception("[dev_agent] Unhandled error for issue %s: %s", issue_id, e)
        try:
            post_chat_message(
                issue_id,
                f"❌ Dev agent encountered an error: {str(e)[:200]}. Please review manually.",
                "dev",
                DB_URL,
            )
        except Exception:
            pass


def _enqueue_qa_agent(issue_id: str) -> None:
    """Fire-and-forget: enqueue the QA agent task."""
    try:
        celery_app.send_task(
            "src.tasks.qa_agent.run",
            args=[issue_id],
            queue="agent",
        )
        logger.info("[dev_agent] QA agent enqueued for issue %s", issue_id)
    except Exception as e:
        logger.error("[dev_agent] Could not enqueue QA agent for issue %s: %s", issue_id, e)
