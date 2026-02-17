"""
QA Agent Celery task — routed via OpenClaw (CLAWBOT).

Trigger: ticket moves to 'ready_for_qa' stage.

Flow:
  1. Transition to in_qa.
  2. Post "QA verification starting..." message.
  3. Fetch issue details + site URL.
  4. HTTP GET the site URL to check basic availability.
  5. Call OpenClaw agent to evaluate whether the fix appears resolved.
  6. Parse pass/fail JSON from the model.
     - Pass → transition to ready_for_uat, post success message.
     - Fail → transition to todo, post failure reason, enqueue dev_agent.
"""
import json
import logging
import os
import re
import uuid
from typing import Optional

import httpx
import requests

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

QA_SYSTEM_PROMPT = """You are a QA engineer verifying whether a website fix was successful. \
You will be given:
- The original issue description reported by the customer
- The fix applied by the dev agent
- The HTTP status returned by the site

Evaluate whether the fix appears to have resolved the issue based on this information.
Respond ONLY with a JSON object in this exact format (no other text):
{"passed": true, "reason": "brief explanation"}
or
{"passed": false, "reason": "brief explanation of what still seems wrong"}"""


def _fetch_qa_context(issue_id: str, db_url: str) -> dict:
    """
    Fetch issue description, site URL, and the last dev agent message.
    """
    from src.db.models import Issue, Site, ChatMessage, SenderType

    with get_db_session(db_url) as session:
        issue = session.get(Issue, uuid.UUID(issue_id))
        if issue is None:
            raise ValueError(f"Issue {issue_id} not found")

        site = session.get(Site, issue.site_id)
        site_url = site.url if site else None

        # Get the most recent dev agent message for fix context
        last_dev_msg = (
            session.query(ChatMessage)
            .filter(
                ChatMessage.issue_id == uuid.UUID(issue_id),
                ChatMessage.agent_role == "dev",
                ChatMessage.sender_type == SenderType.agent,
            )
            .order_by(ChatMessage.created_at.desc())
            .first()
        )

        return {
            "title": issue.title or "Untitled",
            "description": issue.description or "No description provided.",
            "site_url": site_url,
            "last_dev_message": last_dev_msg.content if last_dev_msg else "No dev message found.",
            "dev_fail_count": issue.dev_fail_count,
        }


def _http_check(site_url: str) -> tuple[int, str]:
    """
    Perform a basic HTTP GET on the site URL.
    Returns (status_code, summary_string).
    """
    if not site_url:
        return 0, "No site URL configured"
    try:
        resp = requests.get(site_url, timeout=15, allow_redirects=True)
        return resp.status_code, f"HTTP {resp.status_code}"
    except requests.exceptions.SSLError:
        return 0, "SSL error"
    except requests.exceptions.ConnectionError:
        return 0, "Connection refused / DNS failure"
    except requests.exceptions.Timeout:
        return 408, "Request timed out"
    except Exception as e:
        return 0, f"Error: {str(e)[:100]}"


def _parse_qa_result(text: str) -> Optional[dict]:
    """
    Extract the JSON QA result from the model's response.
    Returns dict with 'passed' and 'reason', or None on parse failure.
    """
    # Try to find a JSON object in the response
    pattern = r'\{[^{}]*"passed"\s*:\s*(true|false)[^{}]*\}'
    matches = re.findall(pattern, text, re.DOTALL)
    for raw in matches:
        try:
            data = json.loads(raw)
            if "passed" in data:
                return data
        except json.JSONDecodeError:
            continue
    # Fallback: try parsing the whole response as JSON
    try:
        data = json.loads(text.strip())
        if "passed" in data:
            return data
    except json.JSONDecodeError:
        pass
    return None


