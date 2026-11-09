"""Deterministic, source-only enrichment context fingerprints.

The autonomous enrichment writer needs a stronger freshness token than the
article body hash alone.  A model answer also depends on document/structural
context, exact-body group topology, and (optionally) source-backed competitor
selection.  This module turns those inputs into one SHA-256 value per current
article.

The module is intentionally pure: it has no database, configuration, or model
client dependency.  Callers must pass a complete catalog snapshot for one
server and either a matching :class:`ExactDuplicatePlan` or the inputs needed
to build one.

The mixed database article ``title`` and all enrichment outputs are
deliberately excluded.  A parser-derived ``source_title`` is recomputed from
``article_ref``/``full_text`` and included under an explicit derivation-policy
version.  Structural-row titles are included because they are parser-owned
ancestry evidence.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from services.law_enrichment_exact_duplicates import (
    DEFAULT_MAX_SCOPE_PREFIX_LENGTH,
    ExactDuplicatePlan,
    StructuralComponent,
    StructuralPath,
    build_exact_duplicate_plan,
    extract_proven_operative_body,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
)


ENRICHMENT_CONTEXT_ALGORITHM_VERSION = "law-enrichment-context-v2"
ENRICHMENT_GENERATION_POLICY_VERSION = (
    "law-enrichment-generation-policy-2026-07-31-v2"
)
SOURCE_TITLE_DERIVATION_POLICY_VERSION = (
    "law-source-title-derivation-2026-07-30-v1"
)
DEFAULT_LOCAL_NEIGHBOR_RADIUS = 2

_STRUCTURAL_KINDS = frozenset({"section", "chapter"})

SourceTitleResolver = Callable[[str | None, str], str | None]


class EnrichmentContextInputError(ValueError):
    """The supplied snapshot, exact plan, or competitor map is inconsistent."""


def _required_int(
    row: Mapping[str, Any],
    field: str,
    *,
    row_id: Any,
) -> int:
    value = row.get(field)
    if isinstance(value, bool):
        raise EnrichmentContextInputError(
            f"row {row_id!r} has invalid {field}"
        )
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise EnrichmentContextInputError(
            f"row {row_id!r} has invalid {field}"
        ) from exc


def _optional_text(
    row: Mapping[str, Any],
    field: str,
    *,
    row_id: int,
) -> str | None:
    value = row.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EnrichmentContextInputError(
            f"row {row_id} has non-text {field}"
        )
    return value


def _required_text(
    row: Mapping[str, Any],
    field: str,
    *,
    row_id: int,
) -> str:
    value = _optional_text(row, field, row_id=row_id)
    if value is None:
        raise EnrichmentContextInputError(
            f"row {row_id} has null {field}"
        )
    return value


def _text_digest(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8")
    return {
        "length": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def normalize_enrichment_project_context(value: str) -> str:
    """Return the exact stable project-context representation used in hashes.

    Project policy text is configuration, not generated output.  Normalize
    Unicode and physical newlines once, but deliberately preserve all other
    internal whitespace: changing the wording or formatting that Gemini sees
    must invalidate the previous enrichment context.
    """

    if not isinstance(value, str):
        raise EnrichmentContextInputError(
            "project_context must be a string"
        )
    return (
        unicodedata.normalize("NFKC", value)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )


def normalize_enrichment_project_family(value: str) -> str:
    """Return a stable project-family identifier for the generation policy."""

    if not isinstance(value, str):
        raise EnrichmentContextInputError(
            "project_family must be a string"
        )
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _default_source_title_resolver(
    article_ref: str | None,
    _full_text: str,
) -> str | None:
    """Conservative pure fallback for callers without the catalog parser.

    Production callers always pass the shared catalog resolver.  The fallback
    keeps this low-level module usable in isolated tests while its explicit
    policy-version token prevents it from being confused with a future parser
    implementation.
    """

    if article_ref is None:
        return None
    value = article_ref.strip()
    return value or None


def _resolved_source_title(
    row: Mapping[str, Any],
    *,
    source_title_resolver: SourceTitleResolver,
    row_id: int,
) -> str | None:
    article_ref = _optional_text(
        row,
        "article_ref",
        row_id=row_id,
    )
    full_text = _required_text(row, "full_text", row_id=row_id)
    try:
        source_title = source_title_resolver(article_ref, full_text)
    except Exception as exc:
        raise EnrichmentContextInputError(
            f"source title derivation failed for row {row_id}"
        ) from exc
    if source_title is None:
        return None
    if not isinstance(source_title, str):
        raise EnrichmentContextInputError(
            f"source title resolver returned non-text for row {row_id}"
        )
    return source_title


def _prepare_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[
    list[tuple[int, int, int, str, Mapping[str, Any]]],
    dict[int, Mapping[str, Any]],
]:
    prepared: list[tuple[int, int, int, str, Mapping[str, Any]]] = []
    by_id: dict[int, Mapping[str, Any]] = {}
    positions: set[tuple[int, int]] = set()

    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise EnrichmentContextInputError(
                f"catalog row {index} is not a mapping"
            )
        row_id = _required_int(row, "id", row_id=index)
        if row_id in by_id:
            raise EnrichmentContextInputError(
                f"duplicate catalog row id {row_id}"
            )
        document_id = _required_int(
            row,
            "law_document_id",
            row_id=row_id,
        )
        source_order = _required_int(
            row,
            "source_order",
            row_id=row_id,
        )
        position = (document_id, source_order)
        if position in positions:
            raise EnrichmentContextInputError(
                "ambiguous duplicate catalog source position: "
                f"law_document_id={document_id}, source_order={source_order}"
            )
        positions.add(position)
        kind = _required_text(row, "kind", row_id=row_id).strip().lower()
        if not kind:
            raise EnrichmentContextInputError(
                f"row {row_id} has empty kind"
            )
        by_id[row_id] = row
        prepared.append((document_id, source_order, row_id, kind, row))

    prepared.sort(key=lambda item: (item[0], item[1], item[2]))
    return prepared, by_id


def _source_projection(
    row: Mapping[str, Any],
    *,
    source_title_resolver: SourceTitleResolver,
) -> dict[str, Any]:
    row_id = _required_int(row, "id", row_id="?")
    full_text = _required_text(row, "full_text", row_id=row_id)
    kind = _required_text(row, "kind", row_id=row_id).strip().lower()
    # Structural rows are parser-owned catalog metadata and are never LLM
    # enrichment targets.  Article ``title`` is intentionally ignored because
    # that same column may contain generated output.
    source_title = (
        _optional_text(row, "title", row_id=row_id)
        if kind in _STRUCTURAL_KINDS
        else _resolved_source_title(
            row,
            source_title_resolver=source_title_resolver,
            row_id=row_id,
        )
    )
    return {
        "id": row_id,
        "law_document_id": _required_int(
            row,
            "law_document_id",
            row_id=row_id,
        ),
        "source_order": _required_int(
            row,
            "source_order",
            row_id=row_id,
        ),
        "kind": kind,
        "code": _optional_text(row, "code", row_id=row_id),
        "article_ref": _optional_text(
            row,
            "article_ref",
            row_id=row_id,
        ),
        "source_title": source_title,
        "document": _optional_text(row, "doc_title", row_id=row_id),
        "text": full_text,
        "article_hash": _required_text(
            row,
            "article_hash",
            row_id=row_id,
        ),
    }


def _structural_projection(source: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only compact parser-owned scope facts shown to Gemini."""

    return {
        "id": int(source["id"]),
        "source_order": int(source["source_order"]),
        "kind": str(source["kind"]),
        "code": source.get("code"),
        "article_ref": source.get("article_ref"),
        "source_title": source.get("source_title"),
    }


