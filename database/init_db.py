# START OF FILE init_db.py

"""
Скрипт для инициализации схемы базы данных PostgreSQL.

⚠️ DEPRECATED: Этот модуль устарел. Используйте Alembic для управления миграциями:
    python -m database.migrate upgrade

Подробнее: database/MIGRATIONS_README.md

---

Этот модуль отвечает за создание всех необходимых таблиц для работы приложения.
Скрипт является идемпотентным: он использует конструкцию `CREATE TABLE IF NOT EXISTS`,
что позволяет безопасно запускать его многократно, не опасаясь ошибок или потери данных.

Конфигурация подключения к БД загружается из переменных окружения,
что является стандартной практикой для безопасного управления учетными данными.
"""

import warnings
import asyncpg
import asyncio
import os
from dotenv import load_dotenv

# Загрузка учетных данных из .env файла для удобства локальной разработки.
load_dotenv()

# Используем единый DATABASE_URL из config.py
from config import DATABASE_URL

# Для обратной совместимости
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")


async def create_tables(conn: asyncpg.Connection):
    """
    Последовательно выполняет SQL-запросы для создания таблиц в базе данных.

    Определяет основную структуру данных приложения, включая пользователей,
    серверы, лимиты использования и историю сообщений.
    Использование `ON DELETE CASCADE` обеспечивает целостность данных при удалении
    связанных сущностей (например, пользователя).
    """
    print("Creating table 'users'...")
    await conn.execute("""
                       -- Таблица для хранения информации о пользователях бота.
                       CREATE TABLE IF NOT EXISTS users
                       (
                           user_id
                           BIGINT
                           PRIMARY
                           KEY,
                           full_name
                           TEXT
                           NOT
                           NULL,
                           username
                           TEXT, -- Может быть NULL, так как не все пользователи его имеют
                           is_admin
                           BOOLEAN
                           DEFAULT
                           FALSE,
                           is_vip
                           BOOLEAN
                           DEFAULT
                           FALSE,
                           is_banned
                           BOOLEAN
                           DEFAULT
                           FALSE,
                           first_seen
                           TIMESTAMPTZ
                           DEFAULT
                           NOW(),
                           last_seen TIMESTAMPTZ DEFAULT NOW(),
                           default_project_id TEXT,
                           default_server_id TEXT,
                           default_question_type TEXT
                       );
                       """)

    print("Creating table 'user_usage'...")
    await conn.execute("""
                       -- Таблица для отслеживания суточных лимитов запросов.
                       CREATE TABLE IF NOT EXISTS user_usage
                       (
                           user_id
                           BIGINT
                           REFERENCES
                           users
                       (
                           user_id
                       ) ON DELETE CASCADE,
                           usage_date DATE DEFAULT CURRENT_DATE,
                           detailed_reqs INTEGER DEFAULT 0,
                           brief_reqs INTEGER DEFAULT 0,
                           interview_gens INTEGER DEFAULT 0,
                           complaint_gens INTEGER DEFAULT 0,
                           PRIMARY KEY
                       (
                           user_id,
                           usage_date
                       ) -- Уникальность записи по пользователю и дате.
                           );
                       """)

    print("Creating table 'projects'...")
    await conn.execute("""
        -- Справочник проектов (Majestic RP, GTA 5 RP и т.д.)
        CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY,
            display_name TEXT UNIQUE NOT NULL,
            description TEXT,
            forum_base_url TEXT,
            is_disabled BOOLEAN DEFAULT FALSE
        );
    """)

    # Миграция: добавляем начальные проекты (если их нет)
    print("Inserting default projects...")
    await conn.execute("""
        INSERT INTO projects (project_id, display_name, description, forum_base_url)
        VALUES 
            ('majestic_rp', 'Majestic RP', 'Официальные серверы Majestic RP', 'https://forum.majestic-rp.ru'),
            ('gta5rp', 'GTA 5 RP', 'Официальные серверы GTA 5 RP', 'https://forum.gta5rp.com')
        ON CONFLICT (project_id) DO NOTHING;
    """)

    print("Creating table 'servers'...")
    await conn.execute("""
        -- Справочник серверов, с которыми работает бот.
        -- Наполняется из config.py при старте приложения.
        CREATE TABLE IF NOT EXISTS servers (
            server_id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE,
            display_name TEXT NOT NULL,
            is_disabled BOOLEAN DEFAULT FALSE
        );
    """)

    # Миграция: добавляем колонку project_id если её нет (для существующих БД)
    print("Checking if 'servers.project_id' column exists...")
    column_exists = await conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = 'servers' AND column_name = 'project_id'
        );
    """)

    if not column_exists:
        print("Adding 'project_id' column to 'servers' table...")
        await conn.execute("""
            ALTER TABLE servers ADD COLUMN project_id TEXT REFERENCES projects(project_id) ON DELETE CASCADE;
        """)
        # Привязываем существующие серверы к majestic_rp
        print("Linking existing servers to 'majestic_rp' project...")
        await conn.execute("""
            UPDATE servers SET project_id = 'majestic_rp' WHERE project_id IS NULL;
        """)
    else:
        print("Column 'servers.project_id' already exists, skipping migration.")

    print("Creating table 'law_documents' with 'content_hash' field...")
    await conn.execute("""
        -- Хранилище текстов законов и связанных с ними URI файлов в Gemini.
        CREATE TABLE IF NOT EXISTS law_documents (
            id SERIAL PRIMARY KEY,
            server_id TEXT REFERENCES servers(server_id) ON DELETE CASCADE,
            source_thread_id INTEGER,  -- ID темы на форуме
            source_url TEXT,  -- URL источника закона
            title TEXT NOT NULL,
            content TEXT,
            gemini_file_uri TEXT,  -- URI файла, загруженного в Gemini API / GCS.
            content_hash TEXT,  -- MD5-хеш содержимого файла.
            derived_sync_pending BOOLEAN NOT NULL DEFAULT FALSE,
            last_updated TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (server_id, source_thread_id)  -- Уникальность по серверу и ID темы.
        );
    """)
    print("Creating table 'message_history'...")
    await conn.execute("""
                       -- Таблица для хранения истории диалогов с пользователями.
                       CREATE TABLE IF NOT EXISTS message_history
                       (
                           id
                           SERIAL
                           PRIMARY
                           KEY,
                           user_id
                           BIGINT
                           REFERENCES
                           users
                       (
                           user_id
                       ) ON DELETE CASCADE,
                           server_id TEXT, -- Сервер, в контексте которого велся диалог.
                           role TEXT NOT NULL, -- 'user' или 'model' в соответствии с Gemini API.
                           content TEXT,
                           timestamp TIMESTAMPTZ DEFAULT NOW()
                       );
                       """)

    print("Creating table 'court_cases'...")
    await conn.execute("""
                       -- Судебные дела (темы с форума)
                       CREATE TABLE IF NOT EXISTS court_cases
                       (
                           id SERIAL PRIMARY KEY,
                           user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                           server_name VARCHAR(100) NOT NULL,
                           thread_url TEXT NOT NULL UNIQUE,
                           thread_id VARCHAR(50),
                           case_title TEXT,
                           plaintiff VARCHAR(200),
                           defendant VARCHAR(200),
                           status VARCHAR(20) DEFAULT 'active',
                           created_at TIMESTAMPTZ DEFAULT NOW(),
                           closed_at TIMESTAMPTZ,
                           last_updated TIMESTAMPTZ DEFAULT NOW()
                       );
                       """)

    print("Creating table 'court_messages'...")
    await conn.execute("""
                       -- Сообщения из судебного дела
                       CREATE TABLE IF NOT EXISTS court_messages
                       (
                           id SERIAL PRIMARY KEY,
                           court_case_id INTEGER REFERENCES court_cases(id) ON DELETE CASCADE,
                           message_order INTEGER NOT NULL,
                           author VARCHAR(200),
                           author_role VARCHAR(20),
                           message_timestamp TIMESTAMPTZ,
                           text_content TEXT,
                           has_images BOOLEAN DEFAULT FALSE,
                           created_at TIMESTAMPTZ DEFAULT NOW()
                       );
                       """)

    print("Creating table 'court_images'...")
    await conn.execute("""
                       -- Изображения из судебных дел (постановления и документы)
                       CREATE TABLE IF NOT EXISTS court_images
                       (
                           id SERIAL PRIMARY KEY,
                           court_message_id INTEGER REFERENCES court_messages(id) ON DELETE CASCADE,
                           image_url TEXT NOT NULL,
                           bbcode_tag TEXT,
                           ocr_text TEXT,
                           document_type VARCHAR(50),
                           processed_at TIMESTAMPTZ
                       );
                       """)

    print("Creating table 'court_dialogue_logs'...")
    await conn.execute("""
                       -- Логи диалогов с судебным помощником
                       CREATE TABLE IF NOT EXISTS court_dialogue_logs
                       (
                           id SERIAL PRIMARY KEY,
                           court_case_id INTEGER REFERENCES court_cases(id) ON DELETE CASCADE,
                           user_message TEXT NOT NULL,
                           assistant_response TEXT NOT NULL,
                           used_tools TEXT[],
                           created_at TIMESTAMPTZ DEFAULT NOW()
                       );
                       """)

    print("Creating table 'complaints_history'...")
    await conn.execute("""
                       -- История составленных жалоб и исков
                       CREATE TABLE IF NOT EXISTS complaints_history
                       (
                           id SERIAL PRIMARY KEY,
                           user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                           server_id TEXT,
                           complaint_type TEXT NOT NULL, -- 'complaint' или 'lawsuit'
                           forum_url TEXT NOT NULL,
                           situation_description TEXT NOT NULL,
                           generated_text TEXT NOT NULL,
                           generated_bb_code TEXT NOT NULL,
                           created_at TIMESTAMPTZ DEFAULT NOW()
                       );
                       """)

    print("Creating table 'precedents'...")
    await conn.execute("""
                       -- Судебные прецеденты и толкования
                       CREATE TABLE IF NOT EXISTS precedents
                       (
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
                       """)

    print("Creating index 'idx_precedents_server_active'...")
    await conn.execute("""
                       CREATE INDEX IF NOT EXISTS idx_precedents_server_active 
                       ON precedents(server_id, is_active);
                       """)

    print("All tables created or already exist.")

    # Запускаем миграции для добавления новых колонок в существующие таблицы
    await run_migrations(conn)


async def add_column_if_not_exists(conn, table_name: str, column_name: str, column_definition: str):
    """
    Добавляет колонку в таблицу, если она ещё не существует.

    Args:
        conn: Соединение с БД
        table_name: Имя таблицы
        column_name: Имя новой колонки
        column_definition: SQL-определение колонки (например: 'TEXT', 'INTEGER DEFAULT 0', 'TEXT[]')
    """
    exists = await conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns 
            WHERE table_name = $1 AND column_name = $2
        )
    """, table_name, column_name)

    if not exists:
        print(f"  Adding column '{column_name}' to table '{table_name}'...")
        await conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")
        print(f"  ✓ Column '{column_name}' added successfully.")
    else:
        print(f"  Column '{column_name}' already exists in '{table_name}', skipping.")


