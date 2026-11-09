"""Authoritative enrichment freshness and searchable-quality evaluation.

``law_articles.enrich_hash`` proves only that the article body seen by the
writer is current.  Enrichment also depends on structural/source context and
must retain an attested title owner plus enough unambiguous approved synonyms
to be useful to the runtime search.

Every workflow which decides that enrichment is complete must use this module
instead of reproducing a subset of those checks in SQL.  The evaluator always
captures the complete server catalog because one article's current context can
depend on another article, its structural ancestry, or exact-body topology.
Optional article/document filters limit only the returned decision, never the
source snapshot used to recompute context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from law_sources_data import get_project_for_server
from services.law_enrichment_competitors import select_enrichment_competitors
from services.law_catalog_truth import (
    LawCatalogTruthError,
    load_verified_law_catalog_truth,
)
from services.law_enrichment_context import build_enrichment_context_digests
from services.law_enrichment_exact_duplicates import build_exact_duplicate_plan
from services.law_enrichment_ownership import (
    is_safe_enrichment_title_ownership,
)
from services.law_enrichment_output import (
    build_enrichment_output_hash,
    foreign_auto_ownership_article_ids,
)
from services.law_synonym_service import (
    MIN_REPLACEMENT_EFFECTIVE,
    MIN_REPLACEMENT_PHYSICAL,
    canonical_synonym_term,
    is_explicitly_empty_or_repealed_article,
    load_exact_signature_registry,
    normalize_synonym_term,
)


class LawEnrichmentCompletionError(RuntimeError):
    """The current server state could not be evaluated safely."""


@dataclass(frozen=True)
class ArticleEnrichmentCompletion:
    """One article's complete freshness/quality decision."""

    article_id: int
    law_document_id: int
    complete: bool
    reasons: tuple[str, ...]
    physical_synonyms: int
    effective_synonyms: int
    expected_context_hash: str


@dataclass(frozen=True)
class LawEnrichmentCompletion:
    """A deterministic completion snapshot for one selected server scope."""

    server_id: str
    article_states: tuple[ArticleEnrichmentCompletion, ...]

    @property
    def articles(self) -> int:
        return len(self.article_states)

    @property
    def enriched(self) -> int:
        """Compatibility count: articles satisfying the *full* contract."""

        return sum(state.complete for state in self.article_states)

    @property
    def complete(self) -> bool:
        """Whether every selected article is complete.

        An empty document-scoped selection is complete because a valid
        chapter/section-only document has no enrichment targets.  Callers that
        require a non-empty server corpus must additionally require
        ``articles > 0`` (the all-44/readiness workflows already do).
        """

        return self.enriched == self.articles

    @property
    def incomplete_article_ids(self) -> tuple[int, ...]:
        return tuple(
            state.article_id
            for state in self.article_states
            if not state.complete
        )

    @property
    def incomplete_document_ids(self) -> tuple[int, ...]:
        return tuple(sorted({
            state.law_document_id
            for state in self.article_states
            if not state.complete
        }))

    @property
    def reasons_by_article(self) -> dict[int, tuple[str, ...]]:
        return {
            state.article_id: state.reasons
            for state in self.article_states
            if state.reasons
        }


def _normalized_filter(
    values: Optional[Sequence[int]],
    *,
    field: str,
) -> Optional[set[int]]:
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise LawEnrichmentCompletionError(
            f"{field} must be a sequence of positive integers"
        )
    normalized: set[int] = set()
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
        ):
            raise LawEnrichmentCompletionError(
                f"{field} must contain positive integers"
            )
        normalized.add(value)
    return normalized


def _source_title_resolver():
    # Keep the import lazy: law_catalog_service calls this evaluator from its
    # incremental sync path and therefore cannot be imported at module load.
    from services.law_catalog_service import source_catalog_title

    return source_catalog_title


def _project_context_for_server(server_id: str) -> str:
    """Resolve the shared project context without freezing a lazy import.

    ``law_synonym_service`` imports this evaluator lazily to avoid a module
    cycle.  Binding its project-context function while that first import is
    running can otherwise capture a temporary/test implementation forever,
    making the loader and completion evaluator derive different context
    hashes in one process.
    """

    from services import law_synonym_service

    return law_synonym_service.project_context_for_server(server_id)


