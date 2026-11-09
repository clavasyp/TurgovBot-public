-- Миграция: Добавление таблицы projects и колонки project_id в servers
-- Запустите этот скрипт в pgAdmin или psql перед запуском бота

-- 1. Создаём таблицу projects (если не существует)
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    display_name TEXT UNIQUE NOT NULL,
    description TEXT,
    forum_base_url TEXT,
    is_disabled BOOLEAN DEFAULT FALSE
);

-- 2. Добавляем колонку project_id в servers (если не существует)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'servers' AND column_name = 'project_id'
    ) THEN
        ALTER TABLE servers ADD COLUMN project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE;
    END IF;
END $$;

-- 3. Вставляем начальные данные для проектов
INSERT INTO projects (project_id, display_name, description, forum_base_url)
VALUES
    ('majestic_rp', 'Majestic RP', 'Официальные серверы Majestic RP', 'https://forum.majestic-rp.ru'),
    ('gta5rp', 'GTA 5 RP', 'Официальные серверы GTA 5 RP', 'https://forum.gta5rp.com')
ON CONFLICT (project_id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    description = EXCLUDED.description,
    forum_base_url = EXCLUDED.forum_base_url;

-- 4. Обновляем существующие серверы, привязывая их к majestic_rp
UPDATE servers SET project_id = 'majestic_rp' WHERE project_id IS NULL;

SELECT 'Migration completed successfully!' as status;

