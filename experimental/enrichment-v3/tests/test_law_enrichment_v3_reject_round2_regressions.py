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
    for unsafe in unsafe_outputs:
        try:
            validate(source, unsafe)
        except ValueError:
            continue
        unexpectedly_supported.append(unsafe)
    assert not unexpectedly_supported, (
        "source-unsupported outputs were accepted: "
        f"{unexpectedly_supported!r}"
    )


@pytest.mark.parametrize(
    "marker",
    (
        "за вычетом",
        "за минусом",
        "не считая",
        "без учёта",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_subtractive_ground_markers_share_one_exclusion_contract(
    marker: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия изымается за нарушение закона, "
        f"{marker} убийства."
    )
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за нарушение закона",
        "Лицензия изымается за нарушение закона, кроме мошенничества",
        "Лицензия изымается за убийство",
    )
    validate(
        source,
        "Лицензия изымается за нарушение закона, кроме убийства",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_subtractive_ground_cannot_be_added_or_lost_in_condition(
    validate: RelationValidator,
) -> None:
    plain = "Если нарушены правила, лицензия изымается."
    invented = (
        "Если нарушены правила, за вычетом убийства, "
        "лицензия изымается"
    )
    _assert_rejected(validate, plain, invented)

    source = invented + "."
    _assert_rejected(
        validate,
        source,
        "Если нарушены правила, лицензия изымается",
    )
    validate(
        "Если нарушены правила (за вычетом убийства), "
        "лицензия изымается.",
        "Если нарушены правила, кроме убийства, лицензия изымается",
    )


@pytest.mark.parametrize(
    "preposition",
    ("через", "вне", "внутри", "возле", "вдоль"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_oblique_territory_never_replaces_passage_patient(
    preposition: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Губернатор может закрыть проезд {preposition} "
        "территории тюрьмы."
    )
    validate(source, "Губернатор может закрыть проезд")
    _assert_rejected(
        validate,
        source,
        "Губернатор может закрыть территорию тюрьмы",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_passive_oblique_territory_keeps_passage_as_patient(
    validate: RelationValidator,
) -> None:
    source = "Проезд через территорию тюрьмы закрывается судом."
    validate(source, source.rstrip("."))
    _assert_rejected(validate, source, "Суд закрывает территорию тюрьмы")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_direct_territory_after_oblique_clause_remains_searchable(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может закрыть проезд через территорию, "
        "затем губернатор может закрыть саму территорию."
    )
    validate(source, "Губернатор может закрыть территорию")


@pytest.mark.parametrize(
    "operative_actor",
    (
        "Организация Центр",
        "Полиция Дельта",
        "FIB",
        "LSPD",
        "DEA Alpha",
    ),
    ids=(
        "organization",
        "russian-police",
        "known-ascii-fib",
        "known-ascii-lspd",
        "unknown-ascii-name",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_leading_permission_comma_list_preserves_real_actor(
    operative_actor: str,
    validate: RelationValidator,
) -> None:
    source = (
        "С разрешения суда Альфа, прокуратуры Бета и губернатора Гамма, "
        f"{operative_actor} может отозвать пропуск."
    )
    honest = (
        f"{operative_actor} с разрешения суда Альфа, прокуратуры Бета и "
        "губернатора Гамма может отозвать пропуск"
    )
    validate(source, honest)
    _assert_rejected(
        validate,
        source,
        f"{operative_actor} с разрешения суда Альфа может отозвать пропуск",
        f"{operative_actor} с разрешения суда Альфа и прокуратуры Бета "
        "может отозвать пропуск",
    )
