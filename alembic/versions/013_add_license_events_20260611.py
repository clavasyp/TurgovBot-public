"""add license_events (append-only аудит device/overlay-лицензии для серверного детекта аномалий)

Revision ID: 013_add_license_events
Revises: 012_add_license_keys

Серверный детект аномалий (Слой C плана усиления защиты). Данные активации/верификации/телеметрии
уже собираются в license_keys.hwid_components, но НИГДЕ не журналируются под device-flow и никогда
не сравниваются. Эта таблица — append-only журнал событий лицензии, по которому работают детекторы:
  * дрейф HWID-компонентов (один pubkey рапортует разное железо → спуфинг/шеринг);
  * один pubkey с многих IP (шеринг ключа);
  * велосити активаций/ребайндов;
  * флаги integrity/дебаггера клиента;
  * расхождение hwid vs bound_hwid в verify();
  * много ключей с одной машины.

event_id — UUID, генерится КЛИЕНТОМ (дуал-сенд по двум каналам main+relay) для идемпотентного дедупа;
серверные события (challenge/activate/verify) генерят uuid4 на сервере. PK по event_id = дубль с
второго канала просто игнорируется (ON CONFLICT DO NOTHING).

Пишется вручную (op.execute), без autogenerate — как 012 (модель в database/models.py
рассинхронизирована с реальной БД).
"""
from alembic import op


revision = '013_add_license_events'
down_revision = '012_add_license_keys'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Append-only журнал событий лицензии. Источник для серверных детекторов аномалий.
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS license_events (
            event_id     UUID PRIMARY KEY,                  -- идемпотентность дедупа (дуал-сенд по 2 каналам)
            ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            event_type   TEXT NOT NULL,                     -- challenge|activate|verify|telemetry|anomaly
            key_lookup   TEXT,                              -- sha256(normalize(key)) — без сырого ключа
            user_id      BIGINT,
            hwid         TEXT,
            hwid_pubkey  TEXT,
            ip           INET,
            components   JSONB,                             -- покомпонентные хеши снимка железа
            app_version  TEXT,
            os           TEXT,
            integrity    JSONB,                             -- debugger/tamper-флаги клиента
            severity     SMALLINT NOT NULL DEFAULT 0,       -- 0 info | 1 soft | 2 medium | 3 hard
            detail       JSONB                              -- сработавшие детекторы / контекст
        )
        """
    )
    # Индексы под запросы детекторов: дрейф/велосити по pubkey и ключу, мульти-IP, окна по времени.
    op.execute("CREATE INDEX IF NOT EXISTS idx_license_events_pubkey_ts ON license_events (hwid_pubkey, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_license_events_keylookup_ts ON license_events (key_lookup, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_license_events_ip_ts ON license_events (ip, ts DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_license_events_type_ts ON license_events (event_type, ts DESC)")
    # Частичный индекс под «активные тревоги» (severity>=1) — быстрые админ-выборки.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_license_events_severity_ts "
        "ON license_events (ts DESC) WHERE severity >= 1"
    )


def downgrade() -> None:
    pass
