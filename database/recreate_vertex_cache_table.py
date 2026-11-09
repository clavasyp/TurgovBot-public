"""
Миграция: удаление старой таблицы vertex_cache_metadata и создание новой с правильной структурой.

ВНИМАНИЕ: Это удалит все существующие кэши!
Запуск: python -m database.recreate_vertex_cache_table
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


async def recreate_vertex_cache_table(conn: asyncpg.Connection):
    """
    Удаляет старую таблицу vertex_cache_metadata и создает новую с правильной структурой.
    """
    print("Dropping old table 'vertex_cache_metadata' if exists...")
    await conn.execute("DROP TABLE IF EXISTS vertex_cache_metadata CASCADE;")
    print("Old table dropped successfully.")

    print("\nCreating new table 'vertex_cache_metadata'...")
    await conn.execute("""
        CREATE TABLE vertex_cache_metadata (
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
    print("New table created successfully.")

    print("\nCreating indexes on vertex_cache_metadata...")
    await conn.execute("""
        CREATE INDEX idx_vertex_cache_active 
        ON vertex_cache_metadata(server_id, is_active) 
        WHERE is_active = TRUE;
    """)

    await conn.execute("""
        CREATE INDEX idx_vertex_cache_expiry 
        ON vertex_cache_metadata(expires_at, is_active) 
        WHERE is_active = TRUE;
    """)
    print("Indexes created successfully.")

    print("\n✅ Table 'vertex_cache_metadata' recreated successfully!")
    print("NOTE: All old caches were deleted. New caches will be created automatically on first use.")


async def main():
    """Подключается к БД и выполняет миграцию."""
    if not DB_PASS:
        print("FATAL: DB_PASS environment variable is not set. Please check your .env file.")
        return

    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Successfully connected to PostgreSQL.")
        await recreate_vertex_cache_table(conn)
        await conn.close()
        print("\nMigration completed successfully. Connection closed.")
    except Exception as e:
        print(f"\n❌ An error occurred during migration: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

