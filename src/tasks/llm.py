"""
LLM helper — routes all model calls through the OpenClaw gateway
/v1/chat/completions endpoint (OpenAI-compatible).

No ANTHROPIC_API_KEY needed. Uses the local OpenClaw token.
"""
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OPENCLAW_GATEWAY_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
OPENCLAW_GATEWAY_TOKEN = os.getenv("OPENCLAW_GATEWAY_TOKEN", "0c3fa3ffecad310e0cf6a5c579ca9aed70d0343e1daccbe1")
OPENCLAW_AGENT_ID = os.getenv("OPENCLAW_AGENT_ID", "sitedoc")


class LLMResponse:
    """Wraps LLM response content + token usage metadata."""
    def __init__(self, content: str, model: str, prompt_tokens: int, completion_tokens: int):
        self.content = content
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens

    def __str__(self) -> str:
        return self.content


def call_llm(
    system_prompt: str,
    messages: list[dict],
    model: Optional[str] = None,  # ignored — OpenClaw uses its configured model
    timeout: int = 300,
) -> "LLMResponse":
    """
    Call the OpenClaw gateway's /v1/chat/completions endpoint.

    Args:
        system_prompt: System instruction for the agent.
        messages: List of {"role": "user"|"assistant", "content": str} dicts.
        model: Ignored (OpenClaw routes through its configured model).
        timeout: Request timeout in seconds.

    Returns:
        LLMResponse with .content (str), .model, .prompt_tokens, .completion_tokens, .total_tokens.

    Raises:
        RuntimeError: On HTTP error or unexpected response shape.
    """
    url = f"{OPENCLAW_GATEWAY_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENCLAW_GATEWAY_TOKEN}",
        "Content-Type": "application/json",
        "x-openclaw-agent-id": OPENCLAW_AGENT_ID,
    }

    # Prepend system message if provided
    full_messages = []
    if system_prompt:
        full_messages.append({"role": "user", "content": f"[SYSTEM]\n{system_prompt}\n[/SYSTEM]"})
        full_messages.append({"role": "assistant", "content": "Understood. I will follow these instructions."})
    full_messages.extend(messages)

    payload = {
        "model": "openclaw",
        "messages": full_messages,
        "stream": False,
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        model_name = data.get("model", "openclaw")
        return LLMResponse(
            content=content,
            model=model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
    except requests.exceptions.Timeout:
        raise RuntimeError(f"OpenClaw gateway timed out after {timeout}s")
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"OpenClaw gateway HTTP error: {e} — {resp.text[:300]}")
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected response shape from OpenClaw: {e}")
