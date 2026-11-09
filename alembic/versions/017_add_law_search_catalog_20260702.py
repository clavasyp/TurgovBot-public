"""Add law search catalog (умный поиск по законке для оверлея)

Revision ID: 017_add_law_search_catalog
Revises: 016_add_support_tickets
Create Date: 2026-07-02

Каталог статей для гибридного поиска БЕЗ LLM в рантайме (FTS russian + pg_trgm +
словарь синонимов + опциональное векторное плечо). Наполняется офлайн-билдером
(services/law_catalog_service.py) из law_documents; весь AI (структура наказаний,
синонимы) — на этапе индексации, не на запросе.

- law_articles        — нормализованные статьи: код («12.1»), название, полный текст,
                        структура наказания (офлайн-LLM), центроид эмбеддингов чанков,
                        generated-tsvector (russian, взвешенный: код+название A, текст B)
- law_synonyms        — сленг/синонимы → статья («дм» → убийство); поля source/status/
                        suggested_by заложены под будущий краудсорсинг от юзеров
- law_article_related — предрассчитанный топ похожих статей (косинус центроидов),
                        «ещё релевантное» без вычислений в рантайме

Написана ВРУЧНУЮ (как 004-016), моделей в models.py нет — рантайм через asyncpg.
Идемпотентна (IF NOT EXISTS). law_documents.id нестабильны (ре-парс форума =
DELETE+INSERT) → все FK с ON DELETE CASCADE, каталог пересобирается билдером.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '017_add_law_search_catalog'
down_revision: Union[str, None] = '016_add_support_tickets'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # vector уже установлен (006); pg_trgm есть в образе pgvector/pgvector:pg16
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_articles (
            id              SERIAL PRIMARY KEY,
            law_document_id INT NOT NULL REFERENCES law_documents(id) ON DELETE CASCADE,
            server_id       TEXT NOT NULL,
            article_ref     TEXT,                -- сырой заголовок из чанкера (ключ связи с law_chunks; NULL = преамбула/цельный док)
            kind            TEXT NOT NULL DEFAULT 'article',  -- article | chapter | section | clause | other
            code            TEXT,                -- нормализованный номер: '12.1', '5', 'IV' (NULL если не распознан)
            title           TEXT,                -- название статьи без номера ('Халатность')
            doc_title       TEXT,                -- название документа-источника ('Уголовный кодекс') — денормализовано для карточек
            full_text       TEXT NOT NULL,
            penalty_summary TEXT,                -- краткая строка санкции для карточки ('лишение свободы 5 лет / штраф $50000')
            penalty_json    JSONB,               -- структура санкции из офлайн-LLM (сроки/штрафы/розыск), NULL = не извлечено
            article_hash    TEXT NOT NULL,       -- md5(full_text) — skip неизменённых статей при ребилде
            enrich_hash     TEXT,                -- article_hash, для которого выполнено LLM-обогащение (протухание = enrich_hash != article_hash)
            embedding       halfvec(3072),       -- центроид эмбеддингов чанков статьи (для related-матрицы; без новых API-вызовов)
            -- Взвешенный FTS: код+название (A) важнее тела статьи (B)
            fts             tsvector GENERATED ALWAYS AS (
                                setweight(to_tsvector('russian', coalesce(code, '') || ' ' || coalesce(title, '')), 'A') ||
                                setweight(to_tsvector('russian', coalesce(full_text, '')), 'B')
                            ) STORED,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_law_articles_server ON law_articles (server_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_law_articles_doc ON law_articles (law_document_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_law_articles_doc_ref ON law_articles (law_document_id, article_ref)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_law_articles_fts ON law_articles USING gin (fts)")
    # Триграммы по названию — ловят опечатки и ошибки локального STT («холатность»)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_law_articles_title_trgm "
        "ON law_articles USING gin (lower(coalesce(title, '')) gin_trgm_ops)"
    )
    # ANN по центроидам — ускоряет офлайн-расчёт related и векторное плечо поиска
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_law_articles_embedding "
        "ON law_articles USING hnsw (embedding halfvec_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_synonyms (
            id           SERIAL PRIMARY KEY,
            server_id    TEXT NOT NULL,
            article_id   INT NOT NULL REFERENCES law_articles(id) ON DELETE CASCADE,
            term         TEXT NOT NULL,                     -- нормализованный (lower, trim) синоним/сленг
            weight       REAL NOT NULL DEFAULT 1.0,
            source       TEXT NOT NULL DEFAULT 'auto',      -- auto (офлайн-LLM) | owner (вручную) | user (краудсорсинг, потом)
            status       TEXT NOT NULL DEFAULT 'approved',  -- approved | pending | rejected (модерация краудсорсинга)
            suggested_by BIGINT,                            -- user_id предложившего (краудсорсинг, потом)
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (article_id, term)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_law_synonyms_server_term ON law_synonyms (server_id, term)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_law_synonyms_term_trgm "
        "ON law_synonyms USING gin (term gin_trgm_ops)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_article_related (
            article_id INT NOT NULL REFERENCES law_articles(id) ON DELETE CASCADE,
            related_id INT NOT NULL REFERENCES law_articles(id) ON DELETE CASCADE,
            score      REAL NOT NULL,
            PRIMARY KEY (article_id, related_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS law_article_related")
    op.execute("DROP TABLE IF EXISTS law_synonyms")
    op.execute("DROP TABLE IF EXISTS law_articles")
    # pg_trgm сознательно не удаляем (может использоваться другими объектами)
