from __future__ import annotations

import pytest

from services.law_enrichment_validation import (
    _canonical_mandatory_predicate,
    _extract_strong_modal_action_claims,
    _validate_strong_modal_productive_residual_source_evidence,
    validate_penalty_source_evidence,
)
from tests.test_law_enrichment_v3_closed_uk_list_regressions import (
    ALL_OUTPUT_PATHS as BASE_OUTPUT_PATHS,
    RelationValidator,
)


def _validate_penalty_summary(source: str, output: str) -> None:
    validate_penalty_source_evidence("Наказание: " + source, output, None)


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


PATH_IDS = (
    "title",
    "single-synonym",
    "full-set",
    "penalty-summary",
    "penalty-other",
)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Суд обязан уничтожить архив.",
            "Суд должен уничтожить архив",
        ),
        (
            "Суд обязан архив уничтожить.",
            "Суд должен уничтожить архив",
        ),
        (
            "Обязан суд уничтожить архив.",
            "Суд должен уничтожить архив",
        ),
        (
            "Уничтожить архив обязан суд.",
            "Суд должен уничтожить архив",
        ),
        (
            "Архив уничтожить суд обязан.",
            "Суд должен уничтожить архив",
        ),
        (
            "Архив уничтожить обязан суд.",
            "Суд должен уничтожить архив",
        ),
        (
            "Суд обязан будет уничтожить архив.",
            "Суд должен будет уничтожить архив",
        ),
        (
            "Суд обязан был уничтожить архив.",
            "Суд должен был уничтожить архив",
        ),
        (
            "Суд обязан лично и немедленно уничтожить архив.",
            "Суд должен лично и немедленно уничтожить архив",
        ),
        (
            "Суд может уничтожить архив.",
            "Суд вправе уничтожить архив",
        ),
        (
            "Архив необходимо уничтожить.",
            "Архив следует уничтожить",
        ),
        (
            "Суду необходимо уничтожить архив.",
            "Суд обязан уничтожить архив",
        ),
        (
            "Гражданину разрешено уничтожить архив.",
            "Гражданин может уничтожить архив",
        ),
        (
            "Суду нельзя уничтожать архив.",
            "Суду запрещено уничтожать архив",
        ),
        (
            "Суд обязан явиться.",
            "Суд должен явиться",
        ),
    ),
    ids=(
        "ordinary",
        "postmodal-object-first",
        "modal-first-actor",
        "inverted-postmodal-actor",
        "inverted-premodal-actor",
        "inverted-leading-object",
        "future-auxiliary",
        "past-auxiliary",
        "coordinated-modifiers",
        "permission",
        "impersonal",
        "dative-duty-actor",
        "dative-may-actor",
        "dative-prohibition-actor",
        "intransitive",
    ),
)
def test_v3_generic_modal_supported_orders_keep_exact_roles(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Суд обязан уничтожить архив.", "Суд обязан уничтожить"),
        ("Суд обязан уничтожить архив.", "Суд обязан уничтожить запись"),
        ("Суд обязан архив уничтожить.", "Суд обязан запись уничтожить"),
        (
            "Суд обязан публично уничтожить архив.",
            "Суд обязан публично уничтожить запись",
        ),
        (
            "Суд обязан в течение суток уничтожить архив.",
            "Суд обязан в течение суток уничтожить запись",
        ),
        ("Обязан суд уничтожить архив.", "Обязан суд уничтожить запись"),
        (
            "Уничтожить архив суд обязан.",
            "Уничтожить запись суд обязан",
        ),
        (
            "Архив суд уничтожить обязан.",
            "Запись суд уничтожить обязан",
        ),
        (
            "Уничтожить обязан суд архив.",
            "Уничтожить обязан суд запись",
        ),
        (
            "Суд обязан: уничтожить архив.",
            "Суд обязан: уничтожить запись",
        ),
        (
            "Суд обязан зарегистрироваться.",
            "Суд обязан зарегистрировать",
        ),
        (
            "Суд обязан зарегистрировать архив.",
            "Суд обязан зарегистрироваться",
        ),
        (
            "Суду необходимо уничтожить архив.",
            "Прокуратура обязана уничтожить архив",
        ),
        (
            "Гражданину разрешено уничтожить архив.",
            "Гражданин может уничтожить запись",
        ),
        (
            "Суд обязан не лично уничтожить архив.",
            "Суд обязан лично уничтожить архив",
        ),
        (
            "Суд обязан не немедленно уничтожить архив.",
            "Суд обязан немедленно уничтожить архив",
        ),
    ),
    ids=(
        "drop-patient",
        "change-patient",
        "postmodal-change",
        "unknown-modifier-change",
        "bounded-time-change",
        "modal-first-change",
        "pre-modal-actor-change",
        "actor-before-action-change",
        "postmodal-actor-patient-change",
        "colon-gap-change",
        "drop-reflexivity",
        "invent-reflexivity",
        "dative-wrong-actor",
        "dative-wrong-patient",
        "modifier-negation",
        "modifier-negation-adverb",
    ),
)
def test_v3_generic_modal_never_silently_drops_unresolved_material(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд обязан показать гражданину документ.",
            "Суд обязан показать документ гражданину",
        ),
        (
            "Суд обязан показать гражданину документ.",
            "Суд обязан показать гражданин документ",
        ),
        (
            "Суд обязан показать гражданину документ.",
            "Суд обязан показать гражданину документу",
        ),
        (
            "Суд обязан показать гражданину документ.",
            "Суд обязан показать документ",
        ),
        (
            "Суд обязан показать гражданину документ.",
            "Суд обязан показать гражданину",
        ),
        (
            "Суд обязан показать гражданину документ.",
            "Суд обязан показать гражданину документ документ",
        ),
    ),
    ids=("role-order", "recipient-case", "object-case", "drop-recipient", "drop-object", "duplicate-object"),
)
def test_v3_generic_modal_multi_role_surface_is_exact_and_multiplicity_aware(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
def test_v3_generic_modal_multi_role_exact_faithful_surface_passes(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд обязан показать гражданину документ.",
        "Суд должен показать гражданину документ",
    )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Архив и документ необходимо уничтожить.",
            "Документ и архив следует уничтожить",
        ),
        (
            "Архив или документ необходимо уничтожить.",
            "Документ либо архив следует уничтожить",
        ),
        (
            "Архив, документ и запись необходимо уничтожить.",
            "Запись, архив и документ следует уничтожить",
        ),
        (
            "Архив, а также документ необходимо уничтожить.",
            "Документ и архив следует уничтожить",
        ),
        (
            "Как архив, так и документ необходимо уничтожить.",
            "Документ и архив следует уничтожить",
        ),
    ),
    ids=("and", "or", "comma-nary", "also", "correlative"),
)
def test_v3_homogeneous_modal_patient_lists_are_commutative_multisets(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Архив и документ необходимо уничтожить.",
            "Архив или документ необходимо уничтожить",
        ),
        (
            "Архив и документ необходимо уничтожить.",
            "Архив необходимо уничтожить",
        ),
        (
            "Архив, архив и документ необходимо уничтожить.",
            "Архив и документ необходимо уничтожить",
        ),
        (
            "Архив лично и немедленно необходимо уничтожить.",
            "Немедленно и архив лично необходимо уничтожить",
        ),
        (
            "Документ между судом и прокуратурой необходимо уничтожить.",
            "Прокуратурой и документ между судом необходимо уничтожить",
        ),
        (
            "Архив огнём и мечом необходимо уничтожить.",
            "Мечом и архив огнём необходимо уничтожить",
        ),
        (
            "Архив, документ необходимо уничтожить.",
            "Документ, архив необходимо уничтожить",
        ),
    ),
    ids=(
        "and-to-or",
        "drop-member",
        "drop-duplicate",
        "modifier-not-list",
        "oblique-not-list",
        "instrument-not-list",
        "two-item-apposition",
    ),
)
def test_v3_modal_boolean_contract_rejects_logic_or_role_changes(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "При аварии суд обязан уничтожить архив.",
            "Суд должен уничтожить архив при аварии",
        ),
        (
            "При нарушении суд обязан уничтожить архив.",
            "Суд должен уничтожить архив при нарушении",
        ),
        (
            "Если наступило событие суд обязан уничтожить архив.",
            "Суд должен уничтожить архив, если наступило событие",
        ),
        (
            "Чтобы закрыть дело, суд обязан уничтожить архив.",
            "Суд должен уничтожить архив, чтобы закрыть дело",
        ),
        (
            "Дабы закрыть дело, суд обязан уничтожить архив.",
            "Суд должен уничтожить архив, дабы закрыть дело",
        ),
        (
            "Для закрытия дела суд обязан уничтожить архив.",
            "Суд должен уничтожить архив для закрытия дела",
        ),
        (
            "За нарушение правил, суд обязан уничтожить архив.",
            "Суд должен уничтожить архив за нарушение правил",
        ),
    ),
    ids=(
        "event",
        "open-condition",
        "if-condition",
        "purpose",
        "purpose-daby",
        "nominal-purpose",
        "ground",
    ),
)
def test_v3_modal_context_can_move_but_cannot_disappear(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "При аварии суд обязан уничтожить архив.",
            "Суд обязан уничтожить архив",
        ),
        (
            "При нарушении суд обязан уничтожить архив.",
            "Суд обязан уничтожить архив при пожаре",
        ),
        (
            "Чтобы закрыть дело, суд обязан уничтожить архив.",
            "Суд обязан уничтожить архив",
        ),
        (
            "Суд обязан уничтожить архив, чтобы предотвратить утечку.",
            "Суд обязан предотвратить утечку",
        ),
        (
            "За нарушение правил, суд обязан уничтожить архив.",
            "Суд обязан уничтожить архив за решение суда",
        ),
        (
            "Суд обязан уничтожить архив при аварии и сжечь документ при пожаре.",
            "Суд обязан уничтожить архив при пожаре и сжечь документ при аварии",
        ),
        (
            "Суд обязан уничтожить архив при аварии и сжечь документ при пожаре.",
            "Суд обязан уничтожить архив при аварии и сжечь документ",
        ),
        (
            "За нарушение суд обязан уничтожить архив.",
            "За нарушение суд обязан уничтожить запись",
        ),
        (
            "Чтобы закрыть дело суд обязан уничтожить архив.",
            "Чтобы закрыть дело суд обязан уничтожить запись",
        ),
    ),
    ids=(
        "drop-condition",
        "change-condition",
        "drop-purpose",
        "purpose-is-not-coaction",
        "change-ground",
        "swap-local-conditions",
        "drop-second-local-condition",
        "ambiguous-ground-sentinel",
        "ambiguous-purpose-sentinel",
    ),
)
def test_v3_modal_context_is_owned_by_one_physical_action(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд обязан уничтожить архив и сжечь документ.",
            "Суд обязан уничтожить архив, а затем сжечь документ",
        ),
        (
            "Суд обязан уничтожить архив и сжечь документ.",
            "Суд обязан уничтожить архив, однако сжечь документ",
        ),
        (
            "Суд обязан уничтожить архив и сжечь документ.",
            "Суд обязан уничтожить архив, при этом сжечь документ",
        ),
        (
            "Суд обязан уничтожить или сжечь архив.",
            "Суд обязан уничтожить и сжечь архив",
        ),
        (
            "Суд обязан уничтожить архив и сжечь документ.",
            "Суд обязан уничтожить документ и сжечь архив",
        ),
    ),
    ids=("and-sequence", "and-contrast", "and-continuation", "or-and", "swap-patients"),
)
def test_v3_modal_coordination_preserves_operator_and_local_roles(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Суд не обязан уничтожить архив.", "Суд обязан не уничтожить архив"),
        ("Суд обязан не уничтожить архив.", "Суд не обязан уничтожить архив"),
        ("Суд может уничтожить архив.", "Суд обязан уничтожить архив"),
        ("Суд обязан уничтожить архив.", "Суд может уничтожить архив"),
        ("Суд мог уничтожить архив.", "Суд может уничтожить архив"),
        ("Суд может уничтожить архив.", "Суд сможет уничтожить архив"),
        ("Суд обязан был уничтожить архив.", "Суд обязан будет уничтожить архив"),
        ("Комиссия должна была уничтожить архив.", "Комиссия должна был уничтожить архив"),
        ("Комиссия должна был уничтожить архив.", "Комиссия должна были уничтожить архив"),
        ("Запрещено уничтожить архив.", "Следует уничтожить архив"),
        ("Запрещено уничтожить архив.", "Запрещено не уничтожить архив"),
        ("Нельзя уничтожать архив.", "Можно уничтожать архив"),
    ),
    ids=(
        "modal-vs-action-negation",
        "action-vs-modal-negation",
        "may-to-duty",
        "duty-to-may",
        "past-to-present",
        "present-to-future",
        "past-to-future-auxiliary",
        "past-aux-gender",
        "different-invalid-past-aux",
        "prohibition-to-duty",
        "prohibition-action-negation",
        "nelzya-to-mozhno",
    ),
)
def test_v3_modal_family_tense_and_negation_scope_are_exact(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Комиссия должна уничтожить архив.", "Комиссия должен уничтожить архив"),
        ("Комиссия должен уничтожить архив.", "Комиссия должно уничтожить архив"),
        ("Ведомство должно уничтожить архив.", "Ведомство должна уничтожить архив"),
        ("Суды обязаны уничтожить архив.", "Суды обязан уничтожить архив"),
        ("Суд обязан уничтожить архив.", "Суды обязаны уничтожить архив"),
        ("Районный суд обязан уничтожить архив.", "Верховный суд обязан уничтожить архив"),
        ("Комиссия могла уничтожить архив.", "Комиссия мог уничтожить архив"),
        ("Суды смогут уничтожить архив.", "Суды сможет уничтожить архив"),
    ),
    ids=(
        "feminine",
        "different-invalid-gender",
        "neuter",
        "plural-agreement",
        "actor-number",
        "actor-identity",
        "past-may-gender",
        "future-may-number",
    ),
)
def test_v3_modal_actor_identity_number_and_gender_cannot_be_laundered(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Суд обязан уничтожить архив.",
            "Архив должен быть уничтожен судом",
        ),
        (
            "Архив должен быть уничтожен судом.",
            "Суд обязан уничтожить архив",
        ),
        (
            "Судом архив должен быть уничтожен.",
            "Суд обязан уничтожить архив",
        ),
        (
            "Архив судом должен быть уничтожен.",
            "Суд обязан уничтожить архив",
        ),
        (
            "Архив должен судом быть уничтожен.",
            "Суд обязан уничтожить архив",
        ),
        (
            "Архив должен быть судом уничтожен.",
            "Суд обязан уничтожить архив",
        ),
        (
            "Суд обязан уничтожить лицензию.",
            "Лицензия должна быть уничтожена судом",
        ),
        (
            "Суд обязан уничтожить архивы.",
            "Архивы должны быть уничтожены судом",
        ),
        (
            "Суд обязан уничтожить письмо.",
            "Письмо должно быть уничтожено судом",
        ),
        (
            "Суд обязан уничтожить документ комиссии.",
            "Документ комиссии должен быть уничтожен судом",
        ),
    ),
    ids=("active-passive", "passive-active", "actor-first", "patient-first", "actor-before-aux", "actor-before-participle", "feminine", "plural", "neuter", "owner-tail"),
)
def test_v3_modal_passive_accepts_only_proved_role_layouts(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Архив должен быть уничтожен судом.", "Архивы должен быть уничтожен судом"),
        ("Лицензия должна быть уничтожена судом.", "Лицензия должен быть уничтожен судом"),
        ("Документ комиссии должен быть уничтожен.", "Документ комиссии должна быть уничтожена"),
        ("Архив должен быть уничтожен судом.", "Архив должен быть уничтожен прокуратурой"),
        ("Архив должен быть уничтожен судом.", "Запись должна быть уничтожена судом"),
        ("Архив должен быть уничтожен судом.", "Архив должен быть уничтожен суд"),
    ),
    ids=("number", "feminine", "owner-tail-laundering", "actor", "patient", "actor-case"),
)
def test_v3_modal_passive_keeps_patient_actor_and_observed_agreement(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        ("Суд выдал лицензию.", "Лицензия выдана судом"),
        ("Лицензия выдана судом.", "Суд выдал лицензию"),
        ("Суд выдаёт лицензию.", "Лицензия выдаётся судом"),
        ("Лицензия выдаётся судом.", "Суд выдаёт лицензию"),
    ),
    ids=("past-active-passive", "past-passive-active", "present-active-passive", "present-passive-active"),
)
def test_v3_definite_family_accepts_only_proved_cross_voice_paraphrases(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Суд выдал лицензию.", "Лицензия выдана прокуратурой"),
        ("Суд выдал лицензию.", "Ордер выдан судом"),
        ("Суд выдал лицензию.", "Лицензия выдаётся судом"),
        ("Лицензия выдаётся судом.", "Суд выдал лицензию"),
    ),
    ids=("actor", "patient", "past-to-present", "present-to-past"),
)
def test_v3_definite_cross_voice_keeps_actor_patient_and_tense(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Суд обязан уничтожить архив.", "Суд уничтожает архив"),
        ("Суд может уничтожить архив.", "Суд уничтожает архив"),
        ("Суд обязан сообщить адрес.", "Суд сообщает адрес"),
        ("Суд обязан выдать лицензию.", "Суд выдаёт лицензию"),
        ("Суд обязан выдать лицензию.", "Суд может выдать лицензию"),
        ("Суд может выдать лицензию.", "Суд обязан выдать лицензию"),
        ("Суд уничтожает архив.", "Прокуратура уничтожает документ"),
        (
            "Суд обязан выдать лицензию. Прокуратура может выдать ордер.",
            "Суд может выдать лицензию",
        ),
        (
            "Суд обязан отозвать лицензию компании Альфа. Суд может отозвать лицензию компании Бета.",
            "Суд может отозвать лицензию компании Альфа",
        ),
        (
            "Суд обязан лишить гражданина лицензии. Суд может лишить гражданина пропуска.",
            "Суд может лишить гражданина лицензии",
        ),
        (
            "Суд обязан уничтожить архив, чтобы комиссия проверила запись, но прокуратура сжигает документ.",
            "Суд обязан уничтожить архив, чтобы комиссия проверила запись, но прокуратура сжигает журнал",
        ),
    ),
    ids=(
        "open-duty-to-fact",
        "open-may-to-fact",
        "mature-inform-duty-to-fact",
        "mature-issue-duty-to-fact",
        "mature-duty-to-may",
        "mature-may-to-duty",
        "finite-roles",
        "cross-occurrence-modality",
        "cross-occurrence-owner",
        "cross-occurrence-deprivation-pair",
        "subordinate-does-not-own-later-finite",
    ),
)
def test_v3_explicit_modality_is_bound_to_one_physical_occurrence(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Архив подлежит уничтожению.", "Запись подлежит уничтожению"),
        ("Архив не подлежит уничтожению.", "Архив подлежит уничтожению"),
        ("Суд обязуется уничтожить архив.", "Прокуратура обязуется уничтожить архив"),
        ("Суд обязуется уничтожить архив.", "Суд обязуется уничтожить запись"),
        ("Обязательно уничтожить архив.", "Обязательно уничтожить запись"),
        ("Уничтожение архива обязательно.", "Уничтожение записи обязательно"),
        ("Нельзя суду уничтожать архив.", "Нельзя прокуратуре уничтожать архив"),
        ("Нельзя уничтожать архив.", "Нельзя уничтожать запись"),
        (
            "На суд не возлагается обязанность уничтожить архив.",
            "На суд не возлагается обязанность уничтожить запись",
        ),
        (
            "На суд возложена обязанность уничтожить архив.",
            "На прокуратуру возложена обязанность уничтожить архив",
        ),
        (
            "Обязанность уничтожить архив возложена на суд.",
            "Обязанность уничтожить запись возложена на суд",
        ),
        (
            "Необходимо, чтобы суд уничтожил архив.",
            "Необходимо, чтобы суд уничтожил запись",
        ),
    ),
    ids=(
        "podlezhit-patient",
        "podlezhit-polarity",
        "obyazuetsya-actor",
        "obyazuetsya-patient",
        "standalone-obyazatelno",
        "nominal-obyazatelno",
        "nelzya-actor",
        "nelzya-patient",
        "negated-imposition",
        "past-imposition-actor",
        "inverse-imposition-patient",
        "finite-complement",
    ),
)
def test_v3_unparsed_common_modal_grammars_fail_closed_exactly(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "value",
    (
        "Обязанность суда — уничтожить архив",
        "В обязанности суда входит уничтожение архива",
        "Суд имеет обязанность уничтожить архив",
    ),
    ids=("dash-infinitive", "enters-nominal", "has-duty"),
)
def test_v3_noun_duty_exact_positive_controls(
    value: str,
    validate: RelationValidator,
) -> None:
    validate(value + ".", value)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "invented",
    (
        "Обязанность суда — уничтожить архив",
        "В обязанности суда входит уничтожение архива",
        "Суд имеет обязанность уничтожить архив",
    ),
    ids=("dash-infinitive", "enters-nominal", "has-duty"),
)
def test_v3_noun_duty_is_never_source_free(
    invented: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Настоящая статья определяет порядок рассмотрения дела.",
            invented,
        )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Обязанность суда — уничтожить архив.",
            "Обязанность прокуратуры — уничтожить архив",
        ),
        (
            "Обязанность суда — уничтожить архив.",
            "Обязанность суда — уничтожить запись",
        ),
        (
            "Обязанность суда — уничтожить архив.",
            "Обязанность суда",
        ),
        (
            "В обязанности суда входит уничтожение архива.",
            "В обязанности суда входит уничтожение записи",
        ),
    ),
    ids=("actor", "patient", "drop-complement", "nominal-patient"),
)
def test_v3_noun_duty_never_changes_or_drops_operative_content(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
def test_v3_bare_rights_and_duties_heading_remains_topical(
    validate: RelationValidator,
) -> None:
    validate("Права и обязанности суда.", "Права и обязанности суда")


def test_v3_bare_rights_and_duties_heading_has_no_modal_claim() -> None:
    claims = _extract_strong_modal_action_claims("Права и обязанности суда.")
    assert not any(
        claim.object == "unresolved_noun_duty_clause" for claim in claims
    )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "query",
    (
        "Кто обязан явиться?",
        "Что суд обязан уничтожить?",
        "Может ли суд уничтожить архив?",
        "Подлежит ли архив уничтожению?",
        "Нельзя ли уничтожить архив?",
    ),
)
def test_v3_nonassertive_modal_queries_do_not_become_legal_facts(
    query: str,
    validate: RelationValidator,
) -> None:
    neutral = "Настоящая статья определяет порядок рассмотрения дела."
    validate(neutral, query)


