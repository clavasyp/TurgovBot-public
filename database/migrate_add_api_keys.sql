-- Миграция: Создание таблицы api_keys для REST API
-- Дата: 2026-01-24
-- Alembic revision: 005_add_api_keys

-- Создаём таблицу api_keys
CREATE TABLE IF NOT EXISTS api_keys (
    id SERIAL PRIMARY KEY,
    key_hash VARCHAR(64) UNIQUE NOT NULL,           -- SHA256 хеш ключа
    key_prefix VARCHAR(12) NOT NULL,                 -- Первые 12 символов для UI (sk_live_XXXX)
    name VARCHAR(100) NOT NULL,                      -- Название ключа
    owner_user_id BIGINT NOT NULL,                   -- Telegram ID владельца
    key_type VARCHAR(10) NOT NULL,                   -- 'web' или 'app'
    allowed_origins TEXT[] DEFAULT NULL,             -- Домены для CORS (для web-ключей)
    allowed_ips TEXT[] DEFAULT NULL,                 -- Whitelist IP (опционально)
    is_active BOOLEAN DEFAULT TRUE,                  -- Активен ли ключ
    requests_per_minute INTEGER DEFAULT 10,          -- Лимит RPM
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_used_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    total_requests INTEGER DEFAULT 0,
    last_ip VARCHAR(45) DEFAULT NULL                 -- Для IPv6
);

-- Создаём индексы
CREATE INDEX IF NOT EXISTS ix_api_keys_owner ON api_keys (owner_user_id);
CREATE INDEX IF NOT EXISTS ix_api_keys_active ON api_keys (is_active);

-- Комментарий к таблице
COMMENT ON TABLE api_keys IS 'API-ключи для внешнего REST API доступа к функциям бота';
COMMENT ON COLUMN api_keys.key_hash IS 'SHA256 хеш API-ключа (сам ключ не хранится!)';
COMMENT ON COLUMN api_keys.key_type IS 'Тип ключа: web (с CORS) или app (без CORS, блокирует браузер)';
COMMENT ON COLUMN api_keys.allowed_origins IS 'Разрешённые домены для CORS (только для web-ключей)';
COMMENT ON COLUMN api_keys.allowed_ips IS 'Whitelist IP-адресов (опционально для обоих типов)';
