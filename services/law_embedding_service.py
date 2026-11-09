"""Версионированные локальные embeddings каталога законов и их DB-очередь.

Контейнер бота пересобирает ``law_articles``, но локальная FRIDA смонтирована только
в API-контейнер. Поэтому синхронизация лишь фиксирует ожидаемый input hash и кладёт
задание в PostgreSQL; фоновый worker API считает вектор и пишет его только если
статья за это время не изменилась.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)

EMBEDDING_RECIPE_VERSION = "law-article-v1"
_INPUT_SEPARATOR = "\x1f"


def active_space_id() -> str:
    """Активное совместимое пространство векторов Overlay law search."""
    return str(getattr(config, "LAW_CATALOG_EMBED_SPACE_ID", "frida-v1")).strip() or "frida-v1"


def _value(article: Mapping[str, Any], key: str) -> str:
    value = article.get(key)
    return "" if value is None else str(value)


def embedding_input_components(article: Mapping[str, Any]) -> Tuple[str, str, str]:
    """Ровно те поля, из которых recipe v1 собирает текст для FRIDA."""
    return (
        _value(article, "title"),
        _value(article, "penalty_summary"),
        _value(article, "full_text")[:2000],
    )


def build_embedding_text(article: Mapping[str, Any]) -> str:
    """Единственный рецепт document-текста для нового verified FRIDA-вектора."""
    title, penalty, body = embedding_input_components(article)
    text = ". ".join(part for part in (title, penalty, body) if part).strip()
    return text or title or "статья"


def embedding_input_hash(article: Mapping[str, Any]) -> str:
    """SHA-256 исходных компонентов recipe v1 (без неоднозначной конкатенации)."""
    payload = _INPUT_SEPARATOR.join(embedding_input_components(article)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _to_pgvector(values: Sequence[float]) -> str:
    """Format pgvector text locally, without importing the cloud embedding stack."""
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


async def validate_active_space_passport() -> None:
    """Сверяет фактическую локальную модель/recipe с паспортом PostgreSQL.

    При несовпадении worker запускать нельзя: иначе он записал бы несовместимые
    числа под старым ``space_id`` и тихо испортил весь ANN-корпус.
    """
    from services.law_embedder import get_law_embedder

    embedder = get_law_embedder()
    if embedder is None:
        raise RuntimeError("local FRIDA embedder is not initialized")
    row = await db_manager.fetchrow(
        """
        SELECT model_name, model_revision, dimensions, query_prefix, document_prefix,
               pooling, normalized, recipe_version, metadata
        FROM law_embedding_spaces
        WHERE space_id = $1
        """,
        active_space_id(),
    )
    if not row:
        raise RuntimeError(f"embedding passport not found: {active_space_id()}")
    expected = {
        "model_name": config.LAW_CATALOG_EMBED_MODEL_NAME,
        "model_revision": embedder.model_revision,
        "dimensions": embedder.dims,
        "query_prefix": embedder.query_prefix,
        "document_prefix": embedder.doc_prefix,
        "pooling": embedder.pooling,
        "normalized": embedder.normalized,
        "recipe_version": EMBEDDING_RECIPE_VERSION,
    }
    mismatches = [
        f"{key}: db={row[key]!r}, runtime={value!r}"
        for key, value in expected.items()
        if row[key] != value
    ]
    if mismatches:
        raise RuntimeError(
            f"embedding passport {active_space_id()} mismatch: " + "; ".join(mismatches)
        )

    metadata: Any = row["metadata"] or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"embedding passport {active_space_id()} has invalid metadata JSON"
            ) from exc
    if not isinstance(metadata, Mapping):
        raise RuntimeError(f"embedding passport {active_space_id()} metadata is not an object")

    def _as_revision(value: Any) -> Optional[str]:
        if not value:
            return None
        text = str(value)
        return text if text.startswith("sha256:") else f"sha256:{text}"

    metadata_mismatches: List[str] = []
    if _as_revision(metadata.get("weights_sha256")) != embedder.weights_revision:
        metadata_mismatches.append(
            "weights_sha256: db="
            f"{metadata.get('weights_sha256')!r}, runtime={embedder.weights_revision!r}"
        )
    if _as_revision(metadata.get("artifact_manifest_sha256")) != embedder.model_revision:
        metadata_mismatches.append(
            "artifact_manifest_sha256: db="
            f"{metadata.get('artifact_manifest_sha256')!r}, runtime={embedder.model_revision!r}"
        )
    metadata_versions = metadata.get("runtime_versions")
    if metadata_versions != embedder.runtime_versions:
        metadata_mismatches.append(
            f"runtime_versions: db={metadata_versions!r}, runtime={embedder.runtime_versions!r}"
        )
    configured_versions = dict(getattr(config, "LAW_CATALOG_EMBED_RUNTIME_VERSIONS", {}))
    if metadata_versions != configured_versions:
        metadata_mismatches.append(
            f"runtime_versions: db={metadata_versions!r}, config={configured_versions!r}"
        )
    if metadata_mismatches:
        raise RuntimeError(
            f"embedding passport {active_space_id()} metadata mismatch: "
            + "; ".join(metadata_mismatches)
        )


@asynccontextmanager
async def catalog_server_lock(server_id: str):
    """Межпроцессный session advisory lock на пересборку одного сервера.

    Соединение держится, но транзакция — нет: парсинг/обогащение могут быть долгими,
    и держать на всё это открытую транзакцию было бы опасно.
    """
    key = f"law-catalog:{server_id}"
    async with db_manager.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", key)
        try:
            yield
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)


async def _upsert_job(
    conn,
    *,
    article_id: int,
    server_id: str,
    space_id: str,
    input_hash: str,
    keep_current: bool,
    reset_attempts: bool = False,
) -> None:
    """Upsert a job without stealing an in-flight worker's lease.

    ``keep_current`` marks a harmless forced refresh whose existing vector may
    remain searchable.  An explicit reset revives terminal failures, while an
    already-processing job keeps its current lease.
    """
    await conn.execute(
        """
        INSERT INTO law_embedding_jobs
            (article_id, server_id, space_id, input_hash, status, attempts,
             last_error, available_at, locked_at, keep_current, updated_at)
        VALUES ($1, $2, $3, $4, 'pending', 0, NULL, now(), NULL, $5, now())
        ON CONFLICT ON CONSTRAINT uq_law_embedding_jobs_article_space
        DO UPDATE SET
            server_id = EXCLUDED.server_id,
            input_hash = EXCLUDED.input_hash,
            status = CASE
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                     AND law_embedding_jobs.status = 'processing'
                    THEN law_embedding_jobs.status
                WHEN $6::boolean THEN 'pending'
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                    THEN law_embedding_jobs.status
                ELSE 'pending'
            END,
            attempts = CASE
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                     AND law_embedding_jobs.status = 'processing'
                     AND $6::boolean
                    THEN 0
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                     AND law_embedding_jobs.status = 'processing'
                    THEN law_embedding_jobs.attempts
                WHEN $6::boolean THEN 0
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                    THEN law_embedding_jobs.attempts
                ELSE 0
            END,
            last_error = CASE
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                     AND law_embedding_jobs.status = 'processing'
                     AND $6::boolean
                    THEN NULL
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                     AND law_embedding_jobs.status = 'processing'
                    THEN law_embedding_jobs.last_error
                WHEN $6::boolean THEN NULL
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                    THEN law_embedding_jobs.last_error
                ELSE NULL
            END,
            available_at = CASE
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                     AND law_embedding_jobs.status = 'processing'
                    THEN law_embedding_jobs.available_at
                WHEN $6::boolean THEN now()
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                    THEN law_embedding_jobs.available_at
                ELSE now()
            END,
            locked_at = CASE
                WHEN law_embedding_jobs.input_hash = EXCLUDED.input_hash
                     AND law_embedding_jobs.status = 'processing'
                    THEN law_embedding_jobs.locked_at
                ELSE NULL
            END,
            keep_current = EXCLUDED.keep_current,
            updated_at = now()
        """,
        article_id,
        server_id,
        space_id,
        input_hash,
        bool(keep_current),
        bool(reset_attempts),
    )


async def ensure_article_embedding_target(
    conn,
    article_id: int,
    *,
    force: bool = False,
) -> bool:
    """Сверяет статью с active space и при необходимости ставит job.

    Возвращает ``True``, если задание создано/обновлено. Устаревший активный
    вектор удаляется сразу. При ``force=True`` корректный текущий вектор остаётся
    доступным до атомарной замены новым.
    """
    row = await conn.fetchrow(
        """
        SELECT id, server_id, kind, title, penalty_summary, left(full_text, 2000) AS full_text
        FROM law_articles
        WHERE id = $1
        FOR UPDATE
        """,
        article_id,
    )
    if not row or row["kind"] != "article":
        return False

    space_id = active_space_id()
    current_hash = embedding_input_hash(dict(row))
    existing = await conn.fetchrow(
        """
        SELECT input_hash, server_id
        FROM law_article_embeddings
        WHERE article_id = $1 AND space_id = $2
        FOR UPDATE
        """,
        article_id,
        space_id,
    )

    has_valid_current = bool(
        existing
        and existing["input_hash"] == current_hash
        and existing["server_id"] == row["server_id"]
    )
    if has_valid_current:
        # Каноническая таблица уже корректна. Подтягиваем legacy-зеркало для
        # плавного отката старого кода и убираем лишнее задание, если оно было.
        await conn.execute(
            """
            UPDATE law_articles a
            SET embedding_local = e.embedding,
                embedding_local_space = e.space_id,
                embedding_local_input_hash = e.input_hash,
                embedding_local_updated_at = e.updated_at
            FROM law_article_embeddings e
            WHERE a.id = $1 AND e.article_id = a.id AND e.space_id = $2
            """,
            article_id,
            space_id,
        )
        if not force:
            # keep_current=true означает намеренный forced refresh. Обычная
            # сверка не должна отменять его лишь потому, что старый вектор ещё
            # остаётся корректным и доступным поиску.
            await conn.execute(
                "DELETE FROM law_embedding_jobs "
                "WHERE article_id = $1 AND space_id = $2 AND keep_current = false",
                article_id,
                space_id,
            )
            return False

        await _upsert_job(
            conn,
            article_id=article_id,
            server_id=row["server_id"],
            space_id=space_id,
            input_hash=current_hash,
            keep_current=True,
            reset_attempts=True,
        )
        return True

    # Missing/stale vector cannot remain searchable while inference is pending.
    await conn.execute(
        "DELETE FROM law_article_embeddings WHERE article_id = $1 AND space_id = $2",
        article_id,
        space_id,
    )
    if existing:
        # The old related graph was built from the vector just invalidated.
        # Keep a durable marker; processing waits while the replacement job is
        # retryable and proceeds if that job eventually becomes terminal.
        await mark_related_rebuild(conn, row["server_id"], space_id)
    await conn.execute(
        """
        UPDATE law_articles
        SET embedding_local = NULL,
            embedding_local_space = $2,
            embedding_local_input_hash = $3,
            embedding_local_updated_at = NULL,
            updated_at = now()
        WHERE id = $1
        """,
        article_id,
        space_id,
        current_hash,
    )
    await _upsert_job(
        conn,
        article_id=article_id,
        server_id=row["server_id"],
        space_id=space_id,
        input_hash=current_hash,
        keep_current=False,
        reset_attempts=force,
    )
    return True


async def reconcile_embedding_jobs(
    server_id: Optional[str] = None,
    *,
    force: bool = False,
) -> int:
    """Находит отсутствующие/устаревшие active-space векторы и ставит jobs.

    Полный проход делается worker-ом при старте API. В нормальной работе точечные
    jobs уже создаёт catalog sync, а этот проход служит страховкой и backfill-ом.
    """
    space_id = active_space_id()
    params: List[Any] = [space_id]
    server_filter = ""
    if server_id is not None:
        params.append(server_id)
        server_filter = "AND a.server_id = $2"
    rows = await db_manager.fetch(
        f"""
        SELECT a.id, a.server_id, a.kind, a.title, a.penalty_summary,
               left(a.full_text, 2000) AS full_text,
               a.embedding_local_space, a.embedding_local_input_hash,
               e.input_hash AS stored_input_hash,
               e.server_id AS stored_server_id
        FROM law_articles a
        LEFT JOIN law_article_embeddings e
          ON e.article_id = a.id AND e.space_id = $1
        WHERE a.kind = 'article' {server_filter}
        ORDER BY a.id
        """,
        *params,
    )

    candidates_by_server: Dict[str, List[int]] = {}
    for row in rows:
        expected = embedding_input_hash(dict(row))
        if (
            force
            or row["stored_input_hash"] != expected
            or (
                row["stored_input_hash"] is not None
                and row["stored_server_id"] != row["server_id"]
            )
            or row["embedding_local_space"] != space_id
            or row["embedding_local_input_hash"] != expected
        ):
            candidates_by_server.setdefault(row["server_id"], []).append(row["id"])

    queued = 0
    batch_size = 250
    for candidate_server in sorted(candidates_by_server):
        candidates = candidates_by_server[candidate_server]
        for offset in range(0, len(candidates), batch_size):
            batch = sorted(candidates[offset:offset + batch_size])
            # Catalog/delete/related writers use the same server lock. Pre-lock
            # every article before ensure() can touch the shared related marker,
            # so this transaction never acquires marker -> next-article.
            async with catalog_server_lock(candidate_server):
                async with db_manager.transaction() as conn:
                    await conn.fetch(
                        "SELECT id FROM law_articles "
                        "WHERE id = ANY($1::int[]) ORDER BY id FOR UPDATE",
                        batch,
                    )
                    for article_id in batch:
                        if await ensure_article_embedding_target(conn, article_id, force=force):
                            queued += 1
    if queued:
        logger.info(
            "[LAW_EMBED_JOBS] поставлено заданий=%d server=%s space=%s force=%s",
            queued,
            server_id or "*",
            space_id,
            force,
        )
    return queued


def _affected_rows(status: Any) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def recover_stale_jobs(
    stale_seconds: int = 600,
    *,
    max_attempts: Optional[int] = None,
) -> int:
    """Recover expired leases without creating an unclaimable pending job.

    Attempts are charged when a job is claimed.  If the worker disappears on
    its final allowed attempt, the old implementation changed it back to
    ``pending`` while leaving ``attempts == max_attempts``.  Such a row could
    neither be claimed nor recognised as terminal.  Exhausted leases are now
    made explicitly terminal; graceful shutdown uses the lease-release helper
    below and compensates the interrupted attempt instead.
    """
    attempts_limit = int(
        max_attempts
        if max_attempts is not None
        else getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5)
    )
    status = await db_manager.execute(
        """
        UPDATE law_embedding_jobs
        SET status = CASE WHEN attempts >= $2 THEN 'failed' ELSE 'pending' END,
            locked_at = NULL, available_at = now(), updated_at = now(),
            last_error = COALESCE(last_error, 'worker lease expired')
        WHERE status = 'processing'
          AND (locked_at IS NULL
               OR locked_at < now() - ($1::int * interval '1 second'))
        """,
        int(stale_seconds),
        attempts_limit,
    )
    return _affected_rows(status)


async def _release_embedding_leases(
    jobs: Iterable[Mapping[str, Any]],
    *,
    reason: str = "worker cancelled",
) -> int:
    """Immediately return only leases still owned by this worker.

    A graceful cancellation is not an inference failure, so the attempt that
    was charged by ``_claim_jobs`` is compensated.  The full lease identity
    (id, input hash and locked_at) prevents an old worker from releasing a job
    that has already been recovered and claimed by somebody else.
    """
    released = 0
    async with db_manager.transaction() as conn:
        for job in jobs:
            status = await conn.execute(
                """
                UPDATE law_embedding_jobs
                SET status = 'pending', attempts = greatest(attempts - 1, 0),
                    last_error = $4, available_at = now(), locked_at = NULL,
                    updated_at = now()
                WHERE id = $1 AND input_hash = $2
                  AND status = 'processing' AND locked_at = $3
                """,
                job["id"],
                job["input_hash"],
                job["locked_at"],
                reason[:2000],
            )
            released += _affected_rows(status)
    return released


async def mark_related_rebuild(conn, server_id: str, space_id: str) -> None:
    """Durably mark a server/space related graph dirty.

    The upsert deliberately changes even a currently-processing marker back to
    ``pending``.  Its worker will then fail the lease-guarded delete after the
    old rebuild, guaranteeing that a vector committed during that rebuild
    triggers one more pass.
    """
    await conn.execute(
        """
        INSERT INTO law_related_rebuild_jobs
            (server_id, space_id, status, attempts, last_error,
             available_at, locked_at, updated_at)
        VALUES ($1, $2, 'pending', 0, NULL, now(), NULL, now())
        ON CONFLICT (server_id, space_id)
        DO UPDATE SET status = 'pending', attempts = 0, last_error = NULL,
                      available_at = now(), locked_at = NULL, updated_at = now()
        """,
        server_id,
        space_id,
    )


async def recover_stale_related_rebuilds(
    stale_seconds: int = 600,
    *,
    max_attempts: Optional[int] = None,
) -> int:
    attempts_limit = int(
        max_attempts
        if max_attempts is not None
        else getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5)
    )
    status = await db_manager.execute(
        """
        UPDATE law_related_rebuild_jobs
        SET status = CASE WHEN attempts >= $2 THEN 'failed' ELSE 'pending' END,
            locked_at = NULL, available_at = now(), updated_at = now(),
            last_error = COALESCE(last_error, 'worker lease expired')
        WHERE status = 'processing'
          AND (locked_at IS NULL
               OR locked_at < now() - ($1::int * interval '1 second'))
        """,
        int(stale_seconds),
        attempts_limit,
    )
    return _affected_rows(status)


async def _release_related_rebuild_leases(
    jobs: Iterable[Mapping[str, Any]],
    *,
    reason: str = "worker cancelled",
) -> int:
    """Return related rebuild claims owned by this worker immediately."""
    released = 0
    async with db_manager.transaction() as conn:
        for job in jobs:
            status = await conn.execute(
                """
                UPDATE law_related_rebuild_jobs
                SET status = 'pending', attempts = greatest(attempts - 1, 0),
                    last_error = $4, available_at = now(), locked_at = NULL,
                    updated_at = now()
                WHERE server_id = $1 AND space_id = $2
                  AND status = 'processing' AND locked_at = $3
                """,
                job["server_id"],
                job["space_id"],
                job["locked_at"],
                reason[:2000],
            )
            released += _affected_rows(status)
    return released


async def retry_terminal_jobs(
    server_id: str,
    *,
    space_id: Optional[str] = None,
    include_related: bool = True,
    max_attempts: Optional[int] = None,
) -> Dict[str, int]:
    """Targeted recovery for a server without refreshing valid vectors.

    This is intentionally server-scoped and is used by the explicit CLI
    backfill path.  Valid canonical vectors are left untouched and any
    exhausted refresh/job next to such a vector is merely removed.  Only
    missing/stale non-keep-current targets are reset for inference.
    """
    target_space = space_id or active_space_id()
    attempts_limit = int(
        max_attempts
        if max_attempts is not None
        else getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5)
    )
    async with db_manager.transaction() as conn:
        cleaned_status = await conn.execute(
            """
            DELETE FROM law_embedding_jobs j
            USING law_articles a, law_article_embeddings e
            WHERE j.article_id = a.id
              AND e.article_id = a.id AND e.space_id = j.space_id
              AND j.server_id = $1 AND j.space_id = $2
              AND j.status IN ('pending', 'failed') AND j.attempts >= $3
              AND a.kind = 'article' AND a.server_id = j.server_id
              AND e.server_id = a.server_id AND e.input_hash = j.input_hash
              AND a.embedding_local_space = j.space_id
              AND a.embedding_local_input_hash = j.input_hash
            """,
            server_id,
            target_space,
            attempts_limit,
        )
        reset_status = await conn.execute(
            """
            UPDATE law_embedding_jobs j
            SET status = 'pending', attempts = 0, last_error = NULL,
                available_at = now(), locked_at = NULL, updated_at = now()
            WHERE j.server_id = $1 AND j.space_id = $2
              AND j.status IN ('pending', 'failed') AND j.attempts >= $3
              AND NOT j.keep_current
              AND EXISTS (
                  SELECT 1 FROM law_articles a
                  WHERE a.id = j.article_id AND a.kind = 'article'
              )
            """,
            server_id,
            target_space,
            attempts_limit,
        )
        related_status: Any = "UPDATE 0"
        if include_related:
            related_status = await conn.execute(
                """
                UPDATE law_related_rebuild_jobs
                SET status = 'pending', attempts = 0, last_error = NULL,
                    available_at = now(), locked_at = NULL, updated_at = now()
                WHERE server_id = $1 AND space_id = $2
                  AND status IN ('pending', 'failed') AND attempts >= $3
                """,
                server_id,
                target_space,
                attempts_limit,
            )
    result = {
        "cleaned_valid": _affected_rows(cleaned_status),
        "embedding": _affected_rows(reset_status),
        "related": _affected_rows(related_status),
    }
    if any(result.values()):
        logger.warning(
            "[LAW_EMBED_JOBS] targeted retry server=%s space=%s: %s",
            server_id,
            target_space,
            result,
        )
    return result


async def _claim_related_rebuild_jobs(
    limit: int,
    *,
    server_id: Optional[str] = None,
    max_attempts: int = 5,
) -> List[Dict[str, Any]]:
    args: List[Any] = [int(limit), int(max_attempts)]
    server_filter = ""
    if server_id is not None:
        args.append(server_id)
        server_filter = "AND r.server_id = $3"
    async with db_manager.transaction() as conn:
        rows = await conn.fetch(
            f"""
            WITH picked AS (
                SELECT r.server_id, r.space_id
                FROM law_related_rebuild_jobs r
                WHERE r.status IN ('pending', 'failed')
                  AND r.available_at <= now()
                  AND r.attempts < $2
                  {server_filter}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM law_embedding_jobs e
                      WHERE e.server_id = r.server_id AND e.space_id = r.space_id
                        AND (e.status <> 'failed' OR e.attempts < $2)
                  )
                ORDER BY r.available_at, r.server_id, r.space_id
                FOR UPDATE SKIP LOCKED
                LIMIT $1
            )
            UPDATE law_related_rebuild_jobs r
            SET status = 'processing', attempts = r.attempts + 1,
                locked_at = now(), updated_at = now()
            FROM picked
            WHERE r.server_id = picked.server_id AND r.space_id = picked.space_id
            RETURNING r.server_id, r.space_id, r.attempts, r.locked_at
            """,
            *args,
        )
    return [dict(row) for row in rows]


async def _release_related_rebuild_job(job: Mapping[str, Any]) -> None:
    """Release a claim when new embedding work appeared before rebuild began."""
    await db_manager.execute(
        """
        UPDATE law_related_rebuild_jobs
        SET status = 'pending', attempts = greatest(attempts - 1, 0),
            available_at = now(), locked_at = NULL, updated_at = now()
        WHERE server_id = $1 AND space_id = $2
          AND status = 'processing' AND locked_at = $3
        """,
        job["server_id"],
        job["space_id"],
        job["locked_at"],
    )


async def _fail_related_rebuild_job(job: Mapping[str, Any], error: BaseException) -> None:
    delay = min(300, 2 ** max(1, int(job["attempts"])))
    message = f"{type(error).__name__}: {error}"[:2000]
    await db_manager.execute(
        """
        UPDATE law_related_rebuild_jobs
        SET status = 'failed', last_error = $4,
            available_at = now() + ($5::int * interval '1 second'),
            locked_at = NULL, updated_at = now()
        WHERE server_id = $1 AND space_id = $2
          AND status = 'processing' AND locked_at = $3
        """,
        job["server_id"],
        job["space_id"],
        job["locked_at"],
        message,
        delay,
    )


async def _rebuild_related_matrix(server_id: str, space_id: str) -> int:
    """Rebuild related rows without importing catalog/cloud embedding modules."""
    top_n = int(config.LAW_RELATED_TOP_N)
    min_score = float(config.LAW_RELATED_MIN_SCORE)
    timeout_ms = max(1, int(float(config.LAW_RELATED_REBUILD_TIMEOUT) * 1000))
    async with db_manager.transaction() as conn:
        await conn.execute(
            "SELECT set_config('statement_timeout', $1, true)", f"{timeout_ms}ms"
        )
        await conn.execute(
            "SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"
        )
        await conn.execute(
            "DELETE FROM law_article_related USING law_articles a "
            "WHERE law_article_related.article_id = a.id AND a.server_id = $1 "
            "AND law_article_related.vector_space = $2",
            server_id,
            space_id,
        )
        status = await conn.execute(
            """
            INSERT INTO law_article_related (article_id, related_id, vector_space, score)
            SELECT a.id, related.id, $2, related.score
            FROM law_articles a
            JOIN law_article_embeddings ae
              ON ae.article_id = a.id AND ae.space_id = $2
            CROSS JOIN LATERAL (
                SELECT b.id, (1 - (be.embedding <=> ae.embedding))::real AS score
                FROM law_article_embeddings be
                JOIN law_articles b ON b.id = be.article_id
                WHERE be.space_id = $2
                  AND b.server_id = a.server_id AND b.id <> a.id
                  AND b.kind = 'article'
                ORDER BY be.embedding <=> ae.embedding
                LIMIT $3
            ) AS related
            WHERE a.server_id = $1 AND a.kind = 'article' AND related.score >= $4
            ON CONFLICT (article_id, vector_space, related_id) DO NOTHING
            """,
            server_id,
            space_id,
            top_n,
            min_score,
        )
    try:
        inserted = int(str(status).split()[-1])
    except (ValueError, IndexError):
        inserted = 0
    logger.info(
        "[LAW_EMBED_JOBS] related rebuilt server=%s space=%s rows=%d",
        server_id,
        space_id,
        inserted,
    )
    return inserted


async def process_related_rebuild_batch(
    *,
    limit: int = 4,
    server_id: Optional[str] = None,
) -> int:
    """Process due related-graph markers with retry and lease protection."""
    max_attempts = int(getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5))
    jobs = await _claim_related_rebuild_jobs(
        max(1, int(limit)), server_id=server_id, max_attempts=max_attempts,
    )
    if not jobs:
        return 0

    try:
        return await _process_claimed_related_rebuilds(jobs, max_attempts=max_attempts)
    except asyncio.CancelledError:
        # Shield the cleanup from the cancellation currently unwinding this
        # task. Lease guards make this safe even if a newer worker reclaimed a
        # row between cancellation and cleanup.
        try:
            await asyncio.shield(_release_related_rebuild_leases(jobs))
        except Exception:
            # A DB outage may prevent immediate release; stale recovery is the
            # fallback. Never turn graceful task cancellation into a different
            # exception just because cleanup could not reach PostgreSQL.
            logger.exception("[LAW_EMBED_JOBS] failed to release cancelled related leases")
        raise


async def _process_claimed_related_rebuilds(
    jobs: Sequence[Mapping[str, Any]],
    *,
    max_attempts: int,
) -> int:
    """Process an already claimed related batch (split out for lease cleanup)."""

    succeeded = 0
    for job in jobs:
        try:
            # Catalog DELETE+INSERT and graph rebuild for one server must not
            # overlap.  Re-check embedding work after acquiring that lock: it
            # may have appeared after the marker was claimed.
            async with catalog_server_lock(job["server_id"]):
                has_embedding_work = await db_manager.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM law_embedding_jobs
                        WHERE server_id = $1 AND space_id = $2
                          AND (status <> 'failed' OR attempts < $3)
                    )
                    """,
                    job["server_id"],
                    job["space_id"],
                    max_attempts,
                )
                if has_embedding_work:
                    await _release_related_rebuild_job(job)
                    continue
                await _rebuild_related_matrix(job["server_id"], job["space_id"])
            deleted = await db_manager.execute(
                """
                DELETE FROM law_related_rebuild_jobs
                WHERE server_id = $1 AND space_id = $2
                  AND status = 'processing' AND locked_at = $3
                """,
                job["server_id"],
                job["space_id"],
                job["locked_at"],
            )
            if str(deleted).endswith("1"):
                succeeded += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await _fail_related_rebuild_job(job, exc)
            logger.exception(
                "[LAW_EMBED_JOBS] related rebuild failed server=%s space=%s",
                job["server_id"],
                job["space_id"],
            )
    return succeeded


