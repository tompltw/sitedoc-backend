"""
SQLAlchemy ORM models for SiteDoc.
Multi-tenant architecture — RLS enforced at PostgreSQL level.
"""
import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Column, String, DateTime, ForeignKey, Text, Integer,
    Float, Enum, BigInteger, func, text as sa_text
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


class KanbanColumn(PyEnum):
    triage = "triage"
    ready_for_uat_approval = "ready_for_uat_approval"
    todo = "todo"
    in_progress = "in_progress"
    ready_for_qa = "ready_for_qa"
    in_qa = "in_qa"
    ready_for_uat = "ready_for_uat"
    done = "done"
    dismissed = "dismissed"


class AgentRole(PyEnum):
    pm = "pm"
    dev = "dev"
    qa = "qa"
    tech_lead = "tech_lead"


class TransitionActorType(PyEnum):
    customer = "customer"
    pm_agent = "pm_agent"
    dev_agent = "dev_agent"
    qa_agent = "qa_agent"
    tech_lead = "tech_lead"
    system = "system"


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
    database = "database"
    cpanel = "cpanel"
    wp_app_password = "wp_app_password"


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
    plugin_token = Column(String(128), unique=True, index=True)
    plugin_version = Column(String(32))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    customer = relationship("Customer", back_populates="sites")
    credentials = relationship("SiteCredential", back_populates="site", cascade="all, delete-orphan")
    issues = relationship("Issue", back_populates="site")
    conversations = relationship("Conversation", back_populates="site")
    backups = relationship("Backup", back_populates="site")
    agents = relationship("SiteAgent", back_populates="site", cascade="all, delete-orphan")


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
    # Pipeline columns
    kanban_column = Column(Enum(KanbanColumn, name="kanban_column", create_type=False), nullable=False, default=KanbanColumn.triage)
    dev_fail_count = Column(Integer, nullable=False, default=0)
    ticket_number = Column(BigInteger, server_default=sa_text("nextval('issues_ticket_number_seq')"))
    pm_agent_id = Column(UUID(as_uuid=True), ForeignKey("site_agents.id", ondelete="SET NULL"))
    dev_agent_id = Column(UUID(as_uuid=True), ForeignKey("site_agents.id", ondelete="SET NULL"))
    stall_check_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at = Column(DateTime(timezone=True))

    site = relationship("Site", back_populates="issues")
    customer = relationship("Customer", back_populates="issues")
    agent_actions = relationship("AgentAction", back_populates="issue", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="issue", cascade="all, delete-orphan")
    transitions = relationship("TicketTransition", back_populates="issue", cascade="all, delete-orphan")
    attachments = relationship("TicketAttachment", back_populates="issue", cascade="all, delete-orphan")


class SiteAgent(Base):
    __tablename__ = "site_agents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    site_id = Column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"), nullable=False)
    agent_role = Column(String(20), nullable=False)
    model = Column(String(100), nullable=False, default="claude-haiku-4-5")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    site = relationship("Site", back_populates="agents")


class TicketTransition(Base):
    __tablename__ = "ticket_transitions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False)
    from_col = Column(Enum(KanbanColumn, name="kanban_column", create_type=False))
    to_col = Column(Enum(KanbanColumn, name="kanban_column", create_type=False), nullable=False)
    actor_type = Column(String(20), nullable=False)
    actor_id = Column(UUID(as_uuid=True))
    note = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    issue = relationship("Issue", back_populates="transitions")


class AgentAction(Base):
    __tablename__ = "agent_actions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False)
    action_type = Column(String(100), nullable=False)
    description = Column(Text)
    status = Column(Enum(ActionStatus, name="action_status", create_type=False), nullable=False, default=ActionStatus.pending)
    before_state = Column(Text)  # JSON snapshot
    after_state = Column(Text)   # JSON snapshot
    # Token / cost tracking
    model_used = Column(String(100), nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    issue = relationship("Issue", back_populates="agent_actions")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False)
    sender_type = Column(Enum(SenderType, name="sender_type", create_type=False), nullable=False)
    content = Column(Text, nullable=False)
    agent_role = Column(String(20))  # 'pm' | 'dev' | 'qa' | 'tech_lead' | None (user)
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


class TicketAttachment(Base):
    __tablename__ = "ticket_attachments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issue_id = Column(UUID(as_uuid=True), ForeignKey("issues.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(255), nullable=False)       # original filename
    stored_name = Column(String(255), nullable=False)    # UUID-based stored filename
    mime_type = Column(String(100))
    size_bytes = Column(Integer)
    uploaded_by = Column(String, default="user")         # "user" or agent role
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    issue = relationship("Issue", back_populates="attachments")