def test_v3_auxiliary_byt_is_never_projected_as_the_legal_action() -> None:
    claims = _extract_strong_modal_action_claims(
        "Архив должен быть уничтожен судом."
    )
    assert not any(
        claim.action == "strong_modal_action" and claim.object == "бы"
        for claim in claims
    )


@pytest.mark.parametrize(
    "value",
    (
        "Суд обязан уничтожить архив.",
        "Суд может уничтожить архив.",
        "Архив должен быть уничтожен судом.",
        "Архив подлежит уничтожению.",
        "Суд обязуется уничтожить архив.",
    ),
    ids=("duty", "permission", "passive", "podlezhit", "obyazuetsya"),
)
def test_v3_modal_governors_and_owned_actions_are_not_standalone_facts(
    value: str,
) -> None:
    claims = _extract_strong_modal_action_claims(value)
    assert not any(
        claim.action == "strong_modal_family" and claim.modality == "definite"
        for claim in claims
    )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        ("Суд дал гражданину документ.", "Суд дал гражданину документ"),
        ("Суд взял документ.", "Суд взял документ"),
        ("Гражданин пришёл.", "Гражданин пришёл"),
    ),
    ids=("dal", "vzyal", "prishel-intransitive"),
)
def test_v3_irregular_finite_facts_keep_exact_positive_controls(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд дал гражданину документ",
        ),
        (
            "Суд дал гражданину документ.",
            "Прокуратура дала гражданину документ",
        ),
        (
            "Суд дал гражданину документ.",
            "Суд дал гражданин документ",
        ),
        (
            "Суд дал гражданину документ.",
            "Суд дал гражданину запись",
        ),
        ("Суд дал гражданину документ.", "Суд дал гражданину"),
    ),
    ids=("source-free", "actor", "recipient-case", "object", "drop-object"),
)
def test_v3_irregular_finite_facts_never_borrow_or_drop_roles(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд обязан уничтожить архив, чтобы комиссия проверила запись и утилизировала документ.",
            "Комиссия утилизировала документ",
        ),
        (
            "Чтобы комиссия утилизировала документ, суд обязан уничтожить архив.",
            "Комиссия утилизировала документ",
        ),
        (
            "Заявление подано, чтобы суд уничтожил архив.",
            "Суд уничтожил архив",
        ),
        (
            "Если комиссия проверила запись, суд уничтожил архив.",
            "Комиссия проверила запись",
        ),
    ),
    ids=("second-postmodal", "premodal", "nonmodal-purpose", "leading-condition"),
)
def test_v3_subordinate_finite_predicates_never_become_standalone_facts(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Заявление подано, чтобы суд уничтожил архив.",
            "Заявление подано, чтобы суд уничтожил архив",
        ),
        (
            "Суд обязан уничтожить архив, чтобы комиссия проверила запись, но прокуратура сжигает документ.",
            "Прокуратура сжигает документ",
        ),
        (
            "Комиссия утилизировала документ, а суд обязан уничтожить архив.",
            "Комиссия утилизировала документ",
        ),
    ),
    ids=("exact-purpose", "later-independent", "earlier-independent"),
)
def test_v3_subordinate_fence_preserves_proved_independent_facts(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
def test_v3_subordinate_fence_keeps_later_independent_roles_exact(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд обязан уничтожить архив, чтобы комиссия проверила запись, "
        "но прокуратура сжигает документ."
    )
    with pytest.raises(ValueError):
        validate(source, "Прокуратура сжигает журнал")


@pytest.mark.parametrize(
    ("value", "subordinate_predicates", "has_independent"),
    (
        (
            "Суд обязан уничтожить архив, чтобы комиссия проверила запись и утилизировала документ.",
            ("проверила", "утилизировала"),
            False,
        ),
        (
            "Чтобы комиссия утилизировала документ, суд обязан уничтожить архив.",
            ("утилизировала",),
            False,
        ),
        (
            "Заявление подано, чтобы суд уничтожил архив.",
            ("уничтожил",),
            True,
        ),
        (
            "Суд обязан уничтожить архив, чтобы комиссия проверила запись, но прокуратура сжигает документ.",
            ("проверила",),
            True,
        ),
    ),
    ids=("two-postmodal", "premodal", "nonmodal-purpose", "later-independent"),
)
def test_v3_subordinate_finite_ownership_is_physical(
    value: str,
    subordinate_predicates: tuple[str, ...],
    has_independent: bool,
) -> None:
    claims = _extract_strong_modal_action_claims(value)
    definite_objects = {
        claim.object
        for claim in claims
        if claim.action == "strong_modal_family" and claim.modality == "definite"
    }
    assert all(
        _canonical_mandatory_predicate(predicate) not in definite_objects
        for predicate in subordinate_predicates
    )
    assert bool(definite_objects) is has_independent


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        ("Суд пишет документ.", "Суд пишет документ"),
        ("Суд бережёт архив.", "Суд бережёт архив"),
        ("Суд пишет предмет.", "Суд пишет предмет"),
        ("Суд пишет билет.", "Суд пишет билет"),
        ("Суд пишет макет.", "Суд пишет макет"),
        ("Суд пишет печать.", "Суд пишет печать"),
        ("Суд пишет лицензию.", "Суд пишет лицензию"),
        ("Суд пишет новую записку.", "Суд пишет новую записку"),
        ("Суд пишет новое заключение.", "Суд пишет новое заключение"),
    ),
    ids=(
        "pishet",
        "berezhet",
        "predmet-patient",
        "bilet-patient",
        "maket-patient",
        "pechat-patient",
        "typed-license-patient",
        "explicit-feminine-accusative",
        "explicit-neuter-accusative",
    ),
)
def test_v3_productive_et_finite_keeps_exact_positive_controls(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд пишет документ",
        ),
        ("Суд пишет документ.", "Прокуратура пишет документ"),
        ("Суд пишет документ.", "Суд пишет запись"),
        ("Суд пишет документ.", "Суд пишет"),
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд пишет предмет",
        ),
        ("Суд пишет предмет.", "Прокуратура пишет предмет"),
        ("Суд пишет предмет.", "Суд пишет билет"),
        ("Суд пишет предмет.", "Суд пишет"),
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд пишет печать",
        ),
        ("Суд пишет печать.", "Прокуратура пишет печать"),
        ("Суд пишет печать.", "Суд пишет макет"),
        ("Суд пишет печать.", "Суд пишет"),
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд бережёт архив",
        ),
    ),
    ids=(
        "source-free",
        "actor",
        "patient",
        "drop-patient",
        "predmet-source-free",
        "predmet-actor",
        "predmet-patient",
        "predmet-drop-patient",
        "pechat-source-free",
        "pechat-actor",
        "pechat-patient",
        "pechat-drop-patient",
        "yo-source-free",
    ),
)
def test_v3_productive_et_finite_never_borrows_or_drops_roles(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("value", "noun"),
    (
        ("Предмет регулирования.", "предмет"),
        ("Билет гражданина.", "билет"),
        ("Суд: предмет регулирования.", "предмет"),
        ("Суд предмет рассматривает.", "предмет"),
        ("Суд макет документа.", "макет"),
        ("Суд жилет сотрудника.", "жилет"),
        ("Суд буклет организации.", "буклет"),
        ("Суд браслет заключённого.", "браслет"),
        ("Суд омлет гражданина.", "омлет"),
        ("Суд кадет корпуса.", "кадет"),
        ("Суд ответ заявителю.", "ответ"),
        ("Суд совет гражданину.", "совет"),
        ("Суд ответ заявительнице.", "ответ"),
        ("Суд ответ судье.", "ответ"),
        ("Суд совет матери.", "совет"),
        ("Суд макет зданий.", "макет"),
        ("Суд ответ дел.", "ответ"),
        ("Суд совет прав.", "совет"),
    ),
    ids=(
        "bare-subject",
        "bare-ticket",
        "tempting-heading",
        "object-first",
        "unlisted-maket",
        "unlisted-zhilet",
        "unlisted-buklet",
        "unlisted-braslet",
        "unlisted-omlet",
        "unlisted-kadet",
        "dative-u",
        "dative-yu",
        "dative-nitse",
        "dative-ye",
        "dative-i",
        "genitive-plural-iy",
        "zero-genitive-del",
        "zero-genitive-prav",
    ),
)
def test_v3_productive_et_never_projects_topical_nouns(
    value: str,
    noun: str,
) -> None:
    claims = _extract_strong_modal_action_claims(value)
    assert not any(
        claim.action == "strong_modal_family"
        and claim.object == _canonical_mandatory_predicate(noun)
        for claim in claims
    )


