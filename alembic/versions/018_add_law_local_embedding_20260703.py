"""Add local embedding column for law search (FRIDA / локальная модель на сервере)

Revision ID: 018_add_law_local_embedding
Revises: 017_add_law_search_catalog
Create Date: 2026-07-03

Отдельная колонка под ЛОКАЛЬНУЮ модель эмбеддингов каталога поиска (ai-forever/FRIDA, 1536d).
Зачем отдельная, а не ALTER существующей law_articles.embedding halfvec(3072):
- облачный gemini-embedding-001 давал холодный SA-токен ~11с > таймаут → векторная нога поиска
  мертва в проде. Локальная модель на CPU оживляет её (вектор с первого запроса).
- пространства -001 (3072) и FRIDA (1536) НЕсовместимы → нужна отдельная колонка + отдельный HNSW.
- старая embedding halfvec(3072) остаётся НЕТРОНУТОЙ как откат до отдельной миграции 019 (дроп позже).

1536 ≤ 2000 → обычный pgvector `vector` (halfvec НЕ нужен, он был только из-за 3072). Индекс ×4 меньше.
Оператор косинуса `<=>` + vector_cosine_ops (эмбеддинги L2-нормализованы в services/law_embedder.py).

Наполняется офлайн-скриптом scripts/reembed_law_articles.py (БЕЗ LLM/квоты, ~10мин на 10 серверов) —
это НЕ обогащение. Пока LAW_CATALOG_EMBED_ENABLED=false колонка просто лежит пустой, поиск идёт по-старому.

Написана ВРУЧНУЮ (как 004-017). Идемпотентна (IF NOT EXISTS).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '018_add_law_local_embedding'
down_revision: Union[str, None] = '017_add_law_search_catalog'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # vector-расширение уже установлено (миграция 006). FRIDA = 1536 dims → обычный vector(1536).
    op.execute("ALTER TABLE law_articles ADD COLUMN IF NOT EXISTS embedding_local vector(1536)")
    # HNSW строится на пустой колонке (мгновенно); заполнение — офлайн-скриптом пере-эмбеддинга.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_articles_emb_local
        ON law_articles USING hnsw (embedding_local vector_cosine_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_law_articles_emb_local")
    op.execute("ALTER TABLE law_articles DROP COLUMN IF EXISTS embedding_local")
