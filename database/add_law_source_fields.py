"""
Миграция: добавление полей source_thread_id и source_url в таблицу law_documents.

Эти поля необходимы для отслеживания источников законов и их уникальности.
Запуск: python -m database.add_law_source_fields
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


async def add_law_source_fields(conn: asyncpg.Connection):
    """
    Добавляет поля source_thread_id и source_url в таблицу law_documents (если их еще нет).
    """
    print("Adding fields 'source_thread_id' and 'source_url' to 'law_documents' table...")

    # Проверяем, существуют ли уже эти поля
    try:
        # Добавляем source_thread_id
        await conn.execute("""
            ALTER TABLE law_documents 
            ADD COLUMN IF NOT EXISTS source_thread_id INTEGER;
        """)
        print("  - Field 'source_thread_id' added (or already exists)")

        # Добавляем source_url
        await conn.execute("""
            ALTER TABLE law_documents 
            ADD COLUMN IF NOT EXISTS source_url TEXT;
        """)
        print("  - Field 'source_url' added (or already exists)")

        # Создаем уникальный индекс на source_thread_id и server_id
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_law_documents_server_thread 
            ON law_documents(server_id, source_thread_id) 
            WHERE source_thread_id IS NOT NULL;
        """)
        print("  - Unique index on (server_id, source_thread_id) created")

        print("Migration completed successfully!")

    except Exception as e:
        print(f"Error during migration: {e}")
        raise


async def main():
    """Подключается к БД и выполняет миграцию."""
    if not DB_PASS:
        print("FATAL: DB_PASS environment variable is not set. Please check your .env file.")
        return

    try:
        conn = await asyncpg.connect(DATABASE_URL)
        print("Successfully connected to PostgreSQL.")
        await add_law_source_fields(conn)
        await conn.close()
        print("Connection closed.")
    except Exception as e:
        print(f"An error occurred during migration: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

