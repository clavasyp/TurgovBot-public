"""Add law_chunks table with pgvector for semantic retrieval

Revision ID: 006_add_law_chunks
Revises: 005_add_analysis_cache
Create Date: 2026-05-29

Семантический ретривал (RAG) для голосового ассистента: включает расширение pgvector
и создаёт таблицу law_chunks (чанки законов + эмбеддинги gemini-embedding-001, 768d).

ВАЖНО:
  * Требуется образ Postgres с pgvector (docker-compose: pgvector/pgvector:pg16).
  * Миграция написана ВРУЧНУЮ (как 004) — модель в models.py не добавляется, чтобы
    не тянуть SQLAlchemy-тип Vector и не цеплять autogenerate (рантайм работает через asyncpg).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '006_add_law_chunks'
down_revision: Union[str, None] = '005_add_analysis_cache'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_chunks (
            id              SERIAL PRIMARY KEY,
            law_document_id INTEGER NOT NULL REFERENCES law_documents(id) ON DELETE CASCADE,
            server_id       TEXT NOT NULL,
            chunk_index     INTEGER NOT NULL,
            title           TEXT,
            article_ref     TEXT,
            content         TEXT NOT NULL,
            content_hash    TEXT,
            token_estimate  INTEGER,
            embedding       vector(768),
            created_at      TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_law_chunks_server ON law_chunks (server_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_law_chunks_doc ON law_chunks (law_document_id)")
    # HNSW-индекс под косинусную дистанцию (<=>) для быстрого ANN-поиска.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_law_chunks_embedding "
        "ON law_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS law_chunks")
    # Расширение vector НЕ удаляем — может использоваться другими объектами.
