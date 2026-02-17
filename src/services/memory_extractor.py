"""
Memory extraction service.

Runs on every incoming message (async, fire-and-forget via Celery).
Uses Claude Haiku to classify and extract structured data from messages.
Stores results in conversation_memory table (Layer 1).
Also enqueues vector embedding for RAG (Layer 2).
"""
import json
import logging
from typing import Optional
from uuid import UUID

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from src.core.config import settings

logger = logging.getLogger(__name__)

# Haiku client — cheap, fast, fire-and-forget
_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


EXTRACTION_SYSTEM = """You are a message classifier and data extractor for a website maintenance AI.
Extract structured information from the user message.

Return ONLY valid JSON in this exact format:
{
  "categories": ["credential"|"task"|"decision"|"preference"|"file_url"|"general"],
  "extractions": [
    {
      "category": "credential",
      "payload": {
        "type": "wordpress|ssh|ftp|api_key|other",
        "url": "...",
        "username": "...",
        "password": "...",
        "notes": "..."
      }
    },
    {
      "category": "task",
      "payload": {
        "description": "...",
        "priority": "low|medium|high",
        "target": "url or site reference"
      }
    },
    {
      "category": "decision",
      "payload": {
        "key": "short_key_snake_case",
        "value": "...",
        "description": "..."
      }
    },
    {
      "category": "preference",
      "payload": {
        "key": "short_key_snake_case",
        "value": "...",
        "description": "..."
      }
    },
    {
      "category": "file_url",
      "payload": {
        "url": "...",
        "type": "repo|page|file|doc|other",
        "description": "..."
      }
    }
  ]
}

Rules:
- Only extract categories present in the message
- credentials: logins, API keys, tokens, passwords, SSH details
- tasks: explicit requests to fix/build/change something
- decisions: choices made (theme colors, tech stack, approach)
- preferences: always/never rules, coding style, tooling choices
- file_url: links, repo URLs, page references
- If nothing extractable, return {"categories": ["general"], "extractions": []}
- Be conservative — only extract what's clearly stated"""


async def extract_and_store(
    db: AsyncSession,
    conversation_id: UUID,
    customer_id: UUID,
    site_id: Optional[UUID],
    message_content: str,
    message_id: Optional[UUID] = None,
) -> dict:
    """
    Extract structured memory from a message and store it.
    Called async from Celery worker — does NOT block the response.
    """
    try:
        client = get_client()

        # Call Haiku for classification + extraction
        response = await client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=EXTRACTION_SYSTEM,
            messages=[{"role": "user", "content": message_content}],
        )

        raw = response.content[0].text.strip()

        # Parse JSON response
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(raw[start:end])
            else:
                logger.warning("Haiku returned non-JSON: %s", raw[:200])
                return {"stored": 0}

        extractions = result.get("extractions", [])
        stored = 0

        for extraction in extractions:
            category = extraction.get("category", "general")
            if category == "general":
                continue  # Don't store general chat

            payload = extraction.get("payload", {})
            if not payload:
                continue

            await db.execute(
                text("""
                    INSERT INTO conversation_memory
                        (conversation_id, customer_id, site_id, category, payload,
                         source_message_id, extracted_by)
                    VALUES
                        (:conversation_id, :customer_id, :site_id, :category, :payload::jsonb,
                         :source_message_id, 'haiku')
                """),
                {
                    "conversation_id": str(conversation_id),
                    "customer_id": str(customer_id),
                    "site_id": str(site_id) if site_id else None,
                    "category": category,
                    "payload": json.dumps(payload),
                    "source_message_id": str(message_id) if message_id else None,
                }
            )
            stored += 1

        await db.commit()

        # Update memory_last_synced_at on the conversation
        await db.execute(
            text("""
                UPDATE conversations
                SET memory_last_synced_at = now()
                WHERE id = :conversation_id
            """),
            {"conversation_id": str(conversation_id)}
        )
        await db.commit()

        logger.info(
            "Extracted %d items from message (conversation=%s)",
            stored, conversation_id
        )
        return {"stored": stored, "categories": result.get("categories", [])}

    except Exception as e:
        logger.error("Memory extraction failed: %s", e, exc_info=True)
        return {"stored": 0, "error": str(e)}


