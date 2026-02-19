"""Add database, cpanel, wp_app_password credential types

Revision ID: 006_credential_types
Revises: 005_ticket_attachments
Create Date: 2025-01-01 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = '006_credential_types'
down_revision = '005_ticket_attachments'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction in older PG,
    # so we commit first via execute_if / raw connection.
    op.execute("ALTER TYPE credential_type ADD VALUE IF NOT EXISTS 'database'")
    op.execute("ALTER TYPE credential_type ADD VALUE IF NOT EXISTS 'cpanel'")
    op.execute("ALTER TYPE credential_type ADD VALUE IF NOT EXISTS 'wp_app_password'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values without recreating the type.
    # This is a no-op downgrade; manually recreate if needed.
    pass
