"""
Chat routes — messages per issue + memory extraction.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.api.schemas import MessageCreate, MessageResponse
from src.db.models import ChatMessage, Customer, Issue, SenderType
from src.db.session import get_db

router = APIRouter()


@router.get("/issues/{issue_id}/context")
async def get_conversation_context(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """
    Debug endpoint — returns the assembled hybrid context the agent would see
    for this issue's conversation. Useful for frontend context debug view.
    """
    from sqlalchemy import text
    from src.services.memory_extractor import assemble_context

    # Verify issue belongs to customer
    result = await db.execute(
        select(Issue).where(Issue.id == issue_id, Issue.customer_id == current_customer.id)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")

    # Get conversation for this site
    conv_result = await db.execute(
        text("""
            SELECT id FROM conversations
            WHERE site_id = :site_id AND customer_id = :customer_id
            ORDER BY updated_at DESC LIMIT 1
        """),
        {"site_id": str(issue.site_id), "customer_id": str(current_customer.id)}
    )
    conv_row = conv_result.fetchone()

    if not conv_row:
        return {
            "structured_memory": {"credentials": [], "tasks": [], "decisions": [], "preferences": [], "file_urls": []},
            "recent_messages": [],
            "rag_results": [],
            "token_estimate": 0,
            "conversation_id": None,
        }

    context = await assemble_context(
        db=db,
        conversation_id=uuid.UUID(str(conv_row[0])),
        customer_id=current_customer.id,
        current_message="",
        recent_n=5,
        rag_top_k=5,
    )
    context["conversation_id"] = str(conv_row[0])
    return context


async def _get_issue_for_customer(
    issue_id: uuid.UUID,
    customer_id: uuid.UUID,
    db: AsyncSession,
) -> Issue:
    result = await db.execute(
        select(Issue).where(Issue.id == issue_id, Issue.customer_id == customer_id)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    return issue


@router.get("/issues/{issue_id}/messages", response_model=list[MessageResponse])
async def list_messages(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    await _get_issue_for_customer(issue_id, current_customer.id, db)

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.issue_id == issue_id)
        .order_by(ChatMessage.created_at.asc())
    )
    return result.scalars().all()


@router.post("/issues/{issue_id}/messages", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def post_message(
    issue_id: uuid.UUID,
    body: MessageCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    issue = await _get_issue_for_customer(issue_id, current_customer.id, db)

    # Check if this is the first message — if so, enqueue the diagnose task
    count_result = await db.execute(
        select(func.count()).select_from(ChatMessage).where(ChatMessage.issue_id == issue_id)
    )
    message_count = count_result.scalar_one()
    is_first_message = message_count == 0

    message = ChatMessage(
        issue_id=issue_id,
        sender_type=SenderType.user,
        content=body.content,
    )
    db.add(message)
    await db.flush()
    await db.refresh(message)

    # Resolve conversation_id for memory extraction
    # (conversation is site-scoped; use or create one)
    conversation_id = await _get_or_create_conversation(
        db=db,
        site_id=issue.site_id,
        customer_id=current_customer.id,
    )

    # Fire-and-forget: extract memory from this message (Layer 1 + Layer 2)
    _enqueue_memory_extraction(
        conversation_id=str(conversation_id),
        customer_id=str(current_customer.id),
        site_id=str(issue.site_id),
        message_content=body.content,
        message_id=str(message.id),
        sender_type="user",
    )

    if is_first_message:
        _enqueue_diagnose_task(issue_id=str(issue_id))

    # Always enqueue chat reply for every user message
    _enqueue_chat_reply(issue_id=str(issue_id), message_content=body.content)

    return message


async def _get_or_create_conversation(
    db: AsyncSession,
    site_id: uuid.UUID,
    customer_id: uuid.UUID,
) -> uuid.UUID:
    """Get existing open conversation for site, or create a new one."""
    from src.db.models import Conversation
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.site_id == site_id,
            Conversation.customer_id == customer_id,
        )
        .order_by(Conversation.updated_at.desc())
        .limit(1)
    )
    conv = result.scalar_one_or_none()
    if conv:
        return conv.id

    conv = Conversation(site_id=site_id, customer_id=customer_id)
    db.add(conv)
    await db.flush()
    await db.refresh(conv)
    return conv.id


def _enqueue_memory_extraction(
    conversation_id: str,
    customer_id: str,
    site_id: str,
    message_content: str,
    message_id: str,
    sender_type: str,
) -> None:
    """Fire-and-forget: extract structured memory from the message."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.memory_extraction.extract_message_memory",
            args=[conversation_id, customer_id, site_id, message_content, message_id],
            queue="memory",
        )
        celery_app.send_task(
            "src.tasks.memory_extraction.embed_message",
            args=[conversation_id, customer_id, message_content, sender_type],
            queue="memory",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not enqueue memory extraction (Celery unavailable?)"
        )


def _enqueue_chat_reply(issue_id: str, message_content: str) -> None:
    """Fire-and-forget: enqueue a Celery chat reply task for the user message."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.chat_reply.reply_to_user",
            args=[issue_id, message_content],
            queue="agent",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Could not enqueue chat reply for issue %s", issue_id)


def _enqueue_diagnose_task(issue_id: str) -> None:
    """Fire-and-forget: enqueue a Celery diagnose task for the issue."""
    try:
        from celery import Celery
        import os
        celery_app = Celery(broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
        celery_app.send_task(
            "src.tasks.diagnose.diagnose_issue",
            args=[issue_id],
            queue="agent",
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Could not enqueue diagnose task for issue %s (Celery unavailable?)", issue_id
        )
