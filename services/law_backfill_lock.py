"""Global fail-fast lock shared by long law backfills.

Phase A (RAG/catalog) and Phase B (synonym enrichment) share Vertex quota and
database capacity.  A single PostgreSQL namespace prevents separate one-off
containers from accidentally running those heavy writers in parallel.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from database.manager import db_manager


logger = logging.getLogger(__name__)

LAW_BACKFILL_GLOBAL_LOCK_NAME = "law-backfill-global"


class LawBackfillLockBusy(RuntimeError):
    """Another Phase A/Phase B backfill currently owns the global slot."""


@asynccontextmanager
async def law_backfill_global_lock(
    *,
    manager: Any = None,
) -> AsyncIterator[None]:
    """Acquire the shared backfill slot without waiting or pinning a waiter."""

    manager = manager or db_manager
    async with manager.connection() as connection:
        acquired = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
            LAW_BACKFILL_GLOBAL_LOCK_NAME,
        )
        if acquired is not True:
            raise LawBackfillLockBusy(
                "another law Phase A/Phase B backfill is already running"
            )
        try:
            yield
        finally:
            try:
                unlocked = await connection.fetchval(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))",
                    LAW_BACKFILL_GLOBAL_LOCK_NAME,
                )
            except Exception:
                logger.exception(
                    "failed to explicitly release law backfill advisory lock; "
                    "the pool will reset the connection"
                )
            else:
                if unlocked is not True:
                    logger.error(
                        "law backfill advisory lock was no longer held: %s",
                        LAW_BACKFILL_GLOBAL_LOCK_NAME,
                    )
