from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import export_law_enrichment_batches as exporter
from services import law_enrichment_completion as completion_service
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
)
from services.law_catalog_service import source_article_hash
from services.law_catalog_truth import load_verified_law_catalog_truth
from services.law_synonym_service import SynonymWriteError


_FIRST_TEXT = "Статья 1.1. First\nFirst body"
_SECOND_TEXT = "Статья 1.2. Second\nSecond body"
_SOURCE_CONTENT = f"{_FIRST_TEXT}\n{_SECOND_TEXT}"


class _ReadOnlyConn:
    def __init__(
        self,
        *,
        pending_document: bool = False,
        content_hash: str | None = "source-hash",
        content: str | None = _SOURCE_CONTENT,
        catalog_mutator=None,
    ):
        self.pending_document = pending_document
        self.content_hash = content_hash
        self.content = content
        self.catalog_mutator = catalog_mutator
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "FROM law_documents" in normalized:
            return [
                {
                    "id": 10,
                    "server_id": "las_vegas",
                    "source_thread_id": 100,
                    "title": "Criminal Code",
                    "content": self.content,
                    "content_hash": self.content_hash,
                    "gemini_file_uri": "gs://example/source",
                    "derived_sync_pending": self.pending_document,
                    "last_updated": "2026-07-28T01:00:00+00:00",
                }
            ]
        if "FROM law_articles" in normalized:
            rows = [
                {
                    **row,
                    "server_id": "las_vegas",
                    "parent_server_id": "las_vegas",
                }
                for row in _generation_state()["catalog_rows"]
            ]
            if self.catalog_mutator is not None:
                self.catalog_mutator(rows)
            if "id = ANY($2::int[])" in normalized:
                wanted = set(args[1])
                return [row for row in rows if row["id"] in wanted]
            if "LEFT JOIN law_documents" in normalized:
                return rows
            assert "enrich_hash IS DISTINCT FROM article_hash" in normalized
            return rows
        if "FROM law_synonyms" in normalized:
            wanted = set(args[1])
            rows = [
                {"article_id": 1, "term": "first phrase"},
                {"article_id": 1, "term": "legal first"},
            ]
            return [row for row in rows if row["article_id"] in wanted]
        raise AssertionError(normalized)


def _generation_state(target_ids=(1, 2)):
    first_text = _FIRST_TEXT
    second_text = _SECOND_TEXT
    rows = [
        {
            "id": 1,
            "law_document_id": 10,
            "source_order": 0,
            "article_ref": "Статья 1.1. First",
            "kind": "article",
            "article_hash": source_article_hash(first_text),
            "code": "1.1",
            "title": "First",
            "title_provenance": "source",
            "doc_title": "Criminal Code",
            "full_text": first_text,
            "penalty_summary": None,
            "penalty_json": None,
            "enrich_hash": None,
            "enrichment_context_hash": None,
        },
        {
            "id": 2,
            "law_document_id": 10,
            "source_order": 1,
            "article_ref": "Статья 1.2. Second",
            "kind": "article",
            "article_hash": source_article_hash(second_text),
            "code": "1.2",
            "title": "Second",
            "title_provenance": "source",
            "doc_title": "Criminal Code",
            "full_text": second_text,
            "penalty_summary": "fine",
            "penalty_json": {"fine": 100},
            "enrich_hash": None,
            "enrichment_context_hash": None,
        },
    ]
    return {
        "snapshot": {
            "algorithm": "sha256",
            "required_target_ids": list(target_ids),
            "value": "a" * 64,
        },
        "articles": rows,
        "catalog_rows": rows,
        "auto_rows": [
            {
                "article_id": 1,
                "term": "first phrase",
                "status": "approved",
            },
            {
                "article_id": 1,
                "term": "legal first",
                "status": "approved",
            },
        ],
        "context_hashes": {
            1: "b" * 64,
            2: "c" * 64,
        },
    }