async def _evaluate_law_enrichment_completion_snapshot(
    queryable,
    server_id: str,
    *,
    article_ids: Optional[Sequence[int]] = None,
    document_ids: Optional[Sequence[int]] = None,
) -> LawEnrichmentCompletion:
    """Recompute authoritative completion for one server/scope.

    ``queryable`` is an asyncpg connection/pool-like object exposing
    ``fetch``.  ``article_ids`` and ``document_ids`` are mutually exclusive.
    Context is nevertheless recomputed from every current catalog row on the
    server so a neighbor/ancestry/exact-group change cannot be hidden by the
    scope filter.
    """

    if not isinstance(server_id, str) or not server_id.strip():
        raise LawEnrichmentCompletionError(
            "server_id must be a non-empty string"
        )
    selected_article_ids = _normalized_filter(
        article_ids,
        field="article_ids",
    )
    selected_document_ids = _normalized_filter(
        document_ids,
        field="document_ids",
    )
    if (
        selected_article_ids is not None
        and selected_document_ids is not None
    ):
        raise LawEnrichmentCompletionError(
            "article_ids and document_ids are mutually exclusive"
        )

    try:
        verified_truth = await load_verified_law_catalog_truth(
            queryable,
            server_id,
            lock_rows=False,
        )
    except LawCatalogTruthError as exc:
        raise LawEnrichmentCompletionError(
            f"{server_id}: source documents and persisted catalog do not "
            "match the active parser contract; run normal catalog reconcile"
        ) from exc
    catalog_rows = [dict(row) for row in verified_truth.catalog_rows]
    article_rows = [
        row
        for row in catalog_rows
        if str(row.get("kind") or "") == "article"
    ]
    article_by_id = {int(row["id"]): row for row in article_rows}
    if len(article_by_id) != len(article_rows):
        raise LawEnrichmentCompletionError(
            f"{server_id}: duplicate article ids in catalog snapshot"
        )

    if selected_article_ids is not None:
        missing = sorted(selected_article_ids - set(article_by_id))
        if missing:
            raise LawEnrichmentCompletionError(
                f"{server_id}: selected article ids are missing/non-article: "
                f"{missing}"
            )
        selected_rows = [
            row
            for row in article_rows
            if int(row["id"]) in selected_article_ids
        ]
    elif selected_document_ids is not None:
        selected_rows = [
            row
            for row in article_rows
            if int(row["law_document_id"]) in selected_document_ids
        ]
    else:
        selected_rows = article_rows

    # No selected article requires no synonym/context queries.  This is
    # important for valid chapter-primary documents.
    if not selected_rows:
        return LawEnrichmentCompletion(
            server_id=server_id,
            article_states=(),
        )

    resolver = _source_title_resolver()
    try:
        exact_plan = build_exact_duplicate_plan(
            catalog_rows,
            is_substantive_article=lambda row: (
                not is_explicitly_empty_or_repealed_article(
                    row.get("full_text")
                )
            ),
            canonicalize_scope=canonical_synonym_term,
        )
        context_hashes = build_enrichment_context_digests(
            catalog_rows,
            exact_duplicate_plan=exact_plan,
            competitor_selector=select_enrichment_competitors,
            project_context=_project_context_for_server(server_id),
            project_family=str(get_project_for_server(server_id) or ""),
            source_title_resolver=resolver,
        )
    except Exception as exc:
        raise LawEnrichmentCompletionError(
            f"{server_id}: current enrichment context cannot be proven"
        ) from exc

    approved_rows = await queryable.fetch(
        """
        SELECT s.article_id, s.term
        FROM law_synonyms s
        JOIN law_articles a ON a.id = s.article_id
        WHERE s.server_id = $1 AND a.server_id = $1
          AND a.kind = 'article' AND s.status = 'approved'
        ORDER BY s.article_id, s.id
        """,
        server_id,
    )
    auto_rows = await queryable.fetch(
        """
        SELECT s.server_id, s.article_id, s.term, s.weight, s.source,
               s.status, s.suggested_by
        FROM law_synonyms s
        JOIN law_articles a ON a.id = s.article_id
        WHERE a.server_id = $1 AND a.kind = 'article'
          AND s.source = 'auto'
        ORDER BY s.article_id, s.id
        """,
        server_id,
    )
    auto_rows_by_article: dict[int, list[dict[str, Any]]] = {}
    for row in auto_rows:
        auto_rows_by_article.setdefault(int(row["article_id"]), []).append(
            dict(row)
        )
    try:
        foreign_auto_ids = set(foreign_auto_ownership_article_ids(
            server_id=server_id,
            auto_rows=(dict(row) for row in auto_rows),
        ))
    except ValueError as exc:
        raise LawEnrichmentCompletionError(
            f"{server_id}: auto synonym ownership cannot be proven"
        ) from exc
    approved_raw_counts: dict[int, int] = {}
    physical_terms: dict[int, set[str]] = {}
    for row in approved_rows:
        article_id = int(row["article_id"])
        approved_raw_counts[article_id] = (
            approved_raw_counts.get(article_id, 0) + 1
        )
        term = normalize_synonym_term(row["term"])
        if term is not None:
            physical_terms.setdefault(article_id, set()).add(term)

    registry = await load_exact_signature_registry(queryable, server_id)
    states: list[ArticleEnrichmentCompletion] = []
    for article in sorted(
        selected_rows,
        key=lambda row: (
            int(row["law_document_id"]),
            int(row["source_order"]),
            int(row["id"]),
        ),
    ):
        article_id = int(article["id"])
        document_id = int(article["law_document_id"])
        expected_context_hash = context_hashes.get(article_id)
        if expected_context_hash is None:
            raise LawEnrichmentCompletionError(
                f"{server_id}: article {article_id} has no current context hash"
            )
        tombstone = is_explicitly_empty_or_repealed_article(
            article.get("full_text")
        )
        source_title = resolver(
            article.get("article_ref"),
            str(article.get("full_text") or ""),
        )
        physical = len(physical_terms.get(article_id, set()))
        effective = registry.effective_count(article_id)
        reasons: list[str] = []

        if article.get("enrich_hash") != article.get("article_hash"):
            reasons.append("article_hash_stale")
        stored_context = article.get("enrichment_context_hash")
        if stored_context is None:
            reasons.append("context_hash_missing")
        elif str(stored_context) != expected_context_hash:
            reasons.append("context_hash_stale")
        if not is_safe_enrichment_title_ownership(
            article,
            source_title,
            tombstone,
        ):
            reasons.append("unsafe_title_provenance")
        if article_id in foreign_auto_ids:
            reasons.append("foreign_auto_ownership")
        stored_output_hash = article.get("enrichment_output_hash")
        if stored_output_hash is None:
            reasons.append("output_unknown")
        else:
            try:
                current_output_hash = build_enrichment_output_hash(
                    server_id=server_id,
                    article_id=article_id,
                    title=article.get("title"),
                    title_provenance=article.get("title_provenance"),
                    penalty_summary=article.get("penalty_summary"),
                    penalty_json=article.get("penalty_json"),
                    auto_rows=auto_rows_by_article.get(article_id, ()),
                )
            except (TypeError, ValueError):
                current_output_hash = None
            if current_output_hash != str(stored_output_hash):
                reasons.append("output_mismatch")

        if tombstone:
            if approved_raw_counts.get(article_id, 0) != 0:
                reasons.append("tombstone_has_approved_synonyms")
            if physical != 0:
                reasons.append("tombstone_physical_not_zero")
            if effective != 0:
                reasons.append("tombstone_effective_not_zero")
            if article.get("penalty_summary") is not None:
                reasons.append("tombstone_has_penalty_summary")
            if article.get("penalty_json") is not None:
                reasons.append("tombstone_has_penalty_json")
        else:
            if physical < MIN_REPLACEMENT_PHYSICAL:
                reasons.append("physical_synonyms_below_minimum")
            if effective < MIN_REPLACEMENT_EFFECTIVE:
                reasons.append("effective_synonyms_below_minimum")

        states.append(ArticleEnrichmentCompletion(
            article_id=article_id,
            law_document_id=document_id,
            complete=not reasons,
            reasons=tuple(reasons),
            physical_synonyms=physical,
            effective_synonyms=effective,
            expected_context_hash=expected_context_hash,
        ))

    return LawEnrichmentCompletion(
        server_id=server_id,
        article_states=tuple(states),
    )


async def evaluate_law_enrichment_completion(
    queryable,
    server_id: str,
    *,
    article_ids: Optional[Sequence[int]] = None,
    document_ids: Optional[Sequence[int]] = None,
) -> LawEnrichmentCompletion:
    """Evaluate one coherent snapshot, opening repeatable-read when needed.

    Callers already inside a protected transaction pass its connection and
    retain their existing lock/snapshot contract.  Pool-level callers pass the
    shared ``db_manager``; the wrapper then prevents catalog, synonym and
    metadata queries from observing different commits.
    """

    from database.manager import db_manager

    if queryable is db_manager:
        async with db_manager.transaction() as conn:
            await conn.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            return await _evaluate_law_enrichment_completion_snapshot(
                conn,
                server_id,
                article_ids=article_ids,
                document_ids=document_ids,
            )
    return await _evaluate_law_enrichment_completion_snapshot(
        queryable,
        server_id,
        article_ids=article_ids,
        document_ids=document_ids,
    )
