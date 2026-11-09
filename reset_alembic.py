import asyncio
import logging
import os
import sys

# Добавляем корневую директорию проекта в sys.path, чтобы Python видел наши модули
sys.path.append(os.getcwd())

from database.manager import db_manager

# Настройка логирования
logging.basicConfig(level=logging.INFO)

async def reset_alembic_version():
    """Сбрасывает версию миграции в таблице alembic_version на '001_initial'."""
    try:
        print("Подключаюсь к базе данных...")
        await db_manager.connect()
        print("Успешное подключение.")

        print("Сбрасываю таблицу alembic_version...")
        # Очищаем таблицу версий
        await db_manager.execute("DELETE FROM alembic_version;")
        
        # Вставляем нужную версию
        await db_manager.execute("INSERT INTO alembic_version (version_num) VALUES ('001_initial');")
        
        print("✅ УСПЕХ! Версия сброшена на '001_initial'.")
        print("Теперь запустите: alembic upgrade head")

    except Exception as e:
        print(f"❌ ОШИБКА: {e}")
    finally:
        await db_manager.close()

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(reset_alembic_version())