def _install_generation_state(
    monkeypatch,
    *,
    incomplete_ids=(1, 2),
    closure_ids=None,
):
    seen: dict[str, object] = {}

    async def completion(_conn, server_id, **_kwargs):
        seen["completion_server_id"] = server_id
        return SimpleNamespace(
            incomplete_article_ids=tuple(incomplete_ids),
        )

    async def explicit(_conn, _server_id, target_ids, **_kwargs):
        seen["explicit_target_ids"] = list(target_ids)
        if 999 in target_ids:
            raise SynonymWriteError(
                "explicit replacement targets missing/non-article ids: [999]"
            )
        selected = (
            tuple(sorted(set(closure_ids)))
            if closure_ids is not None
            else tuple(sorted(set(target_ids)))
        )
        state = _generation_state(selected)
        truth = await load_verified_law_catalog_truth(
            _conn,
            _server_id,
        )
        state["snapshot"]["source_catalog_binding"] = truth.binding
        return state

    monkeypatch.setattr(
        completion_service,
        "evaluate_law_enrichment_completion",
        completion,
    )
    monkeypatch.setattr(
        exporter,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        explicit,
    )
    return seen


@pytest.mark.asyncio
async def test_export_snapshot_is_read_only_source_bound_and_batched(
    monkeypatch,
):
    seen = _install_generation_state(monkeypatch)
    conn = _ReadOnlyConn()
    payload = await exporter._build_export_snapshot(
        conn,
        "las_vegas",
        batch_size=1,
    )

    manifest = payload["manifest"]
    assert manifest["schema"] == exporter.EXPORT_SCHEMA
    assert manifest["pending_articles"] == 2
    assert manifest["selection_mode"] == "pending_enrichment"
    assert manifest["requested_article_ids"] is None
    assert len(payload["batches"]) == 2
    assert manifest["source_fingerprint"]["documents"] == 1
    assert manifest["source_fingerprint"]["snapshot_version"] == 3
    assert all(query.startswith("SELECT ") for query in conn.queries)
    assert seen == {
        "completion_server_id": "las_vegas",
        "explicit_target_ids": [1, 2],
    }

    first = payload["batches"][0]["articles"][0]
    assert first["id"] == 1
    assert first["article_hash"] == source_article_hash(
        "Статья 1.1. First\nFirst body"
    )
    assert first["generation_context_hash"] == "b" * 64
    assert len(first["generation_input_hash"]) == 64
    assert "generation_input_hash" in first["review"][
        "required_output_fields"
    ]
    assert first["source"]["current_auto_synonyms"] == [
        "first phrase",
        "legal first",
    ]
    assert first["review"]["status"] == "pending_codex_review"
    # Source envelopes cannot be passed directly to the apply CLI as its
    # accepted top-level JSON array.
    assert isinstance(payload["batches"][0], dict)
    assert "articles" in payload["batches"][0]

    manifest_body = dict(manifest)
    manifest_hash = manifest_body.pop("manifest_hash")
    assert manifest_hash == exporter._payload_hash(manifest_body)


@pytest.mark.asyncio
async def test_export_refuses_pending_source_before_reading_catalog():
    conn = _ReadOnlyConn(pending_document=True)
    with pytest.raises(exporter.PhaseAError, match="normal catalog reconcile"):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=15,
        )
    assert len(conn.queries) == 1
    assert "FROM law_documents" in conn.queries[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", (None, "", "   "))
async def test_export_refuses_unsafe_source_content_before_completion(
    monkeypatch,
    content,
):
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("completion/loader must not run")

    monkeypatch.setattr(
        completion_service,
        "evaluate_law_enrichment_completion",
        must_not_run,
    )
    monkeypatch.setattr(
        exporter,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        must_not_run,
    )
    conn = _ReadOnlyConn(content=content)

    with pytest.raises(exporter.PhaseAError, match="normal catalog reconcile"):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=15,
        )

    assert len(conn.queries) == 1
    assert "FROM law_documents" in conn.queries[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "other"),
        ("code", "999"),
        ("title", "Forged stored structural title"),
    ],
)
async def test_export_refuses_stored_parser_drift_before_selection_or_network(
    monkeypatch,
    field,
    value,
):
    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("selection/network boundary must not run")

    monkeypatch.setattr(
        completion_service,
        "evaluate_law_enrichment_completion",
        must_not_run,
    )
    monkeypatch.setattr(
        exporter,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        must_not_run,
    )

    def mutate(rows):
        rows[0][field] = value

    conn = _ReadOnlyConn(catalog_mutator=mutate)
    with pytest.raises(exporter.PhaseAError, match="normal catalog reconcile"):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=15,
        )

    assert len(conn.queries) == 2
    assert "FROM law_documents" in conn.queries[0]
    assert "FROM law_articles" in conn.queries[1]


