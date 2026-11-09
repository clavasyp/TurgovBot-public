"""Fail-open, bounded telemetry writer for Overlay law-search quality.

This module is intentionally independent from ranking.  The request path only
builds a compact event and calls ``enqueue_search_event`` (Queue.put_nowait).
PostgreSQL writes, retention cleanup and all failures stay in one background
worker and can never change a search response.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Optional

import config

logger = logging.getLogger(__name__)

_INSERT_SQL = """
    INSERT INTO law_search_events (
        event_id, server_id, query_text, query_normalized, query_hash, tier,
        requested_limit, outcome, error_code, duration_ms, algorithm_version,
        embedding_space, intent_kind, search_path, search_mode, degraded,
        warnings, result_count, top_article_id, top_score, top_score_gap,
        results, related_ids, query_features, diagnostics
    )
    VALUES (
        $1::uuid, $2, $3, $4, $5, $6,
        $7, $8, $9, $10, $11,
        $12, $13, $14, $15, $16,
        $17::text[], $18, $19, $20, $21,
        $22::jsonb, $23::bigint[], $24::jsonb, $25::jsonb
    )
"""

_RETENTION_LOCK_KEY = 0x4C41575345415243  # "LAWSEARC", signed bigint-safe.
_RETENTION_BATCH_SIZE = 5_000
_RETENTION_MAX_BATCHES = 10
_RETENTION_INTERVAL_SECONDS = 60 * 60
_ALLOWED_OUTCOMES = {"success", "empty", "unavailable", "error", "cancelled"}
_ALLOWED_MODES = {"full", "partial", "keyword_only"}
_SENSITIVE_KEYS = {
    "authorization", "token", "jwt", "user_id", "device_id", "hwid",
    "ip", "client_ip", "api_key", "secret", "password",
}


def _is_sensitive_key(value: Any) -> bool:
    key = str(value).lower()
    return (
        key in _SENSITIVE_KEYS
        or key.endswith(("_token", "_secret", "_password", "_api_key"))
    )


def _clean_text(value: Any, limit: int) -> str:
    text = str(value or "").replace("\x00", "")
    return text[:limit]


def _query_fingerprint_basis(query_text: str) -> str:
    """Canonicalise harmless formatting while preserving meaning and word order."""
    return re.sub(r"\s+", " ", query_text.strip().lower().replace("ё", "е"))


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Bound diagnostic JSON so one event cannot grow without limit."""
    if depth >= 5:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _clean_text(value, 500)
    if isinstance(value, Mapping):
        items = list(value.items())[:60]
        return {
            _clean_text(key, 80): _json_safe(item, depth=depth + 1)
            for key, item in items
            if not _is_sensitive_key(key)
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(item, depth=depth + 1) for item in list(value)[:60]]
    return _clean_text(value, 200)


