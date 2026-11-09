"""Add loophole_analysis_results (архив ВСЕХ анализов для просмотра из админки)

Revision ID: 009_add_analysis_results
Revises: 008_add_first_started_at
Create Date: 2026-06-02

Отдельный журнал-архив КАЖДОГО завершённого анализа дыр (в т.ч. 0-дырочных — чтобы
разбирать, почему выдало 0). НЕ связан с кэшем (loophole_analysis_cache не трогаем):
кэш по-прежнему хранит только дорогие full_server и БЕЗ нулей. Этот архив пишется на
любой завершённый анализ и листается из админки (🕳 Очередь анализатора → 📊 Все анализы).

Написана ВРУЧНУЮ (как 004-008), модель в models.py не трогаем (autogenerate сгенерил бы
DROP живых колонок User — см. историю проекта).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '009_add_analysis_results'
down_revision: Union[str, None] = '008_add_first_started_at'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS loophole_analysis_results (
            id              SERIAL PRIMARY KEY,
            job_id          INTEGER REFERENCES loophole_jobs(id) ON DELETE SET NULL,
            user_id         BIGINT,
            chat_id         BIGINT,
            server_id       TEXT,
            analysis_type   TEXT,
            model_profile   TEXT,
            selection_mode  TEXT,
            total_findings  INTEGER NOT NULL DEFAULT 0,
            result          JSONB NOT NULL,
            is_out_of_band  BOOLEAN NOT NULL DEFAULT false,
            created_at      TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    # Листинг архива — по свежести; фильтр «только 0 дыр» для разбора ложных нулей.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_results_created "
        "ON loophole_analysis_results (created_at DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_analysis_results_zero "
        "ON loophole_analysis_results (created_at DESC) WHERE total_findings = 0"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_analysis_results_zero")
    op.execute("DROP INDEX IF EXISTS idx_analysis_results_created")
    op.execute("DROP TABLE IF EXISTS loophole_analysis_results")
