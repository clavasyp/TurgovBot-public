"""Add user default question settings

Revision ID: 003_add_user_question_defaults
Revises: 002_add_question_reqs
Create Date: 2026-05-09

Добавляет сохраненный шаблон вопроса пользователя: проект, сервер и тип ответа.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '003_add_user_question_defaults'
down_revision: Union[str, None] = '002_add_question_reqs'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('default_project_id', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('default_server_id', sa.Text(), nullable=True))
    op.add_column('users', sa.Column('default_question_type', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'default_question_type')
    op.drop_column('users', 'default_server_id')
    op.drop_column('users', 'default_project_id')