def _compact_results(response: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    compact: List[Dict[str, Any]] = []
    if not response:
        return compact
    rows = response.get("results")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return compact
    for rank, raw in enumerate(list(rows)[:20], start=1):
        if not isinstance(raw, Mapping):
            continue
        try:
            article_id = int(raw.get("id"))
        except (TypeError, ValueError):
            continue
        sources = raw.get("sources")
        if not isinstance(sources, Sequence) or isinstance(sources, (str, bytes, bytearray)):
            sources = []
        compact.append(
            {
                "rank": rank,
                "id": article_id,
                "code": _clean_text(raw.get("code"), 80) or None,
                "title": _clean_text(raw.get("title"), 500) or None,
                "doc_title": _clean_text(raw.get("doc_title"), 500) or None,
                "score": _finite_float(raw.get("score")),
                "sources": [_clean_text(source, 40) for source in list(sources)[:20]],
            }
        )
    return compact


def _related_ids(response: Optional[Mapping[str, Any]]) -> List[int]:
    if not response:
        return []
    related = response.get("related")
    if not isinstance(related, Sequence) or isinstance(related, (str, bytes, bytearray)):
        return []
    result: List[int] = []
    for row in list(related)[:20]:
        if not isinstance(row, Mapping):
            continue
        try:
            result.append(int(row.get("id")))
        except (TypeError, ValueError):
            continue
    return result


def build_search_event(
    *,
    server_id: str,
    query: str,
    requested_limit: int,
    duration_ms: float,
    outcome: str,
    response: Optional[Mapping[str, Any]] = None,
    diagnostics: Optional[Mapping[str, Any]] = None,
    error_code: Optional[str] = None,
    tier: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a privacy-minimised snapshot of what the user actually saw."""
    trace = dict(diagnostics or {})
    query_text = _clean_text(query, 200)
    normalized = _clean_text(trace.get("query_normalized"), 500)
    # Repeated-answer stability must compare the same user question.  Hashing
    # only ``normalized`` would merge role-reversed or otherwise distinct
    # questions that happen to lose the same stop words during normalisation.
    fingerprint_basis = _query_fingerprint_basis(query_text)
    query_hash = hashlib.sha256(fingerprint_basis.encode("utf-8")).hexdigest()
    results = _compact_results(response)
    top_score = results[0]["score"] if results else None
    second_score = results[1]["score"] if len(results) > 1 else None
    score_gap = (
        top_score - second_score
        if top_score is not None and second_score is not None
        else None
    )
    warnings = response.get("warnings", []) if response else []
    if not isinstance(warnings, Sequence) or isinstance(warnings, (str, bytes, bytearray)):
        warnings = []
    search_mode = response.get("search_mode") if response else None
    if search_mode not in _ALLOWED_MODES:
        search_mode = None
    safe_outcome = outcome if outcome in _ALLOWED_OUTCOMES else "error"
    query_features = trace.pop("query_features", {})
    return {
        "event_id": str(uuid.uuid4()),
        "server_id": _clean_text(server_id, 160),
        "query_text": query_text,
        "query_normalized": normalized,
        "query_hash": query_hash,
        "tier": _clean_text(tier, 40) or None,
        "requested_limit": max(1, min(20, int(requested_limit))),
        "outcome": safe_outcome,
        "error_code": _clean_text(error_code, 100) or None,
        "duration_ms": max(0, min(2_147_483_647, int(round(duration_ms)))),
        "algorithm_version": _clean_text(
            trace.pop("algorithm_version", "unknown"), 100,
        ) or "unknown",
        "embedding_space": _clean_text(
            trace.pop("embedding_space", config.LAW_CATALOG_EMBED_SPACE_ID), 100,
        ) or None,
        "intent_kind": _clean_text(trace.pop("intent_kind", None), 100) or None,
        "search_path": _clean_text(trace.pop("search_path", None), 40) or None,
        "search_mode": search_mode,
        "degraded": bool(response.get("degraded", False)) if response else False,
        "warnings": [_clean_text(warning, 100) for warning in list(warnings)[:50]],
        "result_count": len(results),
        "top_article_id": results[0]["id"] if results else None,
        "top_score": top_score,
        "top_score_gap": score_gap,
        "results": results,
        "related_ids": _related_ids(response),
        "query_features": _json_safe(query_features),
        "diagnostics": _json_safe(trace),
    }


def _event_args(event: Mapping[str, Any]) -> tuple:
    return (
        # asyncpg's PostgreSQL UUID codec expects uuid.UUID, not an arbitrary
        # string (the explicit $1::uuid cast does not make text encoding safe).
        uuid.UUID(str(event["event_id"])),
        event["server_id"],
        event["query_text"],
        event["query_normalized"],
        event["query_hash"],
        event.get("tier"),
        event["requested_limit"],
        event["outcome"],
        event.get("error_code"),
        event["duration_ms"],
        event["algorithm_version"],
        event.get("embedding_space"),
        event.get("intent_kind"),
        event.get("search_path"),
        event.get("search_mode"),
        bool(event.get("degraded")),
        list(event.get("warnings") or []),
        event["result_count"],
        event.get("top_article_id"),
        event.get("top_score"),
        event.get("top_score_gap"),
        json.dumps(event.get("results") or [], ensure_ascii=False, separators=(",", ":")),
        list(event.get("related_ids") or []),
        json.dumps(event.get("query_features") or {}, ensure_ascii=False, separators=(",", ":")),
        json.dumps(event.get("diagnostics") or {}, ensure_ascii=False, separators=(",", ":")),
    )


class LawSearchLogWriter:
    """One bounded queue and one batch writer per API process."""

    def __init__(
        self,
        pool: Any,
        *,
        queue_size: int,
        batch_size: int,
        flush_seconds: float,
        db_timeout: float,
        retention_days: int,
    ) -> None:
        self._pool = pool
        self._queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._flush_seconds = flush_seconds
        self._db_timeout = db_timeout
        self._retention_days = retention_days
        self._stopping = False
        self._task: Optional[asyncio.Task[None]] = None
        self._cleanup_task: Optional[asyncio.Task[None]] = None
        self.enqueued = 0
        self.written = 0
        self.dropped = 0
        self.write_failures = 0
        self._last_drop_warning = 0.0
        self._last_write_warning = 0.0

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="law-search-log-writer")
        self._task.add_done_callback(self._on_done)
        self._cleanup_task = asyncio.create_task(
            self._run_cleanup(),
            name="law-search-log-retention",
        )
        self._cleanup_task.add_done_callback(self._on_done)

    def enqueue(self, event: Dict[str, Any]) -> bool:
        if self._stopping:
            self.dropped += 1
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            now = time.monotonic()
            if now - self._last_drop_warning >= 60:
                self._last_drop_warning = now
                logger.warning(
                    "[LAW_SEARCH_LOG] queue full; dropped=%d queued=%d",
                    self.dropped,
                    self._queue.qsize(),
                )
            return False
        self.enqueued += 1
        return True

    def is_active(self) -> bool:
        task = self._task
        return not self._stopping and task is not None and not task.done()

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        if not task.done():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            # During normal operation _on_done logs this.  During shutdown the
            # priority is to retrieve the exception without blocking teardown.
            pass

    async def stop(self, timeout: float) -> None:
        self._stopping = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.05, timeout)

        cleanup_task, self._cleanup_task = self._cleanup_task, None
        lingering: set[asyncio.Task[None]] = set()
        if cleanup_task is not None:
            cleanup_task.cancel()
            cleanup_budget = min(0.5, max(0.0, deadline - loop.time()))
            done, pending = await asyncio.wait(
                {cleanup_task},
                timeout=cleanup_budget,
            )
            for finished in done:
                self._consume_task_result(finished)
            lingering.update(pending)

        task, self._task = self._task, None
        if task is not None:
            remaining_time = max(0.0, deadline - loop.time())
            done, pending = await asyncio.wait({task}, timeout=remaining_time)
            for finished in done:
                self._consume_task_result(finished)
            if pending:
                remaining_events = self._queue.qsize()
                self.dropped += remaining_events
                task.cancel()
                lingering.update(pending)
                logger.warning(
                    "[LAW_SEARCH_LOG] shutdown timeout; dropping %d queued events",
                    remaining_events,
                )

        if lingering:
            remaining_time = max(0.0, deadline - loop.time())
            if remaining_time:
                done, pending = await asyncio.wait(
                    lingering,
                    timeout=remaining_time,
                )
                for finished in done:
                    self._consume_task_result(finished)
                lingering = set(pending)

        if lingering:
            # A DB driver stuck while cancelling must not hang API shutdown.
            # terminate() is safe here: this writer is stopped only while the
            # owning API is tearing down the same pool.
            terminate = getattr(self._pool, "terminate", None)
            if callable(terminate):
                terminate()
            logger.warning(
                "[LAW_SEARCH_LOG] %d telemetry task(s) did not stop before deadline",
                len(lingering),
            )

    def stats(self) -> Dict[str, int]:
        return {
            "queued": self._queue.qsize(),
            "enqueued": self.enqueued,
            "written": self.written,
            "dropped": self.dropped,
            "write_failures": self.write_failures,
        }

    def _on_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._stopping:
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error(
                "[LAW_SEARCH_LOG] writer stopped unexpectedly: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )

    async def _run(self) -> None:
        while not (self._stopping and self._queue.empty()):
            batch = await self._next_batch()
            if batch:
                await self._write_batch(batch)

    async def _run_cleanup(self) -> None:
        """Keep retention work independent from the latency-sensitive writer."""
        while not self._stopping:
            await self._cleanup_retention()
            await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)

    async def _next_batch(self) -> List[Dict[str, Any]]:
        try:
            first = await asyncio.wait_for(
                self._queue.get(), timeout=self._flush_seconds,
            )
        except asyncio.TimeoutError:
            return []
        batch = [first]
        while len(batch) < self._batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _write_batch(self, batch: List[Dict[str, Any]]) -> None:
        try:
            await asyncio.wait_for(
                self._pool.executemany(_INSERT_SQL, [_event_args(event) for event in batch]),
                timeout=self._db_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.write_failures += 1
            self.dropped += len(batch)
            now = time.monotonic()
            if (
                self._last_write_warning == 0.0
                or now - self._last_write_warning >= 60
            ):
                self._last_write_warning = now
                logger.warning(
                    "[LAW_SEARCH_LOG] failed to write events (%s); "
                    "failures=%d dropped=%d; search unaffected",
                    type(exc).__name__,
                    self.write_failures,
                    self.dropped,
                )
            return
        self.written += len(batch)

    async def _cleanup_retention(self) -> None:
        """Delete old rows in bounded batches under a transaction-scoped lock."""
        try:
            # Includes pool acquisition, transaction commit and every delete.
            # A cancelled/failed transaction automatically releases the xact lock.
            total_timeout = max(
                5.0,
                self._db_timeout * (_RETENTION_MAX_BATCHES + 3),
            )

            async def cleanup_once() -> None:
                for _ in range(_RETENTION_MAX_BATCHES):
                    # Release the shared API-pool connection after every small
                    # transaction instead of monopolising it for the full pass.
                    async with self._pool.acquire() as connection:
                        async with connection.transaction():
                            locked = await connection.fetchval(
                                "SELECT pg_try_advisory_xact_lock($1)",
                                _RETENTION_LOCK_KEY,
                            )
                            if not locked:
                                return
                            result = await connection.execute(
                                """
                                WITH doomed AS (
                                    SELECT id
                                    FROM law_search_events
                                    WHERE created_at < NOW()
                                        - ($1::integer * INTERVAL '1 day')
                                    ORDER BY created_at
                                    LIMIT $2
                                )
                                DELETE FROM law_search_events AS event
                                USING doomed
                                WHERE event.id = doomed.id
                                """,
                                self._retention_days,
                                _RETENTION_BATCH_SIZE,
                            )
                    deleted = int(str(result).rsplit(" ", 1)[-1])
                    if deleted < _RETENTION_BATCH_SIZE:
                        break
                    await asyncio.sleep(0)

            await asyncio.wait_for(cleanup_once(), timeout=total_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[LAW_SEARCH_LOG] retention cleanup skipped (%s); search unaffected",
                type(exc).__name__,
            )


_writer: Optional[LawSearchLogWriter] = None


async def start_law_search_logging(pool: Any) -> bool:
    """Start the best-effort writer.  Startup remains successful on any failure."""
    global _writer
    if not config.LAW_SEARCH_LOG_ENABLED or pool is None:
        return False
    if _writer is not None:
        return True
    try:
        writer = LawSearchLogWriter(
            pool,
            queue_size=config.LAW_SEARCH_LOG_QUEUE_SIZE,
            batch_size=config.LAW_SEARCH_LOG_BATCH_SIZE,
            flush_seconds=config.LAW_SEARCH_LOG_FLUSH_SECONDS,
            db_timeout=config.LAW_SEARCH_LOG_DB_TIMEOUT,
            retention_days=config.LAW_SEARCH_LOG_RETENTION_DAYS,
        )
        writer.start()
        _writer = writer
        logger.info(
            "[LAW_SEARCH_LOG] enabled (queue=%d, batch=%d, retention=%dd)",
            config.LAW_SEARCH_LOG_QUEUE_SIZE,
            config.LAW_SEARCH_LOG_BATCH_SIZE,
            config.LAW_SEARCH_LOG_RETENTION_DAYS,
        )
        return True
    except Exception:
        logger.exception("[LAW_SEARCH_LOG] failed to start; search remains available")
        _writer = None
        return False


async def stop_law_search_logging() -> None:
    global _writer
    writer, _writer = _writer, None
    if writer is None:
        return
    await writer.stop(config.LAW_SEARCH_LOG_SHUTDOWN_TIMEOUT)
    logger.info("[LAW_SEARCH_LOG] stopped: %s", writer.stats())


def enqueue_search_event(event: Dict[str, Any]) -> bool:
    """Non-blocking and fail-open even if called before startup or during shutdown."""
    writer = _writer
    if writer is None:
        return False
    try:
        return writer.enqueue(event)
    except Exception:
        logger.exception("[LAW_SEARCH_LOG] enqueue failed; search remains available")
        return False


def is_law_search_logging_active() -> bool:
    """Cheap guard used before collecting diagnostics on the request path."""
    writer = _writer
    return bool(
        config.LAW_SEARCH_LOG_ENABLED
        and writer is not None
        and writer.is_active()
    )


def law_search_log_stats() -> Dict[str, int]:
    writer = _writer
    return writer.stats() if writer is not None else {
        "queued": 0,
        "enqueued": 0,
        "written": 0,
        "dropped": 0,
        "write_failures": 0,
    }
