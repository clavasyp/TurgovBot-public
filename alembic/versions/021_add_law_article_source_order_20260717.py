"""Add durable source order for stable law article reconciliation.

Revision ID: 021_add_law_article_source_order
Revises: 020_add_law_embedding_versioning
Create Date: 2026-07-17

``law_articles.id`` remains the public stable identifier used by Overlay
favorites.  ``source_order`` is not an identity key: real documents contain
many repeated article numbers and headers.  It only records the current block
position so duplicate, otherwise-identical rows can be reconciled
deterministically while existing rows are updated in place.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "021_add_law_article_source_order"
down_revision: Union[str, None] = "020_add_law_embedding_versioning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE law_articles ADD COLUMN IF NOT EXISTS source_order INTEGER")
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   (
                       row_number() OVER (
                           PARTITION BY law_document_id
                           ORDER BY id
                       ) - 1
                   )::integer AS source_order
            FROM law_articles
        )
        UPDATE law_articles AS article
        SET source_order = ranked.source_order
        FROM ranked
        WHERE article.id = ranked.id
          AND article.source_order IS NULL
        """
    )
    op.execute("ALTER TABLE law_articles ALTER COLUMN source_order SET NOT NULL")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'ck_law_articles_source_order_nonnegative'
                  AND conrelid = 'law_articles'::regclass
            ) THEN
                ALTER TABLE law_articles
                ADD CONSTRAINT ck_law_articles_source_order_nonnegative
                CHECK (source_order >= 0);
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'uq_law_articles_document_source_order'
                  AND conrelid = 'law_articles'::regclass
            ) THEN
                ALTER TABLE law_articles
                ADD CONSTRAINT uq_law_articles_document_source_order
                UNIQUE (law_document_id, source_order)
                DEFERRABLE INITIALLY DEFERRED;
            END IF;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE law_articles "
        "DROP CONSTRAINT IF EXISTS uq_law_articles_document_source_order"
    )
    op.execute(
        "ALTER TABLE law_articles "
        "DROP CONSTRAINT IF EXISTS ck_law_articles_source_order_nonnegative"
    )
    op.execute("ALTER TABLE law_articles DROP COLUMN IF EXISTS source_order")
