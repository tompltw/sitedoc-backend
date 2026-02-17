"""
Dev Agent Celery task — spawns an isolated OpenClaw background agent session.

Trigger: ticket moves to 'todo' stage (customer approved work).

Flow (non-blocking):
  1. Transition issue to in_progress.
  2. Post "Starting diagnosis and fix..." message.
  3. Fetch issue context + decrypt credentials from DB.
  4. Build a comprehensive task prompt for the spawned agent.
  5. Fire sessions_spawn via OpenClaw /tools/invoke — returns in <1s.
  6. Celery task completes. The spawned agent runs async and calls back
     POST /api/v1/internal/agent-result when done.
"""
import base64
import logging
import os
import uuid

from src.tasks.base import (
    celery_app,
    get_db_session,
    post_chat_message,
    transition_issue,
)
from src.tasks.openclaw import spawn_agent

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")
INTERNAL_API_URL = os.getenv("INTERNAL_API_URL", "http://localhost:5000")
AGENT_INTERNAL_TOKEN = os.getenv("AGENT_INTERNAL_TOKEN", "")


# ---------------------------------------------------------------------------
# Credential decryption
# ---------------------------------------------------------------------------

def _get_fernet():
    """Build a Fernet instance from CREDENTIAL_ENCRYPTION_KEY (env/config)."""
    from cryptography.fernet import Fernet

    raw = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "changeme32byteskeyplaceholder123").encode()
    # Pad/truncate to exactly 32 bytes then base64url-encode → valid Fernet key
    key = base64.urlsafe_b64encode(raw[:32].ljust(32, b"\0"))
    return Fernet(key)


def _decrypt(encrypted_value: str) -> str:
    """Decrypt a Fernet-encrypted credential value."""
    try:
        return _get_fernet().decrypt(encrypted_value.encode()).decode()
    except Exception as e:
        logger.warning("[dev_agent] Could not decrypt credential: %s", e)
        return "(decryption failed)"


# ---------------------------------------------------------------------------
# Issue context
# ---------------------------------------------------------------------------

def _fetch_issue_context(issue_id: str, db_url: str) -> dict:
    """
    Fetch issue details and DECRYPTED site credentials from DB.
    Returns a rich context dict for prompt building.
    """
    from src.db.models import Issue, Site, SiteCredential

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")

        site = session.get(Site, issue.site_id) if issue.site_id else None
        site_url = site.url if site else "unknown"
        site_name = site.name if site else "unknown"

        creds = (
            session.query(SiteCredential)
            .filter(SiteCredential.site_id == issue.site_id)
            .all()
        ) if issue.site_id else []

        credential_map = {
            c.credential_type.value: _decrypt(c.encrypted_value)
            for c in creds
        }

        return {
            "issue_id": issue_id,
            "title": issue.title or "Untitled",
            "description": issue.description or "No description provided.",
            "site_url": site_url,
            "site_name": site_name,
            "dev_fail_count": issue.dev_fail_count,
            "credential_map": credential_map,
        }


# ---------------------------------------------------------------------------
# Task prompt builder
# ---------------------------------------------------------------------------

def _build_task_prompt(ctx: dict) -> str:
    """
    Build the full task prompt for the spawned OpenClaw sub-agent.
    Includes the issue context, credentials, what to do, and callback instructions.
    """
    cred_lines = "\n".join(
        f"  - {k}: {v}" for k, v in ctx["credential_map"].items()
    ) or "  (no credentials on file)"

    fail_note = (
        f"\n⚠️ This fix has failed QA {ctx['dev_fail_count']} time(s) previously. "
        "Try a different approach and be thorough.\n"
        if ctx["dev_fail_count"] > 0
        else ""
    )

    callback_url = f"{INTERNAL_API_URL}/api/v1/internal/agent-result"

    return f"""You are the Dev Agent for SiteDoc — a managed website maintenance service.
Your job is to IMPLEMENT the fix described below, not just plan it.
Use your tools (exec, browser, SSH) to actually apply the change.
{fail_note}
## Issue
Title: {ctx["title"]}
Site: {ctx["site_name"]} ({ctx["site_url"]})

## Description
{ctx["description"]}

## Credentials
{cred_lines}

## Instructions
1. Analyse the issue and determine the exact fix needed.
2. Use exec/browser/SSH to implement the fix on the live site.
3. Verify the fix works (check the page, run tests, etc.).
4. When finished, call the callback below — do NOT skip this step.

## Callback (REQUIRED — call this when done)
POST {callback_url}
Headers:
  Authorization: Bearer {AGENT_INTERNAL_TOKEN}
  Content-Type: application/json
Body (success):
{{
  "issue_id": "{ctx["issue_id"]}",
  "agent_role": "dev",
  "status": "success",
  "message": "<brief summary of what you did and how you verified it>",
  "transition_to": "ready_for_qa"
}}
Body (failure):
{{
  "issue_id": "{ctx["issue_id"]}",
  "agent_role": "dev",
  "status": "failure",
  "message": "<what you tried and why it failed>",
  "transition_to": null
}}

Start working now. Be thorough and verify before calling the callback.
"""


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------

@celery_app.task(name="src.tasks.dev_agent.run")
def run(issue_id: str) -> None:
    """
    Fire-and-forget: spawn an isolated OpenClaw agent session to implement
    the fix, then return immediately. The sub-agent calls back when done.
    """
    logger.info("[dev_agent] Spawning agent for issue %s", issue_id)

    try:
        # 1. Transition to in_progress
        try:
            transition_issue(
                issue_id=issue_id,
                to_col="in_progress",
                actor_type="dev_agent",
                note="Dev agent picking up ticket.",
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

        # 3. Fetch issue context (with decrypted credentials)
        ctx = _fetch_issue_context(issue_id, DB_URL)
        task_prompt = _build_task_prompt(ctx)

        # 4. Spawn isolated background agent (returns in <1s)
        result = spawn_agent(
            task=task_prompt,
            label=f"dev-agent-{issue_id[:8]}",
        )
        logger.info("[dev_agent] Agent spawned for issue %s: %s", issue_id, result)

        # 5. Notify chat that agent is running
        session_key = result.get("childSessionKey", "unknown")
        post_chat_message(
            issue_id,
            f"🤖 Dev agent is running (session: `{session_key}`). "
            "I'll update this ticket when the fix is applied.",
            "dev",
            DB_URL,
        )

    except Exception as e:
        logger.exception("[dev_agent] Failed to spawn agent for issue %s: %s", issue_id, e)
        try:
            post_chat_message(
                issue_id,
                f"❌ Dev agent encountered an error: {str(e)[:200]}. Please review manually.",
                "dev",
                DB_URL,
            )
        except Exception:
            pass
