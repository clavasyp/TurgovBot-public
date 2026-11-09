from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from services import law_enrichment_completion as completion
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LAW_SOURCE_CATALOG_BINDING_SCHEMA,
    VerifiedLawCatalogTruth,
)
from services.law_enrichment_output import build_enrichment_output_hash


def _article(
    article_id: int,
    *,
    document_id: int = 10,
    article_hash: str | None = None,
    enrich_hash: str | None = None,
    context_hash: str | None = None,
    title: str | None = None,
    provenance: str | None = None,
    full_text: str | None = None,
    penalty_summary=None,
    penalty_json=None,
):
    current_hash = article_hash or f"hash-{article_id}"
    return {
        "id": article_id,
        "law_document_id": document_id,
        "source_order": article_id,
        "article_ref": f"Article {article_id}",
        "kind": "article",
        "code": "CODE",
        "title": title,
        "doc_title": "Code",
        "full_text": full_text or f"Article {article_id}. Substantive body.",
        "article_hash": current_hash,
        "enrich_hash": current_hash if enrich_hash is None else enrich_hash,
        "penalty_summary": penalty_summary,
        "penalty_json": penalty_json,
        "title_provenance": provenance,
        "enrichment_context_hash": (
            f"context-{article_id}"
            if context_hash is None
            else context_hash
        ),
        "enrichment_output_hash": f"output-{article_id}",
    }


class _Queryable:
    def __init__(self, articles, approved):
        self.articles = list(articles)
        self.approved = list(approved)
        self.auto = [
            {
                "server_id": "server",
                "article_id": int(row["article_id"]),
                "term": str(row["term"]),
                "weight": 1.0,
                "source": "auto",
                "status": "approved",
                "suggested_by": None,
            }
            for row in approved
        ]
        self.queries = []

    async def fetch(self, query, *_args):
        self.queries.append(query)
        if "FROM law_articles" in query and "law_synonyms" not in query:
            return self.articles
        if "s.source = 'auto'" in query:
            return self.auto
        if "SELECT s.article_id, s.term" in query:
            return self.approved
        raise AssertionError(f"unexpected query: {query}")


@pytest.fixture(autouse=True)
def _verified_catalog_boundary_for_completion_units(monkeypatch):
    async def load(queryable, server_id, **_kwargs):
        rows = [
            {
                **dict(row),
                "server_id": server_id,
                "parent_server_id": server_id,
            }
            for row in queryable.articles
        ]
        fingerprint = {
            "algorithm": "sha256",
            "snapshot_version": 3,
            "documents": 1,
            "value": "a" * 64,
        }
        return VerifiedLawCatalogTruth(
            server_id=server_id,
            source_documents=({
                "id": 10,
                "server_id": server_id,
                "source_thread_id": 1,
                "title": "Code",
                "content": "synthetic completion source",
                "derived_sync_pending": False,
            },),
            catalog_rows=tuple(rows),
            binding={
                "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
                "parser_contract_version": (
                    LAW_CATALOG_PARSER_CONTRACT_VERSION
                ),
                "server_id": server_id,
                "source_fingerprint": fingerprint,
                "documents": 1,
                "catalog_rows": len(rows),
                "article_rows": sum(
                    row["kind"] == "article" for row in rows
                ),
                "tombstone_rows": 0,
                "source_projection_hash": "b" * 64,
                "catalog_projection_hash": "c" * 64,
                "value": "d" * 64,
            },
        )

    monkeypatch.setattr(
        completion,
        "load_verified_law_catalog_truth",
        load,
    )


@dataclass
class _Registry:
    counts: dict[int, int]

    def effective_count(self, article_id: int) -> int:
        return self.counts.get(article_id, 0)


def _terms(article_id: int, count: int = 8):
    return [
        {"article_id": article_id, "term": f"unique term {article_id}-{index}"}
        for index in range(count)
    ]


def _install_context(monkeypatch, article_ids, *, effective=None):
    monkeypatch.setattr(
        completion,
        "build_exact_duplicate_plan",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        completion,
        "build_enrichment_context_digests",
        lambda *_args, **_kwargs: {
            article_id: f"context-{article_id}"
            for article_id in article_ids
        },
    )
    monkeypatch.setattr(
        completion,
        "_source_title_resolver",
        lambda: (lambda _article_ref, _full_text: None),
    )
    monkeypatch.setattr(
        completion,
        "build_enrichment_output_hash",
        lambda **values: f"output-{values['article_id']}",
    )

    async def load_registry(_queryable, _server_id):
        return _Registry(
            effective
            or {article_id: 8 for article_id in article_ids}
        )

    monkeypatch.setattr(
        completion,
        "load_exact_signature_registry",
        load_registry,
    )