@pytest.mark.parametrize(
    ("value", "kept", "rejected"),
    (
        (
            "Суд предмет пишет документ.",
            "пишет",
            "предмет",
        ),
        (
            "Суд предмет рассматривает.",
            "рассматривает",
            "предмет",
        ),
        (
            "Суд предмет внимательно рассматривает.",
            "рассматривает",
            "предмет",
        ),
        (
            "Суд пишет печать.",
            "пишет",
            "печать",
        ),
    ),
    ids=(
        "same-class-sov",
        "mature-direct",
        "mature-after-adverb",
        "infinitive-shaped-patient",
    ),
)
def test_v3_productive_et_competing_finite_owns_the_action_slot(
    value: str,
    kept: str,
    rejected: str,
) -> None:
    claims = _extract_strong_modal_action_claims(value)
    definite_objects = {
        claim.object
        for claim in claims
        if claim.action == "strong_modal_family" and claim.modality == "definite"
    }
    assert _canonical_mandatory_predicate(kept) in definite_objects
    assert _canonical_mandatory_predicate(rejected) not in definite_objects


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Суд предмет пишет документ.",
            "Суд предмет пишет документ",
        ),
        (
            "Суд предмет внимательно рассматривает.",
            "Суд предмет внимательно рассматривает",
        ),
    ),
    ids=("same-class-sov", "mature-after-adverb"),
)
def test_v3_productive_et_competing_finite_exact_controls(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд предмет пишет документ",
        ),
        (
            "Суд предмет пишет документ.",
            "Прокуратура предмет пишет документ",
        ),
        (
            "Суд предмет пишет документ.",
            "Суд предмет пишет архив",
        ),
        (
            "Суд предмет пишет документ.",
            "Суд предмет пишет",
        ),
    ),
    ids=("source-free", "actor", "patient", "drop-patient"),
)
def test_v3_productive_et_same_class_sov_keeps_roles_exact(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    ("value", "connector"),
    (
        ("Суд пишет и рассматривает документ.", "and-forward"),
        ("Суд пишет или рассматривает документ.", "or-forward"),
        ("Суд рассматривает и пишет документ.", "and-reverse"),
        ("Суд рассматривает либо пишет документ.", "or-reverse"),
        (
            "Суд рассматривает документ и пишет заключение.",
            "later-own-patient",
        ),
        (
            "Суд пишет, а также рассматривает документ.",
            "comma-also-forward",
        ),
        (
            "Суд пишет, и рассматривает документ.",
            "comma-and-forward",
        ),
        (
            "Суд рассматривает документ, а прокуратура пишет заключение.",
            "independent-subject",
        ),
    ),
    ids=(
        "and-forward",
        "or-forward",
        "and-reverse",
        "or-reverse",
        "own-patient",
        "comma-also-forward",
        "comma-and-forward",
        "independent-subject",
    ),
)
def test_v3_productive_et_tight_coordination_emits_every_action(
    value: str,
    connector: str,
) -> None:
    del connector
    claims = _extract_strong_modal_action_claims(value)
    definite_objects = {
        claim.object
        for claim in claims
        if claim.action == "strong_modal_family" and claim.modality == "definite"
    }
    assert _canonical_mandatory_predicate("пишет") in definite_objects
    assert _canonical_mandatory_predicate("рассматривает") in definite_objects


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "value",
    (
        "Суд пишет и рассматривает документ",
        "Суд пишет или рассматривает документ",
        "Суд рассматривает и пишет документ",
        "Суд рассматривает либо пишет документ",
        "Суд рассматривает документ и пишет заключение",
        "Суд пишет, а также рассматривает документ",
        "Суд пишет, и рассматривает документ",
        "Суд рассматривает документ, а прокуратура пишет заключение",
    ),
    ids=(
        "and-forward",
        "or-forward",
        "and-reverse",
        "or-reverse",
        "own-patient",
        "comma-also-forward",
        "comma-and-forward",
        "independent-subject",
    ),
)
def test_v3_productive_et_tight_coordination_exact_controls(
    value: str,
    validate: RelationValidator,
) -> None:
    validate(value + ".", value)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд рассматривает документ.",
            "Суд пишет и рассматривает документ",
        ),
        (
            "Суд рассматривает документ.",
            "Суд рассматривает и пишет документ",
        ),
        (
            "Суд рассматривает документ.",
            "Суд пишет или рассматривает документ",
        ),
        (
            "Суд рассматривает документ.",
            "Суд пишет, а также рассматривает документ",
        ),
        (
            "Суд рассматривает документ.",
            "Суд пишет, и рассматривает документ",
        ),
        (
            "Суд рассматривает документ.",
            "Суд рассматривает документ, а прокуратура пишет заключение",
        ),
        (
            "Суд пишет и рассматривает документ.",
            "Прокуратура пишет и рассматривает документ",
        ),
        (
            "Суд пишет и рассматривает документ.",
            "Суд пишет и рассматривает архив",
        ),
        (
            "Суд пишет и рассматривает документ.",
            "Суд рассматривает документ",
        ),
        (
            "Суд рассматривает документ и пишет заключение.",
            "Суд рассматривает документ и пишет архив",
        ),
        (
            "Суд рассматривает документ, а прокуратура пишет заключение.",
            "Суд рассматривает документ, а комиссия пишет заключение",
        ),
        (
            "Суд рассматривает документ, а прокуратура пишет заключение.",
            "Суд рассматривает документ, а прокуратура пишет архив",
        ),
    ),
    ids=(
        "source-free-forward",
        "source-free-reverse",
        "source-free-alternative",
        "source-free-comma-also",
        "source-free-comma-and",
        "source-free-independent",
        "actor",
        "shared-patient",
        "drop-action",
        "later-own-patient",
        "independent-actor",
        "independent-patient",
    ),
)
def test_v3_productive_et_tight_coordination_keeps_roles_exact(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Суд ответ заявителю.", "Суд ответ архив"),
        ("Суд совет гражданину.", "Суд совет архив"),
        ("Суд ответ заявительнице.", "Суд ответ архив"),
        ("Суд ответ судье.", "Суд ответ архив"),
        ("Суд совет матери.", "Суд совет архив"),
        ("Суд макет зданий.", "Суд макет архив"),
        ("Суд ответ дел.", "Суд ответ архив"),
        ("Суд совет прав.", "Суд совет архив"),
    ),
    ids=(
        "dative-u",
        "dative-yu",
        "dative-nitse",
        "dative-ye",
        "dative-i",
        "genitive-plural-iy",
        "zero-genitive-del",
        "zero-genitive-prav",
    ),
)
def test_v3_productive_et_oblique_noun_never_supports_action(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "topical",
    (
        "Предмет регулирования",
        "Билет гражданина",
        "Суд: предмет регулирования",
        "Суд предмет рассматривает",
        "Суд макет документа",
        "Суд жилет сотрудника",
        "Суд буклет организации",
        "Суд браслет заключённого",
        "Суд омлет гражданина",
        "Суд кадет корпуса",
        "Суд ответ заявителю",
        "Суд совет гражданину",
        "Суд ответ заявительнице",
        "Суд ответ судье",
        "Суд совет матери",
        "Суд макет зданий",
        "Суд ответ дел",
        "Суд совет прав",
    ),
    ids=(
        "bare-subject",
        "bare-ticket",
        "tempting-heading",
        "object-first",
        "unlisted-maket",
        "unlisted-zhilet",
        "unlisted-buklet",
        "unlisted-braslet",
        "unlisted-omlet",
        "unlisted-kadet",
        "dative-u",
        "dative-yu",
        "dative-nitse",
        "dative-ye",
        "dative-i",
        "genitive-plural-iy",
        "zero-genitive-del",
        "zero-genitive-prav",
    ),
)
def test_v3_productive_et_topical_controls_remain_unchanged(
    topical: str,
    validate: RelationValidator,
) -> None:
    source = "Ответственность: " + topical
    validate(source + ".", source)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Суд должен был уничтожить лицензию.",
            "Лицензия должна была быть уничтожена судом",
        ),
        (
            "Лицензия должна была быть уничтожена судом.",
            "Суд должен был уничтожить лицензию",
        ),
        (
            "Суд должен будет уничтожить лицензию.",
            "Лицензия должна будет быть уничтожена судом",
        ),
    ),
    ids=("past-active-passive", "past-passive-active", "future-active-passive"),
)
def test_v3_valid_tense_auxiliary_is_voice_neutral(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд должен был уничтожить лицензию.",
            "Лицензия должна был быть уничтожена судом",
        ),
        (
            "Лицензия должна была быть уничтожена судом.",
            "Лицензия должна были быть уничтожена судом",
        ),
        (
            "Лицензия должна был быть уничтожена судом.",
            "Лицензия должна были быть уничтожена судом",
        ),
    ),
    ids=("correct-to-invalid", "correct-to-invalid-plural", "invalid-laundering"),
)
def test_v3_invalid_tense_auxiliary_keeps_raw_agreement_state(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Суд мог уничтожить архив.",
            "Суд мог был уничтожить архив",
        ),
        (
            "Суды могли уничтожить архив.",
            "Суды могли были уничтожить архив",
        ),
        (
            "Суд сможет уничтожить архив.",
            "Суд сможет будет уничтожить архив",
        ),
        (
            "Суды смогут уничтожить архив.",
            "Суды смогут будут уничтожить архив",
        ),
        (
            "Суд имел право уничтожить архив.",
            "Суд имел право был уничтожить архив",
        ),
    ),
    ids=(
        "past-singular",
        "past-plural",
        "future-singular",
        "future-plural",
        "past-right",
    ),
)
def test_v3_lexically_tensed_modal_rejects_double_tense_auxiliary(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "value",
    (
        "Суд мог был уничтожить архив",
        "Суды смогут будут уничтожить архив",
    ),
    ids=("past", "future"),
)
def test_v3_double_tense_malformed_source_is_exact_only(
    value: str,
    validate: RelationValidator,
) -> None:
    validate(value + ".", value)


