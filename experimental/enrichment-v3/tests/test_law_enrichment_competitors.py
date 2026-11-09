"""Synthetic contract for far-article enrichment contrast selection."""

from __future__ import annotations

import copy
import math
import random
from collections import Counter, defaultdict

import pytest

from services import law_enrichment_competitors as competitor_module
from services.law_enrichment_competitors import (
    EnrichmentCompetitor,
    select_enrichment_competitors,
)


def _article(
    article_id: int,
    *,
    document_id: int,
    source_order: int,
    title: str,
    text: str,
    document: str = "Закон штата San Andreas",
    synonyms: tuple[str, ...] = (),
) -> dict:
    return {
        "id": article_id,
        "law_document_id": document_id,
        "source_order": source_order,
        "code": str(source_order),
        "title": title,
        "doc_title": document,
        "full_text": text,
        "current_auto_synonyms": list(synonyms),
    }


@pytest.mark.parametrize(
    ("organization", "activity"),
    [
        ("EMS", "оказывает экстренную медицинскую помощь"),
        ("LSSD", "обеспечивает патрулирование и общественную безопасность"),
    ],
)
def test_cross_document_location_and_organization_competitor_is_selected(
    organization,
    activity,
):
    paleto = _article(
        101,
        document_id=10,
        source_order=1,
        title=f"Зона ответственности {organization} в Палето-Бэй",
        text=(
            f"Служба {organization} {activity} на территории Палето-Бэй."
        ),
    )
    sandy = _article(
        202,
        document_id=20,
        source_order=4,
        title=f"Зона ответственности {organization} в Сэнди-Шорс",
        text=(
            f"Служба {organization} {activity} на территории Сэнди-Шорс."
        ),
    )
    unrelated = _article(
        303,
        document_id=30,
        source_order=1,
        title="Общий порядок",
        text="Настоящая статья устанавливает порядок, предусмотренный законом штата.",
    )

    matches = select_enrichment_competitors(
        [paleto],
        [unrelated, sandy, paleto],
    )[paleto["id"]]

    assert matches
    assert matches[0].article_id == sandy["id"]
    assert matches[0].reason == "cross_document:rare_lexical_overlap"


def test_cross_document_kisa_control_norm_is_selected():
    target = _article(
        401,
        document_id=41,
        source_order=3,
        title="Контроль деятельности КИСА",
        text=(
            "КИСА осуществляет внутренний контроль работы государственных "
            "информационных систем."
        ),
        document="Закон о государственном агентстве",
    )
    competitor = _article(
        402,
        document_id=42,
        source_order=9,
        title="Надзор и вмешательство КИСА",
        text=(
            "Контроль и вмешательство КИСА допускаются при угрозе "
            "государственным информационным системам."
        ),
        document="Закон о национальной безопасности",
    )
    ministry_noise = _article(
        403,
        document_id=43,
        source_order=2,
        title="Контроль Министерства культуры",
        text="Министерство культуры контролирует проведение публичных мероприятий.",
    )

    matches = select_enrichment_competitors(
        [target],
        [ministry_noise, target, competitor],
    )[target["id"]]

    assert [match.article_id for match in matches] == [competitor["id"]]


def test_legal_boilerplate_and_numbers_only_do_not_create_candidates():
    target = _article(
        501,
        document_id=51,
        source_order=1,
        title="Статья 12",
        text=(
            "Настоящая статья 12 устанавливает порядок, предусмотренный "
            "законом штата."
        ),
    )
    noise = _article(
        502,
        document_id=52,
        source_order=1,
        title="Статья 12",
        text=(
            "В соответствии со статьей 12 настоящий закон устанавливает "
            "порядок штата."
        ),
    )

    assert select_enrichment_competitors([target], [target, noise])[501] == ()


