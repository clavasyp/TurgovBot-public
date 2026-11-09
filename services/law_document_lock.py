"""Shared PostgreSQL lock for one server's ``law_documents`` generation.

Forum sync, manual recovery and long-running backfills must use the exact same
advisory-lock namespace.  Keeping the primitive here prevents a one-off script
from copying a nearly-identical lock name and racing the normal update path.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from database.manager import db_manager


logger = logging.getLogger(__name__)

LAW_DOCUMENT_LOCK_PREFIX = "law-document-sync:"


def law_document_lock_name(server_id: str) -> str:
    """Return the stable cross-process advisory-lock name for ``server_id``."""

    normalized = str(server_id or "").strip()
    if not normalized:
        raise ValueError("server_id must not be empty")
    return f"{LAW_DOCUMENT_LOCK_PREFIX}{normalized}"


@asynccontextmanager
async def law_document_server_lock(
    server_id: str,
    *,
    local_lock: Optional[Any] = None,
    connection_slots: Optional[Any] = None,
    manager: Any = None,
) -> AsyncIterator[None]:
    """Serialize one server's source-document generation across processes.

    ``local_lock`` and ``connection_slots`` are optional because the bot owns
    process-local fan-out control, while a dedicated one-server backfill does
    not need another local queue.  ``manager`` is injectable so existing bot
    tests and alternate runtimes can use the same lock implementation.
    """

    manager = manager or db_manager
    lock_name = law_document_lock_name(server_id)

    @asynccontextmanager
    async def _optional_context(context: Optional[Any]) -> AsyncIterator[None]:
        if context is None:
            yield
            return
        async with context:
            yield

    # Take the cheap in-process lock before reserving a pool connection.
    async with _optional_context(local_lock):
        async with _optional_context(connection_slots):
            async with manager.connection() as connection:
                await connection.execute(
                    "SELECT pg_advisory_lock(hashtextextended($1, 0))",
                    lock_name,
                )
                try:
                    yield
                finally:
                    try:
                        unlocked = await connection.fetchval(
                            "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                            lock_name,
                        )
                    except Exception:
                        # asyncpg resets session state before a broken connection
                        # returns to the pool, so logging is safer than masking a
                        # possible exception from the protected body.
                        logger.exception(
                            "Не удалось явно освободить advisory lock %s; "
                            "соединение будет сброшено пулом.",
                            lock_name,
                        )
                    else:
                        if unlocked is not True:
                            logger.error(
                                "PostgreSQL law-document sync advisory lock уже "
                                "не удерживался: %s",
                                lock_name,
                            )
