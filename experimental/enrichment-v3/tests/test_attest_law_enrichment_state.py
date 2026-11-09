from __future__ import annotations

import copy
from contextlib import asynccontextmanager

import pytest

import scripts.attest_law_enrichment_state as attestation
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
)


def _article(
    article_id: int,
    *,
    title: str | None,
    article_ref: str | None = None,
    full_text: str | None = None,
    article_hash: str | None = None,
    enrich_hash: str | None = None,
    title_provenance: str | None = "legacy_unknown",
    context_hash: str | None = None,
    server_id: str = "memphis",
) -> dict:
    article_hash = article_hash or f"body-hash-{article_id}"
    article_ref = article_ref or f"Статья {article_id}"
    full_text = full_text or (
        f"{article_ref}\n1. Проверенное положение закона номер {article_id}."
    )
    return {
        "id": article_id,
        "server_id": server_id,
        "law_document_id": 100 + article_id,
        "source_order": article_id,
        "article_ref": article_ref,
        "kind": "article",
        "code": str(article_id),
        "title": title,
        "doc_title": "Кодекс",
        "full_text": full_text,
        "article_hash": article_hash,
        "enrich_hash": (
            article_hash if enrich_hash is None else enrich_hash
        ),
        "penalty_summary": None,
        "penalty_json": None,
        "title_provenance": title_provenance,
        "enrichment_context_hash": context_hash,
        "updated_at": "2026-07-30T00:00:00Z",
    }


def _captured(article: dict) -> dict:
    return {
        field: article[field]
        for field in (
            "id",
            "law_document_id",
            "source_order",
            "article_ref",
            "kind",
            "code",
            "title",
            "doc_title",
            "full_text",
            "article_hash",
            "enrich_hash",
            "title_provenance",
            "enrichment_context_hash",
        )
    }


def _evidence(article: dict, *, auto_terms=(), **updates) -> dict:
    row = {
        "server_id": article["server_id"],
        "article_id": article["id"],
        "article_hash": article["article_hash"],
        "generation_input_hash": "a" * 64,
        "artifact_hash": "b" * 64,
        "post_commit_generation_hash": enrichment_generation_input_hash(
            article,
            auto_terms,
        ),
    }
    row.update(updates)
    return row


def test_plan_classifies_all_title_states_and_never_erases_stale_context():
    source = _article(
        1,
        title="Источник",
        article_ref="Статья 1. Источник",
        full_text=(
            "Статья 1. Источник\n"
            "1. Проверенное содержательное положение источника."
        ),
        # Migration 026 intentionally leaves historical provenance NULL;
        # attestation must classify it from source/evidence, not trust a
        # prefilled label.
        title_provenance=None,
    )
    generated = _article(
        2,
        title="Проверенное название",
        title_provenance=None,
    )
    null_title = _article(
        3,
        title=None,
        enrich_hash="older-body-hash",
        title_provenance="generated",
    )
    legacy = _article(
        4,
        title="Недоказанное старое название",
        title_provenance=None,
        context_hash="c" * 64,
    )
    rows = [source, generated, null_title, legacy]
    contexts = {
        1: "1" * 64,
        2: "2" * 64,
        3: "3" * 64,
        4: "4" * 64,
    }

    plan, report = attestation._plan_server_attestation(
        "memphis",
        catalog_rows=[_captured(row) for row in rows],
        articles=rows,
        auto_rows=[],
        approved_synonym_counts={},
        context_hashes=contexts,
        evidence_rows=[_evidence(generated)],
    )
    planned = {item["article"]["id"]: item for item in plan}

    assert planned[1]["desired_title_provenance"] == "source"
    assert planned[1]["desired_enrichment_context_hash"] is None
    assert planned[2]["desired_title_provenance"] == "generated"
    assert planned[2]["desired_enrichment_context_hash"] is None
    assert planned[3]["desired_title_provenance"] is None
    assert planned[3]["desired_enrichment_context_hash"] is None
    assert planned[4]["desired_title_provenance"] == "legacy_unknown"
    assert planned[4]["desired_enrichment_context_hash"] == "c" * 64
    assert report["title_source"] == 1
    assert report["title_generated"] == 1
    assert report["title_null"] == 1
    assert report["title_legacy_unknown"] == 1
    assert report["generation_evidence_matched"] == 1
    assert report["enrichment_stale"] == 1
    assert report["context_skipped_stale"] == 1
    assert report["context_stale"] == 1
    assert report["context_unknown_requires_regeneration"] == 2
    assert report["context_unknown_article_ids"] == [1, 2]
    assert report["context_stale_article_ids"] == [4]