async def run_migrations(conn):
    """
    Запускает миграции для добавления новых колонок в существующие таблицы.
    Добавляйте новые миграции в конец этой функции.
    """
    print("\n--- Running migrations ---")

    # === МИГРАЦИИ ДЛЯ ТАБЛИЦЫ precedents ===
    # Миграция: добавление image_urls в precedents (2024-12-11)
    await add_column_if_not_exists(conn, 'precedents', 'image_urls', 'TEXT[]')

    # === МИГРАЦИИ ДЛЯ ДРУГИХ ТАБЛИЦ ===
    # Пример: await add_column_if_not_exists(conn, 'users', 'new_field', 'TEXT DEFAULT NULL')

    # Миграция: настройки шаблона вопросов пользователя (2026-05-09)
    await add_column_if_not_exists(conn, 'users', 'default_project_id', 'TEXT')
    await add_column_if_not_exists(conn, 'users', 'default_server_id', 'TEXT')
    await add_column_if_not_exists(conn, 'users', 'default_question_type', 'TEXT')
    await add_column_if_not_exists(
        conn,
        'law_documents',
        'derived_sync_pending',
        'BOOLEAN NOT NULL DEFAULT FALSE',
    )
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_law_documents_derived_sync_pending
        ON law_documents (server_id, source_thread_id)
        WHERE derived_sync_pending;
    """)

    # === ДОБАВЛЯЙТЕ НОВЫЕ МИГРАЦИИ НИЖЕ ===
    # Миграция: пример добавления нового поля
    # await add_column_if_not_exists(conn, 'table_name', 'column_name', 'COLUMN_TYPE DEFAULT value')

    print("--- Migrations completed ---\n")


async def main():
    """
    Основная функция-оркестратор. Устанавливает соединение с БД
    и запускает процесс создания таблиц.
    """
    # Предупреждение о deprecation
    warnings.warn(
        "\n⚠️  init_db.py устарел!\n"
        "   Используйте Alembic для управления миграциями:\n"
        "   python -m database.migrate upgrade\n"
        "   Подробнее: database/MIGRATIONS_README.md\n",
        DeprecationWarning,
        stacklevel=2
    )

    if not DB_PASS:
        print("FATAL: DB_PASS environment variable is not set. Please check your .env file.")
        return

    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Successfully connected to PostgreSQL.")
        await create_tables(conn)
        await conn.close()
        print("Connection closed.")
    except Exception as e:
        print(f"An error occurred during database initialization: {e}")


if __name__ == "__main__":
    # Запуск асинхронной функции main.
    asyncio.run(main())

# END OF FILE init_db.py
