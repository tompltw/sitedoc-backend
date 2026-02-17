"""
PM Agent Celery task — routed via OpenClaw (CLAWBOT).

Trigger: new customer message in triage / ready_for_uat_approval stage.

Flow:
  1. Fetch all existing chat messages for context.
  2. Build conversation history.
  3. Call OpenClaw agent with system prompt + history + new user message.
  4. Post agent reply to DB (agent_role='pm') + publish WebSocket event.
  5. Detect ticket-confirmation JSON in reply → update issue + transition.
"""
import json
import logging
import os
import re
import uuid
from typing import Optional

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

PM_SYSTEM_PROMPT = """You are a helpful PM agent for SiteDoc, a website maintenance service. \
Your job is to gather issue details from the customer so a dev agent can fix their site. \
Be concise and professional.

You MUST collect ALL of the following before creating the ticket:
1. A clear description of the issue
2. Exact reproduction steps
3. Expected behavior vs actual behavior

SSH credentials (if not already on file): if the customer's site has no SSH credentials stored, \
you must ask for SSH host, username, and password.

Once you have gathered all required details, confirm your understanding with the customer \
("I understand you're seeing X — is that correct?"). When the customer confirms, output \
a single JSON block on its own line in the following exact format:

{"ticket_confirmed": true, "title": "<short title>", "description": "<full structured description including steps, expected, actual>"}

Do NOT output this JSON until the customer has confirmed all details are correct."""


def _get_chat_history(issue_id: str, db_url: str) -> list[dict]:
    """
    Return Anthropic-format conversation history for this issue.
    Maps: sender_type='user' → role='user', sender_type='agent' → role='assistant'
    """
    from src.db.models import ChatMessage, SenderType

    with get_db_session(db_url) as session:
        messages = (
            session.query(ChatMessage)
            .filter(ChatMessage.issue_id == uuid.UUID(issue_id))
            .order_by(ChatMessage.created_at.asc())
            .all()
        )
        history = []
        for m in messages:
            role = "user" if m.sender_type == SenderType.user else "assistant"
            history.append({"role": role, "content": m.content})
        return history


def _has_ssh_credentials(issue_id: str, db_url: str) -> bool:
    """Check whether the site associated with this issue has SSH credentials on file."""
    from src.db.models import Issue, SiteCredential, CredentialType

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            return False
        cred = (
            session.query(SiteCredential)
            .filter(
                SiteCredential.site_id == issue.site_id,
                SiteCredential.credential_type == CredentialType.ssh,
            )
            .first()
        )
        return cred is not None


def _extract_ticket_json(text: str) -> Optional[dict]:
    """
    Look for a JSON block containing ticket_confirmed=true in the agent reply.
    Returns parsed dict or None.
    """
    # Match JSON objects that contain "ticket_confirmed"
    pattern = r'\{[^{}]*"ticket_confirmed"\s*:\s*true[^{}]*\}'
    matches = re.findall(pattern, text, re.DOTALL)
    for raw in matches:
        try:
            data = json.loads(raw)
            if data.get("ticket_confirmed") is True:
                return data
        except json.JSONDecodeError:
            continue
    return None


def _update_issue_from_ticket(issue_id: str, title: str, description: str, db_url: str) -> None:
    """Update issue title and description once ticket is confirmed."""
    from src.db.models import Issue

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")
        issue.title = title
        issue.description = description


@celery_app.task(name="src.tasks.pm_agent.handle_message")
def handle_message(issue_id: str, user_message: str) -> None:
    """
    Handle an incoming customer message for a triage-stage issue.

    Args:
        issue_id:    UUID string of the issue.
        user_message: Raw text of the customer's latest message.
    """
    logger.info("[pm_agent] Handling message for issue %s", issue_id)

    try:
        # 1. Fetch existing conversation history (excluding the incoming message, which isn't
        #    persisted yet from the agent's perspective)
        history = _get_chat_history(issue_id, DB_URL)

        # 2. Determine if SSH creds are available and include a hint in the system prompt
        has_ssh = _has_ssh_credentials(issue_id, DB_URL)
        system_prompt = PM_SYSTEM_PROMPT
        if not has_ssh:
            system_prompt += (
                "\n\nIMPORTANT: This site does NOT have SSH credentials on file. "
                "You MUST ask the customer for their SSH host, username, and password "
                "before the ticket can be worked on."
            )

        # 3. Build messages list — history + new user message
        messages = history + [{"role": "user", "content": user_message}]

        # 4. Call OpenClaw agent
        resp = httpx.post(
            CLAWBOT_URL,
            headers=CLAWBOT_HEADERS(),
            json={
                "model": f"openclaw:{CLAWBOT_AGENT_ID}",
                "max_tokens": 1024,
                "messages": [{"role": "system", "content": system_prompt}] + messages,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        agent_reply = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("[pm_agent] Got reply for issue %s (%d chars)", issue_id, len(agent_reply))

        # 5. Post agent reply to DB + WS
        post_chat_message(issue_id, agent_reply, "pm", DB_URL)

        # 6. Check for ticket confirmation JSON
        ticket_data = _extract_ticket_json(agent_reply)
        if ticket_data:
            title = ticket_data.get("title", "Untitled Issue")
            description = ticket_data.get("description", "")
            logger.info("[pm_agent] Ticket confirmed for issue %s — title: %s", issue_id, title)

            # Update issue record
            _update_issue_from_ticket(issue_id, title, description, DB_URL)

            # Transition to ready_for_uat_approval
            try:
                transition_issue(
                    issue_id=issue_id,
                    to_col="ready_for_uat_approval",
                    actor_type="pm_agent",
                    note="PM agent confirmed ticket details with customer.",
                    db_url=DB_URL,
                )
            except Exception as te:
                logger.error("[pm_agent] Transition failed for issue %s: %s", issue_id, te)
                post_chat_message(
                    issue_id,
                    "⚠️ I've confirmed the ticket details but had trouble updating the ticket stage. "
                    "Our team will follow up shortly.",
                    "pm",
                    DB_URL,
                )

    except Exception as e:
        logger.exception("[pm_agent] Unhandled error for issue %s: %s", issue_id, e)
        try:
            post_chat_message(
                issue_id,
                "⚠️ I encountered an unexpected error. Please try again or contact support.",
                "pm",
                DB_URL,
            )
        except Exception:
            pass
