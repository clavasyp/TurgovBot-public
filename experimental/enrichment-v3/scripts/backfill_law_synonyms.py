#!/usr/bin/env python3
"""Deterministic, human-reviewed Phase B synonym workflow.

This CLI never calls Gemini, Vertex or another model.  It has four explicit
steps for one server:

* audit current readiness/coverage;
* export an immutable manifest for Codex/agent review;
* aggregate all reviewed batch artifacts into one globally collision-resolved
  full-server plan;
* atomically apply that plan under the shared lock hierarchy.

Generated artifacts are bound to source-document, article and synonym-corpus
fingerprints.  Batch size changes only review ergonomics: collision resolution
and the database write always see the complete server plan at once.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from law_sources_data import get_flat_servers
from services.law_backfill_lock import law_backfill_global_lock
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import active_space_id, catalog_server_lock
from services.law_source_snapshot import law_source_fingerprint
from services.law_synonym_service import (
    SynonymWriteError,
    acquire_synonym_write_lock,
    add_auto_synonyms_bulk_in_transaction,
    is_explicitly_empty_or_repealed_article,
    load_exact_signature_registry,
    preview_auto_synonyms_bulk_in_transaction,
    synonym_corpus_fingerprint,
)


MANIFEST_SCHEMA = "law-synonym-codex-manifest-v1"
BATCH_SCHEMA = "law-synonym-codex-batch-v1"
PLAN_SCHEMA = "law-synonym-codex-plan-v1"
CHECKPOINT_SCHEMA = "law-synonym-codex-apply-v1"

GENERATION_INSTRUCTIONS = (
    "Для каждой статьи добавь только узкие, подтвержденные ее текстом поисковые "
    "формулировки. Сохраняй действие, субъект/объект, отрицание и цель. Дай "
    "разговорную и юридическую canonical-форму; не используй общие одиночные "
    "роли/темы и не выдумывай факты. Верни только synonyms для каждого id."
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _with_hash(value: Dict[str, Any], field: str) -> Dict[str, Any]:
    result = dict(value)
    result[field] = _hash_payload(value)
    return result


def _verify_hash(value: Mapping[str, Any], field: str, schema: str) -> None:
    if value.get("schema") != schema:
        raise SynonymWriteError(
            f"expected schema {schema!r}, got {value.get('schema')!r}"
        )
    stored = value.get(field)
    body = dict(value)
    body.pop(field, None)
    actual = _hash_payload(body)
    if stored != actual:
        raise SynonymWriteError(
            f"{schema} {field} mismatch: stored={stored!r}, actual={actual!r}"
        )


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SynonymWriteError(f"{path} must contain one JSON object")
    return value


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class _Checkpoint:
    """Append-only fsynced checkpoint with an exact immutable header."""

    def __init__(self, path: Path, run_id: str, metadata: Dict[str, Any]):
        self.path = path
        self.run_id = run_id
        self.header = {"type": "header", "run_id": run_id, **metadata}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            first = json.loads(
                self.path.read_text(encoding="utf-8").splitlines()[0]
            )
            if first != self.header:
                raise RuntimeError(
                    f"checkpoint {self.path} header does not match this run contract"
                )
        else:
            self.append(self.header)

    def records(self) -> list[Dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def result(self) -> Optional[Dict[str, Any]]:
        return next(
            (
                record
                for record in self.records()[1:]
                if record.get("type") == "result"
            ),
            None,
        )

    def append(self, value: Dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())


async def _source_fingerprint(queryable, server_id: str) -> Dict[str, Any]:
    rows = await queryable.fetch(
        """
        SELECT id, server_id, source_thread_id, title, content_hash
        FROM law_documents
        WHERE server_id = $1
        ORDER BY id
        """,
        server_id,
    )
    return law_source_fingerprint([dict(row) for row in rows])


async def _snapshot(
    server_id: str,
    queryable=None,
) -> tuple[list[dict[str, Any]], float, Any]:
    queryable = queryable or db_manager
    rows = await queryable.fetch(
        """
        SELECT a.id, a.article_hash, a.code, a.title, a.doc_title, a.full_text,
               COALESCE(array_agg(s.term ORDER BY s.term)
                        FILTER (WHERE s.id IS NOT NULL AND s.status = 'approved'),
                        ARRAY[]::text[]) AS terms
        FROM law_articles a
        LEFT JOIN law_synonyms s
          ON s.article_id = a.id AND s.server_id = a.server_id
        WHERE a.server_id = $1 AND a.kind = 'article'
        GROUP BY a.id
        ORDER BY a.id
        """,
        server_id,
    )
    registry = await load_exact_signature_registry(queryable, server_id)
    result: list[dict[str, Any]] = []
    physical_total = 0
    evaluated_count = 0
    for row in rows:
        item = dict(row)
        item["terms"] = list(item.get("terms") or ())
        item["physical"] = len(item["terms"])
        item["effective"] = registry.effective_count(int(item["id"]))
        item["exempt"] = is_explicitly_empty_or_repealed_article(
            item.get("full_text")
        )
        if not item["exempt"]:
            physical_total += item["physical"]
            evaluated_count += 1
        result.append(item)
    average = physical_total / evaluated_count if evaluated_count else 0.0
    return result, average, registry


async def _write_gate(server_id: str) -> Dict[str, Any]:
    """Fail-closed readiness contract checked while document/catalog locks hold."""

    from services.law_enrichment_completion import (
        evaluate_law_enrichment_completion,
    )
    from services.law_search_service import _vector_corpus_state

    space_id = active_space_id()
    enrichment = await evaluate_law_enrichment_completion(
        db_manager,
        server_id,
    )
    counts = await db_manager.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM law_documents
           WHERE server_id = $1 AND derived_sync_pending IS TRUE)
             AS pending_documents,
          (SELECT count(*) FROM law_related_rebuild_jobs
           WHERE server_id = $1) AS related_jobs,
          (SELECT count(*) FROM law_embedding_jobs
           WHERE server_id = $1 AND space_id = $2) AS embedding_jobs
        """,
        server_id,
        space_id,
    )
    vector = await _vector_corpus_state(server_id, space_id)
    articles = enrichment.articles
    enriched = enrichment.enriched
    pending = int(counts["pending_documents"] or 0)
    related_jobs = int(counts["related_jobs"] or 0)
    embedding_jobs = int(counts["embedding_jobs"] or 0)
    ready = bool(
        articles > 0
        and enriched == articles
        and pending == 0
        and related_jobs == 0
        and embedding_jobs == 0
        and vector.total == articles
        and vector.complete
        and vector.compatible
    )
    return {
        "ready": ready,
        "articles": articles,
        "enriched": enriched,
        "derived_sync_pending": pending,
        "related_jobs": related_jobs,
        "embedding_jobs": embedding_jobs,
        "frida": {
            "space_id": space_id,
            "total": vector.total,
            "ready": vector.ready,
            "queued": vector.queued,
            "failed": vector.failed,
            "mismatched": vector.mismatched,
            "complete": vector.complete,
            "compatible": vector.compatible,
        },
    }


