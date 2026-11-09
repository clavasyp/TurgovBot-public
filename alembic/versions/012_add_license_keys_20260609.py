"""add license_keys + hwid_bans + hwid/key_id on device_tokens (key-activation + HWID binding)

Revision ID: 012_add_license_keys
Revises: 011_law_chunks_halfvec

Лицензирование оверлея по КЛЮЧУ (бот выдаёт ключ → юзер активирует в софте) с привязкой к
железу (HWID): 1 ключ = 1 ПК, сам-сброс N раз, бан юзера → бан его HWID. Пишется вручную
(op.execute), без autogenerate — модель User в database/models.py рассинхронизирована с реальной БД.

  * license_keys — выданные ключи (key plaintext для переотображения ботом + key_lookup=sha256 для
    лукапа без сырого ключа в логах/WHERE), привязка hwid+hwid_pubkey, счётчик ребайндов.
  * hwid_bans — забаненное железо (каскад от бана юзера + ручной бан админом).
  * device_tokens += hwid, key_id — чтобы refresh/WS-accept перепроверяли hwid-бан и статус ключа.
"""
from alembic import op


revision = '012_add_license_keys'
down_revision = '011_law_chunks_halfvec'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Лицензионные ключи. key хранится плейнтекстом (бот должен переотобразить юзеру),
    # key_lookup = sha256(normalize(key)) — поиск по нему, сырой ключ не попадает в WHERE/логи.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS license_keys (
            id               BIGSERIAL PRIMARY KEY,
            key              TEXT NOT NULL,
            key_lookup       TEXT NOT NULL UNIQUE,
            user_id          BIGINT NOT NULL,
            tier             TEXT NOT NULL,
            status           TEXT NOT NULL DEFAULT 'active',   -- active | revoked
            hwid             TEXT,                              -- NULL до первой привязки
            hwid_pubkey      TEXT,                              -- ed25519 pub устройства (raw, base64)
            hwid_components  JSONB,                             -- покомпонентные хеши для нечёткого матча
            rebind_count     INTEGER NOT NULL DEFAULT 0,
            rebind_max       INTEGER NOT NULL DEFAULT 2,
            last_rebind_at   TIMESTAMPTZ,
            bound_at         TIMESTAMPTZ,
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            expires_at       TIMESTAMPTZ                        -- NULL = бессрочно (резерв)
        )
        """
    )
    # «1 активный ключ на аккаунт» — частичный уникальный индекс (как uq_device_active_user).
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_license_active_user "
        "ON license_keys (user_id) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_license_keys_hwid ON license_keys (hwid)"
    )

    # Забаненное железо. Каскад от бана юзера (source='user_ban_cascade') или ручной бан админа.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS hwid_bans (
            id          BIGSERIAL PRIMARY KEY,
            hwid        TEXT UNIQUE NOT NULL,
            reason      TEXT,
            banned_by   BIGINT,
            source      TEXT,                                  -- user_ban_cascade | manual
            banned_at   TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # device_tokens: привязка выданной пары токенов к hwid и ключу — чтобы refresh/WS-accept
    # перепроверяли hwid-бан и статус ключа (не только tier юзера). Legacy-строки = NULL (катовер).
    op.execute("ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS hwid TEXT")
    op.execute("ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS key_id BIGINT")


def downgrade() -> None:
    pass
