"""Add token tracking columns to agent_actions

Revision ID: 007_agent_token_tracking
Revises: 006_credential_types
Create Date: 2026-02-19
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "007_agent_token_tracking"
down_revision: Union[str, None] = "006_credential_types"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE agent_actions
        ADD COLUMN IF NOT EXISTS model_used VARCHAR(100),
        ADD COLUMN IF NOT EXISTS prompt_tokens INTEGER,
        ADD COLUMN IF NOT EXISTS completion_tokens INTEGER,
        ADD COLUMN IF NOT EXISTS total_tokens INTEGER;
    """)


def downgrade() -> None:
    op.execute("""
    ALTER TABLE agent_actions
        DROP COLUMN IF EXISTS model_used,
        DROP COLUMN IF EXISTS prompt_tokens,
        DROP COLUMN IF EXISTS completion_tokens,
        DROP COLUMN IF EXISTS total_tokens;
    """)
