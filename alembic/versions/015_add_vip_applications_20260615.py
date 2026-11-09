"""Add vip_applications (заявки на ВИП/безлимит за промокод) + users.is_unlimited

Revision ID: 015_add_vip_applications
Revises: 014_add_license_commands
Create Date: 2026-06-15

Заявки на безлимитный статус по акции «введи промокод в игре». Юзер прикладывает
пруфы (видео+скрины) и форму (сервер+проект / логин / описание), заявка падает в
отдельный TG-канал с кнопками Одобрить/Отклонить; на одобрении выдаётся users.is_unlimited.

⚠️ Грант — отдельный флаг is_unlimited (НЕ is_vip): даёт ТОЛЬКО безлимит, оверлей НЕ
открывает (оверлей остаётся за подпиской на канал). Поэтому in/убираем его в bypass
лимитов (user_handlers/core.py), но НЕ в compute_access_tier (services/device_auth.py).

Статусы заявки: pending / approved / rejected. Частичный UNIQUE-индекс
uq_vip_apps_pending — «одна активная заявка на юзера» (после отклонения можно подать снова).

Написана ВРУЧНУЮ (как 004-014), модель в models.py не трогаем (autogenerate сгенерил бы
DROP живых колонок User — см. историю проекта).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '015_add_vip_applications'
down_revision: Union[str, None] = '014_add_license_commands'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Флаг безлимита (промо). Отдельный от is_vip — даёт только безлимит, без оверлея.
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_unlimited BOOLEAN NOT NULL DEFAULT FALSE"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS vip_applications (
            id                  SERIAL PRIMARY KEY,
            user_id             BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            status              TEXT NOT NULL DEFAULT 'pending',
            answers             JSONB,              -- {server, login, description}
            media               JSONB,              -- [{type:'photo'|'video', file_id}]
            channel_chat_id     BIGINT,             -- куда запостили заявку (для правки карточки)
            channel_message_id  BIGINT,
            reviewed_by         BIGINT,
            reviewed_at         TIMESTAMPTZ,
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Листинг/счётчик по статусу и свежести.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_vip_apps_status "
        "ON vip_applications (status, created_at DESC)"
    )
    # Одна активная (pending) заявка на юзера — БД-гарантия против дублей.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_vip_apps_pending "
        "ON vip_applications (user_id) WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_vip_apps_pending")
    op.execute("DROP INDEX IF EXISTS idx_vip_apps_status")
    op.execute("DROP TABLE IF EXISTS vip_applications")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_unlimited")