def test_provenance_attestation_invalidates_a_preexisting_output_fingerprint():
    article = _article(
        5,
        title="Источник",
        article_ref="Статья 5. Источник",
        full_text=(
            "Статья 5. Источник\n"
            "1. Проверенное содержательное положение источника."
        ),
        title_provenance="legacy_unknown",
    )
    article["enrichment_output_hash"] = "a" * 64

    plan, report = attestation._plan_server_attestation(
        "memphis",
        catalog_rows=[_captured(article)],
        articles=[article],
        auto_rows=[],
        approved_synonym_counts={},
        context_hashes={5: "5" * 64},
        evidence_rows=[],
    )

    assert plan[0]["desired_title_provenance"] == "source"
    assert plan[0]["desired_enrichment_output_hash"] is None
    assert plan[0]["changed"] is True
    assert report["provenance_changes"] == 1
    assert report["would_update"] == 1


def test_unchanged_provenance_preserves_a_preexisting_output_fingerprint():
    article = _article(
        6,
        title=None,
        title_provenance=None,
        context_hash="6" * 64,
    )
    article["enrichment_output_hash"] = "b" * 64

    plan, report = attestation._plan_server_attestation(
        "memphis",
        catalog_rows=[_captured(article)],
        articles=[article],
        auto_rows=[],
        approved_synonym_counts={},
        context_hashes={6: "6" * 64},
        evidence_rows=[],
    )

    assert plan[0]["desired_title_provenance"] is None
    assert plan[0]["desired_enrichment_output_hash"] == "b" * 64
    assert plan[0]["changed"] is False
    assert report["provenance_changes"] == 0
    assert report["would_update"] == 0


@pytest.mark.parametrize(
    "evidence_update",
    [
        {"server_id": "foreign"},
        {"article_id": 999},
        {"article_hash": "foreign-body"},
        {"post_commit_generation_hash": "f" * 64},
        {"artifact_hash": "not-a-sha"},
        {"generation_input_hash": "not-a-sha"},
    ],
)
def test_generated_title_requires_exact_well_formed_durable_evidence(
    evidence_update,
):
    article = _article(
        10,
        title="Историческое название",
        title_provenance="generated",
    )
    plan, report = attestation._plan_server_attestation(
        "memphis",
        catalog_rows=[_captured(article)],
        articles=[article],
        auto_rows=[],
        approved_synonym_counts={},
        context_hashes={10: "1" * 64},
        evidence_rows=[_evidence(article, **evidence_update)],
    )

    assert (
        plan[0]["desired_title_provenance"]
        == attestation.TITLE_PROVENANCE_LEGACY_UNKNOWN
    )
    assert report["generation_evidence_matched"] == 0


def test_changed_auto_synonym_makes_old_post_commit_evidence_stale():
    article = _article(11, title="Сгенерированное название")
    evidence = _evidence(article, auto_terms=["старый синоним"])
    auto_rows = [
        {
            "article_id": 11,
            "server_id": "memphis",
            "term": "новый синоним",
            "source": "auto",
            "status": "approved",
        }
    ]

    plan, report = attestation._plan_server_attestation(
        "memphis",
        catalog_rows=[_captured(article)],
        articles=[article],
        auto_rows=auto_rows,
        approved_synonym_counts={11: 1},
        context_hashes={11: "1" * 64},
        evidence_rows=[evidence],
    )

    assert plan[0]["desired_title_provenance"] == "legacy_unknown"
    assert report["generation_evidence_matched"] == 0


def test_plan_rejects_missing_or_foreign_context_ownership():
    article = _article(12, title=None)
    with pytest.raises(
        attestation.AttestationError,
        match="context ownership mismatch",
    ):
        attestation._plan_server_attestation(
            "memphis",
            catalog_rows=[_captured(article)],
            articles=[article],
            auto_rows=[],
            approved_synonym_counts={},
            context_hashes={999: "1" * 64},
            evidence_rows=[],
        )

    foreign = _article(12, title=None, server_id="foreign")
    with pytest.raises(
        attestation.AttestationError,
        match="foreign ownership",
    ):
        attestation._plan_server_attestation(
            "memphis",
            catalog_rows=[_captured(foreign)],
            articles=[foreign],
            auto_rows=[],
            approved_synonym_counts={},
            context_hashes={12: "1" * 64},
            evidence_rows=[],
        )


