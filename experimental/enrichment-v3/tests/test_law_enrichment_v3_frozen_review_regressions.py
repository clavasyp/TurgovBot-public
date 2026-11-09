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


ACTOR_EXPRESSION_CASES = (
    (
        "FIB и (или) USSS отзывают пропуск.",
        "FIB отзывает пропуск",
    ),
    (
        "FIB и (или) USSS выдали лицензию, а затем отозвали пропуск.",
        "FIB отозвал пропуск",
    ),
)


PASSIVE_JOINT_CASES = (
    (
        "Территориальный пропуск совместно с прокуратурой "
        "отзывается судом.",
        "Суд отзывает территориальный пропуск",
    ),
    (
        "Суд отзывает пропуск совместно, с прокуратурой.",
        "Суд отзывает пропуск",
    ),
)


GENERAL_EXCLUSION_CASES = (
    (
        "Полиция, не включая LSPD, выдаёт лицензию.",
        "LSPD выдаёт лицензию",
    ),
    (
        "Полиция, кроме LSPD, может выдавать лицензию.",
        "Полиция может выдавать лицензию",
    ),
)


EXCLUDED_GROUND_CASES = tuple(
    (
        (
            "В случае нарушения уголовных статей, "
            f"{exception}, лицензия парамедика изымается."
        ),
        (
            "Лицензия парамедика изымается за нарушение "
            "уголовной статьи об убийстве"
        ),
    )
    for exception in (
        "кроме статьи об убийстве",
        "за исключением статьи об убийстве",
        "исключая статью об убийстве",
    )
)


YEAR_FIRST_DATE_CASES = (
    (
        "Суд 2026.05.01 может отозвать пропуск.",
        "Суд может отозвать пропуск",
    ),
    (
        "Суд 2026/05/01 может отозвать пропуск.",
        "Суд может отозвать пропуск",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    ACTOR_EXPRESSION_CASES,
    ids=("finite", "inherited-sequence"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_parenthesized_or_stays_atomic(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    PASSIVE_JOINT_CASES,
    ids=("between-patient-and-passive", "malformed-marker"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_joint_marker_never_reopens_one_actor(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    GENERAL_EXCLUSION_CASES,
    ids=("excluded-actor", "dropped-family-restriction"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_general_relation_preserves_actor_exclusion(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    EXCLUDED_GROUND_CASES,
    ids=("except", "with-exception", "excluding"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_excluded_ground_cannot_become_positive_ground(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    YEAR_FIRST_DATE_CASES,
    ids=("dot", "slash"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_year_first_date_cannot_be_dropped(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        *ACTOR_EXPRESSION_CASES,
        PASSIVE_JOINT_CASES[0],
        *GENERAL_EXCLUSION_CASES,
        *EXCLUDED_GROUND_CASES,
        *YEAR_FIRST_DATE_CASES,
    ),
)
def test_v3_new_grammar_keeps_source_projection_supported(
    source: str,
    output: str,
) -> None:
    del output
    supported = source.rstrip(".")
    _validate_title(source, supported)
    _validate_single_synonym(source, supported)


def test_v3_plain_joint_still_exposes_each_named_actor() -> None:
    source = "FIB и USSS отзывают пропуск."
    for validate in (_validate_title, _validate_single_synonym):
        validate(source, "FIB отзывает пропуск")
        validate(source, "USSS отзывает пропуск")
    validate_synonym_source_evidence(
        source,
        ["FIB отзывает пропуск", "USSS отзывает пропуск"],
    )


def test_v3_slash_actor_expression_remains_atomic() -> None:
    source = "FIB/USSS отзывают пропуск."
    for validate in OUTPUT_PATHS:
        with pytest.raises(ValueError):
            validate(source, "FIB отзывает пропуск")


def test_v3_well_formed_leading_passive_joint_remains_supported() -> None:
    source = "Совместно с прокуратурой пропуск отзывается судом."
    output = "Пропуск отзывается судом совместно с прокуратурой"
    _validate_title(source, output)
    _validate_single_synonym(source, output)


def test_v3_disjoint_roster_exclusion_keeps_nonexcluded_family() -> None:
    source = (
        "Лица, уполномоченные выдавать лицензию\r\n"
        "Право на выдачу лицензии имеют следующие лица:\r\n"
        "- Правоохранители, кроме прокуратуры"
    )
    for validate in (_validate_title, _validate_single_synonym):
        validate(source, "Полиция выдаёт лицензию")


def test_v3_positive_condition_ground_bridge_remains_supported() -> None:
    source = (
        "В случае нарушения уголовной статьи об убийстве "
        "лицензия парамедика изымается."
    )
    output = (
        "Лицензия парамедика изымается за нарушение уголовной "
        "статьи об убийстве"
    )
    _validate_title(source, output)
    _validate_single_synonym(source, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_detailed_condition_cannot_be_reduced_to_broad_violation(
    validate: RelationValidator,
) -> None:
    source = (
        "В случае нарушения уголовных статей, подразумевающих насилие в "
        "какой-либо форме: теракт, похищение, убийство - лицензия "
        "парамедика изымается."
    )
    honest = (
        "Лицензия парамедика изымается за нарушение уголовной статьи "
        "об убийстве"
    )
    unsafe = (
        "Лицензия парамедика изымается за нарушение уголовной статьи"
    )
    validate(source, honest)
    with pytest.raises(ValueError):
        validate(source, unsafe)


def test_v3_supported_day_first_date_stays_required() -> None:
    source = "Суд 01.05.2026 может отозвать пропуск."
    supported = source.rstrip(".")
    _validate_title(source, supported)
    _validate_single_synonym(source, supported)
    for validate in OUTPUT_PATHS:
        with pytest.raises(ValueError):
            validate(source, "Суд может отозвать пропуск")
