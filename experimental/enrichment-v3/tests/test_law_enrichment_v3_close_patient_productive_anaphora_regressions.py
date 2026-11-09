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


COMPANY_SOURCE = "Суд признал компанию Альфа закрытой."
OBJECT_SOURCE = "Суд признал объект Альфа закрытым."


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            COMPANY_SOURCE,
            "Компания Бета упомянута, суд признал вышеобозначенную закрытой",
        ),
        (
            COMPANY_SOURCE,
            (
                "Компания Бета упомянута, суд признал вышеозначенную "
                "организацию закрытой"
            ),
        ),
        (
            COMPANY_SOURCE,
            (
                "Компания Бета упомянута, настоящая компания признана "
                "судом закрытой"
            ),
        ),
        (
            COMPANY_SOURCE,
            "Компания Бета упомянута, её суд признал закрытой",
        ),
        (
            COMPANY_SOURCE,
            "Компания Бета упомянута, ту суд признал закрытой",
        ),
        (
            OBJECT_SOURCE,
            "Объект Бета упомянут, тот объект суд признал закрытым",
        ),
        (
            COMPANY_SOURCE,
            (
                "Компания Бета упомянута, сия компания признана "
                "судом закрытой"
            ),
        ),
        (
            OBJECT_SOURCE,
            "Объект Бета упомянут, сей объект признан судом закрытым",
        ),
        (
            COMPANY_SOURCE,
            "Компания Бета упомянута, она является закрытой",
        ),
        (
            COMPANY_SOURCE,
            "Компания Бета упомянута, она остаётся закрытой",
        ),
        (
            COMPANY_SOURCE,
            "Компания Бета упомянута, суд закрыл её",
        ),
    ),
    ids=(
        "above-designated",
        "aforementioned",
        "present-company",
        "object-first-her",
        "object-first-that",
        "object-first-that-object",
        "this-archaic-company",
        "this-archaic-object",
        "pronoun-only-is",
        "pronoun-only-remains",
        "post-action-pronoun",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_productive_close_patient_anaphora_fails_closed_in_every_path(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            COMPANY_SOURCE,
            "Суд признал вышеобозначенную компанию Альфа закрытой",
        ),
        (
            COMPANY_SOURCE,
            "Настоящую компанию Альфа суд признал закрытой",
        ),
        (
            COMPANY_SOURCE,
            "Её компанию Альфа суд признал закрытой",
        ),
        (
            COMPANY_SOURCE,
            "Ту компанию Альфа суд признал закрытой",
        ),
        (
            OBJECT_SOURCE,
            "Сей объект Альфа суд признал закрытым",
        ),
        (
            "Она признала компанию Альфа закрытой.",
            "Она признала компанию Альфа закрытой",
        ),
        (
            (
                "Суд признал её доводы убедительными, а компанию Альфа "
                "закрытой."
            ),
            (
                "Суд признал её доводы убедительными, а компанию Альфа "
                "закрытой"
            ),
        ),
        (
            "После её проверки суд признал компанию Альфа закрытой.",
            "После её проверки суд признал компанию Альфа закрытой",
        ),
        (
            "Компания «Она» является закрытой.",
            "Компания «Она» является закрытой",
        ),
        (
            "Служащий рассказал посторонним закрытые данные.",
            "служащий рассказал посторонним закрытые данные",
        ),
        (
            "Суд признал данные закрытыми.",
            "Суд признал данные закрытыми",
        ),
        (
            "Доступ к закрытым данным запрещён.",
            "доступ к закрытым данным запрещён",
        ),
        (
            "Закрытая территория обладает особым статусом.",
            "что такое закрытая территория",
        ),
    ),
    ids=(
        "explicit-above-designated-name",
        "explicit-present-name",
        "explicit-possessed-name",
        "explicit-that-name",
        "explicit-archaic-name",
        "pronoun-is-active-actor",
        "pronoun-belongs-to-prior-object",
        "pronoun-belongs-to-context",
        "pronoun-is-quoted-identity",
        "closed-data-is-not-deictic",
        "data-is-not-demonstrative-adjective",
        "closed-data-oblique-case",
        "identificational-what-is-question",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_named_patients_and_unrelated_pronouns_remain_supported(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
