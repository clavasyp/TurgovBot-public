"""Shared validation and writes for law-search synonyms.

The runtime normalizes a user query *before* looking in ``law_synonyms``.  This
module therefore stores both a useful surface form and its runtime-canonical
form, and uses the exact search leg's PostgreSQL signature to prevent new
cross-article exact collisions.

All writers (regular LLM enrichment and additive backfills) go through this
module.  Additive writes only touch ``law_synonyms``.
"""

from __future__ import annotations

import re
import unicodedata
import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from database.manager import db_manager
from services.law_enrichment_exact_duplicates import (
    ExactDuplicateError,
    build_exact_duplicate_plan,
    close_targets_over_exact_duplicate_groups,
)
from services.law_enrichment_competitors import select_enrichment_competitors
from services.law_enrichment_context import (
    build_enrichment_context_digests,
)
from services.law_enrichment_artifact import normalize_penalty_json
from services.law_enrichment_output import (
    build_enrichment_output_hash,
    foreign_auto_ownership_article_ids,
)
from services.law_enrichment_ownership import (
    is_safe_enrichment_title_ownership,
)
from services.law_catalog_truth import (
    LawCatalogTruthError,
    load_verified_law_catalog_truth,
    validate_law_source_catalog_binding,
)


MIN_TERM_LENGTH = 2
MAX_TERM_LENGTH = 80

# --- Пол записи и планка качества: две РАЗНЫЕ работы одного числа ----------
#
# Раньше число 12 делало две работы сразу: было ГЕЙТОМ ЗАПИСИ и одновременно
# СИГНАЛОМ КАЧЕСТВА.  Замер показал цену такого совмещения: при медиане
# семь выживших синонимов на статью порог 12 брали 2 статьи из 269, то есть
# обогащение вообще не доходило до записи, а альтернативой записанной статье
# с шестью синонимами был НОЛЬ синонимов, а не двенадцать.
#
# Поэтому роли расщеплены.
#
#   ПОЛ ЗАПИСИ (``BULK_REPLACEMENT_*``) — 5.  Ниже него писать нельзя.
#   ПЛАНКА КАЧЕСТВА (``ENRICHMENT_QUALITY_TARGET``) — 12.  Нехватка до неё
#   больше НЕ запрещает запись; она поднимает ПОМЕТКУ НА ПЕРЕОБОГАЩЕНИЕ,
#   и статья переспрашивается отдельной кампанией.
#
# Контракт доверия при этом не сдвинут ни на байт: каждый записываемый
# синоним по-прежнему проходит тот же строгий валидатор.  Порог решает
# только «сколько», и никогда «насколько честно».
#
# ``BULK_REPLACEMENT_*`` по-прежнему зажат ``max()`` в писателе, чтобы
# аргумент вызывающего не мог ослабить пол.
#
# Пол записи — РЕШЕНИЕ ЗАКАЗЧИКА, а не настройка развёртывания.  Ручки у него
# нет намеренно (M-B): переменная окружения мимо ``config.py`` позволяла бы
# опустить и порог записи, и счётное правило строгого гейта до 2, то есть
# ниже принятого решения, причём молча и без следа в истории.  Кому нужно
# строже — поднимает порог АРГУМЕНТОМ вызова: ``max()`` в писателе пропускает
# ужесточение и не пропускает ослабление.
#
# Аудит завершённости (``MIN_REPLACEMENT_*``) обязан быть НЕ ВЫШЕ пола
# записи, иначе записанная статья тут же объявляется неполной и сервер уходит
# в вечный цикл «записали → объявили неполной → pending → повтор, который
# ничего не выбирает, потому что статья уже не устарела».  Поэтому он следует
# за полом вниз и никогда не поднимается выше исторического значения 8.
#
# M-E: это понижение 8 → 5 задевает НЕ ТОЛЬКО аудит завершённости.
# ``MIN_REPLACEMENT_*`` — ещё и умолчание одностатейного писателя
# (``law_catalog_service._write_enrichment`` и
# ``_write_enrichment_in_transaction``) и порог ``replace_auto_synonyms``,
# через который идёт применение reviewed-артефактов.  Значит вместе с полом
# опускается и минимум, ниже которого одна статья не запишется НИ ОДНИМ из
# этих путей: 8 → 5.  Это осознанная часть решения, а не побочный эффект.
BULK_REPLACEMENT_PHYSICAL_DEFAULT = 5
MIN_REPLACEMENT_PHYSICAL_DEFAULT = 8
ENRICHMENT_QUALITY_TARGET = 12

BULK_REPLACEMENT_PHYSICAL = BULK_REPLACEMENT_PHYSICAL_DEFAULT
BULK_REPLACEMENT_EFFECTIVE = BULK_REPLACEMENT_PHYSICAL
MIN_REPLACEMENT_PHYSICAL = min(
    MIN_REPLACEMENT_PHYSICAL_DEFAULT,
    BULK_REPLACEMENT_PHYSICAL,
)
MIN_REPLACEMENT_EFFECTIVE = MIN_REPLACEMENT_PHYSICAL
AUTO_REPLACEMENT_CATALOG_BINDING_SCHEMA = (
    "law-auto-replacement-catalog-v1"
)


class SynonymWriteError(RuntimeError):
    """The requested synonym write failed a fail-closed safety check."""


