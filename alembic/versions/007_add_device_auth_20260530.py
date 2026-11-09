"""add device auth tables (device_tokens, device_auth_requests) for overlay client

Revision ID: 007_add_device_auth
Revises: 006_add_law_chunks

Авторизация десктоп-оверлея по подписке/тестер-статусу БЕЗ секрета на клиенте:
device-code flow (клиент показывает код → юзер подтверждает в Telegram-боте → клиент
получает короткоживущий access-JWT + ротируемый refresh-токен). Пишется вручную (op.execute),
без autogenerate — модель User в database/models.py рассинхронизирована с реальной БД.
"""
from alembic import op


revision = '007_add_device_auth'
down_revision = '006_add_law_chunks'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Активные/выданные токены устройств. Храним только ХЭШ refresh-токена (как api_keys.key_hash).
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS device_tokens (
            id                  BIGSERIAL PRIMARY KEY,
            user_id             BIGINT NOT NULL,
            device_id           TEXT NOT NULL,
            refresh_token_hash  TEXT NOT NULL,
            refresh_expires_at  TIMESTAMPTZ NOT NULL,
            access_jti          TEXT,
            tier                TEXT NOT NULL,
            revoked             BOOLEAN NOT NULL DEFAULT FALSE,
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            last_seen_at        TIMESTAMPTZ,
            client_meta         JSONB
        )
        """
    )
    # «1 активное устройство на аккаунт» — частичный уникальный индекс (как uq_loophole_active_user).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_device_active_user "
        "ON device_tokens (user_id) WHERE revoked = FALSE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_tokens_refresh ON device_tokens (refresh_token_hash)"
    )

    # Заявки на привязку устройства (device-code flow). Короткоживущие.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS device_auth_requests (
            id              BIGSERIAL PRIMARY KEY,
            device_code     TEXT UNIQUE NOT NULL,
            user_code       TEXT NOT NULL,
            device_id       TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'pending',
            user_id         BIGINT,
            tier            TEXT,
            client_meta     JSONB,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            expires_at      TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_auth_user_code ON device_auth_requests (user_code)"
    )


def downgrade() -> None:
    pass
