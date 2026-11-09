"""Add first_started_at to loophole_jobs (pro→flash fallback timing)

Revision ID: 008_add_first_started_at
Revises: 007_add_device_auth
Create Date: 2026-05-31

Метка времени ПЕРВОГО реального старта задания анализатора. Ставится один раз при
первом claim'е (COALESCE), переживает реквью. По ней решается фолбэк pro→flash: 30 минут
считаются от НАЧАЛА работы над заданием, а не от постановки в очередь (исключает простой
в очереди при concurrency=1 + бэклоге). Написана ВРУЧНУЮ (как 004-007), модель в models.py
не трогаем.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '008_add_first_started_at'
down_revision: Union[str, None] = '007_add_device_auth'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE loophole_jobs ADD COLUMN IF NOT EXISTS first_started_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE loophole_jobs DROP COLUMN IF EXISTS first_started_at")
