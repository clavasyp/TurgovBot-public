"""Safety contracts for deterministic Codex-reviewed Phase B artifacts."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import _apply_claude_enrich as full_enrich
from scripts import backfill_law_synonyms as backfill
from scripts import rollback_law_synonym_backfill as rollback
from services import law_synonym_service as synonym_service
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
)
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
        "law_document_id": 100 + article_id,
        "source_order": article_id,
        "article_hash": article_hash,
        "code": str(article_id),
        "title": f"Article {article_id}",
        "doc_title": "Code",
        "full_text": f"Substantive text {article_id}",
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": None,
    }
    row.update(updates)
    return row


class _FullConn:
    def __init__(
        self,
        rows,
        *,
        pending=False,
        auto_synonyms=(),
        artifact_commits=(),
    ):
        self.rows = rows
        self.pending = pending
        self.auto_synonyms = list(auto_synonyms)
        self.artifact_commits = list(artifact_commits)
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


def _install_full_locks(monkeypatch):
    @asynccontextmanager
    async def global_lock():
        yield

    @asynccontextmanager
    async def server_lock(_server_id):
        yield

    monkeypatch.setattr(full_enrich, "law_backfill_global_lock", global_lock)
    monkeypatch.setattr(full_enrich, "law_document_server_lock", server_lock)
    monkeypatch.setattr(full_enrich, "catalog_server_lock", server_lock)


def _full_item(
    article_id: int,
    article_hash: str,
    *,
    generation_article: dict | None = None,
) -> dict:
    generation_article = generation_article or _full_article_row(
        article_id, article_hash
    )
    return {
        "id": article_id,
        "article_hash": article_hash,
        "title": "Precise title",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": ["precise phrase", "another precise phrase"],
        "generation_input_hash": enrichment_generation_input_hash(
            generation_article, ()
        ),
    }


@pytest.mark.asyncio
async def test_full_enrich_rejects_missing_generation_hash_before_any_write(monkeypatch):
    _install_full_locks(monkeypatch)
    db = _FullDb(_FullConn([]))
    monkeypatch.setattr(full_enrich, "db_manager", db)

    with pytest.raises(SynonymWriteError, match="generation-time"):
        item = _full_item(1, "")
        await full_enrich.apply_items(
            "memphis", [item]
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
    mutate(item)

    with pytest.raises(SynonymWriteError):
        await full_enrich.apply_items("memphis", [item])
    assert events == []
    assert db.transactions == 0
    assert db.conn.calls == []


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

    monkeypatch.setattr(full_enrich, "_write_enrichment_in_transaction", write)

    with pytest.raises(SynonymWriteError, match="unfinished derived"):
        await full_enrich.apply_items(
            "memphis", [_full_item(1, "hash-1")]
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

    async def registry(_conn, _server_id):
        return object()

    writes = []

    async def write(conn_arg, _server_id, article_id, *_args, **kwargs):
        writes.append((conn_arg, article_id))
        assert kwargs["minimum_synonym_physical"] == 12
        assert kwargs["minimum_synonym_effective"] == 12
        if article_id == 2:
            raise SynonymWriteError("quality")
        return True

    monkeypatch.setattr(full_enrich, "load_exact_signature_registry", registry)
    monkeypatch.setattr(full_enrich, "_write_enrichment_in_transaction", write)

    with pytest.raises(SynonymWriteError, match="quality"):
        await full_enrich.apply_items(
            "memphis",
            [
                _full_item(1, "hash-1"),
                _full_item(2, "hash-2"),
            ],
        )
    assert db.transactions == 1
    assert db.rolled_back == 1
    assert writes == [(conn, 1), (conn, 2)]
    assert conn.artifact_commits == []


@pytest.mark.asyncio
async def test_full_enrich_current_correction_keeps_shared_additive_floor(
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

    async def registry(_conn, _server_id):
        return object()

    seen = []

    async def write(
        _conn,
        _server_id,
        _article_id,
        *_args,
        **kwargs,
    ):
        seen.append((
            kwargs["minimum_synonym_physical"],
            kwargs["minimum_synonym_effective"],
        ))
        return True

    monkeypatch.setattr(
        full_enrich,
        "load_exact_signature_registry",
        registry,
    )
    monkeypatch.setattr(
        full_enrich,
        "_write_enrichment_in_transaction",
        write,
    )
    item = _full_item(1, "hash-1", generation_article=current)
    item["generation_input_hash"] = enrichment_generation_input_hash(
        current,
        existing_terms,
    )

    result = await full_enrich.apply_items("memphis", [item])

    assert result["written"] == 1
    assert result["already_applied"] == 0
    assert seen == [(
        synonym_service.MIN_REPLACEMENT_PHYSICAL,
        synonym_service.MIN_REPLACEMENT_EFFECTIVE,
    )]
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

    monkeypatch.setattr(full_enrich, "_write_enrichment_in_transaction", write)

    with pytest.raises(SynonymWriteError, match="generation input changed"):
        await full_enrich.apply_items(
            "memphis",
            [_full_item(1, "hash-1", generation_article=exported)],
        )
    assert db.rolled_back == 1
    assert writes == []


@pytest.mark.asyncio
async def test_full_enrich_partial_collision_commit_replay_is_idempotent(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    exported = _full_article_row(1, "hash-1")
    item = _full_item(1, "hash-1", generation_article=exported)
    item["penalty_summary"] = "fine"
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
        penalty_summary="fine",
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
            "article_hash": "hash-1",
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

    result = await full_enrich.apply_items("memphis", [item])

    assert result["written"] == 0
    assert result["already_applied"] == 1
    assert not any(
        "UPDATE law_articles" in query
        or "INSERT INTO law_synonyms" in query
        for _kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_full_enrich_rejects_partial_collision_without_commit_evidence(
    monkeypatch,
):
    _install_full_locks(monkeypatch)
    exported = _full_article_row(1, "hash-1")
    item = _full_item(1, "hash-1", generation_article=exported)
    item["penalty_summary"] = "fine"
    item["penalty"] = {"fine": "$100"}
    expanded = sorted(
        {
            term
            for terms in synonym_service.expand_synonym_terms(
                item["synonyms"]
            ).values()
            for term in terms
        }
    )
    accepted_terms = expanded[:-1]
    conn = _FullConn([dict(exported)])
    db = _FullDb(conn)
    monkeypatch.setattr(full_enrich, "db_manager", db)

    async def registry(_conn, _server_id):
        return object()

    write_calls = []

    async def write(
        conn_arg,
        _server_id,
        article_id,
        summary,
        penalty,
        _synonyms,
        stats,
        **_kwargs,
    ):
        write_calls.append(article_id)
        conn_arg.rows[0]["penalty_summary"] = summary
        conn_arg.rows[0]["penalty_json"] = json.dumps(penalty)
        conn_arg.rows[0]["enrich_hash"] = "hash-1"
        conn_arg.auto_synonyms = [
            {"article_id": article_id, "term": term}
            for term in accepted_terms
        ]
        stats["synonyms"] += len(accepted_terms)
        stats["synonym_collisions_rejected"] = 1
        return True

    monkeypatch.setattr(
        full_enrich,
        "load_exact_signature_registry",
        registry,
    )
    monkeypatch.setattr(
        full_enrich,
        "_write_enrichment_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="exact-leg signature collisions",
    ):
        await full_enrich.apply_items("memphis", [item])

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
    item["penalty_summary"] = "fine"
    item["penalty"] = {"fine": "$100"}
    accepted_terms = ["another precise phrase"]
    committed = _full_article_row(
        1,
        "hash-1",
        penalty_summary="fine",
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
            "article_hash": "hash-1",
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
        "_write_enrichment_in_transaction",
        write,
    )

    with pytest.raises(
        SynonymWriteError,
        match="generation input changed",
    ):
        await full_enrich.apply_items("memphis", [item])
    assert writes == []
    assert db.rolled_back == 1


class _RollbackConnection:
    def __init__(self, rows, *, deleted=None):
        self.rows = rows
        self.deleted = deleted
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "FROM law_synonym_backfill_inserts" in query:
            return self.rows
        if "DELETE FROM law_synonyms" in query:
            ids = list(args[2])
            returned = ids if self.deleted is None else self.deleted
            return [{"id": synonym_id} for synonym_id in returned]
        raise AssertionError(query)


class _RollbackDb:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def transaction(self):
        yield self.conn


def _install_rollback(monkeypatch, conn):
    monkeypatch.setattr(rollback, "db_manager", _RollbackDb(conn))

    @asynccontextmanager
    async def lock(_server_id):
        yield

    monkeypatch.setattr(rollback, "law_document_server_lock", lock)
    monkeypatch.setattr(rollback, "catalog_server_lock", lock)


def _rollback_row(
    synonym_id: int,
    *,
    missing: bool = False,
    changed_field: str | None = None,
) -> dict:
    row = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "synonym_id": synonym_id,
        "server_id": "memphis",
        "article_id": 7,
        "term": "precise phrase",
        "article_hash": "hash-7",
        "source": "auto",
        "status": "approved",
        "weight": 1.0,
        "suggested_by": None,
        "current_id": None if missing else synonym_id,
        "current_server_id": None if missing else "memphis",
        "current_article_id": None if missing else 7,
        "current_term": None if missing else "precise phrase",
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
