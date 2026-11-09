"""Deterministic contrast candidates for automatic law enrichment.

The normal enrichment prompt already receives the two neighbouring articles on
either side of a target.  That local window cannot reveal a similar norm in a
different document (or much farther away in the same document), which is where
many synonym-ownership mistakes originate.

This module is deliberately pure and conservative:

* it reads mappings and returns immutable match metadata;
* it never calls a model, database, search service, or writer;
* it does not return article text (the caller owns prompt-size decisions);
* legal boilerplate and standalone numbers cannot create a match;
* results are ordered by score and article id, so input order is irrelevant.

The selector is only a context-recall aid.  A match is not proof of a collision
and must never be used as an automatic rejection by itself.
"""

from __future__ import annotations

import heapq
import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


DEFAULT_NEIGHBOR_RADIUS = 2
DEFAULT_MAX_COMPETITORS = 3
DEFAULT_MINIMUM_SCORE = 0.22
# Pair-score caching is an optimisation only.  Keep it process-call local and
# strictly bounded: adversarial corpora can have O(N²) distinct semantic
# signatures even though normal legal corpora contain many repeated shapes.
MAX_SIMILARITY_CACHE_ENTRIES = 8192

_WORD_RE = re.compile(
    r"\d+(?:[.,]\d+)?|[A-Za-zА-Яа-яЁё]+",
    re.UNICODE,
)
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?\Z")
_SPACE_RE = re.compile(r"\s+")

# These words are either legal scaffolding or general Russian function words.
# In particular, sharing only "статья / закон / штат / порядок" must never be
# enough to nominate another article.
_STOP_WORDS = frozenset(
    {
        "а",
        "без",
        "более",
        "бы",
        "был",
        "была",
        "были",
        "быть",
        "в",
        "во",
        "весь",
        "влечет",
        "влечёт",
        "глава",
        "для",
        "до",
        "его",
        "ее",
        "её",
        "если",
        "же",
        "закон",
        "закона",
        "законе",
        "законом",
        "законодательство",
        "и",
        "из",
        "или",
        "им",
        "их",
        "к",
        "как",
        "кодекс",
        "кодекса",
        "кодексе",
        "который",
        "ли",
        "либо",
        "лицо",
        "лица",
        "лицом",
        "может",
        "на",
        "настоящая",
        "настоящего",
        "настоящей",
        "настоящим",
        "настоящий",
        "не",
        "него",
        "нее",
        "но",
        "о",
        "об",
        "от",
        "по",
        "под",
        "положение",
        "порядок",
        "порядка",
        "порядке",
        "порядком",
        "после",
        "предусмотрен",
        "предусмотрена",
        "предусмотрено",
        "предусмотренные",
        "при",
        "пункт",
        "пункта",
        "раздел",
        "с",
        "сан",
        "со",
        "согласно",
        "соответствии",
        "соответствующий",
        "ст",
        "статья",
        "статье",
        "статьи",
        "статью",
        "статьей",
        "статьёй",
        "статей",
        "также",
        "такие",
        "тем",
        "то",
        "у",
        "указанный",
        "устанавливает",
        "установленный",
        "часть",
        "части",
        "чем",
        "что",
        "штат",
        "штата",
        "штате",
        "штатом",
        "это",
        "этой",
        "этот",
        "является",
    }
)

