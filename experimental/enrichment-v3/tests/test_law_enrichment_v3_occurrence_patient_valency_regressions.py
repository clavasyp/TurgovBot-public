from __future__ import annotations

import pytest

from tests.test_law_enrichment_v3_closed_uk_list_regressions import (
    ALL_OUTPUT_PATHS as BASE_OUTPUT_PATHS,
    RelationValidator,
)
from services.law_enrichment_validation import validate_penalty_source_evidence


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
    *BASE_OUTPUT_PATHS[:3],
    _validate_penalty_summary,
    _validate_penalty_other,
)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Полиция задержала гражданина.", "Полиция задержала"),
        ("Суд обыскал автомобиль.", "Суд обыскал"),
        ("Суд лишил гражданина лицензии.", "Суд лишил"),
        ("Суд закрыл организацию.", "Суд закрыл"),
        ("Суд арестовал гражданина.", "Суд арестовал"),
        (
            "Суд вынес решение об аннулировании лицензии.",
            "Суд вынес решение об аннулировании",
        ),
        (
            "Лицо обязано сообщить адрес.",
            "Лицо обязано сообщить",
        ),
        (
            "Суд обязан допустить гражданина.",
            "Суд обязан допустить",
        ),
    ),
    ids=(
        "detain",
        "search",
        "deprive",
        "close",
        "open-arrest",
        "definite-nominal",
        "mandatory-inform",
        "mandatory-admit",
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
def test_v3_every_finite_high_impact_transitive_action_keeps_a_patient(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Суд арестовал гражданина.",
            "Гражданин был арестован судом",
            ("Суд арестовал", "Суд арестовал организацию"),
        ),
        (
            "Суд конфисковал такси.",
            "Такси было конфисковано судом",
            ("Суд конфисковал", "Такси был конфискован судом"),
        ),
        (
            "Суд распустил организацию.",
            "Организация была распущена судом",
            ("Суд распустил", "Суд ликвидировал организацию"),
        ),
        (
            "Суд ликвидировал организацию.",
            "Организация была ликвидирована судом",
            ("Суд ликвидировал", "Суд распустил организацию"),
        ),
    ),
    ids=("arrest", "confiscate", "dissolve", "liquidate"),
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
def test_v3_plain_strict_open_facts_reach_patient_and_source_gates(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Суд распустил и ликвидировал организацию.",
            "Организация была распущена и ликвидирована судом",
        ),
        (
            "Организация была распущена и ликвидирована судом.",
            "Суд распустил и ликвидировал организацию",
        ),
        (
            "Имущество было конфисковано и ликвидировано судом.",
            "Суд конфисковал и ликвидировал имущество",
        ),
    ),
    ids=(
        "active-trailing-patient",
        "passive-leading-patient",
        "open-lexical-leading-patient",
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
def test_v3_tight_mixed_strict_open_actions_share_one_physical_role_set(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд распустил и ликвидировал организацию.",
            "Организация была распущен и ликвидирована судом",
        ),
        (
            "Организация была распущена. Ликвидирована судом.",
            "Организация была распущена и ликвидирована судом",
        ),
        (
            "Организация была распущена; ликвидирована судом.",
            "Организация была распущена и ликвидирована судом",
        ),
        (
            "Имущество было конфисковано и ликвидировано судом.",
            "Имущество было конфискован и ликвидировано судом",
        ),
    ),
    ids=(
        "wrong-agreement",
        "sentence-fence",
        "semicolon-fence",
        "open-lexical-wrong-agreement",
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
def test_v3_strict_open_role_sharing_stays_inside_one_tight_cluster(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Правило применяется к гражданину.",
            "Правило применяется к гражданину",
        ),
        (
            "Правила применяются к гражданам.",
            "Правила применяются к гражданам",
        ),
        (
            "Требования применяются к требованиям закона.",
            "Требования применяются к требованиям закона",
        ),
        (
            "Правила применяются к правилам поведения.",
            "Правила применяются к правилам поведения",
        ),
        (
            "Правило применяется к гражданину штата.",
            "Правило применяется к гражданину штата",
        ),
    ),
    ids=(
        "singular",
        "plural",
        "plural-target-genitive-tail",
        "rules-behavior-tail",
        "citizen-state-tail",
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
def test_v3_apply_relation_keeps_bounded_subject_and_dative_target(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Правила применяются к гражданину.",
            "Правила применяется к гражданину",
        ),
        (
            "Правило применяется к гражданину.",
            "Правило применяется к гражданин",
        ),
        (
            "Правило применяется к гражданину.",
            "Правило применяется к гражданам",
        ),
        (
            "Правила применяются к гражданам.",
            "Правило применяется к гражданам",
        ),
        (
            "Требования применяются к требованиям закона.",
            "Требования применяются к требованиям закону",
        ),
        (
            "Правило применяется к гражданину штата.",
            "Правило применяется к гражданину штату",
        ),
    ),
    ids=(
        "predicate-number",
        "target-not-dative",
        "target-number",
        "subject-number",
        "genitive-tail-case",
        "identity-tail-case",
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
def test_v3_apply_relation_rejects_case_and_agreement_laundering(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "query"),
    (
        ("Суд арестовал гражданина.", "Кто арестовал гражданина?"),
        ("Суд конфисковал такси.", "Что конфисковал суд?"),
        ("Суд распустил организацию.", "Кто распустил организацию?"),
        ("Суд ликвидировал организацию.", "Кто ликвидировал организацию?"),
    ),
    ids=("arrest", "confiscate", "dissolve", "liquidate"),
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
def test_v3_strict_open_questions_remain_nonassertive(
    source: str,
    query: str,
    validate: RelationValidator,
) -> None:
    validate(source, query)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд назначил обыск.",
            "Суд приостановил обыск",
        ),
        (
            "Организация утвердила бюджет.",
            "Организация приостановила бюджет",
        ),
    ),
    ids=("search", "budget"),
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
def test_v3_typed_patient_cannot_borrow_a_different_source_predicate(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Суд отменил обыск.",
            "Обыск был отменён судом",
        ),
        (
            "Полиция изъяла оружие.",
            "Оружие было изъято полицией",
        ),
        (
            "Организация приостановила бюджет.",
            "Бюджет был приостановлен организацией",
        ),
        (
            "Суд отменил слушание.",
            "Слушание было отменено судом",
        ),
    ),
    ids=("cancel-search", "seize-weapon", "pause-budget", "open-patient"),
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
def test_v3_source_proven_predicate_patient_pairs_remain_supported(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    ("source", "faithful", "wrong_patient"),
    (
        (
            "Суд выдал допуск.",
            "Допуск был выдан судом",
            "Суд выдал разрешение",
        ),
        (
            "Суд выдал разрешение.",
            "Разрешение было выдано судом",
            "Суд выдал сертификат",
        ),
        (
            "Суд выдал сертификат.",
            "Сертификат был выдан судом",
            "Суд выдал паспорт",
        ),
        (
            "Суд выдал паспорт.",
            "Паспорт был выдан судом",
            "Суд выдал ордер",
        ),
        (
            "Суд выдал ордер.",
            "Ордер был выдан судом",
            "Суд выдал допуск",
        ),
    ),
    ids=("access-pass", "authorization", "certificate", "passport", "warrant"),
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
def test_v3_every_typed_regulated_issue_patient_uses_source_evidence(
    source: str,
    faithful: str,
    wrong_patient: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, wrong_patient)


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
def test_v3_regulated_issue_cannot_replace_source_revocation(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Суд отозвал сертификат.",
            "Суд выдал сертификат",
        )


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Организация утвердила бюджет.",
            "Приостанавливает бюджет",
        ),
        (
            "Суд отозвал сертификат.",
            "Выдаёт сертификат",
        ),
    ),
    ids=("implicit-revocation-actor", "implicit-issue-actor"),
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
def test_v3_assertive_present_action_cannot_escape_via_implicit_actor(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд подписал сертификат.",
            "Суд выдал сертификат",
        ),
        (
            "Суд оформил сертификат.",
            "Суд выдал сертификат",
        ),
        (
            "Суд выдал сертификат.",
            "Суд подписал сертификат",
        ),
    ),
    ids=("signature-to-issue", "registration-to-issue", "issue-to-signature"),
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
def test_v3_issue_effect_families_cannot_prove_each_other(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
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
def test_v3_explicit_issue_effects_remain_independently_searchable(
    validate: RelationValidator,
) -> None:
    source = "Суд выдал сертификат и подписал сертификат."
    validate(source, "Суд выдал сертификат")
    validate(source, "Суд подписал сертификат")


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
def test_v3_self_state_revocation_still_owns_its_subject_patient(
    validate: RelationValidator,
) -> None:
    validate(
        "Лицензия утрачивает силу.",
        "Лицензия теряет силу",
    )
    validate(
        "Пропуски прекращают своё действие.",
        "Действие пропусков прекращается",
    )


@pytest.mark.parametrize(
    "unsafe",
    (
        "Полиция задержала гражданина",
        "Полиция освободила гражданина",
        "Суд утвердил бюджет",
        "Прокуратура возбудила уголовное дело",
        "Полиция предъявила ордер",
        "Полиция депортирует гражданина",
        "Суд выдал документ",
        "Суд подписал акт",
        "Суд допустил гражданина",
        "Суд назначил штраф",
        "Суд взыскивает штраф",
        "Суд налагает штраф",
    ),
    ids=(
        "detain",
        "release",
        "approve",
        "open-case",
        "present",
        "deport-present",
        "issue-document",
        "sign-act",
        "admit",
        "assign-fine",
        "recover-fine",
        "impose-fine-present",
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
def test_v3_every_finite_high_impact_fact_requires_source_evidence(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate("Суд отозвал лицензию.", unsafe)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Полиция задержала гражданина.",
            "Гражданина задержала полиция",
        ),
        (
            "Полиция освободила гражданина.",
            "Гражданина освободила полиция",
        ),
        (
            "Суд утвердил бюджет.",
            "Бюджет утвердил суд",
        ),
        (
            "Прокуратура возбудила уголовное дело.",
            "Уголовное дело возбудила прокуратура",
        ),
        (
            "Суд выдал документ.",
            "Документ выдал суд",
        ),
        (
            "Суд подписал акт.",
            "Акт подписал суд",
        ),
        (
            "Суд допустил гражданина.",
            "Гражданина допустил суд",
        ),
        (
            "Суд назначил штраф.",
            "Штраф назначил суд",
        ),
        (
            "Суд взыскивает штраф.",
            "Штраф взыскивает суд",
        ),
        (
            "Суд налагает штраф.",
            "Штраф налагает суд",
        ),
    ),
    ids=(
        "detain",
        "release",
        "approve",
        "open-case",
        "issue-document",
        "sign-act",
        "admit",
        "assign-fine",
        "recover-fine",
        "impose-fine-present",
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
def test_v3_source_proven_finite_high_impact_facts_survive_word_order(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Суд отменил открытое слушание.",
            "Открытое слушание было отменено судом",
            (
                "Суд отменил закрытое слушание",
                "Суд отменил открытый слушание",
            ),
        ),
        (
            "Суд отменил два слушания.",
            "Два слушания были отменены судом",
            ("Суд отменил слушание",),
        ),
        (
            "Суд отменил две встречи.",
            "Две встречи были отменены судом",
            (
                "Суд отменил два встречи",
                "Суд отменил двух встречи",
                "Суд отменил две новых встречи",
            ),
        ),
    ),
    ids=("modifier", "cardinality-neuter", "cardinality-feminine"),
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
def test_v3_open_patient_keeps_bounded_modifier_and_number(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Суд отменил слушания.",
            "Слушания были отменены судом",
            (
                "Суд отменил слушание",
                "Слушания было отменено судом",
                "Слушание были отменены судом",
            ),
        ),
        (
            "Суд освободил водителя.",
            "Водитель был освобождён судом",
            (
                "Водитель была освобождена судом",
                "Суд освободил водитель",
            ),
        ),
        (
            "Суд отменил заказ.",
            "Заказ был отменён судом",
            ("Суд отменил заказа",),
        ),
        (
            "Суд отменил заказы.",
            "Заказы были отменены судом",
            ("Суд отменил заказов",),
        ),
        (
            "Суд конфисковал такси.",
            "Такси было конфисковано судом",
            ("Такси был конфискован судом",),
        ),
        (
            "Суд отменил должность.",
            "Должность была отменена судом",
            ("Должность был отменён судом",),
        ),
        (
            "Суд конфисковал уголь.",
            "Уголь был конфискован судом",
            ("Суд конфисковал угол",),
        ),
        (
            "Суд конфисковал медь.",
            "Медь была конфискована судом",
            ("Суд конфисковал мед",),
        ),
    ),
    ids=(
        "unquantified-plural",
        "animate-case-change",
        "inanimate-case-change",
        "inanimate-plural-case-change",
        "indeclinable-neuter",
        "soft-sign-feminine",
        "soft-sign-coal-collision",
        "soft-sign-copper-collision",
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
def test_v3_open_patient_proves_case_number_and_passive_agreement(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


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
def test_v3_open_neuter_patient_keeps_genitive_nominal_case(
    validate: RelationValidator,
) -> None:
    source = "Суд отменил право."

    validate(source, "отмена права судом")
    with pytest.raises(ValueError):
        validate(source, "отмена право судом")


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Суд освободил двух водителей.",
            "Два водителя были освобождены судом",
            "Суд освободил два водителя",
        ),
        (
            "Суд отменил несколько заказов.",
            "Несколько заказов были отменены судом",
            "Суд отменил несколько заказ",
        ),
        (
            "Суд отменил несколько встреч.",
            "Несколько встреч были отменены судом",
            "Суд отменил несколько встречи",
        ),
        (
            "Суд отменил несколько правил.",
            "Несколько правил были отменены судом",
            "Суд отменил несколько правила",
        ),
    ),
    ids=(
        "animate-two",
        "several-suffix-plural",
        "several-zero-feminine",
        "several-zero-neuter",
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
def test_v3_open_cardinal_keeps_case_animacy_and_head_form(
    source: str,
    faithful: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "faithful",
    (
        "Водителя освободил суд",
        "Водителя суд освободил",
        "Суд водителя освободил",
    ),
    ids=("ovs", "osv", "sov"),
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
def test_v3_open_patient_is_recovered_before_an_active_action(
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate("Суд освободил водителя.", faithful)


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
def test_v3_open_patient_primary_head_owns_its_genitive_tail(
    validate: RelationValidator,
) -> None:
    source = "Суд отменил заказ клиента."

    validate(source, "Заказ клиента был отменён судом")
    for unsafe in (
        "Суд отменил заказ",
        "Суд отменил заказ компании",
        "Суд отменил заказ клиент",
        "Суд отменил заказ клиенту",
        "Суд отменил клиента",
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
def test_v3_open_patient_post_head_list_tail_is_exact(
    validate: RelationValidator,
) -> None:
    source = "Суд отменил слушание и заседание."

    validate(source, "Слушание и заседание были отменены судом")
    for unsafe in (
        "Суд отменил слушание и заседания",
        "Суд отменил слушание",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Суд назначил обязательные работы.",
            "Обязательные работы были назначены судом",
            ("Суд назначил", "Суд назначил штраф"),
        ),
        (
            "Суд взыскал компенсацию.",
            "Компенсация была взыскана судом",
            ("Суд взыскал", "Суд взыскал задолженность"),
        ),
        (
            "Суд наложил арест.",
            "Арест был наложен судом",
            ("Суд наложил", "Суд наложил запрет"),
        ),
    ),
    ids=("assign", "recover", "impose"),
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
def test_v3_open_sanction_patient_is_exact_and_voice_neutral(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Лицо обязано сообщить местонахождение.",
            "Лицо должно сообщить местонахождение",
            ("Лицо обязано сообщить", "Лицо обязано сообщить телефон"),
        ),
        (
            "Лицо обязано сообщить телефон.",
            "Лицо должно сообщить телефон",
            ("Лицо обязано сообщить", "Лицо обязано сообщить причину"),
        ),
        (
            "Лицо обязано сообщить причину.",
            "Лицо должно сообщить причину",
            ("Лицо обязано сообщить", "Лицо обязано сообщить адрес"),
        ),
    ),
    ids=("location", "phone", "reason"),
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
def test_v3_mandatory_inform_keeps_an_open_information_patient(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Суд сообщил местонахождение.",
            "Местонахождение было сообщено судом",
            ("Суд сообщил", "Суд сообщил телефон"),
        ),
        (
            "Суд сообщил информацию.",
            "Информация была сообщена судом",
            ("Суд сообщил", "Суд сообщил основание"),
        ),
        (
            "Суд заверил квитанцию.",
            "Квитанция была заверена судом",
            ("Суд заверил", "Суд заверил договор"),
        ),
        (
            "Суд заверил договор.",
            "Договор был заверен судом",
            ("Суд заверил", "Суд заверил протокол"),
        ),
    ),
    ids=("inform-open", "inform-typed", "certify-typed", "certify-open"),
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
def test_v3_factual_inform_and_certify_keep_their_patient(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


@pytest.mark.parametrize(
    ("source", "query"),
    (
        ("Суд задержал гражданина.", "Кто задержал?"),
        ("Суд выдал лицензию.", "Кто выдал?"),
        ("Суд арестовал гражданина.", "Кто арестовал?"),
        ("Суд сообщил местонахождение.", "Кто сообщил?"),
        ("Суд заверил договор.", "Кто заверил?"),
    ),
    ids=("detain", "issue", "arrest", "inform", "certify"),
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
def test_v3_nonassertive_query_may_omit_a_transitive_patient(
    source: str,
    query: str,
    validate: RelationValidator,
) -> None:
    validate(source, query)


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
def test_v3_deprivation_keeps_two_open_roles_across_voice_and_order(
    validate: RelationValidator,
) -> None:
    source = "Суд лишил водителя права управления транспортом."
    for faithful in (
        "Водитель был лишен права управления транспортом судом",
        "Водителя права управления транспортом лишил суд",
    ):
        validate(source, faithful)


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
def test_v3_deprivation_keeps_typed_affected_and_open_taken_role(
    validate: RelationValidator,
) -> None:
    source = "Суд лишил организацию статуса участника."
    validate(
        source,
        "Организация была лишена статуса участника судом",
    )
    for unsafe in (
        "Суд лишил организацию",
        "Суд лишил статуса участника",
        "Суд лишил организацию полномочий участника",
        "Суд лишил организацию статус участника",
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
def test_v3_deprivation_keeps_each_role_multiset_and_boolean_operator(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд лишил водителя и подозреваемого права управления "
        "и статуса участника."
    )
    validate(
        source,
        "Суд лишил подозреваемого и водителя статуса участника "
        "и права управления",
    )
    for unsafe in (
        "Суд лишил подозреваемого права управления и статуса участника",
        "Суд лишил водителя и подозреваемого права управления",
        "Суд лишил водителя или подозреваемого права управления "
        "и статуса участника",
        "Суд лишил водителя и подозреваемого права управления "
        "или статуса участника",
        "Суд лишил водителя и водителя права управления "
        "и статуса участника",
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
def test_v3_deprivation_comma_list_is_canonical_joint_not_or(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд лишил гражданина, водителя, сотрудника права доступа."
    )
    for faithful in (
        "Суд лишил сотрудника, гражданина, водителя права доступа",
        "Суд лишил сотрудника, гражданина и водителя права доступа",
        "Суд лишил сотрудника и гражданина и водителя права доступа",
    ):
        validate(source, faithful)
    for unsafe in (
        "Суд лишил гражданина или водителя или сотрудника права доступа",
        "Суд лишил гражданина, водителя права доступа",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Суд лишил водитель права управления транспортом",
        "Суд лишил водителя право управления транспортом",
        "Водителя был лишен права управления транспортом судом",
        "Водитель были лишены права управления транспортом судом",
        "Суд лишил водителя права управления при условии (решения",
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
def test_v3_deprivation_rejects_wrong_role_case_and_passive_agreement(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = "Суд лишил водителя права управления транспортом."
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        ("Суд лишил гражданина.", "Гражданина лишил суд"),
        ("Суд лишил лицензии.", "Лицензии лишил суд"),
        (
            "Суд лишил права управления.",
            "Права управления лишил суд",
        ),
    ),
    ids=("affected-only", "taken-only", "taken-entitlement-tail"),
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
def test_v3_deprivation_one_explicit_role_remains_supported(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


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
def test_v3_deprivation_query_may_leave_the_affected_role_open(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд лишил водителя права управления транспортом.",
        "Кого лишили права управления транспортом?",
    )


@pytest.mark.parametrize(
    "separator",
    (". ", "; "),
    ids=("sentence", "semicolon"),
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
def test_v3_deprivation_roles_do_not_cross_hard_clause_fences(
    separator: str,
    validate: RelationValidator,
) -> None:
    source = "Суд лишил водителя" + separator + "Право управления утрачено."
    with pytest.raises(ValueError):
        validate(source, "Суд лишил водителя права управления")


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
def test_v3_deprivation_passive_actor_keeps_both_roles_in_any_position(
    validate: RelationValidator,
) -> None:
    source = "Суд лишил гражданина права управления."
    for faithful in (
        "Гражданин был лишен судом права управления",
        "Гражданин судом был лишен права управления",
        "Гражданин был судом лишен права управления",
        "Гражданин был лишен права управления судом",
    ):
        validate(source, faithful)
    for unsafe in (
        "Гражданин был лишен суд права управления",
        "Гражданин суд был лишен права управления",
        "Гражданин был лишен прокуратурой права управления",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "При нарушении суд лишил гражданина лицензии.",
            "Гражданин был лишен лицензии судом при нарушении",
            (
                "Суд лишил гражданина лицензии",
                "При соблюдении гражданин был лишен лицензии судом",
            ),
        ),
        (
            "Суд лишил гражданина лицензии при нарушении.",
            "При нарушении гражданин был лишен лицензии судом",
            (
                "Суд лишил гражданина лицензии",
                "При соблюдении гражданин был лишен лицензии судом",
            ),
        ),
        (
            "Суд лишил гражданина лицензии за нарушение закона.",
            "За нарушение закона гражданин был лишен лицензии судом",
            (
                "Суд лишил гражданина лицензии",
                "За нарушение договора гражданин был лишен лицензии судом",
            ),
        ),
        (
            "За нарушение закона суд лишил гражданина лицензии.",
            "Гражданин был лишен лицензии судом за нарушение закона",
            (
                "Суд лишил гражданина лицензии",
                "Гражданин был лишен лицензии судом за нарушение договора",
            ),
        ),
    ),
    ids=(
        "condition-leading-to-trailing",
        "condition-trailing-to-leading",
        "ground-trailing-to-leading",
        "ground-leading-to-trailing",
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
def test_v3_deprivation_two_role_contract_keeps_condition_and_ground(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


@pytest.mark.parametrize(
    ("source", "faithful", "dropped", "changed"),
    (
        (
            "Суд лишил потерпевшую лицензии.",
            "Потерпевшая была лишена лицензии судом",
            "Суд лишил лицензии",
            "Суд лишил свидетельницу лицензии",
        ),
        (
            "Суд лишил свидетельницу права управления.",
            "Свидетельница была лишена права управления судом",
            "Суд лишил права управления",
            "Суд лишил собственницу права управления",
        ),
        (
            "Суд лишил собственницу статуса участника.",
            "Собственница была лишена статуса участника судом",
            "Суд лишил статуса участника",
            "Суд лишил потерпевшую статуса участника",
        ),
    ),
    ids=("substantivized", "feminine-role", "feminine-owner"),
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
def test_v3_deprivation_open_feminine_affected_role_is_exact(
    source: str,
    faithful: str,
    dropped: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in (dropped, changed):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "dropped", "changed"),
    (
        (
            "Суд лишил магазин лицензии.",
            "Магазин был лишен лицензии судом",
            "Суд лишил лицензии",
            "Суд лишил склад лицензии",
        ),
        (
            "Суд лишил руководство лицензии.",
            "Руководство было лишено лицензии судом",
            "Суд лишил лицензии",
            "Суд лишил управление лицензии",
        ),
        (
            "Суд лишил мать лицензии.",
            "Мать была лишена лицензии судом",
            "Суд лишил лицензии",
            "Суд лишил дочь лицензии",
        ),
        (
            "Суд лишил документ силы.",
            "Документ был лишен силы судом",
            "Суд лишил силы",
            "Суд лишил протокол силы",
        ),
    ),
    ids=("shop", "management", "mother", "typed-document"),
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
def test_v3_deprivation_structural_affected_role_is_not_person_only(
    source: str,
    faithful: str,
    dropped: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in (dropped, changed):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Суд лишил водителю лицензии",
        "Суд лишил водитель лицензии",
        "Суд лишил сотруднику лицензии",
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
def test_v3_deprivation_active_affected_case_is_exact(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = "Суд лишил водителя лицензии."
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "dropped", "changed"),
    (
        (
            "Суд лишил начальника отдела лицензии.",
            "Начальник отдела был лишен лицензии судом",
            "Суд лишил лицензии",
            "Суд лишил начальника управления лицензии",
        ),
        (
            "Суд лишил представителя организации лицензии.",
            "Представитель организации был лишен лицензии судом",
            "Суд лишил лицензии",
            "Суд лишил представителя компании лицензии",
        ),
        (
            "Суд лишил генерального директора компании права доступа.",
            "Генеральный директор компании был лишен права доступа судом",
            "Суд лишил права доступа",
            "Суд лишил генерального директора банка права доступа",
        ),
        (
            "Суд лишил водителя Иванова лицензии.",
            "Водитель Иванов был лишен лицензии судом",
            "Суд лишил лицензии",
            "Суд лишил водителя Петрова лицензии",
        ),
    ),
    ids=("department", "organization", "company", "surname"),
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
def test_v3_deprivation_keeps_dependent_affected_identity(
    source: str,
    faithful: str,
    dropped: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in (dropped, changed):
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
def test_v3_deprivation_active_actor_may_follow_the_taken_role(
    validate: RelationValidator,
) -> None:
    source = "Районный суд лишил водителя лицензии."
    validate(source, "Водителя лишил лицензии районный суд")
    for unsafe in (
        "Лишил лицензии районный суд",
        "Водителя лишила лицензии районная прокуратура",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Инспектор лишил свидетельницу лицензии.",
            "Свидетельницу лишил лицензии инспектор",
        ),
        (
            "Начальник управления лишил свидетельницу лицензии.",
            "Свидетельницу лишил лицензии начальник управления",
        ),
    ),
    ids=("inspector", "management-identity"),
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
def test_v3_deprivation_source_proved_actor_is_not_shortlist_limited(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in (
        faithful.replace("Свидетельницу ", ""),
        faithful.replace("Свидетельницу", "Собственницу"),
        faithful.replace("инспектор", "прокурор").replace(
            "начальник управления",
            "прокурор",
        ),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "dropped", "changed"),
    (
        (
            "Суд лишил суд лицензии.",
            "Суд лишил суд лицензии",
            "Суд лишил лицензии",
            "Суд лишил прокуратуру лицензии",
        ),
        (
            "Организация лишила организацию статуса.",
            "Организация была лишена статуса организацией",
            "Организация лишила статуса",
            "Организация лишила гражданина статуса",
        ),
    ),
    ids=("court", "organization"),
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
def test_v3_deprivation_same_identity_actor_is_not_erased_as_patient(
    source: str,
    faithful: str,
    dropped: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in (dropped, changed):
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
def test_v3_deprivation_actorless_institution_is_not_masked_as_actor(
    validate: RelationValidator,
) -> None:
    source = "Необходимо лишить суд лицензии."
    validate(source, "Необходимо лишить суд лицензии")
    for unsafe in (
        "Необходимо лишить лицензии",
        "Необходимо лишить прокуратуру лицензии",
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
def test_v3_deprivation_roles_may_be_on_opposite_action_sides(
    validate: RelationValidator,
) -> None:
    source = "Суд лишил водителя лицензии."
    for faithful in (
        "Лицензии суд лишил водителя",
        "Лицензии лишил суд водителя",
    ):
        validate(source, faithful)
    for unsafe in (
        "Лицензии суд лишил сотрудника",
        "Права доступа суд лишил водителя",
        "Лицензии прокуратура лишила водителя",
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
def test_v3_deprivation_passive_roles_are_not_side_hardcoded(
    validate: RelationValidator,
) -> None:
    source = "Суд лишил водителя лицензии."
    for faithful in (
        "Был лишен водитель лицензии судом",
        "Судом был лишен водитель лицензии",
        "Водитель лицензии был лишен судом",
        "Лицензии водитель был лишен судом",
        "Лицензии был лишен водитель судом",
    ):
        validate(source, faithful)
    for unsafe in (
        "Был лишен лицензии судом",
        "Лицензии сотрудник был лишен судом",
        "Права доступа водитель был лишен судом",
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
def test_v3_deprivation_ambiguous_same_side_reverse_is_exact_only(
    validate: RelationValidator,
) -> None:
    source = "Лицензии водителя лишил суд."
    validate(source, "Лицензии водителя лишил суд")
    for unsafe in (
        "Лицензии лишил суд",
        "Лицензии сотрудника лишил суд",
        "Водителя лицензии лишил суд",
        "Суд лишил водителя лицензии",
        "Лицензии водитель был лишен судом",
        "Лишение водителя лицензии",
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
def test_v3_deprivation_nominal_two_role_heading_is_exact_only(
    validate: RelationValidator,
) -> None:
    source = "Лишение гражданина лицензии."
    validate(source, "Лишение гражданина лицензии")
    for unsafe in (
        "Лишение лицензии",
        "Лишение организации лицензии",
        "Лишение лицензии гражданина",
        "Суд лишил гражданина лицензии",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)
    validate("Лишение лицензии.", "Лишение лицензии")
    validate(
        "Лишение права управления.",
        "Лишение права управления",
    )


@pytest.mark.parametrize(
    ("source", "changed"),
    (
        (
            "Лишение свидетельницы лицензии.",
            "Лишение собственницы лицензии",
        ),
        (
            "Лишение потерпевшей лицензии.",
            "Лишение подозреваемой лицензии",
        ),
        ("Лишение матери лицензии.", "Лишение дочери лицензии"),
        ("Лишение дочери лицензии.", "Лишение матери лицензии"),
        (
            "Лишение руководства лицензии.",
            "Лишение управления лицензии",
        ),
        (
            "Лишение правоохранителя лицензии.",
            "Лишение инспектора лицензии",
        ),
    ),
    ids=(
        "witness",
        "substantivized",
        "mother",
        "daughter",
        "management",
        "law-enforcement",
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
def test_v3_deprivation_nominal_open_affected_role_is_not_dropped(
    source: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    for unsafe in ("Лишение лицензии", changed):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        "Суд лишил гражданина лицензии.",
        "Гражданин был лишен лицензии судом.",
    ),
    ids=("active", "passive"),
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
def test_v3_deprivation_complete_finite_pair_may_become_nominal_topic(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "Лишение гражданина лицензии")
    for unsafe in (
        "Лишение лицензии",
        "Лишение гражданина",
        "Лишение организации лицензии",
        "Лишение гражданина статуса",
        "Лишение лицензии гражданина",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Суд лишил её лицензии.",
            "Она была лишена лицензии судом",
            ("Суд лишил лицензии", "Суд лишил их лицензии"),
        ),
        (
            "Суд лишил их лицензии.",
            "Они были лишены лицензии судом",
            ("Суд лишил лицензии", "Суд лишил её лицензии"),
        ),
        (
            "Суд лишил его лицензии.",
            "Его лицензии лишил суд",
            ("Суд лишил лицензии", "Суд лишил её лицензии"),
        ),
    ),
    ids=("feminine", "plural", "ambiguous-masculine-neuter"),
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
def test_v3_deprivation_pronominal_affected_role_is_preserved(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for candidate in unsafe:
        with pytest.raises(ValueError):
            validate(source, candidate)


@pytest.mark.parametrize(
    ("source", "faithful", "changed"),
    (
        (
            "Суд лишил меня лицензии.",
            "Я был лишен лицензии судом",
            "Суд лишил тебя лицензии",
        ),
        (
            "Суд лишил тебя лицензии.",
            "Ты была лишена лицензии судом",
            "Суд лишил меня лицензии",
        ),
        (
            "Суд лишил нас лицензии.",
            "Мы были лишены лицензии судом",
            "Суд лишил вас лицензии",
        ),
        (
            "Суд лишил вас лицензии.",
            "Вы были лишены лицензии судом",
            "Суд лишил нас лицензии",
        ),
        (
            "Суд лишил себя лицензии.",
            "Суд лишил себя лицензии",
            "Суд лишил меня лицензии",
        ),
    ),
    ids=("me", "you-singular", "us", "you-plural", "reflexive"),
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
def test_v3_deprivation_personal_pronoun_case_bridge_is_bounded(
    source: str,
    faithful: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in ("Суд лишил лицензии", changed):
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
def test_v3_deprivation_possessive_pronoun_is_not_the_affected_role(
    validate: RelationValidator,
) -> None:
    source = "Суд лишил гражданина его лицензии."
    validate(source, "Суд лишил гражданина его лицензии")
    for unsafe in (
        "Суд лишил гражданина лицензии",
        "Суд лишил его лицензии",
        "Суд лишил гражданина её лицензии",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "changed", "wrong_case"),
    (
        (
            "Суд лишил Иванова лицензии.",
            "Иванов был лишен лицензии судом",
            "Суд лишил Петрова лицензии",
            "Суд лишил Иванов лицензии",
        ),
        (
            "Суд лишил Иванову лицензии.",
            "Иванова была лишена лицензии судом",
            "Суд лишил Петрову лицензии",
            "Суд лишил Иванова лицензии",
        ),
    ),
    ids=("male-surname", "female-surname"),
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
def test_v3_deprivation_surname_only_affected_role_is_case_bounded(
    source: str,
    faithful: str,
    changed: str,
    wrong_case: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in ("Суд лишил лицензии", changed, wrong_case):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "changed"),
    (
        ("Лишение ребёнка лицензии.", "Лишение отца лицензии"),
        ("Лишение женщины лицензии.", "Лишение супруги лицензии"),
        ("Лишение отца лицензии.", "Лишение ребёнка лицензии"),
        ("Лишение судьи лицензии.", "Лишение женщины лицензии"),
        ("Лишение супруга лицензии.", "Лишение ребёнка лицензии"),
        ("Лишение супруги лицензии.", "Лишение судьи лицензии"),
        ("Лишение детей лицензии.", "Лишение супругов лицензии"),
    ),
    ids=(
        "child",
        "woman",
        "father",
        "judge",
        "male-spouse",
        "female-spouse",
        "children",
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
def test_v3_deprivation_nominal_common_noun_affected_is_not_dropped(
    source: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    for unsafe in ("Лишение лицензии", changed):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        "Лишение водительского удостоверения.",
        "Лишение права управления.",
        "Лишение возможности участия.",
    ),
    ids=("adjectival-document", "right-scope", "ability-scope"),
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
def test_v3_deprivation_nominal_one_role_entitlement_stays_one_role(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))


@pytest.mark.parametrize(
    "source",
    (
        "При нарушении суд лишил гражданина лицензии.",
        "Суд лишил гражданина лицензии за нарушение закона.",
        "Суд временно лишил гражданина лицензии.",
        "Суд не лишил гражданина лицензии.",
        "Суд может лишить гражданина лицензии.",
        "По решению прокуратуры суд лишил гражданина лицензии.",
        "Суд лишил гражданина лицензии по статье 5.",
    ),
    ids=(
        "condition",
        "ground",
        "action-modifier",
        "negative",
        "may",
        "decision",
        "reference",
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
def test_v3_deprivation_finite_to_nominal_does_not_drop_source_facets(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "Лишение гражданина лицензии")


@pytest.mark.parametrize(
    "source",
    (
        "Вчера суд лишил гражданина лицензии.",
        "Суд сегодня лишил гражданина лицензии.",
        "Ранее гражданин был лишен лицензии судом.",
    ),
    ids=("leading-time", "pre-action-time", "passive-time"),
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
def test_v3_deprivation_finite_to_nominal_keeps_raw_lexical_residue(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, "Лишение гражданина лицензии")


@pytest.mark.parametrize(
    "unsafe",
    (
        "Временное лишение гражданина лицензии",
        "Лишение гражданина лицензии при нарушении",
        "Лишение гражданина лицензии за нарушение закона",
        "Лишение гражданина лицензии по статье 5",
    ),
    ids=("modifier", "condition", "ground", "reference"),
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
def test_v3_deprivation_finite_to_nominal_output_is_a_bare_topic(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = "Суд лишил гражданина лицензии."
    validate(source, "Лишение гражданина лицензии")
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        "Временное лишение гражданина лицензии.",
        "Лишение гражданина лицензии при нарушении.",
    ),
    ids=("modifier", "condition"),
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
def test_v3_deprivation_nominal_copy_does_not_drop_surrounding_facets(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, "Лишение гражданина лицензии")


@pytest.mark.parametrize(
    ("source", "masculine", "feminine", "neuter"),
    (
        (
            "Суд лишил меня лицензии.",
            "Я был лишен лицензии судом",
            "Я была лишена лицензии судом",
            "Я было лишено лицензии судом",
        ),
        (
            "Суд лишил тебя лицензии.",
            "Ты был лишен лицензии судом",
            "Ты была лишена лицензии судом",
            "Ты было лишено лицензии судом",
        ),
    ),
    ids=("first-singular", "second-singular"),
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
def test_v3_deprivation_singular_human_pronoun_rejects_neuter_passive(
    source: str,
    masculine: str,
    feminine: str,
    neuter: str,
    validate: RelationValidator,
) -> None:
    validate(source, masculine)
    validate(source, feminine)
    with pytest.raises(ValueError):
        validate(source, neuter)
