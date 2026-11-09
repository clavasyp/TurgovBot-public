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
    ("source", "unsafe"),
    (
        (
            "Суд закрыл компанию Альфа за убийство.",
            "Суд закрыл организацию",
        ),
        (
            "Суд закрыл счёт компании Альфа за убийство.",
            "Суд закрыл счёт",
        ),
        (
            "Суд закрыл компанию Альфа по решению губернатора.",
            "Суд закрыл организацию",
        ),
        (
            "Суд закрыл компанию Альфа 1 мая 2026 года.",
            "Суд закрыл организацию",
        ),
        (
            "Суд закрыл компанию Альфа по статье 5.",
            "Суд закрыл организацию",
        ),
        (
            "Суд закрыл компанию Альфа без разрешения суда.",
            "Суд закрыл организацию",
        ),
        (
            "Суд закрыл компанию Альфа при нарушении закона.",
            "Суд закрыл организацию",
        ),
    ),
    ids=(
        "direct-ground",
        "owned-ground",
        "decision",
        "calendar-date",
        "legal-reference",
        "negative-permission",
        "condition",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_complete_external_facet_after_close_patient_cannot_be_absorbed(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "honest", "unsafe"),
    (
        (
            "Суд закрыл компанию Центр по разрешению споров.",
            "Компания Центр по разрешению споров закрыта судом",
            "Суд закрыл компанию Центр по разрешению лицензий",
        ),
        (
            "Суд закрыл счёт компании Центр по разрешению споров.",
            "Счёт компании Центр по разрешению споров закрыт судом",
            "Суд закрыл счёт компании Центр по разрешению лицензий",
        ),
        (
            "Суд закрыл компанию Центр по решению задач.",
            "Компания Центр по решению задач закрыта судом",
            "Суд закрыл компанию Центр по решению споров",
        ),
        (
            "Суд закрыл компанию Дом в Саду.",
            "Компания Дом в Саду закрыта судом",
            "Суд закрыл компанию Дом в Поле",
        ),
    ),
    ids=(
        "permission-looking-name",
        "owned-permission-looking-name",
        "decision-looking-name",
        "locative-connector-name",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_unquoted_connector_name_is_not_cut_as_external_adjunct(
    source: str,
    honest: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Компания Альфа закрыта FIB.",
            "FIB закрыта компанией Альфа",
        ),
        (
            "Компания Альфа закрыла суд Бета.",
            "Суд Бета закрыл компанию Альфа",
        ),
        (
            "Предприятие Альфа закрыло суд Бета.",
            "Суд Бета закрыл предприятие Альфа",
        ),
        (
            "FIB закрыла организация Альфа.",
            "Организация Альфа закрыта FIB",
        ),
        (
            "Суд закрыт FIB.",
            "FIB закрыт судом",
        ),
    ),
    ids=(
        "passive-ascii-prefix",
        "active-company-actor",
        "active-enterprise-actor",
        "object-first-ascii-patient",
        "bare-institutions",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_actor_and_patient_cannot_be_reversed(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "honest"),
    (
        (
            "Компанию Альфа закрыл суд Бета.",
            "Суд Бета закрыл компанию Альфа",
        ),
        (
            "Организация Альфа закрыла суд Бета.",
            "Суд Бета был закрыт организацией Альфа",
        ),
        (
            "Компания Альфа закрыла суд Бета.",
            "Суд Бета был закрыт компанией Альфа",
        ),
        (
            "FIB закрыла организация Альфа.",
            "FIB была закрыта организацией Альфа",
        ),
    ),
    ids=(
        "accusative-object-first",
        "literal-organization-actor",
        "organization-alias-actor",
        "ascii-object-first",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_case_and_verb_agreement_honest_voice_controls(
    source: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)


@pytest.mark.parametrize("operator", ("|", "\\", "*", "@", "="))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_unquoted_patient_identity_symbol_residue_is_not_discarded(
    operator: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд закрыл компанию Alpha {operator} Beta."
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "Суд закрыл компанию Alpha Beta",
    )


@pytest.mark.parametrize("operator", ("&", "/", "+", "#", "|"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_quoted_patient_identity_operator_is_not_discarded(
    operator: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд закрыл компанию «Alpha {operator} Beta»."
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "Суд закрыл компанию «Alpha Beta»",
    )


@pytest.mark.parametrize(
    ("source_operator", "output_operator"),
    (("&", "+"), ("/", "&"), ("|", "=")),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_quoted_owner_identity_operator_cannot_be_substituted(
    source_operator: str,
    output_operator: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Суд закрыл счёт компании "
        f"«Alpha {source_operator} Beta»."
    )
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "Суд закрыл счёт компании "
        f"«Alpha {output_operator} Beta»",
    )


@pytest.mark.parametrize(
    ("source", "honest", "unsafe"),
    (
        (
            "Суд объявил компанию Альфа закрытой.",
            "Закрытие компании Альфа судом",
            "Закрытие компании Бета судом",
        ),
        (
            "Суд объявил счёт компании Альфа закрытым.",
            "Закрытие счёта компании Альфа судом",
            "Закрытие счёта компании Бета судом",
        ),
        (
            "Суд признал компанию Альфа закрытой.",
            "Суд признал компанию Альфа закрытой",
            "Суд признал компанию Бета закрытой",
        ),
    ),
    ids=("company", "owned-account", "declarative-to-declarative"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_declarative_close_keeps_exact_patient_contract(
    source: str,
    honest: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, unsafe)
