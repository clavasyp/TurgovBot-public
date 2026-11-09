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


def _assert_rejected(
    validate: RelationValidator,
    source: str,
    *unsafe_outputs: str,
) -> None:
    unexpectedly_supported: list[str] = []
    for output in unsafe_outputs:
        try:
            validate(source, output)
        except ValueError:
            continue
        unexpectedly_supported.append(output)
    assert not unexpectedly_supported, (
        "source-unsupported access-pass outputs were accepted: "
        f"{unexpectedly_supported!r}"
    )


@pytest.mark.parametrize(
    "condition_intro",
    (
        "в случаях, когда",
        "при условии, что",
        "в том случае, если",
        "в ситуации, когда",
        "при обстоятельствах, когда",
    ),
    ids=(
        "cases-when",
        "provided-that",
        "that-case-if",
        "situation-when",
        "circumstances-when",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_finite_revocation_keeps_positive_access_pass_prerequisite(
    condition_intro: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом "
        f"{condition_intro} преступление было совершено с использованием "
        "такого пропуска."
    )

    validate(
        source,
        "Суд может аннулировать пропуск за преступление с использованием "
        "пропуска",
    )
    _assert_rejected(
        validate,
        source,
        "Суд может аннулировать пропуск",
    )


@pytest.mark.parametrize(
    "exclusion_scope",
    (
        "кроме прямо предусмотренных случаев",
        "кроме таких случаев",
        "за исключением отдельно предусмотренных случаев",
        "исключая прямо указанные случаи",
        "кроме случаев, прямо предусмотренных законом",
    ),
    ids=(
        "premodified-explicit",
        "demonstrative",
        "premodified-separate",
        "premodified-stated",
        "postmodified-by-law",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_modified_access_pass_case_exclusion_stays_negative(
    exclusion_scope: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом, "
        f"{exclusion_scope}, когда преступление было совершено с "
        "использованием такого пропуска."
    )

    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        (
            "Суд может аннулировать пропуск, когда преступление было "
            "совершено с использованием такого пропуска"
        ),
        (
            "Суд может аннулировать пропуск за преступление с "
            "использованием пропуска"
        ),
        "Суд может аннулировать пропуск",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_prerequisite_does_not_block_topical_nominal(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом при условии, что преступление "
        "было совершено с использованием такого пропуска."
    )

    validate(source, "аннулирование пропуска судом")
