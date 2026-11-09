"""Versioned proof that ``law_articles`` is derived from ``law_documents``.

The source fingerprint and the persisted catalog projection are useful only
when they describe the same parser generation.  This module owns that missing
link: it replays the shared catalog parser over the exact document bytes and
compares the complete persisted catalog before enrichment selection, model
traffic, export, completion, or a live write is allowed.

The parser itself deliberately remains in :mod:`law_catalog_service`.  Imports
of that module are lazy so this service can also be used by catalog/synonym
code without creating a module-import cycle.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from services.law_source_snapshot import (
    SOURCE_CONTENT_FINGERPRINT_VERSION,
    law_source_content_fingerprint,
)


# Any change to segmentation, header recovery, kind/code derivation, structural
# title derivation, tombstone classification, or catalog source hashing must
# bump this complete parser contract.  Context freshness and every native-v3
# artifact bind this value.
LAW_CATALOG_PARSER_CONTRACT_VERSION = (
    "law-catalog-parser-contract-2026-07-31-v1"
)
LAW_SOURCE_CATALOG_BINDING_SCHEMA = "law-source-catalog-binding-v1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_TITLE_PROVENANCE = "source"
_GENERATED_TITLE_PROVENANCE = "generated"


class LawCatalogTruthError(RuntimeError):
    """The persisted catalog cannot be proven from the current source."""


@dataclass(frozen=True)
class VerifiedLawCatalogTruth:
    """One owned, replay-verified server source/catalog snapshot."""

    server_id: str
    source_documents: tuple[dict[str, Any], ...]
    catalog_rows: tuple[dict[str, Any], ...]
    binding: dict[str, Any]

    @property
    def article_rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            row for row in self.catalog_rows if row["kind"] == "article"
        )

    @property
    def source_fingerprint(self) -> dict[str, Any]:
        return dict(self.binding["source_fingerprint"])


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _positive_int(value: Any, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise LawCatalogTruthError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, *, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
    ):
        raise LawCatalogTruthError(
            f"{field} must be a non-negative integer"
        )
    return value


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LawCatalogTruthError(f"{field} must be text or null")
    return value


def _required_text(value: Any, *, field: str, nonempty: bool = False) -> str:
    if not isinstance(value, str):
        raise LawCatalogTruthError(f"{field} must be text")
    if nonempty and not value.strip():
        raise LawCatalogTruthError(f"{field} must be non-empty text")
    return value


def _owned_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    field: str,
) -> list[dict[str, Any]]:
    """Materialize each possibly-stateful mapping exactly once."""

    try:
        materialized = list(rows)
    except TypeError as exc:
        raise LawCatalogTruthError(f"{field} must be iterable") from exc
    owned: list[dict[str, Any]] = []
    for index, row in enumerate(materialized):
        if not isinstance(row, Mapping):
            raise LawCatalogTruthError(
                f"{field}[{index}] must be an object"
            )
        # dict(row) is the sole read of the caller-owned mapping.  All
        # validation, hashing and returned state use this deep-owned copy.
        owned.append(deepcopy(dict(row)))
    return owned


def _normal_source_documents(
    server_id: str,
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    owned = _owned_rows(rows, field="law_documents")
    if not owned:
        raise LawCatalogTruthError(
            f"{server_id}: no law_documents; normal catalog reconcile required"
        )
    normalized: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index, row in enumerate(owned):
        doc_id = _positive_int(
            row.get("id"),
            field=f"law_documents[{index}].id",
        )
        if doc_id in seen_ids:
            raise LawCatalogTruthError(
                f"{server_id}: duplicate law_document id {doc_id}; "
                "normal catalog reconcile required"
            )
        seen_ids.add(doc_id)
        owner = _required_text(
            row.get("server_id"),
            field=f"law_document {doc_id} server_id",
            nonempty=True,
        )
        if owner != server_id:
            raise LawCatalogTruthError(
                f"{server_id}: law_document {doc_id} belongs to {owner!r}; "
                "normal catalog reconcile required"
            )
        if row.get("derived_sync_pending") is True:
            raise LawCatalogTruthError(
                f"{server_id}: law_document {doc_id} is pending derived sync; "
                "run normal catalog reconcile"
            )
        pending = row.get("derived_sync_pending")
        if pending not in (None, False):
            raise LawCatalogTruthError(
                f"{server_id}: law_document {doc_id} has invalid "
                "derived_sync_pending; normal catalog reconcile required"
            )
        title = _required_text(
            row.get("title"),
            field=f"law_document {doc_id} title",
        )
        content = _required_text(
            row.get("content"),
            field=f"law_document {doc_id} content",
            nonempty=True,
        )
        source_thread_id = row.get("source_thread_id")
        if source_thread_id is not None:
            source_thread_id = _positive_int(
                source_thread_id,
                field=f"law_document {doc_id} source_thread_id",
            )
        normalized.append({
            **row,
            "id": doc_id,
            "server_id": owner,
            "source_thread_id": source_thread_id,
            "title": title,
            "content": content,
            "derived_sync_pending": False,
        })
    normalized.sort(key=lambda row: row["id"])
    return normalized


def _normal_catalog_rows(
    server_id: str,
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    owned = _owned_rows(rows, field="law_articles")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_positions: set[tuple[int, int]] = set()
    for index, row in enumerate(owned):
        article_id = _positive_int(
            row.get("id"),
            field=f"law_articles[{index}].id",
        )
        if article_id in seen_ids:
            raise LawCatalogTruthError(
                f"{server_id}: duplicate law_article id {article_id}; "
                "normal catalog reconcile required"
            )
        seen_ids.add(article_id)
        document_id = _positive_int(
            row.get("law_document_id"),
            field=f"law_article {article_id} law_document_id",
        )
        source_order = _nonnegative_int(
            row.get("source_order"),
            field=f"law_article {article_id} source_order",
        )
        position = (document_id, source_order)
        if position in seen_positions:
            raise LawCatalogTruthError(
                f"{server_id}: duplicate catalog position {position}; "
                "normal catalog reconcile required"
            )
        seen_positions.add(position)
        owner = _required_text(
            row.get("server_id"),
            field=f"law_article {article_id} server_id",
            nonempty=True,
        )
        parent_owner = _optional_text(
            row.get("parent_server_id"),
            field=f"law_article {article_id} parent_server_id",
        )
        article_ref = _optional_text(
            row.get("article_ref"),
            field=f"law_article {article_id} article_ref",
        )
        kind = _required_text(
            row.get("kind"),
            field=f"law_article {article_id} kind",
            nonempty=True,
        )
        code = _optional_text(
            row.get("code"),
            field=f"law_article {article_id} code",
        )
        title = _optional_text(
            row.get("title"),
            field=f"law_article {article_id} title",
        )
        doc_title = _required_text(
            row.get("doc_title"),
            field=f"law_article {article_id} doc_title",
        )
        full_text = _required_text(
            row.get("full_text"),
            field=f"law_article {article_id} full_text",
        )
        article_hash = _required_text(
            row.get("article_hash"),
            field=f"law_article {article_id} article_hash",
            nonempty=True,
        )
        provenance = _optional_text(
            row.get("title_provenance"),
            field=f"law_article {article_id} title_provenance",
        )
        normalized.append({
            **row,
            "id": article_id,
            "server_id": owner,
            "parent_server_id": parent_owner,
            "law_document_id": document_id,
            "source_order": source_order,
            "article_ref": article_ref,
            "kind": kind,
            "code": code,
            "title": title,
            "doc_title": doc_title,
            "full_text": full_text,
            "article_hash": article_hash,
            "title_provenance": provenance,
        })
    normalized.sort(
        key=lambda row: (
            row["law_document_id"],
            row["source_order"],
            row["id"],
        )
    )
    return normalized


def _binding_projection_row(
    row: Mapping[str, Any],
    *,
    parser_title: str | None,
    tombstone: bool,
) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "server_id": row["server_id"],
        "law_document_id": int(row["law_document_id"]),
        "source_order": int(row["source_order"]),
        "article_ref": row.get("article_ref"),
        "kind": row["kind"],
        "code": row.get("code"),
        "parser_title": parser_title,
        "doc_title": row["doc_title"],
        "full_text_sha256": hashlib.sha256(
            row["full_text"].encode("utf-8")
        ).hexdigest(),
        "article_hash": row["article_hash"],
        "tombstone": tombstone,
    }


def _replay_expected_catalog(
    server_id: str,
    documents: list[dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    from services.law_catalog_service import (
        _assert_catalog_mutation_safe,
        _catalog_rows,
    )

    expected_by_position: dict[tuple[int, int], dict[str, Any]] = {}
    try:
        for document in documents:
            document_id = document["id"]
            _assert_catalog_mutation_safe(
                document["content"],
                doc_id=document_id,
            )
            for parsed in _catalog_rows(document["content"]):
                position = (
                    document_id,
                    int(parsed["source_order"]),
                )
                if position in expected_by_position:
                    raise LawCatalogTruthError(
                        f"{server_id}: parser produced duplicate catalog "
                        f"position {position}"
                    )
                expected_by_position[position] = {
                    **parsed,
                    "server_id": server_id,
                    "law_document_id": document_id,
                    "doc_title": document["title"],
                }
    except LawCatalogTruthError:
        raise
    except Exception as exc:
        raise LawCatalogTruthError(
            f"{server_id}: shared parser replay failed; run normal catalog "
            "reconcile"
        ) from exc
    return expected_by_position


def build_expected_law_catalog_rows(
    server_id: str,
    source_documents: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return the shared parser's complete expected source projection."""

    documents = _normal_source_documents(server_id, source_documents)
    expected = _replay_expected_catalog(server_id, documents)
    return tuple(expected[position] for position in sorted(expected))


