"""
Миграция: добавление таблицы vertex_cache_metadata для управления кэшами Vertex AI.

Эта таблица хранит метаданные о созданных кэшах, их хэши и сроки действия.
Запуск: python -m database.add_vertex_cache_table
"""

import asyncpg
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# Добавляем корень проекта в path для импорта config
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import DATABASE_URL


async def add_vertex_cache_table(conn: asyncpg.Connection):
    """
    Создает таблицу для хранения метаданных кэшей Vertex AI Context Caching.
    Только для кэширования законов (промпты не кэшируются).
    """
    print("Creating table 'vertex_cache_metadata'...")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS vertex_cache_metadata (
            id SERIAL PRIMARY KEY,
            cache_type TEXT NOT NULL DEFAULT 'laws',  -- Всегда 'laws'
            server_id TEXT NOT NULL,                  -- ID сервера
            cache_name TEXT NOT NULL UNIQUE,          -- Уникальное имя кэша в Vertex AI
            laws_hash TEXT NOT NULL,                  -- MD5 хэш всех законов сервера
            created_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ NOT NULL,          -- Дата истечения (создание + 7 дней)
            is_active BOOLEAN DEFAULT TRUE,
            UNIQUE(server_id),                        -- Один кэш на сервер
            FOREIGN KEY (server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );
    """)

    print("Creating indexes on vertex_cache_metadata for faster lookups...")
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_vertex_cache_active 
        ON vertex_cache_metadata(server_id, is_active) 
        WHERE is_active = TRUE;
    """)

    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_vertex_cache_expiry 
        ON vertex_cache_metadata(expires_at, is_active) 
        WHERE is_active = TRUE;
    """)

    print("Table 'vertex_cache_metadata' created successfully with indexes.")
    print("NOTE: This table is simplified - only for caching laws, not prompts.")


async def main():
    """Подключается к БД и выполняет миграцию."""
    if not DB_PASS:
        print("FATAL: DB_PASS environment variable is not set. Please check your .env file.")
        return

    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Successfully connected to PostgreSQL.")
        await add_vertex_cache_table(conn)
        await conn.close()
        print("Migration completed successfully. Connection closed.")
    except Exception as e:
        print(f"An error occurred during migration: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

