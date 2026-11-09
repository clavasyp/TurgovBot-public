"""Durable retry semantics for law_documents derived indexes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

import services.indexing_service as indexing
import services.law_catalog_service as catalog


class _CasConnection:
    def __init__(self, *, cas_matches: bool = True):
        self.cas_matches = cas_matches
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        if not self.cas_matches:
            return None
        return args[0]


class _DerivedDb:
    def __init__(
        self,
        docs,
        *,
        has_rag: bool,
        has_catalog: bool,
        cas_matches: bool = True,
    ):
        self.docs = docs
        self.has_rag = has_rag
        self.has_catalog = has_catalog
        self.conn = _CasConnection(cas_matches=cas_matches)
        self.fetch_calls = []
        self.fetchval_calls = []
        self.transaction_entries = 0

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return self.docs

    async def fetchval(self, query, *args):
        self.fetchval_calls.append((query, args))
        if "FROM law_chunks" in query:
            return self.has_rag
        if "FROM law_articles" in query:
            return self.has_catalog
        raise AssertionError(f"unexpected fetchval: {query}")

    @asynccontextmanager
    async def transaction(self):
        self.transaction_entries += 1
        yield self.conn


def _pending_doc(doc_id: int = 10, thread_id: int = 100):
    return {
        "id": doc_id,
        "server_id": "memphis",
        "source_thread_id": thread_id,
        "title": "Criminal code",
        "content": "Article 1. Long enough source text for indexing.",
        "content_hash": "content-hash",
        "last_updated": datetime(2026, 7, 26, tzinfo=timezone.utc),
        "derived_sync_pending": True,
    }


def _set_flags(monkeypatch, *, rag: bool = True, catalog_sync: bool = True):
    monkeypatch.setattr(indexing.config, "LAW_INDEX_AUTO_REINDEX", rag)
    monkeypatch.setattr(indexing.config, "LAW_CATALOG_AUTO_SYNC", catalog_sync)


def _set_catalog_result(monkeypatch, *, succeeded=(), failed=()):
    async def sync_catalog_for_documents(_server_id, _doc_ids):
        return {
            "documents": len(succeeded),
            "articles": len(succeeded),
            "succeeded_document_ids": list(succeeded),
            "failed_document_ids": list(failed),
            "failed_documents": len(failed),
        }

    monkeypatch.setattr(
        catalog,
        "sync_catalog_for_documents",
        sync_catalog_for_documents,
    )


@pytest.mark.asyncio
async def test_successful_rag_and_catalog_clear_pending_with_version_cas(monkeypatch):
    doc = _pending_doc()
    db = _DerivedDb([doc], has_rag=True, has_catalog=True)
    monkeypatch.setattr(indexing, "db_manager", db)
    _set_flags(monkeypatch)

    async def index_document(_doc):
        return 2

    monkeypatch.setattr(indexing, "index_document", index_document)
    _set_catalog_result(monkeypatch, succeeded=(doc["id"],))

    stats = await indexing.reindex_changed_documents("memphis", [doc["source_thread_id"]])

    assert stats == {
        "documents": 1,
        "chunks": 2,
        "skipped": 0,
        "pending_cleared": 1,
        "failed": 0,
    }
    assert db.transaction_entries == 1
    assert len(db.conn.calls) == 1
    query, args = db.conn.calls[0]
    assert "derived_sync_pending = FALSE" in query
    assert "last_updated IS NOT DISTINCT FROM $3" in query
    assert "RETURNING id" in query
    assert args == (doc["id"], "memphis", doc["last_updated"])


@pytest.mark.asyncio
@pytest.mark.parametrize("rag_result", [0, RuntimeError("embedding failed")])
async def test_rag_failure_or_empty_result_keeps_pending(
    monkeypatch,
    rag_result,
):
    doc = _pending_doc()
    db = _DerivedDb([doc], has_rag=True, has_catalog=True)
    monkeypatch.setattr(indexing, "db_manager", db)
    _set_flags(monkeypatch)

    async def index_document(_doc):
        if isinstance(rag_result, Exception):
            raise rag_result
        return rag_result

    monkeypatch.setattr(indexing, "index_document", index_document)
    _set_catalog_result(monkeypatch, succeeded=(doc["id"],))

    stats = await indexing.reindex_changed_documents("memphis", [doc["source_thread_id"]])

    assert stats["pending_cleared"] == 0
    assert stats["failed"] == 1
    assert db.transaction_entries == 0
    assert db.fetch_calls
    assert "last_updated" in db.fetch_calls[0][0]


@pytest.mark.asyncio
async def test_catalog_document_failure_keeps_pending_after_rag_success(monkeypatch):
    doc = _pending_doc()
    db = _DerivedDb([doc], has_rag=True, has_catalog=True)
    monkeypatch.setattr(indexing, "db_manager", db)
    _set_flags(monkeypatch)

    async def index_document(_doc):
        return 3

    monkeypatch.setattr(indexing, "index_document", index_document)
    _set_catalog_result(monkeypatch, failed=(doc["id"],))

    stats = await indexing.reindex_changed_documents("memphis", [doc["source_thread_id"]])

    assert stats["documents"] == 1
    assert stats["chunks"] == 3
    assert stats["pending_cleared"] == 0
    assert stats["failed"] == 1
    assert db.transaction_entries == 0


@pytest.mark.asyncio
async def test_catalog_sync_runs_and_clears_when_server_has_no_rag_baseline(monkeypatch):
    doc = _pending_doc()
    db = _DerivedDb([doc], has_rag=False, has_catalog=True)
    monkeypatch.setattr(indexing, "db_manager", db)
    _set_flags(monkeypatch)

    async def index_document(_doc):
        raise AssertionError("RAG must not run without a baseline")

    monkeypatch.setattr(indexing, "index_document", index_document)
    _set_catalog_result(monkeypatch, succeeded=(doc["id"],))

    stats = await indexing.reindex_changed_documents("memphis", [doc["source_thread_id"]])

    assert stats["documents"] == 0
    assert stats["chunks"] == 0
    assert stats["skipped"] == 1
    assert stats["pending_cleared"] == 1
    assert stats["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("has_rag", "has_catalog", "rag_enabled", "catalog_enabled"),
    [
        (True, False, False, True),
        (False, True, True, False),
    ],
    ids=["rag-kill-switch", "catalog-kill-switch"],
)
async def test_required_path_kill_switch_keeps_pending(
    monkeypatch,
    has_rag,
    has_catalog,
    rag_enabled,
    catalog_enabled,
):
    doc = _pending_doc()
    db = _DerivedDb(
        [doc],
        has_rag=has_rag,
        has_catalog=has_catalog,
    )
    monkeypatch.setattr(indexing, "db_manager", db)
    _set_flags(
        monkeypatch,
        rag=rag_enabled,
        catalog_sync=catalog_enabled,
    )

    async def index_document(_doc):
        raise AssertionError("disabled/nonexistent RAG path must not run")

    monkeypatch.setattr(indexing, "index_document", index_document)
    _set_catalog_result(monkeypatch, succeeded=(doc["id"],))

    stats = await indexing.reindex_changed_documents("memphis", [doc["source_thread_id"]])

    assert stats["pending_cleared"] == 0
    assert stats["failed"] == 1
    assert db.transaction_entries == 0
    assert db.fetch_calls
    assert "derived_sync_pending" in db.fetch_calls[0][0]


@pytest.mark.asyncio
async def test_cas_miss_from_concurrent_newer_save_keeps_pending(monkeypatch):
    doc = _pending_doc()
    db = _DerivedDb(
        [doc],
        has_rag=True,
        has_catalog=True,
        cas_matches=False,
    )
    monkeypatch.setattr(indexing, "db_manager", db)
    _set_flags(monkeypatch)

    async def index_document(_doc):
        return 1

    monkeypatch.setattr(indexing, "index_document", index_document)
    _set_catalog_result(monkeypatch, succeeded=(doc["id"],))

    stats = await indexing.reindex_changed_documents("memphis", [doc["source_thread_id"]])

    assert stats["pending_cleared"] == 0
    assert stats["failed"] == 1
    assert db.transaction_entries == 1
    assert len(db.conn.calls) == 1


@pytest.mark.asyncio
async def test_unexpected_catalog_exception_happens_before_any_pending_clear(monkeypatch):
    doc = _pending_doc()
    db = _DerivedDb([doc], has_rag=True, has_catalog=True)
    monkeypatch.setattr(indexing, "db_manager", db)
    _set_flags(monkeypatch)

    async def index_document(_doc):
        return 1

    async def broken_catalog_sync(_server_id, _doc_ids):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(indexing, "index_document", index_document)
    monkeypatch.setattr(
        catalog,
        "sync_catalog_for_documents",
        broken_catalog_sync,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await indexing.reindex_changed_documents(
            "memphis",
            [doc["source_thread_id"]],
        )

    assert db.transaction_entries == 0
    assert db.conn.calls == []


class _CatalogDb:
    def __init__(self, docs, *, incomplete_enrichment_ids=()):
        self.docs = docs
        self.incomplete_enrichment_ids = set(incomplete_enrichment_ids)

    async def fetchval(self, query, *_args):
        if "FROM law_articles" in query:
            return True
        if "law_embedding_jobs" in query:
            return 0
        raise AssertionError(f"unexpected fetchval: {query}")

    async def fetch(self, query, *_args):
        if "enrich_hash IS DISTINCT FROM a.article_hash" in query:
            return [
                {"id": doc_id}
                for doc_id in sorted(self.incomplete_enrichment_ids)
            ]
        if "FROM law_documents" in query:
            return self.docs
        raise AssertionError(f"unexpected fetch: {query}")


@pytest.mark.asyncio
async def test_catalog_summary_classifies_each_requested_document(monkeypatch):
    docs = [
        {"id": 10, "server_id": "memphis", "title": "A", "content": "A"},
        {"id": 20, "server_id": "memphis", "title": "B", "content": "B"},
    ]
    monkeypatch.setattr(catalog, "db_manager", _CatalogDb(docs))
    monkeypatch.setattr(catalog.config, "LAW_CATALOG_AUTO_SYNC", True)
    monkeypatch.setattr(catalog.config, "LAW_CATALOG_AUTO_ENRICH", False)

    async def build_catalog_for_document(doc):
        if doc["id"] == 20:
            raise RuntimeError("bad document")
        return 4

    async def queued_related_spaces(_server_id):
        return []

    monkeypatch.setattr(
        catalog,
        "build_catalog_for_document",
        build_catalog_for_document,
    )
    monkeypatch.setattr(
        catalog,
        "_queued_related_spaces",
        queued_related_spaces,
    )
    monkeypatch.setattr(catalog, "active_space_id", lambda: "frida-v1")

    summary = await catalog._sync_catalog_for_documents(
        "memphis",
        [10, 20, 30],
    )

    assert summary["documents"] == 2
    assert summary["articles"] == 4
    assert summary["succeeded_document_ids"] == [10]
    assert summary["failed_document_ids"] == [30, 20]
    assert summary["failed_documents"] == 2


async def _run_catalog_enrichment_summary(
    monkeypatch,
    *,
    incomplete=(),
    enrich_result=None,
    enrich_error: Exception | None = None,
):
    docs = [
        {
            "id": 10,
            "server_id": "memphis",
            "title": "A",
            "content": "A",
        }
    ]
    monkeypatch.setattr(
        catalog,
        "db_manager",
        _CatalogDb(
            docs,
            incomplete_enrichment_ids=incomplete,
        ),
    )
    monkeypatch.setattr(catalog.config, "LAW_CATALOG_AUTO_SYNC", True)
    monkeypatch.setattr(catalog.config, "LAW_CATALOG_AUTO_ENRICH", True)

    async def build_catalog_for_document(_doc):
        return 1

    async def enrich_articles_for_server(_server_id, *, only_stale):
        assert only_stale is True
        if enrich_error is not None:
            raise enrich_error
        return enrich_result or {
            "articles": 1,
            "synonyms": 8,
            "batches_failed": 0,
        }

    async def queued_related_spaces(_server_id):
        return []

    monkeypatch.setattr(
        catalog,
        "build_catalog_for_document",
        build_catalog_for_document,
    )
    monkeypatch.setattr(
        catalog,
        "enrich_articles_for_server",
        enrich_articles_for_server,
    )
    monkeypatch.setattr(
        catalog,
        "_queued_related_spaces",
        queued_related_spaces,
    )
    monkeypatch.setattr(catalog, "active_space_id", lambda: "frida-v1")
    return await catalog._sync_catalog_for_documents(
        "memphis",
        [10],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enrich_result",
    (
        {"articles": 0, "synonyms": 0, "batches_failed": 1},
        {"articles": 0, "synonyms": 0, "batches_failed": 0},
    ),
    ids=["bad-response", "quality-collision"],
)
async def test_incomplete_enrichment_keeps_document_pending(
    monkeypatch,
    enrich_result,
):
    summary = await _run_catalog_enrichment_summary(
        monkeypatch,
        incomplete=(10,),
        enrich_result=enrich_result,
    )

    assert summary["succeeded_document_ids"] == []
    assert summary["failed_document_ids"] == [10]
    assert summary["failed_documents"] == 1


@pytest.mark.asyncio
async def test_enrichment_exception_keeps_document_pending_even_if_probe_is_clean(
    monkeypatch,
):
    summary = await _run_catalog_enrichment_summary(
        monkeypatch,
        incomplete=(),
        enrich_error=RuntimeError("model unavailable"),
    )

    assert summary["succeeded_document_ids"] == []
    assert summary["failed_document_ids"] == [10]


@pytest.mark.asyncio
async def test_full_enrichment_marks_catalog_document_successful(monkeypatch):
    summary = await _run_catalog_enrichment_summary(
        monkeypatch,
        incomplete=(),
    )

    assert summary["succeeded_document_ids"] == [10]
    assert summary["failed_document_ids"] == []
