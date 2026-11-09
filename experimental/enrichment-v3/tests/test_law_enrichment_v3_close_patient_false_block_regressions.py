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


def _validate_safe_anaphoric_full_set(source: str, output: str) -> None:
    validate_synonym_source_evidence(
        source,
        [output, "незаконное нахождение на объекте"],
    )


ANAPHORIC_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_safe_anaphoric_full_set,
)


@pytest.mark.parametrize(
    ("source", "supported"),
    (
        (
            (
                "Закрытое судебное разбирательство допускается по "
                "определению судьи. Посещение таких закрытых судебных "
                "заседаний зрителями возможно только с разрешения судьи."
            ),
            "закрытый суд по определению судьи",
        ),
        (
            (
                "Сотрудники CID и прокуратуры имеют право посещать "
                "территории КПЗ в рамках надзора."
            ),
            "Закрытые территории: надзор CID и прокуратуры в КПЗ",
        ),
        (
            "Сокрытие личности в запрещённых для этого местах.",
            "гражданин закрыл лицо там где нельзя",
        ),
        (
            (
                "Закрытое судебное разбирательство допускается, когда "
                "публичность может унизить честь и достоинство участников."
            ),
            "закрыть процесс ради чести и достоинства",
        ),
        (
            (
                "В любой момент указом губернатора особая территория может "
                "быть признана закрытой для посещения."
            ),
            "Закрытие особой территории губернатором",
        ),
        (
            "Компания Альфа была закрыта судом.",
            "решение суда о закрытии компании Альфа",
        ),
        (
            "Суд признал компанию Альфа закрытой.",
            "Суд признал данную компанию Альфа закрытой",
        ),
        (
            "Суд закрыл компанию Альфа в указанную дату.",
            "Суд закрыл компанию Альфа в указанную дату",
        ),
        (
            (
                "Суд признал данную жалобу обоснованной, а компанию "
                "Альфа закрытой."
            ),
            (
                "Суд признал данную жалобу обоснованной, а компанию "
                "Альфа закрытой"
            ),
        ),
        (
            (
                "Компания «Суд признал данную закрытой» была закрыта "
                "прокуратурой."
            ),
            (
                "Компания «Суд признал данную закрытой» была закрыта "
                "прокуратурой"
            ),
        ),
    ),
    ids=(
        "attributive-closed-court",
        "closed-territories-heading",
        "ordinary-covered-face",
        "close-process-purpose",
        "passive-declarative-territory",
        "explicit-nominal-close-patient",
        "explicit-deictic-named-patient",
        "deictic-outside-patient-slot",
        "deictic-on-prior-coordinated-object",
        "legal-words-inside-quoted-identity",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_close_patient_contextual_surfaces_remain_supported(
    source: str,
    supported: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Суд закрыл компанию Бета",
        "Закрытая компания Бета",
        "Компания Бета, закрытая судом",
        "Закрытая судом компания Бета",
    ),
    ids=(
        "finite",
        "leading-attributive",
        "postposed-attributive",
        "interposed-attributive",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_genuine_named_close_patient_cannot_change_alpha_to_beta(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = "Суд закрыл компанию Альфа."
    with pytest.raises(ValueError):
        validate(source, unsafe)


ANAPHORIC_SOURCE = (
    "Незаконное нахождение на объекте, обладающем особым статусом, "
    "если таковой был закрыт для посещения уполномоченным лицом."
)


@pytest.mark.parametrize(
    "validate",
    ANAPHORIC_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_unresolved_source_anaphora_keeps_a_safe_topical_search_surface(
    validate: RelationValidator,
) -> None:
    validate(ANAPHORIC_SOURCE, "нахождение на объекте особого статуса")


@pytest.mark.parametrize(
    "unsafe",
    (
        "объект особого статуса закрыт уполномоченным лицом",
        "объект особого статуса закрыт губернатором",
        "объект особого статуса закрыт полицией",
        "объект особого статуса закрыт гражданином",
        "объект секретного статуса закрыт уполномоченным лицом",
    ),
    ids=(
        "conditional-fact-exposure",
        "changed-governor-actor",
        "changed-police-actor",
        "changed-citizen-actor",
        "changed-patient-descriptor",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_unresolved_source_anaphora_cannot_become_a_close_fact(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(ANAPHORIC_SOURCE, unsafe)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Таковая была закрыта судом",
        "Компания Бета осмотрена, затем таковая была закрыта судом",
        "Компания Бета работает, затем таковая может быть закрыта судом",
        "Компания Бета работает, затем таковая должна быть закрыта судом",
        "Компания Бета работает, затем таковая также была закрыта судом",
        "Компания Бета работает, затем таковая была немедленно закрыта судом",
        "Компания Бета работает, затем таковая не была закрыта судом",
        "Компания Бета работает, затем таковая не закрывается судом",
        "Компания Бета осмотрена, затем таковой был закрыт судом",
        "Компания Бета осмотрена, затем таковая был закрыта судом",
        "Компания Бета осмотрена, затем таковая была закрыт судом",
        "Судом была закрыта таковая",
        "Суд закрыл таковую",
        "Суд может закрыть таковую",
        "Суд закрыл именно таковую",
        "Документ «Компания Бета» описан, затем таковая была закрыта судом",
        "12.7 Компания Бета осмотрена 12.8, затем таковая была закрыта судом",
        "Фабрика Бета осмотрена, затем таковая была закрыта судом",
        "Суд вынес решение о закрытии таковой",
        "Решение суда о закрытии таковой",
        "Суд вынес решение о закрытии «таковой»",
        "Компания Бета упомянута, суд признал данную закрытой",
        "Компания Бета упомянута, суд признал «данную» закрытой",
        "Компания Бета упомянута, суд признал эту компанию закрытой",
        "Компания Бета упомянута, суд признал указанную закрытой",
        "Компания Бета упомянута, суд признал вышеуказанную закрытой",
        "Компания Бета упомянута, суд признал названную закрытой",
        "Компания Бета упомянута, суд признал упомянутую закрытой",
        "Компания Бета упомянута, суд признал последнюю закрытой",
    ),
    ids=(
        "dropped-antecedent",
        "changed-antecedent",
        "modal-may",
        "modal-mandatory",
        "focus-before-auxiliary",
        "focus-after-auxiliary",
        "negative-past",
        "negative-present",
        "wrong-pronoun-gender",
        "wrong-auxiliary-gender",
        "wrong-action-gender",
        "postposed-passive-anaphor",
        "postposed-active-anaphor",
        "postposed-modal-anaphor",
        "postposed-focused-anaphor",
        "quoted-head",
        "flattened-section",
        "unsupported-head",
        "nominal-decision",
        "nominal-label",
        "quoted-nominal-label",
        "standalone-given",
        "quoted-standalone-given",
        "this-generic-company",
        "standalone-indicated",
        "standalone-above-indicated",
        "standalone-named",
        "standalone-mentioned",
        "standalone-last",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_generated_close_anaphora_is_always_fail_closed(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = "Компания Альфа была закрыта судом."
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Компания Бета упомянута, суд признал данную закрытой",
        "Компания Бета упомянута, суд признал эту закрытой",
        "Компания Бета упомянута, суд признал эту компанию закрытой",
        "Компания Бета упомянута, суд признал соответствующую закрытой",
        "Компания Бета упомянута, суд признал описанную закрытой",
        "Компания Бета упомянута, суд признал рассматриваемую закрытой",
        "Компания Бета упомянута, суд признал предыдущую закрытой",
        "Компания Бета упомянута, суд признал следующую закрытой",
        (
            "Компания Бета упомянута, суд признал данную, ранее "
            "упомянутую, закрытой"
        ),
        (
            "Компания Бета упомянута, суд признал данную компанию, ранее "
            "упомянутую, закрытой"
        ),
        "Компания Бета упомянута, суд признал данную компанию как закрытую",
        "Компания Бета упомянута, суд объявил вышеописанную организацию закрытой",
        "Компания Бета упомянута, суд объявил обозначенную организацию закрытой",
    ),
    ids=(
        "given",
        "this",
        "this-company",
        "corresponding",
        "described",
        "considered",
        "previous",
        "next",
        "comma-given",
        "comma-given-company",
        "given-company-as-closed",
        "above-described-organization",
        "designated-organization",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_active_close_patient_deictic_cannot_borrow_an_antecedent(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = "Суд признал компанию Альфа закрытой."
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Компания Бета упомянута, данная признана судом закрытой",
        "Компания Бета упомянута, данная компания признана судом закрытой",
        "Компания Бета упомянута, судом данная признана закрытой",
        "Компания Бета упомянута, данную суд признал закрытой",
        "Компания Бета упомянута, данную компанию суд признал закрытой",
        "Компания Бета упомянута, эту организацию суд объявил закрытой",
        "Компания Бета упомянута, данная является закрытой",
        "Компания Бета упомянута, данная остаётся закрытой",
        "Компания Бета упомянута, данная считается навсегда закрытой",
    ),
    ids=(
        "passive-bare",
        "passive-explicit-head",
        "passive-actor-first",
        "object-first-bare",
        "object-first-explicit-head",
        "object-first-this-organization",
        "copular-is",
        "copular-remains",
        "passive-adverb",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_predicative_close_patient_deictic_cannot_borrow_an_antecedent(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = "Суд признал компанию Альфа закрытой."
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Компания Альфа признана судом закрытой.",
            "Компания Альфа признана судом закрытой",
        ),
        (
            "Суд признал компанию Альфа закрытой.",
            "Данную компанию Альфа суд признал закрытой",
        ),
        (
            "Суд признал компанию Альфа закрытой.",
            "Данный суд признал компанию Альфа закрытой",
        ),
        (
            "Компания «Данная» является закрытой.",
            "Компания «Данная» является закрытой",
        ),
    ),
    ids=(
        "passive-explicit-patient",
        "object-first-explicit-identity",
        "deictic-modifies-active-actor",
        "quoted-proper-name-copula",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_predicative_close_patient_explicit_controls_remain_supported(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Компания «Таковая» была закрыта судом.",
            "Решение суда о закрытии компании «Таковая»",
        ),
        (
            "Компания «Данная» была закрыта судом.",
            "Суд признал компанию «Данная» закрытой",
        ),
    ),
    ids=("proper-name-takovaya", "proper-name-dannaya"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_deictic_lexeme_inside_exact_patient_identity_is_not_anaphora(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


RESTRICTED_PUBLIC_PLACE_SOURCE = (
    "Даже на объектах с ограниченным доступом, таких как закрытые "
    "территории или режимные объекты, действуют правила поведения. "
    "За их нарушение ответственность наступае по тем же нормам, "
    "что и для общественных мест."
)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_restricted_place_search_keeps_the_source_object_family(
    validate: RelationValidator,
) -> None:
    validate(
        RESTRICTED_PUBLIC_PLACE_SOURCE,
        "ответственность на режимном объекте как в общественном месте",
    )
    with pytest.raises(ValueError):
        validate(
            RESTRICTED_PUBLIC_PLACE_SOURCE,
            "ответственность на закрытом объекте как в общественном месте",
        )


DETAINED_OFFICIAL_ACCESS_SOURCE = (
    "Если задержан государственный служащий, начальство сотрудника "
    "имеет право пройти на закрытую территорию к месту проведения "
    "процессуальных действий."
)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_closed_territory_access_cannot_become_a_closed_object(
    validate: RelationValidator,
) -> None:
    validate(
        DETAINED_OFFICIAL_ACCESS_SOURCE,
        "проход начальства на закрытую территорию к задержанному",
    )
    with pytest.raises(ValueError):
        validate(
            DETAINED_OFFICIAL_ACCESS_SOURCE,
            "посещение закрытого объекта начальством задержанного",
        )