async def _claim_jobs(
    limit: int,
    *,
    server_id: Optional[str] = None,
    max_attempts: int = 5,
) -> List[Dict[str, Any]]:
    args: List[Any] = [int(limit), int(max_attempts), active_space_id()]
    server_filter = ""
    if server_id is not None:
        args.append(server_id)
        server_filter = "AND j.server_id = $4"
    async with db_manager.transaction() as conn:
        rows = await conn.fetch(
            f"""
            WITH picked AS (
                SELECT j.id
                FROM law_embedding_jobs j
                WHERE j.space_id = $3
                  AND j.status IN ('pending', 'failed')
                  AND j.available_at <= now()
                  AND j.attempts < $2
                  {server_filter}
                ORDER BY j.available_at, j.id
                FOR UPDATE SKIP LOCKED
                LIMIT $1
            )
            UPDATE law_embedding_jobs j
            SET status = 'processing', attempts = j.attempts + 1,
                locked_at = now(), updated_at = now()
            FROM picked
            WHERE j.id = picked.id
            RETURNING j.id, j.article_id, j.server_id, j.space_id,
                      j.input_hash, j.attempts, j.keep_current, j.locked_at
            """,
            *args,
        )
    return [dict(row) for row in rows]


async def _fail_jobs(jobs: Iterable[Dict[str, Any]], error: BaseException) -> None:
    message = f"{type(error).__name__}: {error}"[:2000]
    async with db_manager.transaction() as conn:
        for job in jobs:
            # Небольшой exponential backoff; после max_attempts job остаётся failed
            # и виден readiness/оператору, но не крутится бесконечно.
            delay = min(300, 2 ** max(1, int(job["attempts"])))
            await conn.execute(
                """
                UPDATE law_embedding_jobs
                SET status = 'failed', last_error = $3,
                    available_at = now() + ($4::int * interval '1 second'),
                    locked_at = NULL, updated_at = now()
                WHERE id = $1 AND input_hash = $2 AND status = 'processing'
                  AND locked_at = $5
                """,
                job["id"],
                job["input_hash"],
                message,
                delay,
                job["locked_at"],
            )