def build_source_only_enrichment_projection(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_title_resolver: SourceTitleResolver | None = None,
) -> dict[int, dict[str, Any]]:
    """Project a complete catalog into the exact source context Gemini sees.

    The result contains only article source facts plus compact parser-owned
    section/chapter ancestry.  Database enrichment fields (generated title,
    penalties, synonyms, provenance and context hashes) are never read.
    Both prompt construction and freshness hashing consume this same
    projection so structural evidence cannot silently exist in only one path.
    """

    resolver = source_title_resolver or _default_source_title_resolver
    if not callable(resolver):
        raise EnrichmentContextInputError(
            "source_title_resolver must be callable"
        )

    prepared, _rows_by_id = _prepare_rows(rows)
    projections: dict[int, dict[str, Any]] = {}
    current_document: int | None = None
    section: dict[str, Any] | None = None
    chapter: dict[str, Any] | None = None

    for document_id, _source_order, row_id, kind, row in prepared:
        if document_id != current_document:
            current_document = document_id
            section = None
            chapter = None

        source = _source_projection(
            row,
            source_title_resolver=resolver,
        )
        if kind in _STRUCTURAL_KINDS:
            component = _structural_projection(source)
            if kind == "section":
                section = component
                chapter = None
            else:
                chapter = component
            continue
        if kind != "article":
            continue

        projections[row_id] = {
            "source": source,
            "structural_ancestry": {
                "section": dict(section) if section is not None else None,
                "chapter": dict(chapter) if chapter is not None else None,
            },
        }
    return projections


