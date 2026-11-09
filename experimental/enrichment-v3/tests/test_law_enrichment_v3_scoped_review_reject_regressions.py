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
    validate_synonym_source_evidence(
        source,
        [source_surface, output]
        if output != source_surface
        else [source_surface],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)


@pytest.mark.parametrize(
    "source",
    (
        "Суд может вынести решение об аннулировании лицензии.",
        "Суд вправе вынести решение об аннулировании лицензии.",
        "Суд имеет право вынести решение об аннулировании лицензии.",
    ),
    ids=("may", "entitled", "has-right"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_optional_decision_cannot_become_definite_embedded_action(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "суд может аннулировать лицензию")
    with pytest.raises(ValueError):
        validate(source, "суд аннулирует лицензию")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_asserted_decision_still_proves_definite_embedded_action(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд вынес решение об аннулировании лицензии.",
        "суд аннулирует лицензию",
    )


@pytest.mark.parametrize(
    "source",
    (
        "Возможно, суд может вынести решение об аннулировании лицензии.",
        "Суд не может вынести решение об аннулировании лицензии.",
    ),
    ids=("nonassertive", "negative"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_decision_projection_keeps_assertiveness_and_polarity(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "суд может аннулировать лицензию")


@pytest.mark.parametrize(
    "modifier",
    ("обязательно", "абсолютно", "безусловно"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_anytime_scope_cannot_launder_negation_through_modifiers(
    modifier: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск №1 может быть отозван не "
        f"{modifier} в любое время."
    )
    with pytest.raises(ValueError):
        validate(source, "можно отозвать пропуск №1 без ожидания срока")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_positive_anytime_scope_remains_available(
    validate: RelationValidator,
) -> None:
    validate(
        "Пропуск №1 может быть отозван в любое время.",
        "можно отозвать пропуск №1 без ожидания срока",
    )