# A small language-level suffix set provides stable recall across ordinary
# Russian inflections.  It is intentionally not a full morphological analyser:
# aggressive stemming would manufacture similarities in legal text.
_RU_SUFFIXES = tuple(
    sorted(
        {
            "ающего",
            "яющего",
            "ующего",
            "ившего",
            "ывшего",
            "ировать",
            "ируются",
            "ирует",
            "ируют",
            "ование",
            "ования",
            "ениями",
            "ациями",
            "яциями",
            "овского",
            "евского",
            "ического",
            "скими",
            "ского",
            "скому",
            "ством",
            "ствами",
            "ства",
            "ение",
            "ения",
            "ений",
            "ением",
            "ению",
            "овать",
            "ывает",
            "ивают",
            "енный",
            "енная",
            "енное",
            "енные",
            "енного",
            "енной",
            "енному",
            "енными",
            "ающий",
            "ающая",
            "ающее",
            "ающие",
            "яющий",
            "яющая",
            "яющее",
            "яющие",
            "иями",
            "ами",
            "ями",
            "ого",
            "ему",
            "ому",
            "ими",
            "ыми",
            "их",
            "ых",
            "им",
            "ым",
            "ий",
            "ый",
            "ой",
            "ая",
            "яя",
            "ое",
            "ее",
            "ые",
            "ие",
            "ам",
            "ям",
            "ах",
            "ях",
            "ов",
            "ев",
            "ом",
            "ем",
            "ую",
            "юю",
            "а",
            "я",
            "ы",
            "и",
            "у",
            "ю",
            "е",
            "о",
            "й",
            "ь",
        },
        key=lambda value: (-len(value), value),
    )
)

# Domain-wide action/concept stems receive a modest boost.  These are not
# server, document, or article identifiers.
_ACTION_PREFIXES = (
    "аннулир",
    "арест",
    "вмеш",
    "ввод",
    "делегир",
    "задерж",
    "запрещ",
    "изб",
    "контрол",
    "лиценз",
    "лиш",
    "надзор",
    "напад",
    "назнач",
    "наруш",
    "обыск",
    "огранич",
    "осуществл",
    "отстран",
    "передач",
    "подчин",
    "похищ",
    "преслед",
    "приостан",
    "примен",
    "провер",
    "причин",
    "розыск",
    "сбыт",
    "стрел",
    "угрож",
    "уклон",
    "уволь",
    "убий",
    "хран",
)

_CONTEXT_PREFIXES = (
    "агентств",
    "больниц",
    "город",
    "департамент",
    "киса",
    "министер",
    "округ",
    "организац",
    "прокуратур",
    "служб",
    "территор",
    "управлен",
)

_THRESHOLD_CUES = frozenset(
    {
        "более",
        "до",
        "менее",
        "минимум",
        "не",
        "от",
        "превышает",
        "ровно",
        "свыше",
    }
)
_THRESHOLD_UNIT_PREFIXES = (
    "грам",
    "килограм",
    "миллиграм",
    "минут",
    "час",
    "дн",
    "доллар",
    "звезд",
    "метр",
    "процент",
)


@dataclass(frozen=True)
class EnrichmentCompetitor:
    """One bounded context candidate; source text intentionally stays absent."""

    article_id: int
    score: float
    reason: str


@dataclass(frozen=True)
class _Profile:
    article_id: int
    document_id: Any
    source_order: int
    features: Mapping[str, float]
    eligibility_tokens: frozenset[str]
    anchor_tokens: frozenset[str]
    threshold_features: frozenset[str]


@dataclass(frozen=True)
class _PreparedSimilarityProfile:
    """IDF-weighted values reused across all pair comparisons."""

    weights: Mapping[str, float]
    norm: float
    mass: float


class _BoundedSimilarityCache:
    """Small exact-result cache whose capacity cannot grow with pair count."""

    __slots__ = ("_max_entries", "_values")

    def __init__(self, max_entries: int) -> None:
        if max_entries < 0:
            raise ValueError("max_entries must be non-negative")
        self._max_entries = max_entries
        self._values: dict[tuple[int, int], float | None] = {}

    def lookup(self, key: tuple[int, int]) -> tuple[bool, float | None]:
        try:
            return True, self._values[key]
        except KeyError:
            return False, None

    def store(self, key: tuple[int, int], value: float | None) -> None:
        if len(self._values) < self._max_entries:
            self._values[key] = value

    def __len__(self) -> int:
        return len(self._values)