def test_plan_refuses_parser_metadata_drift_before_attesting_title():
    article = _article(
        13,
        title="Источник",
        article_ref="Статья 99. Источник",
        full_text=(
            "Статья 99. Источник\n"
            "1. Проверенное содержательное положение источника."
        ),
    )
    with pytest.raises(
        attestation.AttestationError,
        match="parser fields differ",
    ):
        attestation._plan_server_attestation(
            "memphis",
            catalog_rows=[_captured(article)],
            articles=[article],
            auto_rows=[],
            approved_synonym_counts={},
            context_hashes={13: "1" * 64},
            evidence_rows=[],
        )


def test_plan_refuses_a_generated_value_over_a_source_owned_title():
    article = _article(
        14,
        title="Чужое сгенерированное название",
        article_ref="Статья 14. Название источника",
        full_text=(
            "Статья 14. Название источника\n"
            "1. Проверенное содержательное положение источника."
        ),
    )
    with pytest.raises(
        attestation.AttestationError,
        match="source-owned title differs",
    ):
        attestation._plan_server_attestation(
            "memphis",
            catalog_rows=[_captured(article)],
            articles=[article],
            auto_rows=[],
            approved_synonym_counts={},
            context_hashes={14: "1" * 64},
            # Even otherwise-current evidence cannot transfer ownership of a
            # title that the source itself owns.
            evidence_rows=[_evidence(article)],
        )


def test_plan_refuses_a_missing_source_owned_title():
    article = _article(
        16,
        title=None,
        article_ref="Статья 16. Название источника",
        full_text=(
            "Статья 16. Название источника\n"
            "1. Проверенное содержательное положение источника."
        ),
        title_provenance=None,
    )
    with pytest.raises(
        attestation.AttestationError,
        match="source-owned title differs",
    ):
        attestation._plan_server_attestation(
            "memphis",
            catalog_rows=[_captured(article)],
            articles=[article],
            auto_rows=[],
            approved_synonym_counts={},
            context_hashes={16: "1" * 64},
            evidence_rows=[],
        )


@pytest.mark.parametrize(
    ("article_updates", "auto_rows"),
    [
        ({"title": "Утратила силу"}, []),
        ({"title_provenance": "generated"}, []),
        ({"penalty_summary": "старое наказание"}, []),
        ({"penalty_json": {"fine": "100"}}, []),
        (
            {},
            [
                {
                    "id": 1,
                    "server_id": "memphis",
                    "article_id": 15,
                    "term": "старый активный синоним",
                    "weight": 1.0,
                    "source": "auto",
                    "status": "approved",
                    "suggested_by": None,
                }
            ],
        ),
    ],
)
def test_plan_refuses_every_dirty_tombstone_enrichment_class(
    article_updates,
    auto_rows,
):
    article = _article(
        15,
        title=None,
        article_ref="Статья 15",
        full_text="Статья 15\nУтратила силу.",
        title_provenance=None,
    )
    article.update(article_updates)

    with pytest.raises(attestation.AttestationError, match="dirty tombstone"):
        attestation._plan_server_attestation(
            "memphis",
            catalog_rows=[_captured(article)],
            articles=[article],
            auto_rows=auto_rows,
            approved_synonym_counts=({15: len(auto_rows)} if auto_rows else {}),
            context_hashes={15: "1" * 64},
            evidence_rows=[],
        )


def test_plan_refuses_tombstone_with_any_approved_synonym_owner():
    article = _article(
        17,
        title=None,
        article_ref="\u0421\u0442\u0430\u0442\u044c\u044f 17",
        full_text=(
            "\u0421\u0442\u0430\u0442\u044c\u044f 17\n"
            "\u0423\u0442\u0440\u0430\u0442\u0438\u043b\u0430 "
            "\u0441\u0438\u043b\u0443."
        ),
        title_provenance=None,
    )

    with pytest.raises(attestation.AttestationError, match="dirty tombstone"):
        attestation._plan_server_attestation(
            "memphis",
            catalog_rows=[_captured(article)],
            articles=[article],
            auto_rows=[],
            approved_synonym_counts={17: 1},
            context_hashes={17: "1" * 64},
            evidence_rows=[],
        )


