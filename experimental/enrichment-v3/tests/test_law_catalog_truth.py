from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

import rate_limiter
from services import law_catalog_service as catalog_service
from services import law_synonym_service as synonym_service
from services.law_catalog_service import _catalog_rows
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LawCatalogTruthError,
    validate_law_source_catalog_binding,
    verify_law_documents_catalog_truth,
)


SERVER_ID = "memphis"


def _document(
    content: str = "Статья 1. Проверочная норма\n1. Текст нормы.",
) -> dict:
    return {
        "id": 10,
        "server_id": SERVER_ID,
        "source_thread_id": 123,
        "title": "Проверочный кодекс",
        "content": content,
        "content_hash": "legacy-is-not-trusted",
        "derived_sync_pending": False,
    }


def _catalog(
    document: dict,
    *,
    first_id: int = 100,
) -> list[dict]:
    result: list[dict] = []
    for index, parsed in enumerate(_catalog_rows(document["content"])):
        result.append({
            "id": first_id + index,
            "server_id": SERVER_ID,
            "parent_server_id": SERVER_ID,
            "law_document_id": document["id"],
            "source_order": parsed["source_order"],
            "article_ref": parsed["article_ref"],
            "kind": parsed["kind"],
            "code": parsed["code"],
            "title": parsed["title"],
            "doc_title": document["title"],
            "full_text": parsed["full_text"],
            "article_hash": parsed["article_hash"],
            "title_provenance": (
                "source" if parsed["title"] is not None else None
            ),
            "enrich_hash": None,
            "penalty_summary": None,
            "penalty_json": None,
            "enrichment_context_hash": None,
            "enrichment_output_hash": None,
        })
    return result


def _verify(document: dict, rows: list[dict], **kwargs):
    return verify_law_documents_catalog_truth(
        SERVER_ID,
        [document],
        rows,
        **kwargs,
    )


def test_replay_binds_complete_source_catalog_projection() -> None:
    document = _document()
    truth = _verify(document, _catalog(document))

    assert truth.binding["parser_contract_version"] == (
        LAW_CATALOG_PARSER_CONTRACT_VERSION
    )
    assert truth.binding["documents"] == 1
    assert truth.binding["catalog_rows"] == len(truth.catalog_rows)
    assert truth.binding["source_fingerprint"]["snapshot_version"] == 3


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (
            lambda docs, rows: docs[0].__setitem__("id", "10"),
            "positive integer",
        ),
        (
            lambda docs, rows: rows[0].__setitem__("id", 100.0),
            "positive integer",
        ),
        (
            lambda docs, rows: rows[0].__setitem__("source_order", "0"),
            "non-negative integer",
        ),
    ],
)
def test_identity_and_position_fields_require_exact_integers(
    mutator,
    match: str,
) -> None:
    document = _document()
    documents = [document]
    rows = _catalog(document)
    mutator(documents, rows)

    with pytest.raises(LawCatalogTruthError, match=match):
        verify_law_documents_catalog_truth(
            SERVER_ID,
            documents,
            rows,
        )


@pytest.mark.parametrize("alias", [True, 123.0, "123"])
def test_source_thread_id_requires_an_exact_positive_integer(alias) -> None:
    document = _document()
    rows = _catalog(document)
    document["source_thread_id"] = alias

    with pytest.raises(LawCatalogTruthError, match="positive integer"):
        _verify(document, rows)


@pytest.mark.parametrize(
    ("field", "alias"),
    [
        ("snapshot_version", 3.0),
        ("documents", True),
        ("documents", 1.0),
    ],
)
def test_compact_binding_rejects_python_equal_numeric_aliases(
    field,
    alias,
) -> None:
    document = _document()
    binding = _verify(document, _catalog(document)).binding
    binding["source_fingerprint"][field] = alias

    with pytest.raises(ValueError, match="exact-content fingerprint"):
        validate_law_source_catalog_binding(binding)


@pytest.mark.parametrize(
    "field",
    ["kind", "code", "doc_title", "full_text", "article_hash"],
)
def test_persisted_parser_projection_drift_fails_closed(field: str) -> None:
    document = _document()
    rows = _catalog(document)
    rows[0][field] = {
        "kind": "other",
        "code": "999",
        "doc_title": "Чужой документ",
        "full_text": "Detached catalog body",
        "article_hash": "0" * 32,
    }[field]

    with pytest.raises(LawCatalogTruthError, match="normal catalog reconcile"):
        _verify(document, rows)


def test_missing_extra_and_duplicate_positions_fail_closed() -> None:
    document = _document(
        "Статья 1. Первая\n1. Текст.\n"
        "Статья 2. Вторая\n1. Другой текст."
    )
    rows = _catalog(document)

    with pytest.raises(LawCatalogTruthError, match="topology"):
        _verify(document, rows[:-1])

    duplicate = deepcopy(rows)
    duplicate.append({**deepcopy(rows[-1]), "id": 999})
    with pytest.raises(LawCatalogTruthError, match="duplicate catalog position"):
        _verify(document, duplicate)


