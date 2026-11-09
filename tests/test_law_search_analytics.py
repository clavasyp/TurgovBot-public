"""Targeted tests for fail-open Overlay law-search quality logging."""

from __future__ import annotations

import json
import asyncio
import uuid

import pytest

from services.law_search_analytics import (
    LawSearchLogWriter,
    _event_args,
    build_search_event,
    is_law_search_logging_active,
)


def _response(count: int = 2):
    results = []
    for index in range(count):
        results.append(
            {
                "id": 100 + index,
                "code": f"1.{index}",
                "title": f"Article {index}",
                "doc_title": "Criminal code",
                "score": 0.05 - index * 0.001,
                "sources": ["fts", "vector"],
                "snippet": "must not be copied to analytics",
                "penalty_summary": "must not be copied either",
            }
        )
    return {
        "query": "question",
        "results": results,
        "related": [{"id": 999, "title": "Related"}],
        "degraded": False,
        "search_mode": "full",
        "warnings": [],
    }


def test_build_event_is_compact_versioned_and_privacy_minimised():
    event = build_search_event(
        server_id="memphis",
        query="гражданский стрелял по госнику",
        requested_limit=8,
        duration_ms=123.4,
        outcome="success",
        response=_response(25),
        diagnostics={
            "algorithm_version": "ranker-v7",
            "embedding_space": "frida-v1",
            "intent_kind": "protected_life_attack",
            "search_path": "hybrid",
            "query_normalized": "гражданский стрелял сотрудник",
            "query_features": {"codes": [], "tokens": ["гражданский", "стрелял"]},
            "legs": {"fts": {"status": "ok", "ids": [100]}},
            # Authentication data is neither expected nor promoted to columns.
            "device_id": "do-not-index",
        },
        tier="laws",
    )

    assert event["algorithm_version"] == "ranker-v7"
    assert event["intent_kind"] == "protected_life_attack"
    assert event["query_normalized"] == "гражданский стрелял сотрудник"
    assert event["result_count"] == 20
    assert event["top_article_id"] == 100
    assert event["top_score_gap"] == pytest.approx(0.001)
    assert event["related_ids"] == [999]
    assert len(event["query_hash"]) == 64
    serialized_results = json.dumps(event["results"], ensure_ascii=False)
    assert "snippet" not in serialized_results
    assert "penalty" not in serialized_results
    assert "user_id" not in event and "device_id" not in event
    assert "do-not-index" not in json.dumps(event, ensure_ascii=False)

    args = _event_args(event)
    assert len(args) == 25
    assert isinstance(args[0], uuid.UUID)
    assert "snippet" not in args[21]


def test_query_hash_preserves_word_order_but_ignores_harmless_formatting():
    common = {
        "server_id": "memphis",
        "requested_limit": 8,
        "duration_ms": 10,
        "outcome": "empty",
        "response": _response(0),
        # These two questions can have the same lossy lexical normalisation.
        "diagnostics": {
            "algorithm_version": "ranker-v1",
            "query_normalized": "гражданский стрелял сотрудник",
        },
    }
    first = build_search_event(
        query="Гражданский   стрелял по госнику",
        **common,
    )
    same = build_search_event(
        query="гражданский стрелял по госнику",
        **common,
    )
    reversed_roles = build_search_event(
        query="Госник стрелял по гражданскому",
        **common,
    )

    assert first["query_hash"] == same["query_hash"]
    assert first["query_hash"] != reversed_roles["query_hash"]


def test_queue_overflow_is_non_blocking_and_counted():
    writer = LawSearchLogWriter(
        pool=None,
        queue_size=1,
        batch_size=1,
        flush_seconds=0.01,
        db_timeout=0.1,
        retention_days=90,
    )
    assert writer.enqueue({"event_id": "first"}) is True
    assert writer.enqueue({"event_id": "second"}) is False
    assert writer.stats() == {
        "queued": 1,
        "enqueued": 1,
        "written": 0,
        "dropped": 1,
        "write_failures": 0,
    }


