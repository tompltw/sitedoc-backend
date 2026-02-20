"""
Pydantic v2 request/response schemas for the SiteDoc API.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional, Union

from pydantic import BaseModel, EmailStr

from src.db.models import CredentialType, IssueStatus, IssuePriority, SiteStatus, SenderType, PlanType, KanbanColumn, AgentRole


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
    # Accept either a plain string or a JSON-serialisable dict.
    # The backend will serialise dicts to JSON before encrypting.
    value: Union[str, dict[str, Any]]


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
    kanban_column: KanbanColumn = KanbanColumn.triage
    dev_fail_count: int = 0
    ticket_number: Optional[int] = None
    stall_check_at: Optional[datetime] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class IssueStatusUpdate(BaseModel):
    status: IssueStatus


# ---------------------------------------------------------------------------
# Agent action schemas
# ---------------------------------------------------------------------------

class AgentActionResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    action_type: str
    description: Optional[str] = None
    status: str
    before_state: Optional[str] = None
    after_state: Optional[str] = None
    error_detail: Optional[str] = None
    # Token tracking
    model_used: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class IssueTransitionRequest(BaseModel):
    to_col: KanbanColumn
    note: Optional[str] = None


class TicketTransitionResponse(BaseModel):
    id: uuid.UUID
    issue_id: uuid.UUID
    from_col: Optional[KanbanColumn] = None
    to_col: KanbanColumn
    actor_type: str
    actor_id: Optional[uuid.UUID] = None
    note: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Site agent schemas
# ---------------------------------------------------------------------------

class SiteAgentCreate(BaseModel):
    agent_role: AgentRole
    model: str = "claude-haiku-4-5"


class SiteAgentResponse(BaseModel):
    id: uuid.UUID
    site_id: uuid.UUID
    agent_role: str
    model: str
    created_at: datetime

    model_config = {"from_attributes": True}


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
    agent_role: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Billing schemas
# ---------------------------------------------------------------------------

class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id: str


class BillingPortalResponse(BaseModel):
    portal_url: str