def test_v3_mature_present_passive_uses_the_proved_family_predicate() -> None:
    active = _extract_strong_modal_action_claims("Суд выдаёт лицензию.")
    passive = _extract_strong_modal_action_claims(
        "Лицензия выдаётся судом."
    )
    active_objects = {
        claim.object
        for claim in active
        if claim.action == "strong_modal_family" and claim.modality == "definite"
    }
    passive_objects = {
        claim.object
        for claim in passive
        if claim.action == "strong_modal_family" and claim.modality == "definite"
    }
    assert "regulated_issue" in active_objects
    assert "regulated_issue" in passive_objects
    assert "regulated_issue:reflexive" not in passive_objects


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Архив уничтожается судом.",
            "Архив уничтожается судом",
        ),
    ),
    ids=("exact-reflexive",),
)
def test_v3_unproved_generic_reflexive_remains_exact_only(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        ("Суд уничтожает архив.", "Архив уничтожается судом"),
        ("Архив уничтожается судом.", "Суд уничтожает архив"),
    ),
    ids=("active-to-unproved-reflexive", "unproved-reflexive-to-active"),
)
def test_v3_generic_reflexive_is_not_guessed_as_passive(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


def test_v3_penalty_transport_label_does_not_change_modal_claims() -> None:
    bodies = (
        "Суд обязан уничтожить архив.",
        "Суд пишет документ.",
        "Обязанность суда — уничтожить архив.",
    )
    for body in bodies:
        expected = _extract_strong_modal_action_claims(body)
        assert _extract_strong_modal_action_claims(
            "Наказание: " + body
        ) == expected
        assert _extract_strong_modal_action_claims(
            "Санкция: " + body
        ) == expected


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "invented",
    (
        "Суд обязан уничтожить архив",
        "Суд уничтожает архив",
        "Архив подлежит уничтожению",
        "Суд обязуется уничтожить архив",
        "Нельзя уничтожать архив",
        "Обязательно уничтожить архив",
        "На суд возложена обязанность уничтожить архив",
    ),
    ids=("modal", "finite", "podlezhit", "obyazuetsya", "nelzya", "obyazatelno", "imposed"),
)
def test_v3_source_free_strict_modal_families_reject_every_path(
    invented: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Настоящая статья определяет порядок рассмотрения дела.",
            invented,
        )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "value",
    (
        "Суд уничтожает архив",
        "Архив подлежит уничтожению",
        "Архив не подлежит уничтожению",
        "Суд обязуется уничтожить архив",
        "Нельзя уничтожать архив",
        "Обязательно уничтожить архив",
        "На суд возложена обязанность уничтожить архив",
        "На суд не возлагается обязанность уничтожить архив",
    ),
    ids=("finite", "podlezhit", "podlezhit-negative", "obyazuetsya", "nelzya", "obyazatelno", "imposed", "imposed-negative"),
)
def test_v3_strict_modal_family_exact_positive_controls(
    value: str,
    validate: RelationValidator,
) -> None:
    validate(value + ".", value)


