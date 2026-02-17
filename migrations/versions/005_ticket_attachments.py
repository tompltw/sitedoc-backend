"""Add ticket_attachments table

Revision ID: 005_ticket_attachments
Revises: 004_multi_agent_pipeline
Create Date: 2025-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '005_ticket_attachments'
down_revision = '004_multi_agent_pipeline'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'ticket_attachments',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('issue_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('issues.id', ondelete='CASCADE'), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('stored_name', sa.String(255), nullable=False),
        sa.Column('mime_type', sa.String(100)),
        sa.Column('size_bytes', sa.Integer()),
        sa.Column('uploaded_by', sa.String(), server_default='user'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
    )

    op.create_index('ix_ticket_attachments_issue_id', 'ticket_attachments', ['issue_id'])

    # RLS policy — attachments are accessible if the parent issue is accessible
    op.execute("""
    ALTER TABLE ticket_attachments ENABLE ROW LEVEL SECURITY;

    CREATE POLICY ticket_attachments_isolation ON ticket_attachments
        USING (issue_id IN (
            SELECT id FROM issues
            WHERE customer_id = current_setting('app.current_customer_id', true)::UUID
        ));
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ticket_attachments CASCADE;")
