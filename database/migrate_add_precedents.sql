-- Миграция для добавления таблицы precedents (судебные прецеденты и толкования)
-- Дата создания: 2025-12-10

-- Создание таблицы precedents
CREATE TABLE IF NOT EXISTS precedents (
    id SERIAL PRIMARY KEY,
    server_id TEXT REFERENCES servers(server_id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    source_thread_id INTEGER,
    title TEXT NOT NULL,
    full_content TEXT,
    summary TEXT,  -- AI-сгенерированное краткое описание для фильтра
    gemini_file_uri TEXT,
    content_hash TEXT,
    image_urls TEXT[],  -- Массив ссылок на изображения (для отслеживания изменений)
    precedent_type TEXT DEFAULT 'precedent',  -- 'precedent' или 'interpretation'
    is_active BOOLEAN DEFAULT TRUE,
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (server_id, source_thread_id)
);

-- Миграция: добавление колонки image_urls если её нет
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'precedents' AND column_name = 'image_urls'
    ) THEN
        ALTER TABLE precedents ADD COLUMN image_urls TEXT[];
    END IF;
END $$;

-- Создание индекса для быстрого поиска активных прецедентов по серверу
CREATE INDEX IF NOT EXISTS idx_precedents_server_active
ON precedents(server_id, is_active);

-- Комментарии к полям
COMMENT ON TABLE precedents IS 'Судебные прецеденты и толкования, извлеченные из темы-реестра на форуме';
COMMENT ON COLUMN precedents.source_url IS 'URL темы с прецедентом на форуме';
COMMENT ON COLUMN precedents.source_thread_id IS 'ID темы на форуме (извлекается из URL)';
COMMENT ON COLUMN precedents.title IS 'Заголовок темы с прецедентом';
COMMENT ON COLUMN precedents.full_content IS 'Полный текст судебного решения (последнее сообщение темы)';
COMMENT ON COLUMN precedents.summary IS 'AI-сгенерированное краткое описание (50-150 слов) для фильтрации';
COMMENT ON COLUMN precedents.gemini_file_uri IS 'URI файла в Google Cloud Storage для Gemini API';
COMMENT ON COLUMN precedents.content_hash IS 'MD5-хеш содержимого для отслеживания изменений';
COMMENT ON COLUMN precedents.image_urls IS 'Массив URL изображений судебного решения (для отслеживания изменений)';
COMMENT ON COLUMN precedents.precedent_type IS 'Тип: precedent (прецедент) или interpretation (толкование)';
COMMENT ON COLUMN precedents.is_active IS 'Актуальность: FALSE если ссылка в реестре зачёркнута';