def test_plan_allows_clean_tombstone_without_approved_synonyms():
    article = _article(
        18,
        title=None,
        article_ref="\u0421\u0442\u0430\u0442\u044c\u044f 18",
        full_text=(
            "\u0421\u0442\u0430\u0442\u044c\u044f 18\n"
            "\u0423\u0442\u0440\u0430\u0442\u0438\u043b\u0430 "
            "\u0441\u0438\u043b\u0443."
        ),
        title_provenance=None,
    )

    plan, report = attestation._plan_server_attestation(
        "memphis",
        catalog_rows=[_captured(article)],
        articles=[article],
        auto_rows=[],
        approved_synonym_counts={},
        context_hashes={18: "1" * 64},
        evidence_rows=[],
    )

    assert len(plan) == 1
    assert plan[0]["changed"] is False
    assert report["would_update"] == 0


class _Connection:
    def __init__(
        self,
        rows_by_server,
        *,
        pending_servers=(),
        evidence=(),
        fail_cas_id=None,
    ):
        self.rows_by_server = rows_by_server
        self.pending_servers = set(pending_servers)
        self.evidence = list(evidence)
        self.fail_cas_id = fail_cas_id
        self.calls = []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "FROM law_enrichment_artifact_commits" in query:
            server_id, article_ids = args
            return [
                copy.deepcopy(row)
                for row in self.evidence
                if row["server_id"] == server_id
                and row["article_id"] in article_ids
            ]
        if "FROM law_articles" in query:
            server_id = args[0]
            return copy.deepcopy(self.rows_by_server.get(server_id, []))
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "derived_sync_pending IS TRUE" in query:
            return args[0] in self.pending_servers
        if "UPDATE law_articles" in query:
            article_id = int(args[2])
            if article_id == self.fail_cas_id:
                return None
            row = next(
                (
                    value
                    for value in self.rows_by_server[args[3]]
                    if value["id"] == article_id
                ),
                None,
            )
            if row is None:
                return None
            expected = {
                "law_document_id": args[4],
                "source_order": args[5],
                "article_ref": args[6],
                "kind": args[7],
                "code": args[8],
                "title": args[9],
                "doc_title": args[10],
                "full_text": args[11],
                "article_hash": args[12],
                "enrich_hash": args[13],
                "penalty_summary": args[14],
                "title_provenance": args[16],
                "enrichment_context_hash": args[17],
                "enrichment_output_hash": args[18],
                "updated_at": args[19],
            }
            if any(row.get(key) != value for key, value in expected.items()):
                return None
            row["title_provenance"] = args[0]
            row["enrichment_output_hash"] = args[1]
            return article_id
        raise AssertionError(query)


class _Database:
    def __init__(self, conn):
        self.conn = conn
        self.transactions = 0
        self.rollbacks = 0

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        before = copy.deepcopy(self.conn.rows_by_server)
        try:
            yield self.conn
        except Exception:
            self.conn.rows_by_server = before
            self.rollbacks += 1
            raise

    async def fetch(self, query, *args):
        assert "SELECT DISTINCT server_id" in query
        return [
            {"server_id": server_id}
            for server_id in sorted(self.conn.rows_by_server)
        ]


def _install_runtime(
    monkeypatch,
    db,
    events,
    contexts,
    *,
    approved_counts=None,
):
    approved_counts = approved_counts or {}

    @asynccontextmanager
    async def global_lock():
        events.append("global:enter")
        try:
            yield
        finally:
            events.append("global:exit")

    @asynccontextmanager
    async def document_lock(server_id):
        events.append(f"document:{server_id}:enter")
        try:
            yield
        finally:
            events.append(f"document:{server_id}:exit")

    @asynccontextmanager
    async def catalog_lock(server_id):
        events.append(f"catalog:{server_id}:enter")
        try:
            yield
        finally:
            events.append(f"catalog:{server_id}:exit")

    async def synonym_lock(conn, server_id):
        assert conn is db.conn
        events.append(f"synonym:{server_id}")

    async def generation_state(
        conn,
        server_id,
        *,
        lock_articles,
    ):
        assert conn is db.conn
        events.append(f"snapshot:{server_id}:{lock_articles}")
        rows = copy.deepcopy(conn.rows_by_server.get(server_id, []))
        return {
            "catalog_rows": [_captured(row) for row in rows],
            "articles": [_captured(row) for row in rows],
            "auto_rows": [],
            "approved_synonym_counts": {
                row["id"]: approved_counts[(server_id, row["id"])]
                for row in rows
                if (server_id, row["id"]) in approved_counts
            },
            "context_hashes": {
                row["id"]: contexts[(server_id, row["id"])]
                for row in rows
            },
        }

    monkeypatch.setattr(attestation, "db_manager", db)
    monkeypatch.setattr(
        attestation,
        "law_backfill_global_lock",
        global_lock,
    )
    monkeypatch.setattr(
        attestation,
        "law_document_server_lock",
        document_lock,
    )
    monkeypatch.setattr(
        attestation,
        "catalog_server_lock",
        catalog_lock,
    )
    monkeypatch.setattr(
        attestation,
        "acquire_synonym_write_lock",
        synonym_lock,
    )
    monkeypatch.setattr(
        attestation,
        "load_enrichment_state_for_attestation_in_transaction",
        generation_state,
    )