def _normalize_text(value: Any) -> str:
    return _SPACE_RE.sub(
        " ",
        unicodedata.normalize("NFKC", str(value or ""))
        .lower()
        .replace("ё", "е"),
    ).strip()


def _stem_token(token: str) -> str:
    """Apply a narrow, deterministic inflection fold to one lexical token."""

    if not token or token.isdigit():
        return ""
    if token.isascii():
        if len(token) >= 6 and token.endswith("ies"):
            return token[:-3] + "y"
        if len(token) >= 5 and token.endswith("es"):
            return token[:-2]
        if len(token) >= 5 and token.endswith("s"):
            return token[:-1]
        return token

    if len(token) < 5:
        return token
    for suffix in _RU_SUFFIXES:
        minimum_stem = 5 if len(suffix) >= 2 else 4
        if token.endswith(suffix) and len(token) - len(suffix) >= minimum_stem:
            return token[: -len(suffix)]
    return token


def _is_action(token: str) -> bool:
    return any(token.startswith(prefix) for prefix in _ACTION_PREFIXES)


def _is_context_anchor(token: str) -> bool:
    return any(token.startswith(prefix) for prefix in _CONTEXT_PREFIXES)


def _meaningful_tokens(value: Any) -> tuple[list[str], set[str]]:
    """Return meaningful stems and original all-caps acronym stems."""

    original = unicodedata.normalize("NFKC", str(value or "")).replace("ё", "е")
    raw_tokens = _WORD_RE.findall(original)
    tokens: list[str] = []
    acronyms: set[str] = set()
    for raw in raw_tokens:
        normalized = raw.lower()
        if _NUMBER_TOKEN_RE.fullmatch(normalized) or normalized in _STOP_WORDS:
            continue
        stem = _stem_token(normalized)
        if not stem or stem in _STOP_WORDS or len(stem) < 2:
            continue
        tokens.append(stem)
        if (
            2 <= len(raw) <= 12
            and raw.isalpha()
            and raw.upper() == raw
            and raw.lower() != raw
        ):
            acronyms.add(stem)
    return tokens, acronyms


def _threshold_features(value: Any) -> set[str]:
    """Extract only quantities tied to a recognised threshold unit."""

    raw_tokens = [
        token.lower()
        for token in _WORD_RE.findall(_normalize_text(value))
    ]
    features: set[str] = set()
    for index, token in enumerate(raw_tokens):
        if _NUMBER_TOKEN_RE.fullmatch(token) is None:
            continue
        unit = ""
        for following in raw_tokens[index + 1:index + 3]:
            stem = _stem_token(following)
            if any(stem.startswith(prefix) for prefix in _THRESHOLD_UNIT_PREFIXES):
                unit = stem
                break
        if not unit:
            continue
        cues = [
            cue
            for cue in raw_tokens[max(0, index - 2):index]
            if cue in _THRESHOLD_CUES
        ]
        qualifier = "_".join(cues[-2:]) if cues else "quantity"
        number = token.replace(",", ".")
        if "." in number:
            integer, fraction = number.split(".", 1)
            number = f"{int(integer)}.{fraction.rstrip('0') or '0'}"
        else:
            number = str(int(number))
        features.add(f"threshold:{qualifier}:{number}:{unit}")
    return features


def _put_max(features: dict[str, float], key: str, weight: float) -> None:
    features[key] = max(weight, features.get(key, 0.0))