def verify_law_documents_catalog_truth(
    server_id: str,
    source_documents: Iterable[Mapping[str, Any]],
    catalog_rows: Iterable[Mapping[str, Any]],
    *,
    parser_contract_version: str = LAW_CATALOG_PARSER_CONTRACT_VERSION,
    allow_legacy_title_attestation: bool = False,
) -> VerifiedLawCatalogTruth:
    """Replay the current parser and prove the complete persisted catalog."""

    if not isinstance(server_id, str) or not server_id.strip():
        raise LawCatalogTruthError("server_id must be a non-empty string")
    server_id = server_id.strip()
    if (
        not isinstance(parser_contract_version, str)
        or not parser_contract_version.strip()
    ):
        raise LawCatalogTruthError(
            "parser_contract_version must be non-empty text"
        )
    parser_contract_version = parser_contract_version.strip()
    if not isinstance(allow_legacy_title_attestation, bool):
        raise LawCatalogTruthError(
            "allow_legacy_title_attestation must be bool"
        )

    documents = _normal_source_documents(server_id, source_documents)
    actual_rows = _normal_catalog_rows(server_id, catalog_rows)
    documents_by_id = {row["id"]: row for row in documents}

    # Lazy imports prevent law_catalog_service -> law_synonym_service -> this
    # module from becoming a module-load cycle.
    from services.law_catalog_service import _is_junk_title, source_article_hash
    from services.law_synonym_service import (
        is_explicitly_empty_or_repealed_article,
    )

    expected_by_position = _replay_expected_catalog(server_id, documents)

    actual_by_position = {
        (row["law_document_id"], row["source_order"]): row
        for row in actual_rows
    }
    expected_positions = set(expected_by_position)
    actual_positions = set(actual_by_position)
    if expected_positions != actual_positions:
        missing = sorted(expected_positions - actual_positions)
        extra = sorted(actual_positions - expected_positions)
        raise LawCatalogTruthError(
            f"{server_id}: persisted catalog topology differs from parser "
            f"replay; missing={missing}, extra={extra}; run normal catalog "
            "reconcile"
        )

    binding_rows: list[dict[str, Any]] = []
    for position in sorted(expected_positions):
        expected = expected_by_position[position]
        actual = actual_by_position[position]
        article_id = actual["id"]
        document = documents_by_id.get(actual["law_document_id"])
        if document is None:
            raise LawCatalogTruthError(
                f"{server_id}: law_article {article_id} references a foreign "
                "or missing document; run normal catalog reconcile"
            )
        if (
            actual["server_id"] != server_id
            or actual["parent_server_id"] != server_id
        ):
            raise LawCatalogTruthError(
                f"{server_id}: law_article {article_id} ownership differs "
                "from its source document; run normal catalog reconcile"
            )

        comparisons = {
            "article_ref": expected.get("article_ref"),
            "kind": expected["kind"],
            "code": expected.get("code"),
            "doc_title": expected["doc_title"],
            "full_text": expected["full_text"],
            "article_hash": source_article_hash(expected["full_text"]),
        }
        mismatched = [
            field
            for field, expected_value in comparisons.items()
            if actual.get(field) != expected_value
        ]
        if mismatched:
            raise LawCatalogTruthError(
                f"{server_id}: law_article {article_id} differs from parser "
                f"replay in {mismatched}; run normal catalog reconcile"
            )

        parser_title = expected.get("title")
        tombstone = bool(
            expected["kind"] == "article"
            and is_explicitly_empty_or_repealed_article(
                expected["full_text"]
            )
        )
        stored_title = actual.get("title")
        provenance = actual.get("title_provenance")
        if tombstone:
            if stored_title is not None or provenance is not None:
                raise LawCatalogTruthError(
                    f"{server_id}: tombstone article {article_id} retains a "
                    "title/provenance; run normal catalog reconcile"
                )
        elif parser_title is not None:
            if (
                stored_title != parser_title
                or provenance not in (
                    None,
                    _SOURCE_TITLE_PROVENANCE,
                    *(
                        ("legacy_unknown",)
                        if allow_legacy_title_attestation
                        else ()
                    ),
                )
            ):
                raise LawCatalogTruthError(
                    f"{server_id}: parser-owned title for article "
                    f"{article_id} is stale or has unsafe provenance; run "
                    "normal catalog reconcile"
                )
        elif stored_title is None:
            allowed_null_provenance = (
                (None, "legacy_unknown")
                if allow_legacy_title_attestation
                else (None,)
            )
            if provenance not in allowed_null_provenance:
                raise LawCatalogTruthError(
                    f"{server_id}: titleless article {article_id} has orphan "
                    "provenance; run normal catalog reconcile"
                )
        elif (
            expected["kind"] != "article"
            or provenance not in (
                (_GENERATED_TITLE_PROVENANCE, None, "legacy_unknown")
                if allow_legacy_title_attestation
                else (_GENERATED_TITLE_PROVENANCE,)
            )
            or _is_junk_title(stored_title)
        ):
            raise LawCatalogTruthError(
                f"{server_id}: generated title for article {article_id} is "
                "not safely owned by a parser-titleless article; run normal "
                "catalog reconcile or provenance attestation"
            )

        binding_rows.append(
            _binding_projection_row(
                actual,
                parser_title=parser_title,
                tombstone=tombstone,
            )
        )

    source_fingerprint = law_source_content_fingerprint(documents)
    source_projection = [
        {
            "id": row["id"],
            "server_id": row["server_id"],
            "source_thread_id": row.get("source_thread_id"),
            "title": row["title"],
            "content_length": len(row["content"].encode("utf-8")),
            "content_sha256": hashlib.sha256(
                row["content"].encode("utf-8")
            ).hexdigest(),
        }
        for row in documents
    ]
    catalog_projection_hash = _sha256(binding_rows)
    binding_payload = {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": parser_contract_version,
        "server_id": server_id,
        "source_documents": source_projection,
        "catalog_rows": binding_rows,
    }
    binding = {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": parser_contract_version,
        "server_id": server_id,
        "source_fingerprint": source_fingerprint,
        "documents": len(documents),
        "catalog_rows": len(binding_rows),
        "article_rows": sum(
            row["kind"] == "article" for row in binding_rows
        ),
        "tombstone_rows": sum(row["tombstone"] for row in binding_rows),
        "source_projection_hash": _sha256(source_projection),
        "catalog_projection_hash": catalog_projection_hash,
        "value": _sha256(binding_payload),
    }
    return VerifiedLawCatalogTruth(
        server_id=server_id,
        source_documents=tuple(documents),
        catalog_rows=tuple(actual_rows),
        binding=binding,
    )


