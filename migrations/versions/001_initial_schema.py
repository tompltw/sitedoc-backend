"""Initial schema with all tables and Row-Level Security

Revision ID: 001_initial_schema
Revises: 
Create Date: 2025-01-01 00:00:00.000000

"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ─── ENUM TYPES ────────────────────────────────────────────────────────────
    op.execute("CREATE TYPE plan_type AS ENUM ('free', 'starter', 'pro', 'enterprise')")
    op.execute("CREATE TYPE site_status AS ENUM ('active', 'inactive', 'error')")
    op.execute("CREATE TYPE issue_status AS ENUM ('open', 'in_progress', 'resolved', 'dismissed')")
    op.execute("CREATE TYPE issue_priority AS ENUM ('low', 'medium', 'high', 'critical')")
    op.execute("CREATE TYPE action_status AS ENUM ('pending', 'running', 'completed', 'failed', 'rolled_back')")
    op.execute("CREATE TYPE sender_type AS ENUM ('user', 'agent', 'system')")
    op.execute("CREATE TYPE credential_type AS ENUM ('ssh', 'ftp', 'wp_admin', 'api_key')")

    # ─── CUSTOMERS ─────────────────────────────────────────────────────────────
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("stripe_customer_id", sa.String(255), unique=True),
        sa.Column("plan", sa.Enum("free", "starter", "pro", "enterprise", name="plan_type"), nullable=False, server_default="free"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_customers_email", "customers", ["email"])

    # ─── SITES ─────────────────────────────────────────────────────────────────
    op.create_table(
        "sites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.Enum("active", "inactive", "error", name="site_status"), nullable=False, server_default="active"),
        sa.Column("last_health_check", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_sites_customer_id", "sites", ["customer_id"])

    # ─── SITE_CREDENTIALS ──────────────────────────────────────────────────────
    op.create_table(
        "site_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("credential_type", sa.Enum("ssh", "ftp", "wp_admin", "api_key", name="credential_type"), nullable=False),
        sa.Column("encrypted_value", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    # ─── ISSUES ────────────────────────────────────────────────────────────────
    op.create_table(
        "issues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.Enum("open", "in_progress", "resolved", "dismissed", name="issue_status"), nullable=False, server_default="open"),
        sa.Column("priority", sa.Enum("low", "medium", "high", "critical", name="issue_priority"), nullable=False, server_default="medium"),
        sa.Column("confidence_score", sa.Float),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_issues_site_id", "issues", ["site_id"])
    op.create_index("ix_issues_customer_id", "issues", ["customer_id"])
    op.create_index("ix_issues_status", "issues", ["status"])

    # ─── AGENT_ACTIONS ─────────────────────────────────────────────────────────
    op.create_table(
        "agent_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_type", sa.String(100), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("status", sa.Enum("pending", "running", "completed", "failed", "rolled_back", name="action_status"), nullable=False, server_default="pending"),
        sa.Column("before_state", sa.Text),
        sa.Column("after_state", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_agent_actions_issue_id", "agent_actions", ["issue_id"])

    # ─── CHAT_MESSAGES ─────────────────────────────────────────────────────────
    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("issues.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_type", sa.Enum("user", "agent", "system", name="sender_type"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_chat_messages_issue_id", "chat_messages", ["issue_id"])

    # ─── CONVERSATIONS ─────────────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text),  # Rolling summary every 20 messages
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_conversations_site_id", "conversations", ["site_id"])
    op.create_index("ix_conversations_customer_id", "conversations", ["customer_id"])

    # ─── BACKUPS ───────────────────────────────────────────────────────────────
    op.create_table(
        "backups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("site_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sites.id", ondelete="CASCADE"), nullable=False),
        sa.Column("s3_path", sa.String(2048), nullable=False),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_backups_site_id", "backups", ["site_id"])

    # ─── ROW-LEVEL SECURITY ────────────────────────────────────────────────────
    # Enable RLS on all tenant-scoped tables
    for table in ["sites", "site_credentials", "issues", "agent_actions",
                  "chat_messages", "conversations", "backups"]:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # RLS policies: app sets current_setting('app.current_customer_id')
    # sites — customer sees only their sites
    op.execute("""
        CREATE POLICY customer_sites ON sites
        USING (customer_id = current_setting('app.current_customer_id')::uuid)
    """)

    # site_credentials — via site ownership
    op.execute("""
        CREATE POLICY customer_site_credentials ON site_credentials
        USING (
            site_id IN (
                SELECT id FROM sites
                WHERE customer_id = current_setting('app.current_customer_id')::uuid
            )
        )
    """)

    # issues
    op.execute("""
        CREATE POLICY customer_issues ON issues
        USING (customer_id = current_setting('app.current_customer_id')::uuid)
    """)

    # agent_actions — via issue ownership
    op.execute("""
        CREATE POLICY customer_agent_actions ON agent_actions
        USING (
            issue_id IN (
                SELECT id FROM issues
                WHERE customer_id = current_setting('app.current_customer_id')::uuid
            )
        )
    """)

    # chat_messages — via issue ownership
    op.execute("""
        CREATE POLICY customer_chat_messages ON chat_messages
        USING (
            issue_id IN (
                SELECT id FROM issues
                WHERE customer_id = current_setting('app.current_customer_id')::uuid
            )
        )
    """)

    # conversations
    op.execute("""
        CREATE POLICY customer_conversations ON conversations
        USING (customer_id = current_setting('app.current_customer_id')::uuid)
    """)

    # backups — via site ownership
    op.execute("""
        CREATE POLICY customer_backups ON backups
        USING (
            site_id IN (
                SELECT id FROM sites
                WHERE customer_id = current_setting('app.current_customer_id')::uuid
            )
        )
    """)

    # ─── UPDATED_AT TRIGGER for conversations ──────────────────────────────────
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER conversations_updated_at
        BEFORE UPDATE ON conversations
        FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    """)


def downgrade() -> None:
    # Drop triggers
    op.execute("DROP TRIGGER IF EXISTS conversations_updated_at ON conversations")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at()")

    # Drop RLS policies
    for table, policy in [
        ("sites", "customer_sites"),
        ("site_credentials", "customer_site_credentials"),
        ("issues", "customer_issues"),
        ("agent_actions", "customer_agent_actions"),
        ("chat_messages", "customer_chat_messages"),
        ("conversations", "customer_conversations"),
        ("backups", "customer_backups"),
    ]:
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    # Disable RLS
    for table in ["sites", "site_credentials", "issues", "agent_actions",
                  "chat_messages", "conversations", "backups"]:
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    # Drop tables in reverse FK order
    op.drop_table("backups")
    op.drop_table("conversations")
    op.drop_table("chat_messages")
    op.drop_table("agent_actions")
    op.drop_table("issues")
    op.drop_table("site_credentials")
    op.drop_table("sites")
    op.drop_table("customers")

    # Drop enums
    for enum in ["plan_type", "site_status", "issue_status", "issue_priority",
                 "action_status", "sender_type", "credential_type"]:
        op.execute(f"DROP TYPE IF EXISTS {enum}")
