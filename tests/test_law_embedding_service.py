from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from services import law_embedding_service as embedding_jobs


class _RecordingConnection:
    def __init__(self, statuses=None):
        self.calls = []
        self._statuses = iter(statuses or [])

    async def execute(self, query, *args):
        self.calls.append((query, args))
        return next(self._statuses, "UPDATE 1")


def _install_transaction(monkeypatch, conn):
    @asynccontextmanager
    async def transaction():
        yield conn

    monkeypatch.setattr(embedding_jobs.db_manager, "transaction", transaction)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "recover,table_name",
    [
        (embedding_jobs.recover_stale_jobs, "law_embedding_jobs"),
        (
            embedding_jobs.recover_stale_related_rebuilds,
            "law_related_rebuild_jobs",
        ),
    ],
)
async def test_stale_recovery_never_leaves_exhausted_lease_pending(
    monkeypatch, recover, table_name
):
    captured = {}

    async def execute(query, *args):
        captured["query"] = query
        captured["args"] = args
        return "UPDATE 7"

    monkeypatch.setattr(embedding_jobs.db_manager, "execute", execute)

    recovered = await recover(41, max_attempts=3)

    assert recovered == 7
    assert table_name in captured["query"]
    assert "CASE WHEN attempts >= $2 THEN 'failed' ELSE 'pending' END" in captured["query"]
    assert "WHERE status = 'processing'" in captured["query"]
    assert "locked_at IS NULL" in captured["query"]
    assert captured["args"] == (41, 3)


@pytest.mark.asyncio
async def test_embedding_lease_release_is_owned_and_compensates_attempt(monkeypatch):
    conn = _RecordingConnection(["UPDATE 1", "UPDATE 0"])
    _install_transaction(monkeypatch, conn)
    jobs = [
        {"id": 10, "input_hash": "hash-a", "locked_at": "lease-a"},
        {"id": 11, "input_hash": "hash-b", "locked_at": "old-lease"},
    ]

    released = await embedding_jobs._release_embedding_leases(jobs)

    assert released == 1
    query, args = conn.calls[0]
    assert "attempts = greatest(attempts - 1, 0)" in query
    assert "id = $1 AND input_hash = $2" in query
    assert "status = 'processing' AND locked_at = $3" in query
    assert args[:3] == (10, "hash-a", "lease-a")


@pytest.mark.asyncio
async def test_related_lease_release_is_owned_and_compensates_attempt(monkeypatch):
    conn = _RecordingConnection(["UPDATE 1"])
    _install_transaction(monkeypatch, conn)
    jobs = [
        {"server_id": "memphis", "space_id": "frida-v1", "locked_at": "lease-r"}
    ]

    released = await embedding_jobs._release_related_rebuild_leases(jobs)

    assert released == 1
    query, args = conn.calls[0]
    assert "attempts = greatest(attempts - 1, 0)" in query
    assert "server_id = $1 AND space_id = $2" in query
    assert "status = 'processing' AND locked_at = $3" in query
    assert args[:3] == ("memphis", "frida-v1", "lease-r")


@pytest.mark.asyncio
async def test_cancelled_embedding_batch_releases_claims_immediately(monkeypatch):
    job = {
        "id": 1,
        "article_id": 10,
        "server_id": "miami",
        "space_id": "frida-v1",
        "input_hash": "hash",
        "attempts": 1,
        "keep_current": False,
        "locked_at": "lease",
    }
    started = asyncio.Event()
    never = asyncio.Event()
    released = []

    async def claim(*_args, **_kwargs):
        return [job]

    async def process(_jobs):
        started.set()
        await never.wait()

    async def release(jobs, **_kwargs):
        released.extend(jobs)
        return len(jobs)

    monkeypatch.setattr(embedding_jobs, "_claim_jobs", claim)
    monkeypatch.setattr(embedding_jobs, "_process_claimed_embedding_jobs", process)
    monkeypatch.setattr(embedding_jobs, "_release_embedding_leases", release)

    task = asyncio.create_task(embedding_jobs.process_embedding_job_batch(limit=1))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert released == [job]


@pytest.mark.asyncio
async def test_inference_watchdog_consumes_attempt_marks_terminal_and_propagates(monkeypatch):
    from services.law_embedder import LawEmbeddingInferenceTimeout

    job = {
        "id": 2,
        "article_id": 20,
        "server_id": "miami",
        "space_id": "frida-v1",
        "input_hash": "watchdog-hash",
        "attempts": 5,
        "keep_current": False,
        "locked_at": "watchdog-lease",
    }
    conn = _RecordingConnection(["UPDATE 1"])
    _install_transaction(monkeypatch, conn)

    async def claim(*_args, **_kwargs):
        return [job]

    async def process(_jobs):
        raise LawEmbeddingInferenceTimeout("wedged torch chunk")

    async def forbidden_release(*_args, **_kwargs):
        raise AssertionError("watchdog must spend the attempt, not compensate it")

    monkeypatch.setattr(embedding_jobs, "_claim_jobs", claim)
    monkeypatch.setattr(embedding_jobs, "_process_claimed_embedding_jobs", process)
    monkeypatch.setattr(embedding_jobs, "_release_embedding_leases", forbidden_release)

    with pytest.raises(LawEmbeddingInferenceTimeout):
        await embedding_jobs.process_embedding_job_batch(limit=1)

    assert len(conn.calls) == 1
    query, args = conn.calls[0]
    assert "SET status = 'failed'" in query
    assert "attempts = greatest(attempts - 1, 0)" not in query
    assert "status = 'processing'" in query
    assert "locked_at = $5" in query
    assert args[0:2] == (2, "watchdog-hash")
    assert "LawEmbeddingInferenceTimeout" in args[2]
    assert args[3:] == (32, "watchdog-lease")
    # The claim already returned attempts=max; status='failed' therefore makes
    # this row terminal and _claim_jobs(attempts < max) cannot loop forever.
    assert job["attempts"] == 5


