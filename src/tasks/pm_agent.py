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

from src.tasks.base import (
    celery_app,
    get_db_session,
    post_chat_message,
    transition_issue,
)
from src.tasks.llm import call_llm

logger = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://sitedoc:sitedoc@localhost:5432/sitedoc")

PM_SYSTEM_PROMPT_BASE = """You are a PM agent for SiteDoc, a managed website maintenance service.
You communicate directly with the customer. Be concise and professional.

## Critical rules
- NEVER mention internal systems, dashboards, ticket IDs, API endpoints, or session keys to the customer.
- NEVER ask the customer for information you already have (ticket ID, issue ID, etc.).
- NEVER say you "can't reach" something — you always have full access to transition the ticket.
- You CAN silently move the ticket to any stage without explaining the process to the customer.

## Ticket actions
To perform a ticket action, output a JSON block on its own line (it will be processed silently, not shown to the customer):

Move ticket to a new stage:
{{"ticket_action": "transition", "to_col": "<column>"}}

Confirm and create the ticket (moves to ready_for_uat_approval):
{{"ticket_confirmed": true, "title": "<short title>", "description": "<full structured description>"}}

Available columns and their meaning:
- todo          → queued for dev work (use this to send/resend to dev — triggers dev agent automatically)
- in_progress   → dev is actively working (do NOT use this manually — dev agent sets it)
- ready_for_qa  → dev done, needs QA
- ready_for_uat → QA passed, waiting for customer review
- done          → fully complete
- dismissed     → cancelled/rejected
- triage / ready_for_uat_approval → early stages

When user requests changes after reviewing a fix: use `todo` to send back to dev. Never use `in_progress` directly.

## Current ticket context
Issue ID: {issue_id}
Current stage: {kanban_column}
SSH credentials on file: {has_ssh}
Issue description (already submitted by customer):
<description>
{description}
</description>

## Triage stage behaviour
FIRST read the <description> block above in full. The customer already provided it when they opened the ticket.

The four things needed before confirming:
1. Clear description of the issue
2. Exact reproduction steps
3. Expected vs actual behaviour
4. SSH credentials (only if NOT already on file — check has_ssh above)

**Decision tree (follow exactly):**

CASE A — description covers items 1, 2, AND 3:
  → Do NOT ask any questions. Write ONE message that summarises what you understood
    (issue, steps, expected vs actual), then ask the customer to confirm so you can
    create the ticket. Example opener: "Thanks for the details — here's what I've got: …"
  → If SSH is already on file (has_ssh=yes): combine the summary + confirmation ask in
    one message. Do not ask about SSH.
  → If SSH is NOT on file: after the summary ask for SSH credentials in the same message.

CASE B — description is missing one or more of items 1–3:
  → Ask ONLY for the specific missing pieces in a single message. Never ask for
    information that is already present in the description.

RULE: Never send more than one "intake" message. Combine all gaps into one ask.
RULE: Never ask the customer to repeat something they already told you.

Once you have all details, confirm with the customer. When they confirm, emit the ticket_confirmed JSON.

## ready_for_uat stage (customer is reviewing the fix)
The customer has just reviewed the completed work. Two outcomes:
- Customer APPROVES: thank them, transition to `done`.
- Customer reports a PROBLEM or requests a CHANGE:
  1. Extract the EXACT issue they describe — be precise, include specific details (e.g. "greeting shows above the form, should be below").
  2. Emit a `ticket_action` to transition to `todo` so dev picks it up immediately.
  3. ALSO emit an `update_description` JSON with the appended feedback so dev knows exactly what to fix:
     {{"update_description": true, "append": "<user feedback verbatim + your clarification>"}}
  4. Tell the customer: "Got it, sending this back to the team to fix."
  Do NOT ask clarifying questions — act on what they said immediately.

## ready_for_uat_approval stage (waiting for customer to approve starting work)
Customer is reviewing the ticket summary before work begins. If they approve, transition to `todo`.
If they want changes to the plan, update accordingly and re-confirm.

## Other stages
If the customer asks about status, give a brief update based on the current stage.
If work is complete and the customer confirms it, transition to `done`."""


def _get_issue_context(issue_id: str, db_url: str) -> dict:
    """Fetch current issue stage and relevant context for the system prompt."""
    from src.db.models import Issue

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")
        return {
            "kanban_column": issue.kanban_column.value if issue.kanban_column else "triage",
            "title": issue.title or "",
            "description": issue.description or "",
        }


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


