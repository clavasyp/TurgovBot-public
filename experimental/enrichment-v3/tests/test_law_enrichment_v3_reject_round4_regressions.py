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
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд объявил компанию Альфа закрытой.",
            "Суд объявил компанию Бета закрытой",
        ),
        (
            "Суд объявил закрытой компанию Альфа.",
            "Суд объявил закрытой компанию Бета",
        ),
        (
            "Компания Альфа объявлена закрытой судом.",
            "Компания Бета объявлена закрытой судом",
        ),
        (
            "Суд вправе объявить компанию Альфа закрытой.",
            "Суд вправе объявить компанию Бета закрытой",
        ),
        (
            "Суд признал счёт компании Альфа закрытым.",
            "Суд признал счёт компании Бета закрытым",
        ),
        (
            "Суд признал закрытым счёт компании Альфа.",
            "Суд признал дверь закрытой",
        ),
        (
            "Суд считает территорию порта Альфа закрытой.",
            "Суд считает территорию аэропорта Бета закрытой",
        ),
        (
            "Статья 18. Порядок прекращения деятельности. Организация "
            "обязана вести учёт документов. После проверки материалов суд "
            "объявил компанию Альфа закрытой. Решение публикуется в реестре "
            "и направляется заинтересованным лицам.",
            "Суд объявил компанию Бета закрытой",
        ),
    ),
    ids=(
        "active-name",
        "inverted-name",
        "passive-name",
        "modal-name",
        "owned-account-owner",
        "inverted-owned-account-head",
        "owned-territory",
        "sf-like-long-source",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_declarative_close_preserves_complete_patient_contract(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Суд объявил компанию Альфа закрытой.",
            "Суд объявил компанию Альфа закрытой",
        ),
        (
            "Суд объявил компанию Альфа закрытой.",
            "Суд объявил закрытой компанию Альфа",
        ),
        (
            "Суд объявил компанию Альфа закрытой.",
            "Компания Альфа объявлена закрытой судом",
        ),
        (
            "Компания Альфа объявлена закрытой судом.",
            "Суд объявил компанию Альфа закрытой",
        ),
        (
            "Суд объявил компанию Альфа закрытой.",
            "Суд объявил компанию закрытой",
        ),
        (
            "Суд признал счёт компании Альфа закрытым.",
            "Счёт компании Альфа признан закрытым судом",
        ),
        (
            "Суд считает территорию порта Альфа закрытой.",
            "Суд считает территорию закрытой",
        ),
    ),
    ids=(
        "same-surface",
        "active-to-inverted",
        "active-to-passive-declaration",
        "passive-to-active-declaration",
        "named-to-generic",
        "owned-account-voice",
        "owned-territory-to-generic",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_declarative_close_safe_paraphrases_remain_supported(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд закрыл допуск компании Альфа.",
            "Суд закрыл допуск компании Бета",
        ),
        (
            "Суд закрыл допуск.",
            "Суд закрыл допуск компании Бета",
        ),
        (
            "Суд закрыл допуск компании Альфа.",
            "Полиция закрыла допуск компании Альфа",
        ),
        (
            "Суд может закрыть допуск компании Альфа.",
            "Суд закрыл допуск компании Альфа",
        ),
        (
            "Суд не закрыл допуск компании Альфа.",
            "Суд закрыл допуск компании Альфа",
        ),
        (
            "Суд закрыл допуск на территорию порта Альфа.",
            "Суд закрыл допуск на территорию аэропорта Бета",
        ),
        (
            "Статья 19. Ограничение доступа. При наличии оснований и после "
            "изучения материалов суд может закрыть допуск компании Альфа на "
            "территорию порта Лос-Сантос. Уведомление направляется владельцу "
            "и хранится в реестре в течение установленного срока.",
            "Полиция закрыла допуск компании Бета на территорию аэропорта "
            "Сан-Фиерро",
        ),
    ),
    ids=(
        "owner-identity",
        "bare-to-owned",
        "operative-actor",
        "modality-upgrade",
        "polarity-inversion",
        "territory-scope",
        "sf-like-long-source",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_deferred_access_pass_close_keeps_relation_contract(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            "Суд закрыл допуск.",
            "Суд закрыл допуск",
        ),
        (
            "Суд закрыл допуск.",
            "Суд закрыл пропуск",
        ),
        (
            "Суд закрыл пропуск.",
            "Суд закрыл допуск",
        ),
        (
            "Суд закрыл допуск компании Альфа.",
            "Суд закрыл допуск компании Альфа",
        ),
        (
            "Суд закрыл допуск компании Альфа.",
            "Суд закрыл пропуск компании Альфа",
        ),
        (
            "Суд закрыл пропуск компании Альфа.",
            "Суд закрыл допуск организации Альфа",
        ),
        (
            "Суд закрыл допуск компании Альфа.",
            "Суд закрыл допуск",
        ),
    ),
    ids=(
        "bare-same-surface",
        "bare-dopusk-to-propusk",
        "bare-propusk-to-dopusk",
        "owned-same-surface",
        "owned-same-owner-alias",
        "owned-family-alias",
        "owned-to-bare",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_deferred_access_pass_safe_aliases_remain_supported(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
