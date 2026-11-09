"""add universal license keys (один ключ на всех, без HWID-привязки, отзывной)

Revision ID: 019_add_universal_license
Revises: 018_add_law_local_embedding
Create Date: 2026-07-09

Универсальный лицензионный ключ для отдельного laws-билда оверлея (только поиск по законке):
  * один ключ раздаётся ВСЕМ вместе с установщиком — активируется на любом числе машин,
    БЕЗ привязки к HWID и без лимита активаций;
  * тир ключа фиксированный ('laws', хранится в license_keys.tier) — НЕ зависит от флагов users;
  * отзыв ключа (status='revoked') централизованно гасит все устройства (refresh → key_revoked).

Схема:
  * license_keys.universal / device_tokens.universal — признак универсального ключа/сессии;
  * частичные уникальные индексы «1 активный ключ/устройство на аккаунт» пересоздаются с
    universal=FALSE: универсальный ключ живёт ПОВЕРХ личного ключа владельца-админа, а N машин
    универсального ключа не вытесняют друг друга.

Написана ВРУЧНУЮ (как 004-018). Идемпотентна (IF NOT EXISTS / пересоздание индексов безопасно).
"""
from typing import Sequence, Union

from alembic import op


revision: str = '019_add_universal_license'
down_revision: Union[str, None] = '018_add_law_local_embedding'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE license_keys ADD COLUMN IF NOT EXISTS universal BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS universal BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # «1 активный ключ на аккаунт» — теперь только для ОБЫЧНЫХ ключей: универсальный ключ числится
    # за админом-владельцем (аудит) и не должен конфликтовать с его личным ключом.
    op.execute("DROP INDEX IF EXISTS uq_license_active_user")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_license_active_user "
        "ON license_keys (user_id) WHERE status = 'active' AND universal = FALSE"
    )
    # «1 активное устройство на аккаунт» — тоже только для обычных сессий: у универсального ключа
    # много машин под одним user_id владельца, они НЕ должны вытеснять друг друга (и личное
    # устройство владельца).
    op.execute("DROP INDEX IF EXISTS uq_device_active_user")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_device_active_user "
        "ON device_tokens (user_id) WHERE revoked = FALSE AND universal = FALSE"
    )
    # Отзыв/подсчёт устройств универсального ключа идёт по key_id.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_tokens_key_id ON device_tokens (key_id) "
        "WHERE revoked = FALSE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_device_tokens_key_id")
    op.execute("DROP INDEX IF EXISTS uq_device_active_user")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_device_active_user "
        "ON device_tokens (user_id) WHERE revoked = FALSE"
    )
    op.execute("DROP INDEX IF EXISTS uq_license_active_user")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_license_active_user "
        "ON license_keys (user_id) WHERE status = 'active'"
    )
    op.execute("ALTER TABLE device_tokens DROP COLUMN IF EXISTS universal")
    op.execute("ALTER TABLE license_keys DROP COLUMN IF EXISTS universal")