@pytest.mark.asyncio
async def test_dry_run_is_read_only_and_all_uses_one_transaction_per_server(
    monkeypatch,
):
    rows = {
        "davis": [_article(1, title=None, server_id="davis")],
        "memphis": [_article(2, title=None)],
    }
    conn = _Connection(rows)
    db = _Database(conn)
    events = []
    contexts = {
        ("davis", 1): "1" * 64,
        ("memphis", 2): "2" * 64,
    }
    _install_runtime(monkeypatch, db, events, contexts)

    report = await attestation.attest_servers(None)

    assert report["schema"] == attestation.ATTESTATION_SCHEMA
    assert report["mode"] == "dry-run"
    assert report["server_count"] == 2
    assert db.transactions == 2
    assert all(server["updated"] == 0 for server in report["servers"])
    assert sum(
        "SET TRANSACTION READ ONLY" in query
        for method, query, _args in conn.calls
        if method == "execute"
    ) == 2
    assert not any(
        "UPDATE law_articles" in query
        for method, query, _args in conn.calls
        if method == "fetchval"
    )
    assert events == [
        "global:enter",
        "document:davis:enter",
        "catalog:davis:enter",
        "synonym:davis",
        "snapshot:davis:False",
        "catalog:davis:exit",
        "document:davis:exit",
        "document:memphis:enter",
        "catalog:memphis:enter",
        "synonym:memphis",
        "snapshot:memphis:False",
        "catalog:memphis:exit",
        "document:memphis:exit",
        "global:exit",
    ]


@pytest.mark.asyncio
async def test_apply_is_idempotent_and_uses_full_metadata_cas(
    monkeypatch,
):
    row = _article(
        1,
        title="Источник",
        article_ref="Статья 1. Источник",
        full_text=(
            "Статья 1. Источник\n"
            "1. Проверенное содержательное положение источника."
        ),
        title_provenance="legacy_unknown",
    )
    conn = _Connection({"memphis": [row]})
    db = _Database(conn)
    events = []
    _install_runtime(
        monkeypatch,
        db,
        events,
        {("memphis", 1): "1" * 64},
    )

    first = await attestation.attest_servers(["memphis"], apply=True)
    second = await attestation.attest_servers(["memphis"], apply=True)

    assert first["totals"]["updated"] == 1
    assert second["totals"]["updated"] == 0
    assert conn.rows_by_server["memphis"][0]["title_provenance"] == "source"
    assert conn.rows_by_server["memphis"][0]["enrichment_context_hash"] is None
    assert (
        first["totals"]["context_unknown_requires_regeneration"] == 1
    )
    assert (
        second["totals"]["context_unknown_requires_regeneration"] == 1
    )
    updates = [
        query
        for method, query, _args in conn.calls
        if method == "fetchval" and "UPDATE law_articles" in query
    ]
    assert len(updates) == 1
    cas = updates[0]
    for required in (
        "law_document_id = $5",
        "source_order = $6",
        "article_ref IS NOT DISTINCT FROM $7",
        "kind = $8",
        "code IS NOT DISTINCT FROM $9",
        "title IS NOT DISTINCT FROM $10",
        "doc_title IS NOT DISTINCT FROM $11",
        "full_text IS NOT DISTINCT FROM $12",
        "article_hash = $13",
        "enrich_hash IS NOT DISTINCT FROM $14",
        "penalty_summary IS NOT DISTINCT FROM $15",
        "penalty_json IS NOT DISTINCT FROM $16::jsonb",
        "title_provenance IS NOT DISTINCT FROM $17",
        "enrichment_context_hash IS NOT DISTINCT FROM $18",
        "enrichment_output_hash IS NOT DISTINCT FROM $19",
        "updated_at IS NOT DISTINCT FROM $20",
    ):
        assert required in cas
    assert "SET title_provenance = $1," in cas
    assert "enrichment_output_hash = $2" in cas
    assert "SET enrichment_context_hash" not in cas