def _materialize_auto_replacement_catalog(
    catalog_rows: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    """Read every caller-owned catalog row exactly once into owned memory."""

    if isinstance(catalog_rows, (str, bytes)) or not isinstance(
        catalog_rows,
        Sequence,
    ):
        raise SynonymWriteError(
            "catalog binding requires a sequence of catalog rows"
        )
    materialized: list[Dict[str, Any]] = []
    for row in catalog_rows:
        if not isinstance(row, Mapping):
            raise SynonymWriteError(
                "catalog binding requires mapping catalog rows"
            )
        try:
            materialized.append(deepcopy(dict(row)))
        except (TypeError, ValueError) as exc:
            raise SynonymWriteError(
                "catalog snapshot cannot be materialized safely"
            ) from exc
    return materialized


def _build_auto_replacement_catalog_binding_from_materialized(
    server_id: str,
    catalog_rows: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    """Hash the exact owned rows that will also become writer truth."""

    if not isinstance(server_id, str) or not server_id:
        raise SynonymWriteError(
            "catalog binding requires a non-empty server id"
        )
    payload = {
        "schema": AUTO_REPLACEMENT_CATALOG_BINDING_SCHEMA,
        "server_id": server_id,
        "catalog_rows": catalog_rows,
    }
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SynonymWriteError(
            "catalog snapshot cannot be deterministically serialized"
        ) from exc
    return {
        "catalog_binding_schema": (
            AUTO_REPLACEMENT_CATALOG_BINDING_SCHEMA
        ),
        "catalog_binding_hash": hashlib.sha256(encoded).hexdigest(),
    }


def build_auto_replacement_catalog_binding(
    server_id: str,
    catalog_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, str]:
    """Bind every captured catalog field to one versioned deterministic hash.

    The full catalog includes structural rows as well as articles.  Exact-body
    topology and its trusted section/chapter prefixes therefore cannot be
    safely reconstructed from a detached caller-owned copy.  This binding is
    persisted inside the service-generated generation snapshot and rechecked
    by every complete-replacement writer before its first side effect.
    The normal bulk path additionally re-captures and compares the complete
    snapshot under the synonym lock; this hash is its deterministic catalog
    cross-binding, not a replacement for that database-backed race check.
    """

    materialized = _materialize_auto_replacement_catalog(catalog_rows)
    return _build_auto_replacement_catalog_binding_from_materialized(
        server_id,
        materialized,
    )


def validate_auto_replacement_catalog_binding(
    server_id: str,
    generation_snapshot: Mapping[str, Any],
    catalog_rows: Sequence[Mapping[str, Any]],
) -> list[Dict[str, Any]]:
    """Return a defensive catalog copy only when it matches the snapshot."""

    if not isinstance(generation_snapshot, Mapping):
        raise SynonymWriteError(
            "server-wide enrichment lacks its generation snapshot"
        )
    materialized = _materialize_auto_replacement_catalog(catalog_rows)
    expected_binding = (
        _build_auto_replacement_catalog_binding_from_materialized(
            server_id,
            materialized,
        )
    )
    if (
        generation_snapshot.get("catalog_binding_schema")
        != expected_binding["catalog_binding_schema"]
        or generation_snapshot.get("catalog_binding_hash")
        != expected_binding["catalog_binding_hash"]
    ):
        raise SynonymWriteError(
            "server-wide enrichment catalog differs from its captured "
            "generation snapshot"
        )
    captured_row_count = generation_snapshot.get("catalog_rows")
    if (
        captured_row_count is not None
        and (
            isinstance(captured_row_count, bool)
            or not isinstance(captured_row_count, int)
            or captured_row_count != len(materialized)
        )
    ):
        raise SynonymWriteError(
            "server-wide enrichment catalog row count differs from its "
            "generation snapshot"
        )
    return materialized


def synonym_write_lock_key(server_id: str) -> str:
    return f"law-synonyms:{server_id}"


async def acquire_synonym_write_lock(conn, server_id: str) -> None:
    """Serialize every synonym mutator for one server in a DB transaction."""

    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        synonym_write_lock_key(server_id),
    )


@dataclass(frozen=True)
class ExactLegSignature:
    """The fields compared by ``_leg_synonym_exact``."""

    lexemes: Tuple[str, ...]
    has_without: bool
    has_not: bool
    has_without_purpose: bool
    has_with_purpose: bool


# Short terms do not participate in the exact leg.  They still need an
# ownership key so the coverage metric cannot count the same canonical token
# for several articles as if it were unambiguous.
EffectiveKey = Tuple[str, object]


@dataclass
class ExactSignatureRegistry:
    """In-memory view built from PostgreSQL exact-leg signatures.

    ``records`` retains every approved physical row, including surface-only
    rows.  A row contributes exact-leg coverage only when its term is reachable
    after one runtime query-normalization step from another physical row of the
    same article.  Keeping the physical rows is important for non-idempotent
    normalization chains: adding/removing a surface row can activate/deactivate
    its stored canonical companion.

    Existing collisions are preserved but are not considered effective
    coverage; this task must not destructively rewrite legacy data.
    """

    records: Dict[Tuple[int, str, str], EffectiveKey] = field(default_factory=dict)
    active_records: set[Tuple[int, str, str]] = field(
        default_factory=set,
        repr=False,
    )
    article_records: Dict[int, set[Tuple[int, str, str]]] = field(
        default_factory=lambda: defaultdict(set),
        repr=False,
    )
    owners: Dict[EffectiveKey, Dict[int, int]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    effective_counts: Dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def _activate(
        self,
        record_key: Tuple[int, str, str],
        key: EffectiveKey,
    ) -> None:
        article_id = record_key[0]
        counts = self.owners.setdefault(key, {})
        before = set(counts)
        if len(before) == 1:
            self.effective_counts[next(iter(before))] -= 1
        self.active_records.add(record_key)
        counts[article_id] = counts.get(article_id, 0) + 1
        after = set(counts)
        if len(after) == 1:
            self.effective_counts[next(iter(after))] += 1

    def _deactivate(
        self,
        record_key: Tuple[int, str, str],
        key: EffectiveKey,
    ) -> None:
        article_id = record_key[0]
        counts = self.owners.get(key)
        if not counts:
            self.active_records.discard(record_key)
            return
        before = set(counts)
        if len(before) == 1:
            self.effective_counts[next(iter(before))] -= 1
        self.active_records.discard(record_key)
        remaining = counts.get(article_id, 0) - 1
        if remaining > 0:
            counts[article_id] = remaining
        else:
            counts.pop(article_id, None)
        if not counts:
            self.owners.pop(key, None)
        else:
            after = set(counts)
            if len(after) == 1:
                self.effective_counts[next(iter(after))] += 1

    def _rebuild_article(self, article_id: int) -> None:
        article_id = int(article_id)
        record_keys = self.article_records.get(article_id, set())
        for record_key in tuple(record_keys & self.active_records):
            self._deactivate(record_key, self.records[record_key])

        reachable = _query_reachable_canonical_terms(
            record_key[2]
            for record_key in record_keys
        )
        for record_key in record_keys:
            if record_key[2] in reachable:
                self._activate(record_key, self.records[record_key])

    def add(self, article_id: int, source: str, term: str, key: EffectiveKey) -> None:
        article_id = int(article_id)
        record_key = (article_id, str(source), term)
        if record_key in self.records:
            return
        self.records[record_key] = key
        self.article_records[article_id].add(record_key)
        self._rebuild_article(article_id)

    def remove(self, article_id: int, source: str, term: str) -> None:
        article_id = int(article_id)
        record_key = (article_id, str(source), term)
        key = self.records.get(record_key)
        if key is None:
            return
        if record_key in self.active_records:
            self._deactivate(record_key, key)
        self.records.pop(record_key, None)
        article_records = self.article_records.get(article_id)
        if article_records is not None:
            article_records.discard(record_key)
            if not article_records:
                self.article_records.pop(article_id, None)
        self._rebuild_article(article_id)

    def other_owners(self, key: EffectiveKey, article_id: int) -> set[int]:
        return {
            owner
            for owner in self.owners.get(key, {})
            if owner != int(article_id)
        }

    def effective_count(self, article_id: int) -> int:
        """Count unambiguous canonical keys owned only by this article."""

        return max(0, int(self.effective_counts.get(int(article_id), 0)))

    def clone(self) -> "ExactSignatureRegistry":
        """Return an isolated registry suitable for transaction previews."""

        return deepcopy(self)

    def replace_with(self, other: "ExactSignatureRegistry") -> None:
        """Publish a successful preview without changing this object's identity."""

        self.records = deepcopy(other.records)
        self.active_records = deepcopy(other.active_records)
        self.article_records = deepcopy(other.article_records)
        self.owners = deepcopy(other.owners)
        self.effective_counts = deepcopy(other.effective_counts)


@dataclass(frozen=True, order=True)
class SynonymConflict:
    """One deterministic reason why a staged physical term is unsafe."""

    candidate_article_id: int
    candidate_term: str
    reason: str
    owner_article_id: int
    owner_term: str


@dataclass
class SynonymWriteResult:
    inserted: int = 0
    already_present: int = 0
    rejected_collisions: int = 0
    rejected_terms: Dict[int, list[str]] = field(default_factory=dict)
    effective_before: Dict[int, int] = field(default_factory=dict)
    effective_after: Dict[int, int] = field(default_factory=dict)
    physical_after: Dict[int, int] = field(default_factory=dict)
    accepted_terms: Dict[int, list[str]] = field(default_factory=dict)
    inserted_rows: list[Dict[str, Any]] = field(default_factory=list)
    conflicts: list[SynonymConflict] = field(default_factory=list)


@dataclass
class AutoSynonymBulkReplacementOutcome:
    """Isolated outcome returned before a caller-owned transaction commits."""

    result: SynonymWriteResult
    final_registry: ExactSignatureRegistry


@dataclass
class _AutoReplacementPreviewState:
    result: SynonymWriteResult
    current_articles: Dict[int, Dict[str, Any]]
    existing_rows: list[Dict[str, Any]]
    accepted: Dict[int, set[str]]
    keyed: Dict[int, Dict[EffectiveKey, set[str]]]
    source_registry: ExactSignatureRegistry
    preview_registry: ExactSignatureRegistry
    replaceable_row_ids: set[int]


@dataclass
class _AutoReplacementGenerationState:
    snapshot: Dict[str, Any]
    catalog_rows: list[Dict[str, Any]]
    articles: list[Dict[str, Any]]
    auto_rows: list[Dict[str, Any]]
    context_hashes: Dict[int, str]


def auto_synonym_state_fingerprint(
    article_id: int,
    rows: Iterable[Mapping[str, Any]],
) -> str:
    """Hash the exact auto-row state captured before remote generation.

    Row ids are intentionally included.  A concurrent delete/reinsert of an
    otherwise identical dictionary is still a new generation and must make a
    later replacement preview fail closed.
    """

    normalized: list[Dict[str, Any]] = []
    for row in rows:
        row_article_id = int(row["article_id"])
        if row_article_id != int(article_id):
            raise ValueError(
                "auto synonym state contains a row from another article"
            )
        if str(row["source"]) != "auto":
            raise ValueError(
                "auto synonym state may contain only source='auto' rows"
            )
        suggested_by = row["suggested_by"]
        normalized.append({
            "id": int(row["id"]),
            "server_id": str(row["server_id"]),
            "term": str(row["term"]),
            "weight": float(row["weight"]),
            "status": str(row["status"]),
            "suggested_by": (
                int(suggested_by)
                if suggested_by is not None
                else None
            ),
        })
    normalized.sort(
        key=lambda row: (
            row["id"],
            row["term"],
            row["status"],
            row["weight"],
            row["suggested_by"] is not None,
            row["suggested_by"] or 0,
        )
    )
    payload = json.dumps(
        {
            "article_id": int(article_id),
            "auto_rows": normalized,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _catalog_source_title_resolver():
    """Load the shared parser resolver lazily to avoid module import cycles."""

    from services.law_catalog_service import source_catalog_title

    return source_catalog_title


def _project_family_for_server(server_id: str) -> str:
    """Return the exact project-family id bound into generation freshness."""

    from law_sources_data import get_project_for_server

    return str(get_project_for_server(server_id) or "")


def build_auto_replacement_context_hashes(
    server_id: str,
    catalog_rows: Sequence[Mapping[str, Any]],
    *,
    exact_duplicate_plan=None,
) -> Dict[int, str]:
    """Derive every article context digest from one authoritative catalog."""

    if exact_duplicate_plan is None:
        exact_duplicate_plan = build_exact_duplicate_plan(
            catalog_rows,
            is_substantive_article=lambda row: not (
                is_explicitly_empty_or_repealed_article(
                    row.get("full_text")
                )
            ),
            canonicalize_scope=canonical_synonym_term,
        )
    return build_enrichment_context_digests(
        catalog_rows,
        exact_duplicate_plan=exact_duplicate_plan,
        competitor_selector=select_enrichment_competitors,
        project_context=project_context_for_server(server_id),
        project_family=_project_family_for_server(server_id),
        source_title_resolver=_catalog_source_title_resolver(),
    )


async def _load_auto_replacement_generation_state(
    conn,
    server_id: str,
    *,
    selection_mode: str,
    lock_articles: bool = False,
    allow_unattested_context: bool = False,
    explicit_target_ids: Optional[Sequence[int]] = None,
) -> _AutoReplacementGenerationState:
    """Build the exact server context that must survive remote generation."""

    if selection_mode not in {"stale", "all", "explicit"}:
        raise SynonymWriteError(
            "auto replacement selection_mode must be stale/all/explicit"
        )
    if selection_mode == "explicit":
        if (
            explicit_target_ids is None
            or any(
                isinstance(article_id, bool)
                or not isinstance(article_id, int)
                for article_id in explicit_target_ids
            )
        ):
            raise SynonymWriteError(
                "explicit replacement requires integer target ids"
            )
        normalized_explicit_ids = sorted(set(explicit_target_ids))
        if not normalized_explicit_ids:
            raise SynonymWriteError(
                "explicit replacement requires at least one target id"
            )
    elif explicit_target_ids is not None:
        raise SynonymWriteError(
            "explicit target ids require selection_mode='explicit'"
        )
    else:
        normalized_explicit_ids = []
    try:
        verified_truth = await load_verified_law_catalog_truth(
            conn,
            server_id,
            lock_rows=lock_articles,
            allow_legacy_title_attestation=allow_unattested_context,
        )
    except LawCatalogTruthError as exc:
        raise SynonymWriteError(
            f"{server_id}: source documents and persisted catalog do not "
            "satisfy the active parser contract; run normal catalog "
            "reconcile before enrichment"
        ) from exc
    catalog_rows = verified_truth.catalog_rows
    captured_catalog = [
        {
            "id": int(row["id"]),
            "server_id": str(row["server_id"]),
            "parent_server_id": str(row["parent_server_id"]),
            "law_document_id": int(row["law_document_id"]),
            "source_order": int(row["source_order"]),
            "article_ref": row["article_ref"],
            "kind": str(row["kind"]),
            "code": row["code"],
            "title": row["title"],
            "doc_title": row["doc_title"],
            "full_text": str(row["full_text"] or ""),
            # Keep both the catalog hash and exact text.  The redundancy makes
            # a corrupted hash-maintenance path fail closed too.
            "article_hash": str(row["article_hash"]),
            "enrich_hash": (
                str(row["enrich_hash"])
                if row["enrich_hash"] is not None
                else None
            ),
            "penalty_summary": (
                str(row["penalty_summary"])
                if row.get("penalty_summary") is not None
                else None
            ),
            # asyncpg commonly exposes json/jsonb through the configured text
            # codec, while tests and callers may already provide a mapping.
            # Normalize both forms before hashing so semantically identical
            # jsonb is stable and every real penalty-only change invalidates
            # the server generation snapshot.
            "penalty_json": normalize_penalty_json(
                row.get("penalty_json")
            ),
            "title_provenance": row.get("title_provenance"),
            "enrichment_context_hash": (
                str(row["enrichment_context_hash"])
                if row.get("enrichment_context_hash") is not None
                else None
            ),
            "enrichment_output_hash": (
                str(row["enrichment_output_hash"])
                if row.get("enrichment_output_hash") is not None
                else None
            ),
        }
        for row in catalog_rows
    ]
    captured_articles = [
        row
        for row in captured_catalog
        if row["kind"] == "article"
    ]
    article_ids = [row["id"] for row in captured_articles]
    auto_rows = []
    if article_ids:
        auto_rows = await conn.fetch(
            """
            SELECT s.id, s.server_id, s.article_id, s.term, s.weight,
                   s.source, s.status, s.suggested_by
            FROM law_synonyms s
            JOIN law_articles a ON a.id = s.article_id
            WHERE a.server_id = $1 AND a.kind = 'article'
              AND s.source = 'auto'
            ORDER BY s.article_id, s.id
            """,
            server_id,
        )
    normalized_auto = [
        {
            "id": int(row["id"]),
            "server_id": str(row["server_id"]),
            "article_id": int(row["article_id"]),
            "term": str(row["term"]),
            "weight": float(row["weight"]),
            "source": str(row["source"]),
            "status": str(row["status"]),
            "suggested_by": (
                int(row["suggested_by"])
                if row["suggested_by"] is not None
                else None
            ),
        }
        for row in auto_rows
    ]
    try:
        foreign_auto_ids = foreign_auto_ownership_article_ids(
            server_id=server_id,
            auto_rows=normalized_auto,
        )
    except ValueError as exc:
        raise SynonymWriteError(
            "auto synonym ownership cannot be validated safely"
        ) from exc
    if foreign_auto_ids:
        raise SynonymWriteError(
            "foreign-server auto synonym ownership for article ids: "
            f"{list(foreign_auto_ids)}"
        )
    auto_by_article: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in normalized_auto:
        auto_by_article[int(row["article_id"])].append(row)
    try:
        exact_duplicate_plan = build_exact_duplicate_plan(
            captured_catalog,
            is_substantive_article=lambda row: (
                not is_explicitly_empty_or_repealed_article(
                    row.get("full_text")
                )
            ),
            canonicalize_scope=canonical_synonym_term,
        )
        current_context_hashes = build_auto_replacement_context_hashes(
            server_id,
            captured_catalog,
            exact_duplicate_plan=exact_duplicate_plan,
        )
    except ExactDuplicateError as exc:
        raise SynonymWriteError(
            "exact duplicate target planning failed for the captured catalog"
        ) from exc
    except ValueError as exc:
        raise SynonymWriteError(
            "enrichment context planning failed for the captured catalog"
        ) from exc
    article_hash_stale_ids = sorted(
        row["id"]
        for row in captured_articles
        if row["enrich_hash"] != row["article_hash"]
    )
    current_without_context = [
        row
        for row in captured_articles
        if (
            row["enrich_hash"] == row["article_hash"]
            and row["enrichment_context_hash"] is None
        )
    ]
    source_title_resolver = _catalog_source_title_resolver()
    current_source_titles = {
        row["id"]: source_title_resolver(
            row.get("article_ref"),
            str(row.get("full_text") or ""),
        )
        for row in captured_articles
    }
    unsafe_provenance_ids = sorted(
        row["id"]
        for row in captured_articles
        if row["enrich_hash"] == row["article_hash"]
        and not is_safe_enrichment_title_ownership(
            row,
            current_source_titles[row["id"]],
            is_explicitly_empty_or_repealed_article(
                row.get("full_text")
            ),
        )
    )
    if unsafe_provenance_ids and not allow_unattested_context:
        raise SynonymWriteError(
            "current enrichment title ownership is not safely attested; run "
            "normal catalog reconcile or versioned provenance attestation "
            f"before Gemini for article ids: {unsafe_provenance_ids}"
        )
    try:
        current_output_hashes = {
            row["id"]: build_enrichment_output_hash(
                server_id=server_id,
                article_id=row["id"],
                title=row.get("title"),
                title_provenance=row.get("title_provenance"),
                penalty_summary=row.get("penalty_summary"),
                penalty_json=row.get("penalty_json"),
                auto_rows=auto_by_article.get(row["id"], ()),
            )
            for row in captured_articles
        }
    except ValueError as exc:
        raise SynonymWriteError(
            "persisted enrichment output cannot be fingerprinted safely"
        ) from exc
    current_article_ids = {
        row["id"]
        for row in captured_articles
        if row["enrich_hash"] == row["article_hash"]
    }
    output_unknown_stale_ids = sorted(
        row["id"]
        for row in captured_articles
        if (
            row["id"] in current_article_ids
            and row["enrichment_output_hash"] is None
        )
    )
    output_mismatch_stale_ids = sorted(
        row["id"]
        for row in captured_articles
        if (
            row["id"] in current_article_ids
            and row["enrichment_output_hash"] is not None
            and row["enrichment_output_hash"]
            != current_output_hashes[row["id"]]
        )
    )
    output_stale_ids = sorted(
        set(output_unknown_stale_ids) | set(output_mismatch_stale_ids)
    )
    context_unknown_stale_ids = sorted(
        row["id"]
        for row in current_without_context
    )
    context_unknown_stale_id_set = set(context_unknown_stale_ids)
    context_stale_ids = sorted(
        row["id"]
        for row in captured_articles
        if row["id"] in context_unknown_stale_id_set
        or (
            row["enrichment_context_hash"] is not None
            and row["enrichment_context_hash"]
            != current_context_hashes[row["id"]]
        )
    )
    stale_target_ids = sorted(
        set(article_hash_stale_ids)
        | set(context_stale_ids)
        | set(output_stale_ids)
        | set(unsafe_provenance_ids)
    )
    if selection_mode == "stale":
        base_target_ids = stale_target_ids
    elif selection_mode == "all":
        base_target_ids = article_ids
    else:
        missing_explicit_ids = sorted(
            set(normalized_explicit_ids) - set(article_ids)
        )
        if missing_explicit_ids:
            raise SynonymWriteError(
                "explicit replacement targets missing/non-article ids: "
                f"{missing_explicit_ids}"
            )
        base_target_ids = normalized_explicit_ids
    required_target_ids = sorted(
        close_targets_over_exact_duplicate_groups(
            base_target_ids,
            exact_duplicate_plan,
        )
    )
    catalog_binding = build_auto_replacement_catalog_binding(
        server_id,
        captured_catalog,
    )
    payload = {
        "server_id": server_id,
        "selection_mode": selection_mode,
        "source_catalog_binding": verified_truth.binding,
        **catalog_binding,
        "catalog_rows": captured_catalog,
        "auto_rows": normalized_auto,
        "current_context_hashes": {
            str(article_id): current_context_hashes[article_id]
            for article_id in sorted(current_context_hashes)
        },
        "article_hash_stale_ids": article_hash_stale_ids,
        "context_unknown_stale_ids": context_unknown_stale_ids,
        "context_stale_ids": context_stale_ids,
        "unsafe_provenance_ids": unsafe_provenance_ids,
        "output_unknown_stale_ids": output_unknown_stale_ids,
        "output_mismatch_stale_ids": output_mismatch_stale_ids,
        "output_stale_ids": output_stale_ids,
        "current_output_hashes": {
            str(article_id): current_output_hashes[article_id]
            for article_id in sorted(current_output_hashes)
        },
        "stale_target_ids": stale_target_ids,
        "required_target_ids": required_target_ids,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    snapshot = {
        "algorithm": "sha256",
        "server_id": server_id,
        "selection_mode": selection_mode,
        "source_catalog_binding": deepcopy(verified_truth.binding),
        **catalog_binding,
        "catalog_rows": len(captured_catalog),
        "article_rows": len(captured_articles),
        "auto_rows": len(normalized_auto),
        "article_hash_stale_ids": article_hash_stale_ids,
        "context_unknown_stale_ids": context_unknown_stale_ids,
        "context_stale_ids": context_stale_ids,
        "unsafe_provenance_ids": unsafe_provenance_ids,
        "output_unknown_stale_ids": output_unknown_stale_ids,
        "output_mismatch_stale_ids": output_mismatch_stale_ids,
        "output_stale_ids": output_stale_ids,
        "stale_target_ids": stale_target_ids,
        "required_target_ids": required_target_ids,
        "value": hashlib.sha256(encoded).hexdigest(),
    }
    return _AutoReplacementGenerationState(
        snapshot=snapshot,
        catalog_rows=captured_catalog,
        articles=captured_articles,
        auto_rows=normalized_auto,
        context_hashes=current_context_hashes,
    )


def _export_auto_replacement_generation_state(
    state: _AutoReplacementGenerationState,
) -> Dict[str, Any]:
    auto_by_article: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in state.auto_rows:
        auto_by_article[int(row["article_id"])].append(row)
    return {
        "snapshot": deepcopy(state.snapshot),
        "catalog_rows": deepcopy(state.catalog_rows),
        "articles": deepcopy(state.articles),
        "auto_rows": deepcopy(state.auto_rows),
        "context_hashes": deepcopy(state.context_hashes),
        "auto_fingerprints": {
            int(article["id"]): auto_synonym_state_fingerprint(
                int(article["id"]),
                auto_by_article.get(int(article["id"]), ()),
            )
            for article in state.articles
        },
    }


async def load_auto_replacement_generation_state_in_transaction(
    conn,
    server_id: str,
    *,
    only_stale: bool = True,
    lock_articles: bool = False,
) -> Dict[str, Any]:
    """Load the shared context while the caller owns the synonym lock/tx."""

    if not isinstance(only_stale, bool) or not isinstance(lock_articles, bool):
        raise SynonymWriteError("only_stale and lock_articles must be bool")
    state = await _load_auto_replacement_generation_state(
        conn,
        server_id,
        selection_mode="stale" if only_stale else "all",
        lock_articles=lock_articles,
    )
    return _export_auto_replacement_generation_state(state)


async def load_enrichment_state_for_attestation_in_transaction(
    conn,
    server_id: str,
    *,
    lock_articles: bool = False,
) -> Dict[str, Any]:
    """Load all source/context facts for the versioned ownership attester.

    This is the sole path allowed to inspect current enrichment whose context
    or title ownership is still unattested.  It never marks that state fresh;
    the caller may classify provenance but must leave unknown context for
    regeneration.  Normal Gemini capture intentionally has no such bypass.
    """

    if not isinstance(lock_articles, bool):
        raise SynonymWriteError("lock_articles must be bool")
    state = await _load_auto_replacement_generation_state(
        conn,
        server_id,
        selection_mode="all",
        lock_articles=lock_articles,
        allow_unattested_context=True,
    )
    exported = _export_auto_replacement_generation_state(state)
    approved_rows = await conn.fetch(
        """
        SELECT s.article_id,
               s.server_id AS synonym_server_id,
               COUNT(*)::bigint AS approved_count
        FROM law_synonyms s
        JOIN law_articles a ON a.id = s.article_id
        WHERE a.server_id = $1
          AND a.kind = 'article'
          AND s.status = 'approved'
        GROUP BY s.article_id, s.server_id
        ORDER BY s.article_id, s.server_id
        """,
        server_id,
    )
    article_ids = {
        int(article["id"])
        for article in exported.get("articles", ())
    }
    approved_counts: Dict[int, int] = {}
    for row in approved_rows:
        raw_article_id = row["article_id"]
        raw_count = row["approved_count"]
        if (
            not isinstance(raw_article_id, int)
            or isinstance(raw_article_id, bool)
            or raw_article_id not in article_ids
            or not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count <= 0
        ):
            raise SynonymWriteError(
                "approved synonym ownership cannot be validated safely"
            )
        if str(row["synonym_server_id"]) != server_id:
            raise SynonymWriteError(
                "approved synonym has foreign server ownership for "
                f"article {raw_article_id}"
            )
        if raw_article_id in approved_counts:
            raise SynonymWriteError(
                "approved synonym counts contain duplicate article ownership"
            )
        approved_counts[raw_article_id] = raw_count
    exported["approved_synonym_counts"] = approved_counts
    return exported


async def load_auto_replacement_generation_state_for_targets_in_transaction(
    conn,
    server_id: str,
    target_ids: Sequence[int],
    *,
    lock_articles: bool = False,
) -> Dict[str, Any]:
    """Capture one explicit reviewed target set against full server state."""

    if not isinstance(lock_articles, bool):
        raise SynonymWriteError("lock_articles must be bool")
    state = await _load_auto_replacement_generation_state(
        conn,
        server_id,
        selection_mode="explicit",
        lock_articles=lock_articles,
        explicit_target_ids=target_ids,
    )
    return _export_auto_replacement_generation_state(state)


async def capture_auto_replacement_generation_state_for_targets(
    server_id: str,
    target_ids: Sequence[int],
) -> Dict[str, Any]:
    """Re-capture one explicit target subset with the same write lock.

    Поштучная приёмка может отбросить отдельную статью уже ПОСЛЕ первого
    снимка.  Писатель же требует точного равенства staged-набора и
    ``required_target_ids`` снимка, поэтому уменьшенный набор обязан получить
    свой собственный, самосогласованный снимок — иначе запись честно
    отвергается как рассинхронизированная.  Никаких послаблений: снимок
    берётся тем же загрузчиком и под тем же замком, а CAS по хешам остаётся.
    """

    normalized = sorted({int(value) for value in target_ids})
    if not normalized:
        raise SynonymWriteError(
            "explicit generation re-capture requires a non-empty target set"
        )
    async with db_manager.transaction() as conn:
        await conn.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )
        await acquire_synonym_write_lock(conn, server_id)
        return (
            await load_auto_replacement_generation_state_for_targets_in_transaction(
                conn,
                server_id,
                normalized,
                lock_articles=False,
            )
        )


async def capture_auto_replacement_generation_state(
    server_id: str,
    *,
    only_stale: bool = True,
) -> Dict[str, Any]:
    """Capture token and exact prompt source rows before Gemini/network.

    ``catalog_rows`` contains every structural and article row used to derive
    exact-body groups.  ``articles`` contains the article-only prompt subset.
    The versioned catalog binding inside ``snapshot`` prevents either exported
    copy from being detached from these exact captured rows later.
    ``auto_rows`` contains every status of ``source='auto'``; owner/user rows
    are deliberately excluded and are rechecked live by the PostgreSQL
    collision gate.  Per-article fingerprints are returned so a caller never
    has to reconstruct the CAS contract independently.
    """

    if not isinstance(only_stale, bool):
        raise SynonymWriteError("only_stale must be bool")
    async with db_manager.transaction() as conn:
        await conn.execute(
            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
        )
        await acquire_synonym_write_lock(conn, server_id)
        if only_stale:
            hash_context_state = (
                await load_auto_replacement_generation_state_in_transaction(
                    conn,
                    server_id,
                    only_stale=True,
                )
            )
            # The authoritative completion contract is intentionally broader
            # than body/context/output hashes: searchable quality (8/8) and
            # tombstone 0/0 must also be repairable by the autonomous updater.
            # Import lazily to avoid a module cycle: the evaluator itself uses
            # this module's pure registry helpers, never this capture wrapper.
            from services.law_enrichment_completion import (
                evaluate_law_enrichment_completion,
            )

            completion = await evaluate_law_enrichment_completion(
                conn,
                server_id,
            )
            incomplete_ids = list(completion.incomplete_article_ids)
            # Preserve the normal ``stale`` generation contract when its exact
            # closure already covers every authoritative incomplete target.
            # Switch to explicit mode only for additional quality/output
            # targets that the old hash-only selector could not see.
            if not (
                set(incomplete_ids)
                <= set(
                    hash_context_state["snapshot"]["required_target_ids"]
                )
            ):
                return (
                    await load_auto_replacement_generation_state_for_targets_in_transaction(
                        conn,
                        server_id,
                        incomplete_ids,
                        lock_articles=False,
                    )
                )
            return hash_context_state
        return await load_auto_replacement_generation_state_in_transaction(
            conn,
            server_id,
            only_stale=only_stale,
        )


_SPACE_RE = re.compile(r"\s+")


def normalize_synonym_term(value: Any) -> Optional[str]:
    """Unicode/lower/ё/whitespace normalization shared by every writer."""

    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _SPACE_RE.sub(" ", text).strip().lower().replace("ё", "е")
    if not (MIN_TERM_LENGTH <= len(text) <= MAX_TERM_LENGTH):
        return None
    return text


@lru_cache(maxsize=100_000)
def _canonical_from_surface(surface: str) -> Optional[str]:
    from services.law_search_service import normalize_query

    return normalize_synonym_term(normalize_query(surface).cleaned)


def canonical_synonym_term(value: Any) -> Optional[str]:
    """Return exactly the form that reaches synonym search at runtime."""

    surface = normalize_synonym_term(value)
    if surface is None:
        return None
    # Lazy cached import avoids repeatedly running the full normalizer for
    # duplicate terms while scanning a large server dictionary.
    return _canonical_from_surface(surface)


def _query_reachable_canonical_terms(values: Iterable[Any]) -> set[str]:
    """Return stored terms reachable after exactly one runtime normalization.

    ``normalize_query`` is deliberately not iterated to a fixed point at
    runtime.  Therefore a valid physical group may be ``surface -> canonical1
    -> canonical2``: a query entered as ``surface`` reaches and matches the
    stored ``canonical1`` row, even though normalizing ``canonical1`` again
    would produce ``canonical2``.  A non-fixed-point canonical row without its
    proving surface remains ineffective.
    """

    physical = {
        normalized
        for value in values
        if (normalized := normalize_synonym_term(value)) is not None
    }
    reachable = {
        canonical
        for surface in physical
        if (canonical := canonical_synonym_term(surface)) is not None
    }
    return physical & reachable


def expand_synonym_terms(values: Iterable[Any]) -> Dict[str, set[str]]:
    """Map canonical form -> surface/canonical rows that should be stored."""

    groups: Dict[str, set[str]] = {}
    for value in values or ():
        surface = normalize_synonym_term(value)
        canonical = canonical_synonym_term(value)
        if surface is None or canonical is None:
            continue
        groups.setdefault(canonical, set()).update((surface, canonical))
    return groups


def validate_runtime_canonical_synonym_claims(
    items: Sequence[Mapping[str, Any]],
) -> Dict[int, list[str]]:
    """Normalize and prove unique runtime ownership before any writer runs.

    PostgreSQL stores physical surface forms, while the exact search leg sees
    the value after the runtime query normalizer.  Therefore two visibly
    different strings can still claim the same effective phrase.  This pure
    preflight is shared by autonomous Gemini and reviewed-artifact writers so
    neither path can rely on first-wins database insertion.
    """

    normalized_by_id: Dict[int, list[str]] = {}
    canonical_owners: Dict[str, set[int]] = defaultdict(set)
    for item in items:
        try:
            article_id = int(item["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SynonymWriteError(
                "every synonym claim item must contain an integer id"
            ) from exc
        if isinstance(item.get("id"), bool) or article_id in normalized_by_id:
            raise SynonymWriteError(
                f"duplicate or invalid synonym claim article id {article_id}"
            )
        raw_values = item.get("synonyms")
        if (
            isinstance(raw_values, (str, bytes))
            or not isinstance(raw_values, Sequence)
        ):
            raise SynonymWriteError(
                f"article {article_id} synonyms must be an array"
            )

        surfaces: list[str] = []
        seen_surfaces: set[str] = set()
        seen_canonical: set[str] = set()
        for value in raw_values:
            if not isinstance(value, str):
                raise SynonymWriteError(
                    f"article {article_id} has a non-string synonym"
                )
            surface = normalize_synonym_term(value)
            canonical = canonical_synonym_term(value)
            if surface is None or canonical is None:
                raise SynonymWriteError(
                    f"article {article_id} has a synonym outside shared bounds"
                )
            if surface in seen_surfaces:
                raise SynonymWriteError(
                    f"article {article_id} has duplicate normalized synonyms"
                )
            if canonical in seen_canonical:
                raise SynonymWriteError(
                    f"article {article_id} has duplicate runtime-canonical "
                    "synonym groups"
                )
            seen_surfaces.add(surface)
            seen_canonical.add(canonical)
            surfaces.append(surface)
            canonical_owners[canonical].add(article_id)
        normalized_by_id[article_id] = surfaces

    collisions = {
        canonical: sorted(article_ids)
        for canonical, article_ids in canonical_owners.items()
        if len(article_ids) > 1
    }
    if collisions:
        raise SynonymWriteError(
            "synonym claims reuse canonical synonym groups across articles: "
            f"{list(sorted(collisions.items()))[:5]}"
        )
    return normalized_by_id


_TOMBSTONE_HEADER_RE = re.compile(
    r"^\s*(?:(?:статья|article|глава|раздел)\s+)?"
    r"(?:\d+|[ivxlcdm]+)(?:[.\-](?:\d+|[ivxlcdm]+))*"
    r"(?:\s+от\s+[★*]+\s+до\s+[★*]+)?"
    r"(?:\s*[★*]+)?"
    r"(?:\s*(?:\[[^\]\r\n]+\]|\([^()\r\n]+\)))?"
    r"[\s.:\-–—]*",
    re.IGNORECASE,
)
_REPEALED_RE = re.compile(
    r"^(?:(?:настоящая|данная)\s+)?"
    r"(?:(?:статья|норма)\s+)?"
    r"(?:утратил(?:а|о|и)?\s+силу|"
    r"отменен(?:а|о)?|"
    r"упразднен(?:а|о|ы)?|"
    r"исключен(?:а|о)?|"
    r"не\s+действует)"
    r"(?:\s+(?:в\s+связи\s+с\s+)?решением\s+"
    r"(?:"
    r"(?:(?:верховного|федерального|окружного|апелляционного)\s+)?суда"
    r"(?:\s+(?:штата\s+(?:san[- ]andreas|сан[- ]андреас)|"
    r"соединенных\s+штатов(?:\s+америки)?))?"
    # NFKC turns the numero sign into ``No`` before this expression runs.
    r"(?:\s+по\s+исковому\s+заявлению\s+(?:№|no\.?)\s*"
    r"[A-Za-zА-Яа-яЁё0-9./-]+)?"
    r"|(?:конгресса|правительства|губернатора|"
    r"законодательного\s+собрания|парламента|сената)"
    r"(?:\s+(?:штата\s+(?:san[- ]andreas|сан[- ]андреас)|"
    r"соединенных\s+штатов(?:\s+америки)?))?"
    r")"
    r"\s+от\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    r"(?:\s*г(?:ода)?)?)?"
    r"(?:\s*[.()]|\s+)*$",
    re.IGNORECASE,
)
_TOMBSTONE_REPEAL_FOOTER_RE = re.compile(
    r"^(?:"
    r"вступает\s+в\s+юридическую\s+силу\s+после\s+подписания\s+"
    r"губернатором\s+(?:штата\s+)?"
    r"(?:san[- ]andreas|сан[- ]андреас)\s+"
    r"[A-Za-zА-Яа-яЁё]+(?:[- ][A-Za-zА-Яа-яЁё]+){0,3}"
    r"|нормативный\s+правовой\s+акт\s+был\s+подписан\s+"
    r"губернатором\s+(?:штата\s+)?"
    r"(?:san[- ]andreas|сан[- ]андреас)\s+"
    r"[A-Za-zА-Яа-яЁё]+(?:[- ][A-Za-zА-Яа-яЁё]+){0,3}"
    r"\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
    r")\s*\.?$",
    re.IGNORECASE,
)
_KNOWN_INVISIBLE_FORMAT_RE = re.compile("[\u200b-\u200d\u2060\ufeff]")
_TOMBSTONE_EDGE_DECORATION = " \t\r\n[]{}(),.;:—–-*★"
_TOMBSTONE_QUOTE_PAIRS = {
    ('"', '"'),
    ("«", "»"),
    ("“", "”"),
    ("„", "“"),
}


def _is_explicit_repeal_formula(value: str) -> bool:
    if _REPEALED_RE.fullmatch(value):
        return True
    if len(value) < 2 or (value[0], value[-1]) not in _TOMBSTONE_QUOTE_PAIRS:
        return False
    quoted = value[1:-1].strip(_TOMBSTONE_EDGE_DECORATION)
    return bool(quoted and _REPEALED_RE.fullmatch(quoted))


def is_explicitly_empty_or_repealed_article(full_text: Any) -> bool:
    """Recognize only an actually empty/repealed article, never a vague short norm."""

    raw = unicodedata.normalize("NFKC", str(full_text or ""))
    # Forum markup sometimes leaves these invisible formatting characters
    # after the final dot.  Remove only the explicitly known set; broad
    # Unicode-category stripping could accidentally join substantive words.
    raw = _KNOWN_INVISIBLE_FORMAT_RE.sub("", raw)
    lines = [
        _SPACE_RE.sub(" ", line).strip()
        for line in raw.splitlines()
        if line.strip()
    ]
    if not lines:
        return True
    text = " ".join(lines).strip()
    direct = text.strip(_TOMBSTONE_EDGE_DECORATION)
    if _is_explicit_repeal_formula(direct):
        return True

    header = _TOMBSTONE_HEADER_RE.match(text)
    if header is None:
        return False
    remainder = text[header.end():].strip(_TOMBSTONE_EDGE_DECORATION)
    if not remainder:
        return True
    if _is_explicit_repeal_formula(remainder):
        return True

    # Some forum articles retain only a fully dated repeal statement followed
    # by standard enactment/signature boilerplate.  Strip that boilerplate
    # line-by-line and only from the end, then require a complete repeal marker.
    # In particular, a bare article header plus a signature footer is not a
    # tombstone, and any substantive line left between the marker and footer
    # keeps the article non-exempt.
    repeal_lines = list(lines)
    while (
        len(repeal_lines) > 1
        and _TOMBSTONE_REPEAL_FOOTER_RE.fullmatch(repeal_lines[-1])
    ):
        repeal_lines.pop()
    if len(repeal_lines) == len(lines):
        return False

    repeal_text = " ".join(repeal_lines).strip()
    direct = repeal_text.strip(_TOMBSTONE_EDGE_DECORATION)
    if _is_explicit_repeal_formula(direct):
        return True
    header = _TOMBSTONE_HEADER_RE.match(repeal_text)
    if header is None:
        return False
    remainder = repeal_text[header.end():].strip(_TOMBSTONE_EDGE_DECORATION)
    return bool(remainder and _is_explicit_repeal_formula(remainder))


async def _signature_map(queryable, terms: Sequence[str]) -> Dict[str, Optional[ExactLegSignature]]:
    unique = list(dict.fromkeys(terms))
    if not unique:
        return {}
    rows = await queryable.fetch(
        """
        SELECT term,
               tsvector_to_array(to_tsvector('russian', term)) AS lexemes,
               to_tsvector('simple', term) @@ plainto_tsquery('simple', 'без')
                   AS has_without,
               to_tsvector('simple', term) @@ plainto_tsquery('simple', 'не')
                   AS has_not,
               to_tsvector('simple', term) @@ phraseto_tsquery('simple', 'без цели')
                   AS has_without_purpose,
               to_tsvector('simple', term) @@ phraseto_tsquery('simple', 'с целью')
                   AS has_with_purpose
        FROM unnest($1::text[]) AS candidate(term)
        """,
        unique,
    )
    result: Dict[str, Optional[ExactLegSignature]] = {}
    for row in rows:
        term = str(row["term"])
        lexemes = tuple(row["lexemes"] or ())
        if len(term) < 4 or not lexemes:
            result[term] = None
            continue
        result[term] = ExactLegSignature(
            lexemes=lexemes,
            has_without=bool(row["has_without"]),
            has_not=bool(row["has_not"]),
            has_without_purpose=bool(row["has_without_purpose"]),
            has_with_purpose=bool(row["has_with_purpose"]),
        )
    return result


def _effective_key(term: str, signature: Optional[ExactLegSignature]) -> EffectiveKey:
    if signature is None:
        return ("short", term)
    return ("exact", signature)


async def load_exact_signature_registry(
    queryable,
    server_id: str,
) -> ExactSignatureRegistry:
    """Load all approved physical terms and derive query-reachable coverage."""

    rows = await queryable.fetch(
        """
        SELECT s.article_id, s.source, s.term
        FROM law_synonyms s
        JOIN law_articles a ON a.id = s.article_id
        WHERE s.server_id = $1 AND a.server_id = $1
          AND a.kind = 'article' AND s.status = 'approved'
        """,
        server_id,
    )
    physical_rows: list[Tuple[int, str, str]] = []
    physical_terms: list[str] = []
    for row in rows:
        surface = normalize_synonym_term(row["term"])
        if surface is None:
            continue
        physical_rows.append((int(row["article_id"]), str(row["source"]), surface))
        physical_terms.append(surface)

    signatures = await _signature_map(queryable, physical_terms)
    registry = ExactSignatureRegistry()
    for article_id, source, term in physical_rows:
        registry.add(article_id, source, term, _effective_key(term, signatures.get(term)))
    return registry


async def effective_synonym_counts(
    queryable,
    server_id: str,
    article_ids: Optional[Sequence[int]] = None,
) -> Dict[int, int]:
    registry = await load_exact_signature_registry(queryable, server_id)
    if article_ids is None:
        rows = await queryable.fetch(
            "SELECT id FROM law_articles WHERE server_id = $1 AND kind = 'article'",
            server_id,
        )
        article_ids = [int(row["id"]) for row in rows]
    return {
        int(article_id): registry.effective_count(int(article_id))
        for article_id in article_ids
    }


async def synonym_corpus_fingerprint(queryable, server_id: str) -> Dict[str, Any]:
    """Bind a generated plan to the exact synonym corpus it was reviewed against."""

    rows = await queryable.fetch(
        """
        SELECT s.id, s.server_id, s.article_id, s.term, s.weight,
               s.source, s.status, s.suggested_by
        FROM law_synonyms s
        JOIN law_articles a ON a.id = s.article_id
        WHERE s.server_id = $1 AND a.server_id = $1 AND a.kind = 'article'
        ORDER BY s.id
        """,
        server_id,
    )
    normalized = [
        {
            "id": int(row["id"]),
            "server_id": str(row["server_id"]),
            "article_id": int(row["article_id"]),
            "term": str(row["term"]),
            "weight": float(row["weight"]),
            "source": str(row["source"]),
            "status": str(row["status"]),
            "suggested_by": row["suggested_by"],
        }
        for row in rows
    ]
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "algorithm": "sha256",
        "rows": len(normalized),
        "value": hashlib.sha256(payload).hexdigest(),
    }


async def _prepare_batch(
    conn,
    server_id: str,
    registry: ExactSignatureRegistry,
    items: Sequence[Mapping[str, Any]],
    *,
    ignored_exact_rows: Optional[set[Tuple[int, str]]] = None,
    excluded_current_auto_owner_article_ids: Optional[set[int]] = None,
    conflict_details: Optional[list[SynonymConflict]] = None,
) -> Tuple[Dict[int, set[str]], Dict[int, list[str]], Dict[int, Dict[EffectiveKey, set[str]]]]:
    """Expand, sign and collision-check a validated batch.

    ``registry`` is deliberately used only for coverage accounting.  A caller
    may keep it between short transactions, so it cannot be authoritative for
    collision safety.  Candidate ownership is always re-read from the current
    database while the per-server synonym advisory lock is held.
    """

    expanded_by_article: Dict[int, Dict[str, set[str]]] = {}
    all_physical_terms: list[str] = []
    for item in items:
        article_id = int(item["id"])
        groups = expand_synonym_terms(item.get("synonyms") or ())
        expanded_by_article[article_id] = groups
        for terms in groups.values():
            all_physical_terms.extend(terms)

    # Every row that can physically reach law_synonyms is checked.  A surface
    # slang form can have a different exact-leg signature from its canonical
    # companion, so checking only canonical terms would leave a collision hole.
    signatures = await _signature_map(conn, all_physical_terms)
    keyed: Dict[int, Dict[EffectiveKey, set[str]]] = {}
    claims: Dict[EffectiveKey, set[int]] = defaultdict(set)
    for article_id, groups in expanded_by_article.items():
        keyed[article_id] = {}
        for terms in groups.values():
            for term in terms:
                key = _effective_key(term, signatures.get(term))
                keyed[article_id].setdefault(key, set()).add(term)
                claims[key].add(article_id)
    staged_owner_representatives = {
        key: sorted(
            (
                article_id,
                min(keyed[article_id][key]),
            )
            for article_id in article_ids
        )
        for key, article_ids in claims.items()
    }

    candidates = [
        (article_id, term)
        for article_id, groups in keyed.items()
        for terms in groups.values()
        for term in terms
    ]
    current_owner_details = await _current_candidate_owner_details(
        conn,
        server_id,
        candidates,
        excluded_auto_owner_article_ids=(
            excluded_current_auto_owner_article_ids
        ),
        representative_only=True,
    )
    blocked_exact_rows = await _current_nonapproved_exact_rows(
        conn,
        server_id,
        candidates,
    )
    blocked_exact_rows.difference_update(
        ignored_exact_rows or set()
    )
    accepted: Dict[int, set[str]] = defaultdict(set)
    rejected: Dict[int, list[str]] = defaultdict(list)
    unsafe_terms: Dict[int, set[str]] = defaultdict(set)
    discovered_conflicts: set[SynonymConflict] = set()
    for article_id, groups in keyed.items():
        for key, terms in groups.items():
            staged_owners = staged_owner_representatives[key]
            staged_owner = None
            if len(staged_owners) > 1:
                staged_owner = (
                    staged_owners[0]
                    if staged_owners[0][0] != article_id
                    else staged_owners[1]
                )
            for term in terms:
                other_existing = current_owner_details.get(
                    (article_id, term),
                    set(),
                )
                if (
                    other_existing
                    or staged_owner is not None
                    or (article_id, term) in blocked_exact_rows
                ):
                    unsafe_terms[article_id].add(term)
                if other_existing:
                    owner_article_id, owner_term = min(other_existing)
                    discovered_conflicts.add(SynonymConflict(
                        candidate_article_id=article_id,
                        candidate_term=term,
                        reason="current_approved_owner",
                        owner_article_id=owner_article_id,
                        owner_term=owner_term,
                    ))
                if staged_owner is not None:
                    owner_article_id, owner_term = staged_owner
                    discovered_conflicts.add(SynonymConflict(
                        candidate_article_id=article_id,
                        candidate_term=term,
                        reason="staged_candidate_owner",
                        owner_article_id=owner_article_id,
                        owner_term=owner_term,
                    ))
                if (article_id, term) in blocked_exact_rows:
                    discovered_conflicts.add(SynonymConflict(
                        candidate_article_id=article_id,
                        candidate_term=term,
                        reason="current_nonapproved_exact_row",
                        owner_article_id=article_id,
                        owner_term=term,
                    ))
    # Surface+canonical companions are one semantic proposal.  If either
    # physical row is unsafe, keep neither: otherwise a dead surface-only row
    # could be stored after its reachable canonical companion was rejected.
    for article_id, canonical_groups in expanded_by_article.items():
        for terms in canonical_groups.values():
            if set(terms) & unsafe_terms.get(article_id, set()):
                rejected[article_id].extend(sorted(terms))
            else:
                accepted[article_id].update(terms)
    if conflict_details is not None:
        conflict_details.extend(sorted(discovered_conflicts))
    return dict(accepted), dict(rejected), keyed


async def _current_nonapproved_exact_rows(
    conn,
    server_id: str,
    candidates: Sequence[Tuple[int, str]],
) -> set[Tuple[int, str]]:
    """Return exact UNIQUE blockers that cannot participate in live search.

    ``law_synonyms`` is unique on ``(article_id, term)`` regardless of status
    or the denormalized server_id.  Consequently an old pending/rejected (or
    mismatched-server) row makes ``ON CONFLICT DO NOTHING`` skip an otherwise
    safe approved candidate.  It must be rejected during preview too,
    otherwise coverage is overstated.
    """

    unique = list(
        dict.fromkeys(
            (int(article_id), term)
            for article_id, term in candidates
        )
    )
    if not unique:
        return set()
    rows = await conn.fetch(
        """
        WITH candidate AS (
          SELECT *
          FROM unnest($2::int[], $3::text[])
               AS value(candidate_article_id, candidate_term)
        )
        SELECT candidate.candidate_article_id,
               candidate.candidate_term
        FROM candidate
        JOIN law_synonyms stored
          ON stored.article_id = candidate.candidate_article_id
         AND stored.term = candidate.candidate_term
         AND (
               stored.server_id <> $1
               OR stored.status <> 'approved'
             )
        JOIN law_articles article
          ON article.id = stored.article_id
         AND article.server_id = $1
         AND article.kind = 'article'
        """,
        server_id,
        [article_id for article_id, _term in unique],
        [term for _article_id, term in unique],
    )
    return {
        (
            int(row["candidate_article_id"]),
            str(row["candidate_term"]),
        )
        for row in rows
    }


async def _current_candidate_owner_details(
    conn,
    server_id: str,
    candidates: Sequence[Tuple[int, str]],
    *,
    excluded_auto_owner_article_ids: Optional[set[int]] = None,
    representative_only: bool = False,
) -> Dict[Tuple[int, str], set[Tuple[int, str]]]:
    """Return other CURRENT owners for candidate exact-leg signatures.

    The query intentionally mirrors ``_leg_synonym_exact`` instead of trusting
    a process-local snapshot.  The caller holds ``law-synonyms:<server>`` for
    the surrounding transaction, so every shared writer observes this check
    before inserting.  Raw slang/question rows may be excluded from the
    coverage metric, but if their PostgreSQL signature can vote in the exact
    leg they still block creation of a new cross-article collision.
    """

    unique = list(dict.fromkeys((int(article_id), term) for article_id, term in candidates))
    if not unique:
        return {}
    excluded_auto_ids = sorted({
        int(article_id)
        for article_id in (excluded_auto_owner_article_ids or set())
    })
    excluded_auto_clause = (
        """
              AND NOT (
                    stored.source = 'auto'
                AND stored.article_id = ANY($4::int[])
              )
        """
        if excluded_auto_ids
        else ""
    )
    select_prefix = (
        """
        SELECT DISTINCT ON (
                 match.candidate_article_id,
                 match.candidate_term
               )
        """
        if representative_only
        else "SELECT"
    )
    representative_order = (
        """
        ORDER BY match.candidate_article_id, match.candidate_term,
                 match.owner_article_id, match.owner_term
        """
        if representative_only
        else ""
    )
    rows = await conn.fetch(
        f"""
        WITH candidate_input AS (
          SELECT *
          FROM unnest($2::int[], $3::text[])
               AS value(candidate_article_id, candidate_term)
        ),
        candidate AS (
          SELECT candidate_article_id, candidate_term,
                 char_length(candidate_term) >= 4
                   AND cardinality(
                         tsvector_to_array(to_tsvector('russian', candidate_term))
                       ) > 0 AS uses_exact_leg,
                 tsvector_to_array(
                   to_tsvector('russian', candidate_term)
                 ) AS lexemes,
                 to_tsvector('simple', candidate_term)
                   @@ plainto_tsquery('simple', 'без') AS has_without,
                 to_tsvector('simple', candidate_term)
                   @@ plainto_tsquery('simple', 'не') AS has_not,
                 to_tsvector('simple', candidate_term)
                   @@ phraseto_tsquery('simple', 'без цели')
                      AS has_without_purpose,
                 to_tsvector('simple', candidate_term)
                   @@ phraseto_tsquery('simple', 'с целью')
                      AS has_with_purpose
          FROM candidate_input
        )
        {select_prefix}
               match.candidate_article_id, match.candidate_term,
               match.owner_article_id, match.owner_term
        FROM (
          SELECT candidate.candidate_article_id, candidate.candidate_term,
                 synonym.article_id AS owner_article_id,
                 synonym.term AS owner_term
          FROM candidate
          CROSS JOIN LATERAL (
            SELECT stored.article_id, stored.term
            FROM law_synonyms stored
            WHERE NOT candidate.uses_exact_leg
              AND stored.server_id = $1
              AND stored.status = 'approved'
              AND stored.article_id <> candidate.candidate_article_id
              AND stored.term = candidate.candidate_term
              {excluded_auto_clause}
          ) AS synonym

          UNION ALL

          SELECT candidate.candidate_article_id, candidate.candidate_term,
                 synonym.article_id AS owner_article_id,
                 synonym.term AS owner_term
          FROM candidate
          CROSS JOIN LATERAL (
            SELECT stored.article_id, stored.term
            FROM law_synonyms stored
            WHERE candidate.uses_exact_leg
              AND stored.server_id = $1
              AND stored.status = 'approved'
              AND stored.article_id <> candidate.candidate_article_id
              {excluded_auto_clause}
              AND char_length(stored.term) >= 4
              AND cardinality(
                    tsvector_to_array(to_tsvector('russian', stored.term))
                  ) > 0
              AND tsvector_to_array(to_tsvector('russian', stored.term))
                  = candidate.lexemes
              AND (
                    to_tsvector('simple', stored.term)
                      @@ plainto_tsquery('simple', 'без')
                  ) = candidate.has_without
              AND (
                    to_tsvector('simple', stored.term)
                      @@ plainto_tsquery('simple', 'не')
                  ) = candidate.has_not
              AND (
                    to_tsvector('simple', stored.term)
                      @@ phraseto_tsquery('simple', 'без цели')
                  ) = candidate.has_without_purpose
              AND (
                    to_tsvector('simple', stored.term)
                      @@ phraseto_tsquery('simple', 'с целью')
                  ) = candidate.has_with_purpose
          ) AS synonym
        ) AS match
        JOIN law_articles article
          ON article.id = match.owner_article_id
         AND article.server_id = $1
         AND article.kind = 'article'
        {representative_order}
        """,
        server_id,
        [article_id for article_id, _term in unique],
        [term for _article_id, term in unique],
        *([excluded_auto_ids] if excluded_auto_ids else []),
    )
    owners: Dict[
        Tuple[int, str],
        set[Tuple[int, str]],
    ] = defaultdict(set)
    for row in rows:
        owners[
            (int(row["candidate_article_id"]), str(row["candidate_term"]))
        ].add((
            int(row["owner_article_id"]),
            str(row["owner_term"]),
        ))
    return dict(owners)


async def _current_candidate_owners(
    conn,
    server_id: str,
    candidates: Sequence[Tuple[int, str]],
) -> Dict[Tuple[int, str], set[int]]:
    """Compatibility view of CURRENT owner ids without detailed terms."""

    detailed = await _current_candidate_owner_details(
        conn,
        server_id,
        candidates,
    )
    return {
        candidate: {
            owner_article_id
            for owner_article_id, _owner_term in owners
        }
        for candidate, owners in detailed.items()
    }


async def _insert_rows(
    conn,
    server_id: str,
    article_hashes: Mapping[int, str],
    terms_by_article: Mapping[int, Iterable[str]],
    *,
    run_id: Optional[str] = None,
) -> list[Any]:
    article_ids: list[int] = []
    hashes: list[str] = []
    terms: list[str] = []
    for article_id, article_terms in terms_by_article.items():
        for term in sorted(set(article_terms)):
            article_ids.append(int(article_id))
            hashes.append(str(article_hashes[int(article_id)]))
            terms.append(term)
    if not terms:
        return []
    inserted = list(await conn.fetch(
        """
        WITH incoming AS (
          SELECT *
          FROM unnest($1::int[], $2::text[], $3::text[])
               AS value(article_id, article_hash, term)
        )
        INSERT INTO law_synonyms (server_id, article_id, term, source, status)
        SELECT $4, incoming.article_id, incoming.term, 'auto', 'approved'
        FROM incoming
        JOIN law_articles a ON a.id = incoming.article_id
        WHERE a.server_id = $4 AND a.kind = 'article'
          AND a.article_hash = incoming.article_hash
        ON CONFLICT (article_id, term) DO NOTHING
        RETURNING id, article_id, term, source, status, weight, suggested_by
        """,
        article_ids,
        hashes,
        terms,
        server_id,
    ))
    if run_id and inserted:
        await conn.execute(
            """
            INSERT INTO law_synonym_backfill_inserts
                (run_id, synonym_id, server_id, article_id, term, article_hash,
                 source, status, weight, suggested_by)
            SELECT $1::uuid, value.synonym_id, $2, value.article_id,
                   value.term, value.article_hash, value.source, value.status,
                   value.weight, value.suggested_by
            FROM unnest(
                   $3::int[], $4::int[], $5::text[], $6::text[],
                   $7::text[], $8::text[], $9::real[], $10::bigint[]
                 )
                 AS value(
                   synonym_id, article_id, term, article_hash,
                   source, status, weight, suggested_by
                 )
            ON CONFLICT (run_id, synonym_id) DO NOTHING
            """,
            run_id,
            server_id,
            [int(row["id"]) for row in inserted],
            [int(row["article_id"]) for row in inserted],
            [str(row["term"]) for row in inserted],
            [str(article_hashes[int(row["article_id"])]) for row in inserted],
            [str(row["source"]) for row in inserted],
            [str(row["status"]) for row in inserted],
            [float(row["weight"]) for row in inserted],
            [row["suggested_by"] for row in inserted],
        )
    return inserted


def _validated_run_binding(
    run_id: Optional[str],
    plan_hash: Optional[str],
    manifest_hash: Optional[str],
) -> Optional[Tuple[str, str, str]]:
    values = (run_id, plan_hash, manifest_hash)
    if not any(value is not None for value in values):
        return None
    if not all(isinstance(value, str) and value for value in values):
        raise SynonymWriteError(
            "run_id, plan_hash and manifest_hash must be provided together"
        )
    assert run_id is not None
    assert plan_hash is not None
    assert manifest_hash is not None
    valid_hex = set("0123456789abcdef")
    if (
        len(plan_hash) != 64
        or len(manifest_hash) != 64
        or not set(plan_hash) <= valid_hex
        or not set(manifest_hash) <= valid_hex
    ):
        raise SynonymWriteError(
            "plan_hash and manifest_hash must be lowercase SHA-256 values"
        )
    return run_id, plan_hash, manifest_hash


async def _bind_backfill_run(
    conn,
    server_id: str,
    binding: Tuple[str, str, str],
) -> None:
    """Bind a run UUID to one immutable reviewed plan before any insert."""

    run_id, plan_hash, manifest_hash = binding
    await conn.execute(
        """
        INSERT INTO law_synonym_backfill_runs
            (run_id, server_id, plan_hash, manifest_hash)
        VALUES ($1::uuid, $2, $3, $4)
        ON CONFLICT (run_id) DO NOTHING
        """,
        run_id,
        server_id,
        plan_hash,
        manifest_hash,
    )
    row = await conn.fetchrow(
        """
        SELECT server_id, plan_hash, manifest_hash
        FROM law_synonym_backfill_runs
        WHERE run_id = $1::uuid
        FOR UPDATE
        """,
        run_id,
    )
    expected = {
        "server_id": server_id,
        "plan_hash": plan_hash,
        "manifest_hash": manifest_hash,
    }
    actual = (
        {
            "server_id": str(row["server_id"]),
            "plan_hash": str(row["plan_hash"]),
            "manifest_hash": str(row["manifest_hash"]),
        }
        if row is not None
        else None
    )
    if actual != expected:
        raise SynonymWriteError(
            "run_id is already bound to another server or immutable plan"
        )


async def add_auto_synonyms_bulk_in_transaction(
    conn,
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    registry: Optional[ExactSignatureRegistry] = None,
    run_id: Optional[str] = None,
    plan_hash: Optional[str] = None,
    manifest_hash: Optional[str] = None,
    lock_already_held: bool = False,
) -> SynonymWriteResult:
    """CAS-protected additive write using the caller's open transaction.

    Every item must contain ``id``, the generation-time ``article_hash`` and a
    ``synonyms`` list.  Any ownership/kind/hash mismatch aborts the whole batch
    before a synonym is written.
    """

    run_binding = _validated_run_binding(run_id, plan_hash, manifest_hash)
    if not items:
        if run_binding is not None:
            if not lock_already_held:
                await acquire_synonym_write_lock(conn, server_id)
            await _bind_backfill_run(conn, server_id, run_binding)
        return SynonymWriteResult()
    ids = [item.get("id") for item in items]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
        raise SynonymWriteError("every item must contain an integer article id")
    if len(set(ids)) != len(ids):
        raise SynonymWriteError("duplicate article ids in one synonym batch")
    if any(not isinstance(item.get("article_hash"), str) or not item["article_hash"] for item in items):
        raise SynonymWriteError("every item must contain its generation-time article_hash")

    if not lock_already_held:
        await acquire_synonym_write_lock(conn, server_id)
    rows = await conn.fetch(
        """
        SELECT id, article_hash
        FROM law_articles
        WHERE id = ANY($1::int[]) AND server_id = $2 AND kind = 'article'
        FOR UPDATE
        """,
        ids,
        server_id,
    )
    current = {int(row["id"]): str(row["article_hash"]) for row in rows}
    invalid = [
        int(item["id"])
        for item in items
        if current.get(int(item["id"])) != item["article_hash"]
    ]
    if invalid:
        raise SynonymWriteError(
            f"ownership/kind/article_hash CAS failed for article ids: {invalid}"
        )

    # An additive Phase B write changes the semantic output covered by
    # ``enrichment_output_hash``.  Capture the trusted pre-write metadata and
    # prove every non-NULL fingerprint before inserting anything.  Historical
    # NULL fingerprints deliberately remain NULL: an additive synonym pass
    # must not silently attest legacy title/penalty/output state.
    output_metadata_rows = await conn.fetch(
        """
        SELECT id, title, title_provenance, penalty_summary, penalty_json,
               enrich_hash, enrichment_context_hash,
               enrichment_output_hash
        FROM law_articles
        WHERE id = ANY($1::int[]) AND server_id = $2 AND kind = 'article'
        FOR UPDATE
        """,
        ids,
        server_id,
    )
    output_metadata = {
        int(row["id"]): dict(row)
        for row in output_metadata_rows
    }
    if set(output_metadata) != set(ids):
        raise SynonymWriteError(
            "additive output metadata ownership changed before write"
        )
    pre_auto_rows = await conn.fetch(
        """
        SELECT id, server_id, article_id, term, weight, source, status,
               suggested_by
        FROM law_synonyms
        WHERE article_id = ANY($1::int[]) AND source = 'auto'
        ORDER BY article_id, id
        """,
        ids,
    )
    pre_auto_by_article: Dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in pre_auto_rows:
        pre_auto_by_article[int(row["article_id"])].append(dict(row))
    try:
        foreign_auto_ids = foreign_auto_ownership_article_ids(
            server_id=server_id,
            auto_rows=(dict(row) for row in pre_auto_rows),
        )
    except ValueError as exc:
        raise SynonymWriteError(
            "additive auto synonym ownership cannot be validated safely"
        ) from exc
    if foreign_auto_ids:
        raise SynonymWriteError(
            "foreign-server auto synonym ownership for article ids: "
            f"{list(foreign_auto_ids)}"
        )
    trusted_output_ids: list[int] = []
    invalid_output_ids: list[int] = []
    for article_id in ids:
        metadata = output_metadata[article_id]
        stored_output_hash = metadata.get("enrichment_output_hash")
        if stored_output_hash is None:
            continue
        try:
            current_output_hash = build_enrichment_output_hash(
                server_id=server_id,
                article_id=article_id,
                title=metadata.get("title"),
                title_provenance=metadata.get("title_provenance"),
                penalty_summary=metadata.get("penalty_summary"),
                penalty_json=metadata.get("penalty_json"),
                auto_rows=pre_auto_by_article.get(article_id, ()),
            )
        except (TypeError, ValueError):
            invalid_output_ids.append(article_id)
            continue
        if current_output_hash != str(stored_output_hash):
            invalid_output_ids.append(article_id)
            continue
        trusted_output_ids.append(article_id)
    if invalid_output_ids:
        raise SynonymWriteError(
            "additive output fingerprint is stale or invalid for article ids: "
            f"{sorted(invalid_output_ids)}"
        )
    if run_binding is not None:
        await _bind_backfill_run(conn, server_id, run_binding)

    if registry is None:
        registry = await load_exact_signature_registry(conn, server_id)
    result = SynonymWriteResult(
        effective_before={
            int(item["id"]): registry.effective_count(int(item["id"]))
            for item in items
        }
    )
    accepted, rejected, keyed = await _prepare_batch(
        conn, server_id, registry, items
    )
    result.accepted_terms = {
        int(article_id): sorted(terms)
        for article_id, terms in accepted.items()
    }
    result.rejected_terms = rejected
    result.rejected_collisions = sum(len(values) for values in rejected.values())
    attempted = sum(len(values) for values in accepted.values())
    inserted_rows = await _insert_rows(
        conn,
        server_id,
        current,
        accepted,
        run_id=run_id,
    )
    result.inserted = len(inserted_rows)
    result.already_present = max(0, attempted - result.inserted)

    inserted_lookup = {
        (
            int(row["id"]),
            int(row["article_id"]),
            str(row["term"]),
            str(row["source"]),
            str(row["status"]),
            float(row["weight"]),
            row["suggested_by"],
        )
        for row in inserted_rows
    }

    # Recompute the fingerprint from the rows actually persisted by the
    # INSERT, then CAS it against the exact metadata captured above.  Do this
    # before publishing changes into the caller's in-memory registry so an
    # unexpected CAS failure cannot leave that registry ahead of the rolled
    # back database transaction.
    if trusted_output_ids:
        post_auto_rows = await conn.fetch(
            """
            SELECT id, server_id, article_id, term, weight, source, status,
                   suggested_by
            FROM law_synonyms
            WHERE article_id = ANY($1::int[]) AND source = 'auto'
            ORDER BY article_id, id
            """,
            trusted_output_ids,
        )
        post_auto_by_article: Dict[
            int, list[Mapping[str, Any]]
        ] = defaultdict(list)
        for row in post_auto_rows:
            post_auto_by_article[int(row["article_id"])].append(dict(row))
        for article_id in sorted(trusted_output_ids):
            metadata = output_metadata[article_id]
            try:
                desired_output_hash = build_enrichment_output_hash(
                    server_id=server_id,
                    article_id=article_id,
                    title=metadata.get("title"),
                    title_provenance=metadata.get("title_provenance"),
                    penalty_summary=metadata.get("penalty_summary"),
                    penalty_json=metadata.get("penalty_json"),
                    auto_rows=post_auto_by_article.get(article_id, ()),
                )
            except (TypeError, ValueError) as exc:
                raise SynonymWriteError(
                    "additive output cannot be fingerprinted safely for "
                    f"article {article_id}"
                ) from exc
            updated_id = await conn.fetchval(
                """
                UPDATE law_articles
                SET enrichment_output_hash = $2
                WHERE id = $1
                  AND server_id = $3
                  AND article_hash = $4
                  AND title IS NOT DISTINCT FROM $5
                  AND title_provenance IS NOT DISTINCT FROM $6
                  AND penalty_summary IS NOT DISTINCT FROM $7
                  AND penalty_json IS NOT DISTINCT FROM $8::jsonb
                  AND enrich_hash IS NOT DISTINCT FROM $9
                  AND enrichment_context_hash IS NOT DISTINCT FROM $10
                  AND enrichment_output_hash IS NOT DISTINCT FROM $11
                RETURNING id
                """,
                article_id,
                desired_output_hash,
                server_id,
                current[article_id],
                metadata.get("title"),
                metadata.get("title_provenance"),
                metadata.get("penalty_summary"),
                (
                    json.dumps(
                        normalize_penalty_json(
                            metadata.get("penalty_json")
                        ),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    if metadata.get("penalty_json") is not None
                    else None
                ),
                metadata.get("enrich_hash"),
                metadata.get("enrichment_context_hash"),
                metadata.get("enrichment_output_hash"),
            )
            if updated_id != article_id:
                raise SynonymWriteError(
                    "additive output metadata CAS failed for article "
                    f"{article_id}"
                )

    # Existing physical rows were already represented in the loaded registry;
    # only newly returned rows need to be added.  The registry itself decides
    # which stored companion is reachable after one normalization step.
    physical_keys: Dict[Tuple[int, str], EffectiveKey] = {}
    for article_id, groups in keyed.items():
        for key, grouped_terms in groups.items():
            for term in grouped_terms:
                physical_keys[(article_id, term)] = key
    for (
        _row_id,
        article_id,
        term,
        source,
        _status,
        _weight,
        _suggested_by,
    ) in inserted_lookup:
        key = physical_keys.get((article_id, term))
        if key is not None:
            registry.add(article_id, source, term, key)
    result.inserted_rows = [
        {
            "id": row_id,
            "run_id": run_id,
            "server_id": server_id,
            "article_id": article_id,
            "article_hash": current[article_id],
            "term": term,
            "source": source,
            "status": status,
            "weight": weight,
            "suggested_by": suggested_by,
        }
        for (
            row_id,
            article_id,
            term,
            source,
            status,
            weight,
            suggested_by,
        ) in sorted(inserted_lookup)
    ]
    result.effective_after = {
        int(item["id"]): registry.effective_count(int(item["id"]))
        for item in items
    }
    return result


async def preview_auto_synonyms_bulk_in_transaction(
    conn,
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    registry: Optional[ExactSignatureRegistry] = None,
    lock_already_held: bool = False,
) -> SynonymWriteResult:
    """Resolve a complete immutable plan without mutating any database row."""

    if not items:
        return SynonymWriteResult()
    ids = [item.get("id") for item in items]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
        raise SynonymWriteError("every item must contain an integer article id")
    if len(set(ids)) != len(ids):
        raise SynonymWriteError("duplicate article ids in one synonym plan")
    if any(
        not isinstance(item.get("article_hash"), str)
        or not item["article_hash"]
        for item in items
    ):
        raise SynonymWriteError(
            "every item must contain its generation-time article_hash"
        )
    if not lock_already_held:
        await acquire_synonym_write_lock(conn, server_id)
    rows = await conn.fetch(
        """
        SELECT id, article_hash
        FROM law_articles
        WHERE id = ANY($1::int[]) AND server_id = $2 AND kind = 'article'
        FOR SHARE
        """,
        ids,
        server_id,
    )
    current = {int(row["id"]): str(row["article_hash"]) for row in rows}
    invalid = [
        int(item["id"])
        for item in items
        if current.get(int(item["id"])) != item["article_hash"]
    ]
    if invalid:
        raise SynonymWriteError(
            f"ownership/kind/article_hash CAS failed for article ids: {invalid}"
        )
    if registry is None:
        registry = await load_exact_signature_registry(conn, server_id)
    accepted, rejected, keyed = await _prepare_batch(
        conn, server_id, registry, items
    )
    preview = registry.clone()
    for article_id, groups in keyed.items():
        for key, terms in groups.items():
            for term in terms:
                if term in accepted.get(article_id, set()):
                    preview.add(article_id, "auto", term, key)
    return SynonymWriteResult(
        rejected_collisions=sum(len(values) for values in rejected.values()),
        rejected_terms=rejected,
        accepted_terms={
            int(article_id): sorted(terms)
            for article_id, terms in accepted.items()
        },
        effective_before={
            int(item["id"]): registry.effective_count(int(item["id"]))
            for item in items
        },
        effective_after={
            int(item["id"]): preview.effective_count(int(item["id"]))
            for item in items
        },
    )


def _validated_auto_replacement_generation_snapshot(
    server_id: str,
    generation_snapshot: Mapping[str, Any],
) -> Dict[str, Any]:
    if not isinstance(generation_snapshot, Mapping):
        raise SynonymWriteError(
            "generation_snapshot must be captured before remote generation"
        )
    required = {
        "algorithm",
        "server_id",
        "selection_mode",
        "source_catalog_binding",
        "catalog_binding_schema",
        "catalog_binding_hash",
        "catalog_rows",
        "article_rows",
        "auto_rows",
        "article_hash_stale_ids",
        "context_unknown_stale_ids",
        "context_stale_ids",
        "unsafe_provenance_ids",
        "output_unknown_stale_ids",
        "output_mismatch_stale_ids",
        "output_stale_ids",
        "stale_target_ids",
        "required_target_ids",
        "value",
    }
    if set(generation_snapshot) != required:
        raise SynonymWriteError(
            "generation_snapshot fields differ from the service contract"
        )
    try:
        validate_law_source_catalog_binding(
            generation_snapshot["source_catalog_binding"],
            expected_server_id=server_id,
        )
    except ValueError as exc:
        raise SynonymWriteError(
            "generation_snapshot source/catalog binding is invalid"
        ) from exc
    article_hash_stale_ids = generation_snapshot[
        "article_hash_stale_ids"
    ]
    context_unknown_stale_ids = generation_snapshot[
        "context_unknown_stale_ids"
    ]
    context_stale_ids = generation_snapshot["context_stale_ids"]
    unsafe_provenance_ids = generation_snapshot["unsafe_provenance_ids"]
    output_unknown_stale_ids = generation_snapshot[
        "output_unknown_stale_ids"
    ]
    output_mismatch_stale_ids = generation_snapshot[
        "output_mismatch_stale_ids"
    ]
    output_stale_ids = generation_snapshot["output_stale_ids"]
    stale_ids = generation_snapshot["stale_target_ids"]
    required_ids = generation_snapshot["required_target_ids"]
    if (
        generation_snapshot["algorithm"] != "sha256"
        or generation_snapshot["server_id"] != server_id
        or not isinstance(generation_snapshot["selection_mode"], str)
        or generation_snapshot["selection_mode"]
        not in {"stale", "all", "explicit"}
        or generation_snapshot["catalog_binding_schema"]
        != AUTO_REPLACEMENT_CATALOG_BINDING_SCHEMA
        or not isinstance(
            generation_snapshot["catalog_binding_hash"],
            str,
        )
        or re.fullmatch(
            r"[0-9a-f]{64}",
            generation_snapshot["catalog_binding_hash"],
        ) is None
        or isinstance(generation_snapshot["catalog_rows"], bool)
        or not isinstance(generation_snapshot["catalog_rows"], int)
        or generation_snapshot["catalog_rows"] < 0
        or isinstance(generation_snapshot["article_rows"], bool)
        or not isinstance(generation_snapshot["article_rows"], int)
        or generation_snapshot["article_rows"] < 0
        or generation_snapshot["article_rows"]
        > generation_snapshot["catalog_rows"]
        or isinstance(generation_snapshot["auto_rows"], bool)
        or not isinstance(generation_snapshot["auto_rows"], int)
        or generation_snapshot["auto_rows"] < 0
        or not isinstance(article_hash_stale_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in article_hash_stale_ids
        )
        or article_hash_stale_ids
        != sorted(set(article_hash_stale_ids))
        or not isinstance(context_unknown_stale_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in context_unknown_stale_ids
        )
        or context_unknown_stale_ids
        != sorted(set(context_unknown_stale_ids))
        or not isinstance(context_stale_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in context_stale_ids
        )
        or context_stale_ids != sorted(set(context_stale_ids))
        or not set(context_unknown_stale_ids) <= set(context_stale_ids)
        or not isinstance(unsafe_provenance_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in unsafe_provenance_ids
        )
        or unsafe_provenance_ids != sorted(set(unsafe_provenance_ids))
        or not isinstance(output_unknown_stale_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in output_unknown_stale_ids
        )
        or output_unknown_stale_ids
        != sorted(set(output_unknown_stale_ids))
        or not isinstance(output_mismatch_stale_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in output_mismatch_stale_ids
        )
        or output_mismatch_stale_ids
        != sorted(set(output_mismatch_stale_ids))
        or not isinstance(output_stale_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in output_stale_ids
        )
        or output_stale_ids != sorted(set(output_stale_ids))
        or set(output_stale_ids)
        != set(output_unknown_stale_ids) | set(output_mismatch_stale_ids)
        or not isinstance(stale_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in stale_ids
        )
        or stale_ids != sorted(set(stale_ids))
        or set(stale_ids)
        != (
            set(article_hash_stale_ids)
            | set(context_stale_ids)
            | set(output_stale_ids)
            | set(unsafe_provenance_ids)
        )
        or not isinstance(required_ids, list)
        or any(
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            for article_id in required_ids
        )
        or required_ids != sorted(set(required_ids))
        or (
            generation_snapshot["selection_mode"] != "explicit"
            and not set(stale_ids) <= set(required_ids)
        )
        or len(required_ids) > generation_snapshot["article_rows"]
        or not isinstance(generation_snapshot["value"], str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            generation_snapshot["value"],
        ) is None
    ):
        raise SynonymWriteError("invalid generation_snapshot")
    return {
        key: deepcopy(generation_snapshot[key])
        for key in sorted(required)
    }


async def _assert_auto_replacement_generation_snapshot_locked(
    conn,
    server_id: str,
    target_ids: Sequence[int],
    expected_snapshot: Mapping[str, Any],
    *,
    lock_articles: bool,
) -> Dict[str, Any]:
    current_state = await _load_auto_replacement_generation_state(
        conn,
        server_id,
        selection_mode=str(expected_snapshot["selection_mode"]),
        lock_articles=lock_articles,
        explicit_target_ids=(
            list(expected_snapshot["required_target_ids"])
            if expected_snapshot["selection_mode"] == "explicit"
            else None
        ),
    )
    current_snapshot = current_state.snapshot
    if current_snapshot != expected_snapshot:
        raise SynonymWriteError(
            "server generation snapshot changed during remote generation"
        )
    if current_snapshot["required_target_ids"] != list(target_ids):
        raise SynonymWriteError(
            "full-server required target set differs from staged article ids"
        )
    return current_snapshot


def _validated_auto_replacement_target_ids(
    items: Sequence[Mapping[str, Any]],
) -> list[int]:
    if any(not isinstance(item, Mapping) for item in items):
        raise SynonymWriteError(
            "every synonym replacement item must be an object"
        )
    ids = [item.get("id") for item in items]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in ids):
        raise SynonymWriteError("every item must contain an integer article id")
    if len(set(ids)) != len(ids):
        raise SynonymWriteError(
            "duplicate article ids in one synonym replacement batch"
        )
    if any(
        not isinstance(item.get("article_hash"), str)
        or not item["article_hash"]
        for item in items
    ):
        raise SynonymWriteError(
            "every item must contain its generation-time article_hash"
        )
    if any(
        "expected_enrich_hash" not in item
        or (
            item["expected_enrich_hash"] is not None
            and not isinstance(item["expected_enrich_hash"], str)
        )
        for item in items
    ):
        raise SynonymWriteError(
            "every item must contain its generation-time expected_enrich_hash"
        )
    if any(
        not isinstance(
            item.get("expected_auto_synonym_fingerprint"),
            str,
        )
        or re.fullmatch(
            r"[0-9a-f]{64}",
            item["expected_auto_synonym_fingerprint"],
        ) is None
        for item in items
    ):
        raise SynonymWriteError(
            "every item must contain its generation-time "
            "expected_auto_synonym_fingerprint"
        )
    if any(not isinstance(item.get("synonyms"), list) for item in items):
        raise SynonymWriteError(
            "every item must contain a JSON synonym list"
        )
    return sorted(int(article_id) for article_id in ids)


def _is_replaceable_auto_row(
    row: Mapping[str, Any],
    server_id: str,
) -> bool:
    """Only approved/stale auto rows belong to a staged replacement."""

    return (
        str(row["server_id"]) == server_id
        and str(row["source"]) == "auto"
        and str(row["status"]) in {"approved", "stale"}
    )


async def _build_auto_replacement_preview_locked(
    conn,
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    target_ids: Sequence[int],
    *,
    lock_articles: bool = False,
) -> _AutoReplacementPreviewState:
    """Repeat every replacement CAS/collision check under the caller's lock."""

    article_lock = "FOR UPDATE" if lock_articles else ""
    rows = await conn.fetch(
        f"""
        SELECT id, article_hash, enrich_hash, full_text
        FROM law_articles
        WHERE id = ANY($1::int[]) AND server_id = $2 AND kind = 'article'
        {article_lock}
        """,
        list(target_ids),
        server_id,
    )
    current = {
        int(row["id"]): {
            "id": int(row["id"]),
            "article_hash": str(row["article_hash"]),
            "enrich_hash": (
                str(row["enrich_hash"])
                if row["enrich_hash"] is not None
                else None
            ),
            "full_text": str(row["full_text"] or ""),
        }
        for row in rows
    }
    invalid = [
        int(item["id"])
        for item in items
        if current.get(int(item["id"]), {}).get("article_hash")
        != item["article_hash"]
    ]
    if invalid:
        raise SynonymWriteError(
            "ownership/kind/article_hash CAS failed for article ids: "
            f"{invalid}"
        )

    existing_rows = [
        dict(row)
        for row in await conn.fetch(
            """
            SELECT id, server_id, article_id, term, weight, source, status,
                   suggested_by
            FROM law_synonyms
            WHERE article_id = ANY($1::int[])
            ORDER BY article_id, id
            """,
            list(target_ids),
        )
    ]
    auto_rows_by_article: Dict[int, list[Mapping[str, Any]]] = defaultdict(
        list
    )
    for row in existing_rows:
        if str(row["source"]) == "auto":
            auto_rows_by_article[int(row["article_id"])].append(row)
    changed_generation = [
        int(item["id"])
        for item in items
        if (
            current[int(item["id"])]["enrich_hash"]
            != item["expected_enrich_hash"]
            or auto_synonym_state_fingerprint(
                int(item["id"]),
                auto_rows_by_article.get(int(item["id"]), ()),
            )
            != item["expected_auto_synonym_fingerprint"]
        )
    ]
    if changed_generation:
        raise SynonymWriteError(
            "enrich_hash/auto-synonym generation-state CAS failed for "
            f"article ids: {changed_generation}"
        )

    target_id_set = set(target_ids)
    replaceable_rows = [
        row
        for row in existing_rows
        if _is_replaceable_auto_row(row, server_id)
    ]
    source_registry = await load_exact_signature_registry(conn, server_id)
    replacement_base = source_registry.clone()
    for article_id, source, term in list(replacement_base.records):
        if article_id in target_id_set and source == "auto":
            replacement_base.remove(article_id, source, term)

    conflicts: list[SynonymConflict] = []
    accepted, rejected, keyed = await _prepare_batch(
        conn,
        server_id,
        replacement_base,
        items,
        ignored_exact_rows={
            (int(row["article_id"]), str(row["term"]))
            for row in replaceable_rows
            if str(row["status"]) == "stale"
        },
        excluded_current_auto_owner_article_ids=target_id_set,
        conflict_details=conflicts,
    )
    accepted = {
        int(article_id): set(terms)
        for article_id, terms in accepted.items()
    }
    rejected = {
        int(article_id): sorted(set(terms))
        for article_id, terms in rejected.items()
    }

    preview_registry = replacement_base.clone()
    for article_id, groups in keyed.items():
        for key, grouped_terms in groups.items():
            for term in grouped_terms:
                if term in accepted.get(article_id, set()):
                    preview_registry.add(article_id, "auto", term, key)

    retained_approved: Dict[int, set[str]] = defaultdict(set)
    for row in existing_rows:
        article_id = int(row["article_id"])
        if (
            article_id in target_id_set
            and str(row["server_id"]) == server_id
            and str(row["status"]) == "approved"
            and not _is_replaceable_auto_row(row, server_id)
        ):
            retained_approved[article_id].add(str(row["term"]))

    result = SynonymWriteResult(
        rejected_collisions=sum(
            len(values)
            for values in rejected.values()
        ),
        rejected_terms={
            article_id: rejected.get(article_id, [])
            for article_id in target_ids
            if rejected.get(article_id)
        },
        accepted_terms={
            article_id: sorted(accepted.get(article_id, set()))
            for article_id in target_ids
        },
        effective_before={
            article_id: source_registry.effective_count(article_id)
            for article_id in target_ids
        },
        effective_after={
            article_id: preview_registry.effective_count(article_id)
            for article_id in target_ids
        },
        physical_after={
            article_id: len(
                retained_approved.get(article_id, set())
                | accepted.get(article_id, set())
            )
            for article_id in target_ids
        },
        conflicts=sorted(set(conflicts)),
    )
    return _AutoReplacementPreviewState(
        result=result,
        current_articles=current,
        existing_rows=existing_rows,
        accepted=accepted,
        keyed=keyed,
        source_registry=source_registry,
        preview_registry=preview_registry,
        replaceable_row_ids={
            int(row["id"])
            for row in replaceable_rows
        },
    )


async def preview_auto_synonym_replacements(
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    generation_snapshot: Mapping[str, Any],
) -> SynonymWriteResult:
    """Preview one server-wide required-target replacement without DB writes.

    ``generation_snapshot`` must be the ``snapshot`` member returned by
    :func:`capture_auto_replacement_generation_state` before the caller makes
    any network/model request.  The accompanying captured article/auto rows
    are the only safe source from which to build that request.

    Approved and stale auto rows of every staged target are replaceable.
    Pending/rejected or server-mismatched auto rows remain fail-closed exact
    blockers.  Owner/user rows are immutable.  The generation-state
    fingerprint still includes *all* auto rows, so a concurrent status change
    invalidates the plan.
    """

    expected_snapshot = _validated_auto_replacement_generation_snapshot(
        server_id,
        generation_snapshot,
    )
    target_ids = _validated_auto_replacement_target_ids(items)
    if expected_snapshot["required_target_ids"] != target_ids:
        raise SynonymWriteError(
            "generation snapshot required targets differ from staged article ids"
        )
    async with db_manager.transaction() as conn:
        await conn.execute("SET TRANSACTION READ ONLY")
        await acquire_synonym_write_lock(conn, server_id)
        await _assert_auto_replacement_generation_snapshot_locked(
            conn,
            server_id,
            target_ids,
            expected_snapshot,
            lock_articles=False,
        )
        if not items:
            return SynonymWriteResult()
        state = await _build_auto_replacement_preview_locked(
            conn,
            server_id,
            items,
            target_ids,
        )
        return state.result


def _stored_synonym_identity(
    row: Mapping[str, Any],
) -> Tuple[int, str, int, str, float, str, str, Optional[int]]:
    suggested_by = row["suggested_by"]
    return (
        int(row["id"]),
        str(row["server_id"]),
        int(row["article_id"]),
        str(row["term"]),
        float(row["weight"]),
        str(row["source"]),
        str(row["status"]),
        int(suggested_by) if suggested_by is not None else None,
    )


def _auto_replacement_quality_errors(
    state: _AutoReplacementPreviewState,
    items: Sequence[Mapping[str, Any]],
    physical: Mapping[int, int],
    effective: Mapping[int, int],
    *,
    required_physical: int,
    required_effective: int,
) -> list[str]:
    errors: list[str] = []
    for item in items:
        article_id = int(item["id"])
        tombstone = is_explicitly_empty_or_repealed_article(
            state.current_articles[article_id]["full_text"]
        )
        physical_count = int(physical.get(article_id, 0))
        effective_count = int(effective.get(article_id, 0))
        if tombstone:
            if item["synonyms"]:
                errors.append(
                    f"article {article_id} is a tombstone but synonyms are not empty"
                )
            immutable_rows = sum(
                1
                for row in state.existing_rows
                if (
                    int(row["article_id"]) == article_id
                    and int(row["id"]) not in state.replaceable_row_ids
                )
            )
            if immutable_rows:
                errors.append(
                    f"article {article_id} tombstone retains "
                    f"{immutable_rows} immutable synonym rows"
                )
            if physical_count != 0:
                errors.append(
                    f"article {article_id} tombstone physical={physical_count}"
                    "!=0"
                )
            if effective_count != 0:
                errors.append(
                    f"article {article_id} tombstone effective={effective_count}"
                    "!=0"
                )
            continue
        if physical_count < required_physical:
            errors.append(
                f"article {article_id} physical={physical_count}"
                f"<{required_physical}"
            )
        if effective_count < required_effective:
            errors.append(
                f"article {article_id} effective={effective_count}"
                f"<{required_effective}"
            )
    return errors


async def _replace_auto_synonyms_bulk_with_context(
    transaction_context,
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    generation_snapshot: Mapping[str, Any],
    lock_already_held: bool,
    minimum_physical: int = BULK_REPLACEMENT_PHYSICAL,
    minimum_effective: int = BULK_REPLACEMENT_EFFECTIVE,
) -> AutoSynonymBulkReplacementOutcome:
    """Shared implementation for caller-owned and self-owned transactions.

    The caller must capture ``generation_snapshot`` before any network/model
    call.  It repeats the complete snapshot, target, article, enrichment and
    exact-auto-row CAS under the per-server synonym lock, previews the whole
    staged set, and writes only if every normal article remains above the
    requested coverage floors.

    Only server-matching approved/stale auto rows are replaceable.  Owner/user,
    pending/rejected auto and corrupt server-mismatched rows are immutable.
    No law-article metadata, enrich_hash or embeddings are changed here.
    """

    expected_snapshot = _validated_auto_replacement_generation_snapshot(
        server_id,
        generation_snapshot,
    )
    target_ids = _validated_auto_replacement_target_ids(items)
    if expected_snapshot["required_target_ids"] != target_ids:
        raise SynonymWriteError(
            "generation snapshot required targets differ from staged article ids"
        )
    # This is the autonomous, server-wide regeneration path.  Its accepted
    # quality floor is deliberately stricter than the legacy single-article
    # replacement floor and must not be weakenable by a caller argument.
    required_physical = max(
        BULK_REPLACEMENT_PHYSICAL,
        int(minimum_physical),
    )
    required_effective = max(
        BULK_REPLACEMENT_EFFECTIVE,
        int(minimum_effective),
    )

    committed_registry: Optional[ExactSignatureRegistry] = None
    committed_result: Optional[SynonymWriteResult] = None
    async with transaction_context as conn:
        if not lock_already_held:
            await acquire_synonym_write_lock(conn, server_id)
        await _assert_auto_replacement_generation_snapshot_locked(
            conn,
            server_id,
            target_ids,
            expected_snapshot,
            lock_articles=True,
        )
        if not items:
            return AutoSynonymBulkReplacementOutcome(
                result=SynonymWriteResult(),
                final_registry=await load_exact_signature_registry(
                    conn,
                    server_id,
                ),
            )
        state = await _build_auto_replacement_preview_locked(
            conn,
            server_id,
            items,
            target_ids,
            lock_articles=True,
        )
        if state.result.rejected_collisions:
            raise SynonymWriteError(
                "server-wide replacement preview rejected exact-leg "
                f"collisions: {state.result.conflicts}"
            )
        quality_errors = _auto_replacement_quality_errors(
            state,
            items,
            state.result.physical_after,
            state.result.effective_after,
            required_physical=required_physical,
            required_effective=required_effective,
        )
        if quality_errors:
            raise SynonymWriteError(
                "server-wide replacement quality gate failed before write: "
                + ", ".join(quality_errors)
            )

        immutable_before = sorted(
            _stored_synonym_identity(row)
            for row in state.existing_rows
            if int(row["id"]) not in state.replaceable_row_ids
        )
        immutable_exact = {
            (int(row["article_id"]), str(row["term"]))
            for row in state.existing_rows
            if int(row["id"]) not in state.replaceable_row_ids
        }
        deleted_rows = await conn.fetch(
            """
            DELETE FROM law_synonyms
            WHERE article_id = ANY($1::int[])
              AND server_id = $2
              AND source = 'auto'
              AND status IN ('approved', 'stale')
            RETURNING id, server_id, article_id, term, weight, source, status,
                      suggested_by
            """,
            target_ids,
            server_id,
        )
        deleted_ids = {
            int(row["id"])
            for row in deleted_rows
        }
        if deleted_ids != state.replaceable_row_ids:
            raise SynonymWriteError(
                "replaceable auto-row delete CAS changed inside transaction"
            )

        accepted_for_insert = {
            article_id: set(state.accepted.get(article_id, set()))
            for article_id in target_ids
        }
        article_hashes = {
            int(item["id"]): str(item["article_hash"])
            for item in items
        }
        inserted_rows = await _insert_rows(
            conn,
            server_id,
            article_hashes,
            accepted_for_insert,
        )

        post_articles = {
            int(row["id"]): {
                "id": int(row["id"]),
                "article_hash": str(row["article_hash"]),
                "enrich_hash": (
                    str(row["enrich_hash"])
                    if row["enrich_hash"] is not None
                    else None
                ),
                "full_text": str(row["full_text"] or ""),
            }
            for row in await conn.fetch(
                """
                SELECT id, article_hash, enrich_hash, full_text
                FROM law_articles
                WHERE id = ANY($1::int[]) AND server_id = $2
                  AND kind = 'article'
                FOR UPDATE
                """,
                target_ids,
                server_id,
            )
        }
        if post_articles != state.current_articles:
            raise SynonymWriteError(
                "article generation state changed inside replacement transaction"
            )

        final_rows = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT id, server_id, article_id, term, weight, source, status,
                       suggested_by
                FROM law_synonyms
                WHERE article_id = ANY($1::int[])
                ORDER BY article_id, id
                """,
                target_ids,
            )
        ]
        immutable_after = sorted(
            _stored_synonym_identity(row)
            for row in final_rows
            if not _is_replaceable_auto_row(row, server_id)
        )
        if immutable_after != immutable_before:
            raise SynonymWriteError(
                "immutable owner/user/nonapproved synonym rows changed"
            )

        expected_auto_terms = {
            (
                article_id,
                term,
            )
            for article_id, terms in accepted_for_insert.items()
            for term in terms
            if (article_id, term) not in immutable_exact
        }
        final_auto_terms = {
            (int(row["article_id"]), str(row["term"]))
            for row in final_rows
            if (
                str(row["server_id"]) == server_id
                and str(row["source"]) == "auto"
                and str(row["status"]) == "approved"
            )
        }
        if final_auto_terms != expected_auto_terms or any(
            _is_replaceable_auto_row(row, server_id)
            and str(row["status"]) != "approved"
            for row in final_rows
        ):
            raise SynonymWriteError(
                "post-write auto synonym rows differ from staged replacement"
            )

        candidates = [
            (article_id, term)
            for article_id, terms in accepted_for_insert.items()
            for term in sorted(terms)
        ]
        post_owners = await _current_candidate_owner_details(
            conn,
            server_id,
            candidates,
            representative_only=True,
        )
        post_nonapproved = await _current_nonapproved_exact_rows(
            conn,
            server_id,
            candidates,
        )
        if post_owners or post_nonapproved:
            raise SynonymWriteError(
                "post-insert exact collision/state drift detected"
            )

        final_registry = await load_exact_signature_registry(conn, server_id)
        approved_terms: Dict[int, set[str]] = defaultdict(set)
        for row in final_rows:
            if (
                str(row["server_id"]) == server_id
                and str(row["status"]) == "approved"
            ):
                approved_terms[int(row["article_id"])].add(str(row["term"]))
        missing_candidates = {
            article_id: sorted(
                accepted_for_insert[article_id]
                - approved_terms.get(article_id, set())
            )
            for article_id in target_ids
            if (
                accepted_for_insert[article_id]
                - approved_terms.get(article_id, set())
            )
        }
        if missing_candidates:
            raise SynonymWriteError(
                "post-write approved rows miss staged terms: "
                f"{missing_candidates}"
            )
        final_physical = {
            article_id: len(approved_terms.get(article_id, set()))
            for article_id in target_ids
        }
        final_effective = {
            article_id: final_registry.effective_count(article_id)
            for article_id in target_ids
        }
        post_quality_errors = _auto_replacement_quality_errors(
            state,
            items,
            final_physical,
            final_effective,
            required_physical=required_physical,
            required_effective=required_effective,
        )
        if post_quality_errors:
            raise SynonymWriteError(
                "server-wide replacement post-write quality gate failed: "
                + ", ".join(post_quality_errors)
            )

        inserted_lookup = {
            (
                int(row["id"]),
                int(row["article_id"]),
                str(row["term"]),
                str(row["source"]),
                str(row["status"]),
                float(row["weight"]),
                row["suggested_by"],
            )
            for row in inserted_rows
        }
        attempted = sum(
            len(terms)
            for terms in accepted_for_insert.values()
        )
        committed_result = SynonymWriteResult(
            inserted=len(inserted_lookup),
            already_present=max(0, attempted - len(inserted_lookup)),
            accepted_terms={
                article_id: sorted(accepted_for_insert[article_id])
                for article_id in target_ids
            },
            effective_before=dict(state.result.effective_before),
            effective_after=final_effective,
            physical_after=final_physical,
            inserted_rows=[
                {
                    "id": row_id,
                    "server_id": server_id,
                    "article_id": article_id,
                    "article_hash": article_hashes[article_id],
                    "term": term,
                    "source": source,
                    "status": status,
                    "weight": weight,
                    "suggested_by": suggested_by,
                }
                for (
                    row_id,
                    article_id,
                    term,
                    source,
                    status,
                    weight,
                    suggested_by,
                ) in sorted(inserted_lookup)
            ],
        )
        committed_registry = final_registry

    if committed_result is None or committed_registry is None:
        raise SynonymWriteError("bulk replacement transaction produced no result")
    return AutoSynonymBulkReplacementOutcome(
        result=committed_result,
        final_registry=committed_registry,
    )


async def replace_auto_synonyms_bulk_in_transaction(
    conn,
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    generation_snapshot: Mapping[str, Any],
    lock_already_held: bool = False,
    minimum_physical: int = BULK_REPLACEMENT_PHYSICAL,
    minimum_effective: int = BULK_REPLACEMENT_EFFECTIVE,
) -> AutoSynonymBulkReplacementOutcome:
    """Apply synonyms inside a caller-owned transaction.

    The returned registry is isolated.  The caller must publish it only after
    its outer transaction (including article metadata and FRIDA scheduling)
    commits successfully.  A nested transaction/savepoint protects the caller
    if it catches a post-write validation error instead of aborting the outer
    transaction.  With ``lock_already_held=True`` the caller must have acquired
    the per-server synonym advisory lock *before* taking any article row lock;
    this is the global deadlock-safe lock order.
    """

    is_in_transaction = getattr(conn, "is_in_transaction", None)
    if (
        not callable(is_in_transaction)
        or not is_in_transaction()
    ):
        raise SynonymWriteError(
            "bulk replacement core requires an active caller-owned transaction"
        )
    nested_transaction = getattr(conn, "transaction", None)
    if not callable(nested_transaction):
        raise SynonymWriteError(
            "bulk replacement core requires savepoint support"
        )
    return await _replace_auto_synonyms_bulk_with_context(
        nested_transaction(),
        server_id,
        items,
        generation_snapshot=generation_snapshot,
        lock_already_held=lock_already_held,
        minimum_physical=minimum_physical,
        minimum_effective=minimum_effective,
    )


async def replace_auto_synonyms_bulk(
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    generation_snapshot: Mapping[str, Any],
    registry: Optional[ExactSignatureRegistry] = None,
    minimum_physical: int = BULK_REPLACEMENT_PHYSICAL,
    minimum_effective: int = BULK_REPLACEMENT_EFFECTIVE,
) -> SynonymWriteResult:
    """Self-owned atomic wrapper that publishes a registry after commit."""

    outcome = await _replace_auto_synonyms_bulk_with_context(
        db_manager.transaction(),
        server_id,
        items,
        generation_snapshot=generation_snapshot,
        lock_already_held=False,
        minimum_physical=minimum_physical,
        minimum_effective=minimum_effective,
    )
    if registry is not None and items:
        registry.replace_with(outcome.final_registry)
    return outcome.result


async def add_auto_synonyms_bulk(
    server_id: str,
    items: Sequence[Mapping[str, Any]],
    *,
    registry: Optional[ExactSignatureRegistry] = None,
    run_id: Optional[str] = None,
    plan_hash: Optional[str] = None,
    manifest_hash: Optional[str] = None,
) -> SynonymWriteResult:
    """CAS-protected additive write in one self-owned transaction."""

    async with db_manager.transaction() as conn:
        return await add_auto_synonyms_bulk_in_transaction(
            conn,
            server_id,
            items,
            registry=registry,
            run_id=run_id,
            plan_hash=plan_hash,
            manifest_hash=manifest_hash,
        )


async def replace_auto_synonyms(
    conn,
    server_id: str,
    article_id: int,
    expected_article_hash: str,
    synonyms: Iterable[Any],
    *,
    registry: Optional[ExactSignatureRegistry] = None,
    lock_already_held: bool = False,
    prior_enrichment_current: Optional[bool] = None,
    minimum_physical: int = MIN_REPLACEMENT_PHYSICAL,
    minimum_effective: int = MIN_REPLACEMENT_EFFECTIVE,
) -> SynonymWriteResult:
    """Replace regular auto-enrichment terms using the same shared safeguards.

    The replacement is previewed before the old dictionary is deleted.  A
    sparse/collision-heavy model response therefore aborts the surrounding
    transaction instead of marking an article enriched with a degraded
    dictionary.  Callers may raise either coverage floor for stricter
    workflows, but cannot lower the shared minimums.
    """

    required_physical = max(
        MIN_REPLACEMENT_PHYSICAL,
        int(minimum_physical),
    )
    required_effective = max(
        MIN_REPLACEMENT_EFFECTIVE,
        int(minimum_effective),
    )

    candidate_synonyms = list(synonyms)
    if not lock_already_held:
        await acquire_synonym_write_lock(conn, server_id)
    article = await conn.fetchrow(
        """
        SELECT id, article_hash, enrich_hash, full_text
        FROM law_articles
        WHERE id = $1 AND server_id = $2 AND kind = 'article'
          AND article_hash = $3
        FOR UPDATE
        """,
        article_id,
        server_id,
        expected_article_hash,
    )
    if article is None:
        raise SynonymWriteError(
            f"ownership/kind/article_hash CAS failed for article id {article_id}"
        )

    if registry is None:
        registry = await load_exact_signature_registry(conn, server_id)
    existing_rows = await conn.fetch(
        """
        SELECT id, server_id, article_id, term, weight, source, status,
               suggested_by
        FROM law_synonyms
        WHERE article_id = $1
        FOR UPDATE
        """,
        article_id,
    )
    try:
        foreign_auto_ids = foreign_auto_ownership_article_ids(
            server_id=server_id,
            auto_rows=(
                dict(row)
                for row in existing_rows
                if row.get("source") == "auto"
            ),
        )
    except ValueError as exc:
        raise SynonymWriteError(
            "single replacement auto synonym ownership cannot be "
            "validated safely"
        ) from exc
    if foreign_auto_ids:
        raise SynonymWriteError(
            "foreign-server auto synonym ownership for article ids: "
            f"{list(foreign_auto_ids)}"
        )

    # Preview coverage on an isolated registry.  It is important that a
    # quality-gate exception does not poison the O(N) registry shared by later
    # article transactions.
    same_generation = (
        bool(prior_enrichment_current)
        if prior_enrichment_current is not None
        else str(article["enrich_hash"] or "") == str(article["article_hash"])
    )
    exempt = is_explicitly_empty_or_repealed_article(article["full_text"])
    if exempt:
        if candidate_synonyms:
            raise SynonymWriteError(
                f"article {article_id} is a tombstone but synonyms are not empty"
            )
        retained_rows = [
            row
            for row in existing_rows
            if same_generation or str(row["source"]) != "auto"
        ]
        if retained_rows:
            raise SynonymWriteError(
                f"article {article_id} tombstone retains "
                f"{len(retained_rows)} immutable/current synonym rows"
            )
    # A changed-generation replacement deletes every old auto row in this
    # transaction, so such an exact row will not block the INSERT.
    # Manual/user rows remain immutable and therefore still block the exact
    # candidate.  Same-generation enrichment is additive and deletes nothing.
    ignored_exact_rows = (
        {
            (article_id, str(row["term"]))
            for row in existing_rows
            if str(row["source"]) == "auto"
        }
        if not same_generation
        else set()
    )
    result = SynonymWriteResult(
        effective_before={article_id: registry.effective_count(article_id)}
    )
    accepted, rejected, keyed = await _prepare_batch(
        conn,
        server_id,
        registry,
        [{"id": article_id, "synonyms": candidate_synonyms}],
        ignored_exact_rows=ignored_exact_rows,
    )
    result.rejected_terms = rejected
    result.rejected_collisions = sum(len(values) for values in rejected.values())

    base_registry = registry.clone()
    if not same_generation:
        for row in existing_rows:
            if str(row["source"]) == "auto" and str(row["status"]) == "approved":
                base_registry.remove(
                    int(row["article_id"]),
                    str(row["source"]),
                    str(row["term"]),
                )
    preview_registry = base_registry.clone()
    for key, terms in keyed.get(article_id, {}).items():
        for term in terms:
            if term in accepted.get(article_id, set()):
                preview_registry.add(article_id, "auto", term, key)

    retained_approved = {
        str(row["term"])
        for row in existing_rows
        if str(row["status"]) == "approved"
        and (same_generation or str(row["source"]) != "auto")
    }
    proposed_physical = len(
        retained_approved | set(accepted.get(article_id, set()))
    )
    proposed_effective = preview_registry.effective_count(article_id)
    previous_effective = registry.effective_count(article_id)
    candidate_physical = len(set(accepted.get(article_id, set())))
    candidate_reachable_terms = _query_reachable_canonical_terms(
        accepted.get(article_id, set())
    )
    candidate_effective_keys = {
        key
        for key, terms in keyed.get(article_id, {}).items()
        if set(terms) & candidate_reachable_terms
    }
    candidate_effective = len(candidate_effective_keys)
    quality_errors: list[str] = []
    if exempt:
        if proposed_physical != 0:
            quality_errors.append(
                f"tombstone physical={proposed_physical}!=0"
            )
        if proposed_effective != 0:
            quality_errors.append(
                f"tombstone effective={proposed_effective}!=0"
            )
    else:
        if same_generation and candidate_physical < required_physical:
            quality_errors.append(
                "candidate "
                f"physical={candidate_physical}<{required_physical}"
            )
        if same_generation and candidate_effective < required_effective:
            quality_errors.append(
                "candidate "
                f"effective={candidate_effective}<{required_effective}"
            )
        if proposed_physical < required_physical:
            quality_errors.append(
                f"physical={proposed_physical}<{required_physical}"
            )
        if proposed_effective < required_effective:
            quality_errors.append(
                f"effective={proposed_effective}<{required_effective}"
            )
        if same_generation and proposed_effective < previous_effective:
            quality_errors.append(
                f"effective regression {previous_effective}->{proposed_effective}"
            )
    if quality_errors:
        raise SynonymWriteError(
            f"synonym replacement quality gate failed for article {article_id}: "
            + ", ".join(quality_errors)
        )

    if not same_generation:
        await conn.fetch(
            """
            DELETE FROM law_synonyms
            WHERE article_id = $1 AND source = 'auto'
            RETURNING article_id, term, source
            """,
            article_id,
        )
    article_hashes = {article_id: expected_article_hash}
    attempted = sum(len(values) for values in accepted.values())
    inserted_rows = await _insert_rows(conn, server_id, article_hashes, accepted)
    result.inserted = len(inserted_rows)
    result.already_present = max(0, attempted - result.inserted)

    physical_keys: Dict[str, EffectiveKey] = {}
    for key, terms in keyed.get(article_id, {}).items():
        for term in terms:
            physical_keys[term] = key
    final_registry = base_registry.clone()
    inserted_terms: set[str] = set()
    for row in inserted_rows:
        term = str(row["term"])
        inserted_terms.add(term)
        key = physical_keys.get(term)
        if key is not None:
            final_registry.add(article_id, str(row["source"]), term, key)

    # Defence in depth: repeat the quality floor using only rows known to be
    # physically approved after INSERT.  A skipped ON CONFLICT (or future
    # writer drift) must not let the surrounding transaction commit
    # enrich_hash with an underfilled dictionary.
    final_physical_terms = retained_approved | inserted_terms
    final_physical = len(final_physical_terms)
    final_effective = final_registry.effective_count(article_id)
    final_candidate_terms = (
        set(accepted.get(article_id, set())) & final_physical_terms
    )
    final_candidate_reachable_terms = _query_reachable_canonical_terms(
        final_candidate_terms
    )
    final_candidate_physical = len(final_candidate_terms)
    final_candidate_effective = len({
        key
        for key, terms in keyed.get(article_id, {}).items()
        if set(terms) & final_candidate_reachable_terms
        and set(final_registry.owners.get(key, {})) == {article_id}
    })
    post_write_errors: list[str] = []
    if exempt:
        if final_physical != 0:
            post_write_errors.append(
                f"tombstone physical={final_physical}!=0"
            )
        if final_effective != 0:
            post_write_errors.append(
                f"tombstone effective={final_effective}!=0"
            )
    else:
        if (
            same_generation
            and final_candidate_physical < required_physical
        ):
            post_write_errors.append(
                "candidate "
                f"physical={final_candidate_physical}"
                f"<{required_physical}"
            )
        if (
            same_generation
            and final_candidate_effective < required_effective
        ):
            post_write_errors.append(
                "candidate "
                f"effective={final_candidate_effective}"
                f"<{required_effective}"
            )
        if final_physical < required_physical:
            post_write_errors.append(
                f"physical={final_physical}<{required_physical}"
            )
        if final_effective < required_effective:
            post_write_errors.append(
                f"effective={final_effective}<{required_effective}"
            )
        if same_generation and final_effective < previous_effective:
            post_write_errors.append(
                f"effective regression {previous_effective}->{final_effective}"
            )
    if post_write_errors:
        raise SynonymWriteError(
            "synonym replacement post-write quality gate failed for article "
            f"{article_id}: " + ", ".join(post_write_errors)
        )

    registry.replace_with(final_registry)
    result.effective_after = {
        article_id: final_effective
    }
    return result


def project_context_for_server(server_id: str) -> str:
    """Project-level dialect, never a per-server ranking rule."""

    from law_sources_data import get_project_for_server

    project_id = get_project_for_server(server_id)
    if project_id == "rmrp":
        return (
            "Сеттинг: современная Россия (RMRP). Уместны МВД, ФСБ, СК, "
            "ГИБДД, прокуратура; не используй LSPD/FIB/EMS и GTA-жаргон."
        )
    if project_id in {"majestic_rp", "gta5rp"}:
        return (
            "Сеттинг: RP по GTA V, штат Сан-Андреас. Уместны LSPD/FIB/EMS "
            "и общеупотребимый GTA/RP-жаргон; не переноси российские ведомства."
        )
    return (
        "Сеттинг не определён. Используй только нейтральные формулировки, "
        "которые прямо подтверждены текстом нормы."
    )