@pytest.mark.asyncio
async def test_batch_write_failure_is_swallowed_and_counted():
    class BrokenPool:
        async def executemany(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    writer = LawSearchLogWriter(
        BrokenPool(),
        queue_size=10,
        batch_size=10,
        flush_seconds=0.01,
        db_timeout=0.1,
        retention_days=90,
    )
    event = build_search_event(
        server_id="memphis",
        query="угон",
        requested_limit=8,
        duration_ms=20,
        outcome="empty",
        response=_response(0),
        diagnostics={"algorithm_version": "ranker-v1", "query_normalized": "угон"},
    )

    await writer._write_batch([event])

    assert writer.written == 0
    assert writer.write_failures == 1
    assert writer.dropped == 1


@pytest.mark.asyncio
async def test_graceful_stop_drains_queued_events(monkeypatch):
    class RecordingPool:
        def __init__(self):
            self.rows = []

        async def executemany(self, _query, rows):
            self.rows.extend(rows)

    async def skip_cleanup():
        return None

    pool = RecordingPool()
    writer = LawSearchLogWriter(
        pool,
        queue_size=10,
        batch_size=10,
        flush_seconds=0.01,
        db_timeout=0.2,
        retention_days=90,
    )
    monkeypatch.setattr(writer, "_cleanup_retention", skip_cleanup)
    writer.start()
    event = build_search_event(
        server_id="memphis",
        query="угон",
        requested_limit=8,
        duration_ms=20,
        outcome="empty",
        response=_response(0),
        diagnostics={"algorithm_version": "ranker-v1", "query_normalized": "угон"},
    )
    assert writer.enqueue(event)

    await writer.stop(timeout=1.0)

    assert writer.written == 1
    assert writer.dropped == 0
    assert len(pool.rows) == 1


@pytest.mark.asyncio
async def test_slow_retention_cleanup_does_not_block_event_writes(monkeypatch):
    class RecordingPool:
        def __init__(self):
            self.rows = []
            self.written = asyncio.Event()

        async def executemany(self, _query, rows):
            self.rows.extend(rows)
            self.written.set()

    cleanup_started = asyncio.Event()
    never_finishes = asyncio.Event()

    async def slow_cleanup():
        cleanup_started.set()
        await never_finishes.wait()

    pool = RecordingPool()
    writer = LawSearchLogWriter(
        pool,
        queue_size=10,
        batch_size=10,
        flush_seconds=0.01,
        db_timeout=0.2,
        retention_days=90,
    )
    monkeypatch.setattr(writer, "_cleanup_retention", slow_cleanup)
    writer.start()
    await asyncio.wait_for(cleanup_started.wait(), timeout=0.2)

    event = build_search_event(
        server_id="memphis",
        query="угон",
        requested_limit=8,
        duration_ms=20,
        outcome="empty",
        response=_response(0),
        diagnostics={"algorithm_version": "ranker-v1", "query_normalized": "угон"},
    )
    assert writer.enqueue(event)
    await asyncio.wait_for(pool.written.wait(), timeout=0.2)

    await writer.stop(timeout=1.0)

    assert writer.written == 1
    assert writer.dropped == 0


@pytest.mark.asyncio
async def test_shutdown_deadline_is_bounded_if_cleanup_suppresses_cancel():
    class Pool:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

    first_cancel_seen = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_cleanup():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancel_seen.set()
            await release.wait()

    pool = Pool()
    writer = LawSearchLogWriter(
        pool,
        queue_size=10,
        batch_size=10,
        flush_seconds=0.01,
        db_timeout=0.1,
        retention_days=90,
    )
    writer._task = asyncio.create_task(asyncio.sleep(0))
    cleanup_task = asyncio.create_task(stubborn_cleanup())
    writer._cleanup_task = cleanup_task
    await asyncio.sleep(0)

    started = asyncio.get_running_loop().time()
    await writer.stop(timeout=0.05)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.2
    assert first_cancel_seen.is_set()
    assert pool.terminated is True

    release.set()
    await asyncio.wait_for(cleanup_task, timeout=0.2)


def test_logging_active_requires_a_live_writer(monkeypatch):
    import services.law_search_analytics as analytics

    monkeypatch.setattr(analytics, "_writer", None)
    assert is_law_search_logging_active() is False


@pytest.mark.asyncio
async def test_retention_uses_transaction_lock_and_releases_pool_between_batches():
    class AsyncContext:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_args):
            return False

    class Connection:
        def __init__(self):
            self.transactions = 0
            self.deletes = 0

        def transaction(self):
            self.transactions += 1
            return AsyncContext(self)

        async def fetchval(self, query, _key):
            assert "pg_try_advisory_xact_lock" in query
            return True

        async def execute(self, *_args):
            self.deletes += 1
            return "DELETE 5000" if self.deletes == 1 else "DELETE 12"

    class Pool:
        def __init__(self):
            self.connection = Connection()
            self.acquires = 0

        def acquire(self):
            self.acquires += 1
            return AsyncContext(self.connection)

    pool = Pool()
    writer = LawSearchLogWriter(
        pool,
        queue_size=10,
        batch_size=10,
        flush_seconds=0.01,
        db_timeout=0.1,
        retention_days=90,
    )

    await writer._cleanup_retention()

    assert pool.acquires == 2
    assert pool.connection.transactions == 2
    assert pool.connection.deletes == 2