@celery_app.task(name="src.tasks.qa_agent.run")
def run(issue_id: str) -> None:
    """
    Run QA verification for the given issue.

    Args:
        issue_id: UUID string of the issue.
    """
    logger.info("[qa_agent] Starting QA for issue %s", issue_id)

    try:
        # 1. Transition to in_qa
        try:
            transition_issue(
                issue_id=issue_id,
                to_col="in_qa",
                actor_type="qa_agent",
                note="QA agent picking up ticket for verification.",
                db_url=DB_URL,
            )
        except Exception as e:
            logger.warning("[qa_agent] Could not transition to in_qa: %s", e)

        # 2. Post starting message
        post_chat_message(issue_id, "🧪 QA verification starting...", "qa", DB_URL)

        # 3. Fetch context
        ctx = _fetch_qa_context(issue_id, DB_URL)

        # 4. HTTP check
        http_status, http_summary = _http_check(ctx["site_url"])
        logger.info("[qa_agent] Site %s returned %s", ctx["site_url"], http_summary)

        # 5. Call OpenClaw agent
        qa_prompt = (
            f"Issue reported by customer:\n{ctx['description']}\n\n"
            f"Fix applied by dev agent:\n{ctx['last_dev_message']}\n\n"
            f"HTTP check result: {http_summary} (status code: {http_status})\n\n"
            f"Does this issue appear to be resolved?"
        )

        resp = httpx.post(
            CLAWBOT_URL,
            headers=CLAWBOT_HEADERS(),
            json={
                "model": f"openclaw:{CLAWBOT_AGENT_ID}",
                "max_tokens": 512,
                "messages": [
                    {"role": "system", "content": QA_SYSTEM_PROMPT},
                    {"role": "user", "content": qa_prompt},
                ],
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        qa_response_text = resp.json()["choices"][0]["message"]["content"].strip()
        logger.info("[qa_agent] QA response for issue %s: %s", issue_id, qa_response_text[:200])

        # 6. Parse result
        result = _parse_qa_result(qa_response_text)
        if result is None:
            # Can't parse — treat as failure to be safe
            logger.warning("[qa_agent] Could not parse QA result for issue %s, treating as fail", issue_id)
            result = {"passed": False, "reason": "QA agent could not parse verification result — manual review needed."}

        if result["passed"]:
            # --- QA PASSED ---
            try:
                transition_issue(
                    issue_id=issue_id,
                    to_col="ready_for_uat",
                    actor_type="qa_agent",
                    note=f"QA passed. {result.get('reason', '')}",
                    db_url=DB_URL,
                )
            except Exception as e:
                logger.error("[qa_agent] Could not transition to ready_for_uat: %s", e)

            post_chat_message(
                issue_id,
                "✅ QA passed. Ready for your review!",
                "qa",
                DB_URL,
            )
            logger.info("[qa_agent] Issue %s passed QA", issue_id)

        else:
            # --- QA FAILED ---
            reason = result.get("reason", "Verification failed.")
            logger.info("[qa_agent] Issue %s failed QA: %s", issue_id, reason)

            try:
                transition_issue(
                    issue_id=issue_id,
                    to_col="todo",
                    actor_type="qa_agent",
                    note=f"QA failed: {reason}",
                    db_url=DB_URL,
                )
            except Exception as e:
                logger.error("[qa_agent] Could not transition to todo: %s", e)

            post_chat_message(
                issue_id,
                f"❌ QA failed: {reason}. Sending back to dev.",
                "qa",
                DB_URL,
            )

            # Re-enqueue dev agent for another attempt
            _enqueue_dev_agent(issue_id)

    except Exception as e:
        logger.exception("[qa_agent] Unhandled error for issue %s: %s", issue_id, e)
        try:
            post_chat_message(
                issue_id,
                f"❌ QA agent encountered an error: {str(e)[:200]}. Please review manually.",
                "qa",
                DB_URL,
            )
        except Exception:
            pass


def _enqueue_dev_agent(issue_id: str) -> None:
    """Re-enqueue the dev agent after a QA failure."""
    try:
        celery_app.send_task(
            "src.tasks.dev_agent.run",
            args=[issue_id],
            queue="agent",
        )
        logger.info("[qa_agent] Re-enqueued dev_agent for issue %s", issue_id)
    except Exception as e:
        logger.error("[qa_agent] Could not re-enqueue dev_agent for issue %s: %s", issue_id, e)
