"""Add support_tickets (обращения в поддержку с сайта)

Revision ID: 016_add_support_tickets
Revises: 015_add_vip_applications
Create Date: 2026-06-29

Обращения в поддержку, отправленные с САЙТА (/v1/web/support/ticket). Юзер выбирает тему
и пишет сообщение → запись падает в support_tickets + уведомление в TG-чат поддержки.
Раздел управления — в админке бота (список/просмотр/закрыть/ответить).

Статусы: open / answered / closed. Один юзер может слать несколько обращений (без
partial-unique, в отличие от ВИП-заявок).

Написана ВРУЧНУЮ (как 004-015), модель в models.py не трогаем. Идемпотентна
(IF NOT EXISTS) — таблица могла быть создана сырым DDL до прогона миграции на проде.
"""
from typing import Sequence, Union

from alembic import op


revision: str = '016_add_support_tickets'
down_revision: Union[str, None] = '015_add_vip_applications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS support_tickets (
            id                  SERIAL PRIMARY KEY,
            user_id             BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            topic               TEXT NOT NULL,
            message             TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'open',   -- open | answered | closed
            answer              TEXT,                           -- ответ админа (если отвечал)
            source              TEXT NOT NULL DEFAULT 'web',
            channel_chat_id     BIGINT,                         -- куда запостили уведомление
            channel_message_id  BIGINT,
            reviewed_by         BIGINT,
            reviewed_at         TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_support_tickets_status "
        "ON support_tickets (status, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_support_tickets_status")
    op.execute("DROP TABLE IF EXISTS support_tickets")
