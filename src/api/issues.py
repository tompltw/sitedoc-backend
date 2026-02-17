"""
Issues routes.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_current_customer
from src.api.schemas import IssueCreate, IssueResponse, IssueStatusUpdate
from src.db.models import Customer, Issue, IssueStatus, Site
from src.db.session import get_db

router = APIRouter()


@router.get("/", response_model=list[IssueResponse])
async def list_issues(
    site_id: Optional[uuid.UUID] = Query(None),
    status: Optional[IssueStatus] = Query(None),
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    query = select(Issue).where(Issue.customer_id == current_customer.id)

    if site_id is not None:
        query = query.where(Issue.site_id == site_id)
    if status is not None:
        query = query.where(Issue.status == status)

    query = query.order_by(Issue.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/", response_model=IssueResponse, status_code=status.HTTP_201_CREATED)
async def create_issue(
    body: IssueCreate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    # Verify site belongs to customer
    result = await db.execute(
        select(Site).where(Site.id == body.site_id, Site.customer_id == current_customer.id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Site not found")

    issue = Issue(
        site_id=body.site_id,
        customer_id=current_customer.id,
        title=body.title,
        description=body.description,
        priority=body.priority,
    )
    db.add(issue)
    await db.flush()
    await db.refresh(issue)
    return issue


@router.get("/{issue_id}", response_model=IssueResponse)
async def get_issue(
    issue_id: uuid.UUID,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Issue).where(Issue.id == issue_id, Issue.customer_id == current_customer.id)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")
    return issue


@router.patch("/{issue_id}/status", response_model=IssueResponse)
async def update_issue_status(
    issue_id: uuid.UUID,
    body: IssueStatusUpdate,
    current_customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone

    result = await db.execute(
        select(Issue).where(Issue.id == issue_id, Issue.customer_id == current_customer.id)
    )
    issue = result.scalar_one_or_none()
    if issue is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found")

    issue.status = body.status
    if body.status == IssueStatus.resolved and issue.resolved_at is None:
        issue.resolved_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(issue)
    return issue