def _extract_transition_json(text: str) -> Optional[str]:
    """
    Look for a {"ticket_action": "transition", "to_col": "..."} block in the agent reply.
    Returns the target column string or None.
    """
    pattern = r'\{[^{}]*"ticket_action"\s*:\s*"transition"[^{}]*\}'
    matches = re.findall(pattern, text, re.DOTALL)
    for raw in matches:
        try:
            data = json.loads(raw)
            if data.get("ticket_action") == "transition" and data.get("to_col"):
                return data["to_col"]
        except json.JSONDecodeError:
            continue
    return None


def _extract_description_update(text: str) -> Optional[str]:
    """
    Look for {update_description: true, append: "..."} in agent reply.
    Returns the text to append, or None.
    """
    pattern = r'\{[^{}]*"update_description"\s*:\s*true[^{}]*\}'
    for raw in re.findall(pattern, text, re.DOTALL):
        try:
            data = json.loads(raw)
            if data.get("update_description") is True and data.get("append"):
                return data["append"]
        except json.JSONDecodeError:
            continue
    return None


def _append_issue_description(issue_id: str, append_text: str, db_url: str) -> None:
    """Append user feedback to the issue description so dev agent sees it."""
    from src.db.models import Issue

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            return
        existing = issue.description or ""
        issue.description = existing + f"\n\n---\n**Customer Feedback:**\n{append_text}"


def _strip_json_blocks(text: str) -> str:
    """Remove internal JSON action blocks from agent reply before showing to customer."""
    text = re.sub(r'\{[^{}]*"ticket_action"[^{}]*\}\n?', '', text)
    text = re.sub(r'\{[^{}]*"ticket_confirmed"[^{}]*\}\n?', '', text)
    text = re.sub(r'\{[^{}]*"update_description"[^{}]*\}\n?', '', text)
    return text.strip()


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
        # 1. Fetch existing conversation history
        history = _get_chat_history(issue_id, DB_URL)

        # 2. Fetch issue context + SSH status for the system prompt
        issue_ctx = _get_issue_context(issue_id, DB_URL)
        has_ssh = _has_ssh_credentials(issue_id, DB_URL)

        system_prompt = PM_SYSTEM_PROMPT_BASE.format(
            issue_id=issue_id,
            kanban_column=issue_ctx["kanban_column"],
            has_ssh="yes" if has_ssh else "no",
            description=issue_ctx.get("description", ""),
        )

        # 3. Build messages list — history + new user message
        messages = history + [{"role": "user", "content": user_message}]

        # 4. Call OpenClaw agent via gateway
        agent_reply = call_llm(
            system_prompt=system_prompt,
            messages=messages,
        ).strip()
        logger.info("[pm_agent] Got reply for issue %s (%d chars)", issue_id, len(agent_reply))

        # 5. Strip internal JSON blocks before posting to customer
        visible_reply = _strip_json_blocks(agent_reply)
        if visible_reply:
            post_chat_message(issue_id, visible_reply, "pm", DB_URL)

        # 6. Handle description update (append user feedback before transitioning)
        description_append = _extract_description_update(agent_reply)
        if description_append:
            try:
                _append_issue_description(issue_id, description_append, DB_URL)
                logger.info("[pm_agent] Appended feedback to issue %s description", issue_id)
            except Exception as e:
                logger.error("[pm_agent] Failed to update description for %s: %s", issue_id, e)

        # 7. Handle ticket_action transition (silent — not shown to customer)
        to_col = _extract_transition_json(agent_reply)
        if to_col:
            logger.info("[pm_agent] Transitioning issue %s → %s (ticket_action)", issue_id, to_col)
            try:
                transition_issue(
                    issue_id=issue_id,
                    to_col=to_col,
                    actor_type="pm_agent",
                    note=f"PM agent transitioned ticket to {to_col}.",
                )
            except Exception as te:
                logger.error("[pm_agent] ticket_action transition failed for %s → %s: %s", issue_id, to_col, te)

        # 7. Check for ticket confirmation JSON
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
                )
            except Exception as te:
                logger.error("[pm_agent] Transition failed for issue %s: %s", issue_id, te)

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
