"""Multi-agent pipeline: kanban columns, site_agents, ticket_transitions

Revision ID: 004_multi_agent_pipeline
Revises: 003_pending_approval_status
Create Date: 2026-02-17

Changes:
- New enum: kanban_column (9 stages)
- issues table: kanban_column, dev_fail_count, ticket_number, pm_agent_id, dev_agent_id, stall_check_at
- New table: site_agents (per-site PM + Dev agent records)
- New table: ticket_transitions (audit log of all stage movements)
- chat_messages: agent_role column
"""
from typing import Sequence, Union
from alembic import op

revision: str = "004_multi_agent_pipeline"
down_revision: Union[str, None] = "003_pending_approval_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    -- 1. New kanban_column enum
    CREATE TYPE kanban_column AS ENUM (
        'triage',
        'ready_for_uat_approval',
        'todo',
        'in_progress',
        'ready_for_qa',
        'in_qa',
        'ready_for_uat',
        'done',
        'dismissed'
    );

    -- 2. site_agents table (must exist before FK refs below)
    CREATE TABLE site_agents (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        site_id UUID NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
        agent_role VARCHAR(20) NOT NULL CHECK (agent_role IN ('pm', 'dev', 'qa', 'tech_lead')),
        model VARCHAR(100) NOT NULL DEFAULT 'claude-haiku-4-5',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_site_agents_site_id ON site_agents (site_id);

    -- 3. issues table additions
    ALTER TABLE issues
        ADD COLUMN kanban_column kanban_column NOT NULL DEFAULT 'triage',
        ADD COLUMN dev_fail_count INTEGER NOT NULL DEFAULT 0,
        ADD COLUMN ticket_number BIGSERIAL,
        ADD COLUMN pm_agent_id UUID REFERENCES site_agents(id) ON DELETE SET NULL,
        ADD COLUMN dev_agent_id UUID REFERENCES site_agents(id) ON DELETE SET NULL,
        ADD COLUMN stall_check_at TIMESTAMPTZ;

    CREATE INDEX ix_issues_kanban_column ON issues (kanban_column);

    -- Migrate existing status → kanban_column
    UPDATE issues SET kanban_column = CASE
        WHEN status = 'open'             THEN 'triage'::kanban_column
        WHEN status = 'in_progress'      THEN 'in_progress'::kanban_column
        WHEN status = 'pending_approval' THEN 'ready_for_uat'::kanban_column
        WHEN status = 'resolved'         THEN 'done'::kanban_column
        WHEN status = 'dismissed'        THEN 'dismissed'::kanban_column
        ELSE 'triage'::kanban_column
    END;

    -- 4. ticket_transitions table (audit log)
    CREATE TABLE ticket_transitions (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        issue_id UUID NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        from_col kanban_column,
        to_col kanban_column NOT NULL,
        actor_type VARCHAR(20) NOT NULL CHECK (
            actor_type IN ('customer', 'pm_agent', 'dev_agent', 'qa_agent', 'tech_lead', 'system')
        ),
        actor_id UUID,
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX ix_ticket_transitions_issue_id ON ticket_transitions (issue_id);

    -- 5. chat_messages: add agent_role
    ALTER TABLE chat_messages ADD COLUMN agent_role VARCHAR(20)
        CHECK (agent_role IN ('pm', 'dev', 'qa', 'tech_lead') OR agent_role IS NULL);

    -- 6. RLS policies for new tables
    ALTER TABLE site_agents ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ticket_transitions ENABLE ROW LEVEL SECURITY;

    CREATE POLICY site_agents_isolation ON site_agents
        USING (site_id IN (
            SELECT id FROM sites
            WHERE customer_id = current_setting('app.current_customer_id', true)::UUID
        ));

    CREATE POLICY ticket_transitions_isolation ON ticket_transitions
        USING (issue_id IN (
            SELECT id FROM issues
            WHERE customer_id = current_setting('app.current_customer_id', true)::UUID
        ));
    """)


def downgrade() -> None:
    op.execute("""
    DROP TABLE IF EXISTS ticket_transitions CASCADE;
    DROP TABLE IF EXISTS site_agents CASCADE;

    ALTER TABLE issues
        DROP COLUMN IF EXISTS kanban_column,
        DROP COLUMN IF EXISTS dev_fail_count,
        DROP COLUMN IF EXISTS ticket_number,
        DROP COLUMN IF EXISTS pm_agent_id,
        DROP COLUMN IF EXISTS dev_agent_id,
        DROP COLUMN IF EXISTS stall_check_at;

    ALTER TABLE chat_messages DROP COLUMN IF EXISTS agent_role;

    DROP TYPE IF EXISTS kanban_column;
    """)