async def process_embedding_job_batch(
    *,
    limit: Optional[int] = None,
    server_id: Optional[str] = None,
) -> Tuple[int, Set[str]]:
    """Claim → FRIDA batch → conditional write. Возвращает (успехи, серверы)."""
    batch_limit = int(limit or getattr(config, "LAW_EMBED_JOB_BATCH_SIZE", 16))
    max_attempts = int(getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5))
    jobs = await _claim_jobs(batch_limit, server_id=server_id, max_attempts=max_attempts)
    if not jobs:
        return 0, set()

    from services.law_embedder import LawEmbeddingInferenceTimeout

    try:
        return await _process_claimed_embedding_jobs(jobs)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(_release_embedding_leases(jobs))
        except Exception:
            logger.exception("[LAW_EMBED_JOBS] failed to release cancelled embedding leases")
        raise
    except LawEmbeddingInferenceTimeout as exc:
        # A watchdog timeout means native inference really consumed this
        # attempt and the process is about to hard-exit.  Unlike a graceful
        # SIGTERM/cancellation, do not compensate the claim attempt: record the
        # ordinary retry/terminal failure under the exact lease we still own.
        try:
            await asyncio.shield(_fail_jobs(jobs, exc))
        except Exception:
            # If PostgreSQL itself is unavailable, the row remains processing;
            # stale recovery will turn an exhausted lease into explicit failed
            # instead of the old unclaimable pending/max state.
            logger.exception("[LAW_EMBED_JOBS] failed to record timed-out inference jobs")
        raise


