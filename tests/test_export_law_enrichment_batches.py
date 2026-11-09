from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import export_law_enrichment_batches as exporter
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
)


class _ReadOnlyConn:
    def __init__(self, *, pending_document: bool = False):
        self.pending_document = pending_document
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
                    "content_hash": "source-hash",
                    "gemini_file_uri": "gs://example/source",
                    "derived_sync_pending": self.pending_document,
                    "last_updated": "2026-07-28T01:00:00+00:00",
                }
            ]
        if "FROM law_articles" in normalized:
            rows = [
                {
                    "id": 1,
                    "law_document_id": 10,
                    "source_order": 0,
                    "article_hash": "hash-1",
                    "code": "1.1",
                    "title": "First",
                    "doc_title": "Criminal Code",
                    "full_text": "Article 1.1\nFirst body",
                    "penalty_summary": None,
                    "penalty_json": None,
                },
                {
                    "id": 2,
                    "law_document_id": 10,
                    "source_order": 1,
                    "article_hash": "hash-2",
                    "code": "1.2",
                    "title": "Second",
                    "doc_title": "Criminal Code",
                    "full_text": "Article 1.2\nSecond body",
                    "penalty_summary": "fine",
                    "penalty_json": {"fine": 100},
                },
            ]
            if "id = ANY($2::int[])" in normalized:
                wanted = set(args[1])
                return [row for row in rows if row["id"] in wanted]
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


@pytest.mark.asyncio
async def test_export_snapshot_is_read_only_source_bound_and_batched():
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
    assert all(query.startswith("SELECT ") for query in conn.queries)

    first = payload["batches"][0]["articles"][0]
    assert first["id"] == 1
    assert first["article_hash"] == "hash-1"
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
    with pytest.raises(exporter.PhaseAError, match="still pending"):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=15,
        )
    assert len(conn.queries) == 1
    assert "FROM law_documents" in conn.queries[0]


@pytest.mark.asyncio
async def test_export_can_select_current_articles_for_reviewed_correction():
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
    assert "id = ANY($2::int[])" in conn.queries[1]


@pytest.mark.asyncio
async def test_explicit_correction_export_refuses_wrong_server_or_kind_ids():
    conn = _ReadOnlyConn()

    with pytest.raises(exporter.PhaseAError, match="do not belong"):
        await exporter._build_export_snapshot(
            conn,
            "las_vegas",
            batch_size=15,
            article_ids=[999],
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