@pytest.mark.asyncio
async def test_apply_cas_failure_rolls_back_whole_server(monkeypatch):
    rows = [
        _article(1, title=None, title_provenance="legacy_unknown"),
        _article(2, title=None, title_provenance="legacy_unknown"),
    ]
    original = copy.deepcopy(rows)
    conn = _Connection(
        {"memphis": rows},
        fail_cas_id=2,
    )
    db = _Database(conn)
    events = []
    _install_runtime(
        monkeypatch,
        db,
        events,
        {
            ("memphis", 1): "1" * 64,
            ("memphis", 2): "2" * 64,
        },
    )

    with pytest.raises(attestation.AttestationError, match="failed full"):
        await attestation.attest_servers(["memphis"], apply=True)

    assert db.rollbacks == 1
    assert conn.rows_by_server["memphis"] == original


@pytest.mark.asyncio
async def test_dirty_tombstone_refuses_before_any_server_update(monkeypatch):
    rows = [
        _article(
            1,
            title="Источник",
            article_ref="Статья 1. Источник",
            full_text=(
                "Статья 1. Источник\n"
                "1. Проверенное содержательное положение источника."
            ),
            title_provenance=None,
        ),
        _article(
            2,
            title="Утратила силу",
            article_ref="Статья 2",
            full_text="Статья 2\nУтратила силу.",
            title_provenance=None,
        ),
    ]
    conn = _Connection({"memphis": rows})
    db = _Database(conn)
    events = []
    _install_runtime(
        monkeypatch,
        db,
        events,
        {
            ("memphis", 1): "1" * 64,
            ("memphis", 2): "2" * 64,
        },
    )

    with pytest.raises(attestation.AttestationError, match="dirty tombstone"):
        await attestation.attest_servers(["memphis"], apply=True)

    assert db.rollbacks == 1
    assert not any(
        "UPDATE law_articles" in query
        for method, query, _args in conn.calls
        if method == "fetchval"
    )


@pytest.mark.asyncio
async def test_approved_manual_synonym_on_tombstone_refuses_before_update(
    monkeypatch,
):
    rows = [
        _article(1, title=None, title_provenance="legacy_unknown"),
        _article(
            2,
            title=None,
            article_ref="\u0421\u0442\u0430\u0442\u044c\u044f 2",
            full_text=(
                "\u0421\u0442\u0430\u0442\u044c\u044f 2\n"
                "\u0423\u0442\u0440\u0430\u0442\u0438\u043b\u0430 "
                "\u0441\u0438\u043b\u0443."
            ),
            title_provenance=None,
        ),
    ]
    conn = _Connection({"memphis": rows})
    db = _Database(conn)
    events = []
    _install_runtime(
        monkeypatch,
        db,
        events,
        {
            ("memphis", 1): "1" * 64,
            ("memphis", 2): "2" * 64,
        },
        approved_counts={("memphis", 2): 1},
    )

    with pytest.raises(attestation.AttestationError, match="dirty tombstone"):
        await attestation.attest_servers(["memphis"], apply=True)

    assert db.rollbacks == 1
    assert not any(
        "UPDATE law_articles" in query
        for method, query, _args in conn.calls
        if method == "fetchval"
    )


@pytest.mark.asyncio
async def test_pending_derived_sync_refuses_before_snapshot(monkeypatch):
    row = _article(1, title=None)
    conn = _Connection(
        {"memphis": [row]},
        pending_servers={"memphis"},
    )
    db = _Database(conn)
    events = []
    _install_runtime(
        monkeypatch,
        db,
        events,
        {("memphis", 1): "1" * 64},
    )

    with pytest.raises(attestation.AttestationError, match="unfinished"):
        await attestation.attest_servers(["memphis"])

    assert not any(value.startswith("snapshot:") for value in events)


def test_cli_is_dry_run_by_default_and_requires_one_scope():
    parser = attestation.build_parser()
    one = parser.parse_args(["memphis"])
    all_servers = parser.parse_args(["--all", "--apply"])
    assert one.server_id == "memphis"
    assert one.all is False
    assert one.apply is False
    assert all_servers.server_id is None
    assert all_servers.all is True
    assert all_servers.apply is True
