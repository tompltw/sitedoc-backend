"""
WebSocket endpoint for real-time fix progress + issue updates.

Architecture:
  Celery worker publishes events → Redis pub/sub channel → WebSocket → browser

Channel naming:
  sitedoc:issue:{issue_id}   — events for a specific issue

Event types pushed to clients:
  { "type": "action_started",   "action_id": "...", "action_type": "...", "description": "..." }
  { "type": "action_completed", "action_id": "...", "output": "..." }
  { "type": "action_failed",    "action_id": "...", "error": "..." }
  { "type": "action_rolled_back", "action_id": "..." }
  { "type": "issue_status",     "status": "resolved|rolled_back|open|in_progress" }
  { "type": "diagnosis_ready",  "confidence": 0.85, "actions_count": 3 }
  { "type": "message",          "role": "agent|system", "content": "..." }
  { "type": "ping" }            — keepalive
"""
import asyncio
import json
import logging
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status
from fastapi.websockets import WebSocketState

from src.core.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


def _redis_channel(issue_id: str) -> str:
    return f"sitedoc:issue:{issue_id}"


class ConnectionManager:
    """Manages active WebSocket connections per issue."""

    def __init__(self):
        # issue_id → set of active WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, issue_id: str, ws: WebSocket) -> None:
        await ws.accept()
        if issue_id not in self._connections:
            self._connections[issue_id] = set()
        self._connections[issue_id].add(ws)
        logger.info("[ws] Connected to issue %s (total: %d)", issue_id, len(self._connections[issue_id]))

    def disconnect(self, issue_id: str, ws: WebSocket) -> None:
        if issue_id in self._connections:
            self._connections[issue_id].discard(ws)
            if not self._connections[issue_id]:
                del self._connections[issue_id]
        logger.info("[ws] Disconnected from issue %s", issue_id)

    async def broadcast(self, issue_id: str, message: dict) -> None:
        if issue_id not in self._connections:
            return
        dead = set()
        for ws in self._connections[issue_id]:
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_json(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._connections[issue_id].discard(ws)


manager = ConnectionManager()


async def _redis_subscriber(issue_id: str, ws: WebSocket) -> None:
    """
    Subscribe to Redis pub/sub channel for this issue.
    Forwards all events to the WebSocket client.
    Exits when WebSocket disconnects or Redis connection drops.
    """
    redis_url = settings.REDIS_URL
    redis_client: Optional[aioredis.Redis] = None
    pubsub = None

    try:
        redis_client = await aioredis.from_url(redis_url, decode_responses=True)
        pubsub = redis_client.pubsub()
        channel = _redis_channel(issue_id)
        await pubsub.subscribe(channel)
        logger.info("[ws] Subscribed to Redis channel: %s", channel)

        async for message in pubsub.listen():
            if ws.client_state != WebSocketState.CONNECTED:
                break

            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    await ws.send_json(data)
                except Exception as e:
                    logger.warning("[ws] Failed to forward message: %s", e)

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error("[ws] Redis subscriber error for issue %s: %s", issue_id, e)
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe()
                await pubsub.close()
            except Exception:
                pass
        if redis_client:
            try:
                await redis_client.aclose()
            except Exception:
                pass


@router.websocket("/ws/issues/{issue_id}")
async def issue_websocket(issue_id: str, websocket: WebSocket) -> None:
    """
    WebSocket endpoint for real-time issue updates.

    Client connects → receives live events as fix pipeline executes:
    - diagnosis_ready (confidence score, action count)
    - action_started / action_completed / action_failed
    - issue_status changes
    - agent chat messages

    Auth: pass JWT as ?token=<access_token> query param.
    (Standard WS clients can't set Authorization header.)
    """
    # Auth check via query param
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Validate token
    try:
        from src.core.security import decode_token
        payload = decode_token(token)
        customer_id = payload.get("sub")
        if not customer_id:
            raise ValueError("No sub in token")
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # Verify customer owns this issue
    try:
        from sqlalchemy import text
        from src.db.session import async_session_factory
        async with async_session_factory() as db:
            result = await db.execute(
                text("SELECT 1 FROM issues WHERE id = :issue_id AND customer_id = :customer_id"),
                {"issue_id": issue_id, "customer_id": customer_id}
            )
            if not result.fetchone():
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                return
    except Exception as e:
        logger.error("[ws] Auth DB check failed: %s", e)
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return

    await manager.connect(issue_id, websocket)

    # Send current issue state immediately on connect
    try:
        from sqlalchemy import text
        from src.db.session import async_session_factory
        async with async_session_factory() as db:
            result = await db.execute(
                text("""
                    SELECT status, confidence_score, kanban_column,
                           (SELECT count(*) FROM agent_actions WHERE issue_id = :issue_id) as action_count
                    FROM issues WHERE id = :issue_id
                """),
                {"issue_id": issue_id}
            )
            row = result.fetchone()
            if row:
                await websocket.send_json({
                    "type": "connected",
                    "issue_id": issue_id,
                    "status": row[0],
                    "confidence": float(row[1]) if row[1] else None,
                    "kanban_column": row[2],
                    "actions_count": row[3],
                })
    except Exception as e:
        logger.warning("[ws] Could not send initial state: %s", e)

    # Start Redis subscriber in background
    subscriber_task = asyncio.create_task(_redis_subscriber(issue_id, websocket))

    # Keepalive ping every 30s + listen for client messages
    try:
        while True:
            try:
                # Wait for client message or timeout (keepalive)
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                # Echo pings back
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        await websocket.send_json({"type": "pong"})
                except Exception:
                    pass
            except asyncio.TimeoutError:
                # Send keepalive ping
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({"type": "ping"})
            except WebSocketDisconnect:
                break

    finally:
        subscriber_task.cancel()
        manager.disconnect(issue_id, websocket)


# ---------------------------------------------------------------------------
# Publisher helper — used by Celery tasks to push events
# ---------------------------------------------------------------------------

def publish_event(issue_id: str, event: dict) -> None:
    """
    Synchronous publisher for use inside Celery tasks.
    Publishes to Redis pub/sub → forwarded to connected WebSocket clients.
    """
    import redis as sync_redis
    import os

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    try:
        r = sync_redis.from_url(redis_url, decode_responses=True)
        channel = _redis_channel(issue_id)
        r.publish(channel, json.dumps(event))
        r.close()
    except Exception as e:
        logger.warning("[ws] Failed to publish event for issue %s: %s", issue_id, e)
