"""Add loophole_jobs queue table

Revision ID: 004_add_loophole_jobs
Revises: 003_add_user_question_defaults
Create Date: 2026-05-29

Создаёт таблицу очереди анализатора дыр в законке (loophole_jobs) и индексы.

ВАЖНО: миграция написана ВРУЧНУЮ, а не через autogenerate. Модель `User` в
database/models.py рассинхронизирована с реальной БД (в коде используются колонки
users.is_subscriber / is_tester / is_blocked, которых нет в модели), поэтому слепой
autogenerate с compare_type/compare_server_default попытался бы их удалить. Здесь
затрагивается только новая таблица loophole_jobs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '004_add_loophole_jobs'
down_revision: Union[str, None] = '003_add_user_question_defaults'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'loophole_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('status', sa.Text(), server_default='queued', nullable=False),
        sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('is_out_of_band', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('manual_position', sa.Integer(), nullable=True),
        sa.Column('position_msg_id', sa.BigInteger(), nullable=True),
        sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    # Частичный уникальный индекс: один активный (не out-of-band) запрос на пользователя.
    op.create_index(
        'uq_loophole_active_user', 'loophole_jobs', ['user_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running') AND is_out_of_band = false"),
    )
    # Индекс для выборки следующей задачи и подсчёта позиций.
    op.create_index(
        'idx_loophole_queue_order', 'loophole_jobs',
        ['status', 'is_out_of_band', 'priority', 'manual_position', 'created_at'],
    )


def downgrade() -> None:
    op.drop_index('idx_loophole_queue_order', table_name='loophole_jobs')
    op.drop_index('uq_loophole_active_user', table_name='loophole_jobs')
    op.drop_table('loophole_jobs')