@pytest.mark.asyncio
async def test_complete_requires_current_hash_context_provenance_and_8_by_8(
    monkeypatch,
):
    _install_context(monkeypatch, [1])
    conn = _Queryable(
        [_article(1)],
        _terms(1),
    )

    state = await completion.evaluate_law_enrichment_completion(
        conn,
        "server",
    )

    assert state.complete is True
    assert state.articles == state.enriched == 1
    assert state.incomplete_article_ids == ()
    assert state.article_states[0].physical_synonyms == 8
    assert state.article_states[0].effective_synonyms == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        ({"enrich_hash": "old"}, "article_hash_stale"),
        ({"context_hash": "old"}, "context_hash_stale"),
        ({"context_hash": None}, "context_hash_missing"),
        (
            {"title": "Unattested", "provenance": "legacy_unknown"},
            "unsafe_title_provenance",
        ),
        (
            {"title": "Unattested", "provenance": None},
            "unsafe_title_provenance",
        ),
        ({"output_hash": None}, "output_unknown"),
        ({"output_hash": "old"}, "output_mismatch"),
    ],
)
async def test_hash_only_or_unattested_state_is_never_complete(
    monkeypatch,
    changes,
    expected_reason,
):
    _install_context(monkeypatch, [1])
    # ``_article`` uses None as its default current context; set it explicitly
    # after construction for the NULL-context regression.
    row = _article(
        1,
        **{
            k: v
            for k, v in changes.items()
            if k not in {"context_hash", "output_hash"}
        },
    )
    if "context_hash" in changes:
        row["enrichment_context_hash"] = changes["context_hash"]
    if "output_hash" in changes:
        row["enrichment_output_hash"] = changes["output_hash"]
    state = await completion.evaluate_law_enrichment_completion(
        _Queryable([row], _terms(1)),
        "server",
    )

    assert state.complete is False
    assert state.incomplete_article_ids == (1,)
    assert expected_reason in state.article_states[0].reasons


@pytest.mark.asyncio
async def test_source_and_generated_title_ownership_are_parser_backed(
    monkeypatch,
):
    _install_context(monkeypatch, [1, 2, 3, 4])
    monkeypatch.setattr(
        completion,
        "_source_title_resolver",
        lambda: (
            lambda article_ref, _full_text: (
                "Parser title" if article_ref in {"Article 1", "Article 2"} else None
            )
        ),
    )
    rows = [
        _article(1, title="Parser title", provenance="source"),
        _article(2, title="Generated", provenance="generated"),
        _article(3, title="Generated", provenance="generated"),
        _article(4, title=None, provenance=None),
    ]
    state = await completion.evaluate_law_enrichment_completion(
        _Queryable(rows, sum((_terms(row["id"]) for row in rows), [])),
        "server",
    )

    assert state.incomplete_article_ids == (2,)
    assert state.reasons_by_article[2] == ("unsafe_title_provenance",)


@pytest.mark.asyncio
async def test_tombstone_requires_empty_output_and_zero_approved_rows(
    monkeypatch,
):
    _install_context(monkeypatch, [1], effective={1: 0})
    row = _article(
        1,
        full_text="Статья 1. Утратила силу.",
        penalty_summary="штраф",
        penalty_json={"fine": "100"},
    )
    state = await completion.evaluate_law_enrichment_completion(
        _Queryable([row], [{"article_id": 1, "term": "leftover"}]),
        "server",
    )

    assert state.complete is False
    reasons = set(state.article_states[0].reasons)
    assert "tombstone_has_approved_synonyms" in reasons
    assert "tombstone_physical_not_zero" in reasons
    assert "tombstone_has_penalty_summary" in reasons
    assert "tombstone_has_penalty_json" in reasons


@pytest.mark.asyncio
async def test_legacy_style_auto_mutation_without_hash_update_is_incomplete(
    monkeypatch,
):
    _install_context(monkeypatch, [1])
    monkeypatch.setattr(
        completion,
        "build_enrichment_output_hash",
        build_enrichment_output_hash,
    )
    original = _terms(1)
    original_conn = _Queryable([], original)
    row = _article(1)
    row["enrichment_output_hash"] = build_enrichment_output_hash(
        server_id="server",
        article_id=1,
        title=None,
        title_provenance=None,
        penalty_summary=None,
        penalty_json=None,
        auto_rows=original_conn.auto,
    )
    changed = list(original)
    changed[-1] = {"article_id": 1, "term": "legacy writer mutation"}

    state = await completion.evaluate_law_enrichment_completion(
        _Queryable([row], changed),
        "server",
    )

    assert state.complete is False
    assert "output_mismatch" in state.article_states[0].reasons


