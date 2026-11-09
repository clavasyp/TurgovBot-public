from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _relation_document_patient_passive_agrees,
    _relation_patient_head_features,
    validate_enrichment_title_source_evidence,
    validate_penalty_source_evidence,
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


def _validate_penalty_summary(source: str, output: str) -> None:
    validate_penalty_source_evidence(
        "Наказание: " + source,
        output,
        None,
    )


def _validate_penalty_other(source: str, output: str) -> None:
    validate_penalty_source_evidence(
        "Наказание: " + source,
        None,
        {"other": output},
    )


ALL_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
    _validate_penalty_summary,
    _validate_penalty_other,
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_mandatory_nominal_patient_requires_genitive_case(
    validate: RelationValidator,
) -> None:
    source = "Обязательное аннулирование лицензии на оружие."
    faithful = "обязательное аннулирование оружейной лицензии"
    unsafe = "обязательное аннулирование оружейная лицензия"
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_every_nominal_patient_list_member_keeps_genitive_case(
    validate: RelationValidator,
) -> None:
    source = "Обязательное аннулирование лицензии и разрешения."
    faithful = "обязательное аннулирование разрешения и лицензии"
    validate(source, faithful)
    for unsafe in (
        "обязательное аннулирование разрешение и лицензии",
        "обязательное аннулирование разрешения и лицензия",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_deprivation_complement_keeps_genitive_case(
    validate: RelationValidator,
) -> None:
    source = "Гражданин был лишён действующей лицензии судом."
    faithful = "Гражданин был лишен действующей лицензии судом"
    validate(source, faithful)
    for unsafe in (
        "Гражданин был лишен действующую лицензию судом",
        "Гражданин был лишен действующая лицензия судом",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_semantic_document_inside_syntactic_action_head_is_validated(
    validate: RelationValidator,
) -> None:
    source = "Действие лицензии было приостановлено прокуратурой."
    faithful = "Прокуратурой было приостановлено действие лицензии"
    validate(source, faithful)
    for unsafe in (
        "Действие лицензия было приостановлено прокуратурой",
        "Действие лицензии были приостановлены прокуратурой",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_active_action_head_keeps_semantic_patient_genitive(
    validate: RelationValidator,
) -> None:
    source = "Суд прекратил действие оружейной лицензии."
    faithful = "Действие лицензии на оружие прекратил суд"
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Действие лицензия на оружие прекратил суд")


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_active_self_state_uses_nominative_patient_and_number(
    validate: RelationValidator,
) -> None:
    source = "Все пропуски прекращают своё действие."
    faithful = "Все пропуски прекращают свое действие"
    validate(source, faithful)
    for unsafe in (
        "Все пропуски прекращает свое действие",
        "Все пропусков прекращают свое действие",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_active_animate_plural_patient_uses_accusative_genitive(
    validate: RelationValidator,
) -> None:
    source = "Полиция задержала всех лиц."
    faithful = "Всех лиц задержала полиция"
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Полиция задержала все лица")


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        ("Суд назначил обыски.", "Обыски назначил суд"),
        ("Организация утвердила бюджеты.", "Бюджеты утвердила организация"),
    ),
    ids=("searches", "budgets"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_active_inanimate_plural_patient_is_not_false_blocked(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Суд назначил новые обыски.", "Обыски назначил суд"),
        ("Суд назначил обыски.", "Новые обыски назначил суд"),
        ("Организация утвердила местные бюджеты.", "Организация утвердила бюджеты"),
        ("Организация утвердила бюджеты.", "Организация утвердила местные бюджеты"),
    ),
    ids=(
        "drop-search-modifier",
        "invent-search-modifier",
        "drop-budget-modifier",
        "invent-budget-modifier",
    ),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_single_patient_keeps_non_document_identity_details(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Полиция задержала лиц.", "Полиция задержала лицо"),
        ("Суд назначил обыски.", "Суд назначил обыск"),
        (
            "Организации были закрыты судом.",
            "Организация была закрыта судом",
        ),
    ),
    ids=("persons", "searches", "organizations"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_non_document_patient_number_cannot_change(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


def test_v3_bare_nut_passive_agreement_is_not_active_future_agreement() -> None:
    assert _relation_document_patient_passive_agrees(
        "Документ",
        "затронут",
    ) is True
    assert _relation_document_patient_passive_agrees(
        "Документы",
        "затронут",
    ) is False
    assert _relation_document_patient_passive_agrees(
        "Документы",
        "затронуты",
    ) is True


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_nonstrict_information_relation_still_rejects_bad_owner_case(
    validate: RelationValidator,
) -> None:
    source = "Данные владельца передала прокуратура."
    faithful = "Прокуратура передала данные владельца"
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Прокуратура передала данные владельцу")


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Два документа были аннулированы судом.",
            "Суд аннулировал два документа",
            "Суд аннулировал два документы",
        ),
        (
            "Три сертификата были аннулированы прокуратурой.",
            "Прокуратура аннулировала три сертификата",
            "Прокуратура аннулировала три сертификаты",
        ),
        (
            "Два предприятия были закрыты судом.",
            "Суд закрыл два предприятия",
            "Суд закрыл две предприятия",
        ),
        (
            "Две компании были закрыты судом.",
            "Суд закрыл две компании",
            "Суд закрыл два компании",
        ),
        (
            "Два автомобиля были обысканы полицией.",
            "Полиция обыскала два автомобиля",
            "Полиция обыскала два автомобили",
        ),
    ),
    ids=("document", "certificate", "enterprise", "company", "vehicle"),
)
@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_cardinal_two_to_four_governs_real_head_not_family_gender(
    source: str,
    faithful: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_cardinal_case_normalizes_across_active_animate_voice(
    validate: RelationValidator,
) -> None:
    source = "Два гражданина были задержаны полицией."
    validate(source, "Полиция задержала двух граждан")
    with pytest.raises(ValueError):
        validate(source, "Полиция задержала два гражданина")


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_several_keeps_valid_animate_accusative_forms(
    validate: RelationValidator,
) -> None:
    source = "Несколько граждан были задержаны полицией."
    validate(source, "Полиция задержала несколько граждан")
    validate(source, "Полиция задержала нескольких граждан")
    with pytest.raises(ValueError):
        validate(source, "Полиция задержала несколько гражданина")


def test_v3_both_oblique_form_uses_the_real_plural_head_gender() -> None:
    assert _relation_patient_head_features(
        "organization",
        "компаний",
        role="genitive",
        leading="обеих",
    ) == frozenset({"quantified_plural"})
    assert not _relation_patient_head_features(
        "organization",
        "компаний",
        role="genitive",
        leading="обоих",
    )
    assert _relation_patient_head_features(
        "organization",
        "предприятий",
        role="genitive",
        leading="обоих",
    ) == frozenset({"quantified_plural"})
    assert not _relation_patient_head_features(
        "organization",
        "предприятий",
        role="genitive",
        leading="обеих",
    )


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Обязательное закрытие обеих компаний.",
            "Обязательное закрытие обоих компаний",
        ),
        (
            "Обязательное закрытие обоих предприятий.",
            "Обязательное закрытие обеих предприятий",
        ),
    ),
)
@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_both_oblique_gender_mismatch_fails_every_output_path(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_deprivation_separates_affected_patient_and_taken_complement(
    validate: RelationValidator,
) -> None:
    source = "Суд лишил гражданку действующей лицензии."
    validate(source, "Гражданку действующей лицензии лишил суд")
    for unsafe in (
        "Суд лишил гражданки действующей лицензии",
        "Суд лишил гражданку действующую лицензию",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_revocation_self_state_uses_subject_case_and_agreement(
    validate: RelationValidator,
) -> None:
    source = "Оружейная лицензия утрачивает силу."
    validate(source, "Лицензия на оружие утрачивает силу")
    with pytest.raises(ValueError):
        validate(source, "Лицензию на оружие утрачивает силу")


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_outer_action_governor_has_proved_gender(
    validate: RelationValidator,
) -> None:
    source = "Продолжительность действия лицензии была прекращена судом."
    validate(source, "Судом была прекращена продолжительность действия лицензии")
    with pytest.raises(ValueError):
        validate(source, "Судом был прекращен продолжительность действия лицензии")


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_validity_heading_does_not_bypass_inherited_patient_grammar(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия считается действительной при соблюдении следующих условий:\n"
        "Подписана судом."
    )
    unsafe = (
        "Лицензию считается действительной при соблюдении следующих условий:\n"
        "Подписана судом"
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_comma_coordinated_patient_modifiers_are_not_discarded(
    validate: RelationValidator,
) -> None:
    source = "Новая, действующая лицензия была аннулирована прокуратурой."
    validate(source, "Прокуратура аннулировала новую, действующую лицензию")
    with pytest.raises(ValueError):
        validate(source, "Прокуратура аннулировала действующую лицензию")


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_patient_list_supports_serial_comma_and_a_takzhe(
    validate: RelationValidator,
) -> None:
    source = "Суд аннулировал лицензию, разрешение и пропуск."
    validate(source, "Суд аннулировал пропуск, а также разрешение и лицензию")
    with pytest.raises(ValueError):
        validate(source, "Суд аннулировал разрешение и лицензию")


@pytest.mark.parametrize(
    "output",
    (
        "Суд новую лицензию аннулировал",
        "Аннулировал суд новую лицензию",
    ),
    ids=("sov", "vso"),
)
@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_actor_never_becomes_patient_identity_in_sov_or_vso(
    output: str,
    validate: RelationValidator,
) -> None:
    source = "Суд аннулировал новую лицензию."
    validate(source, output)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS)
def test_v3_adjectival_object_stem_does_not_poison_real_patient(
    validate: RelationValidator,
) -> None:
    source = "Суд назначил бюджетный штраф."
    validate(source, "Бюджетный штраф назначил суд")


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_feminine_person_plural_keeps_case_number_and_both_gender(
    validate: RelationValidator,
) -> None:
    source = "Полиция задержала обеих гражданок."
    validate(source, "Обе гражданки были задержаны полицией")
    with pytest.raises(ValueError):
        validate(source, "Обоих гражданок задержала полиция")
