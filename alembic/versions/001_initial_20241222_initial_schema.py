"""Initial schema - baseline migration

Revision ID: 001_initial
Revises:
Create Date: 2024-12-22

Это базовая миграция, которая описывает текущую схему БД.
Она не вносит изменений, так как таблицы уже существуют.
При первом запуске Alembic нужно пометить эту миграцию как применённую:
    alembic stamp head
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Применение миграции.

    Эта миграция является базовой и описывает существующую схему БД.
    Таблицы создаются только если они ещё не существуют (для новых установок).
    Для существующих БД рекомендуется использовать: alembic stamp 001_initial
    """

    # === ТАБЛИЦА USERS ===
    op.create_table(
        'users',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('full_name', sa.Text(), nullable=False),
        sa.Column('username', sa.Text(), nullable=True),
        sa.Column('is_admin', sa.Boolean(), server_default='false'),
        sa.Column('is_vip', sa.Boolean(), server_default='false'),
        sa.Column('is_banned', sa.Boolean(), server_default='false'),
        sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('user_id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА USER_USAGE ===
    op.create_table(
        'user_usage',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('usage_date', sa.Date(), server_default=sa.func.current_date(), nullable=False),
        sa.Column('detailed_reqs', sa.Integer(), server_default='0'),
        sa.Column('brief_reqs', sa.Integer(), server_default='0'),
        sa.Column('interview_gens', sa.Integer(), server_default='0'),
        sa.Column('complaint_gens', sa.Integer(), server_default='0'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'usage_date'),
        if_not_exists=True
    )

    # === ТАБЛИЦА PROJECTS ===
    op.create_table(
        'projects',
        sa.Column('project_id', sa.Text(), nullable=False),
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('forum_base_url', sa.Text(), nullable=True),
        sa.Column('is_disabled', sa.Boolean(), server_default='false'),
        sa.PrimaryKeyConstraint('project_id'),
        sa.UniqueConstraint('display_name'),
        if_not_exists=True
    )

    # === ТАБЛИЦА SERVERS ===
    op.create_table(
        'servers',
        sa.Column('server_id', sa.Text(), nullable=False),
        sa.Column('project_id', sa.Text(), nullable=True),
        sa.Column('display_name', sa.Text(), nullable=False),
        sa.Column('is_disabled', sa.Boolean(), server_default='false'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.project_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('server_id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА LAW_DOCUMENTS ===
    op.create_table(
        'law_documents',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('server_id', sa.Text(), nullable=True),
        sa.Column('source_thread_id', sa.Integer(), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('gemini_file_uri', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('last_updated', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['server_id'], ['servers.server_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('server_id', 'source_thread_id', name='uq_law_documents_server_thread'),
        if_not_exists=True
    )

    # === ТАБЛИЦА MESSAGE_HISTORY ===
    op.create_table(
        'message_history',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('server_id', sa.Text(), nullable=True),
        sa.Column('role', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА COURT_CASES ===
    op.create_table(
        'court_cases',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('server_name', sa.String(100), nullable=False),
        sa.Column('thread_url', sa.Text(), nullable=False),
        sa.Column('thread_id', sa.String(50), nullable=True),
        sa.Column('case_title', sa.Text(), nullable=True),
        sa.Column('plaintiff', sa.String(200), nullable=True),
        sa.Column('defendant', sa.String(200), nullable=True),
        sa.Column('status', sa.String(20), server_default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_updated', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('thread_url'),
        if_not_exists=True
    )

    # === ТАБЛИЦА COURT_MESSAGES ===
    op.create_table(
        'court_messages',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('court_case_id', sa.Integer(), nullable=True),
        sa.Column('message_order', sa.Integer(), nullable=False),
        sa.Column('author', sa.String(200), nullable=True),
        sa.Column('author_role', sa.String(20), nullable=True),
        sa.Column('message_timestamp', sa.DateTime(timezone=True), nullable=True),
        sa.Column('text_content', sa.Text(), nullable=True),
        sa.Column('has_images', sa.Boolean(), server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['court_case_id'], ['court_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА COURT_IMAGES ===
    op.create_table(
        'court_images',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('court_message_id', sa.Integer(), nullable=True),
        sa.Column('image_url', sa.Text(), nullable=False),
        sa.Column('bbcode_tag', sa.Text(), nullable=True),
        sa.Column('ocr_text', sa.Text(), nullable=True),
        sa.Column('document_type', sa.String(50), nullable=True),
        sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['court_message_id'], ['court_messages.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА COURT_DIALOGUE_LOGS ===
    op.create_table(
        'court_dialogue_logs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('court_case_id', sa.Integer(), nullable=True),
        sa.Column('user_message', sa.Text(), nullable=False),
        sa.Column('assistant_response', sa.Text(), nullable=False),
        sa.Column('used_tools', sa.ARRAY(sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['court_case_id'], ['court_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА COMPLAINTS_HISTORY ===
    op.create_table(
        'complaints_history',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('server_id', sa.Text(), nullable=True),
        sa.Column('complaint_type', sa.Text(), nullable=False),
        sa.Column('forum_url', sa.Text(), nullable=False),
        sa.Column('situation_description', sa.Text(), nullable=False),
        sa.Column('generated_text', sa.Text(), nullable=False),
        sa.Column('generated_bb_code', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА PRECEDENTS ===
    op.create_table(
        'precedents',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('server_id', sa.Text(), nullable=True),
        sa.Column('source_url', sa.Text(), nullable=False),
        sa.Column('source_thread_id', sa.Integer(), nullable=True),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('full_content', sa.Text(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('gemini_file_uri', sa.Text(), nullable=True),
        sa.Column('content_hash', sa.Text(), nullable=True),
        sa.Column('image_urls', sa.ARRAY(sa.Text()), nullable=True),
        sa.Column('precedent_type', sa.Text(), server_default='precedent'),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('last_updated', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['server_id'], ['servers.server_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('server_id', 'source_thread_id', name='uq_precedents_server_thread'),
        if_not_exists=True
    )

    # Индекс для precedents
    op.create_index(
        'idx_precedents_server_active',
        'precedents',
        ['server_id', 'is_active'],
        if_not_exists=True
    )

    # === ТАБЛИЦА ASSISTANT_CASES ===
    op.create_table(
        'assistant_cases',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('server_id', sa.Text(), nullable=True),
        sa.Column('rp_role', sa.Text(), nullable=True),
        sa.Column('case_stage', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), server_default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('last_updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА ASSISTANT_MESSAGES ===
    op.create_table(
        'assistant_messages',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=True),
        sa.Column('sender_role', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=True),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['case_id'], ['assistant_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА REPORTS ===
    op.create_table(
        'reports',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(20), server_default='open'),
        sa.Column('category', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('closed_by', sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА REPORT_MESSAGES ===
    op.create_table(
        'report_messages',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('report_id', sa.Integer(), nullable=True),
        sa.Column('sender_id', sa.BigInteger(), nullable=False),
        sa.Column('sender_role', sa.String(20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('timestamp', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА USER_SUBSCRIPTIONS ===
    op.create_table(
        'user_subscriptions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=True),
        sa.Column('subscription_type', sa.Text(), nullable=False),
        sa.Column('activated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        if_not_exists=True
    )

    # === ТАБЛИЦА ASSISTANT_USAGE ===
    op.create_table(
        'assistant_usage',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('usage_type', sa.Text(), nullable=False),
        sa.Column('last_reset', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('count', sa.Integer(), server_default='0'),
        sa.ForeignKeyConstraint(['user_id'], ['users.user_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'usage_type'),
        if_not_exists=True
    )


def downgrade() -> None:
    """
    Откат миграции.

    ВНИМАНИЕ: Это удалит ВСЕ таблицы и данные!
    Используйте с осторожностью.
    """
    op.drop_table('assistant_usage', if_exists=True)
    op.drop_table('user_subscriptions', if_exists=True)
    op.drop_table('report_messages', if_exists=True)
    op.drop_table('reports', if_exists=True)
    op.drop_table('assistant_messages', if_exists=True)
    op.drop_table('assistant_cases', if_exists=True)
    op.drop_index('idx_precedents_server_active', table_name='precedents', if_exists=True)
    op.drop_table('precedents', if_exists=True)
    op.drop_table('complaints_history', if_exists=True)
    op.drop_table('court_dialogue_logs', if_exists=True)
    op.drop_table('court_images', if_exists=True)
    op.drop_table('court_messages', if_exists=True)
    op.drop_table('court_cases', if_exists=True)
    op.drop_table('message_history', if_exists=True)
    op.drop_table('law_documents', if_exists=True)
    op.drop_table('servers', if_exists=True)
    op.drop_table('projects', if_exists=True)
    op.drop_table('user_usage', if_exists=True)
    op.drop_table('users', if_exists=True)

