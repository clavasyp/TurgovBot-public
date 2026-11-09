"""Regressions for postfixed epistemic modality in the SOURCE (SH-8).

``_relation_is_assertive`` audited only the material *before* the action, so
a trailing ``, возможно`` parenthetical hedged the very same predicate and
still produced the identical claim digest as the categorical sentence.  A
generated surface could therefore drop the hedge and launder an uncertain
source statement into a hard legal fact.  The class had no regression at all
until this file.
"""

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
    validate_synonym_source_evidence(
        source,
        [source.rstrip("."), output],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)

HEDGE_MARKERS: tuple[str, ...] = (
    "возможно",
    "вероятно",
    "якобы",
    "по-видимому",
    "предположительно",
    "по слухам",
)

HEDGED_CLAUSES: tuple[tuple[str, str], ...] = (
    ("Суд аннулирует лицензию", "суд аннулирует лицензию"),
    ("Суд отзывает пропуск", "суд отзывает пропуск"),
    ("Суд пишет документ", "суд пишет документ"),
)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
@pytest.mark.parametrize("marker", HEDGE_MARKERS)
@pytest.mark.parametrize(
    ("clause", "categorical_output"),
    HEDGED_CLAUSES,
    ids=("licence", "pass", "document"),
)
def test_v3_trailing_epistemic_hedge_cannot_become_a_hard_fact(
    validate: RelationValidator,
    marker: str,
    clause: str,
    categorical_output: str,
) -> None:
    source = f"{clause}, {marker}."
    with pytest.raises(ValueError):
        validate(source, categorical_output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
@pytest.mark.parametrize("marker", HEDGE_MARKERS)
@pytest.mark.parametrize(
    ("clause", "categorical_output"),
    HEDGED_CLAUSES,
    ids=("licence", "pass", "document"),
)
def test_v3_trailing_epistemic_hedge_is_preserved_when_kept(
    validate: RelationValidator,
    marker: str,
    clause: str,
    categorical_output: str,
) -> None:
    source = f"{clause}, {marker}."
    validate(source, f"{categorical_output}, {marker}")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
@pytest.mark.parametrize(
    ("clause", "categorical_output"),
    HEDGED_CLAUSES,
    ids=("licence", "pass", "document"),
)
def test_v3_categorical_source_keeps_accepting_its_own_claim(
    validate: RelationValidator,
    clause: str,
    categorical_output: str,
) -> None:
    validate(f"{clause}.", categorical_output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
@pytest.mark.parametrize(
    ("clause", "categorical_output"),
    HEDGED_CLAUSES,
    ids=("licence", "pass", "document"),
)
def test_v3_trailing_object_is_not_mistaken_for_a_hedge(
    validate: RelationValidator,
    clause: str,
    categorical_output: str,
) -> None:
    # A comma-delimited tail that is not consumed by the epistemic marker
    # pattern must stay assertive, otherwise the narrow suffix fence would
    # turn every enumerated clause into an unproved statement.
    source = f"{clause}, а также ведёт реестр."
    validate(source, categorical_output)