@pytest.mark.asyncio
async def test_recomputed_output_hash_cannot_attest_foreign_auto_ownership(
    monkeypatch,
):
    _install_context(monkeypatch, [1])
    monkeypatch.setattr(
        completion,
        "build_enrichment_output_hash",
        build_enrichment_output_hash,
    )
    approved = _terms(1)
    conn = _Queryable([], approved)
    row = _article(1)
    row["enrichment_output_hash"] = build_enrichment_output_hash(
        server_id="server",
        article_id=1,
        title=None,
        title_provenance=None,
        penalty_summary=None,
        penalty_json=None,
        auto_rows=conn.auto,
    )
    corrupted = _Queryable([row], approved)
    corrupted.auto.append({
        "server_id": "another-server",
        "article_id": 1,
        "term": "foreign ownership row",
        "weight": 1.0,
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    })
    # Model a corrupt/mixed writer that knows the output-hash algorithm and
    # deliberately fingerprints the foreign row too.  Matching bytes are not
    # ownership evidence.
    row["enrichment_output_hash"] = build_enrichment_output_hash(
        server_id="server",
        article_id=1,
        title=None,
        title_provenance=None,
        penalty_summary=None,
        penalty_json=None,
        auto_rows=corrupted.auto,
    )

    state = await completion.evaluate_law_enrichment_completion(
        corrupted,
        "server",
    )

    assert state.complete is False
    assert "foreign_auto_ownership" in state.article_states[0].reasons
    assert "output_mismatch" not in state.article_states[0].reasons


@pytest.mark.asyncio
async def test_document_scope_uses_full_context_but_returns_only_targets(
    monkeypatch,
):
    seen_catalog_ids = []
    _install_context(monkeypatch, [1, 2])

    def context_digests(rows, **_kwargs):
        seen_catalog_ids.extend(int(row["id"]) for row in rows)
        return {1: "context-1", 2: "context-2"}

    monkeypatch.setattr(
        completion,
        "build_enrichment_context_digests",
        context_digests,
    )
    rows = [_article(1, document_id=10), _article(2, document_id=20)]
    state = await completion.evaluate_law_enrichment_completion(
        _Queryable(rows, _terms(1) + _terms(2)),
        "server",
        document_ids=[10],
    )

    assert seen_catalog_ids == [1, 2]
    assert [item.article_id for item in state.article_states] == [1]


@pytest.mark.asyncio
async def test_structural_only_document_scope_is_vacuously_complete(monkeypatch):
    conn = _Queryable(
        [{
            "id": 10,
            "law_document_id": 20,
            "source_order": 0,
            "article_ref": "Chapter I",
            "kind": "chapter",
            "code": "I",
            "title": "General",
            "doc_title": "Code",
            "full_text": "Chapter body",
            "article_hash": "chapter-hash",
            "enrich_hash": None,
            "penalty_summary": None,
            "penalty_json": None,
            "title_provenance": None,
            "enrichment_context_hash": None,
            "enrichment_output_hash": None,
        }],
        [],
    )

    state = await completion.evaluate_law_enrichment_completion(
        conn,
        "server",
        document_ids=[20],
    )

    assert state.complete is True
    assert state.articles == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", [1.0, "1", True])
async def test_completion_filters_require_exact_integer_ids(alias):
    conn = _Queryable([], [])

    with pytest.raises(
        completion.LawEnrichmentCompletionError,
        match="positive integers",
    ):
        await completion.evaluate_law_enrichment_completion(
            conn,
            "server",
            article_ids=[alias],
        )

    assert conn.queries == []


@pytest.mark.asyncio
async def test_pool_level_evaluation_uses_one_repeatable_read_snapshot(
    monkeypatch,
):
    import database.manager as database_manager

    class _Conn:
        def __init__(self):
            self.commands = []

        async def execute(self, command):
            self.commands.append(command)

    conn = _Conn()

    class _Manager:
        @asynccontextmanager
        async def transaction(self):
            yield conn

    manager = _Manager()
    monkeypatch.setattr(database_manager, "db_manager", manager)
    expected = completion.LawEnrichmentCompletion(
        server_id="server",
        article_states=(),
    )

    async def snapshot(queryable, server_id, **filters):
        assert queryable is conn
        assert server_id == "server"
        assert filters == {"article_ids": None, "document_ids": [10]}
        return expected

    monkeypatch.setattr(
        completion,
        "_evaluate_law_enrichment_completion_snapshot",
        snapshot,
    )

    actual = await completion.evaluate_law_enrichment_completion(
        manager,
        "server",
        document_ids=[10],
    )

    assert actual is expected
    assert conn.commands == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    ]
