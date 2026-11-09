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


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    "source",
    (
        "Суд не вправе выдавать ордер.",
        "Суд не имеет права выдавать ордер.",
    ),
    ids=("not-entitled", "has-no-right"),
)
def test_v3_denied_permission_cannot_become_positive(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "Суд вправе выдавать ордер")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд не вправе выдавать ордер.",
            "Суд не имеет права выдавать ордер",
        ),
        (
            "Суд не имеет права выдавать ордер.",
            "Суд не вправе выдавать ордер",
        ),
    ),
    ids=("entitled-to-has-right", "has-right-to-entitled"),
)
def test_v3_equivalent_denied_permission_wording_is_supported(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Суд не вправе выдавать ордер.",
            "Суд вправе не выдавать ордер",
        ),
        (
            "Суд не вправе выдавать ордер.",
            "Суд может не выдавать ордер",
        ),
        (
            "Суд вправе не выдавать ордер.",
            "Суд не вправе выдавать ордер",
        ),
        (
            "Суд может не выдавать ордер.",
            "Суд не имеет права выдавать ордер",
        ),
    ),
    ids=(
        "denial-to-entitled-not",
        "denial-to-may-not",
        "entitled-not-to-denial",
        "may-not-to-denial",
    ),
)
def test_v3_denied_permission_is_not_optional_nonperformance(
    source: str,
    bad_output: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Суд вправе не выдавать ордер.",
            "Суд может не выдавать ордер",
        ),
        (
            "Суд может не выдавать ордер.",
            "Суд вправе не выдавать ордер",
        ),
    ),
    ids=("entitled-not-to-may-not", "may-not-to-entitled-not"),
)
def test_v3_optional_nonperformance_remains_optional(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_denied_permission_is_bounded_to_its_clause(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура не вправе выдавать ордер, а суд вправе выдавать "
        "лицензию."
    )
    validate(source, "Суд вправе выдавать лицензию")
    with pytest.raises(ValueError):
        validate(source, "Прокуратура вправе выдавать ордер")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_denied_permission_allows_only_bounded_neutral_modifiers(
    validate: RelationValidator,
) -> None:
    source = "Суд не вправе повторно выдавать ордер."
    validate(source, "Суд не имеет права повторно выдавать ордер")
    with pytest.raises(ValueError):
        validate(source, "Суд вправе повторно выдавать ордер")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("source", "bad_output"),
    (
        (
            "Суд не вправе не выдавать ордер.",
            "Суд не вправе выдавать ордер",
        ),
        (
            "Суд не вправе выдавать ордер.",
            "Суд не вправе не выдавать ордер",
        ),
    ),
    ids=("double-to-single", "single-to-double"),
)
def test_v3_double_negation_does_not_collapse_into_permission_denial(
    source: str,
    bad_output: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, bad_output)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_positive_permission_remains_supported(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд вправе выдавать ордер.",
        "Суд может выдавать ордер",
    )
