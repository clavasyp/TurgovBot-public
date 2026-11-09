"""add license_commands (удалённый кил-свитч: pending-очередь подписанных пер-устройство команд)

Revision ID: 014_add_license_commands
Revises: 013_add_license_events

Слой F плана усиления защиты — удалённая деактивация + принудительная чистка следов. Инвариант
безопасности: команда привязана к ОДНОМУ hwid_pubkey (нет broadcast-формата), подписана ОФЛАЙН-ключом
релиза (приватного ключа на проде НЕТ — тот же ed25519, что подписывает манифесты апдейтов). Сервер
лишь РЕТРАНСЛИРУЕТ подписанную команду: захват сервера НЕ даёт подделать кил (нет приватного ключа).

Очередь, не «выстрелил-забыл»: команда висит pending и отдаётся клиенту при каждом verify()-чек-ине,
пока клиент не пришлёт ПОДПИСАННЫЙ устройством ACK. Юзер офлайн неделю → прилетит при следующем запуске.

payload — это КАНОНИЧЕСКАЯ JSON-строка, ровно те байты, что подписаны (клиент верифицирует sig над
payload как есть, потом парсит). Так исключён любой рассинхрон канонизации между Python-подписантом и
C++-клиентом.
"""
from alembic import op


revision = '014_add_license_commands'
down_revision = '013_add_license_events'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS license_commands (
            command_id   TEXT PRIMARY KEY,                  -- = nonce токена, base64 (идемпотентность дедупа)
            hwid_pubkey  TEXT NOT NULL,                     -- адресат (ровно одно устройство)
            key_lookup   TEXT,
            action       TEXT NOT NULL,                     -- reap
            payload      TEXT NOT NULL,                     -- КАНОНИЧЕСКАЯ JSON-строка (подписанные байты)
            sig          TEXT NOT NULL,                     -- ed25519(payload) офлайн-ключом релиза, base64
            status       TEXT NOT NULL DEFAULT 'pending',   -- pending | acked | cancelled
            created_by   BIGINT,                            -- кто инициировал (для аудита)
            created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            expires_at   TIMESTAMPTZ,                       -- после — не отдаём (истёкший токен бесполезен)
            acked_at     TIMESTAMPTZ,
            ack_sig      TEXT                               -- подпись устройства поверх nonce (доказательство приёма)
        )
        """
    )
    # Выдача pending по pubkey при каждом verify-чек-ине.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_license_commands_pubkey_status "
        "ON license_commands (hwid_pubkey, status)"
    )


def downgrade() -> None:
    pass
