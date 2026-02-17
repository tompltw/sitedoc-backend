"""Initial schema with all tables and Row-Level Security

Revision ID: 001_initial_schema
Revises: 
Create Date: 2026-02-16 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    -- ENUM TYPES
    CREATE TYPE plan_type AS ENUM ('free', 'starter', 'pro', 'enterprise');
    CREATE TYPE site_status AS ENUM ('active', 'inactive', 'error');
    CREATE TYPE issue_status AS ENUM ('open', 'in_progress', 'resolved', 'dismissed');
    CREATE TYPE issue_priority AS ENUM ('low', 'medium', 'high', 'critical');
    CREATE TYPE action_status AS ENUM ('pending', 'running', 'completed', 'failed', 'rolled_back');
    CREATE TYPE sender_type AS ENUM ('user', 'agent', 'system');
    CREATE TYPE credential_type AS ENUM ('ssh', 'ftp', 'wp_admin', 'api_key');

    -- CUSTOMERS
    CREATE TABLE customers (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        email VARCHAR(255) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        stripe_customer_id VARCHAR(255) UNIQUE,
        plan plan_type NOT NULL DEFAULT 'free',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_customers_email ON customers (email);

    -- SITES
    CREATE TABLE sites (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        url VARCHAR(2048) NOT NULL,
        name VARCHAR(255) NOT NULL,
        status site_status NOT NULL DEFAULT 'active',
        last_health_check TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_sites_customer_id ON sites (customer_id);

    -- SITE CREDENTIALS
    CREATE TABLE site_credentials (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        credential_type credential_type NOT NULL,
        encrypted_value TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_site_credentials_site_id ON site_credentials (site_id);

    -- ISSUES
    CREATE TABLE issues (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        title VARCHAR(500) NOT NULL,
        description TEXT,
        status issue_status NOT NULL DEFAULT 'open',
        priority issue_priority NOT NULL DEFAULT 'medium',
        confidence_score FLOAT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        resolved_at TIMESTAMPTZ
    );
    CREATE INDEX ix_issues_site_id ON issues (site_id);
    CREATE INDEX ix_issues_customer_id ON issues (customer_id);
    CREATE INDEX ix_issues_status ON issues (status);

    -- AGENT ACTIONS
    CREATE TABLE agent_actions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        issue_id UUID NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        action_type VARCHAR(100) NOT NULL,
        description TEXT,
        status action_status NOT NULL DEFAULT 'pending',
        before_state JSONB,
        after_state JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_agent_actions_issue_id ON agent_actions (issue_id);

    -- CONVERSATIONS
    CREATE TABLE conversations (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        summary TEXT,
        message_count INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_conversations_site_id ON conversations (site_id);
    CREATE INDEX ix_conversations_customer_id ON conversations (customer_id);

    -- CHAT MESSAGES
    CREATE TABLE chat_messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        issue_id UUID REFERENCES issues(id) ON DELETE CASCADE,
        conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
        sender_type sender_type NOT NULL,
        content TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_chat_messages_issue_id ON chat_messages (issue_id);
    CREATE INDEX ix_chat_messages_conversation_id ON chat_messages (conversation_id);

    -- BACKUPS
    CREATE TABLE backups (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        s3_path VARCHAR(2048),
        size_bytes BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_backups_site_id ON backups (site_id);

    -- ROW-LEVEL SECURITY
    ALTER TABLE customers ENABLE ROW LEVEL SECURITY;
    ALTER TABLE sites ENABLE ROW LEVEL SECURITY;
    ALTER TABLE site_credentials ENABLE ROW LEVEL SECURITY;
    ALTER TABLE issues ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_actions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
    ALTER TABLE chat_messages ENABLE ROW LEVEL SECURITY;
    ALTER TABLE backups ENABLE ROW LEVEL SECURITY;

    CREATE POLICY customers_isolation ON customers
        USING (id = current_setting('app.current_customer_id', true)::UUID);

    CREATE POLICY sites_isolation ON sites
        USING (customer_id = current_setting('app.current_customer_id', true)::UUID);

    CREATE POLICY site_credentials_isolation ON site_credentials
        USING (site_id IN (
            SELECT id FROM sites
            WHERE customer_id = current_setting('app.current_customer_id', true)::UUID
        ));

    CREATE POLICY issues_isolation ON issues
        USING (customer_id = current_setting('app.current_customer_id', true)::UUID);

    CREATE POLICY conversations_isolation ON conversations
        USING (customer_id = current_setting('app.current_customer_id', true)::UUID);

    CREATE POLICY backups_isolation ON backups
        USING (site_id IN (
            SELECT id FROM sites
            WHERE customer_id = current_setting('app.current_customer_id', true)::UUID
        ));

    -- Auto-update updated_at for conversations
    CREATE OR REPLACE FUNCTION update_updated_at_column()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = now();
        RETURN NEW;
    END;
    $$ language 'plpgsql';

    CREATE TRIGGER update_conversations_updated_at
        BEFORE UPDATE ON conversations
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS backups CASCADE;
    DROP TABLE IF EXISTS chat_messages CASCADE;
    DROP TABLE IF EXISTS conversations CASCADE;
    DROP TABLE IF EXISTS agent_actions CASCADE;
    DROP TABLE IF EXISTS issues CASCADE;
    DROP TABLE IF EXISTS site_credentials CASCADE;
    DROP TABLE IF EXISTS sites CASCADE;
    DROP TABLE IF EXISTS customers CASCADE;
    DROP TYPE IF EXISTS plan_type;
    DROP TYPE IF EXISTS site_status;
    DROP TYPE IF EXISTS issue_status;
    DROP TYPE IF EXISTS issue_priority;
    DROP TYPE IF EXISTS action_status;
    DROP TYPE IF EXISTS sender_type;
    DROP TYPE IF EXISTS credential_type;
    DROP FUNCTION IF EXISTS update_updated_at_column();
    """)
