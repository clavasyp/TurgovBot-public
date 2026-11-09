from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
    validate_synonym_source_evidence,
)


RelationValidator = Callable[[str, str], None]


def _validate_title(source: str, output: str) -> None:
    validate_enrichment_title_source_evidence(
        source,
        output,
        source_title=None,
    )


def _validate_single_synonym(source: str, output: str) -> None:
    validate_synonym_source_evidence(source, [output])


def _validate_full_set(source: str, output: str) -> None:
    source_surface = source.rstrip(".")
    synonyms = [source_surface]
    if output != source_surface:
        synonyms.append(output)
    validate_synonym_source_evidence(source, synonyms)


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Если нарушен закон (кроме убийства), суд может отозвать "
            "лицензию.",
            "Отзыв лицензии судом за нарушение закона, кроме убийства",
        ),
        (
            "Если нарушен закон (кроме убийства), суд Альфа может отозвать "
            "лицензию.",
            "Отзыв лицензии судом Альфа за нарушение закона, кроме убийства",
        ),
    ),
    ids=("exact-ground-and-exclusion", "exact-named-actor"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_formula_bridge_accepts_only_complete_source_formula(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Если нарушен закон (кроме убийства), суд может отозвать "
            "лицензию.",
            "Обязательный отзыв лицензии судом за нарушение закона, кроме "
            "убийства",
        ),
        (
            "Если нарушен закон (кроме убийства), суд Альфа может отозвать "
            "лицензию.",
            "Отзыв лицензии судом Бета за нарушение закона, кроме убийства",
        ),
        (
            "Если нарушен закон (кроме убийства), суд может отозвать "
            "лицензию.",
            "Отзыв лицензии судом за нарушение закона",
        ),
        (
            "Если нарушен закон (кроме убийства), суд может отозвать "
            "лицензию.",
            "Отзыв лицензии судом за нарушение закона, кроме кражи",
        ),
        (
            "Если нарушен закон о хранении опасных материалов (кроме "
            "убийства), суд может отозвать лицензию.",
            "Отзыв лицензии судом за нарушение закона, кроме убийства",
        ),
        (
            "Если нарушен закон и получено решение суда (кроме убийства), "
            "суд может отозвать лицензию.",
            "Отзыв лицензии судом за нарушение закона, кроме убийства",
        ),
        (
            "Если закон не нарушен (кроме убийства), суд может отозвать "
            "лицензию.",
            "Отзыв лицензии судом за нарушение закона, кроме убийства",
        ),
    ),
    ids=(
        "may-to-mandatory",
        "changed-named-actor",
        "dropped-exclusion",
        "changed-exclusion",
        "shortened-open-vocabulary-ground",
        "dropped-conjunctive-prerequisite",
        "negated-condition-to-positive-ground",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_formula_bridge_rejects_changed_or_dropped_semantics(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Пропуск отозван судом Альфа.",
            "Отзыв пропуска судом Бета",
        ),
        (
            "Пропуск отозван судом совместно с прокуратурой.",
            "Отзыв пропуска судом",
        ),
    ),
    ids=("changed-topical-identity", "drops-joint-actor"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_topical_nominal_keeps_exact_actor_identity_and_jointness(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)