@pytest.mark.parametrize(
    ("value", "case"),
    (
        ("Суд пишет.", "subject-incomplete"),
        ("Пишет документ.", "orphan-patient"),
        ("Суд пишет, или рассматривает документ.", "alternative-forward"),
        ("Суд пишет, но рассматривает документ.", "conjunctive-forward"),
        ("Суд пишет, далее рассматривает документ.", "sequence-forward"),
        (
            "Суд пишет и затем рассматривает документ.",
            "compound-sequence-forward",
        ),
        (
            "Суд пишет, после чего рассматривает документ.",
            "after-which-forward",
        ),
        ("Суд пишет, однако рассматривает документ.", "contrast-forward"),
        ("Суд пишет, причём рассматривает документ.", "continuation-forward"),
        (
            "Суд пишет и внимательно рассматривает документ.",
            "modifier-forward",
        ),
        (
            "Суд пишет, а также внимательно рассматривает документ.",
            "comma-modifier-forward",
        ),
        (
            "Суд пишет, далее прокуратура рассматривает документ.",
            "foreign-subject-forward",
        ),
        ("Суд рассматривает документ, а пишет заключение.", "reverse-a"),
        ("Суд рассматривает документ, но пишет заключение.", "reverse-no"),
        (
            "Суд рассматривает документ, далее пишет заключение.",
            "reverse-sequence",
        ),
        (
            "Суд рассматривает документ, при этом пишет заключение.",
            "reverse-continuation",
        ),
        (
            "Суд рассматривает документ, или прокуратура пишет заключение.",
            "local-alternative",
        ),
        (
            "Суд рассматривает документ, а также прокуратура пишет заключение.",
            "local-conjunctive",
        ),
        (
            "Суд рассматривает документ, однако прокуратура пишет заключение.",
            "local-contrast",
        ),
        ("Суд документ пишет и рассматривает.", "shared-sov"),
        (
            "Суд документ пишет, а также рассматривает.",
            "shared-sov-comma",
        ),
    ),
    ids=(
        "subject-incomplete",
        "orphan-patient",
        "alternative-forward",
        "conjunctive-forward",
        "sequence-forward",
        "compound-sequence-forward",
        "after-which-forward",
        "contrast-forward",
        "continuation-forward",
        "modifier-forward",
        "comma-modifier-forward",
        "foreign-subject-forward",
        "reverse-a",
        "reverse-no",
        "reverse-sequence",
        "reverse-continuation",
        "local-alternative",
        "local-conjunctive",
        "local-contrast",
        "shared-sov",
        "shared-sov-comma",
    ),
)
def test_v3_productive_et_strict_slots_keep_full_clause_digest(
    value: str,
    case: str,
) -> None:
    del case
    claims = _extract_strong_modal_action_claims(value)
    predicate = _canonical_mandatory_predicate("пишет")
    matching = {
        claim
        for claim in claims
        if claim.action == "strong_modal_family"
        and claim.modality == "definite"
        and claim.object == predicate
    }
    assert matching
    assert all(
        any(
            qualifier.startswith("unresolved_finite_clause:v1:")
            for qualifier in claim.qualifiers
        )
        for claim in matching
    )