async def _process_claimed_embedding_jobs(
    jobs: Sequence[Dict[str, Any]],
) -> Tuple[int, Set[str]]:
    """Process jobs whose leases are already owned by the current worker."""

    article_ids = [job["article_id"] for job in jobs]
    claimed_servers = {job["server_id"] for job in jobs}
    try:
        rows = await db_manager.fetch(
            """
            SELECT id, server_id, kind, title, penalty_summary, left(full_text, 2000) AS full_text,
                   embedding_local_space, embedding_local_input_hash
            FROM law_articles
            WHERE id = ANY($1::int[])
            """,
            article_ids,
        )
    except Exception as exc:
        await _fail_jobs(jobs, exc)
        logger.exception("[LAW_EMBED_JOBS] fetch claimed articles failed")
        return 0, claimed_servers
    by_id = {row["id"]: dict(row) for row in rows}
    valid: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    stale: List[Dict[str, Any]] = []
    for job in jobs:
        article = by_id.get(job["article_id"])
        if not article:
            stale.append(job)
            continue
        current_hash = embedding_input_hash(article)
        if (
            article["kind"] != "article"
            or article["server_id"] != job["server_id"]
            or job["space_id"] != active_space_id()
            or article["embedding_local_space"] != job["space_id"]
            or article["embedding_local_input_hash"] != job["input_hash"]
            or current_hash != job["input_hash"]
        ):
            stale.append(job)
            continue
        valid.append((job, article, build_embedding_text(article)))

    if stale:
        stale_by_server: Dict[str, List[Dict[str, Any]]] = {}
        for job in stale:
            stale_by_server.setdefault(job["server_id"], []).append(job)
        for stale_server in sorted(stale_by_server):
            server_jobs = sorted(
                stale_by_server[stale_server], key=lambda item: item["article_id"]
            )
            try:
                async with catalog_server_lock(stale_server):
                    async with db_manager.transaction() as conn:
                        # Lock all surviving articles before the first ensure()
                        # can lock the shared related marker.
                        await conn.fetch(
                            "SELECT id FROM law_articles "
                            "WHERE id = ANY($1::int[]) ORDER BY id FOR UPDATE",
                            [job["article_id"] for job in server_jobs],
                        )
                        for job in server_jobs:
                            fresh = await conn.fetchrow(
                                "SELECT id FROM law_articles WHERE id = $1 FOR UPDATE",
                                job["article_id"],
                            )
                            deleted = await conn.execute(
                                """
                                DELETE FROM law_embedding_jobs
                                WHERE id = $1 AND input_hash = $2
                                  AND status = 'processing' AND locked_at = $3
                                """,
                                job["id"],
                                job["input_hash"],
                                job["locked_at"],
                            )
                            if not str(deleted).endswith("1"):
                                continue
                            if fresh:
                                await ensure_article_embedding_target(conn, job["article_id"])
            except Exception as exc:
                await _fail_jobs(server_jobs, exc)
                logger.exception(
                    "[LAW_EMBED_JOBS] stale job reconciliation failed server=%s",
                    stale_server,
                )

    if not valid:
        return 0, set()

    from services.law_embedder import get_law_embedder, LawEmbeddingInferenceTimeout

    embedder = get_law_embedder()
    if embedder is None:
        error = RuntimeError("local FRIDA embedder is not initialized")
        await _fail_jobs((item[0] for item in valid), error)
        return 0, {item[0]["server_id"] for item in valid}

    try:
        vectors = await embedder.embed_documents([item[2] for item in valid])
        if len(vectors) != len(valid):
            raise RuntimeError(f"FRIDA returned {len(vectors)} vectors for {len(valid)} articles")
    except LawEmbeddingInferenceTimeout:
        raise
    except Exception as exc:
        await _fail_jobs((item[0] for item in valid), exc)
        logger.exception("[LAW_EMBED_JOBS] batch FRIDA failed")
        return 0, {item[0]["server_id"] for item in valid}

    succeeded = 0
    touched_servers: Set[str] = set()
    writes_by_server: Dict[str, List[Tuple[Dict[str, Any], Sequence[float]]]] = {}
    for (job, _article, _text), vector in zip(valid, vectors):
        writes_by_server.setdefault(job["server_id"], []).append((job, vector))

    for write_server in sorted(writes_by_server):
        server_writes = writes_by_server[write_server]
        try:
            # Catalog/delete/related writers share this lock. Inference stays
            # outside it; only the short conditional commits are serialized.
            async with catalog_server_lock(write_server):
                for job, vector in server_writes:
                    write_succeeded = False
                    try:
                        # One transaction per result: after locking a related
                        # marker we never go on to lock another article.
                        async with db_manager.transaction() as conn:
                            fresh = await conn.fetchrow(
                                """
                                SELECT id, server_id, kind, title, penalty_summary,
                                       left(full_text, 2000) AS full_text,
                                       embedding_local_space, embedding_local_input_hash
                                FROM law_articles
                                WHERE id = $1
                                FOR UPDATE
                                """,
                                job["article_id"],
                            )
                            lease_owned = await conn.fetchval(
                                """
                                SELECT id
                                FROM law_embedding_jobs
                                WHERE id = $1 AND input_hash = $2
                                  AND status = 'processing' AND locked_at = $3
                                FOR UPDATE
                                """,
                                job["id"],
                                job["input_hash"],
                                job["locked_at"],
                            )
                            if not lease_owned:
                                # Lease recovery/reclaim happened during FRIDA.
                                continue
                            if (
                                not fresh
                                or fresh["kind"] != "article"
                                or fresh["server_id"] != job["server_id"]
                                or fresh["embedding_local_space"] != job["space_id"]
                                or fresh["embedding_local_input_hash"] != job["input_hash"]
                                or embedding_input_hash(dict(fresh)) != job["input_hash"]
                            ):
                                await conn.execute(
                                    """
                                    DELETE FROM law_embedding_jobs
                                    WHERE id = $1 AND input_hash = $2
                                      AND status = 'processing' AND locked_at = $3
                                    """,
                                    job["id"], job["input_hash"], job["locked_at"],
                                )
                                if fresh:
                                    await ensure_article_embedding_target(conn, job["article_id"])
                                continue

                            vector_text = _to_pgvector(vector)
                            updated_id = await conn.fetchval(
                                f"""
                                UPDATE law_articles
                                SET embedding_local = $4::{config.LAW_CATALOG_EMBED_PG_TYPE},
                                    embedding_local_updated_at = now(), updated_at = now()
                                WHERE id = $1
                                  AND embedding_local_space = $2
                                  AND embedding_local_input_hash = $3
                                RETURNING id
                                """,
                                job["article_id"], job["space_id"],
                                job["input_hash"], vector_text,
                            )
                            if not updated_id:
                                await conn.execute(
                                    """
                                    DELETE FROM law_embedding_jobs
                                    WHERE id = $1 AND input_hash = $2
                                      AND status = 'processing' AND locked_at = $3
                                    """,
                                    job["id"], job["input_hash"], job["locked_at"],
                                )
                                await ensure_article_embedding_target(conn, job["article_id"])
                                continue
                            await conn.execute(
                                """
                                INSERT INTO law_article_embeddings
                                    (article_id, server_id, space_id, input_hash, embedding, updated_at)
                                VALUES ($1, $2, $3, $4, $5::vector, now())
                                ON CONFLICT (article_id, space_id)
                                DO UPDATE SET server_id = EXCLUDED.server_id,
                                              input_hash = EXCLUDED.input_hash,
                                              embedding = EXCLUDED.embedding,
                                              updated_at = now()
                                """,
                                job["article_id"], job["server_id"], job["space_id"],
                                job["input_hash"], vector_text,
                            )
                            # Vector + dirty intent + lease deletion commit atomically.
                            await mark_related_rebuild(
                                conn, job["server_id"], job["space_id"]
                            )
                            deleted = await conn.execute(
                                """
                                DELETE FROM law_embedding_jobs
                                WHERE id = $1 AND input_hash = $2
                                  AND status = 'processing' AND locked_at = $3
                                """,
                                job["id"], job["input_hash"], job["locked_at"],
                            )
                            write_succeeded = str(deleted).endswith("1")
                        if write_succeeded:
                            succeeded += 1
                            touched_servers.add(write_server)
                    except Exception as exc:
                        await _fail_jobs([job], exc)
                        touched_servers.add(write_server)
                        logger.exception(
                            "[LAW_EMBED_JOBS] conditional vector write failed job=%s",
                            job["id"],
                        )
        except Exception as exc:
            await _fail_jobs((job for job, _vector in server_writes), exc)
            touched_servers.add(write_server)
            logger.exception(
                "[LAW_EMBED_JOBS] server write lock failed server=%s", write_server
            )

    return succeeded, touched_servers


