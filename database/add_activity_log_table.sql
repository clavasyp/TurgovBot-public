-- SQL скрипт для создания таблицы логирования активности пользователей
-- Выполните этот скрипт в вашей PostgreSQL базе данных

CREATE TABLE IF NOT EXISTS user_activity_log (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    action_type VARCHAR(100) NOT NULL,
    action_details TEXT,
    server_id VARCHAR(100),
    metadata JSONB,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Индексы для быстрого поиска
    CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
);

-- Создание индексов для оптимизации запросов
CREATE INDEX IF NOT EXISTS idx_user_activity_user_id ON user_activity_log(user_id);
CREATE INDEX IF NOT EXISTS idx_user_activity_action_type ON user_activity_log(action_type);
CREATE INDEX IF NOT EXISTS idx_user_activity_timestamp ON user_activity_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_user_activity_server_id ON user_activity_log(server_id);

-- Составной индекс для аналитики по пользователю и времени
CREATE INDEX IF NOT EXISTS idx_user_activity_user_time ON user_activity_log(user_id, timestamp DESC);

-- Комментарии к таблице и полям
COMMENT ON TABLE user_activity_log IS 'Журнал активности пользователей в боте';
COMMENT ON COLUMN user_activity_log.user_id IS 'ID пользователя Telegram';
COMMENT ON COLUMN user_activity_log.action_type IS 'Тип действия (command, button_click, question, etc.)';
COMMENT ON COLUMN user_activity_log.action_details IS 'Детали действия';
COMMENT ON COLUMN user_activity_log.server_id IS 'ID сервера (если применимо)';
COMMENT ON COLUMN user_activity_log.metadata IS 'Дополнительная информация в формате JSON';
COMMENT ON COLUMN user_activity_log.timestamp IS 'Время выполнения действия';



