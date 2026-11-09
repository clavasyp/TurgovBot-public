"""Safety contracts for deterministic Codex-reviewed Phase B artifacts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import _apply_claude_enrich as full_enrich
from scripts import backfill_law_synonyms as backfill
from scripts import rollback_law_synonym_backfill as rollback
from services import law_enrichment_completion as enrichment_completion
from services import law_synonym_service as synonym_service
from services.law_catalog_service import _catalog_rows
from services.law_enrichment_artifact import (
    build_apply_envelope_evidence,
    build_law_enrichment_apply_envelope,
    enrichment_generation_input_hash,
    payload_sha256,
)
from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LAW_SOURCE_CATALOG_BINDING_SCHEMA,
    VerifiedLawCatalogTruth,
    verify_law_documents_catalog_truth,
)
from services.law_source_snapshot import law_source_content_fingerprint
from services.law_synonym_service import SynonymWriteError


def _row(article_id: int, *, physical: int, effective: int) -> dict:
    return {
        "id": article_id,
        "article_hash": f"hash-{article_id}",
        "physical": physical,
        "effective": effective,
    }


def test_pending_broad_and_reference_modes_have_stable_distinct_contracts():
    rows = [
        _row(1, physical=14, effective=11),
        _row(2, physical=7, effective=7),
        _row(3, physical=12, effective=12),
        _row(4, physical=14, effective=7),
    ]
    assert [
        row["id"]
        for row in backfill._pending(
            rows, broad=True, target=12, minimum=8
        )
    ] == [2, 4]
    assert [
        row["id"]
        for row in backfill._pending(
            rows, broad=False, target=12, minimum=8
        )
    ] == [2]


@pytest.mark.asyncio
async def test_readiness_gates_use_authoritative_completion(monkeypatch):
    from services import law_enrichment_completion as completion
    from services import law_search_service

    class _Completion:
        articles = 2
        # The second body hash may be current; context/output quality is not.
        enriched = 1

    calls = []

    async def evaluate(queryable, server_id):
        calls.append((queryable, server_id))
        return _Completion()

    monkeypatch.setattr(
        completion,
        "evaluate_law_enrichment_completion",
        evaluate,
    )

    class _Queryable:
        async def fetchrow(self, _query, *_args):
            return {
                "pending_documents": 0,
                "pending": 0,
                "related_jobs": 0,
                "embedding_jobs": 0,
                "vectors": 2,
            }

    db = _Queryable()
    monkeypatch.setattr(backfill, "db_manager", db)

    async def vector_state(_server_id, _space_id):
        return SimpleNamespace(
            total=2,
            ready=2,
            queued=0,
            failed=0,
            mismatched=0,
            complete=True,
            compatible=True,
        )

    monkeypatch.setattr(
        law_search_service,
        "_vector_corpus_state",
        vector_state,
    )

    outer = await backfill._write_gate("memphis")
    inner = await backfill._transaction_gate(db, "memphis")

    assert outer["ready"] is False
    assert inner["ready"] is False
    assert outer["articles"] == inner["articles"] == 2
    assert outer["enriched"] == inner["enriched"] == 1
    assert calls == [(db, "memphis"), (db, "memphis")]


def test_strict_empty_or_repealed_articles_are_reported_but_need_no_filler():
    healthy = _row(1, physical=12, effective=12)
    exempt = {
        **_row(2, physical=0, effective=0),
        "exempt": True,
    }
    rows = [healthy, exempt]

    assert backfill._pending(
        rows, broad=True, target=12, minimum=8
    ) == []
    assert backfill._pending(
        rows, broad=False, target=12, minimum=8
    ) == []
    coverage = backfill._coverage(
        rows, mode="broad", target=12, minimum=8
    )
    assert coverage["ready"] is True
    assert coverage["articles"] == 2
    assert coverage["evaluated_articles"] == 1
    assert coverage["exempt_article_ids"] == [2]
    assert coverage["physical_below_minimum"] == []
    assert coverage["effective_below_minimum"] == []


def test_checkpoint_resume_requires_mode_and_complete_exact_contract(tmp_path):
    path = tmp_path / "run.jsonl"
    run_id = "11111111-1111-1111-1111-111111111111"
    metadata = {
        "schema": backfill.CHECKPOINT_SCHEMA,
        "server_id": "memphis",
        "plan_hash": "a" * 64,
        "mode": "broad",
    }
    checkpoint = backfill._Checkpoint(path, run_id, metadata)
    checkpoint.append({"type": "progress", "run_id": run_id})
    backfill._Checkpoint(path, run_id, dict(metadata))

    incompatible = dict(metadata)
    incompatible["mode"] = "reference"
    with pytest.raises(RuntimeError, match="run contract"):
        backfill._Checkpoint(path, run_id, incompatible)


def _manifest(batch_size: int) -> dict:
    run_id = "11111111-1111-1111-1111-111111111111"
    articles = []
    for article_id in (1, 2):
        source = {
            "id": article_id,
            "article_hash": f"hash-{article_id}",
            "code": str(article_id),
            "title": f"Article {article_id}",
            "document": "Code",
            "text": f"Text {article_id}",
            "have": [],
            "physical": 0,
            "effective": 0,
        }
        source["generation_input_hash"] = backfill._hash_payload(source)
        articles.append(source)
    batches = []
    for start in range(0, len(articles), batch_size):
        batches.append({
            "batch_id": f"{run_id}:{len(batches) + 1:05d}",
            "articles": articles[start:start + batch_size],
        })
    body = {
        "schema": backfill.MANIFEST_SCHEMA,
        "run_id": run_id,
        "server_id": "memphis",
        "target": 12,
        "minimum": 8,
        "mode": "broad",
        "batch_size": batch_size,
        "instructions": "review",
        "source_fingerprint": {"value": "source"},
        "synonym_fingerprint": {"value": "synonyms"},
        "baseline": {},
        "batches": batches,
    }
    return backfill._with_hash(body, "manifest_hash")


def _artifacts(manifest: dict) -> list[dict]:
    result = []
    for batch in manifest["batches"]:
        result.append({
            "schema": backfill.BATCH_SCHEMA,
            "manifest_hash": manifest["manifest_hash"],
            "run_id": manifest["run_id"],
            "server_id": manifest["server_id"],
            "source_fingerprint": manifest["source_fingerprint"],
            "batch_id": batch["batch_id"],
            "articles": [
                {
                    "id": article["id"],
                    "article_hash": article["article_hash"],
                    "generation_input_hash": article["generation_input_hash"],
                    "synonyms": ["общая фраза"]
                    if article["id"] == 1
                    else ["общая фраза"],
                }
                for article in batch["articles"]
            ],
        })
    return result


def test_batch_partition_does_not_change_full_server_plan_items():
    one = _manifest(1)
    eight = _manifest(8)
    items_one = backfill._validated_batch_items(one, _artifacts(one))
    items_eight = backfill._validated_batch_items(eight, _artifacts(eight))
    assert items_one == items_eight
    assert {item["id"] for item in items_one} == {1, 2}


class _PreviewConn:
    async def fetch(self, query, *args):
        normalized = " ".join(query.split())
        if "SELECT id, article_hash FROM law_articles" in normalized:
            ids, _server_id = args
            return [
                {"id": article_id, "article_hash": f"hash-{article_id}"}
                for article_id in ids
            ]
        if "FROM unnest($1::text[]) AS candidate(term)" in normalized:
            return [
                {
                    "term": term,
                    "lexemes": sorted(set(term.split())),
                    "has_without": False,
                    "has_not": False,
                    "has_without_purpose": False,
                    "has_with_purpose": False,
                }
                for term in args[0]
            ]
        if "WITH candidate_input AS" in normalized:
            return []
        if "WITH candidate AS" in normalized:
            return []
        raise AssertionError(normalized)


@pytest.mark.asyncio
async def test_batch_size_one_or_eight_has_identical_global_resolution_and_coverage():
    outcomes = []
    for batch_size in (1, 8):
        manifest = _manifest(batch_size)
        items = backfill._validated_batch_items(
            manifest, _artifacts(manifest)
        )
        preview = await synonym_service.preview_auto_synonyms_bulk_in_transaction(
            _PreviewConn(),
            "memphis",
            items,
            registry=synonym_service.ExactSignatureRegistry(),
            lock_already_held=True,
        )
        rows = [
            {
                **_row(item["id"], physical=0, effective=0),
                "terms": [],
            }
            for item in items
        ]
        resolved = backfill._resolved_rows(
            rows, preview.accepted_terms, preview.effective_after
        )
        outcomes.append(
            (
                preview.accepted_terms,
                preview.rejected_terms,
                backfill._coverage(
                    resolved, mode="broad", target=12, minimum=8
                ),
            )
        )
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] == {}
    assert outcomes[0][1] == {
        1: ["общая фраза"],
        2: ["общая фраза"],
    }


def test_batch_artifacts_require_complete_exact_source_bound_coverage():
    manifest = _manifest(1)
    artifacts = _artifacts(manifest)
    with pytest.raises(SynonymWriteError, match="incomplete"):
        backfill._validated_batch_items(manifest, artifacts[:1])

    artifacts = _artifacts(manifest)
    artifacts[0]["articles"][0]["article_hash"] = "stale"
    with pytest.raises(SynonymWriteError, match="source contract"):
        backfill._validated_batch_items(manifest, artifacts)


def test_phase_b_cli_contains_no_automatic_model_api_path():
    source = Path(backfill.__file__).read_text(encoding="utf-8")
    forbidden = (
        "google.genai",
        "execute_vertex_request",
        "get_current_client",
        "init_client",
        "generate_content",
    )
    assert all(token not in source for token in forbidden)


def test_coverage_requires_average_and_floors_but_reference_preserves_legacy_effective():
    rows = [
        _row(1, physical=12, effective=8),
        _row(2, physical=12, effective=7),
    ]
    assert backfill._coverage(
        rows, mode="broad", target=12, minimum=8
    )["ready"] is False
    assert backfill._coverage(
        rows, mode="reference", target=12, minimum=8
    )["ready"] is True


def _full_article_row(
    article_id: int,
    article_hash: str,
    **updates,
) -> dict:
    row = {
        "id": article_id,
        "server_id": "memphis",
        "law_document_id": 100 + article_id,
        "source_order": article_id,
        "article_hash": article_hash,
        "article_ref": f"Статья {article_id}",
        "kind": "article",
        "code": str(article_id),
        "title": f"Article {article_id}",
        "title_provenance": "generated",
        "doc_title": "Code",
        "full_text": (
            f"Substantive text {article_id}. Наказание: штраф $100."
        ),
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": None,
        "enrichment_context_hash": None,
        "enrichment_output_hash": None,
    }
    row.update(updates)
    canonical_hash = full_enrich.source_article_hash(
        str(row["full_text"])
    )
    row["article_hash"] = canonical_hash
    if row.get("enrich_hash") == article_hash:
        row["enrich_hash"] = canonical_hash
    return row


class _FullConn:
    def __init__(
        self,
        rows,
        *,
        pending=False,
        auto_synonyms=(),
        artifact_commits=(),
        source_content_hash="source-content-hash",
        source_content="Exact law source content",
    ):
        self.rows = rows
        self.pending = pending
        self.auto_synonyms = list(auto_synonyms)
        self.artifact_commits = list(artifact_commits)
        self.source_content_hash = source_content_hash
        self.source_content = source_content
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if "INSERT INTO law_enrichment_artifact_commits" in query:
            (
                server_id,
                article_ids,
                article_hashes,
                generation_hashes,
                artifact_hashes,
                post_hashes,
            ) = args
            for values in zip(
                article_ids,
                article_hashes,
                generation_hashes,
                artifact_hashes,
                post_hashes,
            ):
                row = {
                    "server_id": server_id,
                    "article_id": values[0],
                    "article_hash": values[1],
                    "generation_input_hash": values[2],
                    "artifact_hash": values[3],
                    "post_commit_generation_hash": values[4],
                }
                unique = (
                    row["server_id"],
                    row["article_id"],
                    row["article_hash"],
                    row["artifact_hash"],
                    row["post_commit_generation_hash"],
                )
                if not any(
                    (
                        old["server_id"],
                        old["article_id"],
                        old["article_hash"],
                        old["artifact_hash"],
                        old["post_commit_generation_hash"],
                    ) == unique
                    for old in self.artifact_commits
                ):
                    self.artifact_commits.append(row)
        return "OK"

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "FROM unnest($1::text[])" in query:
            return [
                {
                    "term": term,
                    "lexemes": tuple(str(term).lower().split()),
                    "has_without": False,
                    "has_not": False,
                    "has_without_purpose": False,
                    "has_with_purpose": False,
                }
                for term in args[0]
            ]
        if "FROM law_documents" in query:
            return [
                {
                    "id": 900,
                    "server_id": "memphis",
                    "source_thread_id": 901,
                    "title": "Law source",
                    "content": self.source_content,
                    "content_hash": self.source_content_hash,
                }
            ]
        if "FROM law_enrichment_artifact_commits" in query:
            (
                server_id,
                article_ids,
                article_hashes,
                generation_hashes,
                artifact_hashes,
                post_hashes,
            ) = args
            expected = list(zip(
                article_ids,
                article_hashes,
                generation_hashes,
                artifact_hashes,
                post_hashes,
            ))
            return [
                row
                for row in self.artifact_commits
                if row["server_id"] == server_id
                and any(
                    row["article_id"] == article_id
                    and row["article_hash"] == article_hash
                    and row["generation_input_hash"] == generation_hash
                    and row["artifact_hash"] == artifact_hash
                    and (
                        not post_hash
                        or row["post_commit_generation_hash"] == post_hash
                    )
                    for (
                        article_id,
                        article_hash,
                        generation_hash,
                        artifact_hash,
                        post_hash,
                    ) in expected
                )
            ]
        if "FROM law_articles" in query:
            return self.rows
        if "FROM law_synonyms" in query:
            if "s.source" in query:
                return [
                    {
                        "id": index,
                        "server_id": "memphis",
                        "article_id": row["article_id"],
                        "term": row["term"],
                        "weight": row.get("weight", 1.0),
                        "source": row.get("source", "auto"),
                        "status": row.get("status", "approved"),
                        "suggested_by": row.get("suggested_by"),
                    }
                    for index, row in enumerate(
                        self.auto_synonyms,
                        start=1,
                    )
                ]
            return self.auto_synonyms
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "derived_sync_pending IS TRUE" in query:
            return self.pending
        raise AssertionError(query)


class _FullDb:
    def __init__(self, conn):
        self.conn = conn
        self.transactions = 0
        self.rolled_back = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        try:
            yield self.conn
        except Exception:
            self.rolled_back += 1
            raise


class _BackfillDb:
    def __init__(self, conn, events):
        self.conn = conn
        self.events = events

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction:enter")
        try:
            yield self.conn
        finally:
            self.events.append("transaction:exit")


def _install_backfill_locks(monkeypatch, events):
    @asynccontextmanager
    async def global_lock():
        events.append("global:enter")
        try:
            yield
        finally:
            events.append("global:exit")

    @asynccontextmanager
    async def document_lock(_server_id):
        events.append("document:enter")
        try:
            yield
        finally:
            events.append("document:exit")

    @asynccontextmanager
    async def catalog_lock(_server_id):
        events.append("catalog:enter")
        try:
            yield
        finally:
            events.append("catalog:exit")

    monkeypatch.setattr(backfill, "law_backfill_global_lock", global_lock)
    monkeypatch.setattr(backfill, "law_document_server_lock", document_lock)
    monkeypatch.setattr(backfill, "catalog_server_lock", catalog_lock)


@pytest.mark.asyncio
async def test_manifest_export_reads_one_locked_transaction_snapshot(monkeypatch):
    events = []
    conn = object()
    _install_backfill_locks(monkeypatch, events)
    monkeypatch.setattr(backfill, "db_manager", _BackfillDb(conn, events))

    async def gate(_server_id):
        events.append("gate")
        return {"ready": True}

    async def synonym_lock(conn_arg, _server_id):
        assert conn_arg is conn
        events.append("synonym-lock")

    async def transaction_gate(conn_arg, _server_id):
        assert conn_arg is conn
        events.append("transaction-gate")
        return {"ready": True}

    async def source(conn_arg, _server_id):
        assert conn_arg is conn
        events.append("source")
        return {"value": "source"}

    async def synonym_fingerprint(conn_arg, _server_id):
        assert conn_arg is conn
        events.append("synonym-fingerprint")
        return {"value": "synonym"}

    async def snapshot(_server_id, conn_arg):
        assert conn_arg is conn
        events.append("snapshot")
        row = {
            **_row(7, physical=0, effective=0),
            "code": "7",
            "title": "Article 7",
            "doc_title": "Code",
            "full_text": "Substantive article text",
            "terms": [],
            "exempt": False,
        }
        return [row], 0.0, object()

    monkeypatch.setattr(backfill, "_write_gate", gate)
    monkeypatch.setattr(backfill, "acquire_synonym_write_lock", synonym_lock)
    monkeypatch.setattr(backfill, "_transaction_gate", transaction_gate)
    monkeypatch.setattr(backfill, "_source_fingerprint", source)
    monkeypatch.setattr(
        backfill, "synonym_corpus_fingerprint", synonym_fingerprint
    )
    monkeypatch.setattr(backfill, "_snapshot", snapshot)

    manifest = await backfill.export_manifest(
        "memphis",
        run_id="11111111-1111-1111-1111-111111111111",
        target=12,
        minimum=8,
        batch_size=15,
    )

    assert manifest["source_fingerprint"] == {"value": "source"}
    assert manifest["synonym_fingerprint"] == {"value": "synonym"}
    assert events == [
        "global:enter",
        "document:enter",
        "catalog:enter",
        "gate",
        "transaction:enter",
        "synonym-lock",
        "transaction-gate",
        "source",
        "synonym-fingerprint",
        "snapshot",
        "transaction:exit",
        "catalog:exit",
        "document:exit",
        "global:exit",
    ]


@pytest.mark.asyncio
async def test_checkpoint_result_is_revalidated_against_live_db(monkeypatch):
    events = []
    conn = object()
    _install_backfill_locks(monkeypatch, events)
    monkeypatch.setattr(backfill, "db_manager", _BackfillDb(conn, events))
    monkeypatch.setattr(backfill, "_verify_hash", lambda *_args: None)

    gates = iter(({"ready": True}, {"ready": True}))

    async def gate(_server_id):
        events.append("gate")
        return next(gates)

    async def synonym_lock(conn_arg, _server_id):
        assert conn_arg is conn
        events.append("synonym-lock")

    async def log_rows(conn_arg, run_id, server_id):
        assert conn_arg is conn
        assert run_id == "11111111-1111-1111-1111-111111111111"
        assert server_id == "memphis"
        events.append("log-rows")
        return [{"id": 91}]

    async def run_metadata(conn_arg, run_id):
        assert conn_arg is conn
        events.append("run-metadata")
        return {
            "run_id": run_id,
            "server_id": "memphis",
            "plan_hash": "plan",
            "manifest_hash": "manifest",
        }

    async def validate(conn_arg, plan_arg):
        assert conn_arg is conn
        assert plan_arg["server_id"] == "memphis"
        events.append("validate-live-state")
        return {"ready": True, "evaluated_articles": 1}

    monkeypatch.setattr(backfill, "_write_gate", gate)
    monkeypatch.setattr(backfill, "acquire_synonym_write_lock", synonym_lock)
    monkeypatch.setattr(backfill, "_committed_run_metadata", run_metadata)
    monkeypatch.setattr(backfill, "_committed_log_rows", log_rows)
    monkeypatch.setattr(backfill, "_validate_applied_state", validate)
    plan = {
        "schema": backfill.PLAN_SCHEMA,
        "run_id": "11111111-1111-1111-1111-111111111111",
        "server_id": "memphis",
        "plan_hash": "plan",
        "manifest_hash": "manifest",
    }
    result = {
        "type": "result",
        "run_id": plan["run_id"],
        "server_id": "memphis",
        "plan_hash": "plan",
        "inserted_rows": [{"id": 91}],
    }

    validated = await backfill.revalidate_checkpoint_result(plan, result)

    assert validated["live_revalidated"] is True
    assert validated["coverage"]["ready"] is True
    assert events.index("log-rows") < events.index("validate-live-state")
    assert events[:4] == [
        "global:enter",
        "document:enter",
        "catalog:enter",
        "gate",
    ]


@pytest.mark.asyncio
async def test_checkpoint_result_refuses_missing_db_log_evidence(monkeypatch):
    events = []
    conn = object()
    _install_backfill_locks(monkeypatch, events)
    monkeypatch.setattr(backfill, "db_manager", _BackfillDb(conn, events))
    monkeypatch.setattr(backfill, "_verify_hash", lambda *_args: None)

    async def gate(_server_id):
        return {"ready": True}

    async def synonym_lock(_conn, _server_id):
        return None

    async def no_log(_conn, _run_id, _server_id):
        return []

    async def run_metadata(_conn, run_id):
        return {
            "run_id": run_id,
            "server_id": "memphis",
            "plan_hash": "plan",
            "manifest_hash": "manifest",
        }

    monkeypatch.setattr(backfill, "_write_gate", gate)
    monkeypatch.setattr(backfill, "acquire_synonym_write_lock", synonym_lock)
    monkeypatch.setattr(backfill, "_committed_run_metadata", run_metadata)
    monkeypatch.setattr(backfill, "_committed_log_rows", no_log)
    plan = {
        "schema": backfill.PLAN_SCHEMA,
        "run_id": "11111111-1111-1111-1111-111111111111",
        "server_id": "memphis",
        "plan_hash": "plan",
        "manifest_hash": "manifest",
    }
    result = {
        "type": "result",
        "run_id": plan["run_id"],
        "server_id": "memphis",
        "plan_hash": "plan",
        "inserted_rows": [{"id": 91}],
    }

    with pytest.raises(SynonymWriteError, match="evidence differs"):
        await backfill.revalidate_checkpoint_result(plan, result)


@pytest.mark.asyncio
async def test_recovery_refuses_reused_run_id_bound_to_another_plan(monkeypatch):
    calls = []

    async def wrong_metadata(_conn, run_id):
        calls.append("metadata")
        return {
            "run_id": run_id,
            "server_id": "memphis",
            "plan_hash": "old-plan",
            "manifest_hash": "manifest",
        }

    async def must_not_read_rows(*_args):
        calls.append("rows")
        return [{"id": 91}]

    monkeypatch.setattr(
        backfill, "_committed_run_metadata", wrong_metadata
    )
    monkeypatch.setattr(backfill, "_committed_log_rows", must_not_read_rows)
    plan = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "server_id": "memphis",
        "plan_hash": "new-plan",
        "manifest_hash": "manifest",
    }

    with pytest.raises(SynonymWriteError, match="exact immutable plan"):
        await backfill._recover_committed_apply(object(), plan)
    assert calls == ["metadata"]


def _install_full_lock_contexts(monkeypatch):
    @asynccontextmanager
    async def global_lock():
        yield

    @asynccontextmanager
    async def server_lock(_server_id):
        yield

    monkeypatch.setattr(full_enrich, "law_backfill_global_lock", global_lock)
    monkeypatch.setattr(full_enrich, "law_document_server_lock", server_lock)
    monkeypatch.setattr(full_enrich, "catalog_server_lock", server_lock)


def _install_full_locks(monkeypatch):
    _install_full_lock_contexts(monkeypatch)

    async def live_truth(conn, server_id, *, lock_rows=None):
        if not isinstance(conn.source_content, str) or not (
            conn.source_content.strip()
        ):
            raise SynonymWriteError(
                f"{server_id}: live source/catalog parser binding is unsafe; "
                "run normal catalog reconcile before apply"
            )
        fingerprint = law_source_content_fingerprint([{
            "id": 900,
            "server_id": server_id,
            "source_thread_id": 901,
            "title": "Law source",
            "content": conn.source_content,
        }])
        binding = {
            **_FULL_SOURCE_CATALOG_BINDING,
            "source_fingerprint": fingerprint,
        }
        return VerifiedLawCatalogTruth(
            server_id=server_id,
            source_documents=({
                "id": 900,
                "server_id": server_id,
                "source_thread_id": 901,
                "title": "Law source",
                "content": conn.source_content,
                "derived_sync_pending": False,
            },),
            catalog_rows=tuple(dict(row) for row in conn.rows),
            binding=binding,
        )

    monkeypatch.setattr(
        full_enrich,
        "_load_live_source_catalog_truth",
        live_truth,
    )
    # These are narrow transaction/apply tests with intentionally synthetic
    # catalog rows.  Patch the same verified-truth boundary used by completion
    # so both callers observe one identical owned snapshot; the real parser
    # replay contract has dedicated integration regressions.
    monkeypatch.setattr(
        enrichment_completion,
        "load_verified_law_catalog_truth",
        live_truth,
    )

    async def generation_state(
        conn,
        server_id,
        target_ids,
        *,
        lock_articles,
    ):
        assert server_id == "memphis"
        assert lock_articles is True
        normalized_ids = sorted(set(int(value) for value in target_ids))
        auto_rows = [
            {
                "id": index,
                "server_id": "memphis",
                "article_id": int(row["article_id"]),
                "term": str(row["term"]),
                "weight": float(row.get("weight", 1.0)),
                "source": str(row.get("source", "auto")),
                "status": str(row.get("status", "approved")),
                "suggested_by": row.get("suggested_by"),
            }
            for index, row in enumerate(conn.auto_synonyms, 1)
        ]
        return {
            "snapshot": {
                "selection_mode": "explicit",
                "source_catalog_binding": (
                    _FULL_SOURCE_CATALOG_BINDING
                ),
                "required_target_ids": normalized_ids,
                "value": "d" * 64,
            },
            "catalog_rows": [dict(row) for row in conn.rows],
            "articles": [dict(row) for row in conn.rows],
            "auto_rows": auto_rows,
            "context_hashes": {
                int(row["id"]): "e" * 64
                for row in conn.rows
            },
            "auto_fingerprints": {
                int(row["id"]): "f" * 64
                for row in conn.rows
            },
        }

    monkeypatch.setattr(
        full_enrich,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        generation_state,
    )


_FULL_CONTEXT_CONTRACT = {
    "algorithm_version": ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    "generation_policy_version": ENRICHMENT_GENERATION_POLICY_VERSION,
    "source_title_derivation_policy_version": (
        SOURCE_TITLE_DERIVATION_POLICY_VERSION
    ),
    "catalog_parser_contract_version": (
        LAW_CATALOG_PARSER_CONTRACT_VERSION
    ),
}


_FULL_SOURCE_FINGERPRINT = law_source_content_fingerprint([
    {
        "id": 900,
        "server_id": "memphis",
        "source_thread_id": 901,
        "title": "Law source",
        "content": "Exact law source content",
        "content_hash": "source-content-hash",
    }
])

_FULL_SOURCE_CATALOG_BINDING = {
    "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
    "parser_contract_version": LAW_CATALOG_PARSER_CONTRACT_VERSION,
    "server_id": "memphis",
    "source_fingerprint": _FULL_SOURCE_FINGERPRINT,
    "documents": 1,
    "catalog_rows": 1,
    "article_rows": 1,
    "tombstone_rows": 0,
    "source_projection_hash": "7" * 64,
    "catalog_projection_hash": "8" * 64,
    "value": "9" * 64,
}


def _full_source_document(content: str) -> dict:
    return {
        "id": 900,
        "server_id": "memphis",
        "source_thread_id": 901,
        "title": "Law source",
        "content": content,
        "content_hash": "legacy-hash-is-not-trusted",
        "derived_sync_pending": False,
    }


def _full_parser_catalog_row(content: str, *, article_id: int = 1) -> dict:
    parsed_rows = _catalog_rows(content)
    assert len(parsed_rows) == 1
    parsed = parsed_rows[0]
    return {
        "id": article_id,
        "server_id": "memphis",
        "parent_server_id": "memphis",
        "law_document_id": 900,
        **parsed,
        "doc_title": "Law source",
        "title_provenance": (
            "source" if parsed["title"] is not None else None
        ),
        "enrich_hash": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrichment_context_hash": None,
        "enrichment_output_hash": None,
    }


def _full_envelope(
    items,
    *,
    source_fingerprint=None,
    source_catalog_binding=None,
    context_hash=None,
    selection_mode="pending_enrichment",
    requested_article_ids=None,
    catalog_rows=None,
    review_source_projection_hash=None,
):
    article_ids = [int(item["id"]) for item in items]
    source_fingerprint = (
        source_fingerprint or _FULL_SOURCE_FINGERPRINT
    )
    source_catalog_binding = deepcopy(
        source_catalog_binding
        if source_catalog_binding is not None
        else {
            **_FULL_SOURCE_CATALOG_BINDING,
            "source_fingerprint": source_fingerprint,
        }
    )
    context_hashes = {
        str(item["id"]): context_hash or "e" * 64
        for item in items
    }
    if review_source_projection_hash is None and catalog_rows is not None:
        review_source_projection_hash = (
            full_enrich.build_review_source_projection(
                catalog_rows,
                article_ids,
                catalog_fields_resolver=full_enrich.derive_catalog_fields,
                article_hash_resolver=full_enrich.source_article_hash,
                is_tombstone=(
                    full_enrich.is_explicitly_empty_or_repealed_article
                ),
                canonicalize_scope=full_enrich.canonical_synonym_term,
            )["projection_hash"]
        )
    review_source_projection_hash = (
        review_source_projection_hash or "6" * 64
    )
    evidence = build_apply_envelope_evidence(
        "native_v3",
        {
            "aggregate_schema": (
                "law-full-enrichment-aggregate-evidence-v3"
            ),
            "manifest_schema": "law-full-enrichment-export-v3",
            "manifest_file": "manifest.json",
            "manifest_file_sha256": "1" * 64,
            "manifest_hash": "2" * 64,
            "article_ids": article_ids,
            "source_fingerprint": source_fingerprint,
            "source_catalog_binding": source_catalog_binding,
            "context_contract": _FULL_CONTEXT_CONTRACT,
            "generation_context_hashes": context_hashes,
            "review_source_projection_hash": (
                review_source_projection_hash
            ),
            "reviewed_plan_hash": payload_sha256(items),
            "selection_mode": selection_mode,
            "requested_article_ids": requested_article_ids,
            "inputs": [{
                "batch_number": 1,
                "source_file": "source-batch-00001.json",
                "source_file_sha256": "3" * 64,
                "batch_hash": "4" * 64,
                "reviewed_file": "reviewed-batch-00001.json",
                "reviewed_file_sha256": "5" * 64,
                "articles": len(article_ids),
                "article_ids": article_ids,
            }],
        },
    )
    return build_law_enrichment_apply_envelope(
        server_id="memphis",
        source_fingerprint=source_fingerprint,
        source_catalog_binding=source_catalog_binding,
        context_contract=_FULL_CONTEXT_CONTRACT,
        items=items,
        generation_context_hashes={
            int(item["id"]): context_hash or "e" * 64
            for item in items
        },
        evidence=evidence,
    )


async def _fake_complete_reviewed_write(
    conn,
    server_id,
    articles,
    items,
    generation_state,
    *,
    synonym_lock_already_held,
):
    assert server_id == "memphis"
    assert synonym_lock_already_held is True
    target_ids = sorted(int(item["id"]) for item in items)
    assert generation_state["snapshot"]["selection_mode"] == "explicit"
    assert generation_state["snapshot"]["required_target_ids"] == target_ids
    item_by_id = {int(item["id"]): item for item in items}
    article_by_id = {int(article["id"]): article for article in articles}
    new_auto = [
        row
        for row in conn.auto_synonyms
        if int(row["article_id"]) not in item_by_id
    ]
    inserted = 0
    for article_id, item in item_by_id.items():
        row = next(
            value
            for value in conn.rows
            if int(value["id"]) == article_id
        )
        row["title"] = item["title"]
        row["title_provenance"] = "generated"
        row["penalty_summary"] = item["penalty_summary"]
        row["penalty_json"] = (
            json.dumps(item["penalty"], ensure_ascii=False)
            if item["penalty"] is not None
            else None
        )
        row["enrich_hash"] = row["article_hash"]
        row["enrichment_context_hash"] = generation_state[
            "context_hashes"
        ][article_id]
        assert article_by_id[article_id]["article_hash"] == row["article_hash"]
        expanded = {
            term
            for terms in synonym_service.expand_synonym_terms(
                item["synonyms"]
            ).values()
            for term in terms
        }
        inserted += len(expanded)
        new_auto.extend(
            {"article_id": article_id, "term": term}
            for term in sorted(expanded)
        )
    conn.auto_synonyms = new_auto
    return {
        "articles": len(items),
        "synonyms": inserted,
        "embedding_jobs": len(items),
        "synonym_collisions_rejected": 0,
    }


def _full_item(
    article_id: int,
    article_hash: str,
    *,
    generation_article: dict | None = None,
) -> dict:
    requested_article_hash = article_hash
    generation_article = generation_article or _full_article_row(
        article_id, article_hash
    )
    article_hash = (
        requested_article_hash
        if requested_article_hash == ""
        else str(generation_article["article_hash"])
    )
    return {
        "id": article_id,
        "article_hash": article_hash,
        "title": "Precise title",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [
            f"precise article {article_id} formulation marker {suffix}"
            for suffix in "abcdefghijkl"
        ],
        "generation_input_hash": enrichment_generation_input_hash(
            generation_article, ()
        ),
    }


def test_full_enrich_live_contract_rejects_partial_exact_body_group():
    operative_body = (
        "1. Одинаковое длинное содержание нормы, которое применяется "
        "только в указанной структурной области."
    )
    catalog_rows = [
        {
            "id": 10,
            "law_document_id": 100,
            "source_order": 0,
            "article_ref": "Глава I",
            "kind": "chapter",
            "code": "I",
            "title": "Первая область",
            "doc_title": "Кодекс",
            "full_text": "Глава I\nПервая область",
            "article_hash": "chapter-1",
        },
        {
            **_full_article_row(1, "hash-1"),
            "law_document_id": 100,
            "source_order": 1,
            "full_text": f"Статья 1\n{operative_body}",
        },
        {
            "id": 20,
            "law_document_id": 100,
            "source_order": 2,
            "article_ref": "Глава II",
            "kind": "chapter",
            "code": "II",
            "title": "Вторая область",
            "doc_title": "Кодекс",
            "full_text": "Глава II\nВторая область",
            "article_hash": "chapter-2",
        },
        {
            **_full_article_row(2, "hash-2"),
            "law_document_id": 100,
            "source_order": 3,
            "full_text": f"Статья 2\n{operative_body}",
        },
    ]
    for row in catalog_rows:
        row["article_hash"] = full_enrich.source_article_hash(
            str(row["full_text"])
        )

    with pytest.raises(SynonymWriteError, match="partially covers"):
        full_enrich._validate_reviewed_source_contract(
            {"catalog_rows": catalog_rows},
            [_full_item(1, "hash-1")],
        )


def test_full_enrich_live_contract_rejects_unsupported_penalty():
    article = _full_article_row(
        1,
        "hash-1",
        full_text="Статья 1\nТекст нормы без какой-либо санкции.",
    )
    item = _full_item(1, "hash-1", generation_article=article)
    item["penalty_summary"] = "штраф $100"
    item["penalty"] = {"fine": "$100"}

    with pytest.raises(SynonymWriteError, match="not supported"):
        full_enrich._validate_reviewed_source_contract(
            {"catalog_rows": [article]},
            [item],
        )


def test_full_enrich_live_contract_rejects_unsupported_generated_title():
    article = _full_article_row(
        1,
        "hash-1",
        title=None,
        title_provenance=None,
        doc_title="Закон о лицензиях",
        full_text=(
            "Статья 1\n"
            "1) Суд может аннулировать лицензию за мошенничество."
        ),
    )
    item = _full_item(1, "hash-1", generation_article=article)
    item["title"] = "Аннулирование лицензии судом за опоздание"

    with pytest.raises(
        SynonymWriteError,
        match=r"article 1 reviewed title.*title evidence",
    ):
        full_enrich._validate_reviewed_source_contract(
            {"catalog_rows": [article]},
            [item],
        )


@pytest.mark.asyncio
async def test_full_enrich_unsupported_synonym_blocks_before_shared_writer(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    article = _full_article_row(
        1,
        "hash-1",
        full_text=(
            "Статья 1\n"
            "1) Лицензирующий орган может аннулировать лицензию "
            "при мошенничестве."
        ),
    )
    item = _full_item(1, "hash-1", generation_article=article)
    item["synonyms"][0] = (
        "[не доказанный scope] полиция автоматически аннулирует "
        "лицензию за опоздание"
    )
    conn = _FullConn([article])
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match=r"article 1 reviewed enrichment.*synonym evidence",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope(
                [item],
                selection_mode="explicit_articles",
                requested_article_ids=[1],
            ),
        )

    assert writes == []
    assert db.rolled_back == 1
    assert not any(
        "UPDATE law_articles" in query
        or "INSERT INTO law_synonyms" in query
        for _kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_native_v2_live_refuses_coherently_rehashed_offline_source(
    monkeypatch,
):
    """The live snapshot is the external anchor for a self-consistent export."""

    _install_full_locks(monkeypatch)
    reviewed_source = _full_article_row(1, "hash-1")
    live_source = {
        **reviewed_source,
        "doc_title": "Coherently rehashed but different document",
    }
    item = _full_item(
        1,
        "hash-1",
        generation_article=reviewed_source,
    )
    conn = _FullConn([live_source])
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="live reviewed-source projection differs",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope(
                [item],
                selection_mode="explicit_articles",
                requested_article_ids=[1],
                catalog_rows=[reviewed_source],
            ),
        )

    assert writes == []
    assert db.rolled_back == 1
    assert not any(
        "UPDATE law_articles" in query
        or "INSERT INTO law_synonyms" in query
        for _kind, query, _args in conn.calls
    )


def test_full_enrich_exact_group_uses_only_its_proven_scope_prefix():
    operative_body = (
        "1. Лицензирующий орган может аннулировать лицензию "
        "при мошенничестве."
    )
    catalog_rows = [
        {
            "id": 10,
            "law_document_id": 100,
            "source_order": 0,
            "article_ref": "Глава I",
            "kind": "chapter",
            "code": "I",
            "title": "Полиция автоматически аннулирует лицензию",
            "doc_title": "Кодекс",
            "full_text": (
                "Глава I\n"
                "Полиция автоматически аннулирует лицензию"
            ),
            "article_hash": "chapter-1",
        },
        {
            **_full_article_row(1, "hash-1"),
            "law_document_id": 100,
            "source_order": 1,
            "full_text": f"Статья 1\n{operative_body}",
        },
        {
            "id": 20,
            "law_document_id": 100,
            "source_order": 2,
            "article_ref": "Глава II",
            "kind": "chapter",
            "code": "II",
            "title": "Губернатор всегда аннулирует лицензию",
            "doc_title": "Кодекс",
            "full_text": (
                "Глава II\n"
                "Губернатор всегда аннулирует лицензию"
            ),
            "article_hash": "chapter-2",
        },
        {
            **_full_article_row(2, "hash-2"),
            "law_document_id": 100,
            "source_order": 3,
            "full_text": f"Статья 2\n{operative_body}",
        },
    ]
    for row in catalog_rows:
        row["article_hash"] = full_enrich.source_article_hash(
            str(row["full_text"])
        )
    plan = full_enrich.build_exact_duplicate_plan(
        catalog_rows,
        is_substantive_article=lambda _row: True,
        canonicalize_scope=full_enrich.canonical_synonym_term,
    )
    group = plan.groups[0]
    shared_suffixes = [
        (
            "лицензирующий орган может аннулировать лицензию "
            "при мошенничестве"
        ),
        *[
            f"точная нейтральная формулировка нормы {suffix}"
            for suffix in "abcdefghijk"
        ],
    ]
    items = []
    for member in group.members:
        source_article = next(
            row
            for row in catalog_rows
            if int(row["id"]) == member.article_id
        )
        item = _full_item(
            member.article_id,
            str(source_article["article_hash"]),
            generation_article=source_article,
        )
        item["title"] = f"{member.title_scope_prefix}Precise title"
        item["synonyms"] = [
            f"{member.synonym_scope_prefix}{suffix}"
            for suffix in shared_suffixes
        ]
        items.append(item)

    full_enrich._validate_reviewed_source_contract(
        {"catalog_rows": catalog_rows},
        items,
    )


@pytest.mark.asyncio
async def test_full_enrich_rejects_missing_generation_hash_before_any_write(monkeypatch):
    _install_full_locks(monkeypatch)
    db = _FullDb(_FullConn([]))
    monkeypatch.setattr(full_enrich, "db_manager", db)

    with pytest.raises(SynonymWriteError, match="generation-time"):
        item = _full_item(1, "")
        await full_enrich.apply_items(
            "memphis", _full_envelope([item])
        )
    assert db.transactions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    (
        lambda item: item.update(penalty="not-an-object"),
        lambda item: item.update(synonyms="not-an-array"),
        lambda item: item.update(title=["not", "a", "string"]),
        lambda item: item.update(penalty={"fine": None, "extra": "bad"}),
        lambda item: item.update(unexpected="field"),
        lambda item: item.update(id=True),
    ),
)
async def test_full_enrich_malformed_schema_touches_no_db_or_lock(
    monkeypatch, mutate
):
    events = []

    @asynccontextmanager
    async def global_lock():
        events.append("global")
        yield

    @asynccontextmanager
    async def server_lock(_server_id):
        events.append("server")
        yield

    monkeypatch.setattr(full_enrich, "law_backfill_global_lock", global_lock)
    monkeypatch.setattr(
        full_enrich, "law_document_server_lock", server_lock
    )
    monkeypatch.setattr(full_enrich, "catalog_server_lock", server_lock)
    db = _FullDb(_FullConn([]))
    monkeypatch.setattr(full_enrich, "db_manager", db)
    item = _full_item(1, "hash-1")
    artifact = _full_envelope([item])
    mutate(artifact["articles"][0])
    body = dict(artifact)
    body.pop("envelope_hash")
    artifact["envelope_hash"] = payload_sha256(body)

    with pytest.raises(SynonymWriteError):
        await full_enrich.apply_items("memphis", artifact)
    assert events == []
    assert db.transactions == 0
    assert db.conn.calls == []


@pytest.mark.asyncio
async def test_full_enrich_refuses_bare_reviewed_json_before_locks(
    monkeypatch,
):
    events = []

    @asynccontextmanager
    async def forbidden_lock(*_args, **_kwargs):
        events.append("lock")
        yield

    monkeypatch.setattr(
        full_enrich,
        "law_backfill_global_lock",
        forbidden_lock,
    )
    db = _FullDb(_FullConn([]))
    monkeypatch.setattr(full_enrich, "db_manager", db)

    with pytest.raises(SynonymWriteError, match="bare JSON arrays"):
        await full_enrich.apply_items(
            "memphis",
            [_full_item(1, "hash-1")],
        )
    assert events == []
    assert db.transactions == 0
    assert db.conn.calls == []


@pytest.mark.asyncio
async def test_full_enrich_neighbor_or_competitor_source_drift_rejects_all(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    item = _full_item(1, "hash-1")
    conn = _FullConn(
        [_full_article_row(1, "hash-1")],
        # The target row/generation_input_hash is unchanged.  Only another
        # source fact in the complete server snapshot changed after review.
        source_content="Changed non-target law source content",
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="complete server source fingerprint changed",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([item]),
        )
    assert writes == []
    assert db.rolled_back == 1


@pytest.mark.asyncio
async def test_full_enrich_refuses_missing_live_source_content_hash(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    item = _full_item(1, "hash-1")
    conn = _FullConn(
        [_full_article_row(1, "hash-1")],
        source_content=None,
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="source/catalog parser binding is unsafe",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([item]),
        )
    assert writes == []
    assert db.rolled_back == 1


@pytest.mark.asyncio
async def test_native_v3_live_apply_replays_source_to_catalog_before_write(
    monkeypatch,
):
    """A self-consistent but detached catalog cannot reach the writer."""

    _install_full_lock_contexts(monkeypatch)
    live_source = (
        "Статья 1.\n"
        "1. Лицо обязано соблюдать установленный порядок."
    )
    detached_source = (
        "Статья 1.\n"
        "1. Совершенно другая норма из чужого документа."
    )
    detached_row = _full_parser_catalog_row(detached_source)
    conn = _FullConn(
        [detached_row],
        source_content=live_source,
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )
    item = _full_item(
        1,
        str(detached_row["article_hash"]),
        generation_article=detached_row,
    )

    with pytest.raises(
        SynonymWriteError,
        match="source/catalog parser binding is unsafe",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope(
                [item],
                catalog_rows=[detached_row],
            ),
        )

    assert writes == []
    assert db.rolled_back == 1


@pytest.mark.asyncio
async def test_full_enrich_context_drift_rejects_all_before_writer(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    item = _full_item(1, "hash-1")
    conn = _FullConn([_full_article_row(1, "hash-1")])
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="enrichment context changed",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([item], context_hash="0" * 64),
        )
    assert writes == []
    assert db.rolled_back == 1


@pytest.mark.asyncio
async def test_apply_selection_recomputes_pending_ids_and_exact_closure(
    monkeypatch,
):
    from services import law_enrichment_completion

    seen = {}

    async def completion(*_args, **_kwargs):
        return SimpleNamespace(
            complete=False,
            incomplete_article_ids=(2,),
        )

    async def generation_state(
        _conn,
        _server_id,
        target_ids,
        **_kwargs,
    ):
        seen["target_ids"] = list(target_ids)
        return {
            "snapshot": {"required_target_ids": [1, 2]},
        }

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        completion,
    )
    monkeypatch.setattr(
        full_enrich,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        generation_state,
    )
    items = [_full_item(1, "hash-1"), _full_item(2, "hash-2")]
    envelope = full_enrich.validate_law_enrichment_apply_envelope(
        _full_envelope(items),
        expected_server_id="memphis",
    )

    _state, captured_completion = (
        await full_enrich._load_apply_selection_state(
            object(),
            "memphis",
            envelope,
        )
    )

    assert seen["target_ids"] == [2]
    assert captured_completion.incomplete_article_ids == (2,)


@pytest.mark.asyncio
async def test_apply_selection_refuses_pending_closure_drift(monkeypatch):
    from services import law_enrichment_completion

    async def completion(*_args, **_kwargs):
        return SimpleNamespace(
            complete=False,
            incomplete_article_ids=(1,),
        )

    async def generation_state(*_args, **_kwargs):
        return {
            "snapshot": {"required_target_ids": [1, 2, 3]},
        }

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        completion,
    )
    monkeypatch.setattr(
        full_enrich,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        generation_state,
    )
    items = [_full_item(1, "hash-1"), _full_item(2, "hash-2")]
    envelope = full_enrich.validate_law_enrichment_apply_envelope(
        _full_envelope(items),
        expected_server_id="memphis",
    )

    with pytest.raises(
        SynonymWriteError,
        match="selection/exact-group closure differs",
    ):
        await full_enrich._load_apply_selection_state(
            object(),
            "memphis",
            envelope,
        )


@pytest.mark.asyncio
async def test_apply_selection_rejects_new_unrelated_pending_target(
    monkeypatch,
):
    from services import law_enrichment_completion

    async def completion(*_args, **_kwargs):
        return SimpleNamespace(
            complete=False,
            incomplete_article_ids=(2,),
        )

    async def generation_state(*_args, **_kwargs):
        return {
            "snapshot": {"required_target_ids": [2]},
        }

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        completion,
    )
    monkeypatch.setattr(
        full_enrich,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        generation_state,
    )
    envelope = full_enrich.validate_law_enrichment_apply_envelope(
        _full_envelope([_full_item(1, "hash-1")]),
        expected_server_id="memphis",
    )

    with pytest.raises(
        SynonymWriteError,
        match="selection/exact-group closure differs",
    ):
        await full_enrich._load_apply_selection_state(
            object(),
            "memphis",
            envelope,
        )


@pytest.mark.asyncio
async def test_apply_selection_preserves_explicit_correction_mode(
    monkeypatch,
):
    seen = {}

    async def generation_state(
        _conn,
        _server_id,
        target_ids,
        **_kwargs,
    ):
        seen["target_ids"] = list(target_ids)
        return {
            "snapshot": {"required_target_ids": [1, 2]},
        }

    monkeypatch.setattr(
        full_enrich,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        generation_state,
    )
    items = [_full_item(1, "hash-1"), _full_item(2, "hash-2")]
    envelope = full_enrich.validate_law_enrichment_apply_envelope(
        _full_envelope(
            items,
            selection_mode="explicit_articles",
            requested_article_ids=[1],
        ),
        expected_server_id="memphis",
    )

    _state, completion = await full_enrich._load_apply_selection_state(
        object(),
        "memphis",
        envelope,
    )

    assert completion is None
    assert seen["target_ids"] == [1]


@pytest.mark.asyncio
async def test_full_enrich_refuses_unfinished_derived_sync_before_article_writes(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    conn = _FullConn(
        [_full_article_row(1, "hash-1")],
        pending=True,
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return True

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(SynonymWriteError, match="unfinished derived"):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([_full_item(1, "hash-1")]),
        )
    assert db.transactions == 1
    assert db.rolled_back == 1
    assert writes == []
    assert not any(
        kind == "fetch" and "FROM law_articles" in query
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_full_enrich_is_one_transaction_and_rolls_back_whole_artifact(monkeypatch):
    _install_full_locks(monkeypatch)
    conn = _FullConn([
        _full_article_row(1, "hash-1"),
        _full_article_row(2, "hash-2"),
    ])
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)

    writes = []

    async def write(
        conn_arg,
        _server_id,
        articles,
        items,
        _generation_state,
        **kwargs,
    ):
        writes.append((
            conn_arg,
            [int(article["id"]) for article in articles],
            [int(item["id"]) for item in items],
            kwargs,
        ))
        raise SynonymWriteError("quality")

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(SynonymWriteError, match="quality"):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([
                _full_item(1, "hash-1"),
                _full_item(2, "hash-2"),
            ], catalog_rows=conn.rows),
        )
    assert db.transactions == 1
    assert db.rolled_back == 1
    assert writes == [(
        conn,
        [1, 2],
        [1, 2],
        {"synonym_lock_already_held": True},
    )]
    assert conn.artifact_commits == []


@pytest.mark.asyncio
async def test_full_enrich_current_correction_completely_replaces_old_auto(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    existing_terms = [
        "existing precise phrase",
        "existing canonical phrase",
    ]
    current = _full_article_row(
        1,
        "hash-1",
        enrich_hash="hash-1",
    )
    conn = _FullConn(
        [current],
        auto_synonyms=[
            {"article_id": 1, "term": term}
            for term in existing_terms
        ],
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        _fake_complete_reviewed_write,
    )
    item = _full_item(1, "hash-1", generation_article=current)
    item["generation_input_hash"] = enrichment_generation_input_hash(
        current,
        existing_terms,
    )

    result = await full_enrich.apply_items(
        "memphis",
        _full_envelope([item], catalog_rows=conn.rows),
    )

    assert result["written"] == 1
    assert result["already_applied"] == 0
    final_terms = {
        row["term"]
        for row in conn.auto_synonyms
        if int(row["article_id"]) == 1
    }
    assert final_terms
    assert final_terms.isdisjoint(existing_terms)
    assert len(final_terms) >= 12
    assert db.rolled_back == 0


@pytest.mark.asyncio
async def test_full_enrich_refuses_changed_current_enrichment_generation(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    exported = _full_article_row(1, "hash-1")
    current = _full_article_row(
        1,
        "hash-1",
        penalty_summary="newer normal enrichment",
        penalty_json='{"other":"newer"}',
    )
    conn = _FullConn([current])
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return True

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(SynonymWriteError, match="generation input changed"):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([
                _full_item(
                    1,
                    "hash-1",
                    generation_article=exported,
                )
            ], catalog_rows=[exported]),
        )
    assert db.rolled_back == 1
    assert writes == []


def _durable_replay_case():
    exported = _full_article_row(1, "hash-1")
    item = _full_item(1, "hash-1", generation_article=exported)
    accepted_terms = sorted(
        {
            term
            for terms in synonym_service.expand_synonym_terms(
                item["synonyms"]
            ).values()
            for term in terms
        }
    )
    current = _full_article_row(
        1,
        "hash-1",
        enrich_hash="hash-1",
    )
    conn = _FullConn(
        [current],
        auto_synonyms=[
            {"article_id": 1, "term": term}
            for term in accepted_terms
        ],
        artifact_commits=[{
            "server_id": "memphis",
            "article_id": 1,
            "article_hash": current["article_hash"],
            "generation_input_hash": item["generation_input_hash"],
            "artifact_hash": full_enrich._exact_reviewed_artifact_hash(item),
            "post_commit_generation_hash":
                enrichment_generation_input_hash(
                    current,
                    accepted_terms,
                ),
        }],
    )
    return item, conn


@pytest.mark.asyncio
async def test_full_enrich_partial_collision_commit_replay_is_idempotent(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    from services import law_enrichment_completion

    async def complete(*_args, **_kwargs):
        return SimpleNamespace(
            complete=True,
            incomplete_article_ids=(),
        )

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        complete,
    )
    exported = _full_article_row(1, "hash-1")
    item = _full_item(1, "hash-1", generation_article=exported)
    item["penalty_summary"] = "штраф $100"
    item["penalty"] = {"fine": "$100"}
    desired_terms = sorted(
        {
            term
            for terms in synonym_service.expand_synonym_terms(
                item["synonyms"]
            ).values()
            for term in terms
        }
    )
    # The shared collision guard accepted all but one proposed row.  Replay
    # must use durable post-state evidence, not require the rejected proposal
    # to exist physically.
    accepted_terms = desired_terms[:-1]
    current = _full_article_row(
        1,
        "hash-1",
        penalty_summary="штраф $100",
        # asyncpg returns jsonb as text without a configured JSON codec.
        penalty_json='{"fine":"$100"}',
        enrich_hash="hash-1",
    )
    conn = _FullConn(
        [current],
        auto_synonyms=[
            {"article_id": 1, "term": term}
            for term in accepted_terms
        ],
        artifact_commits=[{
            "server_id": "memphis",
            "article_id": 1,
            "article_hash": current["article_hash"],
            "generation_input_hash": item["generation_input_hash"],
            "artifact_hash": full_enrich._exact_reviewed_artifact_hash(item),
            "post_commit_generation_hash":
                enrichment_generation_input_hash(
                    current,
                    accepted_terms,
                ),
        }],
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)

    result = await full_enrich.apply_items(
        "memphis",
        _full_envelope([item], catalog_rows=conn.rows),
    )

    assert result["written"] == 0
    assert result["already_applied"] == 1
    assert not any(
        "UPDATE law_articles" in query
        or "INSERT INTO law_synonyms" in query
        for _kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_generated_title_apply_keeps_binding_and_replay_is_noop(
    monkeypatch,
):
    """Generated output must not create a source→catalog binding cycle."""

    _install_full_lock_contexts(monkeypatch)
    source_content = (
        "Статья 1.\n"
        "1. Лицо обязано соблюдать установленный порядок."
    )
    source_document = _full_source_document(source_content)
    source_row = _full_parser_catalog_row(source_content)
    assert source_row["title"] is None
    initial_truth = verify_law_documents_catalog_truth(
        "memphis",
        [source_document],
        [source_row],
    )
    conn = _FullConn(
        [source_row],
        source_content=source_content,
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)

    async def generation_state(
        conn_arg,
        server_id,
        target_ids,
        *,
        lock_articles,
    ):
        assert server_id == "memphis"
        assert target_ids == [1]
        assert lock_articles is True
        truth = await full_enrich.load_verified_law_catalog_truth(
            conn_arg,
            server_id,
            lock_rows=True,
        )
        auto_rows = [
            {
                "id": index,
                "server_id": server_id,
                "article_id": int(row["article_id"]),
                "term": str(row["term"]),
                "weight": float(row.get("weight", 1.0)),
                "source": str(row.get("source", "auto")),
                "status": str(row.get("status", "approved")),
                "suggested_by": row.get("suggested_by"),
            }
            for index, row in enumerate(conn_arg.auto_synonyms, 1)
        ]
        catalog_rows = [dict(row) for row in truth.catalog_rows]
        return {
            "snapshot": {
                "selection_mode": "explicit",
                "source_catalog_binding": truth.binding,
                "required_target_ids": [1],
                "value": "d" * 64,
            },
            "catalog_rows": catalog_rows,
            "articles": [
                row for row in catalog_rows if row["kind"] == "article"
            ],
            "auto_rows": auto_rows,
            "context_hashes": {1: "e" * 64},
            "auto_fingerprints": {1: "f" * 64},
        }

    monkeypatch.setattr(
        full_enrich,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        generation_state,
    )
    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        _fake_complete_reviewed_write,
    )

    async def scoped_complete(*_args, **_kwargs):
        return SimpleNamespace(
            complete=True,
            incomplete_article_ids=(),
        )

    monkeypatch.setattr(
        enrichment_completion,
        "evaluate_law_enrichment_completion",
        scoped_complete,
    )

    item = _full_item(
        1,
        str(source_row["article_hash"]),
        generation_article=source_row,
    )
    envelope = _full_envelope(
        [item],
        source_fingerprint=initial_truth.source_fingerprint,
        source_catalog_binding=initial_truth.binding,
        selection_mode="explicit_articles",
        requested_article_ids=[1],
        catalog_rows=[source_row],
    )

    first = await full_enrich.apply_items("memphis", envelope)
    assert first["written"] == 1
    assert first["already_applied"] == 0
    assert first["synonyms"] >= 12
    assert first["stale"] == 0
    assert conn.rows[0]["title"] == item["title"]
    assert conn.rows[0]["title_provenance"] == "generated"

    after_write_truth = await full_enrich.load_verified_law_catalog_truth(
        conn,
        "memphis",
        lock_rows=True,
    )
    assert after_write_truth.binding == initial_truth.binding

    calls_before_replay = len(conn.calls)
    second = await full_enrich.apply_items("memphis", envelope)
    replay_calls = conn.calls[calls_before_replay:]
    assert second == {
        "written": 0,
        "already_applied": 1,
        "synonyms": 0,
        "stale": 0,
    }
    assert not any(
        "UPDATE law_articles" in query
        or "INSERT INTO law_synonyms" in query
        for _kind, query, _args in replay_calls
    )


@pytest.mark.asyncio
async def test_explicit_durable_replay_succeeds_only_when_scoped_current(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    from services import law_enrichment_completion

    seen = {}

    async def scoped_complete(_conn, _server_id, **kwargs):
        seen["article_ids"] = kwargs.get("article_ids")
        return SimpleNamespace(
            complete=True,
            incomplete_article_ids=(),
        )

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        scoped_complete,
    )
    item, conn = _durable_replay_case()
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)

    result = await full_enrich.apply_items(
        "memphis",
        _full_envelope(
            [item],
            selection_mode="explicit_articles",
            requested_article_ids=[1],
            catalog_rows=conn.rows,
        ),
    )

    assert result["written"] == 0
    assert result["already_applied"] == 1
    assert seen["article_ids"] == [1]


@pytest.mark.asyncio
async def test_explicit_durable_replay_rejects_scoped_output_drift(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    from services import law_enrichment_completion

    async def scoped_incomplete(*_args, **_kwargs):
        return SimpleNamespace(
            complete=False,
            incomplete_article_ids=(1,),
        )

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        scoped_incomplete,
    )
    item, conn = _durable_replay_case()
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="explicit artifact durable replay is not current",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope(
                [item],
                selection_mode="explicit_articles",
                requested_article_ids=[1],
                catalog_rows=conn.rows,
            ),
        )

    assert writes == []
    assert db.rolled_back == 1


@pytest.mark.asyncio
async def test_pending_replay_does_not_hide_weight_only_output_drift(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    from services import law_enrichment_completion

    async def output_mismatch(*_args, **_kwargs):
        return SimpleNamespace(
            complete=False,
            incomplete_article_ids=(1,),
        )

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        output_mismatch,
    )
    exported = _full_article_row(1, "hash-1")
    item = _full_item(1, "hash-1", generation_article=exported)
    accepted_terms = sorted(
        {
            term
            for terms in synonym_service.expand_synonym_terms(
                item["synonyms"]
            ).values()
            for term in terms
        }
    )
    current = _full_article_row(
        1,
        "hash-1",
        enrich_hash="hash-1",
    )
    conn = _FullConn(
        [current],
        auto_synonyms=[
            {
                "article_id": 1,
                "term": term,
                # This does not change generation_input_hash, but it changes
                # the persisted semantic enrichment output fingerprint.
                "weight": 0.5,
            }
            for term in accepted_terms
        ],
        artifact_commits=[{
            "server_id": "memphis",
            "article_id": 1,
            "article_hash": current["article_hash"],
            "generation_input_hash": item["generation_input_hash"],
            "artifact_hash": full_enrich._exact_reviewed_artifact_hash(item),
            "post_commit_generation_hash":
                enrichment_generation_input_hash(
                    current,
                    accepted_terms,
                ),
        }],
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return {}

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="not current under authoritative completion",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([item], catalog_rows=conn.rows),
        )

    assert writes == []
    assert db.rolled_back == 1


@pytest.mark.asyncio
async def test_full_enrich_rejects_partial_collision_without_commit_evidence(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    exported = _full_article_row(1, "hash-1")
    item = _full_item(1, "hash-1", generation_article=exported)
    item["penalty_summary"] = "штраф $100"
    item["penalty"] = {"fine": "$100"}
    conn = _FullConn([dict(exported)])
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)

    write_calls = []

    async def write(_conn, _server_id, articles, _items, _state, **_kwargs):
        write_calls.extend(int(article["id"]) for article in articles)
        raise SynonymWriteError(
            "server-wide replacement preview rejected exact-leg "
            "signature collisions"
        )

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="exact-leg signature collisions",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([item], catalog_rows=[exported]),
        )

    assert db.rolled_back == 1
    assert write_calls == [1]
    assert conn.artifact_commits == []


@pytest.mark.asyncio
async def test_full_enrich_commit_evidence_does_not_mask_later_state_change(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    exported = _full_article_row(1, "hash-1")
    item = _full_item(1, "hash-1", generation_article=exported)
    item["penalty_summary"] = "штраф $100"
    item["penalty"] = {"fine": "$100"}
    accepted_terms = ["another precise phrase"]
    committed = _full_article_row(
        1,
        "hash-1",
        penalty_summary="штраф $100",
        penalty_json='{"fine":"$100"}',
        enrich_hash="hash-1",
    )
    later = dict(committed)
    later["title"] = "Later manual title"
    conn = _FullConn(
        [later],
        auto_synonyms=[
            {"article_id": 1, "term": term}
            for term in accepted_terms
        ],
        artifact_commits=[{
            "server_id": "memphis",
            "article_id": 1,
            "article_hash": committed["article_hash"],
            "generation_input_hash": item["generation_input_hash"],
            "artifact_hash": full_enrich._exact_reviewed_artifact_hash(item),
            "post_commit_generation_hash":
                enrichment_generation_input_hash(
                    committed,
                    accepted_terms,
                ),
        }],
    )
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)
    writes = []

    async def write(*_args, **_kwargs):
        writes.append(True)
        return True

    monkeypatch.setattr(
        full_enrich,
        "_write_auto_enrichment_server_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="generation input changed",
    ):
        await full_enrich.apply_items(
            "memphis",
            _full_envelope([item], catalog_rows=[exported]),
        )
    assert writes == []
    assert db.rolled_back == 1


class _RollbackConnection:
    def __init__(
        self,
        rows,
        *,
        deleted=None,
        metadata_rows=None,
        auto_rows=None,
        fail_metadata_cas=False,
        documents=None,
        fail_pending_update=False,
    ):
        self.rows = [dict(row) for row in rows]
        self.deleted = deleted
        self.fail_metadata_cas = fail_metadata_cas
        self.fail_pending_update = fail_pending_update
        if metadata_rows is None:
            article_ids = sorted({
                int(row["article_id"])
                for row in self.rows
            })
            metadata_rows = [
                {
                    "id": article_id,
                    "server_id": "memphis",
                    "kind": "article",
                    "article_hash": next(
                        str(row["current_article_hash"])
                        for row in self.rows
                        if (
                            int(row["article_id"]) == article_id
                            and row["current_article_hash"] is not None
                        )
                    ),
                    "title": None,
                    "title_provenance": None,
                    "penalty_summary": None,
                    "penalty_json": None,
                    "enrich_hash": f"hash-{article_id}",
                    "enrichment_context_hash": f"context-{article_id}",
                    "enrichment_output_hash": None,
                }
                for article_id in article_ids
                if any(
                    int(row["article_id"]) == article_id
                    and row["current_article_hash"] is not None
                    for row in self.rows
                )
            ]
        self.metadata_rows = [dict(row) for row in metadata_rows]
        if auto_rows is None:
            auto_rows = [
                {
                    "id": int(row["synonym_id"]),
                    "server_id": row["current_server_id"],
                    "article_id": row["current_article_id"],
                    "term": row["current_term"],
                    "source": row["current_source"],
                    "status": row["current_status"],
                    "weight": row["current_weight"],
                    "suggested_by": row["current_suggested_by"],
                }
                for row in self.rows
                if row["current_id"] is not None
            ]
        self.auto_rows = [dict(row) for row in auto_rows]
        if documents is None:
            document_ids = sorted({
                int(row.get("law_document_id") or 10)
                for row in self.metadata_rows
            })
            documents = [
                {
                    "id": document_id,
                    "server_id": "memphis",
                    "derived_sync_pending": False,
                }
                for document_id in document_ids
            ]
        self.documents = [dict(row) for row in documents]
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "UPDATE law_documents" in query:
            server_id, requested_ids = args
            requested = {int(value) for value in requested_ids}
            updated = []
            for document in self.documents:
                if (
                    document["server_id"] == server_id
                    and int(document["id"]) in requested
                ):
                    document["derived_sync_pending"] = True
                    if not self.fail_pending_update:
                        updated.append({"id": int(document["id"])})
            return updated
        if "FROM law_synonym_backfill_inserts" in query:
            return self.rows
        if "DELETE FROM law_synonyms" in query:
            ids = list(args[2])
            returned = ids if self.deleted is None else self.deleted
            returned_set = {int(value) for value in returned}
            self.auto_rows = [
                row
                for row in self.auto_rows
                if int(row["id"]) not in returned_set
            ]
            return [{"id": synonym_id} for synonym_id in returned]
        if "SELECT s.article_id, s.term" in query:
            return [
                {
                    "article_id": int(row["article_id"]),
                    "term": str(row["term"]),
                }
                for row in self.auto_rows
                if row.get("status") == "approved"
            ]
        if "s.source = 'auto'" in query:
            return [dict(row) for row in self.auto_rows]
        if "FROM law_articles" in query:
            return [dict(row) for row in self.metadata_rows]
        if "DELETE FROM law_synonyms" in query:
            raise AssertionError("DELETE branch must precede synonym SELECT")
        if "FROM law_synonyms" in query:
            article_ids = {int(value) for value in args[0]}
            return [
                dict(row)
                for row in self.auto_rows
                if (
                    int(row["article_id"]) in article_ids
                    and row.get("source") == "auto"
                )
            ]
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "UPDATE law_articles" not in query:
            raise AssertionError(query)
        if self.fail_metadata_cas:
            return None
        (
            article_id,
            desired_output_hash,
            server_id,
            article_hash,
            title,
            title_provenance,
            penalty_summary,
            _penalty_json,
            enrich_hash,
            enrichment_context_hash,
            old_output_hash,
        ) = args
        article = next(
            (
                row
                for row in self.metadata_rows
                if int(row["id"]) == int(article_id)
            ),
            None,
        )
        if (
            article is None
            or article.get("server_id") != server_id
            or article.get("kind") != "article"
            or article.get("article_hash") != article_hash
            or article.get("title") != title
            or article.get("title_provenance") != title_provenance
            or article.get("penalty_summary") != penalty_summary
            or article.get("enrich_hash") != enrich_hash
            or article.get("enrichment_context_hash")
            != enrichment_context_hash
            or article.get("enrichment_output_hash") != old_output_hash
        ):
            return None
        article["enrichment_output_hash"] = desired_output_hash
        return int(article_id)


class _RollbackDb:
    def __init__(self, conn):
        self.conn = conn
        self.rolled_back = 0

    @asynccontextmanager
    async def transaction(self):
        metadata_before = deepcopy(self.conn.metadata_rows)
        auto_before = deepcopy(self.conn.auto_rows)
        documents_before = deepcopy(self.conn.documents)
        try:
            yield self.conn
        except Exception:
            self.conn.metadata_rows[:] = metadata_before
            self.conn.auto_rows[:] = auto_before
            self.conn.documents[:] = documents_before
            self.rolled_back += 1
            raise


def _install_rollback(
    monkeypatch,
    conn,
    *,
    incomplete_document_ids=(),
    use_real_completion=False,
):
    db = _RollbackDb(conn)
    monkeypatch.setattr(rollback, "db_manager", db)

    @asynccontextmanager
    async def lock(_server_id):
        yield

    monkeypatch.setattr(rollback, "law_document_server_lock", lock)
    monkeypatch.setattr(rollback, "catalog_server_lock", lock)
    if not use_real_completion:
        class _Completion:
            def __init__(self, incomplete):
                self.incomplete_document_ids = tuple(incomplete)

        async def evaluate(_conn, _server_id, _article_ids):
            return _Completion(incomplete_document_ids)

        monkeypatch.setattr(
            rollback,
            "_evaluate_affected_completion",
            evaluate,
        )
    return db


def _rollback_row(
    synonym_id: int,
    *,
    missing: bool = False,
    changed_field: str | None = None,
    term: str = "precise phrase",
) -> dict:
    row = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "synonym_id": synonym_id,
        "server_id": "memphis",
        "article_id": 7,
        "term": term,
        "article_hash": "hash-7",
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
        "current_id": None if missing else synonym_id,
        "current_server_id": None if missing else "memphis",
        "current_article_id": None if missing else 7,
        "current_term": None if missing else term,
        "current_source": None if missing else "auto",
        "current_status": None if missing else "approved",
        "current_weight": None if missing else 1.0,
        "current_suggested_by": None,
        "current_article_hash": None if missing else "hash-7",
    }
    if changed_field:
        row[f"current_{changed_field}"] = {
            "term": "changed",
            "source": "owner",
            "status": "rejected",
            "weight": 0.5,
            "article_hash": "changed-hash",
        }[changed_field]
    return row


@pytest.mark.asyncio
async def test_rollback_missing_or_changed_row_deletes_zero_all_or_nothing(monkeypatch):
    for bad in (
        _rollback_row(102, missing=True),
        _rollback_row(102, changed_field="weight"),
        _rollback_row(102, changed_field="status"),
    ):
        conn = _RollbackConnection([_rollback_row(101), bad])
        _install_rollback(monkeypatch, conn)
        result = await rollback.rollback_run(
            "11111111-1111-1111-1111-111111111111",
            "memphis",
            apply=True,
        )
        assert result["can_apply"] is False
        assert result["deleted_synonym_ids"] == []
        assert not any(
            "DELETE FROM law_synonyms" in call[1] for call in conn.calls
        )


@pytest.mark.asyncio
async def test_rollback_apply_deletes_exact_verified_identity_set(monkeypatch):
    conn = _RollbackConnection([_rollback_row(102), _rollback_row(101)])
    _install_rollback(monkeypatch, conn)
    result = await rollback.rollback_run(
        "11111111-1111-1111-1111-111111111111",
        "memphis",
        apply=True,
    )
    assert result["can_apply"] is True
    assert result["deleted_synonym_ids"] == [101, 102]
    assert result["updated_output_article_ids"] == []


@pytest.mark.asyncio
async def test_trusted_phase_b_apply_and_exact_rollback_both_remain_complete(
    monkeypatch,
):
    from services import law_enrichment_completion as completion

    article_id = 7
    base_auto = [
        {
            "id": 100 + index,
            "server_id": "memphis",
            "article_id": article_id,
            "term": f"unique base term {index}",
            "source": "auto",
            "status": "approved",
            "weight": 1.0,
            "suggested_by": None,
        }
        for index in range(1, 9)
    ]
    additive = {
        "id": 109,
        "server_id": "memphis",
        "article_id": article_id,
        "term": "trusted phase b addition",
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
    }
    article = {
        "id": article_id,
        "server_id": "memphis",
        "law_document_id": 10,
        "source_order": 1,
        "article_ref": "Article 7",
        "kind": "article",
        "code": "CODE",
        "title": None,
        "doc_title": "Code",
        "full_text": "Article 7. Substantive body.",
        "article_hash": "hash-7",
        "enrich_hash": "hash-7",
        "penalty_summary": None,
        "penalty_json": None,
        "title_provenance": None,
        "enrichment_context_hash": "context-7",
        "enrichment_output_hash": None,
    }
    article["enrichment_output_hash"] = (
        rollback.build_enrichment_output_hash(
            server_id="memphis",
            article_id=article_id,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=[*base_auto, additive],
        )
    )
    conn = _RollbackConnection(
        [_rollback_row(109, term=additive["term"])],
        metadata_rows=[article],
        auto_rows=[*base_auto, additive],
    )
    _install_rollback(
        monkeypatch,
        conn,
        use_real_completion=True,
    )

    monkeypatch.setattr(
        completion,
        "build_exact_duplicate_plan",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        completion,
        "build_enrichment_context_digests",
        lambda *_args, **_kwargs: {article_id: "context-7"},
    )
    monkeypatch.setattr(
        completion,
        "_source_title_resolver",
        lambda: (lambda _article_ref, _full_text: None),
    )

    async def load_truth(_queryable, server_id, *, lock_rows=False):
        assert server_id == "memphis"
        return VerifiedLawCatalogTruth(
            server_id=server_id,
            source_documents=(),
            catalog_rows=(dict(conn.metadata_rows[0]),),
            binding=_FULL_SOURCE_CATALOG_BINDING,
        )

    monkeypatch.setattr(
        completion,
        "load_verified_law_catalog_truth",
        load_truth,
    )

    class _Registry:
        def effective_count(self, target_id):
            return len({
                str(row["term"])
                for row in conn.auto_rows
                if (
                    int(row["article_id"]) == int(target_id)
                    and row["status"] == "approved"
                )
            })

    async def load_registry(_queryable, _server_id):
        return _Registry()

    monkeypatch.setattr(
        completion,
        "load_exact_signature_registry",
        load_registry,
    )

    after_apply = await completion.evaluate_law_enrichment_completion(
        conn,
        "memphis",
    )
    assert after_apply.complete is True

    result = await rollback.rollback_run(
        "11111111-1111-1111-1111-111111111111",
        "memphis",
        apply=True,
    )

    assert result["deleted_synonym_ids"] == [109]
    assert result["updated_output_article_ids"] == [article_id]
    assert result["pending_document_ids"] == []
    assert conn.documents == [{
        "id": 10,
        "server_id": "memphis",
        "derived_sync_pending": False,
    }]
    assert conn.metadata_rows[0]["enrichment_output_hash"] == (
        rollback.build_enrichment_output_hash(
            server_id="memphis",
            article_id=article_id,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=base_auto,
        )
    )
    after_rollback = await completion.evaluate_law_enrichment_completion(
        conn,
        "memphis",
    )
    assert after_rollback.complete is True
    assert after_rollback.reasons_by_article == {}


@pytest.mark.asyncio
async def test_rollback_output_cas_failure_rolls_back_delete_and_metadata(
    monkeypatch,
):
    row = _rollback_row(109, term="trusted phase b addition")
    auto_row = {
        "id": 109,
        "server_id": "memphis",
        "article_id": 7,
        "term": row["term"],
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
    }
    article = {
        "id": 7,
        "server_id": "memphis",
        "kind": "article",
        "article_hash": "hash-7",
        "title": None,
        "title_provenance": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": "hash-7",
        "enrichment_context_hash": "context-7",
        "enrichment_output_hash": rollback.build_enrichment_output_hash(
            server_id="memphis",
            article_id=7,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=[auto_row],
        ),
    }
    conn = _RollbackConnection(
        [row],
        metadata_rows=[article],
        auto_rows=[auto_row],
        fail_metadata_cas=True,
    )
    db = _install_rollback(monkeypatch, conn)
    before_metadata = deepcopy(conn.metadata_rows)
    before_auto = deepcopy(conn.auto_rows)

    with pytest.raises(
        RuntimeError,
        match="rollback output metadata CAS failed",
    ):
        await rollback.rollback_run(
            "11111111-1111-1111-1111-111111111111",
            "memphis",
            apply=True,
        )

    assert db.rolled_back == 1
    assert conn.metadata_rows == before_metadata
    assert conn.auto_rows == before_auto


@pytest.mark.asyncio
async def test_degraded_rollback_marks_incomplete_document_pending(
    monkeypatch,
):
    row = _rollback_row(109, term="trusted phase b addition")
    auto_row = {
        "id": 109,
        "server_id": "memphis",
        "article_id": 7,
        "term": row["term"],
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
    }
    article = {
        "id": 7,
        "law_document_id": 10,
        "server_id": "memphis",
        "kind": "article",
        "article_hash": "hash-7",
        "title": None,
        "title_provenance": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": "hash-7",
        "enrichment_context_hash": "context-7",
        "enrichment_output_hash": rollback.build_enrichment_output_hash(
            server_id="memphis",
            article_id=7,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=[auto_row],
        ),
    }
    conn = _RollbackConnection(
        [row],
        metadata_rows=[article],
        auto_rows=[auto_row],
    )
    _install_rollback(
        monkeypatch,
        conn,
        incomplete_document_ids=(10,),
    )

    result = await rollback.rollback_run(
        "11111111-1111-1111-1111-111111111111",
        "memphis",
        apply=True,
    )

    assert result["pending_document_ids"] == [10]
    assert conn.documents[0]["derived_sync_pending"] is True


@pytest.mark.asyncio
async def test_pending_schedule_failure_rolls_back_delete_hash_and_marker(
    monkeypatch,
):
    row = _rollback_row(109, term="trusted phase b addition")
    auto_row = {
        "id": 109,
        "server_id": "memphis",
        "article_id": 7,
        "term": row["term"],
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
    }
    article = {
        "id": 7,
        "law_document_id": 10,
        "server_id": "memphis",
        "kind": "article",
        "article_hash": "hash-7",
        "title": None,
        "title_provenance": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": "hash-7",
        "enrichment_context_hash": "context-7",
        "enrichment_output_hash": rollback.build_enrichment_output_hash(
            server_id="memphis",
            article_id=7,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=[auto_row],
        ),
    }
    conn = _RollbackConnection(
        [row],
        metadata_rows=[article],
        auto_rows=[auto_row],
        fail_pending_update=True,
    )
    db = _install_rollback(
        monkeypatch,
        conn,
        incomplete_document_ids=(10,),
    )
    before_metadata = deepcopy(conn.metadata_rows)
    before_auto = deepcopy(conn.auto_rows)
    before_documents = deepcopy(conn.documents)

    with pytest.raises(
        RuntimeError,
        match="could not durably schedule every incomplete document",
    ):
        await rollback.rollback_run(
            "11111111-1111-1111-1111-111111111111",
            "memphis",
            apply=True,
        )

    assert db.rolled_back == 1
    assert conn.metadata_rows == before_metadata
    assert conn.auto_rows == before_auto
    assert conn.documents == before_documents


@pytest.mark.asyncio
async def test_completion_failure_rolls_back_delete_and_output_hash(
    monkeypatch,
):
    row = _rollback_row(109, term="trusted phase b addition")
    auto_row = {
        "id": 109,
        "server_id": "memphis",
        "article_id": 7,
        "term": row["term"],
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
    }
    article = {
        "id": 7,
        "law_document_id": 10,
        "server_id": "memphis",
        "kind": "article",
        "article_hash": "hash-7",
        "title": None,
        "title_provenance": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": "hash-7",
        "enrichment_context_hash": "context-7",
        "enrichment_output_hash": rollback.build_enrichment_output_hash(
            server_id="memphis",
            article_id=7,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=[auto_row],
        ),
    }
    conn = _RollbackConnection(
        [row],
        metadata_rows=[article],
        auto_rows=[auto_row],
    )
    db = _install_rollback(monkeypatch, conn)

    async def fail_completion(*_args):
        raise RuntimeError("completion unavailable")

    monkeypatch.setattr(
        rollback,
        "_evaluate_affected_completion",
        fail_completion,
    )
    before_metadata = deepcopy(conn.metadata_rows)
    before_auto = deepcopy(conn.auto_rows)
    before_documents = deepcopy(conn.documents)

    with pytest.raises(RuntimeError, match="completion unavailable"):
        await rollback.rollback_run(
            "11111111-1111-1111-1111-111111111111",
            "memphis",
            apply=True,
        )

    assert db.rolled_back == 1
    assert conn.metadata_rows == before_metadata
    assert conn.auto_rows == before_auto
    assert conn.documents == before_documents


@pytest.mark.asyncio
async def test_rollback_matching_hash_still_blocks_foreign_auto_before_delete(
    monkeypatch,
):
    row = _rollback_row(109, term="trusted phase b addition")
    foreign_auto = {
        "id": 109,
        "server_id": "another-server",
        "article_id": 7,
        "term": row["term"],
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
    }
    article = {
        "id": 7,
        "server_id": "memphis",
        "kind": "article",
        "article_hash": "hash-7",
        "title": None,
        "title_provenance": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": "hash-7",
        "enrichment_context_hash": "context-7",
        # Deliberately model a corrupt writer that recomputed the exact
        # semantic hash over the foreign row. Hash equality is not ownership.
        "enrichment_output_hash": rollback.build_enrichment_output_hash(
            server_id="memphis",
            article_id=7,
            title=None,
            title_provenance=None,
            penalty_summary=None,
            penalty_json=None,
            auto_rows=[foreign_auto],
        ),
    }
    conn = _RollbackConnection(
        [row],
        metadata_rows=[article],
        auto_rows=[foreign_auto],
    )
    _install_rollback(monkeypatch, conn)
    before = deepcopy(conn.auto_rows)

    result = await rollback.rollback_run(
        "11111111-1111-1111-1111-111111111111",
        "memphis",
        apply=True,
    )

    assert result["can_apply"] is False
    assert any(
        mismatch.get("reason") == "foreign_auto_ownership"
        for mismatch in result["mismatches"]
    )
    assert result["deleted_synonym_ids"] == []
    assert conn.auto_rows == before
    assert not any(
        "DELETE FROM law_synonyms" in query
        for kind, query, _args in conn.calls
        if kind == "fetch"
    )


@pytest.mark.asyncio
async def test_rollback_stale_output_hash_blocks_before_delete(monkeypatch):
    row = _rollback_row(109, term="trusted phase b addition")
    auto_row = {
        "id": 109,
        "server_id": "memphis",
        "article_id": 7,
        "term": row["term"],
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
    }
    article = {
        "id": 7,
        "server_id": "memphis",
        "kind": "article",
        "article_hash": "hash-7",
        "title": None,
        "title_provenance": None,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": "hash-7",
        "enrichment_context_hash": "context-7",
        "enrichment_output_hash": "0" * 64,
    }
    conn = _RollbackConnection(
        [row],
        metadata_rows=[article],
        auto_rows=[auto_row],
    )
    _install_rollback(monkeypatch, conn)
    before = deepcopy(conn.auto_rows)

    result = await rollback.rollback_run(
        "11111111-1111-1111-1111-111111111111",
        "memphis",
        apply=True,
    )

    assert result["can_apply"] is False
    assert any(
        mismatch.get("reason") == "output_mismatch"
        for mismatch in result["mismatches"]
    )
    assert result["deleted_synonym_ids"] == []
    assert conn.auto_rows == before


def test_migration_024_log_is_append_only_evidence_without_fk_cascade():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "024_add_law_synonym_backfill_log_20260728.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "024_law_synonym_backfill_log"' in migration
    assert 'down_revision: Union[str, None] = "023_law_document_sync_pending"' in migration
    assert '"law_synonym_backfill_inserts"' in migration
    assert '"law_synonym_backfill_runs"' in migration
    assert 'sa.Column("plan_hash"' in migration
    assert 'sa.Column("manifest_hash"' in migration
    assert 'sa.PrimaryKeyConstraint("run_id")' in migration
    assert "ForeignKey(" not in migration
    assert 'sa.Column("source"' in migration
    assert 'sa.Column("status"' in migration
    assert 'sa.Column("weight"' in migration
    assert 'sa.Column("suggested_by"' in migration
    assert 'sa.PrimaryKeyConstraint("run_id", "synonym_id")' in migration
    assert '"law_enrichment_artifact_commits"' in migration
    assert '"generation_input_hash"' in migration
    assert '"artifact_hash"' in migration
    assert '"post_commit_generation_hash"' in migration
    assert '"uq_law_enrich_commit_artifact_outcome"' in migration
    assert "idx_law_synonyms_server_exact_signature" in migration