def test_far_same_document_candidate_is_included_but_local_window_is_not():
    target = _article(
        601,
        document_id=61,
        source_order=1,
        title="Лицензирование адвокатов",
        text="Адвокат получает профессиональную лицензию после аттестации.",
    )
    immediate = _article(
        602,
        document_id=61,
        source_order=2,
        title="Статус адвоката",
        text="Адвокат с профессиональной лицензией обладает специальным статусом.",
    )
    second_neighbor = _article(
        603,
        document_id=61,
        source_order=3,
        title="Обязанности адвоката",
        text="Адвокат обязан соблюдать профессиональную тайну.",
    )
    far = _article(
        604,
        document_id=61,
        source_order=8,
        title="Аннулирование лицензии адвоката",
        text=(
            "Профессиональная лицензия адвоката аннулируется после "
            "дисциплинарного решения."
        ),
    )

    matches = select_enrichment_competitors(
        [target],
        [far, second_neighbor, target, immediate],
    )[target["id"]]

    assert [match.article_id for match in matches] == [far["id"]]
    assert matches[0].reason == "far_same_document:rare_lexical_overlap"


def test_exact_threshold_overlap_is_identified_without_number_noise():
    target = _article(
        701,
        document_id=71,
        source_order=1,
        title="Хранение наркотических веществ",
        text="Запрещено хранение ровно 5 грамм наркотических веществ.",
    )
    same_threshold = _article(
        702,
        document_id=72,
        source_order=1,
        title="Административное хранение наркотиков",
        text="Ответственность наступает за хранение ровно 5 граммов наркотиков.",
    )
    different_threshold = _article(
        703,
        document_id=73,
        source_order=1,
        title="Крупный размер наркотиков",
        text="Ответственность наступает за хранение свыше 20 граммов наркотиков.",
    )

    matches = select_enrichment_competitors(
        [target],
        [different_threshold, target, same_threshold],
    )[target["id"]]

    assert matches[0].article_id == same_threshold["id"]
    assert matches[0].reason == "cross_document:threshold_and_lexical_overlap"


def test_result_is_deterministic_bounded_and_contains_no_article_text():
    target = _article(
        801,
        document_id=81,
        source_order=1,
        title="Полномочия Министерства финансов",
        text=(
            "Министерство финансов осуществляет контроль бюджетных "
            "платежей государственных организаций."
        ),
    )
    candidates = [
        _article(
            article_id,
            document_id=article_id,
            source_order=1,
            title="Бюджетный контроль Министерства финансов",
            text=(
                "Министерство финансов осуществляет контроль бюджетных "
                "платежей государственных организаций."
            ),
        )
        for article_id in (804, 802, 803, 805)
    ]
    corpus = [target, *candidates]
    original = copy.deepcopy(corpus)

    first = select_enrichment_competitors(
        [target],
        corpus,
        max_competitors=2,
    )[target["id"]]
    shuffled = list(corpus)
    random.Random(17).shuffle(shuffled)
    second = select_enrichment_competitors(
        [dict(target)],
        shuffled,
        max_competitors=2,
    )[target["id"]]

    assert first == second
    assert corpus == original
    assert [match.article_id for match in first] == [802, 803]
    assert all(isinstance(match, EnrichmentCompetitor) for match in first)
    assert all(not hasattr(match, "text") for match in first)


@pytest.mark.parametrize(
    ("keyword", "value"),
    [
        ("neighbor_radius", -1),
        ("max_competitors", -1),
        ("minimum_score", -0.01),
        ("minimum_score", 1.01),
    ],
)
def test_invalid_selector_bounds_fail_closed(keyword, value):
    target = _article(
        901,
        document_id=91,
        source_order=1,
        title="Контроль КИСА",
        text="КИСА осуществляет контроль систем.",
    )

    with pytest.raises(ValueError):
        select_enrichment_competitors(
            [target],
            [target],
            **{keyword: value},
        )


def _reference_similarity(
    target,
    candidate,
    *,
    idf,
    corpus_size,
):
    """Faithful pre-optimisation pair scorer used as a differential oracle."""

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
    if not (shared_anchors or shared_thresholds or rare_shared) and len(
        shared_tokens
    ) < 3:
        return None

    target_weights = {
        feature: weight
        for feature, weight in competitor_module._weighted_features(
            target,
            idf,
            corpus_size,
        ).items()
        if not feature.startswith("bigram:")
    }
    candidate_weights = {
        feature: weight
        for feature, weight in competitor_module._weighted_features(
            candidate,
            idf,
            corpus_size,
        ).items()
        if not feature.startswith("bigram:")
    }
    shared_features = set(target_weights) & set(candidate_weights)
    if not shared_features:
        return None

    dot_product = sum(
        target_weights[feature] * candidate_weights[feature]
        for feature in shared_features
    )
    target_norm = math.sqrt(
        sum(weight * weight for weight in target_weights.values())
    )
    candidate_norm = math.sqrt(
        sum(weight * weight for weight in candidate_weights.values())
    )
    if not target_norm or not candidate_norm:
        return None
    cosine = dot_product / (target_norm * candidate_norm)

    shared_mass = sum(
        min(target_weights[feature], candidate_weights[feature])
        for feature in shared_features
    )
    containment = shared_mass / min(
        sum(target_weights.values()),
        sum(candidate_weights.values()),
    )
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


