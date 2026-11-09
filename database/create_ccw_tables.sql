-- Criminal Case Writer Tables
-- Выполните этот скрипт в PostgreSQL для создания необходимых таблиц

-- Таблица сессий создания уголовных дел
CREATE TABLE IF NOT EXISTS criminal_case_sessions (
    id SERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    case_number VARCHAR(50),
    server_id TEXT REFERENCES servers(server_id) ON DELETE SET NULL,
    current_part_index INTEGER DEFAULT 0,
    collected_data JSONB DEFAULT '{}',
    status VARCHAR(20) DEFAULT 'active',
    google_doc_id VARCHAR(100),
    google_doc_url TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индекс для быстрого поиска активных сессий пользователя
CREATE INDEX IF NOT EXISTS ix_criminal_case_sessions_user_status
ON criminal_case_sessions(user_id, status);

-- Таблица частей уголовного дела с версионированием
CREATE TABLE IF NOT EXISTS criminal_case_parts (
    id SERIAL PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES criminal_case_sessions(id) ON DELETE CASCADE,
    part_type VARCHAR(50) NOT NULL,
    version INTEGER DEFAULT 1,
    content TEXT,
    ai_review_notes TEXT,
    is_final BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Индекс для быстрого поиска частей сессии
CREATE INDEX IF NOT EXISTS ix_criminal_case_parts_session_type
ON criminal_case_parts(session_id, part_type);

-- Системный кэш для хранения структуры шаблона и других данных
CREATE TABLE IF NOT EXISTS system_cache (
    key VARCHAR(255) PRIMARY KEY,
    value JSONB,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Готово! Таблицы созданы.

