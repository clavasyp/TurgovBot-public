"""Add loophole_analysis_cache + fingerprint on loophole_jobs

Revision ID: 005_add_analysis_cache
Revises: 004_add_loophole_jobs
Create Date: 2026-05-29

Кэш результатов дорогих анализов «весь свод» (full_server): один раз посчитали —
дальше отдаём готовое. Плюс колонка fingerprint на loophole_jobs для:
  * проверки кэша при старте анализа,
  * дедупликации уже стоящих в очереди идентичных анализов (fan-out при завершении).

Миграция плоская (без pgvector) — разворачивается на текущем образе БД.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '005_add_analysis_cache'
down_revision: Union[str, None] = '004_add_loophole_jobs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS loophole_analysis_cache (
            id             SERIAL PRIMARY KEY,
            fingerprint    TEXT NOT NULL UNIQUE,
            server_id      TEXT NOT NULL,
            analysis_type  TEXT,
            model_profile  TEXT,
            corpus_hash    TEXT,
            result         JSONB NOT NULL,
            hits           INTEGER NOT NULL DEFAULT 0,
            created_at     TIMESTAMPTZ DEFAULT now(),
            last_used_at   TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_analysis_cache_server ON loophole_analysis_cache (server_id)")
    # Отпечаток на задаче очереди: проверка кэша/дедуп идентичных full_server-анализов.
    op.execute("ALTER TABLE loophole_jobs ADD COLUMN IF NOT EXISTS fingerprint TEXT")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_loophole_jobs_fingerprint "
        "ON loophole_jobs (fingerprint) WHERE fingerprint IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_loophole_jobs_fingerprint")
    op.execute("ALTER TABLE loophole_jobs DROP COLUMN IF EXISTS fingerprint")
    op.execute("DROP TABLE IF EXISTS loophole_analysis_cache")