@pytest.mark.asyncio
async def test_completion_refuses_parser_article_stored_as_other_before_omission():
    def hide_parser_article(rows):
        rows[0]["kind"] = "other"

    conn = _ReadOnlyConn(catalog_mutator=hide_parser_article)
    with pytest.raises(
        completion_service.LawEnrichmentCompletionError,
        match="active parser contract",
    ):
        await completion_service.evaluate_law_enrichment_completion(
            conn,
            "las_vegas",
        )

    assert len(conn.queries) == 2
    assert all("FROM law_synonyms" not in query for query in conn.queries)


@pytest.mark.asyncio
async def test_pending_export_includes_quality_only_incomplete_article(
    monkeypatch,
):
    seen = _install_generation_state(
        monkeypatch,
        incomplete_ids=(2,),
    )
    conn = _ReadOnlyConn()

    payload = await exporter._build_export_snapshot(
        conn,
        "las_vegas",
        batch_size=15,
    )

    assert seen["explicit_target_ids"] == [2]
    assert payload["manifest"]["exported_article_ids"] == [2]
    assert payload["manifest"]["pending_articles"] == 1


@pytest.mark.asyncio
async def test_export_can_select_current_articles_for_reviewed_correction(
    monkeypatch,
):
    seen = _install_generation_state(monkeypatch)
    conn = _ReadOnlyConn()

    payload = await exporter._build_export_snapshot(
        conn,
        "las_vegas",
        batch_size=15,
        article_ids=[2, 2],
    )

    manifest = payload["manifest"]
    assert manifest["selection_mode"] == "explicit_articles"
    assert manifest["requested_article_ids"] == [2]
    assert manifest["pending_articles"] == 1
    assert payload["batches"][0]["articles"][0]["id"] == 2
    assert "completion_server_id" not in seen
    assert all(query.startswith("SELECT ") for query in conn.queries)


@pytest.mark.asyncio
async def test_explicit_correction_export_refuses_wrong_server_or_kind_ids(
    monkeypatch,
):
    _install_generation_state(monkeypatch)
    conn = _ReadOnlyConn()

    with pytest.raises(
        exporter.PhaseAError,
        match="missing/non-article",
    ):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=15,
            article_ids=[999],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [2.0, "2", True])
async def test_explicit_export_rejects_article_id_aliases(
    monkeypatch,
    alias,
):
    seen = _install_generation_state(monkeypatch)
    conn = _ReadOnlyConn()

    with pytest.raises(exporter.PhaseAError, match="positive integers"):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=15,
            article_ids=[alias],
        )

    assert "explicit_target_ids" not in seen