def validate_law_source_catalog_binding(
    value: Any,
    *,
    expected_server_id: str | None = None,
) -> dict[str, Any]:
    """Validate the compact native-v3 binding and active parser version."""

    if not isinstance(value, Mapping):
        raise ValueError("source_catalog_binding must be an object")
    expected_fields = {
        "schema",
        "parser_contract_version",
        "server_id",
        "source_fingerprint",
        "documents",
        "catalog_rows",
        "article_rows",
        "tombstone_rows",
        "source_projection_hash",
        "catalog_projection_hash",
        "value",
    }
    if set(value) != expected_fields:
        raise ValueError(
            "source_catalog_binding fields do not match schema"
        )
    if value.get("schema") != LAW_SOURCE_CATALOG_BINDING_SCHEMA:
        raise ValueError("source_catalog_binding schema is not supported")
    if (
        value.get("parser_contract_version")
        != LAW_CATALOG_PARSER_CONTRACT_VERSION
    ):
        raise ValueError(
            "source_catalog_binding parser contract is not current"
        )
    server_id = value.get("server_id")
    if not isinstance(server_id, str) or not server_id:
        raise ValueError("source_catalog_binding server_id is invalid")
    if expected_server_id is not None and server_id != expected_server_id:
        raise ValueError(
            "source_catalog_binding belongs to a different server"
        )
    source_fingerprint = value.get("source_fingerprint")
    if not isinstance(source_fingerprint, Mapping):
        raise ValueError(
            "source_catalog_binding source_fingerprint is invalid"
        )
    fingerprint_fields = {
        "algorithm",
        "snapshot_version",
        "documents",
        "value",
    }
    if set(source_fingerprint) != fingerprint_fields:
        raise ValueError(
            "source_catalog_binding source_fingerprint fields are invalid"
        )
    if (
        source_fingerprint.get("algorithm") != "sha256"
    ):
        raise ValueError(
            "source_catalog_binding must use exact-content fingerprint v3"
        )
    fingerprint_version = source_fingerprint.get("snapshot_version")
    fingerprint_documents = source_fingerprint.get("documents")
    if (
        isinstance(fingerprint_version, bool)
        or not isinstance(fingerprint_version, int)
        or fingerprint_version != SOURCE_CONTENT_FINGERPRINT_VERSION
        or isinstance(fingerprint_documents, bool)
        or not isinstance(fingerprint_documents, int)
        or fingerprint_documents < 1
    ):
        raise ValueError(
            "source_catalog_binding must use exact-content fingerprint v3"
        )
    normalized_counts: dict[str, int] = {}
    for field in (
        "documents",
        "catalog_rows",
        "article_rows",
        "tombstone_rows",
    ):
        raw = value.get(field)
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or raw < (1 if field == "documents" else 0)
        ):
            raise ValueError(
                f"source_catalog_binding {field} is invalid"
            )
        normalized_counts[field] = raw
    if fingerprint_documents != normalized_counts["documents"]:
        raise ValueError(
            "source_catalog_binding document count differs from fingerprint"
        )
    if normalized_counts["article_rows"] > normalized_counts["catalog_rows"]:
        raise ValueError(
            "source_catalog_binding article count exceeds catalog count"
        )
    if normalized_counts["tombstone_rows"] > normalized_counts["article_rows"]:
        raise ValueError(
            "source_catalog_binding tombstone count exceeds article count"
        )
    normalized_hashes: dict[str, str] = {}
    for field in (
        "source_projection_hash",
        "catalog_projection_hash",
        "value",
    ):
        digest = value.get(field)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise ValueError(
                f"source_catalog_binding {field} must be a lowercase SHA-256"
            )
        normalized_hashes[field] = digest
    fingerprint_value = source_fingerprint.get("value")
    if (
        not isinstance(fingerprint_value, str)
        or _SHA256_RE.fullmatch(fingerprint_value) is None
    ):
        raise ValueError(
            "source_catalog_binding fingerprint value is invalid"
        )
    return {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": LAW_CATALOG_PARSER_CONTRACT_VERSION,
        "server_id": server_id,
        "source_fingerprint": {
            "algorithm": "sha256",
            "snapshot_version": SOURCE_CONTENT_FINGERPRINT_VERSION,
            "documents": normalized_counts["documents"],
            "value": fingerprint_value,
        },
        **normalized_counts,
        **normalized_hashes,
    }


