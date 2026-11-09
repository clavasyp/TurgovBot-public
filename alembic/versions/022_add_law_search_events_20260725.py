"""Add an append-only quality log for Overlay law search.

Revision ID: 022_add_law_search_events
Revises: 021_add_law_article_source_order
Create Date: 2026-07-25

The table stores the question and the compact answer snapshot that was actually
returned to Overlay.  It deliberately has no foreign keys to users, devices or
law articles: quality history must survive catalog rebuilds and must not expose
authentication identifiers.
"""
from typing import Sequence, Union

from alembic import op


revision: str = "022_add_law_search_events"
down_revision: Union[str, None] = "021_add_law_article_source_order"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS law_search_events (
            id                  BIGSERIAL PRIMARY KEY,
            event_id            UUID NOT NULL UNIQUE,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            server_id           TEXT NOT NULL,
            query_text          TEXT NOT NULL,
            query_normalized    TEXT NOT NULL DEFAULT '',
            query_hash          CHAR(64) NOT NULL,
            tier                TEXT,
            requested_limit     SMALLINT NOT NULL,
            outcome             TEXT NOT NULL,
            error_code          TEXT,
            duration_ms         INTEGER NOT NULL,
            algorithm_version   TEXT NOT NULL,
            embedding_space     TEXT,
            intent_kind         TEXT,
            search_path         TEXT,
            search_mode         TEXT,
            degraded            BOOLEAN NOT NULL DEFAULT FALSE,
            warnings            TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
            result_count        SMALLINT NOT NULL DEFAULT 0,
            top_article_id      BIGINT,
            top_score           DOUBLE PRECISION,
            top_score_gap       DOUBLE PRECISION,
            results             JSONB NOT NULL DEFAULT '[]'::JSONB,
            related_ids         BIGINT[] NOT NULL DEFAULT ARRAY[]::BIGINT[],
            query_features      JSONB NOT NULL DEFAULT '{}'::JSONB,
            diagnostics         JSONB NOT NULL DEFAULT '{}'::JSONB,
            CONSTRAINT ck_law_search_events_query_length
                CHECK (char_length(query_text) <= 200),
            CONSTRAINT ck_law_search_events_limit
                CHECK (requested_limit BETWEEN 1 AND 20),
            CONSTRAINT ck_law_search_events_outcome
                CHECK (outcome IN ('success', 'empty', 'unavailable', 'error', 'cancelled')),
            CONSTRAINT ck_law_search_events_duration
                CHECK (duration_ms >= 0),
            CONSTRAINT ck_law_search_events_result_count
                CHECK (result_count BETWEEN 0 AND 20),
            CONSTRAINT ck_law_search_events_mode
                CHECK (
                    search_mode IS NULL
                    OR search_mode IN ('full', 'partial', 'keyword_only')
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_search_events_created
        ON law_search_events (created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_search_events_server_created
        ON law_search_events (server_id, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_search_events_query_created
        ON law_search_events (server_id, query_hash, algorithm_version, created_at DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_search_events_top_created
        ON law_search_events (top_article_id, created_at DESC)
        WHERE top_article_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_law_search_events_problem_created
        ON law_search_events (created_at DESC)
        WHERE degraded OR outcome <> 'success'
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS law_search_events")