async def _rebuild_drained_servers(server_ids: Sequence[str]) -> None:
    if not server_ids:
        return
    for server_id in sorted(set(server_ids)):
        await process_related_rebuild_batch(limit=1, server_id=server_id)


async def _select_next_embedding_server(
    *,
    max_attempts: Optional[int] = None,
) -> Optional[str]:
    """Pick the closest-to-complete server that has claimable work now.

    Keeping each background batch on one server lets that server reach a
    complete corpus and enable vector search instead of leaving every catalog
    partially populated.  The subsequent claim still uses ``SKIP LOCKED``, so
    this scheduling hint remains safe with multiple workers.
    """
    attempts_limit = int(
        max_attempts
        if max_attempts is not None
        else getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5)
    )
    server_id = await db_manager.fetchval(
        """
        WITH remaining AS (
            SELECT server_id, count(*) AS jobs_left
            FROM law_embedding_jobs
            WHERE space_id = $1
              AND (status <> 'failed' OR attempts < $2)
            GROUP BY server_id
        ), available AS (
            SELECT server_id, min(available_at) AS first_available_at
            FROM law_embedding_jobs
            WHERE space_id = $1
              AND status IN ('pending', 'failed')
              AND attempts < $2 AND available_at <= now()
            GROUP BY server_id
        )
        SELECT available.server_id
        FROM available
        JOIN remaining USING (server_id)
        ORDER BY remaining.jobs_left ASC,
                 available.first_available_at ASC,
                 available.server_id ASC
        LIMIT 1
        """,
        active_space_id(),
        attempts_limit,
    )
    return str(server_id) if server_id is not None else None


