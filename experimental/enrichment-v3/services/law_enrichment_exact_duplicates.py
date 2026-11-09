"""Pure planning and validation for exact duplicate law article bodies.

Some sources intentionally repeat the same operative wording under different
structural scopes.  Treating those rows as ordinary, independent enrichment
targets makes a text model invent an arbitrary owner for generic queries.  This
module provides the deterministic part of the solution:

* extract an operative body only when the stored first line exactly proves the
  article header;
* find byte-identical, non-empty bodies without trusting a hash alone;
* derive a source-backed section/chapter path from catalog order;
* assign the shortest structural scope that distinguishes every group member;
* close a stale target set over complete duplicate groups; and
* prove that a generated response mirrors titles, synonyms and penalties
  across a complete group.

The module is deliberately independent from the database and from any model
client.  Callers remain responsible for snapshot/CAS checks, server-wide
collision validation and one atomic write.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


DEFAULT_MAX_SCOPE_PREFIX_LENGTH = 60

_SPACE_RE = re.compile(r"\s+")
_STRUCTURAL_KINDS = frozenset({"section", "chapter"})
# Prefer the semantic structural title.  A code-only reference remains a
# deterministic fallback when titles collide; the combined form preserves the
# source meaning while adding that disambiguating code.
_LABEL_MODES = ("title", "combined", "reference", "shortest")


class ExactDuplicateError(ValueError):
    """Base class for a fail-closed exact-duplicate planning error."""


class ExactDuplicateInputError(ExactDuplicateError):
    """Catalog rows do not satisfy the pure planner input contract."""


class ExactDuplicateScopeError(ExactDuplicateError):
    """An exact duplicate group cannot be distinguished from source structure."""


class ExactDuplicateResponseError(ExactDuplicateError):
    """A generated response violates the mirrored-group contract."""


@dataclass(frozen=True)
class StructuralComponent:
    """One source-backed structural row preceding an article."""

    kind: str
    row_id: int
    law_document_id: int
    source_order: int
    article_ref: str | None
    title: str | None


@dataclass(frozen=True)
class StructuralPath:
    """Nearest preceding section/chapter state for one article."""

    law_document_id: int
    document_title: str | None
    section: StructuralComponent | None
    chapter: StructuralComponent | None


@dataclass(frozen=True)
class ExactDuplicateMember:
    """One member of an exact-body group and its mandatory response prefix."""

    article_id: int
    law_document_id: int
    source_order: int
    article_hash: str
    path: StructuralPath
    scope_text: str
    title_scope_prefix: str
    synonym_scope_prefix: str

    @property
    def scope_prefix(self) -> str:
        """Backward-compatible alias for the storage-compatible prefix.

        Historically one lower-case prefix was used for both generated
        titles and synonyms.  Titles now preserve the source label's casing,
        while synonyms deliberately use the same lower/``ё`` normalization
        as their storage surface.  Existing callers that only handled
        synonyms may continue to use ``scope_prefix``.
        """

        return self.synonym_scope_prefix


@dataclass(frozen=True)
class ExactDuplicateGroup:
    """A byte-proven duplicate body with one shared scoping strategy."""

    key: str
    body_sha256: str
    body_length: int
    scope_level: str
    members: tuple[ExactDuplicateMember, ...]

    @property
    def article_ids(self) -> tuple[int, ...]:
        return tuple(member.article_id for member in self.members)


@dataclass(frozen=True)
class ExactDuplicatePlan:
    """Deterministic result for one complete catalog snapshot."""

    groups: tuple[ExactDuplicateGroup, ...]
    excluded_article_ids: tuple[int, ...]

    def group_for_article(
        self,
        article_id: int,
    ) -> ExactDuplicateGroup | None:
        wanted = int(article_id)
        for group in self.groups:
            if any(member.article_id == wanted for member in group.members):
                return group
        return None


@dataclass(frozen=True)
class ExactDuplicateResponseProof:
    """Common suffixes proven by :func:`validate_group_response`."""

    article_ids: tuple[int, ...]
    normalized_base_title: str | None
    normalized_synonym_suffixes: tuple[str, ...]
    canonical_synonym_suffixes: tuple[str, ...]


@dataclass(frozen=True)
class _EligibleArticle:
    article_id: int
    law_document_id: int
    source_order: int
    article_hash: str
    body: bytes
    path: StructuralPath


def _compact_source_text(value: Any) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def _compact_scope_text(value: Any) -> str | None:
    """Return a visible structural label or reject it as unsafe.

    Format/control characters such as ZERO WIDTH SPACE and WORD JOINER can
    make two source labels compare as different while rendering identically
    and reaching the same runtime query.  Do not rewrite such a label into a
    new source spelling: reject that candidate and let the planner try the
    source reference or a wider structural level instead.
    """

    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    for character in text:
        category = unicodedata.category(character)
        if (
            category in {"Cf", "Cs", "Co", "Cn"}
            or (category == "Cc" and not character.isspace())
        ):
            return None
    text = _SPACE_RE.sub(" ", text).strip()
    return text or None


def _normalize_comparison_text(value: Any) -> str:
    text = _compact_source_text(value) or ""
    return text.casefold().replace("ё", "е")


def _normalize_scope_comparison_text(value: Any) -> str:
    text = _compact_scope_text(value) or ""
    return text.casefold().replace("ё", "е")


def _runtime_surface_scope_text(value: Any) -> str:
    """Match the shared writer's lossless surface normalization.

    Scope evidence remains available in ``scope_text`` with source casing.
    Only the emitted prefix follows ``normalize_synonym_term`` so validating
    the already-normalized model response cannot fail merely because a source
    chapter starts with an uppercase letter.
    """

    text = _compact_scope_text(value) or ""
    return text.lower().replace("ё", "е")


def extract_proven_operative_body(
    row: Mapping[str, Any],
) -> bytes | None:
    """Return exact UTF-8 body bytes only for an exactly proven first line.

    The sole tolerated layout variation is the one ``\r`` belonging to a
    CRLF line ending.  The source header itself is not stripped or parsed:
    after removing that optional ``\r`` it must equal ``article_ref.strip()``
    byte-for-text.  This intentionally excludes guessed or repaired headers.
    """

    full_text = row.get("full_text")
    article_ref = row.get("article_ref")
    if not isinstance(full_text, str) or not isinstance(article_ref, str):
        return None
    if "\n" not in full_text:
        return None

    first_line, body = full_text.split("\n", 1)
    if first_line.endswith("\r"):
        first_line = first_line[:-1]
    if first_line != article_ref.strip():
        return None
    if not body.strip():
        return None
    return body.encode("utf-8")


def _required_int(
    row: Mapping[str, Any],
    field: str,
    *,
    row_id: Any,
) -> int:
    value = row.get(field)
    if isinstance(value, bool):
        raise ExactDuplicateInputError(
            f"row {row_id!r} has invalid {field}"
        )
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ExactDuplicateInputError(
            f"row {row_id!r} has invalid {field}"
        ) from exc


def _component_from_row(
    row: Mapping[str, Any],
    *,
    kind: str,
    row_id: int,
    document_id: int,
    source_order: int,
) -> StructuralComponent:
    return StructuralComponent(
        kind=kind,
        row_id=row_id,
        law_document_id=document_id,
        source_order=source_order,
        article_ref=_compact_scope_text(row.get("article_ref")),
        title=_compact_scope_text(row.get("title")),
    )


def _component_label(
    component: StructuralComponent | None,
    mode: str,
) -> str | None:
    if component is None:
        return None
    title = component.title
    reference = component.article_ref
    candidates = tuple(
        dict.fromkeys(value for value in (title, reference) if value)
    )
    if not candidates:
        return None

    if mode == "shortest":
        return min(
            candidates,
            key=lambda value: (
                len(value),
                _normalize_comparison_text(value),
                value,
            ),
        )
    if mode == "title":
        return title or reference
    if mode == "reference":
        return reference or title
    if mode != "combined":
        raise AssertionError(f"unknown structural label mode: {mode}")

    if not title or not reference:
        return title or reference
    normalized_title = _normalize_comparison_text(title)
    normalized_reference = _normalize_comparison_text(reference)
    if (
        normalized_title in normalized_reference
        or normalized_reference in normalized_title
    ):
        return min(
            (title, reference),
            key=lambda value: (
                len(value),
                _normalize_comparison_text(value),
                value,
            ),
        )
    return f"{reference} — {title}"


def _structure_label(
    path: StructuralPath,
    *,
    section_mode: str,
    chapter_mode: str,
) -> str | None:
    section = _component_label(path.section, section_mode)
    chapter = _component_label(path.chapter, chapter_mode)
    if section and chapter:
        return f"{section} / {chapter}"
    return chapter or section


def _scope_candidates(
    level: str,
    articles: Sequence[_EligibleArticle],
) -> Iterable[tuple[str, ...]]:
    if level == "chapter":
        for mode in _LABEL_MODES:
            labels = tuple(
                _component_label(article.path.chapter, mode)
                for article in articles
            )
            if all(labels):
                yield tuple(str(label) for label in labels)
        return

    for section_mode in _LABEL_MODES:
        for chapter_mode in _LABEL_MODES:
            structure = tuple(
                _structure_label(
                    article.path,
                    section_mode=section_mode,
                    chapter_mode=chapter_mode,
                )
                for article in articles
            )
            if not all(structure):
                continue
            structure_labels = tuple(str(label) for label in structure)
            if level == "structure":
                yield structure_labels
                continue
            if level != "document_structure":
                raise AssertionError(f"unknown scope level: {level}")
            documents = tuple(
                article.path.document_title for article in articles
            )
            if not all(documents):
                continue
            yield tuple(
                f"{document} / {label}"
                for document, label in zip(
                    documents,
                    structure_labels,
                    strict=True,
                )
            )


def _select_scopes(
    articles: Sequence[_EligibleArticle],
    *,
    maximum_prefix_length: int,
    canonicalize_scope: Callable[[str], Any] | None,
) -> tuple[
    str,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    canonical_cache: dict[str, str | None] = {}

    def _scope_key(label: str) -> str | None:
        # Runtime lookup sees the lower/storage-compatible surface, not the
        # source-case title prefix.  Prove uniqueness on exactly that portion
        # of the emitted synonym.  Brackets and a common mirrored suffix are
        # intentionally excluded: neither may manufacture uniqueness.
        runtime_surface = _runtime_surface_scope_text(label)
        if runtime_surface in canonical_cache:
            return canonical_cache[runtime_surface]

        source_key = _normalize_scope_comparison_text(runtime_surface)
        if not source_key:
            canonical_cache[runtime_surface] = None
            return None
        if canonicalize_scope is None:
            canonical_cache[runtime_surface] = source_key
            return source_key

        try:
            canonical = canonicalize_scope(runtime_surface)
        except Exception as exc:
            raise ExactDuplicateInputError(
                "canonicalize_scope failed for structural label "
                f"{label!r}"
            ) from exc
        if canonical is None:
            canonical_cache[runtime_surface] = None
            return None
        if not isinstance(canonical, str):
            raise ExactDuplicateInputError(
                "canonicalize_scope must return a string or null for "
                f"structural label {label!r}"
            )
        canonical_key = _normalize_scope_comparison_text(canonical)
        canonical_cache[runtime_surface] = canonical_key or None
        return canonical_cache[runtime_surface]

    for level in ("chapter", "structure", "document_structure"):
        seen: set[tuple[str, ...]] = set()
        for labels in _scope_candidates(level, articles):
            candidate_keys = tuple(_scope_key(label) for label in labels)
            if any(not label for label in candidate_keys):
                continue
            normalized_labels = tuple(
                str(label) for label in candidate_keys
            )
            if normalized_labels in seen:
                continue
            seen.add(normalized_labels)
            if len(set(normalized_labels)) != len(normalized_labels):
                continue
            title_prefixes = tuple(
                f"[{label}] "
                for label in labels
            )
            synonym_prefixes = tuple(
                f"[{_runtime_surface_scope_text(label)}] "
                for label in labels
            )
            if any(
                len(prefix) > maximum_prefix_length
                for prefix in (*title_prefixes, *synonym_prefixes)
            ):
                continue
            return level, labels, title_prefixes, synonym_prefixes

    raise ExactDuplicateScopeError(
        "exact duplicate body cannot be assigned a short, unique, "
        "source-backed structural scope: "
        f"article_ids={[article.article_id for article in articles]}"
    )


def build_exact_duplicate_plan(
    rows: Sequence[Mapping[str, Any]],
    *,
    maximum_scope_prefix_length: int = DEFAULT_MAX_SCOPE_PREFIX_LENGTH,
    is_substantive_article: Callable[[Mapping[str, Any]], bool] | None = None,
    canonicalize_scope: Callable[[str], Any] | None = None,
) -> ExactDuplicatePlan:
    """Build a complete exact-body plan from one catalog snapshot.

    Duplicate ``(law_document_id, source_order)`` positions are rejected
    instead of using a database id as a structural tie-breaker.

    ``is_substantive_article`` is the caller-owned semantic eligibility hook.
    The autonomous enrichment path uses it to exclude empty/repealed articles,
    whose mandatory ``title=null``/``synonyms=[]`` contract cannot be combined
    with scope prefixes.  It is injected here to keep this pure module free of
    imports from the synonym/database service.

    ``canonicalize_scope`` should be the same one-step canonicalizer used by
    runtime synonym lookup.  Candidate labels that become empty or collide
    under it are skipped in favour of a source reference or wider structural
    path.  Omitting it preserves the legacy comparison contract while still
    rejecting invisible ``Cf`` characters in source scope labels.
    """

    if (
        isinstance(maximum_scope_prefix_length, bool)
        or int(maximum_scope_prefix_length) < 4
    ):
        raise ExactDuplicateInputError(
            "maximum_scope_prefix_length must be at least 4"
        )
    maximum_scope_prefix_length = int(maximum_scope_prefix_length)

    prepared: list[tuple[int, int, int, str, Mapping[str, Any]]] = []
    ids: set[int] = set()
    positions: set[tuple[int, int]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ExactDuplicateInputError(
                f"catalog row {index} is not a mapping"
            )
        row_id = _required_int(row, "id", row_id=index)
        if row_id in ids:
            raise ExactDuplicateInputError(f"duplicate catalog row id {row_id}")
        ids.add(row_id)
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
            raise ExactDuplicateInputError(
                "ambiguous duplicate catalog source position: "
                f"law_document_id={document_id}, source_order={source_order}"
            )
        positions.add(position)
        kind = str(row.get("kind") or "").strip().lower()
        prepared.append((document_id, source_order, row_id, kind, row))

    prepared.sort(key=lambda item: (item[0], item[1]))
    eligible: list[_EligibleArticle] = []
    excluded: list[int] = []
    current_document: int | None = None
    current_section: StructuralComponent | None = None
    current_chapter: StructuralComponent | None = None

    for document_id, source_order, row_id, kind, row in prepared:
        if document_id != current_document:
            current_document = document_id
            current_section = None
            current_chapter = None

        if kind in _STRUCTURAL_KINDS:
            component = _component_from_row(
                row,
                kind=kind,
                row_id=row_id,
                document_id=document_id,
                source_order=source_order,
            )
            if kind == "section":
                current_section = component
                current_chapter = None
            else:
                current_chapter = component
            continue
        if kind != "article":
            continue

        if is_substantive_article is not None:
            try:
                substantive = is_substantive_article(row)
            except Exception as exc:
                raise ExactDuplicateInputError(
                    "is_substantive_article failed for catalog row "
                    f"{row_id}"
                ) from exc
            if not isinstance(substantive, bool):
                raise ExactDuplicateInputError(
                    "is_substantive_article must return bool for catalog row "
                    f"{row_id}"
                )
            if not substantive:
                excluded.append(row_id)
                continue

        body = extract_proven_operative_body(row)
        if body is None:
            excluded.append(row_id)
            continue
        path = StructuralPath(
            law_document_id=document_id,
            document_title=_compact_scope_text(row.get("doc_title")),
            section=current_section,
            chapter=current_chapter,
        )
        eligible.append(
            _EligibleArticle(
                article_id=row_id,
                law_document_id=document_id,
                source_order=source_order,
                article_hash=str(row.get("article_hash") or ""),
                body=body,
                path=path,
            )
        )

    by_digest: dict[str, dict[bytes, list[_EligibleArticle]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for article in eligible:
        digest = hashlib.sha256(article.body).hexdigest()
        by_digest[digest][article.body].append(article)

    groups: list[ExactDuplicateGroup] = []
    for digest in sorted(by_digest):
        exact_bodies = by_digest[digest]
        body_items = sorted(exact_bodies.items(), key=lambda item: item[0])
        for collision_index, (body, members) in enumerate(body_items, start=1):
            if len(members) < 2:
                continue
            ordered_members = sorted(
                members,
                key=lambda article: (
                    article.law_document_id,
                    article.source_order,
                    article.article_id,
                ),
            )
            (
                scope_level,
                labels,
                title_prefixes,
                synonym_prefixes,
            ) = _select_scopes(
                ordered_members,
                maximum_prefix_length=maximum_scope_prefix_length,
                canonicalize_scope=canonicalize_scope,
            )
            group_members = tuple(
                ExactDuplicateMember(
                    article_id=article.article_id,
                    law_document_id=article.law_document_id,
                    source_order=article.source_order,
                    article_hash=article.article_hash,
                    path=article.path,
                    scope_text=label,
                    title_scope_prefix=title_prefix,
                    synonym_scope_prefix=synonym_prefix,
                )
                for article, label, title_prefix, synonym_prefix in zip(
                    ordered_members,
                    labels,
                    title_prefixes,
                    synonym_prefixes,
                    strict=True,
                )
            )
            key = (
                digest
                if len(body_items) == 1
                else f"{digest}:{collision_index}"
            )
            groups.append(
                ExactDuplicateGroup(
                    key=key,
                    body_sha256=digest,
                    body_length=len(body),
                    scope_level=scope_level,
                    members=group_members,
                )
            )

    groups.sort(
        key=lambda group: (
            group.article_ids,
            group.body_sha256,
            group.key,
        )
    )
    return ExactDuplicatePlan(
        groups=tuple(groups),
        excluded_article_ids=tuple(sorted(excluded)),
    )


def close_targets_over_exact_duplicate_groups(
    target_ids: Iterable[int],
    plan: ExactDuplicatePlan,
) -> frozenset[int]:
    """Return targets plus every member of each intersecting exact group."""

    closed = {
        int(article_id)
        for article_id in target_ids
        if not isinstance(article_id, bool)
    }
    changed = True
    while changed:
        changed = False
        for group in plan.groups:
            member_ids = set(group.article_ids)
            if closed.isdisjoint(member_ids) or member_ids <= closed:
                continue
            closed.update(member_ids)
            changed = True
    return frozenset(closed)


def _strip_exact_prefix(
    value: Any,
    prefix: str,
    *,
    field: str,
) -> str:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ExactDuplicateResponseError(
            f"{field} must start with exact scope prefix {prefix!r}"
        )
    suffix = value[len(prefix):]
    if not suffix or not suffix.strip():
        raise ExactDuplicateResponseError(
            f"{field} must contain text after the exact scope prefix"
        )
    return suffix


def validate_exact_duplicate_group_response(
    group: ExactDuplicateGroup,
    items: Sequence[Mapping[str, Any]],
    *,
    canonicalize_suffix: Callable[[str], Any] | None = None,
    source_titles: Mapping[int, str | None] | None = None,
) -> ExactDuplicateResponseProof:
    """Prove a complete, structurally mirrored response for one group.

    ``items`` may contain unrelated batch members; only this group's ids are
    inspected.  A caller that has the runtime query canonicalizer should pass
    it as ``canonicalize_suffix``.  The default is the deterministic surface
    comparison normalizer, which keeps this module independent of search/DB
    services.

    When ``source_titles`` is supplied, it must cover every group member
    exactly.  A non-null value is parser/source-owned: the response must
    preserve its compact, case-sensitive value and must not add a scope
    prefix.  A null value marks a generated title, for which the source-case
    title prefix and mirrored base-title rule remain mandatory.  If a group
    mixes source and generated titles, every source title must provide the
    same normalized anchor and every generated base must equal it.  Conflicting
    source anchors fail closed; differing immutable titles remain valid when
    the entire group is source-owned.  Omitting the mapping preserves the
    legacy contract in which every title is generated.
    """

    expected = {member.article_id: member for member in group.members}
    if source_titles is None:
        title_provenance: dict[int, str | None] = {
            article_id: None for article_id in expected
        }
    else:
        invalid_title_keys = [
            key
            for key in source_titles
            if isinstance(key, bool) or not isinstance(key, int)
        ]
        if invalid_title_keys:
            raise ExactDuplicateResponseError(
                "source_titles keys must be exact integer article ids: "
                f"invalid={sorted(map(repr, invalid_title_keys))}"
            )
        supplied_keys = set(source_titles)
        expected_keys = set(expected)
        if supplied_keys != expected_keys:
            missing_title_keys = sorted(expected_keys - supplied_keys)
            unexpected_title_keys = sorted(
                supplied_keys - expected_keys,
                key=lambda value: (type(value).__name__, repr(value)),
            )
            raise ExactDuplicateResponseError(
                "source_titles coverage is not exact: "
                f"missing={missing_title_keys}, "
                f"unexpected={unexpected_title_keys}"
            )
        title_provenance = {}
        for article_id in expected:
            source_title = source_titles[article_id]
            if source_title is None:
                title_provenance[article_id] = None
                continue
            if not isinstance(source_title, str):
                raise ExactDuplicateResponseError(
                    f"source title for article {article_id} must be a "
                    "string or null"
                )
            compact_source_title = _compact_source_text(source_title)
            if compact_source_title is None:
                raise ExactDuplicateResponseError(
                    f"source title for article {article_id} is empty"
                )
            title_provenance[article_id] = compact_source_title

    by_id: dict[int, Mapping[str, Any]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ExactDuplicateResponseError(
                f"response item {index} is not a mapping"
            )
        article_id = item.get("id")
        if (
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            or article_id not in expected
        ):
            continue
        if article_id in by_id:
            raise ExactDuplicateResponseError(
                f"duplicate response for exact-group article {article_id}"
            )
        by_id[article_id] = item

    missing = sorted(set(expected) - set(by_id))
    if missing:
        raise ExactDuplicateResponseError(
            f"exact duplicate group response is incomplete: missing={missing}"
        )

    canonicalizer = canonicalize_suffix or _normalize_comparison_text
    base_titles: list[str] = []
    source_title_anchors: list[str] = []
    synonym_suffix_sequences: list[tuple[str, ...]] = []
    canonical_suffix_sequences: list[tuple[str, ...]] = []
    penalty_summaries: list[Any] = []
    penalties: list[Any] = []

    for member in group.members:
        item = by_id[member.article_id]
        source_title = title_provenance[member.article_id]
        if source_title is not None:
            output_title = item.get("title")
            if not isinstance(output_title, str):
                raise ExactDuplicateResponseError(
                    f"article {member.article_id} must preserve its "
                    "source title"
                )
            compact_output_title = _compact_source_text(output_title)
            if compact_output_title != source_title:
                raise ExactDuplicateResponseError(
                    f"article {member.article_id} changed its source title"
                )
            source_title_anchor = _normalize_comparison_text(source_title)
            if not source_title_anchor:
                raise ExactDuplicateResponseError(
                    f"article {member.article_id} has an empty source-title "
                    "anchor"
                )
            source_title_anchors.append(source_title_anchor)
        else:
            base_title = _strip_exact_prefix(
                item.get("title"),
                member.title_scope_prefix,
                field=f"article {member.article_id} title",
            )
            normalized_base_title = _normalize_comparison_text(base_title)
            if not normalized_base_title:
                raise ExactDuplicateResponseError(
                    f"article {member.article_id} has an empty base title"
                )
            base_titles.append(normalized_base_title)

        raw_synonyms = item.get("synonyms")
        if not isinstance(raw_synonyms, list):
            raise ExactDuplicateResponseError(
                f"article {member.article_id} synonyms must be a list"
            )
        normalized_suffixes: list[str] = []
        canonical_suffixes: list[str] = []
        for synonym_index, raw_synonym in enumerate(raw_synonyms):
            suffix = _strip_exact_prefix(
                raw_synonym,
                member.synonym_scope_prefix,
                field=(
                    f"article {member.article_id} synonym "
                    f"{synonym_index}"
                ),
            )
            normalized_suffix = _normalize_comparison_text(suffix)
            if not normalized_suffix:
                raise ExactDuplicateResponseError(
                    f"article {member.article_id} has an empty synonym suffix"
                )
            canonical = canonicalizer(normalized_suffix)
            if canonical is None:
                raise ExactDuplicateResponseError(
                    f"article {member.article_id} synonym {synonym_index} "
                    "has no runtime-canonical suffix"
                )
            normalized_canonical = _normalize_comparison_text(canonical)
            if not normalized_canonical:
                raise ExactDuplicateResponseError(
                    f"article {member.article_id} synonym {synonym_index} "
                    "has an empty runtime-canonical suffix"
                )
            normalized_suffixes.append(normalized_suffix)
            canonical_suffixes.append(normalized_canonical)
        synonym_suffix_sequences.append(tuple(normalized_suffixes))
        canonical_suffix_sequences.append(tuple(canonical_suffixes))
        penalty_summaries.append(item.get("penalty_summary"))
        penalties.append(item.get("penalty"))

    if len(set(base_titles)) > 1:
        raise ExactDuplicateResponseError(
            "exact duplicate group has asymmetric base titles"
        )
    if base_titles and source_title_anchors:
        distinct_source_anchors = set(source_title_anchors)
        if len(distinct_source_anchors) != 1:
            raise ExactDuplicateResponseError(
                "exact duplicate group has conflicting source-title anchors "
                "for generated titles"
            )
        if base_titles[0] != source_title_anchors[0]:
            raise ExactDuplicateResponseError(
                "exact duplicate group generated base title does not match "
                "its source-title anchor"
            )
    if len(set(synonym_suffix_sequences)) != 1:
        raise ExactDuplicateResponseError(
            "exact duplicate group has asymmetric ordered synonym suffixes"
        )
    if len(set(canonical_suffix_sequences)) != 1:
        raise ExactDuplicateResponseError(
            "exact duplicate group has asymmetric canonical suffixes"
        )
    if any(
        summary != penalty_summaries[0]
        for summary in penalty_summaries[1:]
    ):
        raise ExactDuplicateResponseError(
            "exact duplicate group has asymmetric penalty_summary"
        )
    if any(penalty != penalties[0] for penalty in penalties[1:]):
        raise ExactDuplicateResponseError(
            "exact duplicate group has asymmetric penalty"
        )

    return ExactDuplicateResponseProof(
        article_ids=group.article_ids,
        normalized_base_title=base_titles[0] if base_titles else None,
        normalized_synonym_suffixes=synonym_suffix_sequences[0],
        canonical_synonym_suffixes=canonical_suffix_sequences[0],
    )