def _reference_selector(
    targets,
    all_articles,
    *,
    neighbor_radius=competitor_module.DEFAULT_NEIGHBOR_RADIUS,
    max_competitors=competitor_module.DEFAULT_MAX_COMPETITORS,
    minimum_score=competitor_module.DEFAULT_MINIMUM_SCORE,
):
    """The original exhaustive selector, intentionally without shortcuts."""

    target_profiles = {}
    for article in targets:
        profile = competitor_module._profile(article)
        if profile.article_id in target_profiles:
            raise ValueError(
                f"duplicate target article id {profile.article_id}"
            )
        target_profiles[profile.article_id] = profile
    if not target_profiles or max_competitors == 0:
        return {article_id: () for article_id in target_profiles}

    corpus = competitor_module._corpus_profiles(all_articles)
    idf = competitor_module._idf_by_feature(corpus)
    excluded = competitor_module._local_neighbor_ids(
        target_profiles,
        corpus,
        neighbor_radius,
    )
    token_index = defaultdict(set)
    for candidate in corpus.values():
        for token in candidate.eligibility_tokens:
            token_index[token].add(candidate.article_id)

    results = {}
    for target_id, target in target_profiles.items():
        overlap_counts = Counter()
        for token in target.eligibility_tokens:
            overlap_counts.update(token_index.get(token, ()))

        matches = []
        for candidate_id, overlap_count in overlap_counts.items():
            if (
                overlap_count < 2
                or candidate_id in excluded[target_id]
            ):
                continue
            candidate = corpus[candidate_id]
            score = _reference_similarity(
                target,
                candidate,
                idf=idf,
                corpus_size=len(corpus),
            )
            if score is None or score < minimum_score:
                continue
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
            matches.append(
                EnrichmentCompetitor(
                    article_id=candidate_id,
                    score=round(score, 6),
                    reason=f"{relation}:{detail}",
                )
            )
        matches.sort(key=lambda match: (-match.score, match.article_id))
        results[target_id] = tuple(matches[:max_competitors])
    return results


def _alpha_marker(value: int) -> str:
    """Stable alphabetic marker: unlike digits it survives token filtering."""

    chars = []
    current = value
    while True:
        chars.append(chr(ord("a") + current % 26))
        current //= 26
        if current == 0:
            break
    return "marker" + "".join(reversed(chars))


def _random_corpus(seed: int, size: int = 47) -> list[dict]:
    rng = random.Random(seed)
    vocabulary = (
        "license control agency procedure inspection permit revoke "
        "hospital court search warrant regional federal finance budget "
        "evidence custody vehicle weapon medical public officer duty"
    ).split()
    rows = []
    for article_id in range(1, size + 1):
        words = rng.sample(vocabulary, rng.randint(6, 12))
        marker = _alpha_marker(article_id)
        synonyms = ()
        if article_id % 4 == 0:
            synonyms = (" ".join(rng.sample(words, 3)),)
        rows.append(
            _article(
                article_id,
                document_id=1 + article_id % 7,
                source_order=article_id // 7,
                title=" ".join([*words[:3], marker]),
                text=" ".join([*words, marker]),
                document=f"Code family {article_id % 3}",
                synonyms=synonyms,
            )
        )
    rng.shuffle(rows)
    return rows


@pytest.mark.parametrize("seed", range(12))
def test_optimized_selector_is_exactly_equal_to_exhaustive_reference(seed):
    corpus = _random_corpus(seed)
    rng = random.Random(seed + 1000)
    targets = rng.sample(corpus, 17)
    options = {
        "neighbor_radius": rng.randint(0, 4),
        "max_competitors": rng.randint(1, 6),
        "minimum_score": rng.choice((0.0, 0.22, 0.45, 0.7)),
    }

    expected = _reference_selector(targets, corpus, **options)
    actual = select_enrichment_competitors(targets, corpus, **options)

    assert actual == expected
    shuffled_corpus = list(corpus)
    shuffled_targets = list(targets)
    rng.shuffle(shuffled_corpus)
    rng.shuffle(shuffled_targets)
    assert (
        select_enrichment_competitors(
            shuffled_targets,
            shuffled_corpus,
            **options,
        )
        == expected
    )