async def _process_background_worker_iteration() -> Tuple[int, int]:
    """Run one server-focused embedding batch and one related batch."""
    target_server = await _select_next_embedding_server()
    if target_server is None:
        count, servers = 0, set()
    else:
        count, servers = await process_embedding_job_batch(server_id=target_server)
    if servers:
        await _rebuild_drained_servers(list(servers))
    related_count = await process_related_rebuild_batch()
    return count, related_count


async def _background_iteration_until_stopped(
    stop_event: asyncio.Event,
) -> Optional[Tuple[int, int]]:
    """Cancel an in-flight batch as soon as graceful shutdown is requested."""
    iteration = asyncio.create_task(_process_background_worker_iteration())
    stop_waiter = asyncio.create_task(stop_event.wait())
    try:
        done, _pending = await asyncio.wait(
            {iteration, stop_waiter}, return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_waiter in done:
            iteration.cancel()
            try:
                await iteration
            except asyncio.CancelledError:
                pass
            return None
        return await iteration
    except asyncio.CancelledError:
        iteration.cancel()
        try:
            await asyncio.shield(iteration)
        except asyncio.CancelledError:
            pass
        raise
    finally:
        if not stop_waiter.done():
            stop_waiter.cancel()
            try:
                await stop_waiter
            except asyncio.CancelledError:
                pass


async def _wait_for_valid_passport(stop_event: asyncio.Event, poll_seconds: float) -> bool:
    """Retry transient DB failures but treat a real passport mismatch as fatal."""
    delay = max(1.0, poll_seconds)
    while not stop_event.is_set():
        try:
            await validate_active_space_passport()
            return True
        except asyncio.CancelledError:
            raise
        except RuntimeError:
            # Model/passport incompatibility must never be hidden by retries.
            raise
        except Exception:
            logger.exception("[LAW_EMBED_JOBS] passport check temporarily failed; retrying")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            delay = min(30.0, delay * 2)
    return False


async def run_embedding_worker(stop_event: asyncio.Event) -> None:
    """Фоновый consumer для lifespan API. Один worker безопасен и при нескольких API."""
    poll_seconds = float(getattr(config, "LAW_EMBED_JOB_POLL_SECONDS", 2.0))
    stale_seconds = int(getattr(config, "LAW_EMBED_JOB_STALE_SECONDS", 600))
    reconcile_seconds = float(getattr(config, "LAW_EMBED_RECONCILE_SECONDS", 300.0))
    loop = asyncio.get_running_loop()
    next_recovery_at = loop.time() + min(60.0, max(5.0, stale_seconds / 2))
    next_reconcile_at = loop.time() + max(10.0, reconcile_seconds)
    # Несовместимый passport — жёсткий стоп consumer-а, а не обычная временная
    # ошибка reconciliation: писать такие векторы под active space запрещено.
    if not await _wait_for_valid_passport(stop_event, poll_seconds):
        return
    try:
        await recover_stale_jobs(stale_seconds)
        await recover_stale_related_rebuilds(stale_seconds)
        await reconcile_embedding_jobs()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("[LAW_EMBED_JOBS] startup reconciliation failed")
        # Do not wait a full periodic interval after a transient startup error.
        next_reconcile_at = loop.time()

    from services.law_embedder import LawEmbeddingInferenceTimeout

    while not stop_event.is_set():
        try:
            if loop.time() >= next_recovery_at:
                recovered = await recover_stale_jobs(stale_seconds)
                related_recovered = await recover_stale_related_rebuilds(stale_seconds)
                if recovered or related_recovered:
                    logger.warning(
                        "[LAW_EMBED_JOBS] возвращено stale jobs=%d related=%d",
                        recovered,
                        related_recovered,
                    )
                next_recovery_at = loop.time() + min(60.0, max(5.0, stale_seconds / 2))
            if loop.time() >= next_reconcile_at:
                queued = await reconcile_embedding_jobs()
                if queued:
                    logger.info("[LAW_EMBED_JOBS] periodic reconcile queued=%d", queued)
                next_reconcile_at = loop.time() + max(10.0, reconcile_seconds)
            iteration_result = await _background_iteration_until_stopped(stop_event)
            if iteration_result is None:
                break
            count, related_count = iteration_result
            if count or related_count:
                logger.info("[LAW_EMBED_JOBS] записано векторов=%d", count)
                await asyncio.sleep(0)
                continue
        except asyncio.CancelledError:
            raise
        except LawEmbeddingInferenceTimeout:
            # A native torch thread cannot be cancelled safely. Dedicated wrapper
            # must terminate the process instead of starting another batch.
            raise
        except Exception:
            logger.exception("[LAW_EMBED_JOBS] worker iteration failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
        except asyncio.TimeoutError:
            pass


async def _server_queue_state(server_id: str, max_attempts: int) -> Dict[str, int]:
    row = await db_manager.fetchrow(
        """
        SELECT
            (SELECT count(*) FROM law_embedding_jobs
             WHERE server_id = $1 AND space_id = $2) AS embedding_total,
            (SELECT count(*) FROM law_embedding_jobs
             WHERE server_id = $1 AND space_id = $2
               AND status = 'failed' AND attempts >= $3) AS embedding_terminal,
            (SELECT count(*) FROM law_related_rebuild_jobs
             WHERE server_id = $1) AS related_total,
            (SELECT count(*) FROM law_related_rebuild_jobs
             WHERE server_id = $1
               AND status = 'failed' AND attempts >= $3) AS related_terminal
        """,
        server_id,
        active_space_id(),
        int(max_attempts),
    )
    return {
        "embedding_total": int(row["embedding_total"] or 0),
        "embedding_terminal": int(row["embedding_terminal"] or 0),
        "related_total": int(row["related_total"] or 0),
        "related_terminal": int(row["related_terminal"] or 0),
    }


async def process_server_until_idle(
    server_id: str,
    *,
    force: bool = False,
    batch_size: Optional[int] = None,
    rebuild_related: bool = True,
    deadline_seconds: Optional[float] = None,
) -> int:
    """Backfill one server and wait for durable queues to genuinely drain.

    Retryable/future and externally-processing jobs are waited for until the
    overall deadline.  Terminal failures and deadline expiry raise, giving the
    CLI a non-zero exit instead of reporting a false success.
    """
    await validate_active_space_passport()
    if not force:
        # An explicit server backfill is the operator's targeted retry. Revive
        # only missing/stale terminal targets; do not turn healthy vectors into
        # keep-current force refreshes.
        await retry_terminal_jobs(server_id, include_related=rebuild_related)
    await reconcile_embedding_jobs(server_id, force=force)
    stale_seconds = int(getattr(config, "LAW_EMBED_JOB_STALE_SECONDS", 600))
    max_attempts = int(getattr(config, "LAW_EMBED_JOB_MAX_ATTEMPTS", 5))
    poll_seconds = float(getattr(config, "LAW_EMBED_JOB_POLL_SECONDS", 2.0))
    deadline_window = float(
        deadline_seconds
        if deadline_seconds is not None
        else getattr(config, "LAW_EMBED_CLI_DEADLINE_SECONDS", 1800.0)
    )
    if deadline_window <= 0:
        raise ValueError("deadline_seconds must be positive")
    loop = asyncio.get_running_loop()
    deadline_at = loop.time() + deadline_window
    next_recovery_at = loop.time() + min(60.0, max(5.0, stale_seconds / 2))
    total = 0
    while True:
        if loop.time() >= next_recovery_at:
            await recover_stale_jobs(stale_seconds)
            await recover_stale_related_rebuilds(stale_seconds)
            next_recovery_at = loop.time() + min(60.0, max(5.0, stale_seconds / 2))

        count, _servers = await process_embedding_job_batch(
            limit=batch_size, server_id=server_id,
        )
        total += count
        if rebuild_related:
            await process_related_rebuild_batch(limit=1, server_id=server_id)

        state = await _server_queue_state(server_id, max_attempts)
        terminal_related = state["related_terminal"] if rebuild_related else 0
        if state["embedding_terminal"] or terminal_related:
            raise RuntimeError(
                "law embedding backfill has terminal failures: "
                f"server={server_id} embedding={state['embedding_terminal']} "
                f"related={terminal_related}"
            )
        related_remaining = state["related_total"] if rebuild_related else 0
        if not state["embedding_total"] and not related_remaining:
            return total

        remaining = deadline_at - loop.time()
        if remaining <= 0:
            raise TimeoutError(
                "law embedding backfill deadline exceeded: "
                f"server={server_id} embedding={state['embedding_total']} "
                f"related={related_remaining}"
            )
        try:
            await asyncio.sleep(min(poll_seconds, remaining))
        except asyncio.CancelledError:
            raise
