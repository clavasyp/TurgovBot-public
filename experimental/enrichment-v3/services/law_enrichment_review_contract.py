"""Pure source contract shared by offline review and the live writer.

The reviewed enrichment workflow has two deliberately separated stages:

* an immutable, database-free review of exported source; and
* a live CAS writer which rebuilds the current catalog state.

Both stages must apply the same structural rules.  This module serializes the
source-only subset needed by those rules (parser-owned title and ancestry,
tombstone state, and the complete exact-body plan), binds it with a canonical
SHA-256, and validates reviewed output without a database or model client.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from services.law_enrichment_context import (
    EnrichmentContextInputError,
    build_source_only_enrichment_projection,
)
from services.law_enrichment_exact_duplicates import (
    ExactDuplicateError,
    ExactDuplicateGroup,
    ExactDuplicateMember,
    ExactDuplicatePlan,
    StructuralComponent,
    StructuralPath,
    build_exact_duplicate_plan,
    close_targets_over_exact_duplicate_groups,
    extract_proven_operative_body,
    validate_exact_duplicate_group_response,
)
from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
    validate_enrichment_source_evidence,
)


REVIEW_SOURCE_PROJECTION_SCHEMA = (
    "law-enrichment-reviewed-source-projection-v2"
)
_SHA256_HEX = frozenset("0123456789abcdef")
_SOURCE_FIELDS = {
    "id",
    "law_document_id",
    "source_order",
    "kind",
    "code",
    "article_ref",
    "source_title",
    "document",
    "text",
    "article_hash",
}
_SOURCE_CATALOG_FIELDS = {
    "id",
    "law_document_id",
    "source_order",
    "article_ref",
    "document",
    "text",
    "article_hash",
}
_ANCESTRY_FIELDS = {"section", "chapter"}
_STRUCTURAL_FIELDS = {
    "id",
    "source_order",
    "kind",
    "code",
    "article_ref",
    "source_title",
}
_ARTICLE_FIELDS = {
    "id",
    "article_hash",
    "source",
    "structural_ancestry",
    "tombstone",
    "exact_group_key",
}
_GROUP_FIELDS = {
    "key",
    "body_sha256",
    "body_length",
    "scope_level",
    "members",
}
_MEMBER_FIELDS = {
    "article_id",
    "law_document_id",
    "source_order",
    "article_hash",
    "path",
    "scope_text",
    "title_scope_prefix",
    "synonym_scope_prefix",
}
_PATH_FIELDS = {
    "law_document_id",
    "document_title",
    "section",
    "chapter",
}
_COMPONENT_FIELDS = {
    "kind",
    "row_id",
    "law_document_id",
    "source_order",
    "article_ref",
    "title",
}


class ReviewSourceContractError(ValueError):
    """The exported or live reviewed-source contract is inconsistent."""


CatalogFieldsResolver = Callable[
    [str | None, str],
    tuple[str, str | None, str | None],
]
ArticleHashResolver = Callable[[str], str]


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewSourceContractError(
            f"{field} must be a positive integer"
        )
    return value


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReviewSourceContractError(
            f"{field} must be a non-negative integer"
        )
    return value


def _optional_text(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ReviewSourceContractError(
            f"{field} must be a string or null"
        )
    return value


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReviewSourceContractError(
            f"{field} must be a non-empty string"
        )
    return value


def _component_payload(
    component: StructuralComponent | None,
) -> dict[str, Any] | None:
    if component is None:
        return None
    return {
        "kind": component.kind,
        "row_id": int(component.row_id),
        "law_document_id": int(component.law_document_id),
        "source_order": int(component.source_order),
        "article_ref": component.article_ref,
        "title": component.title,
    }


def _path_payload(path: StructuralPath) -> dict[str, Any]:
    return {
        "law_document_id": int(path.law_document_id),
        "document_title": path.document_title,
        "section": _component_payload(path.section),
        "chapter": _component_payload(path.chapter),
    }


def _group_payload(group: ExactDuplicateGroup) -> dict[str, Any]:
    return {
        "key": group.key,
        "body_sha256": group.body_sha256,
        "body_length": int(group.body_length),
        "scope_level": group.scope_level,
        "members": [
            {
                "article_id": int(member.article_id),
                "law_document_id": int(member.law_document_id),
                "source_order": int(member.source_order),
                "article_hash": member.article_hash,
                "path": _path_payload(member.path),
                "scope_text": member.scope_text,
                "title_scope_prefix": member.title_scope_prefix,
                "synonym_scope_prefix": member.synonym_scope_prefix,
            }
            for member in group.members
        ],
    }


def _group_sort_key(
    group: ExactDuplicateGroup,
) -> tuple[tuple[int, int, int], str]:
    return (
        min(
            (
                int(member.law_document_id),
                int(member.source_order),
                int(member.article_id),
            )
            for member in group.members
        ),
        group.key,
    )


def _seal_projection(
    articles: Sequence[Mapping[str, Any]],
    groups: Sequence[Mapping[str, Any]],
    source_catalog: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    body = {
        "schema": REVIEW_SOURCE_PROJECTION_SCHEMA,
        "source_catalog": [dict(row) for row in source_catalog],
        "articles": [dict(article) for article in articles],
        "exact_groups": [dict(group) for group in groups],
    }
    return {
        **body,
        "projection_hash": _canonical_sha256(body),
    }


def _normalized_source_catalog(
    rows: Any,
) -> list[dict[str, Any]]:
    """Return the hash-bound raw catalog used to rederive all review facts.

    The mixed ``law_articles.title`` column is intentionally absent.  Article
    and structural titles are both recomputed by the shared parser resolver
    from ``article_ref`` and ``text`` before they may influence ownership,
    ancestry or an exact-body scope prefix.
    """

    if not isinstance(rows, list) or not rows:
        raise ReviewSourceContractError(
            "review source projection source_catalog must be a non-empty "
            "array"
        )
    normalized: list[dict[str, Any]] = []
    ids: set[int] = set()
    positions: set[tuple[int, int]] = set()
    for index, raw_row in enumerate(rows):
        field = f"projection source_catalog[{index}]"
        if (
            not isinstance(raw_row, Mapping)
            or set(raw_row) != _SOURCE_CATALOG_FIELDS
        ):
            raise ReviewSourceContractError(
                f"{field} fields do not match schema"
            )
        row_id = _positive_int(raw_row.get("id"), field=f"{field}.id")
        if row_id in ids:
            raise ReviewSourceContractError(
                f"review source projection repeats catalog row {row_id}"
            )
        ids.add(row_id)
        document_id = _positive_int(
            raw_row.get("law_document_id"),
            field=f"{field}.law_document_id",
        )
        source_order = _non_negative_int(
            raw_row.get("source_order"),
            field=f"{field}.source_order",
        )
        position = (document_id, source_order)
        if position in positions:
            raise ReviewSourceContractError(
                "review source projection repeats a catalog position"
            )
        positions.add(position)
        article_ref = _optional_text(
            raw_row.get("article_ref"),
            field=f"{field}.article_ref",
        )
        document = _optional_text(
            raw_row.get("document"),
            field=f"{field}.document",
        )
        text = raw_row.get("text")
        if not isinstance(text, str):
            raise ReviewSourceContractError(
                f"{field}.text must be a string"
            )
        article_hash = _required_text(
            raw_row.get("article_hash"),
            field=f"{field}.article_hash",
        )
        normalized.append({
            "id": row_id,
            "law_document_id": document_id,
            "source_order": source_order,
            "article_ref": article_ref,
            "document": document,
            "text": text,
            "article_hash": article_hash,
        })
    normalized.sort(
        key=lambda row: (
            int(row["law_document_id"]),
            int(row["source_order"]),
            int(row["id"]),
        )
    )
    return normalized


def _source_catalog_from_rows(
    catalog_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(catalog_rows, (str, bytes)) or not isinstance(
        catalog_rows,
        Sequence,
    ):
        raise ReviewSourceContractError(
            "catalog_rows must be a sequence"
        )
    raw_rows: list[dict[str, Any]] = []
    for index, row in enumerate(catalog_rows):
        if not isinstance(row, Mapping):
            raise ReviewSourceContractError(
                f"catalog row {index} is not an object"
            )
        raw_rows.append({
            "id": row.get("id"),
            "law_document_id": row.get("law_document_id"),
            "source_order": row.get("source_order"),
            "article_ref": row.get("article_ref"),
            "document": row.get("doc_title"),
            "text": row.get("full_text"),
            "article_hash": row.get("article_hash"),
        })
    return _normalized_source_catalog(raw_rows)


def _catalog_rows_from_source_catalog(
    source_catalog: Sequence[Mapping[str, Any]],
    *,
    catalog_fields_resolver: CatalogFieldsResolver,
    article_hash_resolver: ArticleHashResolver,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_row in source_catalog:
        row_id = int(raw_row["id"])
        article_ref = raw_row.get("article_ref")
        text = str(raw_row["text"])
        try:
            parser_fields = catalog_fields_resolver(article_ref, text)
        except Exception as exc:
            raise ReviewSourceContractError(
                f"catalog field derivation failed for catalog row {row_id}"
            ) from exc
        if (
            not isinstance(parser_fields, tuple)
            or len(parser_fields) != 3
        ):
            raise ReviewSourceContractError(
                "catalog_fields_resolver must return "
                "(kind, code, title)"
            )
        kind, code, parser_title = parser_fields
        if not isinstance(kind, str) or not kind.strip():
            raise ReviewSourceContractError(
                "catalog_fields_resolver returned an invalid kind"
            )
        kind = kind.strip().lower()
        if code is not None and not isinstance(code, str):
            raise ReviewSourceContractError(
                "catalog_fields_resolver returned a non-text code"
            )
        if parser_title is not None and not isinstance(parser_title, str):
            raise ReviewSourceContractError(
                "catalog_fields_resolver returned a non-text title"
            )
        try:
            derived_article_hash = article_hash_resolver(text)
        except Exception as exc:
            raise ReviewSourceContractError(
                f"article hash derivation failed for catalog row {row_id}"
            ) from exc
        if (
            not isinstance(derived_article_hash, str)
            or not derived_article_hash
        ):
            raise ReviewSourceContractError(
                "article_hash_resolver must return a non-empty string"
            )
        if derived_article_hash != raw_row["article_hash"]:
            raise ReviewSourceContractError(
                f"catalog row {row_id} article_hash differs from the "
                "canonical hash of its source text"
            )
        rows.append({
            "id": row_id,
            "law_document_id": int(raw_row["law_document_id"]),
            "source_order": int(raw_row["source_order"]),
            "kind": kind,
            "code": code,
            "article_ref": article_ref,
            # Structural titles are parser-owned.  Article titles remain
            # deliberately absent here; their parser-derived value is
            # recomputed independently by the context projection.
            "title": (
                parser_title
                if kind in {"section", "chapter"}
                else None
            ),
            "doc_title": raw_row.get("document"),
            "full_text": text,
            "article_hash": derived_article_hash,
        })
    return rows


def build_review_source_projection(
    catalog_rows: Sequence[Mapping[str, Any]],
    target_ids: Sequence[int],
    *,
    catalog_fields_resolver: CatalogFieldsResolver,
    article_hash_resolver: ArticleHashResolver,
    is_tombstone: Callable[[Any], bool],
    canonicalize_scope: Callable[[str], Any],
) -> dict[str, Any]:
    """Build the immutable source-only proof for one complete target closure."""

    if not callable(catalog_fields_resolver):
        raise ReviewSourceContractError(
            "catalog_fields_resolver must be callable"
        )
    if not callable(article_hash_resolver):
        raise ReviewSourceContractError(
            "article_hash_resolver must be callable"
        )
    if not callable(is_tombstone):
        raise ReviewSourceContractError("is_tombstone must be callable")
    if not callable(canonicalize_scope):
        raise ReviewSourceContractError(
            "canonicalize_scope must be callable"
        )
    if isinstance(target_ids, (str, bytes)) or not isinstance(
        target_ids,
        Sequence,
    ):
        raise ReviewSourceContractError(
            "target_ids must be a sequence of integer article ids"
        )
    normalized_target_ids: list[int] = []
    seen_ids: set[int] = set()
    for raw_id in target_ids:
        article_id = _positive_int(raw_id, field="target article id")
        if article_id in seen_ids:
            raise ReviewSourceContractError(
                f"target_ids repeats article {article_id}"
            )
        seen_ids.add(article_id)
        normalized_target_ids.append(article_id)
    if not normalized_target_ids:
        raise ReviewSourceContractError(
            "target_ids must contain at least one article"
        )

    source_catalog = _source_catalog_from_rows(catalog_rows)
    derived_catalog_rows = _catalog_rows_from_source_catalog(
        source_catalog,
        catalog_fields_resolver=catalog_fields_resolver,
        article_hash_resolver=article_hash_resolver,
    )

    def _source_title_resolver(
        article_ref: str | None,
        full_text: str,
    ) -> str | None:
        try:
            fields = catalog_fields_resolver(article_ref, full_text)
        except Exception as exc:
            raise ReviewSourceContractError(
                "catalog field derivation failed"
            ) from exc
        if not isinstance(fields, tuple) or len(fields) != 3:
            raise ReviewSourceContractError(
                "catalog_fields_resolver must return "
                "(kind, code, title)"
            )
        return fields[2]

    def _is_substantive(row: Mapping[str, Any]) -> bool:
        try:
            tombstone = is_tombstone(row.get("full_text"))
        except Exception as exc:
            raise ReviewSourceContractError(
                f"tombstone classification failed for article "
                f"{row.get('id')!r}"
            ) from exc
        if not isinstance(tombstone, bool):
            raise ReviewSourceContractError(
                "is_tombstone must return bool for every article"
            )
        return not tombstone

    try:
        plan = build_exact_duplicate_plan(
            derived_catalog_rows,
            is_substantive_article=_is_substantive,
            canonicalize_scope=canonicalize_scope,
        )
        projections = build_source_only_enrichment_projection(
            derived_catalog_rows,
            source_title_resolver=_source_title_resolver,
        )
    except (ExactDuplicateError, EnrichmentContextInputError) as exc:
        raise ReviewSourceContractError(
            "cannot build source-only exact-body review projection"
        ) from exc

    target_set = frozenset(normalized_target_ids)
    missing_ids = sorted(target_set - set(projections))
    if missing_ids:
        raise ReviewSourceContractError(
            "review targets are missing/non-article catalog rows: "
            f"{missing_ids}"
        )
    closed_ids = close_targets_over_exact_duplicate_groups(
        target_set,
        plan,
    )
    if closed_ids != target_set:
        raise ReviewSourceContractError(
            "review target set partially covers an exact-body group: "
            f"present={sorted(target_set)}, required={sorted(closed_ids)}"
        )

    groups = sorted(
        (
            group
            for group in plan.groups
            if not target_set.isdisjoint(group.article_ids)
        ),
        key=_group_sort_key,
    )
    group_key_by_article = {
        member.article_id: group.key
        for group in groups
        for member in group.members
    }
    ordered_ids = sorted(
        target_set,
        key=lambda article_id: (
            int(projections[article_id]["source"]["law_document_id"]),
            int(projections[article_id]["source"]["source_order"]),
            article_id,
        ),
    )
    articles: list[dict[str, Any]] = []
    for article_id in ordered_ids:
        projection = projections[article_id]
        source = dict(projection["source"])
        try:
            tombstone = is_tombstone(source["text"])
        except Exception as exc:
            raise ReviewSourceContractError(
                f"tombstone classification failed for article {article_id}"
            ) from exc
        if not isinstance(tombstone, bool):
            raise ReviewSourceContractError(
                "is_tombstone must return bool for every article"
            )
        articles.append({
            "id": article_id,
            "article_hash": source["article_hash"],
            "source": source,
            "structural_ancestry": {
                "section": (
                    dict(projection["structural_ancestry"]["section"])
                    if projection["structural_ancestry"]["section"]
                    is not None
                    else None
                ),
                "chapter": (
                    dict(projection["structural_ancestry"]["chapter"])
                    if projection["structural_ancestry"]["chapter"]
                    is not None
                    else None
                ),
            },
            "tombstone": tombstone,
            "exact_group_key": group_key_by_article.get(article_id),
        })
    return _seal_projection(
        articles,
        [_group_payload(group) for group in groups],
        source_catalog,
    )


def _parse_component(
    value: Any,
    *,
    field: str,
) -> StructuralComponent | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _COMPONENT_FIELDS:
        raise ReviewSourceContractError(
            f"{field} fields do not match the structural component schema"
        )
    kind = _required_text(value.get("kind"), field=f"{field}.kind")
    if kind not in {"section", "chapter"}:
        raise ReviewSourceContractError(
            f"{field}.kind must be section or chapter"
        )
    return StructuralComponent(
        kind=kind,
        row_id=_positive_int(
            value.get("row_id"),
            field=f"{field}.row_id",
        ),
        law_document_id=_positive_int(
            value.get("law_document_id"),
            field=f"{field}.law_document_id",
        ),
        source_order=_non_negative_int(
            value.get("source_order"),
            field=f"{field}.source_order",
        ),
        article_ref=_optional_text(
            value.get("article_ref"),
            field=f"{field}.article_ref",
        ),
        title=_optional_text(
            value.get("title"),
            field=f"{field}.title",
        ),
    )


def _parse_path(value: Any, *, field: str) -> StructuralPath:
    if not isinstance(value, Mapping) or set(value) != _PATH_FIELDS:
        raise ReviewSourceContractError(
            f"{field} fields do not match the structural path schema"
        )
    return StructuralPath(
        law_document_id=_positive_int(
            value.get("law_document_id"),
            field=f"{field}.law_document_id",
        ),
        document_title=_optional_text(
            value.get("document_title"),
            field=f"{field}.document_title",
        ),
        section=_parse_component(
            value.get("section"),
            field=f"{field}.section",
        ),
        chapter=_parse_component(
            value.get("chapter"),
            field=f"{field}.chapter",
        ),
    )


def _parse_structural_projection(
    value: Any,
    *,
    field: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _STRUCTURAL_FIELDS:
        raise ReviewSourceContractError(
            f"{field} fields do not match the ancestry schema"
        )
    kind = _required_text(value.get("kind"), field=f"{field}.kind")
    if kind not in {"section", "chapter"}:
        raise ReviewSourceContractError(
            f"{field}.kind must be section or chapter"
        )
    return {
        "id": _positive_int(value.get("id"), field=f"{field}.id"),
        "source_order": _non_negative_int(
            value.get("source_order"),
            field=f"{field}.source_order",
        ),
        "kind": kind,
        "code": _optional_text(value.get("code"), field=f"{field}.code"),
        "article_ref": _optional_text(
            value.get("article_ref"),
            field=f"{field}.article_ref",
        ),
        "source_title": _optional_text(
            value.get("source_title"),
            field=f"{field}.source_title",
        ),
    }


def _ancestry_matches_path(
    ancestry: Mapping[str, Any],
    path: StructuralPath,
    *,
    source: Mapping[str, Any],
    article_id: int,
) -> None:
    if int(path.law_document_id) != int(source["law_document_id"]):
        raise ReviewSourceContractError(
            f"exact group path has wrong document for article {article_id}"
        )
    if path.document_title != source.get("document"):
        raise ReviewSourceContractError(
            f"exact group path has wrong document title for article "
            f"{article_id}"
        )
    for name in ("section", "chapter"):
        compact = ancestry[name]
        component = getattr(path, name)
        if compact is None and component is None:
            continue
        if compact is None or component is None:
            raise ReviewSourceContractError(
                f"exact group path {name} differs from ancestry for article "
                f"{article_id}"
            )
        expected = {
            "id": int(component.row_id),
            "source_order": int(component.source_order),
            "kind": component.kind,
            # ``code`` is not part of the planner component.  It is still
            # bound by the source ancestry projection but cannot influence a
            # scope prefix without article_ref/title evidence.
            "article_ref": component.article_ref,
            "source_title": component.title,
        }
        for key, expected_value in expected.items():
            if compact.get(key) != expected_value:
                raise ReviewSourceContractError(
                    f"exact group path {name}.{key} differs from ancestry "
                    f"for article {article_id}"
                )


def _normalized_projection(
    value: Any,
) -> tuple[
    dict[str, Any],
    dict[int, dict[str, Any]],
    tuple[ExactDuplicateGroup, ...],
]:
    if not isinstance(value, Mapping):
        raise ReviewSourceContractError(
            "review source projection must be an object"
        )
    expected_fields = {
        "schema",
        "source_catalog",
        "articles",
        "exact_groups",
        "projection_hash",
    }
    if set(value) != expected_fields:
        raise ReviewSourceContractError(
            "review source projection fields do not match schema"
        )
    if value.get("schema") != REVIEW_SOURCE_PROJECTION_SCHEMA:
        raise ReviewSourceContractError(
            "unsupported review source projection schema"
        )
    stored_hash = value.get("projection_hash")
    if not _is_sha256(stored_hash):
        raise ReviewSourceContractError(
            "review source projection hash must be a lowercase SHA-256"
        )
    hash_body = dict(value)
    hash_body.pop("projection_hash")
    if stored_hash != _canonical_sha256(hash_body):
        raise ReviewSourceContractError(
            "review source projection hash mismatch"
        )
    source_catalog = _normalized_source_catalog(
        value.get("source_catalog")
    )

    raw_articles = value.get("articles")
    if not isinstance(raw_articles, list) or not raw_articles:
        raise ReviewSourceContractError(
            "review source projection articles must be a non-empty array"
        )
    articles: dict[int, dict[str, Any]] = {}
    article_order: list[int] = []
    positions: set[tuple[int, int]] = set()
    for index, raw_article in enumerate(raw_articles):
        field = f"projection article {index}"
        if (
            not isinstance(raw_article, Mapping)
            or set(raw_article) != _ARTICLE_FIELDS
        ):
            raise ReviewSourceContractError(
                f"{field} fields do not match schema"
            )
        article_id = _positive_int(
            raw_article.get("id"),
            field=f"{field}.id",
        )
        if article_id in articles:
            raise ReviewSourceContractError(
                f"review source projection repeats article {article_id}"
            )
        article_hash = _required_text(
            raw_article.get("article_hash"),
            field=f"{field}.article_hash",
        )
        source = raw_article.get("source")
        if not isinstance(source, Mapping) or set(source) != _SOURCE_FIELDS:
            raise ReviewSourceContractError(
                f"{field}.source fields do not match schema"
            )
        source_id = _positive_int(
            source.get("id"),
            field=f"{field}.source.id",
        )
        document_id = _positive_int(
            source.get("law_document_id"),
            field=f"{field}.source.law_document_id",
        )
        source_order = _non_negative_int(
            source.get("source_order"),
            field=f"{field}.source.source_order",
        )
        if source_id != article_id or source.get("article_hash") != article_hash:
            raise ReviewSourceContractError(
                f"{field} source identity mismatch"
            )
        if source.get("kind") != "article":
            raise ReviewSourceContractError(
                f"{field}.source.kind must be article"
            )
        position = (document_id, source_order)
        if position in positions:
            raise ReviewSourceContractError(
                "review source projection repeats a catalog position"
            )
        positions.add(position)
        for name in (
            "code",
            "article_ref",
            "source_title",
            "document",
        ):
            _optional_text(
                source.get(name),
                field=f"{field}.source.{name}",
            )
        if not isinstance(source.get("text"), str):
            raise ReviewSourceContractError(
                f"{field}.source.text must be a string"
            )
        ancestry = raw_article.get("structural_ancestry")
        if (
            not isinstance(ancestry, Mapping)
            or set(ancestry) != _ANCESTRY_FIELDS
        ):
            raise ReviewSourceContractError(
                f"{field}.structural_ancestry fields do not match schema"
            )
        normalized_ancestry = {
            name: _parse_structural_projection(
                ancestry.get(name),
                field=f"{field}.structural_ancestry.{name}",
            )
            for name in ("section", "chapter")
        }
        tombstone = raw_article.get("tombstone")
        if not isinstance(tombstone, bool):
            raise ReviewSourceContractError(
                f"{field}.tombstone must be bool"
            )
        if tombstone and source.get("source_title") is not None:
            raise ReviewSourceContractError(
                f"{field} tombstone cannot expose a source title"
            )
        exact_group_key = raw_article.get("exact_group_key")
        if exact_group_key is not None:
            _required_text(
                exact_group_key,
                field=f"{field}.exact_group_key",
            )
        normalized_source = dict(source)
        normalized_source["id"] = source_id
        normalized_source["law_document_id"] = document_id
        normalized_source["source_order"] = source_order
        normalized_article = {
            "id": article_id,
            "article_hash": article_hash,
            "source": normalized_source,
            "structural_ancestry": normalized_ancestry,
            "tombstone": tombstone,
            "exact_group_key": exact_group_key,
        }
        articles[article_id] = normalized_article
        article_order.append(article_id)

    raw_groups = value.get("exact_groups")
    if not isinstance(raw_groups, list):
        raise ReviewSourceContractError(
            "review source projection exact_groups must be an array"
        )
    groups: list[ExactDuplicateGroup] = []
    group_keys: set[str] = set()
    grouped_ids: set[int] = set()
    for group_index, raw_group in enumerate(raw_groups):
        field = f"projection exact group {group_index}"
        if (
            not isinstance(raw_group, Mapping)
            or set(raw_group) != _GROUP_FIELDS
        ):
            raise ReviewSourceContractError(
                f"{field} fields do not match schema"
            )
        key = _required_text(raw_group.get("key"), field=f"{field}.key")
        if key in group_keys:
            raise ReviewSourceContractError(
                f"review source projection repeats exact group {key!r}"
            )
        group_keys.add(key)
        body_sha256 = raw_group.get("body_sha256")
        if not _is_sha256(body_sha256):
            raise ReviewSourceContractError(
                f"{field}.body_sha256 must be a lowercase SHA-256"
            )
        body_length = _positive_int(
            raw_group.get("body_length"),
            field=f"{field}.body_length",
        )
        scope_level = _required_text(
            raw_group.get("scope_level"),
            field=f"{field}.scope_level",
        )
        raw_members = raw_group.get("members")
        if not isinstance(raw_members, list) or len(raw_members) < 2:
            raise ReviewSourceContractError(
                f"{field}.members must contain at least two articles"
            )
        members: list[ExactDuplicateMember] = []
        bodies: list[bytes] = []
        for member_index, raw_member in enumerate(raw_members):
            member_field = f"{field}.members[{member_index}]"
            if (
                not isinstance(raw_member, Mapping)
                or set(raw_member) != _MEMBER_FIELDS
            ):
                raise ReviewSourceContractError(
                    f"{member_field} fields do not match schema"
                )
            article_id = _positive_int(
                raw_member.get("article_id"),
                field=f"{member_field}.article_id",
            )
            if article_id in grouped_ids:
                raise ReviewSourceContractError(
                    f"article {article_id} occurs in multiple exact groups"
                )
            grouped_ids.add(article_id)
            article = articles.get(article_id)
            if article is None:
                raise ReviewSourceContractError(
                    f"{member_field} references an article outside this "
                    "projection"
                )
            law_document_id = _positive_int(
                raw_member.get("law_document_id"),
                field=f"{member_field}.law_document_id",
            )
            source_order = _non_negative_int(
                raw_member.get("source_order"),
                field=f"{member_field}.source_order",
            )
            article_hash = _required_text(
                raw_member.get("article_hash"),
                field=f"{member_field}.article_hash",
            )
            path = _parse_path(
                raw_member.get("path"),
                field=f"{member_field}.path",
            )
            member = ExactDuplicateMember(
                article_id=article_id,
                law_document_id=law_document_id,
                source_order=source_order,
                article_hash=article_hash,
                path=path,
                scope_text=_required_text(
                    raw_member.get("scope_text"),
                    field=f"{member_field}.scope_text",
                ),
                title_scope_prefix=_required_text(
                    raw_member.get("title_scope_prefix"),
                    field=f"{member_field}.title_scope_prefix",
                ),
                synonym_scope_prefix=_required_text(
                    raw_member.get("synonym_scope_prefix"),
                    field=f"{member_field}.synonym_scope_prefix",
                ),
            )
            source = article["source"]
            if (
                article["exact_group_key"] != key
                or law_document_id != source["law_document_id"]
                or source_order != source["source_order"]
                or article_hash != article["article_hash"]
            ):
                raise ReviewSourceContractError(
                    f"{member_field} identity differs from its source article"
                )
            _ancestry_matches_path(
                article["structural_ancestry"],
                path,
                source=source,
                article_id=article_id,
            )
            body = extract_proven_operative_body({
                "article_ref": source.get("article_ref"),
                "full_text": source["text"],
            })
            if body is None:
                raise ReviewSourceContractError(
                    f"{member_field} has no proven operative body"
                )
            bodies.append(body)
            members.append(member)
        first_body = bodies[0]
        if (
            any(body != first_body for body in bodies[1:])
            or hashlib.sha256(first_body).hexdigest() != body_sha256
            or len(first_body) != body_length
        ):
            raise ReviewSourceContractError(
                f"{field} body identity differs from source"
            )
        groups.append(
            ExactDuplicateGroup(
                key=key,
                body_sha256=body_sha256,
                body_length=body_length,
                scope_level=scope_level,
                members=tuple(members),
            )
        )

    referenced_group_ids = {
        article_id
        for article_id, article in articles.items()
        if article["exact_group_key"] is not None
    }
    if referenced_group_ids != grouped_ids:
        raise ReviewSourceContractError(
            "review source projection exact-group coverage is incomplete"
        )
    normalized = {
        "schema": REVIEW_SOURCE_PROJECTION_SCHEMA,
        "source_catalog": source_catalog,
        "articles": [articles[article_id] for article_id in article_order],
        "exact_groups": [
            _group_payload(group)
            for group in groups
        ],
        "projection_hash": stored_hash,
    }
    return normalized, articles, tuple(groups)


def _validate_projection_truth(
    normalized: Mapping[str, Any],
    *,
    catalog_fields_resolver: CatalogFieldsResolver,
    article_hash_resolver: ArticleHashResolver,
    is_tombstone: Callable[[Any], bool],
    canonicalize_scope: Callable[[str], Any],
) -> None:
    """Rederive every non-raw field from the hash-bound source catalog."""

    if not callable(catalog_fields_resolver):
        raise ReviewSourceContractError(
            "catalog_fields_resolver must be callable"
        )
    if not callable(article_hash_resolver):
        raise ReviewSourceContractError(
            "article_hash_resolver must be callable"
        )
    if not callable(is_tombstone):
        raise ReviewSourceContractError("is_tombstone must be callable")
    if not callable(canonicalize_scope):
        raise ReviewSourceContractError(
            "canonicalize_scope must be callable"
        )
    target_ids = [
        int(article["id"])
        for article in normalized["articles"]
    ]
    try:
        catalog_rows = _catalog_rows_from_source_catalog(
            normalized["source_catalog"],
            catalog_fields_resolver=catalog_fields_resolver,
            article_hash_resolver=article_hash_resolver,
        )
        rederived = build_review_source_projection(
            catalog_rows,
            target_ids,
            catalog_fields_resolver=catalog_fields_resolver,
            article_hash_resolver=article_hash_resolver,
            is_tombstone=is_tombstone,
            canonicalize_scope=canonicalize_scope,
        )
    except ReviewSourceContractError as exc:
        raise ReviewSourceContractError(
            "review source projection cannot be rederived from its raw "
            f"catalog: {exc}"
        ) from exc
    comparisons = {
        "source_catalog": (
            rederived["source_catalog"],
            normalized["source_catalog"],
        ),
        # Batch article order is an immutable review/manifest fact, but it is
        # not a parser-derived catalog fact.  Compare the exact article
        # payloads by identity so a legitimate source-order-independent batch
        # remains valid while every derived field is still reproved.
        "articles": (
            {
                int(article["id"]): article
                for article in rederived["articles"]
            },
            {
                int(article["id"]): article
                for article in normalized["articles"]
            },
        ),
        "exact_groups": (
            rederived["exact_groups"],
            normalized["exact_groups"],
        ),
    }
    for field, (expected, actual) in comparisons.items():
        if expected != actual:
            raise ReviewSourceContractError(
                "review source projection derived fields differ from its "
                f"raw catalog: {field}"
            )


def select_review_source_projection(
    projection: Mapping[str, Any],
    article_ids: Sequence[int],
) -> dict[str, Any]:
    """Return one independently hashed, exact-group-complete batch subset."""

    _normalized, articles, groups = _normalized_projection(projection)
    selected_order: list[int] = []
    selected: set[int] = set()
    for raw_id in article_ids:
        article_id = _positive_int(raw_id, field="selected article id")
        if article_id in selected:
            raise ReviewSourceContractError(
                f"selected article ids repeat {article_id}"
            )
        if article_id not in articles:
            raise ReviewSourceContractError(
                f"selected article {article_id} is outside the projection"
            )
        selected.add(article_id)
        selected_order.append(article_id)
    if not selected_order:
        raise ReviewSourceContractError(
            "selected article ids must not be empty"
        )
    selected_groups: list[ExactDuplicateGroup] = []
    for group in groups:
        member_ids = set(group.article_ids)
        overlap = member_ids & selected
        if overlap and overlap != member_ids:
            raise ReviewSourceContractError(
                "batch selection splits exact-body group "
                f"{group.key!r}: present={sorted(overlap)}, "
                f"required={sorted(member_ids)}"
            )
        if overlap:
            selected_groups.append(group)
    return _seal_projection(
        [articles[article_id] for article_id in selected_order],
        [_group_payload(group) for group in selected_groups],
        _normalized["source_catalog"],
    )


def combine_review_source_projections(
    projections: Sequence[Mapping[str, Any]],
    *,
    catalog_fields_resolver: CatalogFieldsResolver,
    article_hash_resolver: ArticleHashResolver,
    is_tombstone: Callable[[Any], bool],
    canonicalize_scope: Callable[[str], Any],
) -> dict[str, Any]:
    """Merge batch projections and reject split/repeated exact groups."""

    if not projections:
        raise ReviewSourceContractError(
            "at least one batch projection is required"
        )
    articles: dict[int, dict[str, Any]] = {}
    groups_by_key: dict[str, ExactDuplicateGroup] = {}
    source_catalog: list[dict[str, Any]] | None = None
    for projection in projections:
        normalized, batch_articles, batch_groups = _normalized_projection(
            projection
        )
        _validate_projection_truth(
            normalized,
            catalog_fields_resolver=catalog_fields_resolver,
            article_hash_resolver=article_hash_resolver,
            is_tombstone=is_tombstone,
            canonicalize_scope=canonicalize_scope,
        )
        batch_source_catalog = normalized["source_catalog"]
        if source_catalog is None:
            source_catalog = batch_source_catalog
        elif source_catalog != batch_source_catalog:
            raise ReviewSourceContractError(
                "batch projections do not carry the same complete raw "
                "source catalog"
            )
        duplicates = sorted(set(articles) & set(batch_articles))
        if duplicates:
            raise ReviewSourceContractError(
                f"batch projections repeat article ids: {duplicates}"
            )
        articles.update(batch_articles)
        for group in batch_groups:
            if group.key in groups_by_key:
                raise ReviewSourceContractError(
                    f"batch projections repeat exact group {group.key!r}"
                )
            groups_by_key[group.key] = group
    ordered_articles = sorted(
        articles.values(),
        key=lambda article: (
            int(article["source"]["law_document_id"]),
            int(article["source"]["source_order"]),
            int(article["id"]),
        ),
    )
    ordered_groups = sorted(
        groups_by_key.values(),
        key=_group_sort_key,
    )
    combined = _seal_projection(
        ordered_articles,
        [_group_payload(group) for group in ordered_groups],
        source_catalog or (),
    )
    # Re-run the complete shape/topology proof over the aggregate, not only
    # over its independently valid batches.
    normalized, _articles, _groups = _normalized_projection(combined)
    _validate_projection_truth(
        normalized,
        catalog_fields_resolver=catalog_fields_resolver,
        article_hash_resolver=article_hash_resolver,
        is_tombstone=is_tombstone,
        canonicalize_scope=canonicalize_scope,
    )
    return combined


def review_source_projection_atomic_units(
    projection: Mapping[str, Any],
) -> tuple[tuple[int, ...], ...]:
    """Return source-ordered indivisible batching units."""

    _normalized, articles, groups = _normalized_projection(projection)
    group_by_article = {
        member.article_id: group
        for group in groups
        for member in group.members
    }
    ordered_ids = sorted(
        articles,
        key=lambda article_id: (
            int(articles[article_id]["source"]["law_document_id"]),
            int(articles[article_id]["source"]["source_order"]),
            article_id,
        ),
    )
    emitted_groups: set[str] = set()
    units: list[tuple[int, ...]] = []
    for article_id in ordered_ids:
        group = group_by_article.get(article_id)
        if group is None:
            units.append((article_id,))
            continue
        if group.key in emitted_groups:
            continue
        emitted_groups.add(group.key)
        units.append(tuple(
            member.article_id
            for member in sorted(
                group.members,
                key=lambda member: (
                    int(member.law_document_id),
                    int(member.source_order),
                    int(member.article_id),
                ),
            )
        ))
    return tuple(units)


def review_source_projection_sources(
    projection: Mapping[str, Any],
) -> dict[int, dict[str, Any]]:
    """Return validated source records keyed by article id."""

    _normalized, articles, _groups = _normalized_projection(projection)
    return {
        article_id: dict(article["source"])
        for article_id, article in articles.items()
    }


def validate_reviewed_source_projection(
    projection: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    *,
    catalog_fields_resolver: CatalogFieldsResolver,
    article_hash_resolver: ArticleHashResolver,
    is_tombstone: Callable[[Any], bool],
    canonicalize_scope: Callable[[str], Any],
    canonicalize_suffix: Callable[[str], Any],
) -> None:
    """Validate reviewed output against one exported or live projection."""

    if not callable(canonicalize_suffix):
        raise ReviewSourceContractError(
            "canonicalize_suffix must be callable"
        )
    normalized, articles, groups = _normalized_projection(projection)
    _validate_projection_truth(
        normalized,
        catalog_fields_resolver=catalog_fields_resolver,
        article_hash_resolver=article_hash_resolver,
        is_tombstone=is_tombstone,
        canonicalize_scope=canonicalize_scope,
    )
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ReviewSourceContractError(
            "reviewed items must be a sequence"
        )
    by_id: dict[int, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ReviewSourceContractError(
                f"reviewed item {index} must be an object"
            )
        article_id = item.get("id")
        if (
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            or article_id in by_id
        ):
            raise ReviewSourceContractError(
                f"reviewed item {index} has invalid/duplicate id"
            )
        by_id[article_id] = item
    if set(by_id) != set(articles):
        raise ReviewSourceContractError(
            "reviewed item coverage differs from source projection: "
            f"missing={sorted(set(articles) - set(by_id))}, "
            f"unexpected={sorted(set(by_id) - set(articles))}"
        )

    trusted_prefixes: dict[int, str] = {}
    trusted_title_prefixes: dict[int, str] = {}
    for group in groups:
        try:
            validate_exact_duplicate_group_response(
                group,
                items,
                canonicalize_suffix=canonicalize_suffix,
                source_titles={
                    member.article_id: articles[
                        member.article_id
                    ]["source"]["source_title"]
                    for member in group.members
                },
            )
        except ExactDuplicateError as exc:
            raise ReviewSourceContractError(
                f"exact-body reviewed response failed for group {group.key!r}"
            ) from exc
        for member in group.members:
            trusted_prefixes[
                member.article_id
            ] = member.synonym_scope_prefix
            trusted_title_prefixes[
                member.article_id
            ] = member.title_scope_prefix

    for article_id, article in articles.items():
        item = by_id[article_id]
        source = article["source"]
        if (
            item.get("article_hash") != article["article_hash"]
        ):
            raise ReviewSourceContractError(
                f"reviewed article {article_id} hash differs from projection"
            )
        if article["tombstone"]:
            if (
                item.get("title") is not None
                or item.get("penalty_summary") is not None
                or item.get("penalty") is not None
                or item.get("synonyms")
            ):
                raise ReviewSourceContractError(
                    f"article {article_id} is a tombstone and must have null "
                    "title/penalty plus an empty synonym list"
                )
            continue
        source_title = source.get("source_title")
        context_parts = [
            value
            for value in (source.get("document"),)
            if isinstance(value, str) and value
        ]
        for name in ("section", "chapter"):
            component = article["structural_ancestry"][name]
            if component is None:
                continue
            for field in ("source_title", "article_ref", "code"):
                value = component.get(field)
                if isinstance(value, str) and value:
                    context_parts.append(value)
        try:
            validate_enrichment_title_source_evidence(
                source["text"],
                item.get("title"),
                source_title=source_title,
                source_context="\n".join(context_parts) or None,
                trusted_title_prefix=trusted_title_prefixes.get(
                    article_id
                ),
            )
        except ValueError as exc:
            raise ReviewSourceContractError(
                f"article {article_id} reviewed title is not supported by "
                f"its source: {exc}"
            ) from exc
        try:
            validate_enrichment_source_evidence(
                source["text"],
                item.get("penalty_summary"),
                item.get("penalty"),
                item.get("synonyms"),
                trusted_scope_prefix=trusted_prefixes.get(article_id),
            )
        except ValueError as exc:
            raise ReviewSourceContractError(
                f"article {article_id} reviewed enrichment is not supported "
                f"by its source: {exc}"
            ) from exc
