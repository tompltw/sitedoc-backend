"""Memory architecture: conversation_memory table + pgvector embeddings

Revision ID: 002_memory_architecture
Revises: 001_initial_schema
Create Date: 2026-02-17 00:00:00.000000
"""
from typing import Sequence, Union
from alembic import op

revision: str = "002_memory_architecture"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
    -- Enable pgvector
    CREATE EXTENSION IF NOT EXISTS vector;

    -- Layer 1: Structured memory extracted from messages
    CREATE TABLE conversation_memory (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE NOT NULL,
        customer_id UUID REFERENCES customers(id) ON DELETE CASCADE NOT NULL,
        site_id UUID REFERENCES sites(id) ON DELETE CASCADE,

        -- Category of extracted data
        category TEXT NOT NULL CHECK (category IN (
            'credential', 'task', 'decision', 'preference', 'file_url', 'general'
        )),

        -- Structured payload (category-specific JSON)
        payload JSONB NOT NULL DEFAULT '{}',

        -- Source tracking
        source_message_id UUID,
        extracted_by TEXT NOT NULL DEFAULT 'haiku',

        -- Lifecycle
        is_active BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX idx_conv_memory_conversation ON conversation_memory(conversation_id);
    CREATE INDEX idx_conv_memory_customer ON conversation_memory(customer_id);
    CREATE INDEX idx_conv_memory_category ON conversation_memory(category);
    CREATE INDEX idx_conv_memory_active ON conversation_memory(is_active) WHERE is_active = true;
    CREATE INDEX idx_conv_memory_payload ON conversation_memory USING gin(payload);

    -- Layer 2: Vector embeddings for RAG fallback
    CREATE TABLE message_embeddings (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE NOT NULL,
        customer_id UUID REFERENCES customers(id) ON DELETE CASCADE NOT NULL,

        -- Source (chat_messages or agent messages)
        message_content TEXT NOT NULL,
        sender_type TEXT NOT NULL,

        -- pgvector embedding (1536 dims for text-embedding-3-small)
        embedding vector(1536),

        -- Metadata for filtering
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX idx_msg_embeddings_conversation ON message_embeddings(conversation_id);
    -- HNSW index for fast approximate nearest-neighbor search
    CREATE INDEX idx_msg_embeddings_vector ON message_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);

    -- Extraction job queue (tracks async Haiku extraction status)
    CREATE TABLE memory_extraction_jobs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE NOT NULL,
        message_content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'running', 'done', 'failed')),
        celery_task_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ
    );

    CREATE INDEX idx_extraction_jobs_status ON memory_extraction_jobs(status) WHERE status = 'pending';

    -- updated_at trigger for conversation_memory
    CREATE OR REPLACE FUNCTION update_updated_at_column()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = now();
        RETURN NEW;
    END;
    $$ language 'plpgsql';

    CREATE TRIGGER update_conversation_memory_updated_at
        BEFORE UPDATE ON conversation_memory
        FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

    -- Update conversations table to track memory state
    ALTER TABLE conversations
        ADD COLUMN IF NOT EXISTS memory_last_synced_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS total_tokens_saved INTEGER NOT NULL DEFAULT 0;
    """)


def downgrade() -> None:
    op.execute("""
    DROP TRIGGER IF EXISTS update_conversation_memory_updated_at ON conversation_memory;
    DROP FUNCTION IF EXISTS update_updated_at_column();
    DROP TABLE IF EXISTS memory_extraction_jobs;
    DROP TABLE IF EXISTS message_embeddings;
    DROP TABLE IF EXISTS conversation_memory;
    ALTER TABLE conversations
        DROP COLUMN IF EXISTS memory_last_synced_at,
        DROP COLUMN IF EXISTS total_tokens_saved;
    DROP EXTENSION IF EXISTS vector;
    """)