def _profile(article: Mapping[str, Any]) -> _Profile:
    try:
        article_id = int(article["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("every article must have an integer id") from exc

    try:
        source_order = int(article.get("source_order") or 0)
    except (TypeError, ValueError):
        source_order = 0

    features: dict[str, float] = {}
    eligibility_tokens: set[str] = set()
    anchors: set[str] = set()
    thresholds: set[str] = set()

    field_values: list[tuple[Any, float, bool]] = [
        (article.get("title"), 2.4, True),
        (article.get("full_text"), 1.0, True),
        # A document title improves scoring but cannot by itself make two
        # articles eligible: every distant article in one act shares it.
        (article.get("doc_title"), 0.45, False),
    ]
    synonyms = article.get("current_auto_synonyms") or ()
    if isinstance(synonyms, str):
        synonyms = (synonyms,)
    if isinstance(synonyms, Sequence):
        field_values.extend((value, 1.8, True) for value in synonyms)

    for value, field_weight, eligible in field_values:
        tokens, acronyms = _meaningful_tokens(value)
        if not tokens:
            continue
        if eligible:
            eligibility_tokens.update(tokens)
        for token in tokens:
            token_weight = field_weight
            if _is_action(token):
                token_weight *= 1.25
                if eligible:
                    anchors.add(token)
            if _is_context_anchor(token):
                token_weight *= 1.3
                if eligible:
                    anchors.add(token)
            _put_max(features, f"token:{token}", token_weight)
        for first, second in zip(tokens, tokens[1:]):
            if first == second:
                continue
            _put_max(
                features,
                f"bigram:{first}|{second}",
                field_weight * 1.35,
            )
        for acronym in acronyms:
            _put_max(features, f"acronym:{acronym}", field_weight * 1.8)
            if eligible:
                anchors.add(acronym)

        extracted_thresholds = _threshold_features(value)
        thresholds.update(extracted_thresholds)
        for threshold in extracted_thresholds:
            _put_max(features, threshold, field_weight * 2.0)

    return _Profile(
        article_id=article_id,
        document_id=article.get("law_document_id"),
        source_order=source_order,
        features=features,
        eligibility_tokens=frozenset(eligibility_tokens),
        anchor_tokens=frozenset(anchors),
        threshold_features=frozenset(thresholds),
    )


def _corpus_profiles(
    all_articles: Sequence[Mapping[str, Any]],
) -> dict[int, _Profile]:
    profiles: dict[int, _Profile] = {}
    for article in all_articles:
        profile = _profile(article)
        if profile.article_id in profiles:
            raise ValueError(f"duplicate article id {profile.article_id}")
        profiles[profile.article_id] = profile
    return profiles


def _idf_by_feature(profiles: Mapping[int, _Profile]) -> dict[str, float]:
    document_frequency: Counter[str] = Counter()
    for profile in profiles.values():
        document_frequency.update(profile.features)
    corpus_size = max(1, len(profiles))
    return {
        feature: math.log((corpus_size + 1) / (frequency + 1)) + 1.0
        for feature, frequency in document_frequency.items()
    }


def _weighted_features(
    profile: _Profile,
    idf: Mapping[str, float],
    corpus_size: int,
) -> dict[str, float]:
    unseen_idf = math.log(max(2, corpus_size + 1)) + 1.0
    return {
        feature: weight * idf.get(feature, unseen_idf)
        for feature, weight in profile.features.items()
    }


def _similarity(
    target: _Profile,
    candidate: _Profile,
    *,
    idf: Mapping[str, float],
    corpus_size: int,
) -> float | None:
    return _similarity_prepared(
        target,
        candidate,
        target_prepared=_prepare_similarity_profile(
            target,
            idf=idf,
            corpus_size=corpus_size,
        ),
        candidate_prepared=_prepare_similarity_profile(
            candidate,
            idf=idf,
            corpus_size=corpus_size,
        ),
        idf=idf,
    )


def _prepare_similarity_profile(
    profile: _Profile,
    *,
    idf: Mapping[str, float],
    corpus_size: int,
) -> _PreparedSimilarityProfile:
    weights = {
        feature: weight
        for feature, weight in _weighted_features(
            profile,
            idf,
            corpus_size,
        ).items()
        if not feature.startswith("bigram:")
    }
    return _PreparedSimilarityProfile(
        weights=weights,
        norm=math.sqrt(sum(weight * weight for weight in weights.values())),
        mass=sum(weights.values()),
    )


def _similarity_prepared(
    target: _Profile,
    candidate: _Profile,
    *,
    target_prepared: _PreparedSimilarityProfile,
    candidate_prepared: _PreparedSimilarityProfile,
    idf: Mapping[str, float],
) -> float | None:
    """Equivalent pair score without rebuilding the same weighted vectors."""

    shared_tokens = target.eligibility_tokens & candidate.eligibility_tokens
    if len(shared_tokens) < 2:
        return None

    shared_anchors = target.anchor_tokens & candidate.anchor_tokens
    shared_thresholds = (
        target.threshold_features & candidate.threshold_features
    )
    rare_shared = {
        token
        for token in shared_tokens
        if idf.get(f"token:{token}", 1.0) > 1.05
    }
    # In a tiny or very homogeneous corpus, IDF cannot distinguish terms.
    # Three meaningful shared stems still constitute strong lexical evidence.
    if not (shared_anchors or shared_thresholds or rare_shared) and len(
        shared_tokens
    ) < 3:
        return None

    # Unmatched bigrams are plentiful in normal prose and would swamp a real
    # concept match expressed with different word order.  Use lexical,
    # acronym, and threshold features for the vector; shared bigrams remain a
    # small positive proof below.
    target_weights = target_prepared.weights
    candidate_weights = candidate_prepared.weights
    shared_features = set(target_weights) & set(candidate_weights)
    if not shared_features:
        return None

    dot_product = sum(
        target_weights[feature] * candidate_weights[feature]
        for feature in shared_features
    )
    target_norm = target_prepared.norm
    candidate_norm = candidate_prepared.norm
    if not target_norm or not candidate_norm:
        return None
    cosine = dot_product / (target_norm * candidate_norm)

    shared_mass = sum(
        min(target_weights[feature], candidate_weights[feature])
        for feature in shared_features
    )
    target_mass = target_prepared.mass
    candidate_mass = candidate_prepared.mass
    containment = shared_mass / min(target_mass, candidate_mass)

    score = 0.62 * cosine + 0.38 * containment
    shared_bigrams = {
        feature
        for feature in target.features.keys() & candidate.features.keys()
        if feature.startswith("bigram:")
    }
    if shared_bigrams:
        score += min(0.03, 0.01 * len(shared_bigrams))
    if shared_thresholds:
        score += 0.04
    if shared_anchors:
        score += min(0.04, 0.012 * len(shared_anchors))
    return min(1.0, score)


def _similarity_profile_key(profile: _Profile) -> tuple[Any, ...]:
    """Group only profiles which produce exactly the same similarity score."""

    return (
        # Preserve feature insertion order as well as values.  Python's
        # floating-point norm/mass sums use that order, so collapsing a
        # differently ordered mapping could change a boundary comparison by
        # a final machine epsilon even when the mathematical vector is equal.
        tuple(profile.features.items()),
        tuple(sorted(profile.eligibility_tokens)),
        tuple(sorted(profile.anchor_tokens)),
        tuple(sorted(profile.threshold_features)),
    )


def _local_neighbor_ids(
    target_profiles: Mapping[int, _Profile],
    corpus_profiles: Mapping[int, _Profile],
    neighbor_radius: int,
) -> dict[int, frozenset[int]]:
    by_document: dict[Any, list[_Profile]] = defaultdict(list)
    for profile in corpus_profiles.values():
        if profile.document_id is not None:
            by_document[profile.document_id].append(profile)
    for profile in target_profiles.values():
        if (
            profile.document_id is not None
            and profile.article_id not in corpus_profiles
        ):
            by_document[profile.document_id].append(profile)
    for rows in by_document.values():
        rows.sort(key=lambda row: (row.source_order, row.article_id))

    positions: dict[int, tuple[list[_Profile], int]] = {}
    for rows in by_document.values():
        for index, profile in enumerate(rows):
            positions[profile.article_id] = (rows, index)

    excluded: dict[int, frozenset[int]] = {}
    for target_id in target_profiles:
        rows_and_index = positions.get(target_id)
        if rows_and_index is None:
            excluded[target_id] = frozenset({target_id})
            continue
        rows, index = rows_and_index
        start = max(0, index - neighbor_radius)
        end = min(len(rows), index + neighbor_radius + 1)
        excluded[target_id] = frozenset(
            row.article_id for row in rows[start:end]
        )
    return excluded


def select_enrichment_competitors(
    targets: Sequence[Mapping[str, Any]],
    all_articles: Sequence[Mapping[str, Any]],
    *,
    neighbor_radius: int = DEFAULT_NEIGHBOR_RADIUS,
    max_competitors: int = DEFAULT_MAX_COMPETITORS,
    minimum_score: float = DEFAULT_MINIMUM_SCORE,
) -> dict[int, tuple[EnrichmentCompetitor, ...]]:
    """Select far/cross-document articles worth adding to contrast context.

    ``targets`` may be a normal model batch while ``all_articles`` is the
    complete server catalog.  Existing positional neighbours (including the
    target itself) are excluded.  Returned metadata intentionally contains no
    source text; a caller can resolve selected ids and apply its own prompt
    budget.
    """

    if neighbor_radius < 0:
        raise ValueError("neighbor_radius must be non-negative")
    if max_competitors < 0:
        raise ValueError("max_competitors must be non-negative")
    if not 0.0 <= minimum_score <= 1.0:
        raise ValueError("minimum_score must be between 0 and 1")

    target_profiles: dict[int, _Profile] = {}
    for article in targets:
        profile = _profile(article)
        if profile.article_id in target_profiles:
            raise ValueError(f"duplicate target article id {profile.article_id}")
        target_profiles[profile.article_id] = profile

    if not target_profiles or max_competitors == 0:
        return {article_id: () for article_id in target_profiles}

    corpus = _corpus_profiles(all_articles)
    idf = _idf_by_feature(corpus)
    excluded = _local_neighbor_ids(target_profiles, corpus, neighbor_radius)
    corpus_size = len(corpus)

    signature_ids: dict[tuple[Any, ...], int] = {}
    signature_profiles: dict[int, _Profile] = {}

    def signature_id(profile: _Profile) -> int:
        key = _similarity_profile_key(profile)
        existing = signature_ids.get(key)
        if existing is not None:
            return existing
        created = len(signature_ids)
        signature_ids[key] = created
        signature_profiles[created] = profile
        return created

    target_signatures = {
        article_id: signature_id(profile)
        for article_id, profile in target_profiles.items()
    }
    corpus_signatures: dict[int, int] = {}

    def corpus_signature(article_id: int) -> int:
        existing = corpus_signatures.get(article_id)
        if existing is not None:
            return existing
        created = signature_id(corpus[article_id])
        corpus_signatures[article_id] = created
        return created

    prepared_signatures: dict[int, _PreparedSimilarityProfile] = {}

    def prepared_signature(
        profile_id: int,
    ) -> _PreparedSimilarityProfile:
        existing = prepared_signatures.get(profile_id)
        if existing is not None:
            return existing
        created = _prepare_similarity_profile(
            signature_profiles[profile_id],
            idf=idf,
            corpus_size=corpus_size,
        )
        prepared_signatures[profile_id] = created
        return created

    similarity_cache = _BoundedSimilarityCache(
        MAX_SIMILARITY_CACHE_ENTRIES
    )

    # Two shared meaningful tokens are required, so an inverted token index
    # avoids scoring every target against every server article.
    token_index: dict[str, set[int]] = defaultdict(set)
    for candidate in corpus.values():
        for token in candidate.eligibility_tokens:
            token_index[token].add(candidate.article_id)

    target_ids_by_signature: dict[int, list[int]] = defaultdict(list)
    for target_id, target_signature in target_signatures.items():
        target_ids_by_signature[target_signature].append(target_id)

    def scored_candidates(
        target_signature: int,
        *,
        excluded_ids: frozenset[int],
    ):
        """Yield exact rounded scores for one semantic target profile."""

        target = signature_profiles[target_signature]
        overlap_counts: Counter[int] = Counter()
        for token in target.eligibility_tokens:
            overlap_counts.update(token_index.get(token, ()))

        for candidate_id, overlap_count in overlap_counts.items():
            if overlap_count < 2 or candidate_id in excluded_ids:
                continue
            candidate_signature = corpus_signature(candidate_id)
            cache_key = (
                (target_signature, candidate_signature)
                if target_signature <= candidate_signature
                else (candidate_signature, target_signature)
            )
            cache_hit, score = similarity_cache.lookup(cache_key)
            if not cache_hit:
                left_signature, right_signature = cache_key
                score = _similarity_prepared(
                    signature_profiles[left_signature],
                    signature_profiles[right_signature],
                    target_prepared=prepared_signature(left_signature),
                    candidate_prepared=prepared_signature(right_signature),
                    idf=idf,
                )
                similarity_cache.store(cache_key, score)
            if score is not None and score >= minimum_score:
                yield candidate_id, round(score, 6)

    def competitor_match(
        target: _Profile,
        candidate_id: int,
        rounded_score: float,
    ) -> EnrichmentCompetitor:
        candidate = corpus[candidate_id]
        relation = (
            "far_same_document"
            if candidate.document_id == target.document_id
            and target.document_id is not None
            else "cross_document"
        )
        detail = (
            "threshold_and_lexical_overlap"
            if target.threshold_features & candidate.threshold_features
            else "rare_lexical_overlap"
        )
        return EnrichmentCompetitor(
            article_id=candidate_id,
            score=rounded_score,
            reason=f"{relation}:{detail}",
        )

    results: dict[int, tuple[EnrichmentCompetitor, ...]] = {}
    for target_signature, target_ids in target_ids_by_signature.items():
        if len(target_ids) > 1:
            # Score and rank once for semantically identical targets.  Their
            # document positions still receive individual neighbour filters
            # and reason labels below.
            # A target can discard at most its bounded neighbour-set size, so
            # top K plus that many candidates is sufficient for every member
            # of the group.  There is no need to retain the whole corpus.
            shared_ranking_limit = max_competitors + max(
                len(excluded[target_id])
                for target_id in target_ids
            )
            shared_ranking = heapq.nsmallest(
                shared_ranking_limit,
                scored_candidates(
                    target_signature,
                    excluded_ids=frozenset(),
                ),
                key=lambda match: (-match[1], match[0]),
            )
            for target_id in target_ids:
                target = target_profiles[target_id]
                selected: list[EnrichmentCompetitor] = []
                for candidate_id, rounded_score in shared_ranking:
                    if candidate_id in excluded[target_id]:
                        continue
                    selected.append(
                        competitor_match(
                            target,
                            candidate_id,
                            rounded_score,
                        )
                    )
                    if len(selected) == max_competitors:
                        break
                results[target_id] = tuple(selected)
            continue

        target_id = target_ids[0]
        target = target_profiles[target_id]
        ranked = heapq.nsmallest(
            max_competitors,
            scored_candidates(
                target_signature,
                excluded_ids=excluded[target_id],
            ),
            key=lambda match: (-match[1], match[0]),
        )
        results[target_id] = tuple(
            competitor_match(target, candidate_id, rounded_score)
            for candidate_id, rounded_score in ranked
        )

    # Preserve the original target mapping order even though repeated semantic
    # profiles are evaluated together.
    return {
        target_id: results[target_id]
        for target_id in target_profiles
    }
