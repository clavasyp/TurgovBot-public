"""law_chunks.embedding: vector(768) → halfvec(3072) под gemini-embedding-2-preview

Revision ID: 011_law_chunks_halfvec
Revises: 010_add_tester_applications
Create Date: 2026-06-08

Апгрейд RAG-эмбеддингов на gemini-embedding-2-preview @ 3072 (макс. качество).

Почему halfvec, а не vector(3072):
  pgvector HNSW для типа `vector` ограничен 2000 измерениями, а для `halfvec`
  (half-precision) — до 4000. 3072 влезает только как halfvec. Текстовое
  представление и операторы дистанции (<=>) у halfvec те же, рантайм-каст —
  config.EMBEDDING_PG_TYPE='halfvec' (services/embeddings.py / indexing / retrieval).

TRUNCATE: старые 768d-векторы gemini-embedding-001 несовместимы ни по размерности,
ни по модели — их всё равно переэмбеддим заново (index_server_laws). Поэтому проще
снести столбец и пересоздать (на пустой таблице — без проблем с кастом типа).

Написана ВРУЧНУЮ (как 004-010) — модель в models.py не трогаем (там нет SQLAlchemy-типа
Vector/HalfVec; рантайм ходит в law_chunks через asyncpg напрямую).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '011_law_chunks_halfvec'
down_revision: Union[str, None] = '010_add_tester_applications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Чистим несовместимые 768d-эмбеддинги (переэмбеддятся заново под -2).
    op.execute("TRUNCATE TABLE law_chunks")
    # HNSW-индекс держит столбец — снимаем (DROP COLUMN снёс бы его каскадом, но явно надёжнее).
    op.execute("DROP INDEX IF EXISTS idx_law_chunks_embedding")
    # Пересоздаём столбец сразу нужного типа — на пустой таблице без каста vector→halfvec.
    op.execute("ALTER TABLE law_chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE law_chunks ADD COLUMN embedding halfvec(3072)")
    # HNSW под косинусную дистанцию (<=>) для halfvec.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_law_chunks_embedding "
        "ON law_chunks USING hnsw (embedding halfvec_cosine_ops)"
    )


def downgrade() -> None:
    # Откат к 768d (данные снова чистим — несовместимы).
    op.execute("TRUNCATE TABLE law_chunks")
    op.execute("DROP INDEX IF EXISTS idx_law_chunks_embedding")
    op.execute("ALTER TABLE law_chunks DROP COLUMN IF EXISTS embedding")
    op.execute("ALTER TABLE law_chunks ADD COLUMN embedding vector(768)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_law_chunks_embedding "
        "ON law_chunks USING hnsw (embedding vector_cosine_ops)"
    )