@pytest.mark.parametrize("size", (100, 841, 2000))
def test_dense_repeated_semantics_has_constant_pair_scoring_work(
    monkeypatch,
    size,
):
    original_similarity = competitor_module._similarity_prepared
    original_nsmallest = competitor_module.heapq.nsmallest
    calls = 0
    ranking_limits = []

    def counted_similarity(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_similarity(*args, **kwargs)

    def counted_nsmallest(limit, *args, **kwargs):
        ranking_limits.append(limit)
        return original_nsmallest(limit, *args, **kwargs)

    monkeypatch.setattr(
        competitor_module,
        "_similarity_prepared",
        counted_similarity,
    )
    monkeypatch.setattr(
        competitor_module.heapq,
        "nsmallest",
        counted_nsmallest,
    )
    # The numeric suffix is deliberately non-semantic.  This models imported
    # catalogs whose boilerplate is repeated while article numbers differ.
    corpus = [
        _article(
            article_id,
            document_id=article_id,
            source_order=article_id,
            title=f"License control procedure marker{article_id}",
            text=(
                "Common unique order verification document "
                f"marker{article_id}"
            ),
            document="Administrative Code",
        )
        for article_id in range(1, size + 1)
    ]

    result = select_enrichment_competitors(corpus, corpus)

    assert len(result) == size
    assert all(len(matches) == 3 for matches in result.values())
    assert calls == 1
    assert ranking_limits == [4]
    if size == 100:
        assert result == _reference_selector(corpus, corpus)


def test_repeated_target_profile_preserves_per_target_neighbors_and_reason():
    shared = {
        "title": "License control procedure",
        "text": "Agency license control procedure evidence",
        "document": "Administrative Code",
    }
    first = _article(
        1,
        document_id=10,
        source_order=1,
        **shared,
    )
    first_buffer = _article(
        90,
        document_id=10,
        source_order=2,
        title="Unrelated medical service",
        text="Hospital treatment schedule",
    )
    same_document_far = _article(
        3,
        document_id=10,
        source_order=3,
        **shared,
    )
    second = _article(
        2,
        document_id=20,
        source_order=1,
        **shared,
    )
    second_local = _article(
        4,
        document_id=20,
        source_order=2,
        **shared,
    )
    corpus = [
        second_local,
        same_document_far,
        first_buffer,
        second,
        first,
    ]

    expected = _reference_selector(
        [first, second],
        corpus,
        neighbor_radius=1,
        max_competitors=3,
    )
    actual = select_enrichment_competitors(
        [first, second],
        corpus,
        neighbor_radius=1,
        max_competitors=3,
    )

    assert actual == expected
    assert any(
        match.article_id == 3
        and match.reason.startswith("far_same_document:")
        for match in actual[1]
    )
    assert all(match.article_id != 4 for match in actual[2])


def test_pair_score_cache_is_strictly_bounded_without_changing_results(
    monkeypatch,
):
    created_caches = []
    base_cache = competitor_module._BoundedSimilarityCache

    class RecordingCache(base_cache):
        def __init__(self, max_entries):
            super().__init__(max_entries)
            created_caches.append(self)

    monkeypatch.setattr(
        competitor_module,
        "MAX_SIMILARITY_CACHE_ENTRIES",
        7,
    )
    monkeypatch.setattr(
        competitor_module,
        "_BoundedSimilarityCache",
        RecordingCache,
    )
    corpus = [
        _article(
            article_id,
            document_id=article_id,
            source_order=article_id,
            title=(
                "License control procedure "
                + _alpha_marker(article_id)
            ),
            text=(
                "Common order verification document "
                + _alpha_marker(article_id)
            ),
        )
        for article_id in range(1, 51)
    ]

    expected = _reference_selector(corpus, corpus)
    actual = select_enrichment_competitors(corpus, corpus)

    assert actual == expected
    assert len(created_caches) == 1
    assert len(created_caches[0]) == 7
