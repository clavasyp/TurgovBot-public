"""Add question_reqs column to user_usage - unified question limit

Revision ID: 002_add_question_reqs
Revises: 001_initial
Create Date: 2026-03-16

Добавляет единый счётчик question_reqs в таблицу user_usage.
Старые колонки detailed_reqs и brief_reqs сохраняются для совместимости.
Теперь все типы вопросов (brief, detailed, free) используют question_reqs.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '002_add_question_reqs'
down_revision: Union[str, None] = '001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_usage',
        sa.Column('question_reqs', sa.Integer(), server_default='0', nullable=False)
    )


def downgrade() -> None:
    op.drop_column('user_usage', 'question_reqs')
