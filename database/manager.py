"""
Менеджер подключений к базе данных PostgreSQL.

Этот модуль предоставляет централизованную точку доступа к базе данных через
класс-одиночку (Singleton) `DatabaseManager`.
"""

import asyncpg
import logging
from contextlib import asynccontextmanager

# Импортируем единый DATABASE_URL из config.py (единая точка конфигурации)
from config import DATABASE_URL


class DatabaseManager:
    """
    Класс, инкапсулирующий логику работы с пулом соединений `asyncpg`.

    Предоставляет асинхронные методы-обертки (`fetch`, `execute` и т.д.)
    для выполнения SQL-запросов. Использование пула соединений (`self._pool`)
    является ключевым для производительности в асинхронных приложениях,
    так как он управляет жизненным циклом соединений, переиспользуя их.
    """

    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool: asyncpg.Pool = None

    async def connect(self):
        """
        Инициализирует пул соединений `asyncpg`.

        Этот метод должен вызываться один раз при старте приложения
        (например, в `post_init` обработчике `telegram.ext.Application`).
        """
        if not self._pool:
            try:
                self._pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10)
                logging.info("Пул соединений с базой данных успешно создан.")
            except Exception as e:
                logging.critical("Ошибка при создании пула соединений с базой данных.", exc_info=e)
                # Критично важно прервать запуск, если БД недоступна.
                raise

    async def close(self):
        """
        Корректно закрывает пул соединений.

        Должен вызываться при штатном завершении работы приложения
        (например, в `post_shutdown` обработчике).
        """
        if self._pool:
            await self._pool.close()
            logging.info("Пул соединений с базой данных успешно закрыт.")

    async def fetch(self, query: str, *args):
        """
        Выполняет запрос и возвращает список всех найденных записей (`asyncpg.Record`).
        Оптимален для `SELECT` запросов, ожидающих несколько строк.
        """
        async with self._pool.acquire() as connection:
            return await connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        """
        Выполняет запрос и возвращает первую найденную запись (`asyncpg.Record`) или `None`.
        Идеален для `SELECT ... WHERE id = ...` запросов.
        """
        async with self._pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        """
        Выполняет запрос и возвращает значение из первой колонки первой строки или `None`.
        Удобен для запросов `SELECT COUNT(*)` или `SELECT id ...`.
        """
        async with self._pool.acquire() as connection:
            return await connection.fetchval(query, *args)

    async def execute(self, query: str, *args):
        """
        Выполняет команду (`INSERT`, `UPDATE`, `DELETE`) и возвращает статус выполнения.
        Не предназначен для извлечения данных.
        """
        async with self._pool.acquire() as connection:
            return await connection.execute(query, *args)

    @asynccontextmanager
    async def connection(self):
        """Выдаёт одно соединение из пула без открытия транзакции.

        Нужен для session-level операций (например, PostgreSQL advisory lock),
        которые должны жить на одном и том же соединении дольше одного SQL-запроса.
        """
        async with self._pool.acquire() as connection:
            yield connection

    @asynccontextmanager
    async def transaction(self):
        """
        Асинхронный контекст: одно соединение с открытой транзакцией.

        Все запросы внутри блока выполнять через отданное соединение (`conn.execute(...)`),
        а не через методы менеджера — те берут другие соединения из пула и в транзакцию
        не попадают. Коммит при выходе, роллбэк при исключении.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                yield connection


# Глобальный экземпляр менеджера.
# Импортируется в другие модули для обеспечения единой точки доступа к БД.
db_manager = DatabaseManager(DATABASE_URL)
