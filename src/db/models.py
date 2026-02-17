"""
SQLAlchemy ORM models for SiteDoc.
Multi-tenant architecture — RLS enforced at PostgreSQL level.
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Column, String, DateTime, ForeignKey, Text, Integer,
    Float, Enum, BigInteger, func
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class PlanType(PyEnum):
    free = "free"
    starter = "starter"
    pro = "pro"
    enterprise = "enterprise"


class SiteStatus(PyEnum):
    active = "active"
    inactive = "inactive"
    error = "error"


class IssueStatus(PyEnum):
    open = "open"
    in_progress = "in_progress"
    pending_approval = "pending_approval"
    resolved = "resolved"
    dismissed = "dismissed"


class IssuePriority(PyEnum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"


class ActionStatus(PyEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    rolled_back = "rolled_back"


class SenderType(PyEnum):
    user = "user"
    agent = "agent"
    system = "system"


class CredentialType(PyEnum):
    ssh = "ssh"
    ftp = "ftp"
    wp_admin = "wp_admin"
    api_key = "api_key"


class Customer(Base):
    __tablename__ = "customers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    stripe_customer_id = Column(String(255), unique=True)
    plan = Column(Enum(PlanType, name="plan_type", create_type=False), nullable=False, default=PlanType.free)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    sites = relationship("Site", back_populates="customer", cascade="all, delete-orphan")
    issues = relationship("Issue", back_populates="customer")
    conversations = relationship("Conversation", back_populates="customer")


class Site(Base):
    __tablename__ = "sites"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    url = Column(String(2048), nullable=False)
    name = Column(String(255), nullable=False)
    status = Column(Enum(SiteStatus, name="site_status", create_type=False), nullable=False, default=SiteStatus.active)
    last_health_check = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    customer = relationship("Customer", back_populates="sites")
    credentials = relationship("SiteCredential", back_populates="site", cascade="all, delete-orphan")
    issues = relationship("Issue", back_populates="site")
    conversations = relationship("Conversation", back_populates="site")
    backups = relationship("Backup", back_populates="site")


class SiteCredential(Base):
    __tablename__ = "site_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    credential_type = Column(Enum(CredentialType, name="credential_type", create_type=False), nullable=False)
    encrypted_value = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site = relationship("Site", back_populates="credentials")


class Issue(Base):
    __tablename__ = "issues"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(500), nullable=False)
    description = Column(Text)
    status = Column(Enum(IssueStatus, name="issue_status", create_type=False), nullable=False, default=IssueStatus.open)
    priority = Column(Enum(IssuePriority, name="issue_priority", create_type=False), nullable=False, default=IssuePriority.medium)
    confidence_score = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True))

    site = relationship("Site", back_populates="issues")
    customer = relationship("Customer", back_populates="issues")
    agent_actions = relationship("AgentAction", back_populates="issue", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="issue", cascade="all, delete-orphan")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False)
    action_type = Column(String(100), nullable=False)
    description = Column(Text)
    status = Column(Enum(ActionStatus, name="action_status", create_type=False), nullable=False, default=ActionStatus.pending)
    before_state = Column(Text)  # JSON snapshot
    after_state = Column(Text)   # JSON snapshot
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    issue = relationship("Issue", back_populates="agent_actions")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False)
    sender_type = Column(Enum(SenderType, name="sender_type", create_type=False), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    issue = relationship("Issue", back_populates="chat_messages")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    customer_id = Column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    summary = Column(Text)  # Rolling summary — updated every 20 messages
    message_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    site = relationship("Site", back_populates="conversations")
    customer = relationship("Customer", back_populates="conversations")


class Backup(Base):
    __tablename__ = "backups"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    s3_path = Column(String(2048), nullable=False)
    size_bytes = Column(BigInteger)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site = relationship("Site", back_populates="backups")