async def _transaction_gate(conn, server_id: str) -> Dict[str, Any]:
    """Repeat mutable readiness fields in the synonym write transaction."""

    from services.law_enrichment_completion import (
        evaluate_law_enrichment_completion,
    )

    space_id = active_space_id()
    enrichment = await evaluate_law_enrichment_completion(
        conn,
        server_id,
    )
    row = await conn.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM law_documents
           WHERE server_id = $1 AND derived_sync_pending IS TRUE) AS pending,
          (SELECT count(*) FROM law_embedding_jobs
           WHERE server_id = $1 AND space_id = $2) AS embedding_jobs,
          (SELECT count(*) FROM law_related_rebuild_jobs
           WHERE server_id = $1) AS related_jobs,
          (SELECT count(*)
             FROM law_articles a
             JOIN law_article_embeddings e
               ON e.article_id = a.id
              AND e.server_id = a.server_id
              AND e.space_id = $2
            WHERE a.server_id = $1 AND a.kind = 'article'
              AND e.embedding IS NOT NULL
              AND e.input_hash = a.embedding_local_input_hash
              AND a.embedding_local_space = $2) AS vectors
        """,
        server_id,
        space_id,
    )
    result = {key: int(row[key] or 0) for key in row.keys()}
    result["articles"] = enrichment.articles
    result["enriched"] = enrichment.enriched
    result["ready"] = bool(
        result["articles"] > 0
        and result["enriched"] == result["articles"]
        and result["pending"] == 0
        and result["embedding_jobs"] == 0
        and result["related_jobs"] == 0
        and result["vectors"] == result["articles"]
    )
    return result


async def _readiness_audit(
    server_id: str,
) -> tuple[Dict[str, Any], list[dict[str, Any]], float, Any]:
    rows, average, registry = await _snapshot(server_id)
    audit = await _write_gate(server_id)
    audit.update(
        {
            "physical_average": round(average, 2),
            "physical_below_8": sum(
                not bool(row.get("exempt")) and int(row["physical"]) < 8
                for row in rows
            ),
            "effective_below_8": sum(
                not bool(row.get("exempt")) and int(row["effective"]) < 8
                for row in rows
            ),
            "exempt_article_ids": [
                int(row["id"]) for row in rows if bool(row.get("exempt"))
            ],
            "legacy_exact_collision_groups": sum(
                len(owners) > 1 for owners in registry.owners.values()
            ),
        }
    )
    return audit, rows, average, registry


def _pending(
    rows: Iterable[dict[str, Any]],
    *,
    broad: bool,
    target: int,
    minimum: int,
) -> list[dict[str, Any]]:
    if broad:
        return [
            row
            for row in rows
            if not bool(row.get("exempt"))
            and (
                int(row["physical"]) < target
                or int(row["effective"]) < minimum
            )
        ]
    return [
        row
        for row in rows
        if not bool(row.get("exempt"))
        and int(row["physical"]) < minimum
    ]


def _generation_article(row: Mapping[str, Any]) -> Dict[str, Any]:
    value = {
        "id": int(row["id"]),
        "article_hash": str(row["article_hash"]),
        "code": row.get("code"),
        "title": row.get("title"),
        "document": row.get("doc_title"),
        "text": str(row.get("full_text") or ""),
        "have": list(row.get("terms") or ()),
        "physical": int(row["physical"]),
        "effective": int(row["effective"]),
    }
    value["generation_input_hash"] = _hash_payload(value)
    return value


def _manifest_pending_contract(manifest: Mapping[str, Any]) -> list[tuple[int, str, str]]:
    return sorted(
        (
            int(article["id"]),
            str(article["article_hash"]),
            str(article["generation_input_hash"]),
        )
        for batch in manifest["batches"]
        for article in batch["articles"]
    )


def _coverage(
    rows: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    target: int,
    minimum: int,
) -> Dict[str, Any]:
    evaluated = [row for row in rows if not bool(row.get("exempt"))]
    exempt_ids = [
        int(row["id"]) for row in rows if bool(row.get("exempt"))
    ]
    physical_average = (
        sum(int(row["physical"]) for row in evaluated) / len(evaluated)
        if evaluated
        else 0.0
    )
    poor_physical = [
        int(row["id"])
        for row in evaluated
        if int(row["physical"]) < minimum
    ]
    poor_effective = [
        int(row["id"])
        for row in evaluated
        if int(row["effective"]) < minimum
    ]
    ready = bool(
        evaluated
        and physical_average >= target
        and not poor_physical
        and (mode == "reference" or not poor_effective)
    )
    return {
        "ready": ready,
        "articles": len(rows),
        "evaluated_articles": len(evaluated),
        "exempt_article_ids": exempt_ids,
        "physical_average": round(physical_average, 4),
        "physical_below_minimum": poor_physical,
        "effective_below_minimum": poor_effective,
    }


def _collision_fingerprint(registry) -> Dict[str, Any]:
    """Fingerprint only legacy cross-article exact claims."""

    collisions = [
        {
            "key": repr(key),
            "owners": sorted(int(owner) for owner in owners),
        }
        for key, owners in registry.owners.items()
        if len(owners) > 1
    ]
    collisions.sort(key=lambda value: (value["key"], value["owners"]))
    return {
        "groups": len(collisions),
        "value": _hash_payload(collisions),
    }


async def _assert_manifest_current(
    queryable,
    server_id: str,
    manifest: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], Any]:
    source = await _source_fingerprint(queryable, server_id)
    if source != manifest["source_fingerprint"]:
        raise SynonymWriteError("source fingerprint changed since manifest export")
    synonym = await synonym_corpus_fingerprint(queryable, server_id)
    if synonym != manifest["synonym_fingerprint"]:
        raise SynonymWriteError("synonym corpus changed since manifest export")
    rows, _average, registry = await _snapshot(server_id, queryable)
    pending = _pending(
        rows,
        broad=manifest["mode"] == "broad",
        target=int(manifest["target"]),
        minimum=int(manifest["minimum"]),
    )
    current_contract = sorted(
        (
            int(value["id"]),
            str(value["article_hash"]),
            str(_generation_article(value)["generation_input_hash"]),
        )
        for value in pending
    )
    if current_contract != _manifest_pending_contract(manifest):
        raise SynonymWriteError(
            "pending article set/content changed since manifest export"
        )
    return rows, registry


async def export_manifest(
    server_id: str,
    *,
    run_id: str,
    target: int,
    minimum: int,
    batch_size: int,
) -> Dict[str, Any]:
    async with law_backfill_global_lock():
        async with law_document_server_lock(server_id):
            async with catalog_server_lock(server_id):
                gate = await _write_gate(server_id)
                if not gate["ready"]:
                    raise SynonymWriteError(
                        "server readiness gate failed: "
                        + json.dumps(gate, ensure_ascii=False)
                    )
                async with db_manager.transaction() as conn:
                    await acquire_synonym_write_lock(conn, server_id)
                    transaction_gate = await _transaction_gate(conn, server_id)
                    if not transaction_gate["ready"]:
                        raise SynonymWriteError(
                            "transaction readiness failed during manifest export"
                        )
                    # All three pieces are read in this one protected
                    # transaction: there is no mixed-generation manifest.
                    source = await _source_fingerprint(conn, server_id)
                    synonym = await synonym_corpus_fingerprint(conn, server_id)
                    rows, average, _registry = await _snapshot(server_id, conn)
                    mode = "broad" if average < target else "reference"
                    pending = _pending(
                        rows,
                        broad=mode == "broad",
                        target=target,
                        minimum=minimum,
                    )
                    articles = [_generation_article(row) for row in pending]
                    batches = []
                    for start in range(0, len(articles), batch_size):
                        batch_articles = articles[start : start + batch_size]
                        batches.append(
                            {
                                "batch_id": (
                                    f"{run_id}:{len(batches) + 1:05d}"
                                ),
                                "articles": batch_articles,
                            }
                        )
                    body = {
                        "schema": MANIFEST_SCHEMA,
                        "run_id": run_id,
                        "server_id": server_id,
                        "target": target,
                        "minimum": minimum,
                        "mode": mode,
                        "batch_size": batch_size,
                        "instructions": GENERATION_INSTRUCTIONS,
                        "source_fingerprint": source,
                        "synonym_fingerprint": synonym,
                        "baseline": _coverage(
                            rows,
                            mode=mode,
                            target=target,
                            minimum=minimum,
                        ),
                        "batches": batches,
                    }
                    return _with_hash(body, "manifest_hash")


def _validated_batch_items(
    manifest: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    expected_batches = {
        str(batch["batch_id"]): batch for batch in manifest["batches"]
    }
    received: Dict[str, Mapping[str, Any]] = {}
    for artifact in artifacts:
        if artifact.get("schema") != BATCH_SCHEMA:
            raise SynonymWriteError("review artifact has wrong schema")
        for field in ("manifest_hash", "run_id", "server_id"):
            expected = manifest[
                "manifest_hash" if field == "manifest_hash" else field
            ]
            if artifact.get(field) != expected:
                raise SynonymWriteError(
                    f"review artifact {field} does not match manifest"
                )
        if artifact.get("source_fingerprint") != manifest["source_fingerprint"]:
            raise SynonymWriteError(
                "review artifact source fingerprint does not match manifest"
            )
        batch_id = str(artifact.get("batch_id") or "")
        if batch_id not in expected_batches or batch_id in received:
            raise SynonymWriteError(f"unknown/duplicate batch_id {batch_id!r}")
        received[batch_id] = artifact
    if set(received) != set(expected_batches):
        missing = sorted(set(expected_batches) - set(received))
        raise SynonymWriteError(f"incomplete review artifact coverage: {missing}")

    items: list[Dict[str, Any]] = []
    for batch_id, expected_batch in expected_batches.items():
        artifact = received[batch_id]
        values = artifact.get("articles")
        if not isinstance(values, list):
            raise SynonymWriteError(f"batch {batch_id} articles must be an array")
        expected = {
            int(article["id"]): article
            for article in expected_batch["articles"]
        }
        actual_ids: list[int] = []
        for value in values:
            if not isinstance(value, dict):
                raise SynonymWriteError(f"batch {batch_id} has non-object item")
            article_id = value.get("id")
            synonyms = value.get("synonyms")
            if isinstance(article_id, bool) or not isinstance(article_id, int):
                raise SynonymWriteError(f"batch {batch_id} has invalid id")
            if (
                not isinstance(synonyms, list)
                or not all(isinstance(term, str) for term in synonyms)
            ):
                raise SynonymWriteError(
                    f"batch {batch_id} article {article_id} has invalid synonyms"
                )
            source = expected.get(article_id)
            if source is None:
                raise SynonymWriteError(
                    f"batch {batch_id} contains unexpected article {article_id}"
                )
            if (
                value.get("article_hash") != source["article_hash"]
                or value.get("generation_input_hash")
                != source["generation_input_hash"]
            ):
                raise SynonymWriteError(
                    f"batch {batch_id} article {article_id} source contract changed"
                )
            actual_ids.append(article_id)
            items.append(
                {
                    "id": article_id,
                    "article_hash": str(source["article_hash"]),
                    "synonyms": synonyms,
                }
            )
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected):
            raise SynonymWriteError(
                f"batch {batch_id} article coverage is not exact"
            )
    return sorted(items, key=lambda item: int(item["id"]))


def _resolved_rows(
    rows: Sequence[Mapping[str, Any]],
    accepted: Mapping[int, Sequence[str]],
    effective: Mapping[int, int],
) -> list[Dict[str, Any]]:
    result = []
    for row in rows:
        article_id = int(row["id"])
        value = dict(row)
        value["physical"] = len(
            set(row.get("terms") or ()) | set(accepted.get(article_id, ()))
        )
        value["effective"] = int(
            effective.get(article_id, row["effective"])
        )
        result.append(value)
    return result


async def aggregate_plan(
    manifest: Mapping[str, Any],
    artifacts: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    _verify_hash(manifest, "manifest_hash", MANIFEST_SCHEMA)
    server_id = str(manifest["server_id"])
    items = _validated_batch_items(manifest, artifacts)
    async with law_backfill_global_lock():
        async with law_document_server_lock(server_id):
            async with catalog_server_lock(server_id):
                gate = await _write_gate(server_id)
                if not gate["ready"]:
                    raise SynonymWriteError(
                        "readiness changed before aggregation"
                    )
                async with db_manager.transaction() as conn:
                    await acquire_synonym_write_lock(conn, server_id)
                    rows, registry = await _assert_manifest_current(
                        conn, server_id, manifest
                    )
                    preview = await preview_auto_synonyms_bulk_in_transaction(
                        conn,
                        server_id,
                        items,
                        registry=registry,
                        lock_already_held=True,
                    )
                    resolved = _resolved_rows(
                        rows,
                        preview.accepted_terms,
                        preview.effective_after,
                    )
                    coverage = _coverage(
                        resolved,
                        mode=str(manifest["mode"]),
                        target=int(manifest["target"]),
                        minimum=int(manifest["minimum"]),
                    )
                    if not coverage["ready"]:
                        raise SynonymWriteError(
                            "reviewed full-server plan does not reach final floors: "
                            + json.dumps(coverage, ensure_ascii=False)
                        )
                    body = {
                        "schema": PLAN_SCHEMA,
                        "run_id": str(manifest["run_id"]),
                        "server_id": server_id,
                        "manifest_hash": str(manifest["manifest_hash"]),
                        "source_fingerprint": manifest["source_fingerprint"],
                        "synonym_fingerprint": manifest["synonym_fingerprint"],
                        "mode": str(manifest["mode"]),
                        "target": int(manifest["target"]),
                        "minimum": int(manifest["minimum"]),
                        "pending_contract": _manifest_pending_contract(manifest),
                        "items": items,
                        "resolution": {
                            "accepted_terms": preview.accepted_terms,
                            "rejected_terms": preview.rejected_terms,
                            "effective_before": preview.effective_before,
                            "effective_after": preview.effective_after,
                            "legacy_collision_fingerprint": _collision_fingerprint(
                                registry
                            ),
                        },
                        "predicted_coverage": coverage,
                    }
                    return _with_hash(body, "plan_hash")


def _int_key_map(value: Mapping[Any, Any]) -> Dict[int, Any]:
    return {int(key): item for key, item in value.items()}


async def _assert_plan_current(
    conn,
    plan: Mapping[str, Any],
    *,
    require_synonym_fingerprint: bool,
) -> tuple[list[dict[str, Any]], Any]:
    server_id = str(plan["server_id"])
    source = await _source_fingerprint(conn, server_id)
    if source != plan["source_fingerprint"]:
        raise SynonymWriteError("source fingerprint changed since plan review")
    if require_synonym_fingerprint:
        synonym = await synonym_corpus_fingerprint(conn, server_id)
        if synonym != plan["synonym_fingerprint"]:
            raise SynonymWriteError("synonym corpus changed since plan review")
    rows, _average, registry = await _snapshot(server_id, conn)
    pending = _pending(
        rows,
        broad=plan["mode"] == "broad",
        target=int(plan["target"]),
        minimum=int(plan["minimum"]),
    )
    contract = sorted(
        (
            int(row["id"]),
            str(row["article_hash"]),
            str(_generation_article(row)["generation_input_hash"]),
        )
        for row in pending
    )
    expected = [
        (int(item[0]), str(item[1]), str(item[2]))
        for item in plan["pending_contract"]
    ]
    if contract != sorted(expected):
        raise SynonymWriteError("pending/source contract changed since plan review")
    return rows, registry


def _same_resolution(result, plan: Mapping[str, Any]) -> bool:
    expected = plan["resolution"]
    return (
        {
            key: sorted(value)
            for key, value in result.accepted_terms.items()
        }
        == {
            key: sorted(value)
            for key, value in _int_key_map(
                expected["accepted_terms"]
            ).items()
        }
        and {
            key: sorted(value)
            for key, value in result.rejected_terms.items()
        }
        == {
            key: sorted(value)
            for key, value in _int_key_map(
                expected["rejected_terms"]
            ).items()
        }
    )


async def _committed_log_rows(conn, run_id: str, server_id: str) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        """
        SELECT log.synonym_id AS id, log.run_id::text AS run_id,
               log.server_id, log.article_id, log.article_hash, log.term,
               log.source, log.status, log.weight, log.suggested_by,
               synonym.id AS current_id,
               synonym.server_id AS current_server_id,
               synonym.article_id AS current_article_id,
               synonym.term AS current_term,
               synonym.source AS current_source,
               synonym.status AS current_status,
               synonym.weight AS current_weight,
               synonym.suggested_by AS current_suggested_by,
               article.article_hash AS current_article_hash
        FROM law_synonym_backfill_inserts log
        LEFT JOIN law_synonyms synonym ON synonym.id = log.synonym_id
        LEFT JOIN law_articles article ON article.id = synonym.article_id
        WHERE log.run_id = $1::uuid AND log.server_id = $2
        ORDER BY log.synonym_id
        """,
        run_id,
        server_id,
    )
    result = []
    for row in rows:
        expected = {
            "server_id": row["server_id"],
            "article_id": row["article_id"],
            "term": row["term"],
            "source": row["source"],
            "status": row["status"],
            "weight": float(row["weight"]),
            "suggested_by": row["suggested_by"],
            "article_hash": row["article_hash"],
        }
        actual = {
            "server_id": row["current_server_id"],
            "article_id": row["current_article_id"],
            "term": row["current_term"],
            "source": row["current_source"],
            "status": row["current_status"],
            "weight": (
                float(row["current_weight"])
                if row["current_weight"] is not None
                else None
            ),
            "suggested_by": row["current_suggested_by"],
            "article_hash": row["current_article_hash"],
        }
        if row["current_id"] is None or expected != actual:
            raise SynonymWriteError(
                f"committed run log row {row['id']} is missing or changed"
            )
        result.append(
            {
                key: row[key]
                for key in (
                    "id",
                    "run_id",
                    "server_id",
                    "article_id",
                    "article_hash",
                    "term",
                    "source",
                    "status",
                    "weight",
                    "suggested_by",
                )
            }
        )
    return result


async def _committed_run_metadata(
    conn,
    run_id: str,
) -> Optional[Dict[str, str]]:
    row = await conn.fetchrow(
        """
        SELECT run_id::text, server_id, plan_hash, manifest_hash
        FROM law_synonym_backfill_runs
        WHERE run_id = $1::uuid
        """,
        run_id,
    )
    if row is None:
        return None
    return {
        "run_id": str(row["run_id"]),
        "server_id": str(row["server_id"]),
        "plan_hash": str(row["plan_hash"]),
        "manifest_hash": str(row["manifest_hash"]),
    }


def _assert_run_metadata(
    metadata: Optional[Mapping[str, Any]],
    plan: Mapping[str, Any],
) -> None:
    expected = {
        "run_id": str(plan["run_id"]),
        "server_id": str(plan["server_id"]),
        "plan_hash": str(plan["plan_hash"]),
        "manifest_hash": str(plan["manifest_hash"]),
    }
    actual = (
        {key: str(metadata.get(key)) for key in expected}
        if metadata is not None
        else None
    )
    if actual != expected:
        raise SynonymWriteError(
            "run_id is not bound to this exact immutable plan"
        )


async def _recover_committed_apply(
    conn,
    plan: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    run_id = str(plan["run_id"])
    server_id = str(plan["server_id"])
    metadata = await _committed_run_metadata(conn, run_id)
    if metadata is None:
        logged = await _committed_log_rows(conn, run_id, server_id)
        if logged:
            raise SynonymWriteError(
                "committed insert rows exist without immutable run metadata"
            )
        return None
    _assert_run_metadata(metadata, plan)
    logged = await _committed_log_rows(conn, run_id, server_id)
    if await _source_fingerprint(conn, server_id) != plan["source_fingerprint"]:
        raise SynonymWriteError(
            "committed run exists but source fingerprint changed before recovery"
        )
    article_rows = await conn.fetch(
        """
        SELECT id, article_hash
        FROM law_articles
        WHERE server_id = $1 AND kind = 'article'
          AND id = ANY($2::int[])
        """,
        server_id,
        [int(item["id"]) for item in plan["items"]],
    )
    current_hashes = {
        int(row["id"]): str(row["article_hash"]) for row in article_rows
    }
    stale = [
        int(item["id"])
        for item in plan["items"]
        if current_hashes.get(int(item["id"])) != item["article_hash"]
    ]
    if stale:
        raise SynonymWriteError(
            f"committed run article hashes changed before recovery: {stale}"
        )
    accepted = _int_key_map(plan["resolution"]["accepted_terms"])
    rows = await conn.fetch(
        """
        SELECT article_id, term
        FROM law_synonyms
        WHERE server_id = $1 AND article_id = ANY($2::int[])
          AND status = 'approved'
        """,
        server_id,
        list(accepted),
    )
    current = {
        (int(row["article_id"]), str(row["term"])) for row in rows
    }
    missing = [
        (article_id, term)
        for article_id, terms in accepted.items()
        for term in terms
        if (article_id, term) not in current
    ]
    if missing:
        raise SynonymWriteError(
            f"committed run recovery found missing accepted terms: {missing[:20]}"
        )
    final_rows, _average, _registry = await _snapshot(server_id, conn)
    if (
        _collision_fingerprint(_registry)
        != plan["resolution"]["legacy_collision_fingerprint"]
    ):
        raise SynonymWriteError(
            "committed run recovery found a changed cross-article collision set"
        )
    coverage = _coverage(
        final_rows,
        mode=str(plan["mode"]),
        target=int(plan["target"]),
        minimum=int(plan["minimum"]),
    )
    if not coverage["ready"]:
        raise SynonymWriteError(
            "committed run exists but final coverage is not ready"
        )
    return {
        "type": "result",
        "recovered_from_db_log": True,
        "run_id": run_id,
        "server_id": server_id,
        "plan_hash": plan["plan_hash"],
        "inserted_rows": logged,
        "coverage": coverage,
    }


async def _validate_applied_state(
    conn,
    plan: Mapping[str, Any],
) -> Dict[str, Any]:
    """Revalidate a previously reported success against live protected DB state."""

    server_id = str(plan["server_id"])
    if await _source_fingerprint(conn, server_id) != plan["source_fingerprint"]:
        raise SynonymWriteError(
            "source fingerprint changed after the reported apply"
        )
    article_rows = await conn.fetch(
        """
        SELECT id, article_hash
        FROM law_articles
        WHERE server_id = $1 AND kind = 'article'
          AND id = ANY($2::int[])
        """,
        server_id,
        [int(item["id"]) for item in plan["items"]],
    )
    current_hashes = {
        int(row["id"]): str(row["article_hash"]) for row in article_rows
    }
    stale = [
        int(item["id"])
        for item in plan["items"]
        if current_hashes.get(int(item["id"])) != item["article_hash"]
    ]
    if stale:
        raise SynonymWriteError(
            f"applied plan article hashes are no longer current: {stale}"
        )

    accepted = _int_key_map(plan["resolution"]["accepted_terms"])
    accepted_rows = await conn.fetch(
        """
        SELECT article_id, term
        FROM law_synonyms
        WHERE server_id = $1 AND article_id = ANY($2::int[])
          AND status = 'approved'
        """,
        server_id,
        list(accepted),
    )
    current = {
        (int(row["article_id"]), str(row["term"])) for row in accepted_rows
    }
    missing = [
        (article_id, term)
        for article_id, terms in accepted.items()
        for term in terms
        if (article_id, term) not in current
    ]
    if missing:
        raise SynonymWriteError(
            f"accepted terms disappeared after apply: {missing[:20]}"
        )

    final_rows, _average, registry = await _snapshot(server_id, conn)
    if (
        _collision_fingerprint(registry)
        != plan["resolution"]["legacy_collision_fingerprint"]
    ):
        raise SynonymWriteError(
            "cross-article collision set changed after apply"
        )
    coverage = _coverage(
        final_rows,
        mode=str(plan["mode"]),
        target=int(plan["target"]),
        minimum=int(plan["minimum"]),
    )
    transaction_gate = await _transaction_gate(conn, server_id)
    if not coverage["ready"] or not transaction_gate["ready"]:
        raise SynonymWriteError(
            "live applied-state coverage/readiness is no longer valid"
        )
    return coverage


async def revalidate_checkpoint_result(
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Never trust a cached success without proving its exact rows still exist."""

    _verify_hash(plan, "plan_hash", PLAN_SCHEMA)
    for field in ("run_id", "server_id", "plan_hash"):
        if str(result.get(field)) != str(plan.get(field)):
            raise SynonymWriteError(
                f"checkpoint result {field} does not match the immutable plan"
            )
    server_id = str(plan["server_id"])
    run_id = str(plan["run_id"])
    async with law_backfill_global_lock():
        async with law_document_server_lock(server_id):
            async with catalog_server_lock(server_id):
                gate_before = await _write_gate(server_id)
                if not gate_before["ready"]:
                    raise SynonymWriteError(
                        "live readiness failed while revalidating checkpoint"
                    )
                async with db_manager.transaction() as conn:
                    await acquire_synonym_write_lock(conn, server_id)
                    metadata = await _committed_run_metadata(conn, run_id)
                    _assert_run_metadata(metadata, plan)
                    logged = await _committed_log_rows(
                        conn, run_id, server_id
                    )
                    expected_ids = sorted(
                        int(row["id"])
                        for row in (result.get("inserted_rows") or ())
                    )
                    logged_ids = sorted(int(row["id"]) for row in logged)
                    if expected_ids != logged_ids:
                        raise SynonymWriteError(
                            "checkpoint inserted-row evidence differs from DB log"
                        )
                    coverage = await _validate_applied_state(conn, plan)
                gate_after = await _write_gate(server_id)
                if not gate_after["ready"]:
                    raise SynonymWriteError(
                        "live readiness changed during checkpoint revalidation"
                    )
                validated = dict(result)
                validated["live_revalidated"] = True
                validated["coverage"] = coverage
                validated["gate_before"] = gate_before
                validated["gate_after"] = gate_after
                return validated


