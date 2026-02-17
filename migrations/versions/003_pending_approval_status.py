"""Add pending_approval to issue_status enum

Revision ID: 003_pending_approval_status
Revises: 002_memory_architecture
Create Date: 2026-02-17 00:03:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "003_pending_approval_status"
down_revision: Union[str, None] = "002_memory_architecture"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE issue_status ADD VALUE IF NOT EXISTS 'pending_approval'")


def downgrade() -> None:
    # PostgreSQL doesn't support removing enum values — would require recreate
    pass