@pytest.mark.parametrize(
    ("value", "noun"),
    (
        ("Суд макет документа.", "макет"),
        ("Суд ответ заявителю.", "ответ"),
        ("Суд совет гражданину.", "совет"),
        ("Суд макет зданий.", "макет"),
        ("Суд ответ дел.", "ответ"),
    ),
    ids=(
        "genitive-owner",
        "dative-recipient",
        "dative-person",
        "genitive-plural",
        "zero-genitive",
    ),
)
def test_v3_productive_et_strict_slot_tail_never_promotes_topical_noun(
    value: str,
    noun: str,
) -> None:
    claims = _extract_strong_modal_action_claims(value)
    assert not any(
        claim.action == "strong_modal_family"
        and claim.object == _canonical_mandatory_predicate(noun)
        for claim in claims
    )


@pytest.mark.parametrize(
    "safe_output",
    (
        "Пишет?",
        "Кто пишет?",
        "«Пишет»",
        "(Пишет)",
        "Предмет регулирования",
        "Макет документа",
        "Ответ заявителю",
    ),
    ids=(
        "question",
        "wh-question",
        "quoted",
        "parenthesized",
        "predmet",
        "maket",
        "otvet",
    ),
)
def test_v3_productive_et_bare_residual_guard_ignores_nonassertions(
    safe_output: str,
) -> None:
    _validate_strong_modal_productive_residual_source_evidence(
        "Суд пишет документ.",
        safe_output,
        field="direct-test",
    )


@pytest.mark.parametrize(
    "unsafe_output",
    ("Пишет", "Немедленно пишет"),
    ids=("bare", "owned-modifier"),
)
def test_v3_productive_et_bare_residual_guard_rejects_role_erasure(
    unsafe_output: str,
) -> None:
    with pytest.raises(ValueError):
        _validate_strong_modal_productive_residual_source_evidence(
            "Суд пишет документ.",
            unsafe_output,
            field="direct-test",
        )


@pytest.mark.parametrize(
    ("value", "case"),
    (
        ("Суд внимательно пишет.", "post-actor-modifier"),
        ("Внимательно суд пишет.", "pre-actor-modifier"),
        ("Суд не пишет.", "actor-negation"),
        ("Не пишет.", "actorless-negation"),
        ("Не суд пишет.", "negation-before-actor"),
        ("Пишет суд.", "postposed-actor"),
        ("Пишет — суд.", "postposed-actor-dash"),
        ("Пишет, суд.", "postposed-actor-comma"),
        ("Бюро пишет.", "bureau-actor-collision"),
        ("Лицо пишет.", "person-actor-collision"),
        ("Министерство пишет.", "ministry-actor-collision"),
        (
            "Суд внимательно документ пишет.",
            "modifier-before-sov-patient",
        ),
        (
            "Суд документ внимательно пишет.",
            "modifier-after-sov-patient",
        ),
        ("Не суд документ пишет.", "negation-before-actor-sov-patient"),
        ("Бюро внимательно пишет.", "bureau-modifier-collision"),
        ("Лицо официально пишет.", "person-modifier-collision"),
        (
            "Министерство временно пишет.",
            "ministry-modifier-collision",
        ),
        ("Немедленно пишет.", "known-modifier-before-action"),
        ("Пишет немедленно.", "known-modifier-after-action"),
        ("Внимательно пишет.", "unknown-modifier-before-action"),
        ("Пишет внимательно.", "unknown-modifier-after-action"),
    ),
)
def test_v3_productive_et_structured_incomplete_occurrence_is_strict(
    value: str,
    case: str,
) -> None:
    del case
    predicate = _canonical_mandatory_predicate("пишет")
    matching = {
        claim
        for claim in _extract_strong_modal_action_claims(value)
        if claim.action == "strong_modal_family"
        and claim.object == predicate
        and claim.modality == "definite"
    }
    assert matching
    assert all(
        any(
            qualifier.startswith("unresolved_finite_clause:v1:")
            for qualifier in claim.qualifiers
        )
        for claim in matching
    )


@pytest.mark.parametrize(
    "value",
    (
        "«Пишет».",
        "(Пишет).",
        "Кто пишет?",
        "Пишет?",
        "Чтобы комиссия пишет документ.",
    ),
    ids=(
        "quoted-action",
        "parenthesized-action",
        "wh-query",
        "bare-query",
        "subordinate",
    ),
)
def test_v3_productive_et_action_local_nonassertions_remain_nonclaims(
    value: str,
) -> None:
    predicate = _canonical_mandatory_predicate("пишет")
    assert not any(
        claim.action == "strong_modal_family"
        and claim.object == predicate
        for claim in _extract_strong_modal_action_claims(value)
    )


