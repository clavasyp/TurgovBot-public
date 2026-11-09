from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_relation_truths,
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
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд закрывает компанию Альфа.",
            "Суд закрывает компанию Бета",
        ),
        (
            "Компания Альфа закрыта судом.",
            "Компания Бета закрыта судом",
        ),
        (
            "Суд закроет банк Альфа.",
            "Суд закроет банк Бета",
        ),
        (
            "Компания Альфа закрывается по решению суда.",
            "Компания Бета закрывается по решению суда",
        ),
    ),
    ids=("active", "passive", "future", "reflexive"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_identity_is_preserved_in_every_voice(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд закрыл счёт компании Альфа.",
            "Суд закрыл компанию Альфа",
        ),
        (
            "Суд закрыл доступ банка Альфа.",
            "Суд закрыл банк Альфа",
        ),
        (
            "Суд закрыл филиал компании Альфа.",
            "Суд закрыл компанию Альфа",
        ),
        (
            "Суд закрыл компанию Альфа.",
            "Суд закрыл банк Альфа",
        ),
    ),
    ids=(
        "account-owner-is-not-company-patient",
        "access-owner-is-not-bank-patient",
        "branch-is-not-whole-company",
        "company-is-not-bank",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_head_is_not_replaced_by_owner_or_subtype(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд закрыл территорию порта Лос-Сантос.",
            "Суд закрыл территорию порта Сан-Фиерро",
        ),
        (
            "Суд закрыл северную территорию грузового порта Лос-Сантос.",
            "Суд закрыл южную территорию пассажирского порта Лос-Сантос",
        ),
        (
            "Суд закрыл сектор территории порта Лос-Сантос.",
            "Суд закрыл территорию порта Лос-Сантос",
        ),
        (
            "Суд закрыл зону территории порта Лос-Сантос.",
            "Суд закрыл сектор территории порта Лос-Сантос",
        ),
        (
            "Суд закрыл участок территории порта Лос-Сантос.",
            "Суд закрыл территорию порта Лос-Сантос",
        ),
        (
            "Суд закрыл компанию в северном секторе территории "
            "порта Лос-Сантос.",
            "Суд закрыл территорию порта Лос-Сантос",
        ),
        (
            "Суд закрыл доступ на территорию порта Лос-Сантос.",
            "Суд закрыл территорию порта Лос-Сантос",
        ),
    ),
    ids=(
        "port-name",
        "territory-modifiers",
        "sector-to-whole",
        "zone-to-sector",
        "plot-to-whole",
        "oblique-sector-after-company",
        "oblique-territory-after-access",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_territory_identity_scope_and_oblique_role_are_preserved(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд закрыл компанию «Альфа.",
            "Суд закрыл компанию Альфа",
        ),
        (
            "Суд закрыл компанию " + ("Alpha " * 150) + "Omega.",
            "Суд закрыл компанию " + ("Alpha " * 150) + "Sigma",
        ),
    ),
    ids=("malformed-name", "over-window-name"),
)
@pytest.mark.parametrize(
    "validate",
    MALFORMED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_malformed_or_overflow_identity_fails_closed(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Суд всегда закрывает организацию.",
            "Суд всегда закрывает предприятие",
        ),
        (
            "Суд закрыл компанию Альфа.",
            "Суд закрыл организацию",
        ),
        (
            "Суд закрыл банк Альфа.",
            "Суд закрыл организацию",
        ),
        (
            "Губернатор может временно закрыть любую территорию.",
            "Губернатор может временно закрыть территорию",
        ),
        (
            "Суд закрывает компанию Альфа.",
            "Компания Альфа закрывается судом",
        ),
        (
            "Компания Альфа закрывается судом.",
            "Суд закрывает компанию Альфа",
        ),
        (
            "Организацию всегда закрывает суд.",
            "Суд всегда закрывает организацию",
        ),
        (
            "Суд обязан закрыть и опечатать организацию.",
            "Суд обязан закрыть организацию",
        ),
        (
            "Судом закрыта компания Альфа.",
            "Судом закрыта компания Альфа",
        ),
    ),
    ids=(
        "generic-organization-alias",
        "named-company-to-generic",
        "named-bank-to-generic",
        "neutral-territory-determiner",
        "active-to-passive",
        "passive-to-active",
        "inverted-to-direct",
        "shared-object",
        "actor-first-passive",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_safe_aliases_and_voice_paraphrases_remain_supported(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize(
    "surface",
    (
        "Гражданин закрыл глаза.",
        "Гражданин закрыл дверь.",
        "Суд закрыл вопрос.",
        "Суд закрыл счёт компании Альфа.",
        "Суд закрыл доступ на территорию порта Лос-Сантос.",
    ),
)
def test_ordinary_close_idioms_do_not_become_regulated_patients(
    surface: str,
) -> None:
    assert not any(
        relation.predicate == "закры"
        and relation.direct_object in {"organization", "territory"}
        for relation in _extract_relation_truths(surface)
    )


def test_actor_first_passive_recovers_named_company_patient() -> None:
    relations = _extract_relation_truths("Судом закрыта компания Альфа")
    assert any(
        relation.predicate == "закры"
        and relation.direct_object == "organization"
        and relation.operative_actor == "суд"
        and any(
            argument.role == "patient_identity"
            and argument.value == "organization:альф"
            for argument in relation.arguments
        )
        for relation in relations
    )


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд закрыл счёт компании Альфа.",
            "Суд закрыл счёт компании Бета",
        ),
        (
            "Суд закрыл банковский счёт компании Альфа.",
            "Суд закрыл компанию",
        ),
        (
            "Суд закрыл дело организации Альфа.",
            "Суд закрыл дело организации Бета",
        ),
        (
            "Суд закрыл доступ компании Альфа.",
            "Суд закрыл доступ компании Бета",
        ),
        (
            "Суд закрыл офис компании Альфа.",
            "Суд закрыл офис компании Бета",
        ),
        (
            "Суд закрыл магазин фирмы Альфа.",
            "Суд закрыл магазин фирмы Бета",
        ),
        (
            "Суд закрыл счёт банка Альфа.",
            "Суд закрыл счёт банка Бета",
        ),
        (
            "Суд закрыл почту филиала Альфа.",
            "Суд закрыл почту филиала Бета",
        ),
        (
            "Счёт компании Альфа закрыт судом.",
            "Дверь закрыта судом",
        ),
        (
            "Счёт компании Альфа закрыт судом.",
            "Суд закрыт судом",
        ),
        (
            "Суд закрыл территорию порта Лос-Сантос.",
            "Суд закрыл территорию аэропорта Лос-Сантос",
        ),
        (
            "Суд закрыл территорию порта Лос-Сантос.",
            "Суд закрыл территорию вокзала Лос-Сантос",
        ),
        (
            "Суд закрыл порт Лос-Сантос.",
            "Суд закрыл порт Сан-Фиерро",
        ),
        (
            "Суд закрыл аэропорт Лос-Сантос.",
            "Суд закрыл аэропорт Сан-Фиерро",
        ),
        (
            "Суд закрыл компанию alpha.",
            "Суд закрыл компанию beta",
        ),
        (
            "Суд закрыл компанию Центр по работе с гражданами Альфа.",
            "Суд закрыл компанию Центр по работе с гражданами Бета",
        ),
        (
            "Суд закрыл компанию имени Альфа.",
            "Суд закрыл компанию имени Бета",
        ),
        (
            "Суд закрыл компанию Альфа или Бета.",
            "Суд закрыл компанию Альфа",
        ),
    ),
    ids=(
        "account-owner-name",
        "adjective-led-account-does-not-promote-owner",
        "case-owner-name",
        "access-owner-name",
        "office-owner-name",
        "shop-owner-name",
        "bank-account-owner-name",
        "branch-mail-owner-name",
        "passive-account-is-not-door",
        "passive-actor-is-not-patient",
        "port-owner-is-not-airport-owner",
        "port-owner-is-not-station-owner",
        "direct-port-name",
        "direct-airport-name",
        "lowercase-name",
        "connector-name",
        "name-introduced-by-imeni",
        "alternative-name-cannot-be-narrowed",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_open_head_owner_and_full_identity_are_exact(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд закрыл компанию Альфа и.",
            "Суд закрыл компанию Альфа",
        ),
        (
            "Суд закрыл «компанию Альфа.",
            "Суд закрыл компанию Альфа",
        ),
        (
            "Суд закрыл компанию Альфа».",
            "Суд закрыл компанию Альфа",
        ),
        (
            "Суд закрыл компанию " + ("alpha " * 150) + "omega.",
            "Суд закрыл компанию " + ("alpha " * 150) + "sigma",
        ),
    ),
    ids=(
        "dangling-connector",
        "malformed-opener-before-head",
        "stray-closer-after-head",
        "lowercase-overflow",
    ),
)
@pytest.mark.parametrize(
    "validate",
    MALFORMED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_incomplete_identity_fails_closed_without_truncation(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Суд закрыл счёт компании Альфа.",
            "Счёт компании Альфа закрыт судом",
        ),
        (
            "Счёт компании Альфа закрыт судом.",
            "Суд закрыл счёт компании Альфа",
        ),
        (
            "Судом закрыт счёт компании Альфа.",
            "Суд закрыл счёт компании Альфа",
        ),
        (
            "Суд закрыл счёт компании Альфа.",
            "Суд закрыл счёт организации Альфа",
        ),
        (
            "Суд закрыл счёт компании Альфа.",
            "Суд закрыл счёт",
        ),
        (
            "Суд закрыл порт Лос-Сантос.",
            "Суд закрыл порт",
        ),
    ),
    ids=(
        "owned-account-active-to-passive",
        "owned-account-passive-to-active",
        "owned-account-actor-first-passive",
        "organization-owner-alias",
        "owned-account-to-same-generic-head",
        "named-port-to-generic-port",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_safe_owner_and_voice_generalizations_remain_supported(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


def test_passive_owned_account_keeps_patient_and_actor_roles_separate() -> None:
    relations = _extract_relation_truths(
        "Счёт компании Альфа закрыт судом."
    )
    assert any(
        relation.predicate == "закры"
        and relation.direct_object == "other:счет"
        and relation.operative_actor == "суд"
        and any(
            argument.role == "patient_owner_identity"
            and argument.value.endswith(":organization альф")
            for argument in relation.arguments
        )
        for relation in relations
    )
    assert not any(
        relation.predicate == "закры"
        and relation.direct_object == "other:суд"
        for relation in relations
    )


def test_territory_owner_path_is_not_silently_dropped() -> None:
    relations = _extract_relation_truths(
        "Суд закрыл территорию порта Лос-Сантос."
    )
    assert any(
        relation.predicate == "закры"
        and relation.direct_object == "territory"
        and any(
            argument.role == "patient_owner"
            and argument.value == "port"
            for argument in relation.arguments
        )
        for relation in relations
    )