@pytest.mark.asyncio
async def test_cancelled_related_batch_releases_claims_immediately(monkeypatch):
    job = {
        "server_id": "denver",
        "space_id": "frida-v1",
        "attempts": 1,
        "locked_at": "lease-related",
    }
    started = asyncio.Event()
    never = asyncio.Event()
    released = []

    async def claim(*_args, **_kwargs):
        return [job]

    async def process(_jobs, **_kwargs):
        started.set()
        await never.wait()

    async def release(jobs, **_kwargs):
        released.extend(jobs)
        return len(jobs)

    monkeypatch.setattr(embedding_jobs, "_claim_related_rebuild_jobs", claim)
    monkeypatch.setattr(embedding_jobs, "_process_claimed_related_rebuilds", process)
    monkeypatch.setattr(embedding_jobs, "_release_related_rebuild_leases", release)

    task = asyncio.create_task(embedding_jobs.process_related_rebuild_batch(limit=1))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert released == [job]


@pytest.mark.asyncio
async def test_targeted_retry_does_not_force_refresh_valid_vectors(monkeypatch):
    conn = _RecordingConnection(["DELETE 1", "UPDATE 2", "UPDATE 1"])
    _install_transaction(monkeypatch, conn)

    result = await embedding_jobs.retry_terminal_jobs(
        "orlando", space_id="frida-v1", max_attempts=5
    )

    assert result == {"cleaned_valid": 1, "embedding": 2, "related": 1}
    cleanup_query, cleanup_args = conn.calls[0]
    retry_query, retry_args = conn.calls[1]
    assert cleanup_query.lstrip().startswith("DELETE FROM law_embedding_jobs")
    assert "e.input_hash = j.input_hash" in cleanup_query
    assert "a.embedding_local_input_hash = j.input_hash" in cleanup_query
    assert "SET status = 'pending', attempts = 0" in retry_query
    assert "NOT j.keep_current" in retry_query
    assert "keep_current = TRUE" not in retry_query
    assert cleanup_args == retry_args == ("orlando", "frida-v1", 5)


@pytest.mark.asyncio
async def test_scheduler_selects_server_with_fewest_remaining_jobs(monkeypatch):
    captured = {}

    async def fetchval(query, *args):
        captured["query"] = query
        captured["args"] = args
        return "portland"

    monkeypatch.setattr(embedding_jobs.db_manager, "fetchval", fetchval)

    selected = await embedding_jobs._select_next_embedding_server(max_attempts=4)

    assert selected == "portland"
    assert "count(*) AS jobs_left" in captured["query"]
    assert "ORDER BY remaining.jobs_left ASC" in captured["query"]
    assert "status IN ('pending', 'failed')" in captured["query"]
    assert captured["args"] == (embedding_jobs.active_space_id(), 4)


@pytest.mark.asyncio
async def test_background_iteration_passes_selected_server_to_skip_locked_claim(monkeypatch):
    calls = []

    async def select_server():
        return "washington"

    async def process_embedding(*, server_id=None, **_kwargs):
        calls.append(("embedding", server_id))
        return 3, {server_id}

    async def rebuild_drained(server_ids):
        calls.append(("drained", tuple(server_ids)))

    async def process_related(**_kwargs):
        calls.append(("related", None))
        return 1

    monkeypatch.setattr(embedding_jobs, "_select_next_embedding_server", select_server)
    monkeypatch.setattr(embedding_jobs, "process_embedding_job_batch", process_embedding)
    monkeypatch.setattr(embedding_jobs, "_rebuild_drained_servers", rebuild_drained)
    monkeypatch.setattr(embedding_jobs, "process_related_rebuild_batch", process_related)

    result = await embedding_jobs._process_background_worker_iteration()

    assert result == (3, 1)
    assert calls == [
        ("embedding", "washington"),
        ("drained", ("washington",)),
        ("related", None),
    ]


@pytest.mark.asyncio
async def test_stop_event_cancels_inflight_background_iteration(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    never = asyncio.Event()

    async def iteration():
        started.set()
        try:
            await never.wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        embedding_jobs, "_process_background_worker_iteration", iteration
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        embedding_jobs._background_iteration_until_stopped(stop_event)
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    stop_event.set()
    result = await asyncio.wait_for(task, timeout=1)

    assert result is None
    assert cancelled.is_set()


def test_claim_query_keeps_multi_worker_skip_locked():
    # A focused scheduler is only a hint. Correctness with multiple workers is
    # still provided by the existing atomic claim query.
    import inspect

    source = inspect.getsource(embedding_jobs._claim_jobs)
    assert "FOR UPDATE SKIP LOCKED" in source
