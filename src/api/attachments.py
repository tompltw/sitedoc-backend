"""
Ticket attachment endpoints.

Files are stored on disk (not in chat) so agents and users can access them
independently of the conversation.
"""
import os
import uuid
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.db.models import Customer, Issue, TicketAttachment
from src.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

# Directory where uploaded files are stored
UPLOAD_DIR = Path("/Users/tombui/workspace/projects/sitedoc/sitedoc-backend/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 50 MB per file limit
MAX_FILE_SIZE = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_issue_for_customer(
    issue_id: str,
    customer: Customer,
    db: AsyncSession,
) -> Issue:
    """Fetch an issue and verify it belongs to the authenticated customer."""
    try:
        iid = uuid.UUID(issue_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid issue ID")

    result = await db.execute(
        select(Issue).where(Issue.id == iid, Issue.customer_id == customer.id)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=404, detail="Issue not found")
    return issue


def _attachment_dict(att: TicketAttachment, issue_id: str) -> dict:
    return {
        "id": str(att.id),
        "issue_id": str(att.issue_id),
        "filename": att.filename,
        "mime_type": att.mime_type,
        "size_bytes": att.size_bytes,
        "uploaded_by": att.uploaded_by,
        "created_at": att.created_at.isoformat() if att.created_at else None,
        "download_url": f"/api/v1/issues/{issue_id}/attachments/{att.id}/download",
    }


# ---------------------------------------------------------------------------
# POST /api/v1/issues/{issue_id}/attachments
# ---------------------------------------------------------------------------

@router.post("/issues/{issue_id}/attachments", status_code=status.HTTP_201_CREATED)
async def upload_attachment(
    issue_id: str,
    file: UploadFile = File(...),
    customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file attachment to an issue."""
    issue = await _get_issue_for_customer(issue_id, customer, db)

    # Read and size-check
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large (max 50 MB)")

    # Build a UUID-based stored filename preserving extension
    original_name = file.filename or "upload"
    ext = Path(original_name).suffix  # e.g. ".pdf"
    stored_name = f"{uuid.uuid4()}{ext}"
    dest = UPLOAD_DIR / stored_name

    # Write to disk
    dest.write_bytes(contents)

    # Persist metadata
    attachment = TicketAttachment(
        issue_id=issue.id,
        filename=original_name,
        stored_name=stored_name,
        mime_type=file.content_type,
        size_bytes=len(contents),
        uploaded_by="user",
    )
    db.add(attachment)
    await db.commit()
    await db.refresh(attachment)

    logger.info(
        "[attachments] Uploaded %s (%d bytes) for issue %s",
        original_name, len(contents), issue_id,
    )
    return _attachment_dict(attachment, issue_id)


# ---------------------------------------------------------------------------
# GET /api/v1/issues/{issue_id}/attachments
# ---------------------------------------------------------------------------

@router.get("/issues/{issue_id}/attachments")
async def list_attachments(
    issue_id: str,
    customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """List all attachments for an issue."""
    issue = await _get_issue_for_customer(issue_id, customer, db)

    result = await db.execute(
        select(TicketAttachment)
        .where(TicketAttachment.issue_id == issue.id)
        .order_by(TicketAttachment.created_at.asc())
    )
    attachments = result.scalars().all()
    return [_attachment_dict(a, issue_id) for a in attachments]


# ---------------------------------------------------------------------------
# GET /api/v1/issues/{issue_id}/attachments/{attachment_id}/download
# No auth required so agents can curl the URL directly
# ---------------------------------------------------------------------------

@router.get("/issues/{issue_id}/attachments/{attachment_id}/download")
async def download_attachment(
    issue_id: str,
    attachment_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Stream/download an attachment file. No auth required (agents need access)."""
    try:
        att_uuid = uuid.UUID(attachment_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid attachment ID")

    result = await db.execute(
        select(TicketAttachment).where(TicketAttachment.id == att_uuid)
    )
    attachment = result.scalar_one_or_none()
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    file_path = UPLOAD_DIR / attachment.stored_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(
        path=str(file_path),
        media_type=attachment.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{attachment.filename}"',
        },
    )


# ---------------------------------------------------------------------------
# DELETE /api/v1/issues/{issue_id}/attachments/{attachment_id}
# ---------------------------------------------------------------------------

@router.delete("/issues/{issue_id}/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    issue_id: str,
    attachment_id: str,
    customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    """Delete an attachment (removes file from disk and DB record)."""
    # Verify issue belongs to customer
    await _get_issue_for_customer(issue_id, customer, db)

    try:
        att_uuid = uuid.UUID(attachment_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid attachment ID")

    result = await db.execute(
        select(TicketAttachment).where(TicketAttachment.id == att_uuid)
    )
    attachment = result.scalar_one_or_none()
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Delete from disk
    file_path = UPLOAD_DIR / attachment.stored_name
    if file_path.exists():
        file_path.unlink()
        logger.info("[attachments] Deleted file %s from disk", attachment.stored_name)

    # Delete DB record
    await db.delete(attachment)
    await db.commit()

    logger.info("[attachments] Deleted attachment %s for issue %s", attachment_id, issue_id)