async def assemble_context(
    db: AsyncSession,
    conversation_id: UUID,
    customer_id: UUID,
    current_message: str,
    recent_n: int = 5,
    rag_top_k: int = 5,
) -> dict:
    """
    Assemble the hybrid context for an agent call.

    Returns:
        {
            "structured_memory": {...},  # Layer 1
            "recent_messages": [...],    # Last N messages
            "rag_results": [...],        # Layer 2 (vector search)
            "token_estimate": int
        }
    """
    # Layer 1: Structured memory by category
    memory_result = await db.execute(
        text("""
            SELECT category, payload, updated_at
            FROM conversation_memory
            WHERE conversation_id = :conv_id
              AND customer_id = :customer_id
              AND is_active = true
            ORDER BY updated_at DESC
        """),
        {"conv_id": str(conversation_id), "customer_id": str(customer_id)}
    )
    memory_rows = memory_result.fetchall()

    structured_memory: dict = {
        "credentials": [],
        "tasks": [],
        "decisions": [],
        "preferences": [],
        "file_urls": [],
    }
    for row in memory_rows:
        cat = row[0]
        payload = row[1]
        if cat == "credential":
            structured_memory["credentials"].append(payload)
        elif cat == "task":
            structured_memory["tasks"].append(payload)
        elif cat == "decision":
            structured_memory["decisions"].append(payload)
        elif cat == "preference":
            structured_memory["preferences"].append(payload)
        elif cat == "file_url":
            structured_memory["file_urls"].append(payload)

    # Recent messages (always include for continuity)
    from src.db.models import ChatMessage
    recent_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.issue_id.in_(
            # Get issues for this conversation's site
            text("SELECT id FROM issues WHERE site_id = (SELECT site_id FROM conversations WHERE id = :conv_id)")
        ))
        .order_by(ChatMessage.created_at.desc())
        .limit(recent_n),
        {"conv_id": str(conversation_id)}
    )
    recent_msgs = recent_result.scalars().all()
    recent_messages = [
        {
            "role": msg.sender_type.value,
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
        }
        for msg in reversed(recent_msgs)
    ]

    # Layer 2: Vector search (RAG fallback)
    # Only run if pgvector has embeddings for this conversation
    rag_results = []
    try:
        rag_result = await db.execute(
            text("""
                SELECT message_content, sender_type,
                       1 - (embedding <=> (
                           SELECT embedding FROM message_embeddings
                           WHERE conversation_id = :conv_id
                           ORDER BY created_at DESC LIMIT 1
                       )) AS similarity
                FROM message_embeddings
                WHERE conversation_id = :conv_id
                ORDER BY similarity DESC
                LIMIT :top_k
            """),
            {"conv_id": str(conversation_id), "top_k": rag_top_k}
        )
        rag_rows = rag_result.fetchall()
        rag_results = [
            {"content": r[0], "role": r[1], "similarity": float(r[2])}
            for r in rag_rows
            if r[2] is not None and float(r[2]) > 0.7  # Only high-similarity results
        ]
    except Exception:
        # pgvector not ready or no embeddings yet — skip silently
        pass

    # Rough token estimate
    import json as _json
    context_str = _json.dumps({
        "structured_memory": structured_memory,
        "recent_messages": recent_messages,
        "rag_results": rag_results,
    })
    token_estimate = len(context_str) // 4  # ~4 chars per token

    return {
        "structured_memory": structured_memory,
        "recent_messages": recent_messages,
        "rag_results": rag_results,
        "token_estimate": token_estimate,
    }