@pytest.mark.asyncio
async def test_export_keeps_exact_body_group_in_one_batch(monkeypatch):
    shared_body = "The same operative rule applies to this situation."
    chapter_a = {
        "id": 101,
        "law_document_id": 10,
        "source_order": 0,
        "article_ref": "Глава 1. Scope A",
        "kind": "chapter",
        "article_hash": source_article_hash("Глава 1. Scope A"),
        "code": "1",
        "title": "Scope A",
        "title_provenance": None,
        "doc_title": "Criminal Code",
        "full_text": "Глава 1. Scope A",
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": None,
        "enrichment_context_hash": None,
    }
    article_a = {
        **_generation_state((1,))["articles"][0],
        "source_order": 1,
        "article_ref": "Статья 1",
        "article_hash": source_article_hash(
            f"Статья 1\n{shared_body}"
        ),
        "title": None,
        "title_provenance": None,
        "full_text": f"Статья 1\n{shared_body}",
    }
    chapter_b = {
        **chapter_a,
        "id": 102,
        "source_order": 2,
        "article_ref": "Глава 2. Scope B",
        "article_hash": source_article_hash("Глава 2. Scope B"),
        "code": "2",
        "title": "Scope B",
        "full_text": "Глава 2. Scope B",
    }
    article_b = {
        **_generation_state((2,))["articles"][1],
        "source_order": 3,
        "article_ref": "Статья 2",
        "article_hash": source_article_hash(
            f"Статья 2\n{shared_body}"
        ),
        "title": None,
        "title_provenance": None,
        "full_text": f"Статья 2\n{shared_body}",
    }
    state = {
        "snapshot": {
            "algorithm": "sha256",
            "required_target_ids": [1, 2],
            "value": "a" * 64,
        },
        "articles": [article_a, article_b],
        "catalog_rows": [
            chapter_a,
            article_a,
            chapter_b,
            article_b,
        ],
        "auto_rows": [],
        "context_hashes": {1: "b" * 64, 2: "c" * 64},
    }

    async def explicit(_conn, _server_id, _target_ids, **_kwargs):
        truth = await load_verified_law_catalog_truth(
            _conn,
            _server_id,
        )
        state["snapshot"]["source_catalog_binding"] = truth.binding
        return state

    monkeypatch.setattr(
        exporter,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        explicit,
    )
    conn = _ReadOnlyConn()

    payload = await exporter._build_export_snapshot(
        conn,
        "las_vegas",
        batch_size=2,
        article_ids=[1, 2],
    )

    assert len(payload["batches"]) == 1
    assert [
        article["id"]
        for article in payload["batches"][0]["articles"]
    ] == [1, 2]
    assert payload["batches"][0][
        "review_source_projection"
    ]["exact_groups"]

    with pytest.raises(
        exporter.PhaseAError,
        match="increase the batch size instead of splitting",
    ):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=1,
            article_ids=[1, 2],
        )


def test_write_export_is_exclusive_and_preserves_batch_hashes(tmp_path):
    batch_body = {
        "schema": exporter.BATCH_SCHEMA,
        "server_id": "las_vegas",
        "source_fingerprint": {"value": "source"},
        "batch_number": 1,
        "articles": [],
    }
    batch = exporter._with_hash(batch_body, "batch_hash")
    manifest_body = {
        "schema": exporter.EXPORT_SCHEMA,
        "server_id": "las_vegas",
        "pending_articles": 0,
        "batches": [
            {
                "file": "source-batch-00001.json",
                "batch_hash": batch["batch_hash"],
                "articles": 0,
                "article_ids": [],
            }
        ],
    }
    payload = {
        "manifest": exporter._with_hash(manifest_body, "manifest_hash"),
        "batches": [batch],
    }
    output = tmp_path / "export"

    manifest_path = exporter.write_export(output, payload)
    assert manifest_path == output / "manifest.json"
    written_batch = json.loads(
        (output / "source-batch-00001.json").read_text(encoding="utf-8")
    )
    assert written_batch["batch_hash"] == batch["batch_hash"]
    with pytest.raises(FileExistsError):
        exporter.write_export(output, payload)


def test_exporter_contains_no_model_api_path():
    source = Path(exporter.__file__).read_text(encoding="utf-8")
    forbidden = (
        "google.genai",
        "execute_vertex_request",
        "get_current_client",
        "init_client",
        "generate_content",
    )
    assert all(token not in source for token in forbidden)
    assert 'isolation="repeatable_read"' in source
    assert "readonly=True" in source


def test_generation_hash_normalizes_asyncpg_json_text_and_decoded_object():
    base = {
        "id": 1,
        "article_hash": "hash-1",
        "law_document_id": 10,
        "source_order": 0,
        "code": "1",
        "title": "Title",
        "doc_title": "Code",
        "full_text": "Text",
        "penalty_summary": "fine",
    }
    as_text = {**base, "penalty_json": ' {"fine":"100"} '}
    as_object = {**base, "penalty_json": {"fine": "100"}}

    assert enrichment_generation_input_hash(
        as_text, ["term"]
    ) == enrichment_generation_input_hash(as_object, ["term"])
