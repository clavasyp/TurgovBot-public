"""Add tester_applications (заявки в тестировщики из канала)

Revision ID: 010_add_tester_applications
Revises: 009_add_analysis_results
Create Date: 2026-06-02

Хранилище заявок на статус тестировщика. Юзер подаёт анкету через deep-link
?start=apply_tester, супер-/админ отбирает в админ-панели по метрикам активности.

Статусы: pending / approved / rejected. Частичный UNIQUE-индекс
uq_tester_apps_pending гарантирует «одна активная заявка на юзера» (блок дублей,
пока заявка на рассмотрении; после отклонения можно подать снова).

Написана ВРУЧНУЮ (как 004-009), модель в models.py не трогаем (autogenerate
сгенерил бы DROP живых колонок User — см. историю проекта).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '010_add_tester_applications'
down_revision: Union[str, None] = '009_add_analysis_results'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS tester_applications (
            id                SERIAL PRIMARY KEY,
            user_id           BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            application_type  TEXT NOT NULL DEFAULT 'tester',
            status            TEXT NOT NULL DEFAULT 'pending',
            answers           JSONB,
            reviewed_by       BIGINT,
            reviewed_at       TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Листинг/счётчик по статусу и свежести.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tester_apps_status "
        "ON tester_applications (status, created_at DESC)"
    )
    # Одна активная (pending) заявка на юзера — БД-гарантия против дублей.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_tester_apps_pending "
        "ON tester_applications (user_id) WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tester_apps_pending")
    op.execute("DROP INDEX IF EXISTS idx_tester_apps_status")
    op.execute("DROP TABLE IF EXISTS tester_applications")
