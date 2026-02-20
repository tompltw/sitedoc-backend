"""Add managed hosting fields to sites and issue_type to issues

Revision ID: 008_managed_hosting
Revises: 007_agent_token_tracking
Create Date: 2026-02-20
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "008_managed_hosting"
down_revision: Union[str, None] = "007_agent_token_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create issue_type enum
    op.execute("CREATE TYPE issue_type AS ENUM ('maintenance', 'site_build')")

    # Add issue_type column to issues
    op.execute("""
    ALTER TABLE issues
        ADD COLUMN IF NOT EXISTS issue_type issue_type NOT NULL DEFAULT 'maintenance'
    """)

    # Add managed hosting columns to sites
    op.execute("""
    ALTER TABLE sites
        ADD COLUMN IF NOT EXISTS is_managed BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS slug VARCHAR(63),
        ADD COLUMN IF NOT EXISTS server_ip VARCHAR(45),
        ADD COLUMN IF NOT EXISTS server_path VARCHAR(512),
        ADD COLUMN IF NOT EXISTS custom_domain VARCHAR(255),
        ADD COLUMN IF NOT EXISTS provisioned_at TIMESTAMPTZ
    """)

    # Add unique index on slug (only for non-null values)
    op.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ix_sites_slug ON sites (slug) WHERE slug IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sites_slug")
    op.execute("""
    ALTER TABLE sites
        DROP COLUMN IF EXISTS is_managed,
        DROP COLUMN IF EXISTS slug,
        DROP COLUMN IF EXISTS server_ip,
        DROP COLUMN IF EXISTS server_path,
        DROP COLUMN IF EXISTS custom_domain,
        DROP COLUMN IF EXISTS provisioned_at
    """)
    op.execute("ALTER TABLE issues DROP COLUMN IF EXISTS issue_type")
    op.execute("DROP TYPE IF EXISTS issue_type")
