from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _relation_negative_past_passive_auxiliary_prefix,
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
        ["Порядок работы организации", output],
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


ACTION_MODIFIERS = (
    "немедленно",
    "незамедлительно",
    "сразу",
    "лично",
    "непосредственно",
    "повторно",
    "вновь",
    "ранее",
    "уже",
    "ещё",
    "фактически",
    "реально",
    "формально",
    "юридически",
    "официально",
    "окончательно",
    "временно",
    "частично",
    "полностью",
    "условно",
    "принудительно",
    "добровольно",
    "совместно",
    "единолично",
    "публично",
    "тайно",
    "досрочно",
    "бессрочно",
    "законно",
    "незаконно",
    "умышленно",
    "преднамеренно",
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
@pytest.mark.parametrize("modifier", ACTION_MODIFIERS)
def test_v3_action_modifier_moves_from_pre_action_to_action_patient_gap(
    validate: RelationValidator,
    modifier: str,
) -> None:
    source = f"Прокуратура {modifier} аннулировала лицензию."
    faithful = f"Прокуратура аннулировала {modifier} лицензию"
    replacement = "временно" if modifier != "временно" else "окончательно"

    validate(source, faithful)
    for unsafe in (
        "Прокуратура аннулировала лицензию",
        f"Прокуратура аннулировала {replacement} лицензию",
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
def test_v3_modal_action_modifier_can_move_before_its_modal(
    validate: RelationValidator,
) -> None:
    source = "Прокуратура может повторно аннулировать лицензию."
    faithful = "Прокуратура повторно вправе аннулировать лицензию"

    validate(source, faithful)
    for unsafe in (
        "Прокуратура вправе аннулировать лицензию",
        "Прокуратура окончательно вправе аннулировать лицензию",
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
def test_v3_post_patient_modifier_can_move_before_the_action(
    validate: RelationValidator,
) -> None:
    source = "Прокуратура аннулировала лицензию окончательно."
    faithful = "Прокуратура окончательно аннулировала лицензию"

    validate(source, faithful)
    for unsafe in (
        "Прокуратура аннулировала лицензию",
        "Прокуратура временно аннулировала лицензию",
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
def test_v3_post_participle_modifier_survives_active_reordering(
    validate: RelationValidator,
) -> None:
    source = "Лицензия была аннулирована официально прокуратурой."
    faithful = "Прокуратура официально аннулировала лицензию"

    validate(source, faithful)
    for unsafe in (
        "Прокуратура аннулировала лицензию",
        "Прокуратура фактически аннулировала лицензию",
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
def test_v3_condition_modifier_does_not_leak_into_the_main_action(
    validate: RelationValidator,
) -> None:
    source = (
        "Если организация временно закрыта, прокуратура аннулирует лицензию."
    )
    faithful = "Прокуратура аннулирует лицензию"
    unsafe = "Прокуратура временно аннулирует лицензию"

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
def test_v3_modifier_does_not_jump_between_coordinated_actions(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура временно приостанавливает лицензию и аннулирует пропуск."
    )
    faithful = (
        "Прокуратура временно приостанавливает лицензию и пропуск аннулирует"
    )
    unsafe = (
        "Прокуратура приостанавливает лицензию и временно аннулирует пропуск"
    )

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
@pytest.mark.parametrize(
    "unknown_modifier",
    ("частично", "условно", "принудительно"),
)
def test_v3_unknown_negated_auxiliary_modifier_is_exact_and_movable(
    validate: RelationValidator,
    unknown_modifier: str,
) -> None:
    source = (
        f"Лицензия не была {unknown_modifier} аннулирована прокуратурой."
    )
    faithful = (
        f"Лицензия {unknown_modifier} не была аннулирована прокуратурой"
    )
    replacements = tuple(
        modifier
        for modifier in ("частично", "условно", "принудительно")
        if modifier != unknown_modifier
    )

    validate(source, faithful)
    for unsafe in (
        "Лицензия не была аннулирована прокуратурой",
        *(
            f"Лицензия не была {replacement} аннулирована прокуратурой"
            for replacement in replacements
        ),
        f"Лицензия была {unknown_modifier} аннулирована прокуратурой",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_v3_negated_past_auxiliary_proof_is_explicitly_tri_state() -> None:
    assert (
        _relation_negative_past_passive_auxiliary_prefix("Прокуратура ")
        == "absent"
    )
    assert (
        _relation_negative_past_passive_auxiliary_prefix(
            "Лицензия не была частично "
        )
        == "negative"
    )
    assert (
        _relation_negative_past_passive_auxiliary_prefix(
            "Лицензия не была по решению суда "
        )
        == "unresolved"
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
def test_v3_unresolved_negated_auxiliary_cannot_become_positive(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия не была по решению суда аннулирована прокуратурой."
    )
    unsafe = "Лицензия была по решению суда аннулирована прокуратурой"

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
def test_v3_nonstrict_action_modifier_is_still_source_exact(
    validate: RelationValidator,
) -> None:
    source = "Суд официально подтвердил штраф."
    faithful = "Суд подтвердил штраф официально"

    validate(source, faithful)
    for unsafe in (
        "Суд подтвердил штраф",
        "Суд временно подтвердил штраф",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "modifier",
    ("частично", "условно", "принудительно"),
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
def test_v3_positive_high_impact_adverb_is_not_silently_dropped(
    validate: RelationValidator,
    modifier: str,
) -> None:
    source = f"Суд {modifier} подтвердил штраф."
    faithful = f"Суд подтвердил штраф {modifier}"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд подтвердил штраф")


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
def test_v3_source_occurrence_unresolved_polarity_has_no_positive_escape(
    validate: RelationValidator,
) -> None:
    source = (
        "Штраф не был по решению суда подтверждён судом."
    )
    unsafe = "Штраф был подтверждён судом"

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
def test_v3_patient_owner_adverb_cannot_become_action_modifier_or_drop(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура аннулировала лицензию официально "
        "действующей организации."
    )
    for unsafe in (
        "Прокуратура официально аннулировала лицензию "
        "действующей организации",
        "Прокуратура аннулировала лицензию действующей организации",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)

    action_source = (
        "Прокуратура официально аннулировала лицензию "
        "действующей организации."
    )
    action_faithful = (
        "Прокуратура аннулировала лицензию действующей "
        "организации официально"
    )
    validate(action_source, action_faithful)


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
def test_v3_complete_adverb_cluster_keeps_patient_owner_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура аннулировала лицензию официально и юридически "
        "действующей организации."
    )
    faithful = (
        "Прокуратура аннулировала лицензию юридически и официально "
        "действующей организации"
    )

    validate(source, faithful)
    for unsafe in (
        "Прокуратура официально и юридически аннулировала лицензию "
        "действующей организации",
        "Прокуратура аннулировала лицензию действующей организации",
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
def test_v3_adverbial_patient_state_cannot_move_onto_action(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура аннулировала официально действующую лицензию."
    )
    for unsafe in (
        "Прокуратура официально аннулировала действующую лицензию",
        "Прокуратура аннулировала действующую лицензию",
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
def test_v3_adverbial_active_actor_state_cannot_move_onto_action(
    validate: RelationValidator,
) -> None:
    source = "Официально действующая прокуратура подтвердила штраф."
    for unsafe in (
        "Действующая прокуратура официально подтвердила штраф",
        "Действующая прокуратура подтвердила штраф",
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
def test_v3_passive_actor_adverb_cannot_become_action_modifier_or_drop(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия была аннулирована официально действующей "
        "организацией."
    )
    for unsafe in (
        "Лицензия была аннулирована действующей организацией",
        "Лицензия официально была аннулирована действующей "
        "организацией",
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
def test_v3_unknown_negative_actor_adverb_keeps_its_actor_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия не была аннулирована условно действующей "
        "организацией."
    )
    unsafe = (
        "Лицензия условно не была аннулирована действующей "
        "организацией"
    )

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
def test_v3_negative_passive_accepts_actor_between_auxiliary_and_action(
    validate: RelationValidator,
) -> None:
    source = "Прокуратура не аннулировала лицензию."
    faithful = "Лицензия не была прокуратурой аннулирована"

    validate(source, faithful)
    long_role_spacing = (
        "Лицензия не была"
        + (" " * 100)
        + "прокуратурой"
        + (" " * 100)
        + "аннулирована"
    )
    validate(source, long_role_spacing)


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
def test_v3_negative_passive_accepts_actor_group_inside_scaffold(
    validate: RelationValidator,
) -> None:
    source = "Прокуратура и суд не аннулировали лицензию."
    faithful = (
        "Лицензия не была прокуратурой и судом аннулирована"
    )

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
def test_v3_comma_preposed_modifier_belongs_only_to_the_later_action(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд аннулировал лицензию, сразу приостановил пропуск."
    )
    faithful = (
        "Суд аннулировал лицензию, приостановил пропуск сразу"
    )
    unsafe = (
        "Суд сразу аннулировал лицензию, сразу приостановил "
        "пропуск"
    )

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
def test_v3_ordered_duplicate_actions_cannot_swap_modifier_occurrences(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд официально подтвердил штраф, затем суд подтвердил штраф."
    )
    faithful = (
        "Суд подтвердил штраф официально, затем суд подтвердил штраф"
    )
    unsafe = (
        "Суд подтвердил штраф, затем суд официально подтвердил штраф"
    )

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
def test_v3_modifier_stays_with_patient_during_and_reordering(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд официально подтвердил штраф №1 и подтвердил штраф №2."
    )
    faithful = (
        "Суд подтвердил штраф №2 и официально подтвердил штраф №1"
    )
    unsafe = (
        "Суд подтвердил штраф №1 и официально подтвердил штраф №2"
    )

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
def test_v3_modifier_stays_with_reference_during_and_reordering(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд официально подтвердил штраф по статье 1 и "
        "подтвердил штраф по статье 2."
    )
    faithful = (
        "Суд подтвердил штраф по статье 2 и официально "
        "подтвердил штраф по статье 1"
    )
    unsafe = (
        "Суд подтвердил штраф по статье 1 и официально "
        "подтвердил штраф по статье 2"
    )

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
def test_v3_modifier_local_negation_and_contrast_are_exact(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура не лично, а официально аннулировала "
        "лицензию."
    )
    faithful = (
        "Прокуратура официально, а не лично аннулировала "
        "лицензию"
    )
    unsafe = (
        "Прокуратура лично и официально аннулировала лицензию"
    )

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
def test_v3_modifier_or_is_commutative_but_not_and(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура лично или официально аннулировала лицензию."
    )
    faithful = (
        "Прокуратура официально либо лично аннулировала лицензию"
    )
    unsafe = (
        "Прокуратура лично и официально аннулировала лицензию"
    )

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
def test_v3_not_only_modifier_correlative_is_positive_and(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура не только лично, но и официально "
        "аннулировала лицензию."
    )
    faithful = (
        "Прокуратура официально и лично аннулировала лицензию"
    )
    unsafe = (
        "Прокуратура не лично, а официально аннулировала "
        "лицензию"
    )

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
def test_v3_as_well_as_modifier_correlative_is_complete_and(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура как лично, так и официально аннулировала "
        "лицензию."
    )
    faithful = (
        "Прокуратура официально и лично аннулировала лицензию"
    )

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокуратура официально аннулировала лицензию",
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
def test_v3_outer_negated_modifier_group_cannot_become_positive(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура не (лично или официально) аннулировала "
        "лицензию."
    )
    for unsafe in (
        "Прокуратура лично или официально аннулировала лицензию",
        "Прокуратура официально аннулировала лицензию",
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
def test_v3_positive_modifier_parentheses_preserve_boolean_grouping(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд (лично или официально) и юридически подтвердил штраф."
    )
    faithful = (
        "Суд юридически и (официально либо лично) подтвердил штраф"
    )
    unsafe = (
        "Суд лично или (официально и юридически) подтвердил штраф"
    )

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
def test_v3_to_li_modifier_correlative_is_complete_alternative(
    validate: RelationValidator,
) -> None:
    source = "Суд то ли лично, то ли официально подтвердил штраф."
    faithful = "Суд официально либо лично подтвердил штраф"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд официально подтвердил штраф")

    negated_source = (
        "Суд не (то ли лично, то ли официально) подтвердил штраф."
    )
    with pytest.raises(ValueError):
        validate(
            negated_source,
            "Суд то ли лично, то ли официально подтвердил штраф",
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
def test_v3_formula_fallback_keeps_action_modifier_contract(
    validate: RelationValidator,
) -> None:
    source = "Лицензия аннулируется за нарушение закона."
    unsafe = (
        "Лицензия временно аннулируется за нарушение закона"
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)

    modified_source = (
        "Лицензия временно аннулируется за нарушение закона."
    )
    faithful = (
        "Лицензия аннулируется временно за нарушение закона"
    )
    validate(modified_source, faithful)
    with pytest.raises(ValueError):
        validate(
            modified_source,
            "Лицензия аннулируется за нарушение закона",
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
def test_v3_negative_auxiliary_has_no_character_window_fail_open(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия была фактически аннулирована прокуратурой."
    )
    unsafe = (
        "Лицензия не была"
        + (" " * 170)
        + "фактически аннулирована прокуратурой"
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)

    negative_source = "Прокуратура не аннулировала лицензию."
    faithful = (
        "Лицензия не была"
        + (" " * 170)
        + "аннулирована прокуратурой"
    )
    validate(negative_source, faithful)


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
def test_v3_active_negation_has_no_character_window_fail_open(
    validate: RelationValidator,
) -> None:
    positive_source = "Прокуратура аннулировала лицензию."
    negative_output = (
        "Прокуратура не"
        + (" " * 170)
        + "аннулировала лицензию"
    )

    with pytest.raises(ValueError):
        validate(positive_source, negative_output)

    negative_source = "Прокуратура не аннулировала лицензию."
    validate(negative_source, negative_output)


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
def test_v3_modifier_local_polarity_has_no_character_window(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура не"
        + (" " * 170)
        + "лично, а официально аннулировала лицензию."
    )
    unsafe = (
        "Прокуратура лично и официально аннулировала лицензию"
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "modifier",
    ("поэтапно", "одновременно", "своевременно"),
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
def test_v3_productive_positive_adverb_is_not_silently_dropped(
    validate: RelationValidator,
    modifier: str,
) -> None:
    source = f"Суд {modifier} подтвердил штраф."
    faithful = f"Суд подтвердил штраф {modifier}"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд подтвердил штраф")


@pytest.mark.parametrize(
    "modifier",
    (
        "дважды",
        "трижды",
        "однократно",
        "многократно",
        "вручную",
        "сначала",
        "затем",
        "впоследствии",
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
def test_v3_irregular_action_adverb_is_not_silently_dropped(
    validate: RelationValidator,
    modifier: str,
) -> None:
    source = f"Суд {modifier} подтвердил штраф."
    faithful = f"Суд подтвердил штраф {modifier}"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд подтвердил штраф")


@pytest.mark.parametrize(
    "focus",
    ("только", "лишь", "исключительно"),
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
def test_v3_unary_modifier_focus_is_not_silently_dropped(
    validate: RelationValidator,
    focus: str,
) -> None:
    source = f"Суд {focus} официально подтвердил штраф."
    faithful = f"Суд подтвердил штраф {focus} официально"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд официально подтвердил штраф")


@pytest.mark.parametrize(
    "focus",
    ("только", "лишь", "исключительно"),
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
def test_v3_grouped_unary_modifier_focus_is_not_silently_dropped(
    validate: RelationValidator,
    focus: str,
) -> None:
    source = f"Суд {focus} (лично или официально) подтвердил штраф."
    faithful = f"Суд {focus} (официально либо лично) подтвердил штраф"
    dropped = "Суд лично или официально подтвердил штраф"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, dropped)


@pytest.mark.parametrize(
    "modifier",
    ("сразу же", "ещё раз"),
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
def test_v3_compound_action_modifier_is_one_complete_atom(
    validate: RelationValidator,
    modifier: str,
) -> None:
    source = f"Суд {modifier} подтвердил штраф."
    faithful = f"Суд подтвердил штраф {modifier}"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд подтвердил штраф")


@pytest.mark.parametrize(
    "modifier",
    ("обязательно", "автоматически", "всегда"),
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
def test_v3_negated_modality_adverb_cannot_become_positive(
    validate: RelationValidator,
    modifier: str,
) -> None:
    source = f"Суд не {modifier} подтверждает штраф."
    faithful = f"Суд подтверждает штраф не {modifier}"
    positive = f"Суд {modifier} подтверждает штраф"

    validate(source, faithful)
    validate(faithful + ".", source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, positive)


@pytest.mark.parametrize(
    ("focus", "modifier"),
    (
        ("только", "автоматически"),
        ("лишь", "обязательно"),
        ("исключительно", "всегда"),
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
def test_v3_focused_modality_adverb_cannot_lose_focus(
    validate: RelationValidator,
    focus: str,
    modifier: str,
) -> None:
    source = f"Суд {focus} {modifier} подтверждает штраф."
    faithful = f"Суд подтверждает штраф {focus} {modifier}"
    dropped = f"Суд {modifier} подтверждает штраф"

    validate(source, faithful)
    validate(faithful + ".", source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, dropped)


@pytest.mark.parametrize(
    "modifier",
    ("автоматически", "обязательно", "всегда"),
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
def test_v3_positive_modality_adverb_is_position_independent(
    validate: RelationValidator,
    modifier: str,
) -> None:
    pre_action = f"Суд {modifier} подтверждает штраф."
    post_action = f"Суд подтверждает штраф {modifier}"

    validate(pre_action, post_action)
    validate(post_action + ".", pre_action.rstrip("."))


@pytest.mark.parametrize(
    ("modifier", "adjective"),
    (
        ("Автоматически", "созданная"),
        ("Обязательно", "уполномоченная"),
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
def test_v3_positive_modality_adverb_keeps_nominal_role(
    validate: RelationValidator,
    modifier: str,
    adjective: str,
) -> None:
    source = (
        f"{modifier} {adjective} организация подтвердила штраф."
    )
    faithful = (
        f"Штраф подтвердила {modifier.casefold()} {adjective} организация"
    )
    unsafe = (
        f"{adjective.capitalize()} организация {modifier.casefold()} "
        "подтвердила штраф"
    )

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
def test_v3_productive_adverb_group_is_discovered_before_ownership(
    validate: RelationValidator,
) -> None:
    source = "Суд (поэтапно или своевременно) подтвердил штраф."
    faithful = "Суд своевременно либо поэтапно подтвердил штраф"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд подтвердил штраф")


@pytest.mark.parametrize(
    ("source", "faithful", "dropped"),
    (
        (
            "Поэтапно аннулирована лицензия прокуратурой.",
            "Лицензия поэтапно аннулирована прокуратурой",
            "Лицензия аннулирована прокуратурой",
        ),
        (
            "Поэтапно аннулировала прокуратура лицензию.",
            "Прокуратура поэтапно аннулировала лицензию",
            "Прокуратура аннулировала лицензию",
        ),
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
def test_v3_productive_pre_action_adverb_uses_postposed_role_proof(
    validate: RelationValidator,
    source: str,
    faithful: str,
    dropped: str,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, dropped)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Штраф официально действующая прокуратура подтвердила.",
            "Официально действующая прокуратура подтвердила штраф",
            "Штраф действующая прокуратура официально подтвердила",
        ),
        (
            "Штраф подтвердила официально действующая прокуратура.",
            "Официально действующая прокуратура подтвердила штраф",
            "Штраф официально подтвердила действующая прокуратура",
        ),
        (
            "Официально действующая прокуратура штраф подтвердила.",
            "Официально действующая прокуратура подтвердила штраф",
            "Действующая прокуратура штраф официально подтвердила",
        ),
    ),
    ids=("patient-actor-verb", "patient-verb-actor", "actor-patient-verb"),
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
def test_v3_actor_state_adverb_keeps_role_in_every_word_order(
    validate: RelationValidator,
    source: str,
    faithful: str,
    unsafe: str,
) -> None:
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
def test_v3_modifier_before_comma_stays_with_left_action(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд подтвердил штраф официально, подтвердил штраф."
    )
    faithful = (
        "Суд официально подтвердил штраф, подтвердил штраф"
    )
    unsafe = (
        "Суд подтвердил штраф, официально подтвердил штраф"
    )

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
def test_v3_unread_modifier_group_fails_closed(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд (лично и при этом официально) подтвердил штраф."
    )

    with pytest.raises(ValueError):
        validate(source, "Суд подтвердил штраф")


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
def test_v3_unread_raw_modifier_formula_fails_closed(
    validate: RelationValidator,
) -> None:
    source = "Суд лично и при этом официально подтвердил штраф."

    with pytest.raises(ValueError):
        validate(source, "Суд официально подтвердил штраф")


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
def test_v3_unread_raw_additive_modifier_formula_fails_closed(
    validate: RelationValidator,
) -> None:
    source = "Суд лично, причём официально подтвердил штраф."

    with pytest.raises(ValueError):
        validate(source, "Суд официально подтвердил штраф")


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
def test_v3_finite_nominal_bridge_keeps_action_modifier(
    validate: RelationValidator,
) -> None:
    source = (
        "Штраф; лицензия временно аннулируется за нарушение закона."
    )
    faithful = (
        "Штраф; временное аннулирование лицензии за нарушение закона"
    )
    dropped = "Штраф; аннулирование лицензии за нарушение закона"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, dropped)

    unmodified_source = (
        "Штраф; лицензия аннулируется за нарушение закона."
    )
    with pytest.raises(ValueError):
        validate(unmodified_source, faithful)


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
def test_v3_productive_finite_nominal_bridge_keeps_action_modifier(
    validate: RelationValidator,
) -> None:
    source = (
        "Штраф; лицензия поэтапно аннулируется за нарушение закона."
    )
    faithful = (
        "Штраф; поэтапное аннулирование лицензии за нарушение закона"
    )
    dropped = "Штраф; аннулирование лицензии за нарушение закона"
    unmodified = "Штраф; лицензия аннулируется за нарушение закона."

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, dropped)
    with pytest.raises(ValueError):
        validate(unmodified, faithful)


@pytest.mark.parametrize(
    ("source", "faithful"),
    (
        (
            "Лицензия автоматически аннулируется.",
            "Автоматическое аннулирование лицензии",
        ),
        (
            "Лицензия должна быть аннулирована.",
            "Обязательное аннулирование лицензии",
        ),
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
def test_v3_nominal_modality_is_not_retyped_as_action_modifier(
    validate: RelationValidator,
    source: str,
    faithful: str,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Формально независимая организация подтвердила штраф.",
            "Штраф подтвердила формально независимая организация",
            "Независимая организация формально подтвердила штраф",
        ),
        (
            "Официально уполномоченная организация подтвердила штраф.",
            "Штраф подтвердила официально уполномоченная организация",
            "Уполномоченная организация официально подтвердила штраф",
        ),
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
def test_v3_general_adverbial_actor_adjective_keeps_actor_scope(
    validate: RelationValidator,
    source: str,
    faithful: str,
    unsafe: str,
) -> None:
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
def test_v3_general_adverbial_patient_adjective_keeps_patient_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Штраф; прокуратура аннулировала незаконно приобретённую "
        "лицензию."
    )
    unsafe = (
        "Штраф; прокуратура незаконно аннулировала приобретённую "
        "лицензию"
    )
    faithful = (
        "Штраф; незаконно приобретённую лицензию прокуратура "
        "аннулировала"
    )

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
def test_v3_postpositive_modifier_group_is_complete_and_exact(
    validate: RelationValidator,
) -> None:
    source = "Суд подтвердил штраф (лично или официально)."
    faithful = "Суд официально либо лично подтвердил штраф"
    grouped = "Суд подтвердил штраф (лично или официально)"

    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, "Суд подтвердил штраф")
    with pytest.raises(ValueError):
        validate("Суд подтвердил штраф.", grouped)


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
def test_v3_modifier_formula_overflow_fails_closed(
    validate: RelationValidator,
) -> None:
    overflow = (
        "Суд ((лично или официально) и (формально или юридически) "
        "и (временно или окончательно) и (повторно или вновь) "
        "и (реально или фактически)) подтвердил штраф."
    )

    with pytest.raises(ValueError):
        validate("Суд подтвердил штраф.", overflow.rstrip("."))
    with pytest.raises(ValueError):
        validate(overflow, "Суд лично подтвердил штраф")
