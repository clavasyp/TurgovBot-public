# START OF FILE alembic/env.py

"""
Alembic environment configuration.

Этот файл настраивает Alembic для работы с вашей базой данных PostgreSQL.
Поддерживает как синхронные, так и асинхронные миграции.
"""

import os
from logging.config import fileConfig

from sqlalchemy import pool, create_engine

from alembic import context
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()

# Импортируем Base со всеми моделями для autogenerate
from database.models import Base

# Импортируем единый DATABASE_URL_SYNC из config.py
from config import DATABASE_URL_SYNC

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
target_metadata = Base.metadata


def get_database_url() -> str:
    """
    Возвращает синхронный URL (postgresql+psycopg2://) для Alembic.
    Использует единый DATABASE_URL_SYNC из config.py.
    """
    return DATABASE_URL_SYNC


def get_async_database_url() -> str:
    """
    Возвращает асинхронный URL подключения к БД.
    Формируется из DATABASE_URL_SYNC заменой драйвера.
    """
    return DATABASE_URL_SYNC.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.
    """
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,  # Сравнивать типы колонок
        compare_server_default=True,  # Сравнивать дефолтные значения
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    # Получаем URL и устанавливаем в конфиг
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = get_database_url()

    connectable = create_engine(
        configuration["sqlalchemy.url"],
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,  # Сравнивать типы колонок
            compare_server_default=True,  # Сравнивать дефолтные значения
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

# END OF FILE alembic/env.py