async def apply_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    _verify_hash(plan, "plan_hash", PLAN_SCHEMA)
    server_id = str(plan["server_id"])
    run_id = str(uuid.UUID(str(plan["run_id"])))
    async with law_backfill_global_lock():
        async with law_document_server_lock(server_id):
            async with catalog_server_lock(server_id):
                gate_before = await _write_gate(server_id)
                if not gate_before["ready"]:
                    raise SynonymWriteError("readiness failed before plan apply")

                # Crash recovery runs before the old synonym fingerprint check:
                # a committed run necessarily changed that fingerprint.
                async with db_manager.transaction() as conn:
                    await acquire_synonym_write_lock(conn, server_id)
                    recovered = await _recover_committed_apply(conn, plan)
                    if recovered is not None:
                        transaction_gate = await _transaction_gate(conn, server_id)
                        if not transaction_gate["ready"]:
                            raise SynonymWriteError(
                                "recovered run failed transaction readiness"
                            )
                    else:
                        rows, registry = await _assert_plan_current(
                            conn, plan, require_synonym_fingerprint=True
                        )
                        result = await add_auto_synonyms_bulk_in_transaction(
                            conn,
                            server_id,
                            plan["items"],
                            registry=registry,
                            run_id=run_id,
                            plan_hash=str(plan["plan_hash"]),
                            manifest_hash=str(plan["manifest_hash"]),
                            lock_already_held=True,
                        )
                        if not _same_resolution(result, plan):
                            raise SynonymWriteError(
                                "authoritative collision resolution changed "
                                "since plan aggregation"
                            )
                        if (
                            _collision_fingerprint(registry)
                            != plan["resolution"][
                                "legacy_collision_fingerprint"
                            ]
                        ):
                            raise SynonymWriteError(
                                "plan would create a new cross-article collision"
                            )
                        final_rows, _average, _final_registry = await _snapshot(
                            server_id, conn
                        )
                        if (
                            _collision_fingerprint(_final_registry)
                            != plan["resolution"][
                                "legacy_collision_fingerprint"
                            ]
                        ):
                            raise SynonymWriteError(
                                "final DB snapshot contains a new cross-article collision"
                            )
                        coverage = _coverage(
                            final_rows,
                            mode=str(plan["mode"]),
                            target=int(plan["target"]),
                            minimum=int(plan["minimum"]),
                        )
                        transaction_gate = await _transaction_gate(conn, server_id)
                        if not coverage["ready"] or not transaction_gate["ready"]:
                            raise SynonymWriteError(
                                "final coverage/readiness failed; transaction rolled back: "
                                + json.dumps(
                                    {
                                        "coverage": coverage,
                                        "gate": transaction_gate,
                                    },
                                    ensure_ascii=False,
                                )
                            )
                        if (
                            await _source_fingerprint(conn, server_id)
                            != plan["source_fingerprint"]
                        ):
                            raise SynonymWriteError(
                                "source changed inside apply transaction"
                            )
                        recovered = {
                            "type": "result",
                            "recovered_from_db_log": False,
                            "run_id": run_id,
                            "server_id": server_id,
                            "plan_hash": plan["plan_hash"],
                            "inserted_rows": result.inserted_rows,
                            "accepted_terms": result.accepted_terms,
                            "rejected_terms": result.rejected_terms,
                            "coverage": coverage,
                        }

                gate_after = await _write_gate(server_id)
                if not gate_after["ready"]:
                    # The transaction already committed, so surface this as an
                    # operational failure and preserve exact DB-log recovery.
                    raise SynonymWriteError(
                        "post-commit readiness changed; rerun recovers from DB log"
                    )
                assert recovered is not None
                recovered["gate_before"] = gate_before
                recovered["gate_after"] = gate_after
                return recovered


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic Codex-reviewed Phase B synonym workflow"
    )
    parser.add_argument("server_id")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--audit-only", action="store_true")
    action.add_argument("--export-manifest", type=Path)
    action.add_argument("--aggregate-manifest", type=Path)
    action.add_argument("--apply-plan", type=Path)
    parser.add_argument(
        "--batch-artifact",
        type=Path,
        action="append",
        default=[],
        help="repeat for every reviewed batch during --aggregate-manifest",
    )
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--target", type=int, default=12)
    parser.add_argument("--minimum", type=int, default=8)
    parser.add_argument("--batch-size", type=int, default=15)
    args = parser.parse_args()

    if args.server_id not in get_flat_servers():
        parser.error("server_id must be one configured game server")
    if args.target < args.minimum or args.minimum < 1 or args.batch_size < 1:
        parser.error("target >= minimum >= 1 and batch-size >= 1 are required")
    try:
        explicit_run_id = str(uuid.UUID(args.run_id)) if args.run_id else None
    except ValueError:
        parser.error("--run-id must be a UUID")

    await db_manager.connect()
    try:
        if args.audit_only:
            audit, _rows, _average, _registry = await _readiness_audit(
                args.server_id
            )
            print(json.dumps(audit, ensure_ascii=False, sort_keys=True))
            if not audit["ready"]:
                raise SystemExit(1)
            return

        if args.export_manifest:
            manifest = await export_manifest(
                args.server_id,
                run_id=explicit_run_id or str(uuid.uuid4()),
                target=args.target,
                minimum=args.minimum,
                batch_size=args.batch_size,
            )
            _write_json_exclusive(args.export_manifest, manifest)
            print(
                json.dumps(
                    {
                        "manifest": str(args.export_manifest),
                        "manifest_hash": manifest["manifest_hash"],
                        "run_id": manifest["run_id"],
                        "mode": manifest["mode"],
                        "batches": len(manifest["batches"]),
                        "articles": sum(
                            len(batch["articles"])
                            for batch in manifest["batches"]
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return

        if args.aggregate_manifest:
            if not args.batch_artifact or args.plan_out is None:
                parser.error(
                    "--aggregate-manifest requires --batch-artifact and --plan-out"
                )
            manifest = _load_json(args.aggregate_manifest)
            if manifest.get("server_id") != args.server_id:
                parser.error("manifest belongs to another server")
            artifacts = [_load_json(path) for path in args.batch_artifact]
            plan = await aggregate_plan(manifest, artifacts)
            _write_json_exclusive(args.plan_out, plan)
            print(
                json.dumps(
                    {
                        "plan": str(args.plan_out),
                        "plan_hash": plan["plan_hash"],
                        "run_id": plan["run_id"],
                        "coverage": plan["predicted_coverage"],
                    },
                    ensure_ascii=False,
                )
            )
            return

        assert args.apply_plan is not None
        if args.checkpoint is None:
            parser.error("--apply-plan requires --checkpoint")
        plan = _load_json(args.apply_plan)
        if plan.get("server_id") != args.server_id:
            parser.error("plan belongs to another server")
        _verify_hash(plan, "plan_hash", PLAN_SCHEMA)
        checkpoint = _Checkpoint(
            args.checkpoint,
            str(plan["run_id"]),
            {
                "schema": CHECKPOINT_SCHEMA,
                "server_id": args.server_id,
                "plan_hash": plan["plan_hash"],
                "mode": plan["mode"],
            },
        )
        prior = checkpoint.result()
        if prior is not None:
            validated = await revalidate_checkpoint_result(plan, prior)
            print(json.dumps(validated, ensure_ascii=False, sort_keys=True))
            return
        result = await apply_plan(plan)
        checkpoint.append(result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (SynonymWriteError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"REFUSED: {exc}\n")
    finally:
        await db_manager.close()


if __name__ == "__main__":
    asyncio.run(main())