def test_v3_productive_et_epistemic_frame_is_carried_not_dropped() -> None:
    """Хедж — не молчание, а признак, который обязан совпасть.

    Раньше «Возможно, суд пишет документ.» не давало НИ ОДНОГО claim, и это
    считалось контрактом. На стороне ИСТОЧНИКА такой дроп консервативен, но на
    стороне ВЫВОДА он fail-open: ноль claim-ов означает, что сверять нечего, и
    приставка «Возможно,» отмывала любую перекомбинацию. Враждебное ревью A5
    назвало это самым опасным дефектом валидатора, поэтому доктрина исправлена:
    претензия строится, но несёт поверхность хеджа отдельным атомом.
    """

    predicate = _canonical_mandatory_predicate("пишет")
    hedged = [
        claim
        for claim in _extract_strong_modal_action_claims(
            "Возможно, суд пишет документ."
        )
        if claim.action == "strong_modal_family" and claim.object == predicate
    ]
    assert hedged, "хеджированная клауза обязана давать претензию, а не молчание"
    assert any(
        qualifier.startswith("relation_epistemic_frame:")
        for claim in hedged
        for qualifier in claim.qualifiers
    ), "претензия обязана нести поверхность хеджа отдельным атомом"

    # Обе стороны контракта: хеджированный источник не доказывает
    # категоричный вывод, а категоричный источник не доказывается
    # хеджированной выдумкой.
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            "Наказание: Возможно, суд пишет документ.",
            "Суд пишет документ",
            None,
        )


@pytest.mark.parametrize(
    "unsafe_output",
    (
        "Пишет «архив»",
        "Пишет (архив)",
        "Пишет «архив",
        "Пишет архиву",
        "«Суд» пишет",
        "(Суд) пишет",
    ),
    ids=(
        "quoted-tail",
        "parenthesized-tail",
        "malformed-tail",
        "unresolved-dative-tail",
        "quoted-prefix-actor",
        "parenthesized-prefix-actor",
    ),
)
def test_v3_productive_et_source_owned_unmatched_residue_is_rejected(
    unsafe_output: str,
) -> None:
    with pytest.raises(ValueError):
        _validate_strong_modal_productive_residual_source_evidence(
            "Суд пишет документ.",
            unsafe_output,
            field="direct-test",
        )


@pytest.mark.parametrize(
    "safe_output",
    (
        "Суд пишет документ.",
        "Суд не пишет документ.",
        "«Пишет»",
        "(Пишет)",
        "Кто пишет?",
        "Возможно, суд пишет документ",
        "Чтобы комиссия пишет документ",
    ),
    ids=(
        "exact-positive",
        "exact-negative",
        "quoted-action",
        "parenthesized-action",
        "query",
        "epistemic",
        "subordinate",
    ),
)
def test_v3_productive_et_occurrence_audit_preserves_local_fences(
    safe_output: str,
) -> None:
    _validate_strong_modal_productive_residual_source_evidence(
        "Суд пишет документ. Суд не пишет документ.",
        safe_output,
        field="direct-test",
    )


def test_v3_productive_et_occurrence_audit_keeps_exact_unemitted_sibling(
) -> None:
    source = "Суд пишет документ. Пишет «архив»."
    _validate_strong_modal_productive_residual_source_evidence(
        source,
        "Пишет «архив».",
        field="direct-test",
    )
    with pytest.raises(ValueError):
        _validate_strong_modal_productive_residual_source_evidence(
            source,
            "Пишет «реестр».",
            field="direct-test",
        )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
def test_v3_productive_et_unemitted_sibling_is_exact_on_every_path(
    validate: RelationValidator,
) -> None:
    source = "Суд пишет документ. Пишет «архив»."
    validate(source, "Пишет «архив».")
    with pytest.raises(ValueError):
        validate(source, "Пишет «реестр».")


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "value",
    (
        "Немедленно пишет.",
        "Пишет немедленно.",
        "Внимательно пишет.",
        "Пишет внимательно.",
    ),
    ids=(
        "known-before",
        "known-after",
        "unknown-before",
        "unknown-after",
    ),
)
def test_v3_productive_et_actorless_modifier_is_source_owned_on_every_path(
    value: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Настоящая статья определяет порядок рассмотрения дела.",
            value,
        )
    validate(value, value)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "changed"),
    (
        ("Немедленно пишет.", "Официально пишет."),
        ("Пишет немедленно.", "Пишет официально."),
        ("Внимательно пишет.", "Бережно пишет."),
        ("Пишет внимательно.", "Пишет бережно."),
    ),
    ids=(
        "known-before",
        "known-after",
        "unknown-before",
        "unknown-after",
    ),
)
def test_v3_productive_et_actorless_modifier_cannot_change_or_drop(
    source: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, changed)
    with pytest.raises(ValueError):
        validate(source, "Пишет.")


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "value",
    ("Бюро пишет.", "Лицо пишет.", "Министерство пишет."),
    ids=("bureau", "person", "ministry"),
)
def test_v3_productive_et_actor_head_collision_remains_exact(
    value: str,
    validate: RelationValidator,
) -> None:
    validate(value, value)
    with pytest.raises(ValueError):
        validate(
            "Настоящая статья определяет порядок рассмотрения дела.",
            value,
        )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    "unsafe",
    ("«Суд» пишет.", "(Суд) пишет."),
    ids=("quoted-prefix-actor", "parenthesized-prefix-actor"),
)
def test_v3_productive_et_wrapper_prefix_actor_is_not_erased(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate("Суд пишет документ.", unsafe)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
def test_v3_productive_et_leading_negation_actor_is_source_owned(
    validate: RelationValidator,
) -> None:
    value = "Не суд пишет."
    validate(value, value)
    with pytest.raises(ValueError):
        validate(
            "Настоящая статья определяет порядок рассмотрения дела.",
            value,
        )


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("value", "case"),
    (
        ("Суд пишет", "subject-incomplete"),
        ("Пишет документ", "orphan-patient"),
        ("Суд пишет, или рассматривает документ", "alternative-forward"),
        ("Суд пишет, но рассматривает документ", "conjunctive-forward"),
        ("Суд пишет, далее рассматривает документ", "sequence-forward"),
        (
            "Суд пишет и затем рассматривает документ",
            "compound-sequence-forward",
        ),
        (
            "Суд пишет, после чего рассматривает документ",
            "after-which-forward",
        ),
        ("Суд пишет, однако рассматривает документ", "contrast-forward"),
        ("Суд пишет, причём рассматривает документ", "continuation-forward"),
        (
            "Суд пишет и внимательно рассматривает документ",
            "modifier-forward",
        ),
        (
            "Суд пишет, а также внимательно рассматривает документ",
            "comma-modifier-forward",
        ),
        (
            "Суд пишет, далее прокуратура рассматривает документ",
            "foreign-subject-forward",
        ),
        ("Суд рассматривает документ, а пишет заключение", "reverse-a"),
        ("Суд рассматривает документ, но пишет заключение", "reverse-no"),
        (
            "Суд рассматривает документ, далее пишет заключение",
            "reverse-sequence",
        ),
        (
            "Суд рассматривает документ, однако пишет заключение",
            "reverse-contrast",
        ),
        (
            "Суд рассматривает документ, при этом пишет заключение",
            "reverse-continuation",
        ),
        (
            "Суд рассматривает документ, или прокуратура пишет заключение",
            "local-alternative",
        ),
        (
            "Суд рассматривает документ, а также прокуратура пишет заключение",
            "local-conjunctive",
        ),
        (
            "Суд рассматривает документ, далее прокуратура пишет заключение",
            "local-sequence",
        ),
        (
            "Суд рассматривает документ, однако прокуратура пишет заключение",
            "local-contrast",
        ),
        (
            "Суд рассматривает документ, при этом прокуратура пишет заключение",
            "local-continuation",
        ),
        ("Суд документ пишет и рассматривает", "shared-sov"),
        (
            "Суд документ пишет, а также рассматривает",
            "shared-sov-comma",
        ),
    ),
)
def test_v3_productive_et_structural_fallback_is_exact_on_every_path(
    value: str,
    case: str,
    validate: RelationValidator,
) -> None:
    del case
    validate(value + ".", value)


