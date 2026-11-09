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


def _validate_malformed_full_set(source: str, output: str) -> None:
    validate_synonym_source_evidence(
        source,
        ["Порядок работы организации", output],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)
MALFORMED_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_malformed_full_set,
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
    ("source", "honest", "unsafe"),
    (
        (
            "Счёт компании Альфа закрыл суд.",
            "Счёт компании Альфа был закрыт судом",
            "Дверь закрыл суд",
        ),
        (
            "Офис компании Альфа закрыл суд.",
            "Суд закрыл офис компании Альфа",
            "Магазин компании Альфа закрыл суд",
        ),
    ),
    ids=("owned-account", "owned-office"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_object_first_close_keeps_patient_and_actor_roles(
    source: str,
    honest: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "honest", "unsafe_outputs"),
    (
        (
            "Суд закрыл счёт.",
            "Счёт был закрыт судом",
            (
                "Суд закрыл счёт компании Бета",
                "Суд закрыл дверь",
            ),
        ),
        (
            "Суд закрыл порт.",
            "Порт был закрыт судом",
            (
                "Суд закрыл порт Лос-Сантос",
                "Суд закрыл аэропорт",
            ),
        ),
    ),
    ids=("account", "port"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_close_patient_cannot_gain_name_or_change_head(
    source: str,
    honest: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, *unsafe_outputs)


@pytest.mark.parametrize(
    ("source", "honest", "unsafe"),
    (
        (
            "Суд закрыл компанию Альфа.",
            "Закрытие компании Альфа судом",
            "Закрытие компании Бета судом",
        ),
        (
            "Суд закрыл счёт компании Альфа.",
            "Закрытие счёта компании Альфа судом",
            "Закрытие счёта компании Бета судом",
        ),
        (
            "Суд закрыл территорию порта Альфа.",
            "Закрытие территории порта Альфа судом",
            "Закрытие территории аэропорта Бета судом",
        ),
        (
            "Счёт компании Альфа был закрыт судом.",
            "Закрытие счёта компании Альфа судом",
            "Закрытие двери судом",
        ),
    ),
    ids=(
        "named-company",
        "owned-account",
        "owned-territory",
        "passive-owned-account",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_close_paraphrase_preserves_full_patient_contract(
    source: str,
    honest: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize("operator", ("&", "/", "+"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_patient_name_operator_is_identity_not_discardable_punctuation(
    operator: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд закрывает компанию Alpha {operator} Beta."
    honest = f"Компания Alpha {operator} Beta закрывается судом"
    validate(source, honest)
    _assert_rejected(
        validate,
        source,
        "Суд закрывает компанию Alpha Beta",
        f"Суд закрывает компанию Alpha {operator} Gamma",
        "Суд закрывает компанию Alpha / Beta"
        if operator != "/"
        else "Суд закрывает компанию Alpha + Beta",
    )


@pytest.mark.parametrize("operator", ("&", "/", "+"))
@pytest.mark.parametrize(
    "validate",
    MALFORMED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_dangling_patient_name_operator_fails_closed(
    operator: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд закрывает компанию Alpha {operator}."
    _assert_rejected(validate, source, "Суд закрывает компанию Alpha")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_external_location_can_be_dropped_without_changing_patient(
    validate: RelationValidator,
) -> None:
    source = "Суд закрыл компанию Альфа в городе Лос-Сантос."
    validate(source, "Суд закрыл компанию Альфа")
    _assert_rejected(validate, source, "Суд закрыл город Лос-Сантос")


@pytest.mark.parametrize(
    ("quoted_name", "unsafe"),
    (
        (
            "За убийство",
            "Суд закрывает компанию за убийство",
        ),
        (
            "По решению губернатора",
            "Суд по решению губернатора закрывает компанию",
        ),
        (
            "На территории округа",
            "Суд закрывает компанию на территории округа",
        ),
        (
            "Для борьбы с убийством",
            "Суд закрывает компанию для борьбы с убийством",
        ),
        (
            "Статья 5",
            "Суд закрывает компанию по статье 5",
        ),
        (
            "1 мая 2026 года",
            "Суд 1 мая 2026 года закрывает компанию",
        ),
    ),
    ids=("ground", "decision", "scope", "purpose", "reference", "date"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_quoted_patient_name_cannot_leak_into_relation_arguments(
    quoted_name: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд закрывает компанию «{quoted_name}»."
    validate(source, f"Компания «{quoted_name}» закрывается судом")
    validate(source, "Суд закрывает компанию")
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Лицензия изымается за любое нарушение, кроме того случая, "
            "когда нарушение устранено."
        ),
        (
            "Лицензия изымается за любое нарушение, кроме того нарушения, "
            "которое устранено."
        ),
    ),
    ids=("that-case", "that-violation"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_krome_togo_with_legal_payload_is_an_exclusion(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за любое нарушение",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_standalone_leading_ground_exclusion_moves_with_relation(
    validate: RelationValidator,
) -> None:
    source = (
        "За вычетом убийства, лицензия изымается за нарушение закона."
    )
    validate(
        source,
        "Лицензия изымается за нарушение закона, кроме убийства",
    )
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за нарушение закона",
        "Лицензия изымается за нарушение закона, включая убийство",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nested_exclusion_wrapper_is_semantically_invariant(
    validate: RelationValidator,
) -> None:
    source = (
        "Если нарушены правила ((за вычетом убийства)), "
        "лицензия изымается."
    )
    validate(
        source,
        "Если нарушены правила (кроме убийства), лицензия изымается",
    )
    validate(
        source,
        "Если нарушены правила, лицензия изымается, кроме убийства",
    )
    _assert_rejected(
        validate,
        source,
        "Если нарушены правила, лицензия изымается",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_krome_etogo_discourse_connector_is_not_an_exclusion(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия изымается за нарушение закона, кроме этого, "
        "гражданин читает закон."
    )
    validate(source, "Лицензия изымается за нарушение закона")
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за нарушение закона, кроме убийства",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ascii_passive_actor_is_not_parsed_as_close_patient(
    validate: RelationValidator,
) -> None:
    source = "Счёт компании Альфа закрыт FIB."
    validate(source, "FIB закрыл счёт компании Альфа")
    _assert_rejected(
        validate,
        source,
        "Дверь закрыта FIB",
        "Счёт компании Бета закрыт FIB",
    )
