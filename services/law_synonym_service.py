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
from copy import deepcopy
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from database.manager import db_manager


MIN_TERM_LENGTH = 2
MAX_TERM_LENGTH = 80
MIN_REPLACEMENT_PHYSICAL = 8
MIN_REPLACEMENT_EFFECTIVE = 8


class SynonymWriteError(RuntimeError):
    """The requested synonym write failed a fail-closed safety check."""


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

    ``records`` retains source/term identity so a regular enrichment can remove
    only the old ``source='auto'`` rows from the registry before adding their
    replacements.  Existing collisions are preserved but are not considered
    effective coverage; this task must not destructively rewrite legacy data.
    """

    records: Dict[Tuple[int, str, str], EffectiveKey] = field(default_factory=dict)
    owners: Dict[EffectiveKey, Dict[int, int]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    effective_counts: Dict[int, int] = field(default_factory=lambda: defaultdict(int))

    def add(self, article_id: int, source: str, term: str, key: EffectiveKey) -> None:
        record_key = (int(article_id), str(source), term)
        if record_key in self.records:
            return
        counts = self.owners.setdefault(key, {})
        before = set(counts)
        if len(before) == 1:
            self.effective_counts[next(iter(before))] -= 1
        self.records[record_key] = key
        counts[int(article_id)] = counts.get(int(article_id), 0) + 1
        after = set(counts)
        if len(after) == 1:
            self.effective_counts[next(iter(after))] += 1

    def remove(self, article_id: int, source: str, term: str) -> None:
        record_key = (int(article_id), str(source), term)
        key = self.records.pop(record_key, None)
        if key is None:
            return
        counts = self.owners.get(key)
        if not counts:
            return
        before = set(counts)
        if len(before) == 1:
            self.effective_counts[next(iter(before))] -= 1
        remaining = counts.get(int(article_id), 0) - 1
        if remaining > 0:
            counts[int(article_id)] = remaining
        else:
            counts.pop(int(article_id), None)
        if not counts:
            self.owners.pop(key, None)
        else:
            after = set(counts)
            if len(after) == 1:
                self.effective_counts[next(iter(after))] += 1

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
        self.owners = deepcopy(other.owners)
        self.effective_counts = deepcopy(other.effective_counts)


@dataclass
class SynonymWriteResult:
    inserted: int = 0
    already_present: int = 0
    rejected_collisions: int = 0
    rejected_terms: Dict[int, list[str]] = field(default_factory=dict)
    effective_before: Dict[int, int] = field(default_factory=dict)
    effective_after: Dict[int, int] = field(default_factory=dict)
    accepted_terms: Dict[int, list[str]] = field(default_factory=dict)
    inserted_rows: list[Dict[str, Any]] = field(default_factory=list)


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
    """Load approved canonical terms and their DB-computed exact-leg signatures."""

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
    canonical_rows: list[Tuple[int, str, str]] = []
    canonical_terms: list[str] = []
    for row in rows:
        surface = normalize_synonym_term(row["term"])
        canonical = canonical_synonym_term(row["term"])
        # A surface-only slang/question form is not reachable by the exact
        # synonym leg after query normalization.  Its stored canonical partner
        # is what contributes effective coverage.
        if surface is None or canonical is None or surface != canonical:
            continue
        canonical_rows.append((int(row["article_id"]), str(row["source"]), canonical))
        canonical_terms.append(canonical)

    signatures = await _signature_map(queryable, canonical_terms)
    registry = ExactSignatureRegistry()
    for article_id, source, term in canonical_rows:
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

    candidates = [
        (article_id, term)
        for article_id, groups in keyed.items()
        for terms in groups.values()
        for term in terms
    ]
    current_owners = await _current_candidate_owners(
        conn,
        server_id,
        candidates,
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
    for article_id, groups in keyed.items():
        for key, terms in groups.items():
            other_new = claims[key] - {article_id}
            for term in terms:
                other_existing = current_owners.get((article_id, term), set())
                if (
                    other_existing
                    or other_new
                    or (article_id, term) in blocked_exact_rows
                ):
                    unsafe_terms[article_id].add(term)
    # Surface+canonical companions are one semantic proposal.  If either
    # physical row is unsafe, keep neither: otherwise a dead surface-only row
    # could be stored after its reachable canonical companion was rejected.
    for article_id, canonical_groups in expanded_by_article.items():
        for terms in canonical_groups.values():
            if set(terms) & unsafe_terms.get(article_id, set()):
                rejected[article_id].extend(sorted(terms))
            else:
                accepted[article_id].update(terms)
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


async def _current_candidate_owners(
    conn,
    server_id: str,
    candidates: Sequence[Tuple[int, str]],
) -> Dict[Tuple[int, str], set[int]]:
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
    rows = await conn.fetch(
        """
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
        SELECT match.candidate_article_id, match.candidate_term,
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
        """,
        server_id,
        [article_id for article_id, _term in unique],
        [term for _article_id, term in unique],
    )
    owners: Dict[Tuple[int, str], set[int]] = defaultdict(set)
    for row in rows:
        owners[
            (int(row["candidate_article_id"]), str(row["candidate_term"]))
        ].add(int(row["owner_article_id"]))
    return dict(owners)


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
    # Existing conflicts were already represented in the loaded registry;
    # only newly returned rows need to be added.
    canonical_keys: Dict[Tuple[int, str], EffectiveKey] = {}
    for article_id, groups in keyed.items():
        for key, grouped_terms in groups.items():
            for term in grouped_terms:
                if term == canonical_synonym_term(term):
                    canonical_keys[(article_id, term)] = key
    for (
        _row_id,
        article_id,
        term,
        source,
        _status,
        _weight,
        _suggested_by,
    ) in inserted_lookup:
        key = canonical_keys.get((article_id, term))
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
                if (
                    term in accepted.get(article_id, set())
                    and term == canonical_synonym_term(term)
                ):
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
        SELECT article_id, term, source, status
        FROM law_synonyms
        WHERE article_id = $1
        FOR UPDATE
        """,
        article_id,
    )

    # Preview coverage on an isolated registry.  It is important that a
    # quality-gate exception does not poison the O(N) registry shared by later
    # article transactions.
    same_generation = (
        bool(prior_enrichment_current)
        if prior_enrichment_current is not None
        else str(article["enrich_hash"] or "") == str(article["article_hash"])
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
        [{"id": article_id, "synonyms": list(synonyms)}],
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
            if (
                term in accepted.get(article_id, set())
                and term == canonical_synonym_term(term)
            ):
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
    candidate_effective_keys = {
        key
        for key, terms in keyed.get(article_id, {}).items()
        if any(
            term in accepted.get(article_id, set())
            and term == canonical_synonym_term(term)
            for term in terms
        )
    }
    candidate_effective = len(candidate_effective_keys)
    exempt = is_explicitly_empty_or_repealed_article(article["full_text"])
    quality_errors: list[str] = []
    if not exempt:
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

    canonical_keys: Dict[str, EffectiveKey] = {}
    for key, terms in keyed.get(article_id, {}).items():
        for term in terms:
            if term == canonical_synonym_term(term):
                canonical_keys[term] = key
    final_registry = base_registry.clone()
    inserted_terms: set[str] = set()
    for row in inserted_rows:
        term = str(row["term"])
        inserted_terms.add(term)
        key = canonical_keys.get(term)
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
    final_candidate_physical = len(final_candidate_terms)
    final_candidate_effective = len({
        key
        for key, terms in keyed.get(article_id, {}).items()
        if any(
            term in final_candidate_terms
            and term == canonical_synonym_term(term)
            for term in terms
        )
        and set(final_registry.owners.get(key, {})) == {article_id}
    })
    post_write_errors: list[str] = []
    if not exempt:
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