@pytest.mark.parametrize("validate", ALL_OUTPUT_PATHS, ids=PATH_IDS)
@pytest.mark.parametrize(
    ("source", "unsafe", "case"),
    (
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд пишет",
            "source-free-tier1",
        ),
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Пишет документ",
            "source-free-tier2",
        ),
        (
            "Настоящая статья определяет порядок рассмотрения дела.",
            "Суд пишет, далее рассматривает документ",
            "source-free-bridge",
        ),
        ("Суд пишет документ.", "Суд пишет", "drop-patient"),
        ("Суд пишет документ.", "Пишет документ", "drop-actor"),
        ("Суд пишет документ.", "Пишет", "drop-both"),
        (
            "Суд пишет документ.",
            "Немедленно пишет",
            "drop-both-with-modifier",
        ),
        (
            "Суд пишет документ.",
            "Прокуратура пишет документ",
            "standalone-actor",
        ),
        (
            "Суд пишет документ.",
            "Суд пишет заключение",
            "standalone-patient",
        ),
        (
            "Суд пишет, или рассматривает документ.",
            "Прокуратура пишет, или рассматривает документ",
            "alternative-forward-actor",
        ),
        (
            "Суд пишет, или рассматривает документ.",
            "Суд пишет, или рассматривает архив",
            "alternative-forward-patient",
        ),
        (
            "Суд пишет, или рассматривает документ.",
            "Суд бережёт, или рассматривает документ",
            "alternative-forward-action",
        ),
        (
            "Суд пишет, или рассматривает документ.",
            "Суд пишет, но рассматривает документ",
            "alternative-forward-connector",
        ),
        (
            "Суд пишет, или рассматривает документ.",
            "Суд рассматривает документ",
            "alternative-forward-drop",
        ),
        (
            "Суд пишет, но рассматривает документ.",
            "Прокуратура пишет, но рассматривает документ",
            "conjunctive-forward-actor",
        ),
        (
            "Суд пишет, но рассматривает документ.",
            "Суд пишет, но рассматривает архив",
            "conjunctive-forward-patient",
        ),
        (
            "Суд пишет, но рассматривает документ.",
            "Суд бережёт, но рассматривает документ",
            "conjunctive-forward-action",
        ),
        (
            "Суд пишет, но рассматривает документ.",
            "Суд пишет, или рассматривает документ",
            "conjunctive-forward-connector",
        ),
        (
            "Суд пишет, далее рассматривает документ.",
            "Прокуратура пишет, далее рассматривает документ",
            "sequence-forward-actor",
        ),
        (
            "Суд пишет, далее рассматривает документ.",
            "Суд пишет, далее рассматривает архив",
            "sequence-forward-patient",
        ),
        (
            "Суд пишет, далее рассматривает документ.",
            "Суд бережёт, далее рассматривает документ",
            "sequence-forward-action",
        ),
        (
            "Суд пишет, далее рассматривает документ.",
            "Суд пишет, при этом рассматривает документ",
            "sequence-forward-connector",
        ),
        (
            "Суд пишет и затем рассматривает документ.",
            "Суд пишет и рассматривает документ",
            "compound-sequence-connector",
        ),
        (
            "Суд пишет и затем рассматривает документ.",
            "Суд пишет и затем рассматривает архив",
            "compound-sequence-patient",
        ),
        (
            "Суд пишет, после чего рассматривает документ.",
            "Суд пишет, далее рассматривает документ",
            "after-which-connector",
        ),
        (
            "Суд пишет, после чего рассматривает документ.",
            "Прокуратура пишет, после чего рассматривает документ",
            "after-which-actor",
        ),
        (
            "Суд пишет, однако рассматривает документ.",
            "Прокуратура пишет, однако рассматривает документ",
            "contrast-forward-actor",
        ),
        (
            "Суд пишет, однако рассматривает документ.",
            "Суд пишет, однако рассматривает архив",
            "contrast-forward-patient",
        ),
        (
            "Суд пишет, однако рассматривает документ.",
            "Суд бережёт, однако рассматривает документ",
            "contrast-forward-action",
        ),
        (
            "Суд пишет, однако рассматривает документ.",
            "Суд пишет, зато рассматривает документ",
            "contrast-forward-connector",
        ),
        (
            "Суд пишет, причём рассматривает документ.",
            "Прокуратура пишет, причём рассматривает документ",
            "continuation-forward-actor",
        ),
        (
            "Суд пишет, причём рассматривает документ.",
            "Суд пишет, причём рассматривает архив",
            "continuation-forward-patient",
        ),
        (
            "Суд пишет, причём рассматривает документ.",
            "Суд бережёт, причём рассматривает документ",
            "continuation-forward-action",
        ),
        (
            "Суд пишет, причём рассматривает документ.",
            "Суд пишет, при этом рассматривает документ",
            "continuation-forward-connector",
        ),
        (
            "Суд пишет и внимательно рассматривает документ.",
            "Суд пишет и рассматривает документ",
            "modifier-drop",
        ),
        (
            "Суд пишет и внимательно рассматривает документ.",
            "Суд пишет и немедленно рассматривает документ",
            "modifier-change",
        ),
        (
            "Суд пишет, далее прокуратура рассматривает документ.",
            "Комиссия пишет, далее прокуратура рассматривает документ",
            "foreign-candidate-actor",
        ),
        (
            "Суд пишет, далее прокуратура рассматривает документ.",
            "Суд пишет, далее комиссия рассматривает документ",
            "foreign-anchor-actor",
        ),
        (
            "Суд пишет, далее прокуратура рассматривает документ.",
            "Суд пишет, далее прокуратура рассматривает архив",
            "foreign-patient",
        ),
        (
            "Суд пишет, далее прокуратура рассматривает документ.",
            "Суд пишет, при этом прокуратура рассматривает документ",
            "foreign-connector",
        ),
        (
            "Суд рассматривает документ, или пишет заключение.",
            "Прокуратура рассматривает документ, или пишет заключение",
            "alternative-reverse-actor",
        ),
        (
            "Суд рассматривает документ, или пишет заключение.",
            "Суд рассматривает архив, или пишет заключение",
            "alternative-reverse-anchor-patient",
        ),
        (
            "Суд рассматривает документ, или пишет заключение.",
            "Суд рассматривает документ, или пишет запись",
            "alternative-reverse-candidate-patient",
        ),
        (
            "Суд рассматривает документ, или пишет заключение.",
            "Суд рассматривает документ, но пишет заключение",
            "alternative-reverse-connector",
        ),
        (
            "Суд рассматривает документ, или пишет заключение.",
            "Суд рассматривает документ",
            "alternative-reverse-drop",
        ),
        (
            "Суд рассматривает документ, а пишет заключение.",
            "Суд рассматривает документ, но пишет заключение",
            "conjunctive-reverse-connector",
        ),
        (
            "Суд рассматривает документ, а пишет заключение.",
            "Суд рассматривает документ, а бережёт заключение",
            "conjunctive-reverse-action",
        ),
        (
            "Суд рассматривает документ, далее пишет заключение.",
            "Прокуратура рассматривает документ, далее пишет заключение",
            "sequence-reverse-actor",
        ),
        (
            "Суд рассматривает документ, далее пишет заключение.",
            "Суд рассматривает документ, при этом пишет заключение",
            "sequence-reverse-connector",
        ),
        (
            "Суд рассматривает документ, однако пишет заключение.",
            "Суд рассматривает документ, зато пишет заключение",
            "contrast-reverse-connector",
        ),
        (
            "Суд рассматривает документ, однако пишет заключение.",
            "Суд рассматривает документ, однако пишет запись",
            "contrast-reverse-patient",
        ),
        (
            "Суд рассматривает документ, при этом пишет заключение.",
            "Суд рассматривает документ, причём пишет заключение",
            "continuation-reverse-connector",
        ),
        (
            "Суд рассматривает документ, при этом пишет заключение.",
            "Суд рассматривает документ, при этом бережёт заключение",
            "continuation-reverse-action",
        ),
        (
            "Суд рассматривает документ, или прокуратура пишет заключение.",
            "Суд рассматривает документ, или комиссия пишет заключение",
            "local-alternative-actor",
        ),
        (
            "Суд рассматривает документ, а также прокуратура пишет заключение.",
            "Суд рассматривает документ, а также комиссия пишет заключение",
            "local-conjunctive-actor",
        ),
        (
            "Суд рассматривает документ, далее прокуратура пишет заключение.",
            "Суд рассматривает документ, далее прокуратура пишет запись",
            "local-sequence-patient",
        ),
        (
            "Суд рассматривает документ, однако прокуратура пишет заключение.",
            "Суд рассматривает документ, однако пишет заключение",
            "local-contrast-drop-actor",
        ),
        (
            "Суд рассматривает документ, при этом прокуратура пишет заключение.",
            "Суд рассматривает документ, при этом прокуратура пишет",
            "local-continuation-drop-patient",
        ),
        (
            "Суд рассматривает документ, при этом прокуратура пишет заключение.",
            "Суд рассматривает документ, при этом пишет",
            "local-continuation-drop-both-roles",
        ),
        (
            "Суд документ пишет и рассматривает.",
            "Прокуратура документ пишет и рассматривает",
            "sov-actor",
        ),
        (
            "Суд документ пишет и рассматривает.",
            "Суд архив пишет и рассматривает",
            "sov-patient",
        ),
        (
            "Суд документ пишет и рассматривает.",
            "Суд документ пишет, а также рассматривает",
            "sov-connector",
        ),
        (
            "Суд документ пишет и рассматривает.",
            "Суд документ рассматривает",
            "sov-drop-action",
        ),
    ),
)
def test_v3_productive_et_structural_fallback_rejects_drift_everywhere(
    source: str,
    unsafe: str,
    case: str,
    validate: RelationValidator,
) -> None:
    del case
    with pytest.raises(ValueError):
        validate(source, unsafe)