async def load_verified_law_catalog_truth(
    queryable,
    server_id: str,
    *,
    lock_rows: bool = False,
    allow_legacy_title_attestation: bool = False,
) -> VerifiedLawCatalogTruth:
    """Load and verify a complete server snapshot from an asyncpg-like API."""

    if not isinstance(lock_rows, bool):
        raise TypeError("lock_rows must be bool")
    if not isinstance(allow_legacy_title_attestation, bool):
        raise TypeError("allow_legacy_title_attestation must be bool")
    document_lock = "FOR SHARE" if lock_rows else ""
    catalog_lock = "FOR SHARE OF a" if lock_rows else ""
    source_documents = await queryable.fetch(
        f"""
        SELECT id, server_id, source_thread_id, title, content, content_hash,
               derived_sync_pending
        FROM law_documents
        WHERE server_id = $1
        ORDER BY id
        {document_lock}
        """,
        server_id,
    )
    # Fail on missing/pending/unsafe source before even consulting the
    # persisted catalog.  This is also the mandatory pre-network gate used by
    # completion/export/Gemini.
    normalized_source_documents = _normal_source_documents(
        server_id,
        source_documents,
    )
    catalog_rows = await queryable.fetch(
        f"""
        SELECT a.id, a.server_id, d.server_id AS parent_server_id,
               a.law_document_id, a.source_order, a.article_ref, a.kind,
               a.code, a.title, a.doc_title, a.full_text, a.article_hash,
               a.enrich_hash, a.penalty_summary, a.penalty_json,
               a.title_provenance, a.enrichment_context_hash,
               a.enrichment_output_hash
        FROM law_articles a
        LEFT JOIN law_documents d ON d.id = a.law_document_id
        WHERE a.server_id = $1 OR d.server_id = $1
        ORDER BY a.law_document_id, a.source_order, a.id
        {catalog_lock}
        """,
        server_id,
    )
    return verify_law_documents_catalog_truth(
        server_id,
        normalized_source_documents,
        catalog_rows,
        allow_legacy_title_attestation=allow_legacy_title_attestation,
    )