def _fingerprinted_projection_source(
    source: Mapping[str, Any],
) -> dict[str, Any]:
    """Compact one shared raw source projection for deterministic hashing."""

    return {
        "id": int(source["id"]),
        "law_document_id": int(source["law_document_id"]),
        "source_order": int(source["source_order"]),
        "kind": str(source["kind"]),
        "code": source.get("code"),
        "article_ref": source.get("article_ref"),
        "source_title": source.get("source_title"),
        "document": source.get("document"),
        "article_hash": source.get("article_hash"),
        "text": _text_digest(str(source.get("text") or "")),
    }


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


def _validate_and_index_plan(
    plan: ExactDuplicatePlan,
    *,
    article_rows: Mapping[int, Mapping[str, Any]],
    projections: Mapping[int, Mapping[str, Any]],
) -> tuple[dict[int, dict[str, Any]], frozenset[int]]:
    if not isinstance(plan, ExactDuplicatePlan):
        raise EnrichmentContextInputError(
            "exact_duplicate_plan must be an ExactDuplicatePlan"
        )

    group_payloads: dict[int, dict[str, Any]] = {}
    actual_groups_by_body: dict[bytes, frozenset[int]] = {}
    seen_group_keys: set[str] = set()
    for group in plan.groups:
        if group.key in seen_group_keys:
            raise EnrichmentContextInputError(
                f"duplicate exact-body group key {group.key!r}"
            )
        seen_group_keys.add(group.key)
        if len(group.members) < 2:
            raise EnrichmentContextInputError(
                f"exact-body group {group.key!r} has fewer than two members"
            )

        ordered_members = tuple(
            sorted(
                group.members,
                key=lambda member: (
                    int(member.law_document_id),
                    int(member.source_order),
                    int(member.article_id),
                ),
            )
        )
        bodies: list[bytes] = []
        member_payloads: list[dict[str, Any]] = []
        for member in ordered_members:
            article_id = int(member.article_id)
            if article_id in group_payloads:
                raise EnrichmentContextInputError(
                    f"article {article_id} belongs to multiple exact groups"
                )
            row = article_rows.get(article_id)
            if row is None:
                raise EnrichmentContextInputError(
                    f"exact group references missing article {article_id}"
                )
            document_id = _required_int(
                row,
                "law_document_id",
                row_id=article_id,
            )
            source_order = _required_int(
                row,
                "source_order",
                row_id=article_id,
            )
            article_hash = _required_text(
                row,
                "article_hash",
                row_id=article_id,
            )
            if (
                document_id != int(member.law_document_id)
                or source_order != int(member.source_order)
                or article_hash != member.article_hash
            ):
                raise EnrichmentContextInputError(
                    "exact group member does not match current source facts: "
                    f"article_id={article_id}"
                )
            body = extract_proven_operative_body(row)
            if body is None:
                raise EnrichmentContextInputError(
                    "exact group member no longer has a proven operative "
                    f"body: article_id={article_id}"
                )
            bodies.append(body)
            projection = projections[article_id]
            member_payloads.append({
                "source": _fingerprinted_projection_source(
                    projection["source"],
                ),
                # Current raw ancestry detects drift even if a caller
                # accidentally supplies a plan built from an older snapshot.
                "current_ancestry": projection["structural_ancestry"],
                # Planner evidence records the exact scope decision used by
                # generation and validation.
                "planned_path": _path_payload(member.path),
                "scope_text": member.scope_text,
                "title_scope_prefix": member.title_scope_prefix,
                "synonym_scope_prefix": member.synonym_scope_prefix,
            })

        first_body = bodies[0]
        if any(body != first_body for body in bodies[1:]):
            raise EnrichmentContextInputError(
                f"exact group {group.key!r} members are no longer byte-equal"
            )
        body_sha256 = hashlib.sha256(first_body).hexdigest()
        if (
            body_sha256 != group.body_sha256
            or len(first_body) != int(group.body_length)
        ):
            raise EnrichmentContextInputError(
                f"exact group {group.key!r} body identity is stale"
            )
        if first_body in actual_groups_by_body:
            raise EnrichmentContextInputError(
                "one exact operative body is split across multiple groups"
            )
        actual_groups_by_body[first_body] = frozenset(
            int(member.article_id)
            for member in ordered_members
        )

        payload = {
            "state": "group",
            "key": group.key,
            "body_sha256": group.body_sha256,
            "body_length": int(group.body_length),
            "scope_level": group.scope_level,
            "members": member_payloads,
        }
        for member in ordered_members:
            group_payloads[int(member.article_id)] = payload

    article_ids = set(article_rows)
    try:
        excluded_ids = frozenset(int(value) for value in plan.excluded_article_ids)
    except (TypeError, ValueError) as exc:
        raise EnrichmentContextInputError(
            "exact plan contains an invalid excluded article id"
        ) from exc
    unknown_excluded = excluded_ids - article_ids
    if unknown_excluded:
        raise EnrichmentContextInputError(
            "exact plan excludes missing articles: "
            f"{sorted(unknown_excluded)}"
        )
    overlap = excluded_ids & set(group_payloads)
    if overlap:
        raise EnrichmentContextInputError(
            "exact plan both groups and excludes articles: "
            f"{sorted(overlap)}"
        )

    expected_by_body: dict[bytes, set[int]] = defaultdict(set)
    for article_id, row in article_rows.items():
        if article_id in excluded_ids:
            continue
        body = extract_proven_operative_body(row)
        if body is None:
            raise EnrichmentContextInputError(
                "exact plan omits an article without a proven operative "
                f"body from excluded_article_ids: article_id={article_id}"
            )
        expected_by_body[body].add(article_id)
    expected_groups_by_body = {
        body: frozenset(article_ids)
        for body, article_ids in expected_by_body.items()
        if len(article_ids) >= 2
    }
    if actual_groups_by_body != expected_groups_by_body:
        raise EnrichmentContextInputError(
            "exact plan group topology is incomplete for the current catalog"
        )
    return group_payloads, excluded_ids


