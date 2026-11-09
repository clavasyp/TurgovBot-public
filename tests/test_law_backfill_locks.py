from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from services.law_backfill_lock import (
    LAW_BACKFILL_GLOBAL_LOCK_NAME,
    LawBackfillLockBusy,
    law_backfill_global_lock,
)
from services.law_document_lock import (
    law_document_lock_name,
    law_document_server_lock,
)


class _Connection:
    def __init__(self, *, acquire=True):
        self.acquire = acquire
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "pg_try_advisory_lock" in query:
            return self.acquire
        if "pg_advisory_unlock" in query:
            return True
        raise AssertionError(query)


class _Manager:
    def __init__(self, connection):
        self.value = connection

    @asynccontextmanager
    async def connection(self):
        yield self.value


@pytest.mark.asyncio
async def test_document_lock_uses_exact_shared_namespace_and_releases():
    connection = _Connection()
    manager = _Manager(connection)
    inside = False
    async with law_document_server_lock("las_vegas", manager=manager):
        inside = True
    assert inside
    assert law_document_lock_name("las_vegas") == (
        "law-document-sync:las_vegas"
    )
    assert connection.calls[0][0] == "execute"
    assert "pg_advisory_lock(hashtextextended($1, 0))" in connection.calls[0][1]
    assert connection.calls[0][2] == ("law-document-sync:las_vegas",)
    assert connection.calls[-1][2] == ("law-document-sync:las_vegas",)


@pytest.mark.asyncio
async def test_global_backfill_lock_is_fail_fast_and_does_not_unlock_other_owner():
    connection = _Connection(acquire=False)
    with pytest.raises(LawBackfillLockBusy):
        async with law_backfill_global_lock(manager=_Manager(connection)):
            raise AssertionError("must not enter")
    assert connection.calls == [
        (
            "fetchval",
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))",
            (LAW_BACKFILL_GLOBAL_LOCK_NAME,),
        )
    ]


@pytest.mark.asyncio
async def test_global_backfill_lock_releases_after_body_error():
    connection = _Connection(acquire=True)
    with pytest.raises(RuntimeError, match="body"):
        async with law_backfill_global_lock(manager=_Manager(connection)):
            raise RuntimeError("body")
    assert connection.calls[0][2] == ("law-backfill-global",)
    assert connection.calls[-1][2] == ("law-backfill-global",)
    assert "pg_advisory_unlock" in connection.calls[-1][1]
