"""
Pydantic v2 request/response schemas for the SiteDoc API.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr

from src.db.models import CredentialType, IssueStatus, IssuePriority, SiteStatus, SenderType, PlanType


# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class CustomerResponse(BaseModel):
    id: uuid.UUID
    email: str
    plan: PlanType
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Site schemas
# ---------------------------------------------------------------------------

class SiteCreate(BaseModel):
    url: str
    name: str


class SiteResponse(BaseModel):
    id: uuid.UUID
    customer_id: uuid.UUID
    url: str
    name: str
    status: SiteStatus
    last_health_check: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class CredentialCreate(BaseModel):
    credential_type: CredentialType
    value: str


class CredentialResponse(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    credential_type: CredentialType
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Issue schemas
# ---------------------------------------------------------------------------

class IssueCreate(BaseModel):
    site_id: uuid.UUID
    title: str
    description: Optional[str] = None
    priority: IssuePriority = IssuePriority.medium


class IssueResponse(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    customer_id: uuid.UUID
    title: str
    description: Optional[str] = None
    status: IssueStatus
    priority: IssuePriority
    confidence_score: Optional[float] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class IssueStatusUpdate(BaseModel):
    status: IssueStatus


# ---------------------------------------------------------------------------
# Chat schemas
# ---------------------------------------------------------------------------

class MessageCreate(BaseModel):
    content: str


class MessageResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    sender_type: SenderType
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}