def test_generated_title_is_validated_but_not_source_binding_input() -> None:
    document = _document(
        "Статья 2.\n1. Лицо обязано соблюдать проверочную норму."
    )
    rows = _catalog(document)
    assert rows[0]["title"] is None
    rows[0]["title"] = "Первый проверенный заголовок"
    rows[0]["title_provenance"] = "generated"
    first = _verify(document, rows)

    changed = deepcopy(rows)
    changed[0]["title"] = "Другой проверенный заголовок"
    second = _verify(document, changed)

    assert second.binding == first.binding


def test_parser_owned_title_mutation_is_never_generated_fallback() -> None:
    document = _document()
    rows = _catalog(document)
    assert rows[0]["title"] is not None
    rows[0]["title"] = "Подменённый структурный заголовок"
    rows[0]["title_provenance"] = "generated"

    with pytest.raises(LawCatalogTruthError, match="parser-owned title"):
        _verify(document, rows)


def test_attestation_only_allows_legacy_title_ownership_not_dirty_tombstone() -> None:
    document = _document(
        "Статья 2.\n1. Лицо обязано соблюдать проверочную норму."
    )
    rows = _catalog(document)
    rows[0]["title"] = "Исторический заголовок"
    rows[0]["title_provenance"] = "legacy_unknown"

    with pytest.raises(LawCatalogTruthError, match="safely owned"):
        _verify(document, rows)
    attestation_truth = _verify(
        document,
        rows,
        allow_legacy_title_attestation=True,
    )
    attested = deepcopy(rows)
    attested[0]["title_provenance"] = "generated"
    assert _verify(document, attested).binding == attestation_truth.binding

    tombstone_document = _document("Статья 3\nУтратила силу.")
    tombstone_rows = _catalog(tombstone_document)
    tombstone_rows[0]["title"] = "Старый заголовок"
    tombstone_rows[0]["title_provenance"] = "legacy_unknown"
    with pytest.raises(LawCatalogTruthError, match="tombstone"):
        _verify(
            tombstone_document,
            tombstone_rows,
            allow_legacy_title_attestation=True,
        )


def test_parser_contract_version_changes_binding_without_source_change() -> None:
    document = _document()
    rows = _catalog(document)
    current = _verify(document, rows)
    future = _verify(
        document,
        rows,
        parser_contract_version="law-catalog-parser-contract-future",
    )

    assert future.source_fingerprint == current.source_fingerprint
    assert future.binding["catalog_projection_hash"] == (
        current.binding["catalog_projection_hash"]
    )
    assert future.binding["value"] != current.binding["value"]


class _AutonomousEnrichmentConn:
    def __init__(self, documents: list[dict], rows: list[dict]):
        self.documents = deepcopy(documents)
        self.rows = deepcopy(rows)
        self.calls: list[tuple[str, str]] = []

    async def execute(self, query, *_args):
        normalized = " ".join(query.split())
        self.calls.append(("execute", normalized))
        return "OK"

    async def fetch(self, query, *_args):
        normalized = " ".join(query.split())
        self.calls.append(("fetch", normalized))
        if "FROM law_documents" in normalized:
            return deepcopy(self.documents)
        if "FROM law_articles" in normalized:
            return deepcopy(self.rows)
        raise AssertionError(normalized)


class _AutonomousEnrichmentDb:
    def __init__(self, conn: _AutonomousEnrichmentConn):
        self.conn = conn

    @asynccontextmanager
    async def transaction(self):
        yield self.conn


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["stored_kind", "stored_code", "stored_title"])
async def test_autonomous_gemini_fails_before_first_network_on_catalog_drift(
    monkeypatch,
    drift: str,
) -> None:
    document = _document(
        "Глава I. Общие положения\n"
        "Статья 1.\n"
        "1. Лицо обязано соблюдать установленный порядок."
    )
    rows = _catalog(document)
    if drift == "stored_kind":
        target = next(row for row in rows if row["kind"] == "article")
        target["kind"] = "other"
    elif drift == "stored_code":
        target = next(row for row in rows if row["kind"] == "article")
        target["code"] = "999"
    else:
        target = next(row for row in rows if row["kind"] == "chapter")
        target["title"] = "Подменённый структурный заголовок"

    conn = _AutonomousEnrichmentConn([document], rows)
    monkeypatch.setattr(
        synonym_service,
        "db_manager",
        _AutonomousEnrichmentDb(conn),
    )
    network_calls: list[bool] = []

    async def forbidden_network(*_args, **_kwargs):
        network_calls.append(True)
        raise AssertionError("Gemini network must not run")

    monkeypatch.setattr(
        rate_limiter,
        "execute_vertex_request",
        forbidden_network,
    )

    with pytest.raises(
        synonym_service.SynonymWriteError,
        match="active parser contract",
    ):
        await catalog_service.enrich_articles_for_server(
            SERVER_ID,
            only_stale=True,
        )

    assert network_calls == []
    assert not any(
        "FROM law_synonyms" in query
        for kind, query in conn.calls
        if kind == "fetch"
    )
