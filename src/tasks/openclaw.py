"""
OpenClaw gateway helpers for spawning isolated background agent sessions.

Uses the /tools/invoke HTTP endpoint (no blocking LLM call).
Returns immediately; the spawned agent runs async and calls back when done.
"""
import logging
import os

import requests

logger = logging.getLogger(__name__)

OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
RUN_TIMEOUT_SECONDS = int(os.getenv("AGENT_RUN_TIMEOUT_SECONDS", "900"))  # 15 min default


def spawn_agent(
    task: str,
    label: str | None = None,
    run_timeout_seconds: int | None = None,
) -> dict:
    """
    Spawn an isolated OpenClaw background agent session via /tools/invoke.

    Non-blocking — returns { status: "accepted", runId, childSessionKey }
    within milliseconds. The agent runs asynchronously.

    Args:
        task:                 Full task prompt for the sub-agent.
        label:                Optional human-readable label (e.g. "dev-agent-<issue_id>").
        run_timeout_seconds:  Hard kill after N seconds (default: AGENT_RUN_TIMEOUT_SECONDS env).

    Returns:
        The parsed JSON result from /tools/invoke.

    Raises:
        RuntimeError: On HTTP error or unexpected response.
    """
    timeout = run_timeout_seconds if run_timeout_seconds is not None else RUN_TIMEOUT_SECONDS

    url = f"{OPENCLAW_GATEWAY_URL}/tools/invoke"
    headers = {
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "tool": "sessions_spawn",
        "args": {
            "task": task,
            **({"label": label} if label else {}),
            "runTimeoutSeconds": timeout,
            "cleanup": "keep",  # keep transcript for debugging
        },
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"sessions_spawn failed: {data}")
        logger.info("[openclaw] Spawned agent session: %s", data.get("result"))
        return data["result"]
    except requests.exceptions.Timeout:
        raise RuntimeError("OpenClaw /tools/invoke timed out (30s) — gateway unreachable?")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"OpenClaw /tools/invoke HTTP error: {e} — {resp.text[:300]}")