def _selector_article(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Return source-only fields safe to expose to a competitor selector."""

    source = projection["source"]
    return {
        "id": int(source["id"]),
        "law_document_id": int(source["law_document_id"]),
        "source_order": int(source["source_order"]),
        "kind": source["kind"],
        "code": source.get("code"),
        "article_ref": source.get("article_ref"),
        "title": source.get("source_title"),
        "doc_title": source.get("document"),
        "full_text": source["text"],
        "article_hash": source["article_hash"],
    }


def _build_local_neighbor_context(
    projections: Mapping[int, Mapping[str, Any]],
    *,
    neighbor_radius: int,
) -> dict[int, list[dict[str, Any]]]:
    """Mirror prompt-local article ordering using source-only evidence."""

    by_document: dict[int, list[int]] = {}
    for article_id, projection in projections.items():
        source = projection["source"]
        document_id = int(source["law_document_id"])
        by_document.setdefault(document_id, []).append(article_id)
    for article_ids in by_document.values():
        article_ids.sort(
            key=lambda article_id: (
                int(projections[article_id]["source"]["source_order"]),
                article_id,
            )
        )

    context = {article_id: [] for article_id in projections}
    if neighbor_radius == 0:
        return context

    for article_ids in by_document.values():
        for index, article_id in enumerate(article_ids):
            start = max(0, index - neighbor_radius)
            end = min(len(article_ids), index + neighbor_radius + 1)
            neighbors: list[dict[str, Any]] = []
            for neighbor_index in range(start, end):
                if neighbor_index == index:
                    continue
                neighbor_id = article_ids[neighbor_index]
                neighbors.append({
                    "relation": (
                        "before" if neighbor_index < index else "after"
                    ),
                    "distance": abs(neighbor_index - index),
                    "source": _fingerprinted_projection_source(
                        projections[neighbor_id]["source"],
                    ),
                    "ancestry": projections[neighbor_id][
                        "structural_ancestry"
                    ],
                })
            context[article_id] = neighbors
    return context


def _competitor_id(value: Any, *, target_id: int) -> int:
    if isinstance(value, bool):
        raise EnrichmentContextInputError(
            f"article {target_id} has a boolean competitor id"
        )
    if isinstance(value, Mapping):
        value = value.get("article_id")
    elif not isinstance(value, int):
        value = getattr(value, "article_id", value)
    if isinstance(value, bool):
        raise EnrichmentContextInputError(
            f"article {target_id} has a boolean competitor id"
        )
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise EnrichmentContextInputError(
            f"article {target_id} has an invalid competitor id"
        ) from exc


def _normalize_competitors(
    competitor_ids_by_article: Mapping[int, Iterable[Any]] | None,
    *,
    projections: Mapping[int, Mapping[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    normalized: dict[int, list[dict[str, Any]]] = {
        article_id: []
        for article_id in projections
    }
    if competitor_ids_by_article is None:
        return normalized
    if not isinstance(competitor_ids_by_article, Mapping):
        raise EnrichmentContextInputError(
            "competitor_ids_by_article must be a mapping"
        )

    for raw_target_id, values in competitor_ids_by_article.items():
        if isinstance(raw_target_id, bool):
            raise EnrichmentContextInputError(
                "competitor map contains a boolean target id"
            )
        try:
            target_id = int(raw_target_id)
        except (TypeError, ValueError) as exc:
            raise EnrichmentContextInputError(
                "competitor map contains an invalid target id"
            ) from exc
        if target_id not in projections:
            raise EnrichmentContextInputError(
                f"competitor map references missing target {target_id}"
            )
        if isinstance(values, (str, bytes)) or not isinstance(
            values,
            Iterable,
        ):
            raise EnrichmentContextInputError(
                f"competitors for article {target_id} must be iterable"
            )

        competitor_ids: list[int] = []
        seen: set[int] = set()
        for value in values:
            competitor_id = _competitor_id(value, target_id=target_id)
            if competitor_id == target_id:
                raise EnrichmentContextInputError(
                    f"article {target_id} cannot compete with itself"
                )
            if competitor_id not in projections:
                raise EnrichmentContextInputError(
                    f"article {target_id} references missing competitor "
                    f"{competitor_id}"
                )
            if competitor_id in seen:
                raise EnrichmentContextInputError(
                    f"article {target_id} repeats competitor {competitor_id}"
                )
            seen.add(competitor_id)
            competitor_ids.append(competitor_id)

        normalized[target_id] = [
            {
                "source": _fingerprinted_projection_source(
                    projections[competitor_id]["source"],
                ),
                "ancestry": projections[competitor_id][
                    "structural_ancestry"
                ],
            }
            for competitor_id in sorted(competitor_ids)
        ]

    return normalized


def build_enrichment_context_digests(
    rows: Sequence[Mapping[str, Any]],
    *,
    exact_duplicate_plan: ExactDuplicatePlan | None = None,
    canonicalize_scope: Callable[[str], Any] | None = None,
    is_substantive_article: Callable[[Mapping[str, Any]], bool] | None = None,
    maximum_scope_prefix_length: int = DEFAULT_MAX_SCOPE_PREFIX_LENGTH,
    competitor_ids_by_article: Mapping[int, Iterable[Any]] | None = None,
    competitor_selector: Callable[
        [
            Sequence[Mapping[str, Any]],
            Sequence[Mapping[str, Any]],
        ],
        Mapping[int, Iterable[Any]],
    ] | None = None,
    neighbor_radius: int = DEFAULT_LOCAL_NEIGHBOR_RADIUS,
    algorithm_version: str = ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    generation_policy_version: str = ENRICHMENT_GENERATION_POLICY_VERSION,
    source_title_policy_version: str = (
        SOURCE_TITLE_DERIVATION_POLICY_VERSION
    ),
    parser_contract_version: str = LAW_CATALOG_PARSER_CONTRACT_VERSION,
    project_context: str = "",
    project_family: str = "",
    source_title_resolver: SourceTitleResolver | None = None,
) -> dict[int, str]:
    """Return a SHA-256 enrichment-context value for every current article.

    ``rows`` must be the complete catalog snapshot for one server, including
    structural rows.  Input ordering is irrelevant.

    When ``exact_duplicate_plan`` is omitted, the plan is built from this same
    snapshot and ``canonicalize_scope``/``is_substantive_article`` are passed
    to the pure exact-body planner.  Supplying a prebuilt plan and planner
    callbacks together is rejected, preventing silently ignored policy.

    Competitor context is optional.  Pass either an id/object mapping or a
    selector with the same two-argument shape as
    ``select_enrichment_competitors``.  A selector receives source-only
    article copies with no generated title or current synonyms.  Only selected
    ids and their current source/ancestry facts enter the digest; score,
    reason, generated output, and existing auto synonyms never do.

    ``neighbor_radius`` mirrors the prompt's positional same-document article
    context (two rows on either side by default).  Each neighbor contributes
    its relation, distance, source facts and ancestry; the radius itself is
    versioned in every payload.

    The exact normalized project family/context, generation/title policies,
    and the complete source-to-catalog parser contract are part of every
    digest.  Production must pass the same project context shown in the prompt
    and the shared parser-owned source title resolver.
    """

    if not isinstance(algorithm_version, str) or not algorithm_version.strip():
        raise EnrichmentContextInputError(
            "algorithm_version must be a non-empty string"
        )
    algorithm_version = algorithm_version.strip()
    if (
        not isinstance(generation_policy_version, str)
        or not generation_policy_version.strip()
    ):
        raise EnrichmentContextInputError(
            "generation_policy_version must be a non-empty string"
        )
    generation_policy_version = generation_policy_version.strip()
    if (
        not isinstance(source_title_policy_version, str)
        or not source_title_policy_version.strip()
    ):
        raise EnrichmentContextInputError(
            "source_title_policy_version must be a non-empty string"
        )
    source_title_policy_version = source_title_policy_version.strip()
    if (
        not isinstance(parser_contract_version, str)
        or not parser_contract_version.strip()
    ):
        raise EnrichmentContextInputError(
            "parser_contract_version must be a non-empty string"
        )
    parser_contract_version = parser_contract_version.strip()
    normalized_project_context = normalize_enrichment_project_context(
        project_context
    )
    normalized_project_family = normalize_enrichment_project_family(
        project_family
    )
    if (
        isinstance(neighbor_radius, bool)
        or not isinstance(neighbor_radius, int)
        or neighbor_radius < 0
    ):
        raise EnrichmentContextInputError(
            "neighbor_radius must be a non-negative integer"
        )
    if competitor_ids_by_article is not None and competitor_selector is not None:
        raise EnrichmentContextInputError(
            "pass competitor_ids_by_article or competitor_selector, not both"
        )
    if exact_duplicate_plan is not None and (
        canonicalize_scope is not None
        or is_substantive_article is not None
        or maximum_scope_prefix_length != DEFAULT_MAX_SCOPE_PREFIX_LENGTH
    ):
        raise EnrichmentContextInputError(
            "planner callbacks/options cannot accompany a prebuilt exact plan"
        )

    prepared, _rows_by_id = _prepare_rows(rows)
    article_rows = {
        row_id: row
        for _document_id, _source_order, row_id, kind, row in prepared
        if kind == "article"
    }
    projections = build_source_only_enrichment_projection(
        rows,
        source_title_resolver=source_title_resolver,
    )
    if set(projections) != set(article_rows):
        raise EnrichmentContextInputError(
            "source-only projection does not exactly cover current articles"
        )

    plan = exact_duplicate_plan
    if plan is None:
        plan = build_exact_duplicate_plan(
            rows,
            maximum_scope_prefix_length=maximum_scope_prefix_length,
            is_substantive_article=is_substantive_article,
            canonicalize_scope=canonicalize_scope,
        )
    group_payloads, excluded_ids = _validate_and_index_plan(
        plan,
        article_rows=article_rows,
        projections=projections,
    )

    selected = competitor_ids_by_article
    if competitor_selector is not None:
        source_articles = tuple(
            _selector_article(projections[article_id])
            for article_id in sorted(article_rows)
        )
        try:
            selected = competitor_selector(
                source_articles,
                source_articles,
            )
        except Exception as exc:
            raise EnrichmentContextInputError(
                "competitor_selector failed"
            ) from exc
        if not isinstance(selected, Mapping):
            raise EnrichmentContextInputError(
                "competitor_selector must return a mapping"
            )

    competitors = _normalize_competitors(
        selected,
        projections=projections,
    )
    local_neighbors = _build_local_neighbor_context(
        projections,
        neighbor_radius=neighbor_radius,
    )

    digests: dict[int, str] = {}
    for article_id in sorted(article_rows):
        topology = group_payloads.get(article_id)
        if topology is None:
            topology = {
                "state": (
                    "excluded"
                    if article_id in excluded_ids
                    else "singleton"
                )
            }
        payload = {
            "algorithm_version": algorithm_version,
            "generation_policy": {
                "version": generation_policy_version,
                "source_title_derivation_version": (
                    source_title_policy_version
                ),
                "catalog_parser_contract_version": (
                    parser_contract_version
                ),
                "project_family": normalized_project_family,
                "project_context": normalized_project_context,
            },
            "article": _fingerprinted_projection_source(
                projections[article_id]["source"],
            ),
            "structural_ancestry": projections[article_id][
                "structural_ancestry"
            ],
            "exact_body_topology": topology,
            "local_neighbor_context": {
                "radius": neighbor_radius,
                "items": local_neighbors[article_id],
            },
            "competitors": competitors[article_id],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digests[article_id] = hashlib.sha256(encoded).hexdigest()

    # Every row classified as an article must receive exactly one token.
    if set(digests) != set(article_rows):
        raise AssertionError("not every current article received a digest")
    return digests
