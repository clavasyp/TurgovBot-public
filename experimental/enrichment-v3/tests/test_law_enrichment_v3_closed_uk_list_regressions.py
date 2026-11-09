from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _CLOSED_UK_WEEKDAY_SCOPE_RE,
    _RELATION_ACTION_RE,
    _closed_uk_action_correlative_contracts,
    _closed_uk_action_logic_contract,
    _closed_uk_action_record_bridge_kind,
    _closed_uk_authorization_contexts,
    _closed_uk_authorization_formula_contracts,
    _closed_uk_global_authorization_context_spans,
    _closed_uk_revocation_action_records,
    _closed_uk_weekday_role_families,
    _extract_relation_truths,
    _ground_formula_evidence,
    _predicate_relation,
    _relation_bounded_predicate_candidates,
    _relation_bounded_patient_expression_is_supported,
    _relation_compound_temporal_subordinate_spans,
    _relation_condition_shared_head_parts,
    _relation_document_patient_is_active_object,
    _relation_document_patient_passive_agrees,
    _relation_patient_surface_contract,
    _relation_separator_spans,
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
    source_surface = source.rstrip(".")
    validate_synonym_source_evidence(
        source,
        [source_surface, output]
        if output != source_surface
        else [source_surface],
    )


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set,
)


def _validate_penalty_summary(source: str, output: str) -> None:
    validate_penalty_source_evidence(source, output, None)


def _validate_penalty_other(source: str, output: str) -> None:
    validate_penalty_source_evidence(source, None, {"other": output})


ALL_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    *OUTPUT_PATHS,
    _validate_penalty_summary,
    _validate_penalty_other,
)


def _closed_uk_output_for_path(
    validate: RelationValidator,
    *,
    alias: str,
    penalty: str,
) -> str:
    return penalty if "penalty" in validate.__name__ else alias


CLOSED_UK_LIST_SOURCE = (
    "Лицензия на оружие должна быть аннулирована и изъята при задержании "
    "сотрудниками правоохранительных органов только в случае нарушения "
    "любой из статей: 6.1.1; 6.2; 6.3; 7.1 УК."
)

CLOSED_UK_LIST_ALIAS = (
    "правоохранители обязаны аннулировать и изъять лицензию на оружие "
    "при задержании за любую статью из списка УК"
)

CLOSED_UK_LIST_EXPLICIT = (
    "правоохранители обязаны аннулировать и изъять лицензию на оружие "
    "при задержании за нарушение любой из статей: "
    "6.1.1; 6.2; 6.3; 7.1 УК"
)

CLOSED_UK_LIST_EXPLICIT_NOMINAL = (
    "аннулирование и изъятие лицензии на оружие при задержании "
    "за нарушение любой из статей: 6.1.1; 6.2; 6.3; 7.1 УК"
)

CLOSED_UK_LIST_JURISDICTION_SOURCE = CLOSED_UK_LIST_SOURCE.replace(
    " УК.",
    " УК штата SA.",
)

CLOSED_UK_LIST_COMPACT_PENALTY = (
    "отзыв и изъятие лицензии на оружие при задержании "
    "за любую статью списка УК"
)

CLOSED_UK_LIST_ALTERNATIVE_SOURCE = (
    "Правоохранители обязаны аннулировать или изъять лицензию на оружие "
    "при задержании только в случае нарушения любой из статей: "
    "6.1.1; 6.2; 6.3; 7.1 УК."
)

CLOSED_UK_LIST_ALTERNATIVE_ALIAS = (
    "правоохранители обязаны аннулировать или изъять лицензию на оружие "
    "при задержании за любую статью из списка УК"
)

CLOSED_UK_LIST_ALTERNATIVE_EXPLICIT = (
    "правоохранители обязаны аннулировать или изъять лицензию на оружие "
    "при задержании за нарушение любой из статей: "
    "6.1.1; 6.2; 6.3; 7.1 УК"
)


def test_v3_second_coordinated_passive_inherits_scoped_document_patient() -> None:
    seizure_truths = tuple(
        truth
        for truth in _extract_relation_truths(CLOSED_UK_LIST_SOURCE)
        if (
            truth.predicate == "regulated_revocation"
            and truth.action_family == "seizure"
            and truth.direct_object == "license"
        )
    )
    assert seizure_truths
    assert all(
        truth.patient_surface_contract == "license|scope:weapon"
        and truth.modality == "mandatory"
        and truth.event == "prospective"
        and truth.coordination == "conjunctive"
        for truth in seizure_truths
    )


@pytest.mark.parametrize(
    "output",
    (
        (
            "правоохранители обязаны аннулировать и изъять лицензию на оружие "
            "при задержании "
            "за любую статью из списка УК"
        ),
        (
            "правоохранители обязаны аннулировать и изъять лицензию на оружие "
            "при задержании "
            "за любую статью списка УК"
        ),
    ),
    ids=("from-list", "list-genitive"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_article_list_has_one_exact_search_formula(
    output: str,
    validate: RelationValidator,
) -> None:
    validate(CLOSED_UK_LIST_SOURCE, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
@pytest.mark.parametrize(
    "jurisdiction",
    ("SA", "San Andreas", "Liberty"),
    ids=("abbreviation", "two-word-name", "one-word-name"),
)
def test_v3_closed_uk_compact_alias_may_omit_local_jurisdiction(
    jurisdiction: str,
    validate: RelationValidator,
) -> None:
    source = CLOSED_UK_LIST_SOURCE.replace(
        " УК.",
        f" УК штата {jurisdiction}.",
    )
    validate(source, CLOSED_UK_LIST_ALIAS)


def test_v3_closed_uk_compact_penalty_may_omit_local_jurisdiction() -> None:
    validate_penalty_source_evidence(
        CLOSED_UK_LIST_JURISDICTION_SOURCE,
        CLOSED_UK_LIST_COMPACT_PENALTY,
        None,
    )
    validate_penalty_source_evidence(
        CLOSED_UK_LIST_JURISDICTION_SOURCE,
        None,
        {"other": CLOSED_UK_LIST_COMPACT_PENALTY},
    )


@pytest.mark.parametrize(
    "output",
    (
        CLOSED_UK_LIST_ALIAS + " штата Liberty",
        CLOSED_UK_LIST_ALIAS + " штата San Fierro",
    ),
    ids=("changed-one-token-jurisdiction", "changed-named-jurisdiction"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_alias_cannot_invent_a_jurisdiction(
    output: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_JURISDICTION_SOURCE, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_explicit_numeric_list_keeps_its_jurisdiction(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            CLOSED_UK_LIST_JURISDICTION_SOURCE,
            CLOSED_UK_LIST_EXPLICIT,
        )
    validate(
        CLOSED_UK_LIST_JURISDICTION_SOURCE,
        CLOSED_UK_LIST_EXPLICIT + " штата SA",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_output_cannot_add_jurisdiction_to_plain_source(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, CLOSED_UK_LIST_ALIAS + " штата SA")


def test_v3_closed_uk_penalty_cannot_change_or_add_jurisdiction() -> None:
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            CLOSED_UK_LIST_JURISDICTION_SOURCE,
            CLOSED_UK_LIST_COMPACT_PENALTY + " штата Liberty",
            None,
        )


@pytest.mark.parametrize(
    "leading_context",
    (
        "При санкции суда",
        "На основании приказа",
        "В течение проверки",
        "По окончании проверки",
        "Несмотря на запрет",
        "В отдельных случаях",
        "Теоретически",
        "По неподтвержденной информации",
        "Обычно",
        "Иногда",
        "В редких случаях",
        "При отсутствии возражений",
        "С учетом обстоятельств",
    ),
)
@pytest.mark.parametrize(
    "with_jurisdiction",
    (False, True),
    ids=("plain-list", "projected-local-jurisdiction"),
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
def test_v3_closed_uk_alias_cannot_drop_any_leading_clause_context(
    leading_context: str,
    with_jurisdiction: bool,
    validate: RelationValidator,
) -> None:
    source_body = (
        CLOSED_UK_LIST_JURISDICTION_SOURCE
        if with_jurisdiction
        else CLOSED_UK_LIST_SOURCE
    )
    source = f"{leading_context}, {source_body}"
    output = _closed_uk_output_for_path(
        validate,
        alias=CLOSED_UK_LIST_ALIAS,
        penalty=CLOSED_UK_LIST_COMPACT_PENALTY,
    )
    with pytest.raises(ValueError):
        validate(source, output)


_CLOSED_UK_ACTIVE_NUMERIC_SOURCE = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
    "при задержании только в случае нарушения любой из статей: "
    "6.1.1; 6.2; 6.3; 7.1 УК."
)


def _closed_uk_patient_infix_source(context: str) -> str:
    return CLOSED_UK_LIST_SOURCE.replace(
        "Лицензия на оружие должна",
        f"Лицензия на оружие, {context}, должна",
    )


def _closed_uk_actor_infix_source(context: str) -> str:
    return _CLOSED_UK_ACTIVE_NUMERIC_SOURCE.replace(
        "Правоохранители обязаны",
        f"Правоохранители, {context}, обязаны",
    )


def _closed_uk_patient_infix_output(output: str, context: str) -> str:
    patient = (
        "лицензии на оружие"
        if output == CLOSED_UK_LIST_COMPACT_PENALTY
        else "лицензию на оружие"
    )
    return output.replace(patient, f"{patient}, {context},")


def _closed_uk_actor_infix_output(output: str, context: str) -> str:
    if output == CLOSED_UK_LIST_COMPACT_PENALTY:
        return _closed_uk_patient_infix_output(output, context)
    return output.replace(
        "правоохранители обязаны",
        f"правоохранители, {context}, обязаны",
    )


_CLOSED_UK_CONTEXT_DROP_SOURCES = (
    f"При санкции суда, {CLOSED_UK_LIST_SOURCE}",
    "При санкции суда " + CLOSED_UK_LIST_SOURCE[0].lower() + CLOSED_UK_LIST_SOURCE[1:],
    "При санкции суда — " + CLOSED_UK_LIST_SOURCE[0].lower() + CLOSED_UK_LIST_SOURCE[1:],
    "При санкции суда: " + CLOSED_UK_LIST_SOURCE[0].lower() + CLOSED_UK_LIST_SOURCE[1:],
    f"Только при санкции суда, {CLOSED_UK_LIST_SOURCE}",
    _closed_uk_patient_infix_source("при санкции суда"),
    _closed_uk_actor_infix_source("при санкции суда"),
    CLOSED_UK_LIST_SOURCE.replace(
        "аннулирована и изъята",
        "аннулирована, при санкции суда, и изъята",
    ),
)


@pytest.mark.parametrize(
    "source",
    _CLOSED_UK_CONTEXT_DROP_SOURCES,
    ids=(
        "comma",
        "no-punctuation",
        "dash",
        "colon",
        "focused",
        "patient-infix",
        "actor-infix",
        "between-actions",
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
def test_v3_closed_uk_alias_cannot_drop_structural_context(
    source: str,
    validate: RelationValidator,
) -> None:
    output = _closed_uk_output_for_path(
        validate,
        alias=CLOSED_UK_LIST_ALIAS,
        penalty=CLOSED_UK_LIST_COMPACT_PENALTY,
    )
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source_context", "output_context"),
    (
        ("при санкции суда", "при санкции прокурора"),
        ("иногда", "обычно"),
    ),
    ids=("changed-authority", "changed-frequency"),
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
def test_v3_closed_uk_alias_cannot_change_leading_context(
    source_context: str,
    output_context: str,
    validate: RelationValidator,
) -> None:
    source = f"{source_context.capitalize()}, {CLOSED_UK_LIST_SOURCE}"
    base_output = _closed_uk_output_for_path(
        validate,
        alias=CLOSED_UK_LIST_ALIAS,
        penalty=CLOSED_UK_LIST_COMPACT_PENALTY,
    )
    with pytest.raises(ValueError):
        validate(source, f"{output_context}, {base_output}")


@pytest.mark.parametrize(
    "added_context",
    ("при санкции суда", "иногда"),
    ids=("added-authority", "added-frequency"),
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
def test_v3_closed_uk_alias_cannot_add_leading_context(
    added_context: str,
    validate: RelationValidator,
) -> None:
    base_output = _closed_uk_output_for_path(
        validate,
        alias=CLOSED_UK_LIST_ALIAS,
        penalty=CLOSED_UK_LIST_COMPACT_PENALTY,
    )
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, f"{added_context}, {base_output}")


_CLOSED_UK_CONTEXT_POSITIVE_CASES = (
    (
        f"При санкции суда, {CLOSED_UK_LIST_SOURCE}",
        f"при санкции суда, {CLOSED_UK_LIST_ALIAS}",
        f"при санкции суда, {CLOSED_UK_LIST_COMPACT_PENALTY}",
    ),
    (
        "При санкции суда " + CLOSED_UK_LIST_SOURCE[0].lower() + CLOSED_UK_LIST_SOURCE[1:],
        "при санкции суда " + CLOSED_UK_LIST_ALIAS,
        "при санкции суда " + CLOSED_UK_LIST_COMPACT_PENALTY,
    ),
    (
        "При санкции суда — " + CLOSED_UK_LIST_SOURCE[0].lower() + CLOSED_UK_LIST_SOURCE[1:],
        "при санкции суда — " + CLOSED_UK_LIST_ALIAS,
        "при санкции суда — " + CLOSED_UK_LIST_COMPACT_PENALTY,
    ),
    (
        "При санкции суда: " + CLOSED_UK_LIST_SOURCE[0].lower() + CLOSED_UK_LIST_SOURCE[1:],
        "при санкции суда: " + CLOSED_UK_LIST_ALIAS,
        "при санкции суда: " + CLOSED_UK_LIST_COMPACT_PENALTY,
    ),
    (
        _closed_uk_patient_infix_source("при санкции суда"),
        _closed_uk_patient_infix_output(CLOSED_UK_LIST_ALIAS, "при санкции суда"),
        _closed_uk_patient_infix_output(CLOSED_UK_LIST_COMPACT_PENALTY, "при санкции суда"),
    ),
    (
        _closed_uk_actor_infix_source("при санкции суда"),
        _closed_uk_actor_infix_output(CLOSED_UK_LIST_ALIAS, "при санкции суда"),
        _closed_uk_actor_infix_output(CLOSED_UK_LIST_COMPACT_PENALTY, "при санкции суда"),
    ),
)


@pytest.mark.parametrize(
    ("source", "alias_output", "penalty_output"),
    _CLOSED_UK_CONTEXT_POSITIVE_CASES,
    ids=(
        "comma",
        "no-punctuation",
        "dash",
        "colon",
        "patient-infix",
        "actor-infix",
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
def test_v3_closed_uk_alias_preserves_same_structural_context(
    source: str,
    alias_output: str,
    penalty_output: str,
    validate: RelationValidator,
) -> None:
    validate(
        source,
        _closed_uk_output_for_path(
            validate,
            alias=alias_output,
            penalty=penalty_output,
        ),
    )


_CLOSED_UK_PRE_MODAL_GLOBAL_CONTEXT_REORDER_CASES = (
    (
        "Правоохранители, при санкции суда, обязаны аннулировать и изъять "
        "лицензию на оружие при задержании только в случае нарушения любой "
        "из статей: 6.1; 6.2 УК.",
        "Правоохранители, при санкции суда, обязаны изъять и аннулировать "
        "лицензию на оружие при задержании за любую статью списка УК.",
    ),
    (
        "Лицензия на оружие, при санкции суда, должна быть аннулирована и "
        "изъята при задержании сотрудниками правоохранительных органов "
        "только в случае нарушения любой из статей: 6.1; 6.2 УК.",
        "Лицензия на оружие, при санкции суда, должна быть изъята и "
        "аннулирована при задержании сотрудниками правоохранительных органов "
        "за любую статью списка УК.",
    ),
)


@pytest.mark.parametrize(
    ("source", "output"),
    _CLOSED_UK_PRE_MODAL_GLOBAL_CONTEXT_REORDER_CASES,
    ids=("actor-infix", "passive-patient-infix"),
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
def test_v3_closed_uk_pre_modal_context_stays_global_when_actions_reorder(
    source: str,
    output: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)


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
def test_v3_closed_uk_clean_base_remains_supported_in_every_output_path(
    validate: RelationValidator,
) -> None:
    validate(
        CLOSED_UK_LIST_SOURCE,
        _closed_uk_output_for_path(
            validate,
            alias=CLOSED_UK_LIST_ALIAS,
            penalty=CLOSED_UK_LIST_COMPACT_PENALTY,
        ),
    )


_CLOSED_UK_PATIENT_FIRST_CONTEXT_SOURCE = (
    "Лицензию на оружие, при санкции суда, правоохранители обязаны "
    "аннулировать и изъять при задержании только в случае нарушения "
    "любой из статей: 6.1.1; 6.2; 6.3; 7.1 УК."
)
_CLOSED_UK_PATIENT_FIRST_CONTEXT_ALIAS = (
    "лицензию на оружие, при санкции суда, правоохранители обязаны "
    "аннулировать и изъять при задержании за любую статью из списка УК"
)
_CLOSED_UK_PATIENT_FIRST_CONTEXT_PENALTY = (
    _CLOSED_UK_PATIENT_FIRST_CONTEXT_ALIAS
)


@pytest.mark.parametrize(
    ("active_actions", "nominal_actions"),
    (
        ("аннулировать", "аннулирование"),
        ("изъять", "изъятие"),
    ),
    ids=("dropped-seizure", "dropped-invalidation"),
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
def test_v3_closed_uk_patient_first_context_keeps_both_co_actions(
    active_actions: str,
    nominal_actions: str,
    validate: RelationValidator,
) -> None:
    positive = _closed_uk_output_for_path(
        validate,
        alias=_CLOSED_UK_PATIENT_FIRST_CONTEXT_ALIAS,
        penalty=_CLOSED_UK_PATIENT_FIRST_CONTEXT_PENALTY,
    )
    validate(_CLOSED_UK_PATIENT_FIRST_CONTEXT_SOURCE, positive)
    unsafe = positive.replace(
        "аннулировать и изъять",
        active_actions,
    ).replace(
        "аннулирование и изъятие",
        nominal_actions,
    )
    with pytest.raises(ValueError):
        validate(_CLOSED_UK_PATIENT_FIRST_CONTEXT_SOURCE, unsafe)


_CLOSED_UK_PER_ACTION_CONTEXT_SOURCE = (
    "Правоохранители обязаны при санкции суда аннулировать оружейную лицензию "
    "и при санкции прокурора изъять оружейную лицензию при задержании только в случае "
    "нарушения любой из статей: 6.1.1; 6.2; 6.3; 7.1 УК."
)
_CLOSED_UK_PER_ACTION_CONTEXT_ALIAS = (
    "правоохранители обязаны при санкции суда аннулировать оружейную лицензию "
    "и при санкции прокурора изъять оружейную лицензию при задержании "
    "за любую статью из списка УК"
)
_CLOSED_UK_PER_ACTION_CONTEXT_PENALTY = (
    "аннулирование оружейной лицензии при санкции суда и изъятие оружейной лицензии "
    "при санкции прокурора при задержании за любую статью списка УК"
)


def _closed_uk_swap_action_contexts(output: str) -> str:
    sentinel = "при санкции __temporary_authority__"
    return (
        output.replace("при санкции суда", sentinel, 1)
        .replace("при санкции прокурора", "при санкции суда", 1)
        .replace(sentinel, "при санкции прокурора", 1)
    )


def _closed_uk_drop_first_context_action(output: str) -> str:
    return output.replace(
        "при санкции суда аннулировать оружейную лицензию и ",
        "",
    ).replace(
        "аннулирование оружейной лицензии при санкции суда и ",
        "",
    )


def _closed_uk_drop_second_context_action(output: str) -> str:
    return output.replace(
        " и при санкции прокурора изъять оружейную лицензию",
        "",
    ).replace(
        " и изъятие оружейной лицензии при санкции прокурора",
        "",
    )


def _closed_uk_drop_first_action_context(output: str) -> str:
    return output.replace("при санкции суда ", "", 1)


def _closed_uk_drop_second_action_context(output: str) -> str:
    return output.replace("при санкции прокурора ", "", 1)


@pytest.mark.parametrize(
    "unsafe_transform",
    (
        _closed_uk_swap_action_contexts,
        lambda output: output.replace(
            "при санкции прокурора",
            "при санкции суда",
        ),
        _closed_uk_drop_first_context_action,
        _closed_uk_drop_second_context_action,
        _closed_uk_drop_first_action_context,
        _closed_uk_drop_second_action_context,
    ),
    ids=(
        "swapped-contexts",
        "both-court",
        "dropped-first-action",
        "dropped-second-action",
        "dropped-first-context",
        "dropped-second-context",
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
def test_v3_closed_uk_keeps_each_context_with_its_owned_action(
    unsafe_transform: Callable[[str], str],
    validate: RelationValidator,
) -> None:
    positive = _closed_uk_output_for_path(
        validate,
        alias=_CLOSED_UK_PER_ACTION_CONTEXT_ALIAS,
        penalty=_CLOSED_UK_PER_ACTION_CONTEXT_PENALTY,
    )
    validate(_CLOSED_UK_PER_ACTION_CONTEXT_SOURCE, positive)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PER_ACTION_CONTEXT_SOURCE,
            unsafe_transform(positive),
        )


_CLOSED_UK_REORDERED_ACTION_PAIRS_ALIAS = (
    "правоохранители обязаны при санкции прокурора изъять оружейную лицензию "
    "и при санкции суда аннулировать оружейную лицензию при задержании "
    "за любую статью из списка УК"
)
_CLOSED_UK_REORDERED_ACTION_PAIRS_PENALTY = (
    "изъятие оружейной лицензии при санкции прокурора и аннулирование оружейной лицензии "
    "при санкции суда при задержании за любую статью списка УК"
)
_CLOSED_UK_SWAPPED_ACTIONS_ONLY_ALIAS = (
    "правоохранители обязаны при санкции суда изъять оружейную лицензию "
    "и при санкции прокурора аннулировать оружейную лицензию при задержании "
    "за любую статью из списка УК"
)
_CLOSED_UK_SWAPPED_ACTIONS_ONLY_PENALTY = (
    "изъятие оружейной лицензии при санкции суда и аннулирование оружейной лицензии "
    "при санкции прокурора при задержании за любую статью списка УК"
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
def test_v3_closed_uk_may_reorder_complete_action_context_pairs(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_PER_ACTION_CONTEXT_SOURCE,
        _closed_uk_output_for_path(
            validate,
            alias=_CLOSED_UK_REORDERED_ACTION_PAIRS_ALIAS,
            penalty=_CLOSED_UK_REORDERED_ACTION_PAIRS_PENALTY,
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
def test_v3_closed_uk_cannot_reorder_actions_without_their_contexts(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PER_ACTION_CONTEXT_SOURCE,
            _closed_uk_output_for_path(
                validate,
                alias=_CLOSED_UK_SWAPPED_ACTIONS_ONLY_ALIAS,
                penalty=_CLOSED_UK_SWAPPED_ACTIONS_ONLY_PENALTY,
            ),
        )


_CLOSED_UK_SAME_AUTHORITY_SOURCE = (
    _CLOSED_UK_PER_ACTION_CONTEXT_SOURCE.replace(
        "при санкции прокурора",
        "при санкции суда",
    )
)
_CLOSED_UK_SAME_AUTHORITY_ALIAS = (
    _CLOSED_UK_PER_ACTION_CONTEXT_ALIAS.replace(
        "при санкции прокурора",
        "при санкции суда",
    )
)
_CLOSED_UK_SAME_AUTHORITY_PENALTY = (
    _CLOSED_UK_PER_ACTION_CONTEXT_PENALTY.replace(
        "при санкции прокурора",
        "при санкции суда",
    )
)


def _closed_uk_drop_nth_court_context(output: str, occurrence: int) -> str:
    marker = "при санкции суда "
    start = -1
    for _ in range(occurrence):
        start = output.find(marker, start + 1)
        if start < 0:
            return output
    return output[:start] + output[start + len(marker):]


@pytest.mark.parametrize(
    "occurrence",
    (1, 2),
    ids=("dropped-first-context", "dropped-second-context"),
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
def test_v3_closed_uk_preserves_same_authority_for_each_owned_action(
    occurrence: int,
    validate: RelationValidator,
) -> None:
    positive = _closed_uk_output_for_path(
        validate,
        alias=_CLOSED_UK_SAME_AUTHORITY_ALIAS,
        penalty=_CLOSED_UK_SAME_AUTHORITY_PENALTY,
    )
    validate(_CLOSED_UK_SAME_AUTHORITY_SOURCE, positive)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SAME_AUTHORITY_SOURCE,
            _closed_uk_drop_nth_court_context(positive, occurrence),
        )


_CLOSED_UK_SEIZURE_ONLY_CONTEXT_SOURCE = (
    _CLOSED_UK_PER_ACTION_CONTEXT_SOURCE
    .replace("при санкции суда ", "", 1)
    .replace(
        "при санкции прокурора",
        "при санкции суда",
        1,
    )
)
_CLOSED_UK_SEIZURE_ONLY_CONTEXT_ALIAS = (
    _CLOSED_UK_PER_ACTION_CONTEXT_ALIAS
    .replace("при санкции суда ", "", 1)
    .replace(
        "при санкции прокурора",
        "при санкции суда",
        1,
    )
)
_CLOSED_UK_SEIZURE_ONLY_CONTEXT_PENALTY = (
    _CLOSED_UK_PER_ACTION_CONTEXT_PENALTY
    .replace("при санкции суда ", "", 1)
    .replace(
        "при санкции прокурора",
        "при санкции суда",
        1,
    )
)
_CLOSED_UK_INVALIDATION_ONLY_CONTEXT_ALIAS = (
    _CLOSED_UK_PER_ACTION_CONTEXT_ALIAS.replace(
        "при санкции прокурора ",
        "",
        1,
    )
)
_CLOSED_UK_INVALIDATION_ONLY_CONTEXT_PENALTY = (
    _CLOSED_UK_PER_ACTION_CONTEXT_PENALTY.replace(
        "при санкции прокурора ",
        "",
        1,
    )
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
def test_v3_closed_uk_single_nonleading_context_stays_with_its_action(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SEIZURE_ONLY_CONTEXT_SOURCE,
        _closed_uk_output_for_path(
            validate,
            alias=_CLOSED_UK_SEIZURE_ONLY_CONTEXT_ALIAS,
            penalty=_CLOSED_UK_SEIZURE_ONLY_CONTEXT_PENALTY,
        ),
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SEIZURE_ONLY_CONTEXT_SOURCE,
            _closed_uk_output_for_path(
                validate,
                alias=_CLOSED_UK_INVALIDATION_ONLY_CONTEXT_ALIAS,
                penalty=_CLOSED_UK_INVALIDATION_ONLY_CONTEXT_PENALTY,
            ),
        )


_CLOSED_UK_GLOBAL_CONTEXT_SOURCE = (
    "При санкции суда, "
    + _CLOSED_UK_PER_ACTION_CONTEXT_SOURCE
    .replace("при санкции суда ", "", 1)
    .replace("и при санкции прокурора ", "и ", 1)
)
_CLOSED_UK_GLOBAL_CONTEXT_ALIAS = (
    "при санкции суда, "
    + _CLOSED_UK_PER_ACTION_CONTEXT_ALIAS
    .replace("при санкции суда ", "", 1)
    .replace("и при санкции прокурора ", "и ", 1)
)
_CLOSED_UK_GLOBAL_CONTEXT_REORDERED_ALIAS = (
    "при санкции суда, правоохранители обязаны "
    "изъять оружейную лицензию и аннулировать оружейную лицензию при задержании "
    "за любую статью из списка УК"
)
_CLOSED_UK_GLOBAL_CONTEXT_PENALTY = (
    "при санкции суда, аннулирование и изъятие оружейной лицензии "
    "при задержании за любую статью списка УК"
)
_CLOSED_UK_GLOBAL_CONTEXT_REORDERED_PENALTY = (
    "при санкции суда, изъятие и аннулирование оружейной лицензии "
    "при задержании за любую статью списка УК"
)


@pytest.mark.parametrize("reordered", (False, True), ids=("same-order", "reordered"))
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
def test_v3_closed_uk_clause_leading_context_remains_global(
    reordered: bool,
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_GLOBAL_CONTEXT_SOURCE,
        _closed_uk_output_for_path(
            validate,
            alias=(
                _CLOSED_UK_GLOBAL_CONTEXT_REORDERED_ALIAS
                if reordered
                else _CLOSED_UK_GLOBAL_CONTEXT_ALIAS
            ),
            penalty=(
                _CLOSED_UK_GLOBAL_CONTEXT_REORDERED_PENALTY
                if reordered
                else _CLOSED_UK_GLOBAL_CONTEXT_PENALTY
            ),
        ),
    )


_CLOSED_UK_PER_ACTION_POLARITY_SOURCE = (
    "Правоохранители обязаны не аннулировать оружейную лицензию и изъять оружейную лицензию "
    "при задержании только в случае нарушения любой из статей: "
    "6.1.1; 6.2; 6.3; 7.1 УК."
)
_CLOSED_UK_PER_ACTION_POLARITY_ALIAS = (
    "правоохранители обязаны не аннулировать оружейную лицензию и изъять оружейную лицензию "
    "при задержании за любую статью из списка УК"
)


def _closed_uk_swap_action_polarity(output: str) -> str:
    return output.replace(
        "не аннулировать",
        "аннулировать",
        1,
    ).replace(" и изъять", " и не изъять", 1)


@pytest.mark.parametrize(
    "unsafe_transform",
    (
        _closed_uk_swap_action_polarity,
        lambda output: output.replace(" и изъять", " и не изъять", 1),
        lambda output: output.replace(" не аннулировать", " аннулировать", 1),
    ),
    ids=("swapped-polarity", "both-negative", "dropped-negation"),
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
def test_v3_closed_uk_keeps_polarity_with_its_owned_action(
    unsafe_transform: Callable[[str], str],
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_PER_ACTION_POLARITY_SOURCE,
        _CLOSED_UK_PER_ACTION_POLARITY_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PER_ACTION_POLARITY_SOURCE,
            unsafe_transform(_CLOSED_UK_PER_ACTION_POLARITY_ALIAS),
        )


_CLOSED_UK_JOINT_ACTOR_SOURCE = (
    "Правоохранители и прокуроры обязаны аннулировать и изъять лицензию на оружие "
    "при задержании только в случае нарушения любой из статей: "
    "6.1.1; 6.2; 6.3; 7.1 УК."
)
_CLOSED_UK_JOINT_ACTOR_ALIAS = (
    "правоохранители и прокуроры обязаны аннулировать и изъять лицензию на оружие "
    "при задержании за любую статью из списка УК"
)


@pytest.mark.parametrize(
    "output",
    (
        _CLOSED_UK_JOINT_ACTOR_ALIAS,
        _CLOSED_UK_JOINT_ACTOR_ALIAS.replace(
            "правоохранители и прокуроры",
            "прокуроры и правоохранители",
        ),
    ),
    ids=("same-order", "reversed-order"),
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
def test_v3_closed_uk_joint_actor_order_is_commutative(
    output: str,
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_JOINT_ACTOR_SOURCE, output)


@pytest.mark.parametrize(
    "unsafe_actor",
    (
        "правоохранители",
        "прокуроры",
        "правоохранители и судьи",
        "правоохранители, прокуроры и судьи",
    ),
    ids=("dropped-prosecutors", "dropped-police", "changed-member", "added-member"),
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
def test_v3_closed_uk_joint_actor_cannot_drop_change_or_add_a_member(
    unsafe_actor: str,
    validate: RelationValidator,
) -> None:
    unsafe = _CLOSED_UK_JOINT_ACTOR_ALIAS.replace(
        "правоохранители и прокуроры",
        unsafe_actor,
    )
    with pytest.raises(ValueError):
        validate(_CLOSED_UK_JOINT_ACTOR_SOURCE, unsafe)


_CLOSED_UK_EXPLICIT_JOINT_SOURCE = _CLOSED_UK_JOINT_ACTOR_SOURCE.replace(
    "Правоохранители и прокуроры обязаны",
    "Правоохранители и прокуроры совместно обязаны",
)
_CLOSED_UK_EXPLICIT_JOINT_ALIAS = _CLOSED_UK_JOINT_ACTOR_ALIAS.replace(
    "правоохранители и прокуроры обязаны",
    "правоохранители совместно с прокурорами обязаны",
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
def test_v3_closed_uk_explicit_joint_actor_forms_are_equivalent(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_EXPLICIT_JOINT_SOURCE,
        _CLOSED_UK_EXPLICIT_JOINT_ALIAS,
    )


@pytest.mark.parametrize(
    "unsafe_actor",
    (
        "правоохранители",
        "правоохранители совместно с судьями",
    ),
    ids=("dropped-member", "changed-member"),
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
def test_v3_closed_uk_explicit_joint_actor_keeps_every_member(
    unsafe_actor: str,
    validate: RelationValidator,
) -> None:
    unsafe = _CLOSED_UK_EXPLICIT_JOINT_ALIAS.replace(
        "правоохранители совместно с прокурорами",
        unsafe_actor,
    )
    with pytest.raises(ValueError):
        validate(_CLOSED_UK_EXPLICIT_JOINT_SOURCE, unsafe)


def _closed_uk_explicit_numeric_output(output: str) -> str:
    return output.replace(
        "за любую статью из списка УК",
        (
            "за нарушение любой из статей: "
            "6.1.1; 6.2; 6.3; 7.1 УК"
        ),
    )


@pytest.mark.parametrize(
    ("source", "positive", "unsafe"),
    (
        (
            _CLOSED_UK_JOINT_ACTOR_SOURCE,
            _closed_uk_explicit_numeric_output(
                _CLOSED_UK_JOINT_ACTOR_ALIAS
            ),
            _closed_uk_explicit_numeric_output(
                _CLOSED_UK_JOINT_ACTOR_ALIAS.replace(
                    "правоохранители и прокуроры",
                    "правоохранители",
                )
            ),
        ),
        (
            _CLOSED_UK_SAME_AUTHORITY_SOURCE,
            _closed_uk_explicit_numeric_output(
                _CLOSED_UK_SAME_AUTHORITY_ALIAS
            ),
            _closed_uk_explicit_numeric_output(
                _closed_uk_drop_nth_court_context(
                    _CLOSED_UK_SAME_AUTHORITY_ALIAS,
                    1,
                )
            ),
        ),
        (
            _CLOSED_UK_PER_ACTION_POLARITY_SOURCE,
            _closed_uk_explicit_numeric_output(
                _CLOSED_UK_PER_ACTION_POLARITY_ALIAS
            ),
            _closed_uk_explicit_numeric_output(
                _closed_uk_swap_action_polarity(
                    _CLOSED_UK_PER_ACTION_POLARITY_ALIAS
                )
            ),
        ),
    ),
    ids=("joint-member-drop", "condition-drop", "polarity-swap"),
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
def test_v3_closed_uk_numeric_branch_keeps_per_action_contracts(
    source: str,
    positive: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, positive)
    with pytest.raises(ValueError):
        validate(source, unsafe)


def _closed_uk_exact_surface_alias(source: str) -> str:
    return source.replace(
        (
            "только в случае нарушения любой из статей: "
            "6.1.1; 6.2; 6.3; 7.1 УК"
        ),
        "за любую статью из списка УК",
    )


_CLOSED_UK_NUMBERED_PATIENT_SOURCE = CLOSED_UK_LIST_SOURCE.replace(
    "Лицензия на оружие",
    "Лицензия №7 на оружие",
    1,
)
_CLOSED_UK_OWNED_PATIENT_SOURCE = CLOSED_UK_LIST_SOURCE.replace(
    "Лицензия на оружие",
    "Лицензия на оружие организации «Альфа»",
    1,
)
_CLOSED_UK_ACTIVE_PATIENT_SOURCE = CLOSED_UK_LIST_SOURCE.replace(
    "Лицензия на оружие",
    "Действующая лицензия на оружие",
    1,
)


@pytest.mark.parametrize(
    ("source", "positive", "unsafe"),
    (
        (
            _CLOSED_UK_NUMBERED_PATIENT_SOURCE,
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_NUMBERED_PATIENT_SOURCE
            ),
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_NUMBERED_PATIENT_SOURCE
            ).replace("№7 ", "", 1),
        ),
        (
            _CLOSED_UK_NUMBERED_PATIENT_SOURCE,
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_NUMBERED_PATIENT_SOURCE
            ),
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_NUMBERED_PATIENT_SOURCE
            ).replace("№7", "№8", 1),
        ),
        (
            _CLOSED_UK_OWNED_PATIENT_SOURCE,
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_OWNED_PATIENT_SOURCE
            ),
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_OWNED_PATIENT_SOURCE
            ).replace(" организации «Альфа»", "", 1),
        ),
        (
            _CLOSED_UK_OWNED_PATIENT_SOURCE,
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_OWNED_PATIENT_SOURCE
            ),
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_OWNED_PATIENT_SOURCE
            ).replace("«Альфа»", "«Бета»", 1),
        ),
        (
            _CLOSED_UK_ACTIVE_PATIENT_SOURCE,
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_ACTIVE_PATIENT_SOURCE
            ),
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_ACTIVE_PATIENT_SOURCE
            ).replace("Действующая ", "", 1),
        ),
        (
            _CLOSED_UK_ACTIVE_PATIENT_SOURCE,
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_ACTIVE_PATIENT_SOURCE
            ),
            _closed_uk_exact_surface_alias(
                _CLOSED_UK_ACTIVE_PATIENT_SOURCE
            ).replace("Действующая", "Недействующая", 1),
        ),
    ),
    ids=(
        "number-drop",
        "number-change",
        "owner-drop",
        "owner-change",
        "active-drop",
        "active-change",
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
def test_v3_closed_uk_exact_surface_alias_keeps_patient_qualifiers(
    source: str,
    positive: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, positive)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "detached_jurisdiction",
    (
        "; штат Liberty",
        ". Штат Liberty",
        "; регион Liberty",
    ),
    ids=("semicolon-state", "sentence-state", "semicolon-region"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_compact_alias_cannot_launder_detached_jurisdiction(
    detached_jurisdiction: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            CLOSED_UK_LIST_JURISDICTION_SOURCE,
            CLOSED_UK_LIST_ALIAS + detached_jurisdiction,
        )


@pytest.mark.parametrize(
    "detached_jurisdiction",
    ("; штат Liberty", ". Штат Liberty", "; регион Liberty"),
)
def test_v3_closed_uk_penalty_cannot_launder_detached_jurisdiction(
    detached_jurisdiction: str,
) -> None:
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            CLOSED_UK_LIST_JURISDICTION_SOURCE,
            CLOSED_UK_LIST_COMPACT_PENALTY + detached_jurisdiction,
            None,
        )
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            CLOSED_UK_LIST_JURISDICTION_SOURCE,
            None,
            {
                "other": (
                    CLOSED_UK_LIST_COMPACT_PENALTY
                    + detached_jurisdiction
                )
            },
        )


@pytest.mark.parametrize(
    "source_suffix",
    (
        "штата SA по решению суда",
        "штата SA, кроме статьи 6.1.1",
        "другого штата",
    ),
    ids=("decision-tail", "exclusion-tail", "not-a-proper-jurisdiction"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_alias_cannot_drop_non_jurisdiction_residue(
    source_suffix: str,
    validate: RelationValidator,
) -> None:
    source = CLOSED_UK_LIST_SOURCE.replace(
        " УК.",
        f" УК {source_suffix}.",
    )
    with pytest.raises(ValueError):
        validate(source, CLOSED_UK_LIST_ALIAS)


@pytest.mark.parametrize(
    "unsafe",
    (
        (
            "аннулирование и изъятие лицензии на оружие при задержании "
            "за любую статью из другого списка УК"
        ),
        (
            "аннулирование и изъятие лицензии на оружие при задержании "
            "за любую статью УК"
        ),
        (
            "аннулирование и изъятие лицензии на оружие "
            "за любую статью из списка УК"
        ),
        (
            "аннулирование и изъятие лицензии при задержании "
            "за любую статью из списка УК"
        ),
        (
            "изъятие лицензии на оружие при задержании "
            "за любую статью из списка УК"
        ),
    ),
    ids=(
        "wrong-list",
        "any-criminal-code-article",
        "no-detention",
        "generic-license",
        "dropped-co-action",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_article_list_formula_keeps_all_owned_facets(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_list_alias_requires_same_clause_explicit_list(
    validate: RelationValidator,
) -> None:
    source = (
        "В УК приведён закрытый список статей: 6.1.1; 6.2; 6.3; 7.1. "
        "Лицензия на оружие должна быть аннулирована и изъята при задержании "
        "за нарушение закона."
    )
    output = (
        "аннулирование и изъятие лицензии на оружие при задержании "
        "за любую статью из списка УК"
    )
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_explicit_closed_uk_list_keeps_exact_numeric_identity(
    validate: RelationValidator,
) -> None:
    validate(CLOSED_UK_LIST_SOURCE, CLOSED_UK_LIST_EXPLICIT)


@pytest.mark.parametrize(
    "validate",
    (_validate_title, _validate_full_set),
    ids=("title", "full-set"),
)
def test_v3_nominal_explicit_closed_uk_list_keeps_exact_numeric_identity(
    validate: RelationValidator,
) -> None:
    # A nominal title is safe, while a one-item synonym set must still carry
    # the source-required police authority.  The full set carries that role
    # through its verbatim source companion.
    validate(CLOSED_UK_LIST_SOURCE, CLOSED_UK_LIST_EXPLICIT_NOMINAL)


@pytest.mark.parametrize(
    "unsafe",
    (
        (
            "аннулирование и изъятие лицензии на оружие при задержании "
            "за нарушение любой из статей: 6.1.1; 99.9; 7.1 УК"
        ),
        CLOSED_UK_LIST_EXPLICIT.replace("6.3", "6.4"),
        CLOSED_UK_LIST_EXPLICIT.replace("; 7.1 УК", "; 6.4; 7.1 УК"),
        CLOSED_UK_LIST_EXPLICIT.replace("; 6.3", ""),
        CLOSED_UK_LIST_EXPLICIT.replace(
            "6.1.1; 6.2; 6.3; 7.1",
            "6.2; 6.1.1; 6.3; 7.1",
        ),
    ),
    ids=(
        "reviewer-replaced-and-dropped",
        "replaced",
        "added",
        "dropped",
        "reordered",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_explicit_closed_uk_list_rejects_changed_numeric_identity(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, unsafe)


@pytest.mark.parametrize(
    "source",
    (
        (
            "Лицензия на оружие должна быть аннулирована и изъята при "
            "задержании; справка может указывать любую из статей: "
            "6.1.1; 6.2; 6.3; 7.1 УК."
        ),
        CLOSED_UK_LIST_SOURCE.replace(
            " УК.",
            " УК, за исключением статьи 6.1.1.",
        ),
        "Если закон будет принят, " + CLOSED_UK_LIST_SOURCE,
        (
            "При наличии разрешения суда правоохранители обязаны "
            "аннулировать и изъять лицензию на оружие при задержании "
            "только в случае нарушения любой из статей: "
            "6.1.1; 6.2; 6.3; 7.1 УК."
        ),
        "Возможно, " + CLOSED_UK_LIST_SOURCE,
        "По слухам, " + CLOSED_UK_LIST_SOURCE,
    ),
    ids=(
        "unrelated-list-after-semicolon",
        "suffix-exclusion",
        "leading-if-prerequisite",
        "leading-prerequisite",
        "possibly",
        "rumor",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_alias_requires_one_complete_assertive_hard_clause(
    source: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, CLOSED_UK_LIST_ALIAS)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_alias_preserves_exact_detention_temporal_marker(
    validate: RelationValidator,
) -> None:
    unsafe = CLOSED_UK_LIST_ALIAS.replace(
        "при задержании",
        "после задержания",
    )
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_alias_rejects_invented_oblique_role(
    validate: RelationValidator,
) -> None:
    unsafe = (
        "аннулирование и изъятие лицензии на оружие при задержании "
        "за любую статью из списка УК при поддержке организации Альфа"
    )
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, unsafe)
    unsafe_before_alias = (
        "правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "при задержании при поддержке организации Альфа "
        "за любую статью из списка УК"
    )
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, unsafe_before_alias)


@pytest.mark.parametrize(
    "unsafe",
    (
        CLOSED_UK_LIST_ALIAS.replace("обязаны", "не обязаны"),
        CLOSED_UK_LIST_ALIAS.replace("обязаны", "могут"),
        CLOSED_UK_LIST_ALIAS.replace(" и изъять", " или изъять"),
        CLOSED_UK_LIST_ALIAS.replace(
            "обязаны аннулировать и изъять",
            "аннулировали и изъяли",
        ),
    ),
    ids=("negative", "may", "alternative-actions", "past-event"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_alias_keeps_polarity_modality_and_coordination(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_SOURCE, unsafe)


_CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_CASES = (
    (CLOSED_UK_LIST_ALTERNATIVE_ALIAS, _validate_title),
    (CLOSED_UK_LIST_ALTERNATIVE_ALIAS, _validate_single_synonym),
    (CLOSED_UK_LIST_ALTERNATIVE_ALIAS, _validate_full_set),
    (CLOSED_UK_LIST_ALTERNATIVE_ALIAS, _validate_penalty_summary),
    (CLOSED_UK_LIST_ALTERNATIVE_ALIAS, _validate_penalty_other),
    (CLOSED_UK_LIST_ALTERNATIVE_EXPLICIT, _validate_title),
    (CLOSED_UK_LIST_ALTERNATIVE_EXPLICIT, _validate_single_synonym),
    (CLOSED_UK_LIST_ALTERNATIVE_EXPLICIT, _validate_full_set),
)

_CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_IDS = (
    "compact-alias-title",
    "compact-alias-single-synonym",
    "compact-alias-full-set",
    "compact-alias-penalty-summary",
    "compact-alias-penalty-other",
    "explicit-numeric-list-title",
    "explicit-numeric-list-single-synonym",
    "explicit-numeric-list-full-set",
)


@pytest.mark.parametrize(
    ("output", "validate"),
    _CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_CASES,
    ids=_CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_IDS,
)
def test_v3_closed_uk_alternative_accepts_preserved_or_coordination(
    output: str,
    validate: RelationValidator,
) -> None:
    validate(CLOSED_UK_LIST_ALTERNATIVE_SOURCE, output)


@pytest.mark.parametrize(
    ("output", "validate"),
    _CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_CASES,
    ids=_CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_IDS,
)
def test_v3_closed_uk_alternative_rejects_only_first_action(
    output: str,
    validate: RelationValidator,
) -> None:
    only_first_action = output.replace(
        "аннулировать или изъять",
        "аннулировать",
        1,
    )
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_ALTERNATIVE_SOURCE, only_first_action)


@pytest.mark.parametrize(
    ("output", "validate"),
    _CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_CASES,
    ids=_CLOSED_UK_ALTERNATIVE_OUTPUT_PATH_IDS,
)
def test_v3_closed_uk_alternative_rejects_conjunctive_rewrite(
    output: str,
    validate: RelationValidator,
) -> None:
    conjunctive_actions = output.replace(" или ", " и ", 1)
    with pytest.raises(ValueError):
        validate(CLOSED_UK_LIST_ALTERNATIVE_SOURCE, conjunctive_actions)


_CLOSED_UK_REVIEW_NUMERIC_GROUND = (
    "при задержании только в случае нарушения любой из статей: "
    "6.1; 6.2 УК."
)
_CLOSED_UK_REVIEW_ALIAS_GROUND = (
    "при задержании за любую статью списка УК."
)
_CLOSED_UK_REVIEW_BASE_SOURCE = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_REVIEW_BASE_ALIAS = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)


_CLOSED_UK_POST_ACTION_FACET_CASES = (
    (
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "в пятницу "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "в пятницу "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        _CLOSED_UK_REVIEW_BASE_ALIAS,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "в субботу "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "в пятницу "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
    (
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие, "
        "возможно, "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие, "
        "возможно, "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        _CLOSED_UK_REVIEW_BASE_ALIAS,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие, "
        "вероятно, "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие, "
        "возможно, "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
    (
        "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
        "личное имущество "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
        "личное имущество "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        _CLOSED_UK_REVIEW_BASE_ALIAS,
        "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
        "служебное имущество "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
        "личное имущество "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
    (
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "с уведомлением владельца "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "с уведомлением владельца "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        _CLOSED_UK_REVIEW_BASE_ALIAS,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "с уведомлением прокурора "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "с уведомлением владельца "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
)


@pytest.mark.parametrize(
    ("source", "positive", "dropped", "changed", "added"),
    _CLOSED_UK_POST_ACTION_FACET_CASES,
    ids=("temporal", "epistemic", "second-patient", "notification"),
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
def test_v3_closed_uk_keeps_every_post_action_facet_before_ground(
    source: str,
    positive: str,
    dropped: str,
    changed: str,
    added: str,
    validate: RelationValidator,
) -> None:
    validate(source, positive)
    with pytest.raises(ValueError):
        validate(source, dropped)
    with pytest.raises(ValueError):
        validate(source, changed)
    with pytest.raises(ValueError):
        validate(_CLOSED_UK_REVIEW_BASE_SOURCE, added)


_CLOSED_UK_PROSECUTOR_NOTIFICATION_SOURCE = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
    "с уведомлением прокурора "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_PROSECUTOR_NOTIFICATION_ALIAS = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
    "с уведомлением прокурора "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_prosecutor_as_notification_recipient(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_PROSECUTOR_NOTIFICATION_SOURCE,
        _CLOSED_UK_PROSECUTOR_NOTIFICATION_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PROSECUTOR_NOTIFICATION_SOURCE,
            _CLOSED_UK_PROSECUTOR_NOTIFICATION_ALIAS.replace(
                "с уведомлением прокурора",
                "с уведомлением судьи",
                1,
            ),
        )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PROSECUTOR_NOTIFICATION_SOURCE,
            _CLOSED_UK_REVIEW_BASE_ALIAS,
        )


_CLOSED_UK_SECOND_PATIENT_FORWARD_SOURCE = (
    "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
    "личное имущество "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_SECOND_PATIENT_FORWARD_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
    "личное имущество "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_SECOND_PATIENT_REVERSE_SOURCE = (
    "Правоохранители обязаны изъять личное имущество и аннулировать "
    "лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_SECOND_PATIENT_REVERSE_ALIAS = (
    "Правоохранители обязаны изъять личное имущество и аннулировать "
    "лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_complete_action_patient_pairs_in_both_orders(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SECOND_PATIENT_REVERSE_SOURCE,
        _CLOSED_UK_SECOND_PATIENT_REVERSE_ALIAS,
    )
    validate(
        _CLOSED_UK_SECOND_PATIENT_REVERSE_SOURCE,
        _CLOSED_UK_SECOND_PATIENT_FORWARD_ALIAS,
    )
    validate(
        _CLOSED_UK_SECOND_PATIENT_FORWARD_SOURCE,
        _CLOSED_UK_SECOND_PATIENT_REVERSE_ALIAS,
    )

    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SECOND_PATIENT_REVERSE_SOURCE,
            _CLOSED_UK_SECOND_PATIENT_REVERSE_ALIAS.replace(
                "личное имущество",
                "служебное имущество",
                1,
            ),
        )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SECOND_PATIENT_REVERSE_SOURCE,
            _CLOSED_UK_REVIEW_BASE_ALIAS,
        )


_CLOSED_UK_INTER_ACTION_WEEKDAY_SOURCE = (
    "Правоохранители обязаны аннулировать лицензию на оружие только в пятницу "
    "и изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_INTER_ACTION_WEEKDAY_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие только в пятницу "
    "и изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_INTER_ACTION_WEEKDAY_REORDERED_ALIAS = (
    "Правоохранители обязаны изъять лицензию на оружие и только в пятницу "
    "аннулировать лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_inter_action_weekday_with_its_owned_action(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_INTER_ACTION_WEEKDAY_SOURCE,
        _CLOSED_UK_INTER_ACTION_WEEKDAY_ALIAS,
    )
    validate(
        _CLOSED_UK_INTER_ACTION_WEEKDAY_SOURCE,
        _CLOSED_UK_INTER_ACTION_WEEKDAY_REORDERED_ALIAS,
    )

    unsafe_outputs = (
        _CLOSED_UK_INTER_ACTION_WEEKDAY_ALIAS.replace(
            " только в пятницу",
            "",
            1,
        ),
        _CLOSED_UK_INTER_ACTION_WEEKDAY_ALIAS.replace("только в ", "в ", 1),
        _CLOSED_UK_INTER_ACTION_WEEKDAY_ALIAS.replace(
            "только в пятницу",
            "только в субботу",
            1,
        ),
        _CLOSED_UK_INTER_ACTION_WEEKDAY_ALIAS.replace(
            "аннулировать лицензию на оружие только в пятницу "
            "и изъять лицензию на оружие",
            "аннулировать лицензию на оружие и изъять лицензию на оружие "
            "только в пятницу",
            1,
        ),
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_INTER_ACTION_WEEKDAY_SOURCE, unsafe)


_CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_SOURCE = (
    "Правоохранители обязаны изъять личное имущество и аннулировать лицензию "
    "на оружие только в пятницу "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_ALIAS = (
    "Правоохранители обязаны изъять личное имущество и аннулировать лицензию "
    "на оружие только в пятницу "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_REORDERED_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие только в пятницу "
    "и изъять личное имущество "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_mixed_patient_trailing_facet_with_its_action(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_SOURCE,
        _CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_ALIAS,
    )
    validate(
        _CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_SOURCE,
        _CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_REORDERED_ALIAS,
    )

    unsafe_outputs = (
        "Правоохранители обязаны изъять личное имущество только в пятницу и "
        "аннулировать лицензию на оружие "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        _CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_ALIAS.replace(
            "только в пятницу",
            "в пятницу",
            1,
        ),
        _CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_ALIAS.replace(
            "только в пятницу",
            "только в субботу",
            1,
        ),
        "Правоохранители обязаны изъять и аннулировать лицензию на оружие "
        "только в пятницу "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Правоохранители обязаны изъять личное имущество и лицензию на оружие "
        "и аннулировать лицензию на оружие только в пятницу "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Правоохранители обязаны аннулировать лицензию на оружие только в "
        "пятницу и изъять личное имущество и лицензию на оружие "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_MIXED_PATIENT_TRAILING_WEEKDAY_SOURCE, unsafe)


_CLOSED_UK_ORDERED_ACTION_SOURCE = (
    "Правоохранители обязаны сначала аннулировать лицензию на оружие, затем "
    "изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_ORDERED_ACTION_ALIAS = (
    "Правоохранители обязаны сначала аннулировать лицензию на оружие, затем "
    "изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_REVERSED_ORDERED_ACTION_ALIAS = (
    "Правоохранители обязаны сначала изъять лицензию на оружие, затем "
    "аннулировать лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_preserves_explicit_action_sequence(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_ORDERED_ACTION_SOURCE, _CLOSED_UK_ORDERED_ACTION_ALIAS)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_ORDERED_ACTION_SOURCE,
            _CLOSED_UK_REVERSED_ORDERED_ACTION_ALIAS,
        )


_CLOSED_UK_MIXED_BRIDGE_SOURCE = (
    "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
    "лицензию на оружие или приостановить лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_MIXED_BRIDGE_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
    "лицензию на оружие или приостановить лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_REGROUPED_MIXED_BRIDGE_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие или изъять "
    "лицензию на оружие и приостановить лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_preserves_mixed_three_action_bridge_grouping(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_MIXED_BRIDGE_SOURCE, _CLOSED_UK_MIXED_BRIDGE_ALIAS)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_MIXED_BRIDGE_SOURCE,
            _CLOSED_UK_REGROUPED_MIXED_BRIDGE_ALIAS,
        )


_CLOSED_UK_PARENTHESIZED_MIXED_GROUP_SOURCE = (
    "Правоохранители обязаны аннулировать лицензию на оружие и (изъять "
    "лицензию на оружие или приостановить лицензию на оружие) "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_PARENTHESIZED_MIXED_GROUP_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие и (изъять "
    "лицензию на оружие или приостановить лицензию на оружие) "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_PARENTHESIZED_MIXED_GROUP_REORDERED_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие и (приостановить "
    "лицензию на оружие или изъять лицензию на оружие) "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_REGROUPED_SAME_CONNECTOR_PATH_ALIAS = (
    "Правоохранители обязаны (аннулировать лицензию на оружие и изъять "
    "лицензию на оружие) или приостановить лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_preserves_parenthesized_mixed_action_grouping(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_PARENTHESIZED_MIXED_GROUP_SOURCE,
        _CLOSED_UK_PARENTHESIZED_MIXED_GROUP_ALIAS,
    )
    validate(
        _CLOSED_UK_PARENTHESIZED_MIXED_GROUP_SOURCE,
        _CLOSED_UK_PARENTHESIZED_MIXED_GROUP_REORDERED_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PARENTHESIZED_MIXED_GROUP_SOURCE,
            _CLOSED_UK_REGROUPED_SAME_CONNECTOR_PATH_ALIAS,
        )


_CLOSED_UK_PER_ACTION_MODALITY_CASES = (
    (
        "Правоохранители обязаны аннулировать лицензию на оружие и вправе "
        "изъять лицензию на оружие "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Правоохранители обязаны аннулировать лицензию на оружие и вправе "
        "изъять лицензию на оружие "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        (
            "Правоохранители обязаны аннулировать лицензию на оружие и "
            "изъять лицензию на оружие "
            + _CLOSED_UK_REVIEW_ALIAS_GROUND,
            "Правоохранители вправе аннулировать лицензию на оружие и "
            "обязаны изъять лицензию на оружие "
            + _CLOSED_UK_REVIEW_ALIAS_GROUND,
            "Лицензию на оружие обязаны аннулировать и вправе изъять "
            + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        ),
    ),
    (
        "Лицензия на оружие может быть аннулирована и должна быть изъята "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Лицензия на оружие может быть аннулирована и должна быть изъята "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        (
            "Лицензия на оружие может быть аннулирована и изъята "
            + _CLOSED_UK_REVIEW_ALIAS_GROUND,
            "Лицензия на оружие должна быть аннулирована и может быть изъята "
            + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "positive", "unsafe_outputs"),
    _CLOSED_UK_PER_ACTION_MODALITY_CASES,
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
def test_v3_closed_uk_keeps_each_modality_with_its_owned_action(
    source: str,
    positive: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, positive)
    for unsafe in unsafe_outputs:
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
def test_v3_closed_uk_active_clause_cannot_drop_its_global_actor(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_REVIEW_BASE_SOURCE,
        _closed_uk_output_for_path(
            validate,
            alias=_CLOSED_UK_REVIEW_BASE_ALIAS,
            penalty=CLOSED_UK_LIST_COMPACT_PENALTY,
        ),
    )
    for unsafe_actor in ("Обязаны", "Субъекты обязаны"):
        unsafe = (
            f"{unsafe_actor} аннулировать и изъять лицензию на оружие "
            + _CLOSED_UK_REVIEW_ALIAS_GROUND
        )
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_REVIEW_BASE_SOURCE, unsafe)


_CLOSED_UK_PER_ACTION_ACTOR_CASES = (
    (
        "Лицензию на оружие правоохранители обязаны аннулировать, а прокуроры "
        "обязаны изъять "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Лицензию на оружие правоохранители обязаны аннулировать, а прокуроры "
        "обязаны изъять "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Лицензию на оружие прокуроры обязаны изъять, а правоохранители "
        "обязаны аннулировать "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Лицензию на оружие правоохранители обязаны аннулировать и изъять "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Лицензию на оружие правоохранители обязаны аннулировать, а судьи "
        "обязаны изъять "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
    (
        "Лицензия на оружие должна быть аннулирована правоохранителями и "
        "изъята прокурорами "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Лицензия на оружие должна быть аннулирована правоохранителями и "
        "изъята прокурорами "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Лицензия на оружие должна быть изъята прокурорами и аннулирована "
        "правоохранителями "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Лицензия на оружие должна быть аннулирована правоохранителями и "
        "изъята "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
        "Лицензия на оружие должна быть аннулирована правоохранителями и "
        "изъята судьями "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
)


@pytest.mark.parametrize(
    ("source", "same_order", "reordered", "dropped_actor", "changed_actor"),
    _CLOSED_UK_PER_ACTION_ACTOR_CASES,
    ids=("active-object-first", "passive"),
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
def test_v3_closed_uk_keeps_each_actor_with_its_complete_action_pair(
    source: str,
    same_order: str,
    reordered: str,
    dropped_actor: str,
    changed_actor: str,
    validate: RelationValidator,
) -> None:
    validate(source, same_order)
    validate(source, reordered)
    with pytest.raises(ValueError):
        validate(source, dropped_actor)
    with pytest.raises(ValueError):
        validate(source, changed_actor)


_CLOSED_UK_SAME_HEAD_ACTOR_SOURCE = (
    "Лицензию на оружие суд округа Блейн обязан аннулировать, а суд округа "
    "Лос-Сантос обязан изъять "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_SAME_HEAD_ACTOR_ALIAS = (
    "Лицензию на оружие суд округа Блейн обязан аннулировать, а суд округа "
    "Лос-Сантос обязан изъять "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_SAME_HEAD_ACTOR_REORDERED_ALIAS = (
    "Лицензию на оружие суд округа Лос-Сантос обязан изъять, а суд округа "
    "Блейн обязан аннулировать "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_SAME_HEAD_ACTOR_SWAPPED_ONLY_ALIAS = (
    "Лицензию на оружие суд округа Лос-Сантос обязан аннулировать, а суд "
    "округа Блейн обязан изъять "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_SAME_HEAD_ACTOR_CHANGED_ALIAS = (
    "Лицензию на оружие суд округа Палето обязан аннулировать, а суд округа "
    "Лос-Сантос обязан изъять "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_modified_same_head_actor_with_its_action(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_SAME_HEAD_ACTOR_SOURCE, _CLOSED_UK_SAME_HEAD_ACTOR_ALIAS)
    validate(
        _CLOSED_UK_SAME_HEAD_ACTOR_SOURCE,
        _CLOSED_UK_SAME_HEAD_ACTOR_REORDERED_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SAME_HEAD_ACTOR_SOURCE,
            _CLOSED_UK_SAME_HEAD_ACTOR_SWAPPED_ONLY_ALIAS,
        )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SAME_HEAD_ACTOR_SOURCE,
            _CLOSED_UK_SAME_HEAD_ACTOR_CHANGED_ALIAS,
        )


_CLOSED_UK_SAME_FAMILY_CORRELATION_SOURCE = (
    "Суд округа Блейн обязан аннулировать лицензию на оружие только в пятницу, "
    "а суд округа Лос-Сантос обязан аннулировать лицензию на оружие только в "
    "субботу "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_SAME_FAMILY_CORRELATION_ALIAS = (
    "Суд округа Блейн обязан аннулировать лицензию на оружие только в пятницу, "
    "а суд округа Лос-Сантос обязан аннулировать лицензию на оружие только в "
    "субботу "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_SAME_FAMILY_CORRELATION_REORDERED_ALIAS = (
    "Суд округа Лос-Сантос обязан аннулировать лицензию на оружие только в "
    "субботу, а суд округа Блейн обязан аннулировать лицензию на оружие только "
    "в пятницу "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_correlates_same_family_actor_and_weekday_multiset(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SAME_FAMILY_CORRELATION_SOURCE,
        _CLOSED_UK_SAME_FAMILY_CORRELATION_ALIAS,
    )
    validate(
        _CLOSED_UK_SAME_FAMILY_CORRELATION_SOURCE,
        _CLOSED_UK_SAME_FAMILY_CORRELATION_REORDERED_ALIAS,
    )

    unsafe_outputs = (
        _CLOSED_UK_SAME_FAMILY_CORRELATION_ALIAS
        .replace("только в пятницу", "__weekday_one__", 1)
        .replace("только в субботу", "только в пятницу", 1)
        .replace("__weekday_one__", "только в субботу", 1),
        _CLOSED_UK_SAME_FAMILY_CORRELATION_ALIAS.replace(
            " только в субботу",
            "",
            1,
        ),
        _CLOSED_UK_SAME_FAMILY_CORRELATION_ALIAS.replace(
            "суд округа Лос-Сантос",
            "суд округа Палето",
            1,
        ),
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_SAME_FAMILY_CORRELATION_SOURCE, unsafe)


_CLOSED_UK_NAMED_COURT_NOMINAL_SOURCE = (
    "Суд округа Блейн обязан аннулировать и изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_NAMED_COURT_NOMINAL_ALIAS = (
    "Аннулирование и изъятие лицензии на оружие судом округа Блейн "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_compact_nominal_keeps_explicit_named_actor(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_NAMED_COURT_NOMINAL_SOURCE,
        _CLOSED_UK_NAMED_COURT_NOMINAL_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_NAMED_COURT_NOMINAL_SOURCE,
            _CLOSED_UK_NAMED_COURT_NOMINAL_ALIAS.replace(
                " судом округа Блейн",
                "",
                1,
            ),
        )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_NAMED_COURT_NOMINAL_SOURCE,
            _CLOSED_UK_NAMED_COURT_NOMINAL_ALIAS.replace(
                "судом округа Блейн",
                "судом округа Лос-Сантос",
                1,
            ),
        )


_CLOSED_UK_RESTRICTIVE_AUTHORIZATION_SOURCE = (
    "Только при санкции суда, " + _CLOSED_UK_REVIEW_BASE_SOURCE
)
_CLOSED_UK_RESTRICTIVE_AUTHORIZATION_ALIAS = (
    "Только при санкции суда, " + _CLOSED_UK_REVIEW_BASE_ALIAS
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
def test_v3_closed_uk_preserves_restrictive_authorization_focus(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_SOURCE,
        _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_ALIAS,
    )
    validate(
        _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_SOURCE,
        _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_ALIAS.replace(
            "Только",
            "Лишь",
            1,
        ),
    )
    validate(
        _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_SOURCE,
        _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_ALIAS.replace(
            "Только",
            "Исключительно",
            1,
        ),
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_SOURCE,
            _CLOSED_UK_RESTRICTIVE_AUTHORIZATION_ALIAS.replace(
                "Только при",
                "При",
                1,
            ),
        )


_CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_SOURCE = (
    "Лицензия на оружие должна быть аннулирована, а изъята прокурором "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_ALIAS = (
    "Лицензия на оружие должна быть аннулирована, а изъята прокурором "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_trailing_passive_actor_local_to_second_action(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_SOURCE,
        _CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_ALIAS,
    )
    unsafe_outputs = (
        _CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_ALIAS.replace(
            "аннулирована, а изъята прокурором",
            "аннулирована прокурором, а изъята прокурором",
            1,
        ),
        _CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_ALIAS.replace(
            " изъята прокурором",
            " изъята",
            1,
        ),
        _CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_ALIAS.replace(
            "изъята прокурором",
            "изъята судьей",
            1,
        ),
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_TRAILING_LOCAL_PASSIVE_ACTOR_SOURCE, unsafe)


_CLOSED_UK_TRAILING_MANDATORY_SOURCE = (
    "Аннулирование и изъятие лицензии на оружие обязательно "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_TRAILING_MANDATORY_ALIAS = (
    "Аннулирование и изъятие лицензии на оружие обязательно "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_preserves_trailing_nominal_mandatory_marker(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_TRAILING_MANDATORY_SOURCE, _CLOSED_UK_TRAILING_MANDATORY_ALIAS)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_TRAILING_MANDATORY_SOURCE,
            _CLOSED_UK_TRAILING_MANDATORY_ALIAS.replace(" обязательно", "", 1),
        )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_TRAILING_MANDATORY_SOURCE,
            _CLOSED_UK_TRAILING_MANDATORY_ALIAS.replace(
                "обязательно",
                "возможно",
                1,
            ),
        )


_CLOSED_UK_COMPACT_LAUNDERING_CASES = (
    (
        "Лицензия на оружие может быть аннулирована и изъята "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Лицензия на оружие может быть аннулирована и изъята "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
    (
        "Лицензия на оружие была аннулирована и изъята "
        + _CLOSED_UK_REVIEW_NUMERIC_GROUND,
        "Лицензия на оружие была аннулирована и изъята "
        + _CLOSED_UK_REVIEW_ALIAS_GROUND,
    ),
)
_CLOSED_UK_GENERIC_ACTORLESS_NOMINAL_ALIAS = (
    "Аннулирование и изъятие лицензии на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)


@pytest.mark.parametrize(
    ("source", "exact_alias"),
    _CLOSED_UK_COMPACT_LAUNDERING_CASES,
    ids=("may", "past"),
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
def test_v3_closed_uk_compact_nominal_cannot_launder_modality_or_event(
    source: str,
    exact_alias: str,
    validate: RelationValidator,
) -> None:
    validate(source, exact_alias)
    with pytest.raises(ValueError):
        validate(source, _CLOSED_UK_GENERIC_ACTORLESS_NOMINAL_ALIAS)


_CLOSED_UK_DISTINCT_GENERIC_ACTOR_SOURCE = (
    "Правоохранители обязаны аннулировать лицензию на оружие, а прокуроры "
    "обязаны изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_DISTINCT_GENERIC_ACTOR_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие, а прокуроры "
    "обязаны изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_DISTINCT_GENERIC_ACTOR_REORDERED_ALIAS = (
    "Прокуроры обязаны изъять лицензию на оружие, а правоохранители обязаны "
    "аннулировать лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_distinct_action_actors_block_actorless_compaction(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_DISTINCT_GENERIC_ACTOR_SOURCE,
        _CLOSED_UK_DISTINCT_GENERIC_ACTOR_ALIAS,
    )
    validate(
        _CLOSED_UK_DISTINCT_GENERIC_ACTOR_SOURCE,
        _CLOSED_UK_DISTINCT_GENERIC_ACTOR_REORDERED_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_DISTINCT_GENERIC_ACTOR_SOURCE,
            _CLOSED_UK_GENERIC_ACTORLESS_NOMINAL_ALIAS,
        )


_CLOSED_UK_QUOTED_PATIENT_FACET_CASES = (
    ("с пометкой «Срочно»", "с пометкой «Обычно»"),
    (
        "с уведомлением «Вручено владельцу»",
        "с уведомлением «Не вручено владельцу»",
    ),
    (
        "при условии «Одобрено судом»",
        "при условии «Отклонено судом»",
    ),
)


@pytest.mark.parametrize(
    ("facet", "changed_facet"),
    _CLOSED_UK_QUOTED_PATIENT_FACET_CASES,
    ids=("note", "notification", "condition"),
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
def test_v3_closed_uk_bounds_quoted_patient_before_separate_quoted_facet(
    facet: str,
    changed_facet: str,
    validate: RelationValidator,
) -> None:
    prefix = (
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "организации «Альфа» "
    )
    source = prefix + facet + " " + _CLOSED_UK_REVIEW_NUMERIC_GROUND
    exact_alias = prefix + facet + " " + _CLOSED_UK_REVIEW_ALIAS_GROUND

    validate(source, exact_alias)
    with pytest.raises(ValueError):
        validate(source, exact_alias.replace(facet, changed_facet, 1))
    with pytest.raises(ValueError):
        validate(source, exact_alias.replace(f" {facet}", "", 1))
    with pytest.raises(ValueError):
        validate(source, exact_alias.replace("«Альфа»", "«Бета»", 1))


_CLOSED_UK_POST_CONDITION_OWNER_SOURCE = (
    "Правоохранители обязаны аннулировать лицензию на оружие только если суд "
    "одобрит, а прокуроры обязаны изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_POST_CONDITION_OWNER_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие только если суд "
    "одобрит, а прокуроры обязаны изъять лицензию на оружие "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_POST_CONDITION_OWNER_REORDERED_ALIAS = (
    "Прокуроры обязаны изъять лицензию на оружие, а правоохранители обязаны "
    "аннулировать лицензию на оружие только если суд одобрит "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_POST_CONDITION_MOVED_ONLY_ALIAS = (
    "Правоохранители обязаны аннулировать лицензию на оружие, а прокуроры "
    "обязаны изъять лицензию на оружие только если суд одобрит "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_post_condition_with_its_action_actor_pair(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_POST_CONDITION_OWNER_SOURCE,
        _CLOSED_UK_POST_CONDITION_OWNER_ALIAS,
    )
    validate(
        _CLOSED_UK_POST_CONDITION_OWNER_SOURCE,
        _CLOSED_UK_POST_CONDITION_OWNER_REORDERED_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_POST_CONDITION_OWNER_SOURCE,
            _CLOSED_UK_POST_CONDITION_MOVED_ONLY_ALIAS,
        )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_POST_CONDITION_OWNER_SOURCE,
            _CLOSED_UK_POST_CONDITION_OWNER_ALIAS.replace(
                " только если суд одобрит",
                "",
                1,
            ),
        )


_CLOSED_UK_MIXED_EVENT_SOURCE = (
    "Лицензия на оружие была аннулирована и будет изъята "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_MIXED_EVENT_ALIAS = (
    "Лицензия на оружие была аннулирована и будет изъята "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_keeps_each_event_with_its_owned_action(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_MIXED_EVENT_SOURCE, _CLOSED_UK_MIXED_EVENT_ALIAS)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_MIXED_EVENT_SOURCE,
            _CLOSED_UK_MIXED_EVENT_ALIAS.replace("будет изъята", "изъята", 1),
        )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_MIXED_EVENT_SOURCE,
            _CLOSED_UK_MIXED_EVENT_ALIAS.replace(
                "будет изъята",
                "была изъята",
                1,
            ),
        )


_CLOSED_UK_DETACHED_PERSONAL_PROPERTY_CLAUSE = (
    "Личное имущество подлежит изъятию."
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
def test_v3_closed_uk_exact_numeric_output_cannot_append_detached_claim(
    validate: RelationValidator,
) -> None:
    exact_output = _CLOSED_UK_REVIEW_BASE_SOURCE.rstrip(".")
    validate(_CLOSED_UK_REVIEW_BASE_SOURCE, exact_output)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_REVIEW_BASE_SOURCE,
            f"{exact_output}. {_CLOSED_UK_DETACHED_PERSONAL_PROPERTY_CLAUSE}",
        )


_CLOSED_UK_OPEN_PERSONAL_PROPERTY_SOURCE = (
    "Личное имущество подлежит изъятию "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS = (
    "Личное имущество подлежит изъятию "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
)
_CLOSED_UK_OPEN_PERSONAL_PROPERTY_EXPLICIT = (
    _CLOSED_UK_OPEN_PERSONAL_PROPERTY_SOURCE.rstrip(".")
)

_CLOSED_UK_OPEN_PATIENT_REPLACEMENT_PATH_CASES = (
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS, _validate_title),
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS, _validate_single_synonym),
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS, _validate_full_set),
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS, _validate_penalty_summary),
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS, _validate_penalty_other),
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_EXPLICIT, _validate_title),
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_EXPLICIT, _validate_single_synonym),
    (_CLOSED_UK_OPEN_PERSONAL_PROPERTY_EXPLICIT, _validate_full_set),
)

_CLOSED_UK_OPEN_PATIENT_REPLACEMENT_PATH_IDS = (
    "compact-alias-title",
    "compact-alias-single-synonym",
    "compact-alias-full-set",
    "compact-alias-penalty-summary",
    "compact-alias-penalty-other",
    "explicit-numeric-title",
    "explicit-numeric-single-synonym",
    "explicit-numeric-full-set",
)


@pytest.mark.parametrize(
    ("unsafe", "validate"),
    _CLOSED_UK_OPEN_PATIENT_REPLACEMENT_PATH_CASES,
    ids=_CLOSED_UK_OPEN_PATIENT_REPLACEMENT_PATH_IDS,
)
def test_v3_closed_uk_cannot_replace_whole_patient_with_open_claim(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(_CLOSED_UK_REVIEW_BASE_SOURCE, unsafe)


_CLOSED_UK_UNRELATED_OPEN_SOURCE = (
    "Личное имущество подлежит осмотру "
    + _CLOSED_UK_REVIEW_NUMERIC_GROUND
)
_CLOSED_UK_UNRELATED_OPEN_ALIAS = (
    "Личное имущество подлежит осмотру "
    + _CLOSED_UK_REVIEW_ALIAS_GROUND
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
def test_v3_closed_uk_open_personal_property_source_is_a_positive_control(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_OPEN_PERSONAL_PROPERTY_SOURCE,
        _CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS,
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_closed_uk_unrelated_open_predicate_control(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_UNRELATED_OPEN_SOURCE, _CLOSED_UK_UNRELATED_OPEN_ALIAS)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_UNRELATED_OPEN_SOURCE,
            _CLOSED_UK_OPEN_PERSONAL_PROPERTY_ALIAS,
        )


def test_v3_closed_uk_helper_is_scoped_to_license_revocation_query() -> None:
    for action, object_name in (
        ("fine", "fine"),
        ("instrument_revocation", "access_pass"),
        ("instrument_issue", "license"),
    ):
        evidence = _ground_formula_evidence(
            CLOSED_UK_LIST_SOURCE,
            action=action,
            object_name=object_name,
        )
        assert not any(
            claim.branch
            and claim.branch[0]
            in {
                "__closed_uk_article_list__",
                "__closed_uk_article_list_numeric__",
            }
            for claim in evidence
        )


def test_v3_closed_uk_numeric_identity_and_alias_use_separate_branches() -> None:
    source_branches = {
        claim.branch
        for claim in _ground_formula_evidence(
            CLOSED_UK_LIST_SOURCE,
            action="instrument_revocation",
            object_name="license",
        )
    }
    explicit_output_branches = {
        claim.branch
        for claim in _ground_formula_evidence(
            CLOSED_UK_LIST_EXPLICIT,
            action="instrument_revocation",
            object_name="license",
            allow_closed_uk_list_alias=True,
        )
    }
    alias_output_branches = {
        claim.branch
        for claim in _ground_formula_evidence(
            CLOSED_UK_LIST_ALIAS,
            action="instrument_revocation",
            object_name="license",
            allow_closed_uk_list_alias=True,
        )
    }
    numeric_branch = (
        "__closed_uk_article_list_numeric__",
        "6.1.1",
        "6.2",
        "6.3",
        "7.1",
    )
    alias_branch = ("__closed_uk_article_list__",)
    assert numeric_branch in source_branches
    assert alias_branch in source_branches
    assert explicit_output_branches == {numeric_branch}
    assert alias_output_branches == {alias_branch}


_CLOSED_UK_QUALIFIED_DETENTION_ACTOR_SOURCE = CLOSED_UK_LIST_SOURCE.replace(
    "при задержании сотрудниками правоохранительных органов",
    "при задержании дежурными сотрудниками полиции",
)
_CLOSED_UK_QUALIFIED_DETENTION_ACTOR_PASSIVE_ALIAS = (
    _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_SOURCE.replace(
        "только в случае нарушения любой из статей: "
        "6.1.1; 6.2; 6.3; 7.1 УК.",
        "за любую статью из списка УК",
    )
)
_CLOSED_UK_QUALIFIED_DETENTION_ACTOR_ACTIVE_ALIAS = (
    CLOSED_UK_LIST_ALIAS.replace(
        "правоохранители",
        "дежурные правоохранители",
        1,
    )
)


@pytest.mark.parametrize(
    "faithful",
    (
        _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_PASSIVE_ALIAS,
        _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_ACTIVE_ALIAS,
    ),
    ids=("passive-same-qualifier", "active-family-alias"),
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
def test_v3_closed_uk_keeps_qualified_detention_actor_across_voice(
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_QUALIFIED_DETENTION_ACTOR_SOURCE, faithful)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_SOURCE,
            CLOSED_UK_LIST_ALIAS,
        ),
        (
            _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_SOURCE,
            CLOSED_UK_LIST_ALIAS.replace(
                "правоохранители",
                "уполномоченные правоохранители",
                1,
            ),
        ),
        (
            _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_SOURCE,
            _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_PASSIVE_ALIAS.replace(
                "дежурными",
                "ответственными",
                1,
            ),
        ),
        (
            CLOSED_UK_LIST_SOURCE,
            _CLOSED_UK_QUALIFIED_DETENTION_ACTOR_ACTIVE_ALIAS,
        ),
    ),
    ids=("drop", "change-active", "change-passive", "add"),
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
def test_v3_closed_uk_cannot_drop_change_or_add_detention_actor_qualifier(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_EXACT_DETENTION_ACTOR_SURFACES = (
    "федеральными сотрудниками полиции",
    "старшими сотрудниками полиции",
    "региональными сотрудниками полиции",
    "сотрудниками федеральной полиции",
)


def _closed_uk_exact_detention_actor_source(actor: str) -> str:
    return CLOSED_UK_LIST_SOURCE.replace(
        "сотрудниками правоохранительных органов",
        actor,
        1,
    )


def _closed_uk_exact_detention_actor_alias(actor: str) -> str:
    return _closed_uk_exact_detention_actor_source(actor).replace(
        "только в случае нарушения любой из статей: "
        "6.1.1; 6.2; 6.3; 7.1 УК.",
        "за любую статью из списка УК",
        1,
    )


@pytest.mark.parametrize(
    "actor",
    _CLOSED_UK_EXACT_DETENTION_ACTOR_SURFACES,
    ids=("federal", "senior", "regional", "federal-police"),
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
def test_v3_closed_uk_keeps_unknown_detention_actor_modifier_exactly(
    actor: str,
    validate: RelationValidator,
) -> None:
    validate(
        _closed_uk_exact_detention_actor_source(actor),
        _closed_uk_exact_detention_actor_alias(actor),
    )


@pytest.mark.parametrize(
    ("source_actor", "unsafe_actor"),
    (
        ("федеральными сотрудниками полиции", "сотрудниками полиции"),
        (
            "федеральными сотрудниками полиции",
            "региональными сотрудниками полиции",
        ),
        ("сотрудниками федеральной полиции", "сотрудниками полиции"),
    ),
    ids=("drop-pre-head", "change-pre-head", "drop-post-head"),
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
def test_v3_closed_uk_cannot_launder_unknown_detention_actor_modifier(
    source_actor: str,
    unsafe_actor: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            _closed_uk_exact_detention_actor_source(source_actor),
            _closed_uk_exact_detention_actor_alias(unsafe_actor),
        )


_CLOSED_UK_TYPED_ALIAS_INTRODUCERS = (
    "за нарушение любой статьи из списка УК",
    "в случае нарушения любой статьи из списка УК",
    "только в случае нарушения любой статьи из списка УК",
)


@pytest.mark.parametrize(
    "ground",
    _CLOSED_UK_TYPED_ALIAS_INTRODUCERS,
    ids=("for-violation", "in-case", "only-in-case"),
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
def test_v3_closed_uk_accepts_complete_typed_alias_introducer(
    ground: str,
    validate: RelationValidator,
) -> None:
    validate(
        CLOSED_UK_LIST_SOURCE,
        CLOSED_UK_LIST_ALIAS.replace(
            "за любую статью из списка УК",
            ground,
            1,
        ),
    )


@pytest.mark.parametrize(
    "unsafe_ground",
    (
        "за плату за нарушение любой статьи из списка УК",
        "отвечая за нарушение любой статьи из списка УК",
        "из-за нарушения любой статьи из списка УК",
        "за за нарушение любой статьи из списка УК",
    ),
    ids=("paid", "unrelated-predicate", "because-of", "duplicate-for"),
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
def test_v3_closed_uk_typed_alias_introducer_keeps_unowned_prefix(
    unsafe_ground: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            CLOSED_UK_LIST_SOURCE,
            CLOSED_UK_LIST_ALIAS.replace(
                "за любую статью из списка УК",
                unsafe_ground,
                1,
            ),
        )


_CLOSED_UK_JOINT_POLICE_SOURCE = (
    "Лицензию на оружие дежурные сотрудники полиции и прокурор обязаны "
    "аннулировать и изъять при задержании только в случае нарушения любой "
    "из статей: 6.1; 6.2 УК."
)


def _closed_uk_joint_police_alias(actor: str) -> str:
    return (
        f"Лицензию на оружие {actor} обязаны аннулировать и изъять "
        "при задержании за любую статью списка УК."
    )


_CLOSED_UK_JOINT_POLICE_POSITIVES = (
    (
        _CLOSED_UK_JOINT_POLICE_SOURCE,
        _closed_uk_joint_police_alias(
            "дежурные правоохранители и прокурор"
        ),
    ),
    (
        _CLOSED_UK_JOINT_POLICE_SOURCE.replace(
            "дежурные сотрудники полиции",
            "дежурные правоохранители",
            1,
        ),
        _closed_uk_joint_police_alias(
            "дежурные сотрудники полиции и прокурор"
        ),
    ),
    (
        _CLOSED_UK_JOINT_POLICE_SOURCE,
        _closed_uk_joint_police_alias(
            "прокурор и дежурные правоохранители"
        ),
    ),
    (
        _CLOSED_UK_JOINT_POLICE_SOURCE.replace(
            "дежурные сотрудники полиции",
            "дежурные уполномоченные сотрудники полиции",
            1,
        ),
        _closed_uk_joint_police_alias(
            "прокурор и дежурные уполномоченные правоохранители"
        ),
    ),
    (
        _CLOSED_UK_JOINT_POLICE_SOURCE.replace(
            "и прокурор обязаны",
            "совместно с прокурором обязаны",
            1,
        ),
        _closed_uk_joint_police_alias(
            "дежурные правоохранители совместно с прокурором"
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful"),
    _CLOSED_UK_JOINT_POLICE_POSITIVES,
    ids=(
        "family-alias",
        "reverse-family-alias",
        "reordered-group",
        "two-qualifier-reordered-group",
        "joint-phrase",
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
def test_v3_closed_uk_canonicalizes_only_police_leaf_inside_actor_group(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


@pytest.mark.parametrize(
    "unsafe_actor",
    (
        "дежурные правоохранители",
        "дежурные правоохранители и судья",
        "правоохранители и прокурор",
        "уполномоченные правоохранители и прокурор",
        "ответственные дежурные правоохранители и прокурор",
        "дежурные правоохранители или прокурор",
    ),
    ids=(
        "drop-member",
        "change-member",
        "drop-qualifier",
        "change-qualifier",
        "add-qualifier",
        "change-logic",
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
def test_v3_closed_uk_actor_group_leaf_alias_keeps_other_semantics(
    unsafe_actor: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_JOINT_POLICE_SOURCE,
            _closed_uk_joint_police_alias(unsafe_actor),
        )


_CLOSED_UK_REVIEW_GROUND = (
    "только в случае нарушения любой из статей: "
    "6.1.1; 6.2; 6.3; 7.1 УК."
)
_CLOSED_UK_COMPACT_REVIEW_GROUND = "за любую статью из списка УК."


def _closed_uk_object_first_actor_source(actor: str) -> str:
    return (
        f"Лицензию на оружие {actor} обязаны аннулировать и "
        f"изъять при задержании {_CLOSED_UK_REVIEW_GROUND}"
    )


def _closed_uk_object_first_actor_alias(actor: str) -> str:
    return (
        f"Лицензию на оружие {actor} обязаны аннулировать и "
        f"изъять при задержании {_CLOSED_UK_COMPACT_REVIEW_GROUND}"
    )


_CLOSED_UK_ACTOR_MARKER_POSITIVES = (
    (
        _closed_uk_object_first_actor_source(
            "дежурные сотрудники полиции / прокурор"
        ),
        _closed_uk_object_first_actor_alias(
            "дежурные правоохранители / прокурор"
        ),
    ),
    (
        _closed_uk_object_first_actor_source(
            "(дежурные сотрудники полиции / прокурор) и судья"
        ),
        _closed_uk_object_first_actor_alias(
            "(дежурные правоохранители / прокурор) и судья"
        ),
    ),
    (
        _closed_uk_object_first_actor_source(
            "дежурные сотрудники полиции совместно с прокурором"
        ),
        _closed_uk_object_first_actor_alias(
            "дежурные правоохранители совместно с прокурором"
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful"),
    _CLOSED_UK_ACTOR_MARKER_POSITIVES,
    ids=("slash", "nested-slash", "explicit-joint"),
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
def test_v3_closed_uk_preserves_actor_marker_class_across_family_alias(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


_CLOSED_UK_ACTOR_MARKER_HOSTILES = (
    (
        _CLOSED_UK_ACTOR_MARKER_POSITIVES[0][0],
        _CLOSED_UK_ACTOR_MARKER_POSITIVES[0][1].replace(
            " / прокурор", " или прокурор", 1
        ),
    ),
    (
        _CLOSED_UK_ACTOR_MARKER_POSITIVES[1][0],
        _CLOSED_UK_ACTOR_MARKER_POSITIVES[1][1].replace(
            " / прокурор", " или прокурор", 1
        ),
    ),
    (
        _CLOSED_UK_ACTOR_MARKER_POSITIVES[2][0],
        _CLOSED_UK_ACTOR_MARKER_POSITIVES[2][1].replace(
            " совместно с прокурором", " и прокурор", 1
        ),
    ),
    (
        _closed_uk_object_first_actor_source(
            "дежурные сотрудники полиции и прокурор"
        ),
        _closed_uk_object_first_actor_alias(
            "дежурные правоохранители совместно с прокурором"
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    _CLOSED_UK_ACTOR_MARKER_HOSTILES,
    ids=("slash-to-or", "nested-slash-to-or", "joint-to-and", "and-to-joint"),
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
def test_v3_closed_uk_cannot_launder_actor_marker_class(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_OBJECT_FIRST_OWNER_SOURCE = _closed_uk_object_first_actor_source(
    "организации «Альфа» дежурные сотрудники полиции и прокурор"
)
_CLOSED_UK_OBJECT_FIRST_OWNER_ALIAS = _closed_uk_object_first_actor_alias(
    "организации «Альфа» дежурные правоохранители и прокурор"
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
def test_v3_closed_uk_object_first_owner_is_not_promoted_to_actor(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_OBJECT_FIRST_OWNER_SOURCE,
        _CLOSED_UK_OBJECT_FIRST_OWNER_ALIAS,
    )
    for unsafe in (
        _CLOSED_UK_OBJECT_FIRST_OWNER_ALIAS.replace(
            " организации «Альфа»", "", 1
        ),
        _CLOSED_UK_OBJECT_FIRST_OWNER_ALIAS.replace("«Альфа»", "«Бета»", 1),
        _CLOSED_UK_OBJECT_FIRST_OWNER_ALIAS.replace("и прокурор", "и судья", 1),
    ):
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_OBJECT_FIRST_OWNER_SOURCE, unsafe)


_CLOSED_UK_MERELY_SOURCE = (
    "Лицензия на оружие должна быть аннулирована и изъята при "
    "задержании сотрудниками полиции лишь в случае нарушения любой из "
    "статей: 6.1.1; 6.2; 6.3; 7.1 УК."
)
_CLOSED_UK_MERELY_ALIAS = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
    "при задержании лишь за нарушение любой статьи из списка УК."
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_focus_merely_is_not_deprivation_action(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_MERELY_SOURCE, _CLOSED_UK_MERELY_ALIAS)


def test_v3_closed_uk_deprivation_stem_keeps_real_actions_only() -> None:
    assert _RELATION_ACTION_RE.search("лишь в случае нарушения") is None
    assert _RELATION_ACTION_RE.search("суд может лишить лицензии") is not None
    assert _RELATION_ACTION_RE.search("лишение лицензии судом") is not None


_CLOSED_UK_LOCAL_DETENTION_CASES = (
    (
        "Лицензию на оружие правоохранители обязаны аннулировать, а изъять "
        f"при задержании {_CLOSED_UK_REVIEW_GROUND}",
        "Лицензию на оружие правоохранители обязаны аннулировать, а изъять "
        f"при задержании {_CLOSED_UK_COMPACT_REVIEW_GROUND}",
    ),
    (
        "Лицензию на оружие правоохранители обязаны аннулировать, затем изъять "
        f"при задержании {_CLOSED_UK_REVIEW_GROUND}",
        "Лицензию на оружие правоохранители обязаны аннулировать, затем изъять "
        f"при задержании {_CLOSED_UK_COMPACT_REVIEW_GROUND}",
    ),
    (
        "Лицензию на оружие правоохранители обязаны аннулировать, и затем изъять "
        f"при задержании {_CLOSED_UK_REVIEW_GROUND}",
        "Лицензию на оружие правоохранители обязаны аннулировать, и затем изъять "
        f"при задержании {_CLOSED_UK_COMPACT_REVIEW_GROUND}",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful"),
    _CLOSED_UK_LOCAL_DETENTION_CASES,
    ids=("contrast", "sequence", "and-sequence"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_keeps_trailing_detention_with_nearest_action(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(
            source,
            "При задержании правоохранители обязаны аннулировать и изъять "
            f"лицензию на оружие {_CLOSED_UK_COMPACT_REVIEW_GROUND}",
        )


_CLOSED_UK_PER_ACTION_DETENTION_SOURCE = (
    "Лицензия на оружие должна быть аннулирована при задержании ответственными "
    "сотрудниками полиции и изъята при задержании дежурными сотрудниками полиции "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_PER_ACTION_DETENTION_ALIAS = (
    _CLOSED_UK_PER_ACTION_DETENTION_SOURCE.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_keeps_each_detention_performer_with_its_action(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_PER_ACTION_DETENTION_SOURCE,
        _CLOSED_UK_PER_ACTION_DETENTION_ALIAS,
    )
    for unsafe in (
        _CLOSED_UK_PER_ACTION_DETENTION_ALIAS.replace(
            "ответственными", "дежурными", 1
        ),
        _CLOSED_UK_PER_ACTION_DETENTION_ALIAS.replace(
            "дежурными", "ответственными", 1
        ),
    ):
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_PER_ACTION_DETENTION_SOURCE, unsafe)


_CLOSED_UK_QUOTED_DETENTION_SOURCE = (
    "Лицензия на оружие должна быть аннулирована и изъята с пометкой "
    "«при задержании сотрудниками полиции» "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_QUOTED_DETENTION_ALIAS = _CLOSED_UK_QUOTED_DETENTION_SOURCE.replace(
    _CLOSED_UK_REVIEW_GROUND,
    _CLOSED_UK_COMPACT_REVIEW_GROUND,
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_quoted_detention_text_is_opaque_identity(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_QUOTED_DETENTION_SOURCE,
        _CLOSED_UK_QUOTED_DETENTION_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_QUOTED_DETENTION_SOURCE,
            _CLOSED_UK_QUOTED_DETENTION_ALIAS.replace(
                "при задержании", "после задержания", 1
            ),
        )


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_unresolved_actor_group_never_proves_formula(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source(
        "правоохранители совместно с, прокурор"
    )
    unsafe = _closed_uk_object_first_actor_alias("судья")
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_dangling_plain_actor_connector_never_proves_formula(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source("правоохранители и")
    unsafe = _closed_uk_object_first_actor_alias("правоохранители")
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_REORDERED_PER_ACTION_DETENTION_ALIAS = (
    "Лицензия на оружие должна быть изъята при задержании дежурными "
    "сотрудниками полиции и аннулирована при задержании ответственными сотрудниками "
    "полиции "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_may_reorder_complete_action_detention_pairs(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_PER_ACTION_DETENTION_SOURCE,
        _CLOSED_UK_REORDERED_PER_ACTION_DETENTION_ALIAS,
    )


@pytest.mark.parametrize(
    "owner",
    (
        "компании «Альфа»",
        "гражданина Иванова",
    ),
    ids=("company", "citizen"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_object_first_exact_owner_is_grammar_not_lexeme_allowlist(
    owner: str,
    validate: RelationValidator,
) -> None:
    source_actor = "дежурные сотрудники полиции и прокурор"
    output_actor = "дежурные правоохранители и прокурор"
    source = _closed_uk_object_first_actor_source(f"{owner} {source_actor}")
    faithful = _closed_uk_object_first_actor_alias(f"{owner} {output_actor}")
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, faithful.replace(owner, "", 1))


_CLOSED_UK_OBJECT_FIRST_ACTIVE_FACET_SOURCE = (
    _closed_uk_object_first_actor_source(
        "дежурные сотрудники полиции и прокурор"
    ).replace("Лицензию", "Действующую лицензию", 1)
)
_CLOSED_UK_OBJECT_FIRST_ACTIVE_FACET_ALIAS = (
    _closed_uk_object_first_actor_alias(
        "дежурные правоохранители и прокурор"
    ).replace("Лицензию", "Действующую лицензию", 1)
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_object_first_patient_facet_survives_title_path(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_OBJECT_FIRST_ACTIVE_FACET_SOURCE,
        _CLOSED_UK_OBJECT_FIRST_ACTIVE_FACET_ALIAS,
    )
    for unsafe in (
        _CLOSED_UK_OBJECT_FIRST_ACTIVE_FACET_ALIAS.replace(
            "Действующую ", "", 1
        ),
        _CLOSED_UK_OBJECT_FIRST_ACTIVE_FACET_ALIAS.replace(
            "Действующую", "Недействующую", 1
        ),
    ):
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_OBJECT_FIRST_ACTIVE_FACET_SOURCE, unsafe)


_CLOSED_UK_DETENTION_BEFORE_SECOND_SOURCE = (
    "Лицензию на оружие правоохранители обязаны аннулировать и при задержании "
    f"изъять {_CLOSED_UK_REVIEW_GROUND}"
)
_CLOSED_UK_DETENTION_BEFORE_SECOND_ALIAS = (
    "Лицензию на оружие правоохранители обязаны аннулировать и при задержании "
    f"изъять {_CLOSED_UK_COMPACT_REVIEW_GROUND}"
)
_CLOSED_UK_DETENTION_AFTER_FIRST_ALIAS = (
    "Лицензию на оружие правоохранители обязаны аннулировать при задержании и "
    f"изъять {_CLOSED_UK_COMPACT_REVIEW_GROUND}"
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_inter_action_detention_side_is_semantic(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_DETENTION_BEFORE_SECOND_SOURCE,
        _CLOSED_UK_DETENTION_BEFORE_SECOND_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_DETENTION_BEFORE_SECOND_SOURCE,
            _CLOSED_UK_DETENTION_AFTER_FIRST_ALIAS,
        )
    reverse_source = _CLOSED_UK_DETENTION_AFTER_FIRST_ALIAS.replace(
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
        _CLOSED_UK_REVIEW_GROUND,
    )
    validate(reverse_source, _CLOSED_UK_DETENTION_AFTER_FIRST_ALIAS)
    with pytest.raises(ValueError):
        validate(reverse_source, _CLOSED_UK_DETENTION_BEFORE_SECOND_ALIAS)


_CLOSED_UK_QUOTED_SEMICOLON_SOURCE = (
    _CLOSED_UK_QUOTED_DETENTION_SOURCE.replace(
        "сотрудниками полиции»",
        "сотрудниками полиции; срочно»",
        1,
    )
)
_CLOSED_UK_QUOTED_SEMICOLON_ALIAS = (
    _CLOSED_UK_QUOTED_DETENTION_ALIAS.replace(
        "сотрудниками полиции»",
        "сотрудниками полиции; срочно»",
        1,
    )
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_hard_delimiter_inside_quote_is_not_clause_boundary(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_QUOTED_SEMICOLON_SOURCE,
        _CLOSED_UK_QUOTED_SEMICOLON_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_QUOTED_SEMICOLON_SOURCE,
            _CLOSED_UK_QUOTED_SEMICOLON_ALIAS.replace("срочно", "обычно", 1),
        )


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_police_family_alias_preserves_person_identity(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source(
        "полицейский Иванов и прокурор"
    )
    faithful = _closed_uk_object_first_actor_alias(
        "правоохранитель Иванов и прокурор"
    )
    validate(source, faithful)
    for unsafe in (
        faithful.replace("Иванов", "Петров", 1),
        faithful.replace("Иванов", "Иван", 1),
        faithful.replace(" Иванов", "", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)

    bare_source = _closed_uk_object_first_actor_source(
        "полицейский и прокурор"
    )
    with pytest.raises(ValueError):
        validate(bare_source, faithful)


@pytest.mark.parametrize(
    ("source_identity", "faithful_identity", "unsafe_identity"),
    (
        (
            "полицейский Иванов",
            "правоохранитель Иванов",
            "правоохранитель Иван",
        ),
        (
            "полицейский Петров",
            "правоохранитель Петров",
            "правоохранитель Петр",
        ),
        (
            "шериф Иванов",
            "правоохранитель Иванов",
            "правоохранитель Иван",
        ),
    ),
    ids=("ivanov-vs-ivan", "petrov-vs-petr", "sheriff-family-identity"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_productive_surname_is_not_common_name_stem(
    source_identity: str,
    faithful_identity: str,
    unsafe_identity: str,
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source(
        f"{source_identity} и прокурор"
    )
    faithful = _closed_uk_object_first_actor_alias(
        faithful_identity + " и прокурор"
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(
            source,
            _closed_uk_object_first_actor_alias(
                f"{unsafe_identity} и прокурор"
            ),
        )


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_police_alias_keeps_long_bounded_role_qualifiers(
    validate: RelationValidator,
) -> None:
    source_actor = (
        "уполномоченный дежурный ответственный полицейский Иванов и прокурор"
    )
    faithful_actor = (
        "уполномоченный дежурный ответственный правоохранитель Иванов и "
        "прокурор"
    )
    source = _closed_uk_object_first_actor_source(source_actor)
    faithful = _closed_uk_object_first_actor_alias(faithful_actor)
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, faithful.replace("дежурный ", "", 1))


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_police_alias_keeps_balanced_parenthetical_identity(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source(
        "полицейский (Иванов) и прокурор"
    )
    faithful = _closed_uk_object_first_actor_alias(
        "правоохранитель (Иванов) и прокурор"
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, faithful.replace("Иванов", "Иван", 1))


@pytest.mark.parametrize(
    "owner",
    (
        "полицейского Иванова",
        "шерифа Иванова",
        "адвоката Иванова",
    ),
    ids=("police", "sheriff", "open-vocabulary-lawyer"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_object_first_owner_keeps_exact_identity(
    owner: str,
    validate: RelationValidator,
) -> None:
    source_actor = "дежурные сотрудники полиции и прокурор"
    output_actor = "дежурные правоохранители и прокурор"
    source = _closed_uk_object_first_actor_source(f"{owner} {source_actor}")
    faithful = _closed_uk_object_first_actor_alias(f"{owner} {output_actor}")
    validate(source, faithful)
    for unsafe in (
        faithful.replace("Иванова", "Петрова", 1),
        faithful.replace(" Иванова", "", 1),
        faithful.replace(f"{owner} ", "", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)

    bare_owner = owner.replace(" Иванова", "")
    bare_source = _closed_uk_object_first_actor_source(
        f"{bare_owner} {source_actor}"
    )
    with pytest.raises(ValueError):
        validate(bare_source, faithful)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_object_first_owner_allows_weapon_license_word_order(
    validate: RelationValidator,
) -> None:
    source = (
        "Оружейную лицензию организации «Альфа» дежурные сотрудники полиции "
        "и прокурор обязаны аннулировать и изъять при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = (
        "Лицензию на оружие организации «Альфа» дежурные правоохранители и "
        "прокурор обязаны аннулировать и изъять при задержании "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND
    )
    validate(source, faithful)
    for unsafe in (
        faithful.replace("Альфа", "Бета", 1),
        faithful.replace(" организации «Альфа»", "", 1),
        faithful.replace("«Альфа»", "«Альфа Бета»", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
@pytest.mark.parametrize(
    "owner",
    ("организации Филиал", "адвоката Филиал"),
    ids=("known-owner-family", "open-owner-family"),
)
def test_v3_closed_uk_patient_owner_name_is_not_a_finite_predicate(
    owner: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Лицензию на оружие {owner} дежурные полицейские и "
        "прокурор обязаны аннулировать и изъять при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = (
        f"Лицензию на оружие {owner} дежурные правоохранители и "
        "прокурор обязаны аннулировать и изъять при задержании "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, faithful.replace(" Филиал", "", 1))


_CLOSED_UK_DETENTION_GROUP_SOURCE = (
    "Лицензия на оружие должна быть аннулирована и изъята при задержании "
    "дежурными сотрудниками полиции и прокурорами "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_DETENTION_GROUP_ALIAS = (
    "Лицензия на оружие должна быть аннулирована и изъята при задержании "
    "прокурорами и дежурными правоохранителями "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_detention_performer_group_is_complete_and_commutative(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_DETENTION_GROUP_SOURCE, _CLOSED_UK_DETENTION_GROUP_ALIAS)
    for unsafe in (
        _CLOSED_UK_DETENTION_GROUP_ALIAS.replace("прокурорами и ", "", 1),
        _CLOSED_UK_DETENTION_GROUP_ALIAS.replace(
            " и дежурными правоохранителями", "", 1
        ),
        _CLOSED_UK_DETENTION_GROUP_ALIAS.replace(
            "прокурорами и дежурными", "прокурорами или дежурными", 1
        ),
        _CLOSED_UK_DETENTION_GROUP_ALIAS.replace(
            "прокурорами и", "прокурорами и судом и", 1
        ),
        _CLOSED_UK_DETENTION_GROUP_ALIAS.replace("дежурными ", "", 1),
        _CLOSED_UK_DETENTION_GROUP_ALIAS.replace(
            "дежурными правоохранителями",
            "дежурными правоохранителями «Север»",
            1,
        ),
    ):
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_DETENTION_GROUP_SOURCE, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_detention_performer_keeps_exact_identity(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия на оружие должна быть аннулирована и изъята при задержании "
        "дежурными сотрудниками полиции «Север» и прокурорами "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = (
        "Лицензия на оружие должна быть аннулирована и изъята при задержании "
        "прокурорами и дежурными правоохранителями «Север» "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND
    )
    validate(source, faithful)
    for unsafe in (
        faithful.replace("Север", "Юг", 1),
        faithful.replace(" «Север»", "", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_nested_plain_and_actor_group_may_reorder_whole_group(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source(
        "(дежурные сотрудники полиции и прокурор) и судья"
    )
    faithful = _closed_uk_object_first_actor_alias(
        "судья и (дежурные правоохранители и прокурор)"
    )
    validate(source, faithful)
    for unsafe in (
        faithful.replace(" и прокурор", "", 1),
        faithful.replace("прокурор", "губернатор", 1),
        faithful.replace("судья и (", "судья или (", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_previous_finite_clause_is_not_premodal_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд уведомил прокурора и прокуратура обязана аннулировать и изъять "
        "лицензию на оружие при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = source.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
    validate(source, faithful)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_irregular_previous_verb_keeps_clause_boundary(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд вынес приговор прокурору и прокуратура обязана аннулировать и "
        "изъять лицензию на оружие при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = source.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
    connector = source.index(" и прокуратура")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, faithful)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_actor_name_ending_in_al_is_not_a_finite_predicate(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source(
        "организация Филиал и прокуратура"
    )
    faithful = _closed_uk_object_first_actor_alias(
        "организация Филиал и прокуратура"
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(
            source,
            faithful.replace("организация Филиал и ", "", 1),
        )


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_actor_connector_inside_quote_is_opaque(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_object_first_actor_source(
        "суд округа «Блейн, а Север» и прокуратура"
    )
    faithful = _closed_uk_object_first_actor_alias(
        "суд округа «Блейн, а Север» и прокуратура"
    )
    validate(source, faithful)
    for unsafe in (
        faithful.replace("Блейн", "Либерти", 1),
        faithful.replace(" и прокуратура", "", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_v3_closed_uk_real_parenthetical_clause_connector_is_not_opaque() -> None:
    value = (
        "Примечание (суд уведомил прокурора, а прокуратура обязана "
        "аннулировать лицензию)."
    )
    connector = value.index(", а")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(value)
    )

    quoted = value.replace(
        "(суд уведомил прокурора, а прокуратура обязана аннулировать лицензию)",
        "«Суд уведомлений, а прокуратура Север»",
    )
    quoted_connector = quoted.index(", а")
    assert not any(
        start <= quoted_connector < end
        for start, end in _relation_separator_spans(quoted)
    )

    parenthetical_identity = (
        "Лицензию суд (Филиал, а Север) и прокуратура обязаны аннулировать"
    )
    identity_connector = parenthetical_identity.index(", а")
    assert not any(
        start <= identity_connector < end
        for start, end in _relation_separator_spans(parenthetical_identity)
    )


@pytest.mark.parametrize(
    "connector",
    ("да", "/", "+", "плюс"),
    ids=("da", "slash", "plus-symbol", "plus-word"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_unsupported_action_connector_never_proves_formula(
    connector: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензию на оружие правоохранители обязаны аннулировать "
        f"{connector} при задержании изъять {_CLOSED_UK_REVIEW_GROUND}"
    )
    unsafe = source.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)
    moved_detention = (
        "Лицензию на оружие правоохранители обязаны аннулировать при "
        f"задержании {connector} изъять {_CLOSED_UK_COMPACT_REVIEW_GROUND}"
    )
    with pytest.raises(ValueError):
        validate(source, moved_detention)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_leading_detention_performer_scopes_whole_and_cluster(
    validate: RelationValidator,
) -> None:
    source = (
        "При задержании сотрудниками полиции лицензия на оружие должна быть "
        "аннулирована и изъята "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = (
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "при задержании "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND
    )
    validate(source, faithful)
    for unsafe in (
        (
            "Правоохранители обязаны аннулировать и при задержании изъять "
            "лицензию на оружие "
            + _CLOSED_UK_COMPACT_REVIEW_GROUND
        ),
        faithful.replace("Правоохранители", "Прокуроры", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_QUOTED_FACET_CASES = (
    "с пометкой «Срочно»",
    "с пометкой «код; срочно»",
)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_pre_action_quote_does_not_lose_auxiliary_word(
    validate: RelationValidator,
) -> None:
    source = (
        "С пометкой «код быть срочно» лицензия на оружие должна быть "
        "аннулирована и изъята при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = source.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, faithful.replace("код быть срочно", "код срочно", 1))


@pytest.mark.parametrize(
    "facet",
    _CLOSED_UK_QUOTED_FACET_CASES,
    ids=("simple", "internal-semicolon"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_post_action_quoted_facet_is_exact(
    facet: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия на оружие должна быть аннулирована и изъята "
        f"{facet} {_CLOSED_UK_REVIEW_GROUND}"
    )
    faithful = source.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
    validate(source, faithful)
    changed_payload = (
        faithful.replace("Срочно", "Обычно", 1)
        if "Срочно" in faithful
        else faithful.replace("срочно", "обычно", 1)
    )
    unsafe_values = [
        faithful.replace(f" {facet}", "", 1),
        changed_payload,
        faithful.replace("«", "", 1).replace("»", "", 1),
    ]
    if ";" in facet:
        unsafe_values.extend((
            faithful.replace(";", "", 1),
            faithful.replace("код; срочно", "срочно; код", 1),
        ))
    for unsafe in unsafe_values:
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_real_passive_actor_remains_supported(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия на оружие должна быть аннулирована прокурором и изъята "
        "при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = source.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
    validate(source, faithful)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_plain_with_human_is_exact_prepositional_residual(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия на оружие должна быть аннулирована с прокурором «Иванов» "
        "и изъята при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = source.replace(
        _CLOSED_UK_REVIEW_GROUND,
        _CLOSED_UK_COMPACT_REVIEW_GROUND,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, faithful.replace("Иванов", "Петров", 1))


@pytest.mark.parametrize(
    ("qualifier", "changed"),
    (
        ("Временную", "Постоянную"),
        ("Постоянную", "Временную"),
        ("Региональную", "Федеральную"),
        ("Федеральную", "Региональную"),
    ),
    ids=("temporary", "permanent", "regional", "federal"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set", "penalty-summary", "penalty-other"),
)
def test_v3_closed_uk_typed_patient_qualifier_is_owned_by_exact_formula(
    qualifier: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"{qualifier} лицензию на оружие дежурные сотрудники полиции и "
        "прокурор обязаны аннулировать и изъять при задержании "
        + _CLOSED_UK_REVIEW_GROUND
    )
    faithful = (
        f"{qualifier} лицензию на оружие дежурные правоохранители и прокурор "
        "обязаны аннулировать и изъять при задержании "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND
    )
    validate(source, faithful)
    for unsafe in (
        faithful.replace(f"{qualifier} ", "", 1),
        faithful.replace(qualifier, changed, 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_SAME_FAMILY_SEQUENCE_SOURCE = (
    "Сначала суд округа Блейн обязан аннулировать лицензию на оружие, затем "
    "суд округа Лос-Сантос обязан аннулировать лицензию на оружие "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_SAME_FAMILY_SEQUENCE_ALIAS = (
    "Сначала суд округа Блейн обязан аннулировать лицензию на оружие, затем "
    "суд округа Лос-Сантос обязан аннулировать лицензию на оружие "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_SAME_FAMILY_SEQUENCE_ACTOR_SWAP = (
    "Сначала суд округа Лос-Сантос обязан аннулировать лицензию на оружие, "
    "затем суд округа Блейн обязан аннулировать лицензию на оружие "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_sequence_keeps_each_same_family_actor_with_its_action(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SAME_FAMILY_SEQUENCE_SOURCE,
        _CLOSED_UK_SAME_FAMILY_SEQUENCE_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SAME_FAMILY_SEQUENCE_SOURCE,
            _CLOSED_UK_SAME_FAMILY_SEQUENCE_ACTOR_SWAP,
        )


_CLOSED_UK_BOUND_MIXED_ACTION_TREE_SOURCE = (
    "(Суд округа Блейн обязан аннулировать лицензию на оружие и прокуроры "
    "обязаны изъять лицензию на оружие) или суд округа Лос-Сантос обязан "
    "аннулировать лицензию на оружие только в случае нарушения любой из "
    "статей: 6.1; 6.2 УК."
)
_CLOSED_UK_BOUND_MIXED_ACTION_TREE_ALIAS = (
    "(Суд округа Блейн обязан аннулировать лицензию на оружие и прокуроры "
    "обязаны изъять лицензию на оружие) или суд округа Лос-Сантос обязан "
    "аннулировать лицензию на оружие "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_BOUND_MIXED_ACTION_TREE_REORDERED = (
    "Суд округа Лос-Сантос обязан аннулировать лицензию на оружие или "
    "(прокуроры обязаны изъять лицензию на оружие и суд округа Блейн обязан "
    "аннулировать лицензию на оружие) "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_BOUND_MIXED_ACTION_TREE_ACTOR_SWAP = (
    "(Суд округа Лос-Сантос обязан аннулировать лицензию на оружие и "
    "прокуроры обязаны изъять лицензию на оружие) или суд округа Блейн "
    "обязан аннулировать лицензию на оружие "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_bound_mixed_action_tree_reorders_only_whole_branches(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_BOUND_MIXED_ACTION_TREE_SOURCE,
        _CLOSED_UK_BOUND_MIXED_ACTION_TREE_ALIAS,
    )
    validate(
        _CLOSED_UK_BOUND_MIXED_ACTION_TREE_SOURCE,
        _CLOSED_UK_BOUND_MIXED_ACTION_TREE_REORDERED,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_BOUND_MIXED_ACTION_TREE_SOURCE,
            _CLOSED_UK_BOUND_MIXED_ACTION_TREE_ACTOR_SWAP,
        )


_CLOSED_UK_NESTED_CONDITION_SOURCE = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "условии ((есть решение суда или согласие прокурора) и уведомление "
    "владельца) "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_NESTED_CONDITION_ALIAS = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "условии ((есть решение суда или согласие прокурора) и уведомление "
    "владельца) "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_NESTED_CONDITION_EQUIVALENT = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "условии (уведомление владельца и (согласие прокурора или есть решение "
    "суда)) "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_NESTED_CONDITION_REGROUPED = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "условии (есть решение суда или (согласие прокурора и уведомление "
    "владельца)) "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_nested_condition_preserves_boolean_grouping(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_NESTED_CONDITION_SOURCE,
        _CLOSED_UK_NESTED_CONDITION_ALIAS,
    )
    validate(
        _CLOSED_UK_NESTED_CONDITION_SOURCE,
        _CLOSED_UK_NESTED_CONDITION_EQUIVALENT,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_NESTED_CONDITION_SOURCE,
            _CLOSED_UK_NESTED_CONDITION_REGROUPED,
        )


_CLOSED_UK_UNQUOTED_BE_SOURCE = (
    "С пометкой Быть Добру лицензия на оружие должна быть аннулирована и "
    "изъята при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_UNQUOTED_BE_ALIAS = (
    "С пометкой Быть Добру лицензия на оружие должна быть аннулирована и "
    "изъята при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_PLAIN_MODAL_AUXILIARY_SOURCE = (
    "Лицензия на оружие должна быть аннулирована и изъята при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_PLAIN_MODAL_AUXILIARY_ALIAS = (
    "Лицензия на оружие должна быть аннулирована и изъята при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_masks_only_the_owned_modal_auxiliary(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_UNQUOTED_BE_SOURCE, _CLOSED_UK_UNQUOTED_BE_ALIAS)
    validate(
        _CLOSED_UK_PLAIN_MODAL_AUXILIARY_SOURCE,
        _CLOSED_UK_PLAIN_MODAL_AUXILIARY_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_UNQUOTED_BE_SOURCE,
            _CLOSED_UK_UNQUOTED_BE_ALIAS.replace("Быть Добру", "Добру", 1),
        )


def _closed_uk_prefixed_active_source(prefix: str) -> str:
    return (
        f"{prefix} аннулировать и изъять лицензию на оружие при задержании "
        f"{_CLOSED_UK_REVIEW_GROUND}"
    )


def _closed_uk_prefixed_active_alias(prefix: str) -> str:
    return (
        f"{prefix} аннулировать и изъять лицензию на оружие при задержании "
        f"{_CLOSED_UK_COMPACT_REVIEW_GROUND}"
    )


_CLOSED_UK_REQUIRED_SEPARATOR_CASES = (
    (
        _closed_uk_prefixed_active_source(
            "Суд вынес приговор организации и прокуратура обязана"
        ),
        _closed_uk_prefixed_active_alias(
            "Суд вынес приговор организации и прокуратура обязана"
        ),
        " и прокуратура обязана",
        (),
    ),
    (
        _closed_uk_prefixed_active_source(
            "Суд вынес приговор суду и прокуратура обязана"
        ),
        _closed_uk_prefixed_active_alias(
            "Суд вынес приговор суду и прокуратура обязана"
        ),
        " и прокуратура обязана",
        (),
    ),
    (
        _closed_uk_prefixed_active_source(
            "Суд вынес приговор (Суду и Прокуратуре) и губернатор обязан"
        ),
        _closed_uk_prefixed_active_alias(
            "Суд вынес приговор (Суду и Прокуратуре) и губернатор обязан"
        ),
        " и губернатор обязан",
        (" и Прокуратуре",),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "separator_needle", "opaque_needles"),
    _CLOSED_UK_REQUIRED_SEPARATOR_CASES,
    ids=("syncretic-organization", "oblique-court", "nested-oblique-group"),
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
def test_v3_closed_uk_prior_finite_clause_requires_a_real_separator(
    source: str,
    faithful: str,
    separator_needle: str,
    opaque_needles: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    spans = _relation_separator_spans(source)
    separator = source.index(separator_needle)
    assert any(start <= separator < end for start, end in spans)
    for needle in opaque_needles:
        opaque = source.index(needle)
        assert not any(start <= opaque < end for start, end in spans)
    validate(source, faithful)


_CLOSED_UK_COMPLETE_PREMODAL_ACTOR_CASES = (
    (
        "Суды и прокуратуры обязаны",
        (" и прокуратуры",),
    ),
    (
        "Лицензирующие органы и парламенты обязаны",
        (" и парламенты",),
    ),
    (
        "(Суд и Прокуратура) и губернатор обязаны",
        (" и Прокуратура", " и губернатор"),
    ),
    (
        "Суд (Филиал, а Север) и прокуратура обязаны",
        (", а Север", " и прокуратура"),
    ),
    (
        "После проверки суд и прокуратура обязаны",
        (" и прокуратура",),
    ),
    (
        "FIB и LSPD обязаны",
        (" и LSPD",),
    ),
)


@pytest.mark.parametrize(
    ("actor_prefix", "actor_connector_needles"),
    _CLOSED_UK_COMPLETE_PREMODAL_ACTOR_CASES,
    ids=(
        "plural-courts",
        "plural-institutions",
        "nested-nominative",
        "parenthetical-identity-tail",
        "leading-adjunct",
        "ascii-identities",
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
def test_v3_closed_uk_complete_premodal_actor_connectors_are_not_separators(
    actor_prefix: str,
    actor_connector_needles: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    source = _closed_uk_prefixed_active_source(actor_prefix)
    faithful = _closed_uk_prefixed_active_alias(actor_prefix)
    spans = _relation_separator_spans(source)
    for needle in actor_connector_needles:
        connector = source.index(needle)
        assert not any(start <= connector < end for start, end in spans)
    validate(source, faithful)


_CLOSED_UK_GROUPED_COURT_ACTION = (
    "суд округа Блейн обязан аннулировать лицензию на оружие"
)
_CLOSED_UK_GROUPED_POLICE_ACTION = (
    "полиция должна изъять лицензию на оружие"
)


def _closed_uk_balanced_action_group(
    opener: str,
    closer: str,
    connector: str,
    *,
    reordered: bool,
    compact_ground: bool,
) -> str:
    first, second = (
        (_CLOSED_UK_GROUPED_POLICE_ACTION, _CLOSED_UK_GROUPED_COURT_ACTION)
        if reordered
        else (_CLOSED_UK_GROUPED_COURT_ACTION, _CLOSED_UK_GROUPED_POLICE_ACTION)
    )
    ground = (
        _CLOSED_UK_COMPACT_REVIEW_GROUND
        if compact_ground
        else _CLOSED_UK_REVIEW_GROUND
    )
    return f"{opener}{first} {connector} {second}{closer} {ground}"


_CLOSED_UK_BALANCED_ACTION_GROUP_CASES = tuple(
    (
        _closed_uk_balanced_action_group(
            opener,
            closer,
            connector,
            reordered=False,
            compact_ground=False,
        ),
        _closed_uk_balanced_action_group(
            opener,
            closer,
            connector,
            reordered=True,
            compact_ground=True,
        ),
        (
            _closed_uk_balanced_action_group(
                opener,
                closer,
                connector,
                reordered=True,
                compact_ground=True,
            ).replace(closer, "", 1),
            _closed_uk_balanced_action_group(
                opener,
                closer,
                connector,
                reordered=True,
                compact_ground=True,
            ).replace(opener, "", 1),
            _closed_uk_balanced_action_group(
                opener,
                closer,
                connector,
                reordered=True,
                compact_ground=True,
            ).replace(closer, mismatched_closer, 1),
        ),
    )
    for opener, closer, mismatched_closer in (
        ("(", ")", "]"),
        ("[", "]", "}"),
        ("{", "}", ")"),
    )
    for connector in ("и", "или")
)


@pytest.mark.parametrize(
    ("source", "faithful", "malformed_outputs"),
    _CLOSED_UK_BALANCED_ACTION_GROUP_CASES,
    ids=(
        "parentheses-and",
        "parentheses-or",
        "brackets-and",
        "brackets-or",
        "braces-and",
        "braces-or",
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
def test_v3_closed_uk_balanced_action_groups_are_commutative_and_fail_closed(
    source: str,
    faithful: str,
    malformed_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in malformed_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


def _closed_uk_owned_identity_source(identity: str) -> str:
    return (
        f"Лицензию на оружие организации «{identity}» суд обязан "
        f"аннулировать и изъять при задержании {_CLOSED_UK_REVIEW_GROUND}"
    )


def _closed_uk_owned_identity_alias(identity: str) -> str:
    return (
        f"Лицензию на оружие организации «{identity}» суд обязан "
        "аннулировать и изъять при задержании "
        f"{_CLOSED_UK_COMPACT_REVIEW_GROUND}"
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
def test_v3_closed_uk_quoted_owner_action_words_are_opaque_identity(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_owned_identity_source("не изымать")
    faithful = _closed_uk_owned_identity_alias("не изымать")
    validate(source, faithful)
    for unsafe in (
        faithful.replace("не изымать", "не отзывать", 1),
        faithful.replace(" организации «не изымать»", "", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_OWNED_SEQUENCE_CONNECTOR_CASES = (
    (
        "Суд округа Блейн обязан аннулировать лицензию на оружие, и затем "
        "полиция должна изъять лицензию на оружие "
        + _CLOSED_UK_REVIEW_GROUND,
        "Суд округа Блейн обязан аннулировать лицензию на оружие, а затем "
        "полиция должна изъять лицензию на оружие "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        "Полиция должна изъять лицензию на оружие, а затем суд округа Блейн "
        "обязан аннулировать лицензию на оружие "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
    ),
    (
        "Суд округа Блейн обязан аннулировать лицензию на оружие, а затем "
        "полиция должна изъять лицензию на оружие "
        + _CLOSED_UK_REVIEW_GROUND,
        "Суд округа Блейн обязан аннулировать лицензию на оружие, и затем "
        "полиция должна изъять лицензию на оружие "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        "Полиция должна изъять лицензию на оружие, и затем суд округа Блейн "
        "обязан аннулировать лицензию на оружие "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "reversed_actions"),
    _CLOSED_UK_OWNED_SEQUENCE_CONNECTOR_CASES,
    ids=("and-then-to-but-then", "but-then-to-and-then"),
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
def test_v3_closed_uk_sequence_connector_variants_keep_action_order(
    source: str,
    faithful: str,
    reversed_actions: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, reversed_actions)


@pytest.mark.parametrize(
    ("identity", "changed_identity"),
    (("Затем", "После"), ("И", "А")),
    ids=("then", "and"),
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
def test_v3_closed_uk_quoted_connector_word_is_exact_identity_not_action_logic(
    identity: str,
    changed_identity: str,
    validate: RelationValidator,
) -> None:
    source = _closed_uk_owned_identity_source(identity)
    faithful = _closed_uk_owned_identity_alias(identity)
    validate(source, faithful)
    for unsafe in (
        faithful.replace(identity, changed_identity, 1),
        faithful.replace(f" «{identity}»", "", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_POST_MODAL_SEMANTIC_BE_SOURCE = (
    "Лицензия на оружие должна под пометкой Добру Быть аннулирована и "
    "изъята при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_POST_MODAL_SEMANTIC_BE_ALIAS = (
    "Лицензия на оружие должна под пометкой Добру Быть аннулирована и "
    "изъята при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_SHARED_SECOND_AUXILIARY_SOURCE = (
    "Лицензия на оружие должна быть аннулирована и быть изъята при "
    "задержании сотрудниками правоохранительных органов "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_SHARED_SECOND_AUXILIARY_ALIAS = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_INTERPOSED_ADVERB_AUXILIARY_SOURCE = (
    "Лицензия на оружие должна немедленно быть аннулирована и изъята при "
    "задержании сотрудниками правоохранительных органов "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_INTERPOSED_ADVERB_AUXILIARY_ALIAS = (
    "Правоохранители обязаны немедленно аннулировать и изъять лицензию на "
    "оружие при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_post_modal_semantic_be_is_not_an_auxiliary(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_POST_MODAL_SEMANTIC_BE_SOURCE,
        _CLOSED_UK_POST_MODAL_SEMANTIC_BE_ALIAS,
    )
    validate(
        _CLOSED_UK_PLAIN_MODAL_AUXILIARY_SOURCE,
        _CLOSED_UK_PLAIN_MODAL_AUXILIARY_ALIAS,
    )
    validate(
        _CLOSED_UK_SHARED_SECOND_AUXILIARY_SOURCE,
        _CLOSED_UK_SHARED_SECOND_AUXILIARY_ALIAS,
    )
    validate(
        _CLOSED_UK_INTERPOSED_ADVERB_AUXILIARY_SOURCE,
        _CLOSED_UK_INTERPOSED_ADVERB_AUXILIARY_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_POST_MODAL_SEMANTIC_BE_SOURCE,
            _CLOSED_UK_POST_MODAL_SEMANTIC_BE_ALIAS.replace(" Быть", "", 1),
        )


_CLOSED_UK_PRIOR_FINITE_OWNER_LAUNDERING_SOURCE = (
    "Лицензию на оружие приняла прокуратура, и суд обязан аннулировать и "
    "изъять лицензию на оружие при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_PRIOR_FINITE_OWNER_LAUNDERING_OUTPUT = (
    "Суд обязан аннулировать и изъять лицензию на оружие при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_GENITIVE_OWNER_POSITIVE_SOURCE = (
    "Лицензию на оружие организации «Альфа» суд обязан аннулировать и "
    "изъять при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_GENITIVE_OWNER_POSITIVE_ALIAS = (
    "Лицензию на оружие организации «Альфа» суд обязан аннулировать и "
    "изъять при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_prior_finite_clause_is_not_a_genitive_owner(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_GENITIVE_OWNER_POSITIVE_SOURCE,
        _CLOSED_UK_GENITIVE_OWNER_POSITIVE_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PRIOR_FINITE_OWNER_LAUNDERING_SOURCE,
            _CLOSED_UK_PRIOR_FINITE_OWNER_LAUNDERING_OUTPUT,
        )


_CLOSED_UK_QUOTED_MODAL_ACTOR_SOURCE = _closed_uk_object_first_actor_source(
    "организация «Должна Служить» и прокуратура"
)
_CLOSED_UK_QUOTED_MODAL_ACTOR_ALIAS = _closed_uk_object_first_actor_alias(
    "организация «Должна Служить» и прокуратура"
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
def test_v3_closed_uk_quoted_actor_modal_words_are_opaque_identity(
    validate: RelationValidator,
) -> None:
    connector = _CLOSED_UK_QUOTED_MODAL_ACTOR_SOURCE.index(" и прокуратура")
    assert not any(
        start <= connector < end
        for start, end in _relation_separator_spans(
            _CLOSED_UK_QUOTED_MODAL_ACTOR_SOURCE
        )
    )
    validate(
        _CLOSED_UK_QUOTED_MODAL_ACTOR_SOURCE,
        _CLOSED_UK_QUOTED_MODAL_ACTOR_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_QUOTED_MODAL_ACTOR_SOURCE,
            _CLOSED_UK_QUOTED_MODAL_ACTOR_ALIAS.replace(
                "Должна Служить",
                "Должна Помогать",
                1,
            ),
        )


_CLOSED_UK_SINGULAR_NOMINATIVE_ACTOR_CASES = (
    ("Дорожный полицейский обязан", "road-policeman"),
    ("Дорожная полицейская обязана", "road-policewoman"),
    ("Правоохранитель обязан", "law-enforcement-officer"),
)


@pytest.mark.parametrize(
    ("actor_prefix", "_case_id"),
    _CLOSED_UK_SINGULAR_NOMINATIVE_ACTOR_CASES,
    ids=tuple(
        case_id
        for _actor, case_id in _CLOSED_UK_SINGULAR_NOMINATIVE_ACTOR_CASES
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
def test_v3_closed_uk_singular_nominative_law_enforcement_actor_is_complete(
    actor_prefix: str,
    _case_id: str,
    validate: RelationValidator,
) -> None:
    validate(
        _closed_uk_prefixed_active_source(actor_prefix),
        _closed_uk_prefixed_active_alias(actor_prefix),
    )


_CLOSED_UK_PARTIALLY_WRAPPED_CONTROLLER_SOURCE = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "условии [есть решение суда или согласие прокурора] и уведомление "
    "владельца "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_PARTIALLY_WRAPPED_CONTROLLER_EQUIVALENT = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "условии уведомление владельца и [согласие прокурора или есть решение "
    "суда] "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_PARTIALLY_WRAPPED_CONTROLLER_REGROUPED = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие при "
    "условии есть решение суда или [согласие прокурора и уведомление "
    "владельца] "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_boolean_controller_owns_its_complete_wrapped_body(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_PARTIALLY_WRAPPED_CONTROLLER_SOURCE,
        _CLOSED_UK_PARTIALLY_WRAPPED_CONTROLLER_EQUIVALENT,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_PARTIALLY_WRAPPED_CONTROLLER_SOURCE,
            _CLOSED_UK_PARTIALLY_WRAPPED_CONTROLLER_REGROUPED,
        )


_CLOSED_UK_CORRELATIVE_ACTION_CASES = (
    (
        "Либо суд обязан аннулировать лицензию на оружие, либо полиция должна "
        "изъять лицензию на оружие "
        + _CLOSED_UK_REVIEW_GROUND,
        "Либо полиция должна изъять лицензию на оружие, либо суд обязан "
        "аннулировать лицензию на оружие "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        (
            "Суд обязан аннулировать лицензию на оружие, либо полиция должна "
            "изъять лицензию на оружие "
            + _CLOSED_UK_COMPACT_REVIEW_GROUND,
            "Либо суд обязан аннулировать лицензию на оружие, и полиция "
            "должна изъять лицензию на оружие "
            + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        ),
    ),
    (
        "Или суд обязан аннулировать лицензию на оружие, или полиция должна "
        "изъять лицензию на оружие "
        + _CLOSED_UK_REVIEW_GROUND,
        "Или полиция должна изъять лицензию на оружие, или суд обязан "
        "аннулировать лицензию на оружие "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        (
            "Суд обязан аннулировать лицензию на оружие, или полиция должна "
            "изъять лицензию на оружие "
            + _CLOSED_UK_COMPACT_REVIEW_GROUND,
            "Или суд обязан аннулировать лицензию на оружие, и полиция "
            "должна изъять лицензию на оружие "
            + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        ),
    ),
    (
        "Как суд обязан аннулировать лицензию на оружие, так и полиция "
        "должна изъять лицензию на оружие "
        + _CLOSED_UK_REVIEW_GROUND,
        "Как полиция должна изъять лицензию на оружие, так и суд обязан "
        "аннулировать лицензию на оружие "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        (
            "Суд обязан аннулировать лицензию на оружие, так и полиция "
            "должна изъять лицензию на оружие "
            + _CLOSED_UK_COMPACT_REVIEW_GROUND,
            "Как суд обязан аннулировать лицензию на оружие, либо полиция "
            "должна изъять лицензию на оружие "
            + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe_outputs"),
    _CLOSED_UK_CORRELATIVE_ACTION_CASES,
    ids=("either-or-libo", "either-or-ili", "both-and"),
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
def test_v3_closed_uk_correlative_action_markers_are_complete_and_owned(
    source: str,
    faithful: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_QUOTED_GROUND_LIKE_PRIOR_CLAUSE_SOURCE = (
    "Суд уведомил организацию «За нарушение», и прокуратура обязана "
    "аннулировать и изъять лицензию на оружие при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_QUOTED_GROUND_LIKE_PRIOR_CLAUSE_ALIAS = (
    "Суд уведомил организацию «За нарушение», и прокуратура обязана "
    "аннулировать и изъять лицензию на оружие при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_QUOTED_GROUND_LIKE_DROPPED_CLAUSE = (
    "Прокуратура обязана аннулировать и изъять лицензию на оружие при "
    "задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_quoted_ground_like_identity_stays_in_its_prior_clause(
    validate: RelationValidator,
) -> None:
    connector = _CLOSED_UK_QUOTED_GROUND_LIKE_PRIOR_CLAUSE_SOURCE.index(
        " и прокуратура"
    )
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(
            _CLOSED_UK_QUOTED_GROUND_LIKE_PRIOR_CLAUSE_SOURCE
        )
    )
    validate(
        _CLOSED_UK_QUOTED_GROUND_LIKE_PRIOR_CLAUSE_SOURCE,
        _CLOSED_UK_QUOTED_GROUND_LIKE_PRIOR_CLAUSE_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_QUOTED_GROUND_LIKE_PRIOR_CLAUSE_SOURCE,
            _CLOSED_UK_QUOTED_GROUND_LIKE_DROPPED_CLAUSE,
        )


_CLOSED_UK_DOTTED_QUOTED_ACTOR_SOURCE = (
    "Прокуратура «St. Mary» и суд обязаны аннулировать и изъять лицензию на "
    "оружие при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_DOTTED_QUOTED_ACTOR_ALIAS = (
    "Прокуратура «St. Mary» и суд обязаны аннулировать и изъять лицензию на "
    "оружие при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_dot_inside_quoted_actor_identity_is_not_a_boundary(
    validate: RelationValidator,
) -> None:
    connector = _CLOSED_UK_DOTTED_QUOTED_ACTOR_SOURCE.index(" и суд")
    assert not any(
        start <= connector < end
        for start, end in _relation_separator_spans(
            _CLOSED_UK_DOTTED_QUOTED_ACTOR_SOURCE
        )
    )
    validate(
        _CLOSED_UK_DOTTED_QUOTED_ACTOR_SOURCE,
        _CLOSED_UK_DOTTED_QUOTED_ACTOR_ALIAS,
    )
    for unsafe in (
        _CLOSED_UK_DOTTED_QUOTED_ACTOR_ALIAS.replace(
            "St. Mary",
            "St. Mark",
            1,
        ),
        _CLOSED_UK_DOTTED_QUOTED_ACTOR_ALIAS.replace(" «St. Mary»", "", 1),
    ):
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_DOTTED_QUOTED_ACTOR_SOURCE, unsafe)


_CLOSED_UK_TREE_ACTION_A = (
    "суд округа Блейн обязан аннулировать лицензию на оружие"
)
_CLOSED_UK_TREE_ACTION_B = (
    "полиция должна изъять лицензию на оружие"
)
_CLOSED_UK_TREE_ACTION_C = (
    "прокурор обязан аннулировать лицензию на оружие"
)
_CLOSED_UK_TREE_ACTION_D = (
    "губернатор обязан изъять лицензию на оружие"
)


def _closed_uk_correlative_tree_surface(
    expression: str,
    *,
    compact_ground: bool,
) -> str:
    ground = (
        _CLOSED_UK_COMPACT_REVIEW_GROUND
        if compact_ground
        else _CLOSED_UK_REVIEW_GROUND
    )
    return f"{expression} {ground}"


_CLOSED_UK_NESTED_EITHER_AND_SIBLING_SOURCE = (
    _closed_uk_correlative_tree_surface(
        f"(либо {_CLOSED_UK_TREE_ACTION_A}, либо {_CLOSED_UK_TREE_ACTION_B}) "
        f"и {_CLOSED_UK_TREE_ACTION_C}",
        compact_ground=False,
    )
)
_CLOSED_UK_NESTED_EITHER_AND_SIBLING_ALIAS = (
    _closed_uk_correlative_tree_surface(
        f"(либо {_CLOSED_UK_TREE_ACTION_B}, либо {_CLOSED_UK_TREE_ACTION_A}) "
        f"и {_CLOSED_UK_TREE_ACTION_C}",
        compact_ground=True,
    )
)


_CLOSED_UK_COMPOSITE_EITHER_BRANCHES_SOURCE = (
    _closed_uk_correlative_tree_surface(
        f"Либо ({_CLOSED_UK_TREE_ACTION_A} и {_CLOSED_UK_TREE_ACTION_B}), "
        f"либо ({_CLOSED_UK_TREE_ACTION_C} и {_CLOSED_UK_TREE_ACTION_D})",
        compact_ground=False,
    )
)
_CLOSED_UK_COMPOSITE_EITHER_BRANCHES_ALIAS = (
    _closed_uk_correlative_tree_surface(
        f"Либо ({_CLOSED_UK_TREE_ACTION_D} и {_CLOSED_UK_TREE_ACTION_C}), "
        f"либо ({_CLOSED_UK_TREE_ACTION_B} и {_CLOSED_UK_TREE_ACTION_A})",
        compact_ground=True,
    )
)


_CLOSED_UK_NESTED_BOTH_AND_SIBLING_SOURCE = (
    _closed_uk_correlative_tree_surface(
        f"{_CLOSED_UK_TREE_ACTION_C} и (как {_CLOSED_UK_TREE_ACTION_A}, "
        f"так и {_CLOSED_UK_TREE_ACTION_B})",
        compact_ground=False,
    )
)
_CLOSED_UK_NESTED_BOTH_AND_SIBLING_ALIAS = (
    _closed_uk_correlative_tree_surface(
        f"{_CLOSED_UK_TREE_ACTION_C} и (как {_CLOSED_UK_TREE_ACTION_B}, "
        f"так и {_CLOSED_UK_TREE_ACTION_A})",
        compact_ground=True,
    )
)


_CLOSED_UK_TWO_INDEPENDENT_CORRELATIVES_SOURCE = (
    _closed_uk_correlative_tree_surface(
        f"(либо {_CLOSED_UK_TREE_ACTION_A}, либо {_CLOSED_UK_TREE_ACTION_B}) "
        f"и (как {_CLOSED_UK_TREE_ACTION_C}, так и {_CLOSED_UK_TREE_ACTION_D})",
        compact_ground=False,
    )
)
_CLOSED_UK_TWO_INDEPENDENT_CORRELATIVES_ALIAS = (
    _closed_uk_correlative_tree_surface(
        f"(либо {_CLOSED_UK_TREE_ACTION_B}, либо {_CLOSED_UK_TREE_ACTION_A}) "
        f"и (как {_CLOSED_UK_TREE_ACTION_D}, так и {_CLOSED_UK_TREE_ACTION_C})",
        compact_ground=True,
    )
)


_CLOSED_UK_EXPLICIT_PAIRED_AND_SOURCE = _closed_uk_correlative_tree_surface(
    f"И {_CLOSED_UK_TREE_ACTION_A}, и {_CLOSED_UK_TREE_ACTION_B}",
    compact_ground=False,
)
_CLOSED_UK_EXPLICIT_PAIRED_AND_ALIAS = _closed_uk_correlative_tree_surface(
    f"И {_CLOSED_UK_TREE_ACTION_B}, и {_CLOSED_UK_TREE_ACTION_A}",
    compact_ground=True,
)


_CLOSED_UK_TREE_CORRELATIVE_CASES = (
    (
        _CLOSED_UK_NESTED_EITHER_AND_SIBLING_SOURCE,
        _CLOSED_UK_NESTED_EITHER_AND_SIBLING_ALIAS,
        (
            _closed_uk_correlative_tree_surface(
                f"либо ({_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B}) и {_CLOSED_UK_TREE_ACTION_C}",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"({_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B}) и {_CLOSED_UK_TREE_ACTION_C}",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"(либо {_CLOSED_UK_TREE_ACTION_A}, "
                f"{_CLOSED_UK_TREE_ACTION_B}) и {_CLOSED_UK_TREE_ACTION_C}",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"(либо {_CLOSED_UK_TREE_ACTION_A}, и "
                f"{_CLOSED_UK_TREE_ACTION_B}) и {_CLOSED_UK_TREE_ACTION_C}",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"(либо {_CLOSED_UK_TREE_ACTION_A}, "
                f"{_CLOSED_UK_TREE_ACTION_B}) и либо "
                f"{_CLOSED_UK_TREE_ACTION_C}",
                compact_ground=True,
            ),
        ),
    ),
    (
        _CLOSED_UK_COMPOSITE_EITHER_BRANCHES_SOURCE,
        _CLOSED_UK_COMPOSITE_EITHER_BRANCHES_ALIAS,
        (
            _closed_uk_correlative_tree_surface(
                f"(Либо {_CLOSED_UK_TREE_ACTION_A} и "
                f"{_CLOSED_UK_TREE_ACTION_B}), либо "
                f"({_CLOSED_UK_TREE_ACTION_C} и {_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"({_CLOSED_UK_TREE_ACTION_A} и {_CLOSED_UK_TREE_ACTION_B}), "
                f"либо ({_CLOSED_UK_TREE_ACTION_C} и "
                f"{_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"Либо ({_CLOSED_UK_TREE_ACTION_A} и "
                f"{_CLOSED_UK_TREE_ACTION_B}), "
                f"({_CLOSED_UK_TREE_ACTION_C} и {_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"Либо ({_CLOSED_UK_TREE_ACTION_A} и "
                f"{_CLOSED_UK_TREE_ACTION_B}), и "
                f"({_CLOSED_UK_TREE_ACTION_C} и {_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"Либо ({_CLOSED_UK_TREE_ACTION_A} и "
                f"{_CLOSED_UK_TREE_ACTION_B}), "
                f"({_CLOSED_UK_TREE_ACTION_C} и либо "
                f"{_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
        ),
    ),
    (
        _CLOSED_UK_NESTED_BOTH_AND_SIBLING_SOURCE,
        _CLOSED_UK_NESTED_BOTH_AND_SIBLING_ALIAS,
        (
            _closed_uk_correlative_tree_surface(
                f"как {_CLOSED_UK_TREE_ACTION_C} и "
                f"({_CLOSED_UK_TREE_ACTION_A}, так и "
                f"{_CLOSED_UK_TREE_ACTION_B})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"{_CLOSED_UK_TREE_ACTION_C} и "
                f"({_CLOSED_UK_TREE_ACTION_A}, так и "
                f"{_CLOSED_UK_TREE_ACTION_B})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"{_CLOSED_UK_TREE_ACTION_C} и "
                f"(как {_CLOSED_UK_TREE_ACTION_A}, "
                f"{_CLOSED_UK_TREE_ACTION_B})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"{_CLOSED_UK_TREE_ACTION_C} и "
                f"(как {_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"так и {_CLOSED_UK_TREE_ACTION_C} и "
                f"(как {_CLOSED_UK_TREE_ACTION_A}, "
                f"{_CLOSED_UK_TREE_ACTION_B})",
                compact_ground=True,
            ),
        ),
    ),
    (
        _CLOSED_UK_TWO_INDEPENDENT_CORRELATIVES_SOURCE,
        _CLOSED_UK_TWO_INDEPENDENT_CORRELATIVES_ALIAS,
        (
            _closed_uk_correlative_tree_surface(
                f"либо ({_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B}) и "
                f"(как {_CLOSED_UK_TREE_ACTION_C}, так и "
                f"{_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"({_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B}) и "
                f"(как {_CLOSED_UK_TREE_ACTION_C}, так и "
                f"{_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"(либо {_CLOSED_UK_TREE_ACTION_A}, и "
                f"{_CLOSED_UK_TREE_ACTION_B}) и "
                f"(как {_CLOSED_UK_TREE_ACTION_C}, так и "
                f"{_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"(либо {_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B}) и "
                f"({_CLOSED_UK_TREE_ACTION_C}, так и "
                f"{_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"(либо {_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B}) и "
                f"(как {_CLOSED_UK_TREE_ACTION_C}, либо "
                f"{_CLOSED_UK_TREE_ACTION_D})",
                compact_ground=True,
            ),
        ),
    ),
    (
        _CLOSED_UK_EXPLICIT_PAIRED_AND_SOURCE,
        _CLOSED_UK_EXPLICIT_PAIRED_AND_ALIAS,
        (
            _closed_uk_correlative_tree_surface(
                f"{_CLOSED_UK_TREE_ACTION_A}, и {_CLOSED_UK_TREE_ACTION_B}",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"И {_CLOSED_UK_TREE_ACTION_A}, {_CLOSED_UK_TREE_ACTION_B}",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"И {_CLOSED_UK_TREE_ACTION_A}, либо "
                f"{_CLOSED_UK_TREE_ACTION_B}",
                compact_ground=True,
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe_outputs"),
    _CLOSED_UK_TREE_CORRELATIVE_CASES,
    ids=(
        "nested-either-and-sibling",
        "composite-either-branches",
        "nested-both-and-sibling",
        "two-independent-pairs",
        "explicit-paired-and",
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
def test_v3_closed_uk_nested_correlative_tree_owns_both_markers(
    source: str,
    faithful: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in unsafe_outputs:
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_BOOLEAN_OSV_SOURCE = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие если "
    "губернатор поддерживает прокурора и имеется решение суда, "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_BOOLEAN_OSV_ALIAS = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие если "
    "губернатор поддерживает прокурора и имеется решение суда, "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_BOOLEAN_OSV_ROLE_SWAP = (
    "Правоохранители обязаны аннулировать и изъять лицензию на оружие если "
    "губернатора поддерживает прокурор и имеется решение суда, "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_boolean_condition_keeps_subject_and_object_roles(
    validate: RelationValidator,
) -> None:
    validate(_CLOSED_UK_BOOLEAN_OSV_SOURCE, _CLOSED_UK_BOOLEAN_OSV_ALIAS)
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_BOOLEAN_OSV_SOURCE,
            _CLOSED_UK_BOOLEAN_OSV_ROLE_SWAP,
        )


_CLOSED_UK_QUOTED_PREDICATE_LIKE_SEPARATOR_SOURCE = (
    "Суд уведомил организацию, при этом в деле «Служить» прокуратура обязана "
    "аннулировать и изъять лицензию на оружие при задержании "
    + _CLOSED_UK_REVIEW_GROUND
)
_CLOSED_UK_QUOTED_PREDICATE_LIKE_SEPARATOR_ALIAS = (
    "Суд уведомил организацию, при этом в деле «Служить» прокуратура обязана "
    "аннулировать и изъять лицензию на оружие при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_QUOTED_PREDICATE_LIKE_DROPPED_CLAUSE = (
    "Прокуратура обязана аннулировать и изъять лицензию на оружие при "
    "задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
)
_CLOSED_UK_QUOTED_PREDICATE_LIKE_BORROWED_ACTOR = (
    "Суд обязан аннулировать и изъять лицензию на оружие при задержании "
    + _CLOSED_UK_COMPACT_REVIEW_GROUND
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
def test_v3_closed_uk_quoted_predicate_word_does_not_hide_real_separator(
    validate: RelationValidator,
) -> None:
    connector = _CLOSED_UK_QUOTED_PREDICATE_LIKE_SEPARATOR_SOURCE.index(
        " при этом"
    )
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(
            _CLOSED_UK_QUOTED_PREDICATE_LIKE_SEPARATOR_SOURCE
        )
    )
    validate(
        _CLOSED_UK_QUOTED_PREDICATE_LIKE_SEPARATOR_SOURCE,
        _CLOSED_UK_QUOTED_PREDICATE_LIKE_SEPARATOR_ALIAS,
    )
    for unsafe in (
        _CLOSED_UK_QUOTED_PREDICATE_LIKE_DROPPED_CLAUSE,
        _CLOSED_UK_QUOTED_PREDICATE_LIKE_BORROWED_ACTOR,
    ):
        with pytest.raises(ValueError):
            validate(
                _CLOSED_UK_QUOTED_PREDICATE_LIKE_SEPARATOR_SOURCE,
                unsafe,
            )


_CLOSED_UK_IDENTITY_GAPPED_AUXILIARY_CASES = (
    (
        "Лицензия на оружие должна «Добру» Быть аннулирована и изъята при "
        "задержании "
        + _CLOSED_UK_REVIEW_GROUND,
        "Лицензия на оружие должна «Добру» Быть аннулирована и изъята при "
        "задержании "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        " Быть",
    ),
    (
        "Лицензия на оружие должна быть «Добру» аннулирована и изъята при "
        "задержании "
        + _CLOSED_UK_REVIEW_GROUND,
        "Лицензия на оружие должна быть «Добру» аннулирована и изъята при "
        "задержании "
        + _CLOSED_UK_COMPACT_REVIEW_GROUND,
        " быть",
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "auxiliary_surface"),
    _CLOSED_UK_IDENTITY_GAPPED_AUXILIARY_CASES,
    ids=("identity-between-modal-and-be", "identity-between-be-and-action"),
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
def test_v3_closed_uk_identity_gap_prevents_auxiliary_masking(
    source: str,
    faithful: str,
    auxiliary_surface: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    unsafe = faithful.replace(auxiliary_surface, "", 1)
    assert unsafe != faithful
    with pytest.raises(ValueError):
        validate(source, unsafe)


def _closed_uk_boolean_condition_surface(
    condition: str,
    *,
    compact_ground: bool,
) -> str:
    ground = (
        _CLOSED_UK_COMPACT_REVIEW_GROUND
        if compact_ground
        else _CLOSED_UK_REVIEW_GROUND
    )
    return (
        "Правоохранители обязаны аннулировать и изъять лицензию на оружие "
        f"если {condition}, {ground}"
    )


_CLOSED_UK_SHARED_HEAD_COLLISION_SOURCE = _closed_uk_boolean_condition_surface(
    "нарушены правила хранения или правила организации действуют",
    compact_ground=False,
)
_CLOSED_UK_SHARED_HEAD_COLLISION_ALIAS = _closed_uk_boolean_condition_surface(
    "нарушены правила хранения или правила организации действуют",
    compact_ground=True,
)
_CLOSED_UK_SHARED_HEAD_COLLISION_UNSAFE = _closed_uk_boolean_condition_surface(
    "нарушены правила хранения или организации действуют",
    compact_ground=True,
)
_CLOSED_UK_SHARED_HEAD_ELLIPSIS_SOURCE = _closed_uk_boolean_condition_surface(
    "нарушены правила хранения или правила организации",
    compact_ground=False,
)
_CLOSED_UK_SHARED_HEAD_ELLIPSIS_FULL = _closed_uk_boolean_condition_surface(
    "нарушены правила хранения или правила организации",
    compact_ground=True,
)
_CLOSED_UK_SHARED_HEAD_ELLIPSIS_COMPACT = _closed_uk_boolean_condition_surface(
    "нарушены правила хранения или организации",
    compact_ground=True,
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
def test_v3_closed_uk_shared_condition_head_requires_true_ellipsis(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SHARED_HEAD_COLLISION_SOURCE,
        _CLOSED_UK_SHARED_HEAD_COLLISION_ALIAS,
    )
    validate(
        _CLOSED_UK_SHARED_HEAD_ELLIPSIS_SOURCE,
        _CLOSED_UK_SHARED_HEAD_ELLIPSIS_FULL,
    )
    validate(
        _CLOSED_UK_SHARED_HEAD_ELLIPSIS_SOURCE,
        _CLOSED_UK_SHARED_HEAD_ELLIPSIS_COMPACT,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SHARED_HEAD_COLLISION_SOURCE,
            _CLOSED_UK_SHARED_HEAD_COLLISION_UNSAFE,
        )


_CLOSED_UK_JOINT_ACTOR_CORRELATIVE_CASES = (
    (
        _closed_uk_correlative_tree_surface(
            f"Либо {_CLOSED_UK_TREE_ACTION_A}, либо прокурор и губернатор "
            "обязаны изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо прокурор и губернатор обязаны изъять лицензию на оружие, "
            f"либо {_CLOSED_UK_TREE_ACTION_A}",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                f"Либо {_CLOSED_UK_TREE_ACTION_A}, и прокурор и губернатор "
                "обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"{_CLOSED_UK_TREE_ACTION_A}, либо прокурор и губернатор "
                "обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            f"Как {_CLOSED_UK_TREE_ACTION_A}, так и прокурор и губернатор "
            "обязаны изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Как прокурор и губернатор обязаны изъять лицензию на оружие, "
            f"так и {_CLOSED_UK_TREE_ACTION_A}",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                f"Как {_CLOSED_UK_TREE_ACTION_A}, либо прокурор и губернатор "
                "обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                f"{_CLOSED_UK_TREE_ACTION_A}, так и прокурор и губернатор "
                "обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Либо прокуратура обязана аннулировать лицензию на оружие, либо "
            "при условии решения суда или санкции прокурора суд обязан "
            "изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо при условии санкции прокурора или решения суда суд обязан "
            "изъять лицензию на оружие, либо прокуратура обязана "
            "аннулировать лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо прокуратура обязана аннулировать лицензию на оружие, "
                "при условии решения суда или санкции прокурора суд обязан "
                "изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо прокуратура обязана аннулировать лицензию на оружие, и "
                "при условии решения суда или санкции прокурора суд обязан "
                "изъять лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe_outputs"),
    _CLOSED_UK_JOINT_ACTOR_CORRELATIVE_CASES,
    ids=("either-joint-actor", "both-joint-actor", "internal-condition-or"),
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
def test_v3_closed_uk_correlative_mate_is_not_an_actor_or_condition_connector(
    source: str,
    faithful: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in unsafe_outputs:
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_SHARED_HEAD_PREDICATE_COLLISION_CASES = tuple(
    (
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или правила организации {predicate}",
            compact_ground=False,
        ),
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или правила организации {predicate}",
            compact_ground=True,
        ),
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или организации {predicate}",
            compact_ground=True,
        ),
    )
    for predicate in (
        "должны быть доступны",
        "есть в базе",
        "доступны",
    )
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    _CLOSED_UK_SHARED_HEAD_PREDICATE_COLLISION_CASES,
    ids=("modal-passive", "existential", "short-predicative"),
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
def test_v3_closed_uk_shared_head_does_not_absorb_a_second_predicate(
    source: str,
    faithful: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    assert unsafe != faithful
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_FINITE_ACTOR_SEPARATOR_SOURCE = (
    "За нарушение правил суд аннулирует лицензию и прокуратура изымает "
    "оружие."
)
_CLOSED_UK_FINITE_ACTOR_SEPARATOR_UNSAFE = (
    "За нарушение правил суд аннулирует лицензию и изымает оружие"
)
_CLOSED_UK_COORDINATED_ACTOR_POSITIVE = (
    "За нарушение правил суд и прокуратура аннулируют лицензию."
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
def test_v3_closed_uk_new_finite_actor_starts_a_real_relation_separator(
    validate: RelationValidator,
) -> None:
    finite_connector = _CLOSED_UK_FINITE_ACTOR_SEPARATOR_SOURCE.index(
        " и прокуратура"
    )
    assert any(
        start <= finite_connector < end
        for start, end in _relation_separator_spans(
            _CLOSED_UK_FINITE_ACTOR_SEPARATOR_SOURCE
        )
    )
    validate(
        _CLOSED_UK_FINITE_ACTOR_SEPARATOR_SOURCE,
        _CLOSED_UK_FINITE_ACTOR_SEPARATOR_SOURCE.rstrip("."),
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_FINITE_ACTOR_SEPARATOR_SOURCE,
            _CLOSED_UK_FINITE_ACTOR_SEPARATOR_UNSAFE,
        )

    coordinated_connector = _CLOSED_UK_COORDINATED_ACTOR_POSITIVE.index(
        " и прокуратура"
    )
    assert not any(
        start <= coordinated_connector < end
        for start, end in _relation_separator_spans(
            _CLOSED_UK_COORDINATED_ACTOR_POSITIVE
        )
    )
    validate(
        _CLOSED_UK_COORDINATED_ACTOR_POSITIVE,
        _CLOSED_UK_COORDINATED_ACTOR_POSITIVE.rstrip("."),
    )


_CLOSED_UK_INTERNAL_PUNCTUATION_SOURCE = _closed_uk_boolean_condition_surface(
    "подать жалобу, нельзя исполнить решение и получено уведомление",
    compact_ground=False,
)
_CLOSED_UK_INTERNAL_PUNCTUATION_ALIAS = _closed_uk_boolean_condition_surface(
    "подать жалобу, нельзя исполнить решение и получено уведомление",
    compact_ground=True,
)
_CLOSED_UK_INTERNAL_PUNCTUATION_OUTER_GROUP = (
    _closed_uk_boolean_condition_surface(
        "(подать жалобу, нельзя исполнить решение и получено уведомление)",
        compact_ground=True,
    )
)
_CLOSED_UK_INTERNAL_PUNCTUATION_COLLISION = (
    _closed_uk_boolean_condition_surface(
        "подать жалобу нельзя, исполнить решение и получено уведомление",
        compact_ground=True,
    )
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
def test_v3_closed_uk_internal_boolean_punctuation_is_semantic(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_INTERNAL_PUNCTUATION_SOURCE,
        _CLOSED_UK_INTERNAL_PUNCTUATION_ALIAS,
    )
    validate(
        _CLOSED_UK_INTERNAL_PUNCTUATION_SOURCE,
        _CLOSED_UK_INTERNAL_PUNCTUATION_OUTER_GROUP,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_INTERNAL_PUNCTUATION_SOURCE,
            _CLOSED_UK_INTERNAL_PUNCTUATION_COLLISION,
        )


_CLOSED_UK_SHARED_HEAD_PREPOSITIONAL_COLLISION_SOURCE = (
    _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или правила организации в базе",
        compact_ground=False,
    )
)
_CLOSED_UK_SHARED_HEAD_PREPOSITIONAL_COLLISION_ALIAS = (
    _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или правила организации в базе",
        compact_ground=True,
    )
)
_CLOSED_UK_SHARED_HEAD_PREPOSITIONAL_COLLISION_UNSAFE = (
    _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или организации в базе",
        compact_ground=True,
    )
)


_CLOSED_UK_SHARED_HEAD_NOMINAL_ELLIPSIS_CASES = tuple(
    (
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или правила {nominal}",
            compact_ground=False,
        ),
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или {nominal}",
            compact_ground=True,
        ),
    )
    for nominal in (
        "службы безопасности",
        "разрешённой парковки",
        "автоматически доступных зон",
    )
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
def test_v3_closed_uk_shared_head_prepositional_branch_is_not_ellipsis(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SHARED_HEAD_PREPOSITIONAL_COLLISION_SOURCE,
        _CLOSED_UK_SHARED_HEAD_PREPOSITIONAL_COLLISION_ALIAS,
    )
    with pytest.raises(ValueError):
        validate(
            _CLOSED_UK_SHARED_HEAD_PREPOSITIONAL_COLLISION_SOURCE,
            _CLOSED_UK_SHARED_HEAD_PREPOSITIONAL_COLLISION_UNSAFE,
        )


@pytest.mark.parametrize(
    ("source", "faithful"),
    _CLOSED_UK_SHARED_HEAD_NOMINAL_ELLIPSIS_CASES,
    ids=("security-service", "permitted-parking", "automatically-accessible"),
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
def test_v3_closed_uk_shared_head_allows_only_nominal_ellipsis(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


_CLOSED_UK_LONG_EXPLICIT_IDENTITY = "А" * 220


_CLOSED_UK_EXPLICIT_SEPARATOR_FINAL_CASES = (
    (
        "Суд уведомил организацию, при этом решение об аннулировании "
        "лицензии прокуратура обязана исполнить.",
        " при этом",
        (
            "Решение об аннулировании лицензии прокуратура обязана исполнить",
        ),
    ),
    (
        "Суд уведомил организацию, при этом аннулированную лицензию "
        "прокуратура обязана изъять.",
        " при этом",
        ("Аннулированную лицензию прокуратура обязана изъять",),
    ),
    (
        "Суд уведомил организацию, при этом лицензию аннулировала "
        "прокуратура.",
        " при этом",
        ("Лицензию аннулировала прокуратура",),
    ),
    (
        "Вчера суд уведомил гражданина и прокуратура по делу «"
        + _CLOSED_UK_LONG_EXPLICIT_IDENTITY
        + "» должна аннулировать лицензию.",
        " и прокуратура",
        (
            "Прокуратура по делу «"
            + _CLOSED_UK_LONG_EXPLICIT_IDENTITY
            + "» должна аннулировать лицензию",
            "Вчера суд уведомил гражданина и прокуратура по делу «"
            + ("Б" * 220)
            + "» должна аннулировать лицензию",
        ),
    ),
    (
        "Суд уведомил организацию, при этом прокуратура по делу «"
        + _CLOSED_UK_LONG_EXPLICIT_IDENTITY
        + "» должна аннулировать лицензию.",
        " при этом",
        (
            "Прокуратура по делу «"
            + _CLOSED_UK_LONG_EXPLICIT_IDENTITY
            + "» должна аннулировать лицензию",
            "Суд уведомил организацию, при этом прокуратура по делу «"
            + ("Б" * 220)
            + "» должна аннулировать лицензию",
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "connector_needle", "unsafe_outputs"),
    _CLOSED_UK_EXPLICIT_SEPARATOR_FINAL_CASES,
    ids=(
        "nominal-before-predicate",
        "attributive-before-predicate",
        "object-first-postposed-subject",
        "long-main-connector-identity",
        "long-explicit-connector-identity",
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
def test_v3_closed_uk_explicit_separator_uses_the_real_predicate_boundary(
    source: str,
    connector_needle: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    connector = source.index(connector_needle)
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for unsafe in unsafe_outputs:
        assert unsafe != source.rstrip(".")
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_GROUND_MAIN_SEPARATOR_CASES = (
    (
        "За нарушение правил прокурору суд сообщил решение и прокуратура "
        "изымает оружие.",
        " и прокуратура",
        True,
        (
            "За нарушение правил прокурору суд сообщил решение и изымает "
            "оружие",
        ),
    ),
    (
        "За нарушение правил суд аннулирует лицензию и организации передаёт "
        "документ.",
        " и организации",
        False,
        (),
    ),
    (
        "Суд аннулировал лицензию. За нарушение правил суд и прокуратура "
        "изымают оружие.",
        " и прокуратура",
        False,
        (),
    ),
    (
        "За изъятое оружие суд и прокуратура взыскивают штраф.",
        " и прокуратура",
        False,
        (),
    ),
    (
        "За нарушение правил суд уведомляет прокурора и гражданин обязан "
        "аннулировать лицензию.",
        " и гражданин",
        True,
        (
            "За нарушение правил суд уведомляет прокурора и обязан "
            "аннулировать лицензию",
        ),
    ),
    (
        "Суд проверил документ и губернатор аннулировал лицензию.",
        " и губернатор",
        True,
        ("Суд проверил документ и аннулировал лицензию",),
    ),
    (
        "Вчера гражданин дал документ и владелец обязан вернуть его.",
        " и владелец",
        True,
        ("Вчера гражданин дал документ и обязан вернуть его",),
    ),
    (
        "За нарушение правил суд и гражданин аннулируют лицензию.",
        " и гражданин",
        False,
        (),
    ),
    (
        "Суд и губернатор аннулируют лицензию.",
        " и губернатор",
        False,
        (),
    ),
    (
        "Вчера гражданин и владелец дали документ.",
        " и владелец",
        False,
        (),
    ),
)


@pytest.mark.parametrize(
    ("source", "connector_needle", "must_split", "unsafe_outputs"),
    _CLOSED_UK_GROUND_MAIN_SEPARATOR_CASES,
    ids=(
        "oblique-prosecutor-real-subject",
        "dative-organization-object",
        "prior-sentence-does-not-contaminate",
        "attributive-ground-coactors",
        "generic-citizen-real-subject",
        "governor-real-subject",
        "irregular-gave-real-subject",
        "coordinated-court-citizen",
        "coordinated-court-governor",
        "coordinated-citizen-owner",
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
def test_v3_closed_uk_ground_main_separator_respects_grammatical_role(
    source: str,
    connector_needle: str,
    must_split: bool,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    connector = source.index(connector_needle)
    spans = _relation_separator_spans(source)
    is_separator = any(start <= connector < end for start, end in spans)
    assert is_separator is must_split
    validate(source, source.rstrip("."))
    for unsafe in unsafe_outputs:
        assert unsafe != source.rstrip(".")
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_RECORD_AWARE_CORRELATIVE_CASES = (
    (
        _closed_uk_correlative_tree_surface(
            "Либо лицензию на оружие аннулирует прокурор, либо полиция "
            "изымает лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо полиция изымает лицензию на оружие, либо лицензию на "
            "оружие аннулирует прокурор",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо лицензию на оружие аннулирует прокурор, и полиция "
                "изымает лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо лицензию на оружие аннулирует прокурор, полиция "
                "изымает лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Либо суд аннулирует лицензию на оружие при согласии прокурора, "
            "либо полиция изымает лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо полиция изымает лицензию на оружие, либо суд аннулирует "
            "лицензию на оружие при согласии прокурора",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо суд аннулирует лицензию на оружие при согласии "
                "прокурора, и полиция изымает лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо суд аннулирует лицензию на оружие при согласии "
                "прокурора, полиция изымает лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Или суд обязан аннулировать лицензию на оружие, или лицензия "
            "на оружие организации «Альфа» или «Бета» должна быть изъята",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Или лицензия на оружие организации «Альфа» или «Бета» должна "
            "быть изъята, или суд обязан аннулировать лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Или суд обязан аннулировать лицензию на оружие, лицензия "
                "на оружие организации «Альфа» или «Бета» должна быть изъята",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Или суд обязан аннулировать лицензию на оружие, и лицензия "
                "на оружие организации «Альфа» или «Бета» должна быть изъята",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Или суд обязан аннулировать лицензию на оружие, или лицензия на "
            "оружие или разрешение на оружие должны быть изъяты",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Или лицензия на оружие или разрешение на оружие должны быть "
            "изъяты, или суд обязан аннулировать лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Или суд обязан аннулировать лицензию на оружие, лицензия на "
                "оружие или разрешение на оружие должны быть изъяты",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Или суд обязан аннулировать лицензию на оружие, и лицензия "
                "на оружие или разрешение на оружие должны быть изъяты",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Либо суд обязан аннулировать лицензию на оружие, либо прокурор "
            "и гражданин Иванов обязаны изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо прокурор и гражданин Иванов обязаны изъять лицензию на "
            "оружие, либо суд обязан аннулировать лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан аннулировать лицензию на оружие, и прокурор "
                "и гражданин Иванов обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан аннулировать лицензию на оружие, прокурор "
                "и гражданин Иванов обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо прокурор обязан изъять лицензию на оружие, либо суд "
                "обязан аннулировать лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо прокурор и гражданин Петров обязаны изъять лицензию на "
                "оружие, либо суд обязан аннулировать лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Либо суд обязан аннулировать лицензию на оружие после этого "
            "решения, либо полиция должна изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо полиция должна изъять лицензию на оружие, либо суд обязан "
            "аннулировать лицензию на оружие после этого решения",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан аннулировать лицензию на оружие после этого "
                "решения, и полиция должна изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан аннулировать лицензию на оружие после этого "
                "решения, полиция должна изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо полиция должна изъять лицензию на оружие, либо суд "
                "обязан аннулировать лицензию на оружие после этого приказа",
                compact_ground=True,
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe_outputs"),
    _CLOSED_UK_RECORD_AWARE_CORRELATIVE_CASES,
    ids=(
        "left-postposed-actor",
        "left-authorization",
        "right-object-first-owner-alternatives",
        "right-object-first-typed-alternatives",
        "partially-recognized-joint-actor",
        "left-temporal-detail",
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
def test_v3_closed_uk_record_aware_bridge_owns_only_the_outer_mate(
    source: str,
    faithful: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in unsafe_outputs:
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_ACTOR_CORRELATIVE_SOURCE = _closed_uk_correlative_tree_surface(
    "И полиция, и прокуратура обязаны аннулировать и изъять лицензию на "
    "оружие",
    compact_ground=False,
)
_CLOSED_UK_ACTOR_CORRELATIVE_ALIAS = _closed_uk_correlative_tree_surface(
    "И прокуратура, и полиция обязаны изъять и аннулировать лицензию на "
    "оружие",
    compact_ground=True,
)
_CLOSED_UK_ACTOR_TO_ACTION_CORRELATIVE_TRANSFER = (
    _closed_uk_correlative_tree_surface(
        "Полиция и прокуратура обязаны и аннулировать, и изъять лицензию на "
        "оружие",
        compact_ground=True,
    )
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
def test_v3_closed_uk_actor_correlative_cannot_transfer_to_actions(
    validate: RelationValidator,
) -> None:
    actor_connector = _CLOSED_UK_ACTOR_CORRELATIVE_SOURCE.index(
        ", и прокуратура"
    )
    assert not any(
        start <= actor_connector < end
        for start, end in _relation_separator_spans(
            _CLOSED_UK_ACTOR_CORRELATIVE_SOURCE
        )
    )
    validate(
        _CLOSED_UK_ACTOR_CORRELATIVE_SOURCE,
        _CLOSED_UK_ACTOR_CORRELATIVE_ALIAS,
    )
    for unsafe in (
        _CLOSED_UK_ACTOR_TO_ACTION_CORRELATIVE_TRANSFER,
        _CLOSED_UK_ACTOR_CORRELATIVE_ALIAS.replace(
            ", и полиция",
            ", либо полиция",
            1,
        ),
    ):
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_ACTOR_CORRELATIVE_SOURCE, unsafe)


_CLOSED_UK_BOUNDARY_CORRELATIVE_SOURCE = _closed_uk_correlative_tree_surface(
    "Последует: — либо суд обязан аннулировать лицензию на оружие, либо "
    "полиция должна изъять лицензию на оружие",
    compact_ground=False,
)
_CLOSED_UK_BOUNDARY_CORRELATIVE_ALIAS = _closed_uk_correlative_tree_surface(
    "Последует: — либо полиция должна изъять лицензию на оружие, либо суд "
    "обязан аннулировать лицензию на оружие",
    compact_ground=True,
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
def test_v3_closed_uk_boundary_introduced_correlative_owns_both_markers(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_BOUNDARY_CORRELATIVE_SOURCE,
        _CLOSED_UK_BOUNDARY_CORRELATIVE_ALIAS,
    )
    for unsafe in (
        _closed_uk_correlative_tree_surface(
            "Последует: — либо суд обязан аннулировать лицензию на оружие, "
            "и полиция должна изъять лицензию на оружие",
            compact_ground=True,
        ),
        _closed_uk_correlative_tree_surface(
            "Последует: — либо суд обязан аннулировать лицензию на оружие, "
            "полиция должна изъять лицензию на оружие",
            compact_ground=True,
        ),
        _closed_uk_correlative_tree_surface(
            "Последует: — суд обязан аннулировать лицензию на оружие, либо "
            "полиция должна изъять лицензию на оружие",
            compact_ground=True,
        ),
    ):
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_BOUNDARY_CORRELATIVE_SOURCE, unsafe)


_CLOSED_UK_SHARED_PATIENT_SOURCE = _closed_uk_correlative_tree_surface(
    "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
    "при задержании",
    compact_ground=False,
)
_CLOSED_UK_SHARED_PATIENT_FAITHFUL = (
    _closed_uk_correlative_tree_surface(
        "Правоохранители обязаны аннулировать лицензию на оружие и изъять "
        "лицензию на оружие при задержании",
        compact_ground=True,
    )
)
_CLOSED_UK_SHARED_PATIENT_REORDERED = (
    _closed_uk_correlative_tree_surface(
        "Правоохранители обязаны изъять лицензию на оружие при задержании "
        "и аннулировать лицензию на оружие",
        compact_ground=True,
    )
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
def test_v3_closed_uk_shared_patient_stays_with_both_actions(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SHARED_PATIENT_SOURCE,
        _CLOSED_UK_SHARED_PATIENT_FAITHFUL,
    )
    validate(
        _CLOSED_UK_SHARED_PATIENT_SOURCE,
        _CLOSED_UK_SHARED_PATIENT_REORDERED,
    )
    for unsafe in (
        _closed_uk_correlative_tree_surface(
            "Правоохранители обязаны изъять лицензию на оружие и "
            "аннулировать при задержании",
            compact_ground=True,
        ),
        _closed_uk_correlative_tree_surface(
            "Правоохранители обязаны аннулировать и изъять при задержании",
            compact_ground=True,
        ),
    ):
        assert unsafe != _CLOSED_UK_SHARED_PATIENT_FAITHFUL
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_SHARED_PATIENT_SOURCE, unsafe)


_CLOSED_UK_CORRELATIVE_PATIENT_CASES = (
    (
        _closed_uk_correlative_tree_surface(
            "Либо суд обязан аннулировать лицензию на оружие, либо полиция "
            "обязана изъять её",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо суд обязан аннулировать лицензию на оружие, либо полиция "
            "обязана изъять лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан изъять лицензию на оружие, либо полиция "
                "обязана аннулировать лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан аннулировать лицензию на оружие, и полиция "
                "обязана изъять лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Либо суд обязан аннулировать лицензию на оружие, либо полиция "
            "обязана изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо полиция обязана изъять лицензию на оружие, либо суд "
            "обязан аннулировать лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо полиция обязана аннулировать лицензию на оружие, "
                "либо суд обязан изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо полиция обязана изъять лицензию на оружие, и суд "
                "обязан аннулировать лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe_outputs"),
    _CLOSED_UK_CORRELATIVE_PATIENT_CASES,
    ids=("anaphoric-patient-expansion", "complete-patient-branch-reorder"),
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
def test_v3_closed_uk_correlative_patient_cannot_transfer_between_actors(
    source: str,
    faithful: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in unsafe_outputs:
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_COMPLETE_ACTOR_GROUP_CASES = (
    (
        "прокурор и губернатор вправе изъять лицензию на оружие",
        "прокурор вправе изъять лицензию на оружие",
        "прокурор или губернатор вправе изъять лицензию на оружие",
        "прокурор и губернатор обязаны изъять лицензию на оружие",
    ),
    (
        "прокурор или губернатор вправе изъять лицензию на оружие",
        "прокурор вправе изъять лицензию на оружие",
        "прокурор и губернатор вправе изъять лицензию на оружие",
        "прокурор или губернатор обязаны изъять лицензию на оружие",
    ),
    (
        "прокурор, а также губернатор обязаны изъять лицензию на оружие",
        "прокурор обязан изъять лицензию на оружие",
        "прокурор или губернатор обязаны изъять лицензию на оружие",
        "прокурор, а также губернатор вправе изъять лицензию на оружие",
    ),
)


@pytest.mark.parametrize(
    ("right_branch", "dropped_actor", "changed_group", "changed_modality"),
    _CLOSED_UK_COMPLETE_ACTOR_GROUP_CASES,
    ids=("joint-right-actor", "alternative-right-actor", "also-right-actor"),
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
def test_v3_closed_uk_outer_pair_preserves_complete_right_actor_group(
    right_branch: str,
    dropped_actor: str,
    changed_group: str,
    changed_modality: str,
    validate: RelationValidator,
) -> None:
    left_branch = "суд обязан аннулировать лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        right_branch,
        f"Либо {dropped_actor}, либо {left_branch}",
        f"Либо {changed_group}, либо {left_branch}",
        f"Либо {changed_modality}, либо {left_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_PRE_ACTION_INTERNAL_OR_CASES = (
    (
        "только по пятницам или субботам прокуратура обязана изъять "
        "лицензию на оружие",
        "по пятницам и субботам прокуратура обязана изъять лицензию "
        "на оружие",
        "прокуратура обязана изъять лицензию на оружие",
        "только по пятницам или субботам суд обязан аннулировать лицензию "
        "на оружие",
    ),
    (
        "по решению суда или прокурора полиция обязана изъять лицензию "
        "на оружие",
        "по решению суда и прокурора полиция обязана изъять лицензию "
        "на оружие",
        "полиция обязана изъять лицензию на оружие",
        "по решению суда или прокурора суд обязан аннулировать лицензию "
        "на оружие",
    ),
)


@pytest.mark.parametrize(
    ("right_branch", "changed_internal_or", "dropped_scope", "moved_scope"),
    _CLOSED_UK_PRE_ACTION_INTERNAL_OR_CASES,
    ids=("weekday-alternative", "decision-actor-alternative"),
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
def test_v3_closed_uk_outer_pair_owns_pre_action_internal_or(
    right_branch: str,
    changed_internal_or: str,
    dropped_scope: str,
    moved_scope: str,
    validate: RelationValidator,
) -> None:
    left_branch = "суд обязан аннулировать лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        right_branch,
        f"Либо {changed_internal_or}, либо {left_branch}",
        f"Либо {dropped_scope}, либо {left_branch}",
        f"Либо {moved_scope}, либо полиция обязана изъять лицензию на оружие",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_PRE_ACTION_PERMISSION_CASES = (
    (
        "с разрешения прокурора полиция обязана изъять лицензию на оружие",
        "без согласия прокурора полиция обязана изъять лицензию на оружие",
        "с разрешения прокурора суд обязан аннулировать лицензию на оружие",
    ),
    (
        "без согласия прокурора полиция обязана изъять лицензию на оружие",
        "с разрешения прокурора полиция обязана изъять лицензию на оружие",
        "без согласия прокурора суд обязан аннулировать лицензию на оружие",
    ),
)


@pytest.mark.parametrize(
    ("right_branch", "flipped_permission", "moved_permission"),
    _CLOSED_UK_PRE_ACTION_PERMISSION_CASES,
    ids=("positive-permission", "negative-permission"),
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
def test_v3_closed_uk_outer_pair_preserves_pre_action_permission(
    right_branch: str,
    flipped_permission: str,
    moved_permission: str,
    validate: RelationValidator,
) -> None:
    left_branch = "суд обязан аннулировать лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        right_branch,
        "Либо полиция обязана изъять лицензию на оружие, либо "
        f"{left_branch}",
        f"Либо {flipped_permission}, либо {left_branch}",
        f"Либо {moved_permission}, либо полиция обязана изъять "
        "лицензию на оружие",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_SEMANTIC_PARENTHESES_SOURCE = (
    _closed_uk_boolean_condition_surface(
        "уведомление (подать жалобу, нельзя исполнить решение) и санкция",
        compact_ground=False,
    )
)
_CLOSED_UK_SEMANTIC_PARENTHESES_FAITHFUL = (
    _closed_uk_boolean_condition_surface(
        "уведомление (подать жалобу, нельзя исполнить решение) и санкция",
        compact_ground=True,
    )
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
def test_v3_closed_uk_semantic_parentheses_keep_internal_punctuation(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SEMANTIC_PARENTHESES_SOURCE,
        _CLOSED_UK_SEMANTIC_PARENTHESES_FAITHFUL,
    )
    for condition in (
        "уведомление (подать жалобу нельзя, исполнить решение) и санкция",
        "уведомление (подать жалобу), нельзя исполнить решение и санкция",
    ):
        unsafe = _closed_uk_boolean_condition_surface(
            condition,
            compact_ground=True,
        )
        assert unsafe != _CLOSED_UK_SEMANTIC_PARENTHESES_FAITHFUL
        with pytest.raises(ValueError):
            validate(_CLOSED_UK_SEMANTIC_PARENTHESES_SOURCE, unsafe)


_CLOSED_UK_QUOTED_PUNCTUATION_POSITIVES = tuple(
    (
        _closed_uk_boolean_condition_surface(
            condition,
            compact_ground=False,
        ),
        _closed_uk_boolean_condition_surface(
            condition,
            compact_ground=True,
        ),
    )
    for condition in (
        "уведомление «Подать жалобу, нельзя исполнить решение» и санкция",
        "уведомление организации «Право, порядок» и санкция",
    )
)


@pytest.mark.parametrize(
    ("source", "faithful"),
    _CLOSED_UK_QUOTED_PUNCTUATION_POSITIVES,
    ids=("quoted-content", "punctuated-organization-name"),
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
def test_v3_closed_uk_quoted_identity_punctuation_is_opaque(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


_CLOSED_UK_PERMITTED_PARKING_ELLIPSIS_CASES = (
    (
        _closed_uk_boolean_condition_surface(
            "нарушены правила хранения или правила разрешённой парковки",
            compact_ground=False,
        ),
        _closed_uk_boolean_condition_surface(
            "нарушены правила хранения или разрешённой парковки",
            compact_ground=True,
        ),
    ),
    (
        _closed_uk_boolean_condition_surface(
            "нарушены правила хранения или разрешённой парковки",
            compact_ground=False,
        ),
        _closed_uk_boolean_condition_surface(
            "нарушены правила хранения или правила разрешённой парковки",
            compact_ground=True,
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful"),
    _CLOSED_UK_PERMITTED_PARKING_ELLIPSIS_CASES,
    ids=("contract-shared-head", "expand-shared-head"),
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
def test_v3_closed_uk_permitted_parking_supports_nominal_ellipsis_both_ways(
    source: str,
    faithful: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)


_CLOSED_UK_FINITE_SHARED_HEAD_REPRO_CASES = tuple(
    (
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или правила организации {predicate}",
            compact_ground=False,
        ),
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или правила организации {predicate}",
            compact_ground=True,
        ),
        _closed_uk_boolean_condition_surface(
            f"нарушены правила хранения или организации {predicate}",
            compact_ground=True,
        ),
    )
    for predicate in ("работают", "были в базе")
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    _CLOSED_UK_FINITE_SHARED_HEAD_REPRO_CASES,
    ids=("finite-works", "finite-were-in-database"),
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
def test_v3_closed_uk_finite_predicate_blocks_nominal_head_inheritance(
    source: str,
    faithful: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    assert unsafe != faithful
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_EXACT_SEPARATOR_SUBJECT_CASES = (
    (
        "Суд проверил документ и комиссия аннулировала лицензию.",
        " и комиссия",
        ("Суд проверил документ и аннулировал лицензию",),
        "Суд и комиссия проверили документ.",
    ),
    (
        "Суд проверил документ и граждане обязаны вернуть документ.",
        " и граждане",
        ("Суд проверил документ и обязан вернуть документ",),
        "Суд и граждане обязаны вернуть документ.",
    ),
    (
        "Суд проверил документ и прокуратура после уведомления сторон "
        "должна аннулировать лицензию.",
        " и прокуратура",
        (
            "Суд проверил документ и после уведомления сторон должна "
            "аннулировать лицензию",
            "Суд проверил документ и после уведомления сторон обязан "
            "аннулировать лицензию",
        ),
        "Суд и прокуратура после уведомления сторон должны аннулировать "
        "лицензию.",
    ),
)


@pytest.mark.parametrize(
    ("source", "connector_needle", "unsafe_outputs", "coordinated"),
    _CLOSED_UK_EXACT_SEPARATOR_SUBJECT_CASES,
    ids=("commission", "citizens", "prosecution-with-leading-adjunct"),
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
def test_v3_closed_uk_new_subject_after_and_starts_a_relation(
    source: str,
    connector_needle: str,
    unsafe_outputs: tuple[str, ...],
    coordinated: str,
    validate: RelationValidator,
) -> None:
    connector = source.index(connector_needle)
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    coordinated_connector = coordinated.index(connector_needle)
    assert not any(
        start <= coordinated_connector < end
        for start, end in _relation_separator_spans(coordinated)
    )
    validate(coordinated, coordinated.rstrip("."))
    for unsafe in unsafe_outputs:
        assert unsafe != source.rstrip(".")
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_LONG_RIGHT_SUBJECT_ADJUNCT = (
    "по материалам дела «"
    + ("А" * 230)
    + "» после уведомления сторон и получения заключения комиссии"
)
_CLOSED_UK_INSIDE_GROUND_SEPARATOR_CASES = (
    (
        "За нарушение правил суд проверяет документ и прокуратура "
        "аннулирует лицензию.",
        " и прокуратура",
        "аннулирует",
        (
            "За нарушение правил суд проверяет документ и аннулирует "
            "лицензию",
        ),
    ),
    (
        "За нарушение правил суд проверяет документ и прокуратура "
        + _CLOSED_UK_LONG_RIGHT_SUBJECT_ADJUNCT
        + " должна аннулировать лицензию.",
        " и прокуратура",
        " должна аннулировать",
        (
            "За нарушение правил суд проверяет документ и "
            + _CLOSED_UK_LONG_RIGHT_SUBJECT_ADJUNCT
            + " должен аннулировать лицензию",
        ),
    ),
)
_CLOSED_UK_INSIDE_GROUND_COACTOR_POSITIVES = (
    "За нарушение правил суд и прокуратура проверяют документ.",
    "За нарушение правил проверенный судом и прокуратурой документ "
    "хранится в архиве.",
)


@pytest.mark.parametrize(
    ("source", "connector_needle", "predicate_needle", "unsafe_outputs"),
    _CLOSED_UK_INSIDE_GROUND_SEPARATOR_CASES,
    ids=("direct-right-predicate", "long-right-adjunct-before-predicate"),
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
def test_v3_closed_uk_inside_ground_separator_looks_through_right_adjunct(
    source: str,
    connector_needle: str,
    predicate_needle: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    connector = source.index(connector_needle)
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    predicate = source.index(predicate_needle, connector)
    if "по материалам дела" in source:
        assert predicate - connector > 220
    validate(source, source.rstrip("."))
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)
    for coordinated in _CLOSED_UK_INSIDE_GROUND_COACTOR_POSITIVES:
        coactor_connector = coordinated.index(" и прокуратур")
        assert not any(
            start <= coactor_connector < end
            for start, end in _relation_separator_spans(coordinated)
        )
        validate(coordinated, coordinated.rstrip("."))


_CLOSED_UK_PRE_HEAD_AND_GROUPED_BRIDGE_CASES = (
    (
        _closed_uk_correlative_tree_surface(
            "Либо суд обязан аннулировать лицензию на оружие, либо "
            "оружейная или служебная лицензия должна быть изъята при "
            "задержании",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо оружейная или служебная лицензия должна быть изъята при "
            "задержании, либо суд обязан аннулировать лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан аннулировать лицензию на оружие, и "
                "оружейная или служебная лицензия должна быть изъята при "
                "задержании",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо служебная лицензия должна быть изъята при задержании, "
                "либо суд обязан аннулировать лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "Либо суд обязан аннулировать лицензию на оружие, либо "
            "(прокурор и губернатор) обязаны изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Либо (прокурор и губернатор) обязаны изъять лицензию на "
            "оружие, либо суд обязан аннулировать лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "Либо суд обязан аннулировать лицензию на оружие, и "
                "(прокурор и губернатор) обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "Либо прокурор обязан изъять лицензию на оружие, либо суд "
                "обязан аннулировать лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe_outputs"),
    _CLOSED_UK_PRE_HEAD_AND_GROUPED_BRIDGE_CASES,
    ids=("pre-head-alternative-patient", "parenthesized-actor-group"),
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
def test_v3_closed_uk_bridge_preserves_pre_head_patient_and_grouped_actor(
    source: str,
    faithful: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in unsafe_outputs:
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_NARY_BRIDGE_CASES = (
    (
        _closed_uk_correlative_tree_surface(
            "И суд обязан аннулировать лицензию на оружие, и прокурор и "
            "губернатор обязаны изъять лицензию на оружие, и полиция "
            "обязана приостановить лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "И полиция обязана приостановить лицензию на оружие, и прокурор "
            "и губернатор "
            "обязаны изъять лицензию на оружие, и суд обязан аннулировать "
            "лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "И суд обязан аннулировать лицензию на оружие, и прокурор "
                "обязан изъять лицензию на оружие, и полиция обязана "
                "приостановить лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "И суд обязан аннулировать лицензию на оружие, либо "
                "прокурор и губернатор обязаны изъять лицензию на оружие, "
                "и полиция обязана приостановить лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "И суд обязан аннулировать лицензию на оружие, и прокурор и "
                "губернатор обязаны изъять лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "И суд обязан аннулировать лицензию на оружие, и при задержании "
            "полиция обязана изъять лицензию на оружие, и прокурор обязан "
            "приостановить лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "И прокурор обязан приостановить лицензию на оружие, и при "
            "задержании полиция "
            "обязана изъять лицензию на оружие, и суд обязан аннулировать "
            "лицензию на оружие",
            compact_ground=True,
        ),
        (
            _closed_uk_correlative_tree_surface(
                "И суд обязан аннулировать лицензию на оружие, и полиция "
                "обязана изъять лицензию на оружие, и прокурор обязан "
                "приостановить лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "И при задержании суд обязан аннулировать лицензию на "
                "оружие, и полиция обязана изъять лицензию на оружие, и "
                "прокурор обязан приостановить лицензию на оружие",
                compact_ground=True,
            ),
            _closed_uk_correlative_tree_surface(
                "И суд обязан аннулировать лицензию на оружие, либо при "
                "задержании полиция обязана изъять лицензию на оружие, и "
                "прокурор обязан приостановить лицензию на оружие",
                compact_ground=True,
            ),
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe_outputs"),
    _CLOSED_UK_NARY_BRIDGE_CASES,
    ids=("three-branch-with-joint-middle-actor", "middle-branch-condition"),
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
def test_v3_closed_uk_nary_bridge_preserves_each_complete_branch(
    source: str,
    faithful: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for unsafe in unsafe_outputs:
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_LEADING_DISCOURSE_AND_CASES = (
    (
        _closed_uk_correlative_tree_surface(
            "И суд обязан аннулировать и изъять лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "Суд обязан аннулировать и изъять лицензию на оружие",
            compact_ground=True,
        ),
        _closed_uk_correlative_tree_surface(
            "Суд обязан аннулировать лицензию на оружие",
            compact_ground=True,
        ),
    ),
    (
        _closed_uk_correlative_tree_surface(
            "И суд обязан аннулировать лицензию на оружие, и изъять "
            "лицензию на оружие",
            compact_ground=False,
        ),
        _closed_uk_correlative_tree_surface(
            "И суд обязан изъять лицензию на оружие, и аннулировать "
            "лицензию на оружие",
            compact_ground=True,
        ),
        _closed_uk_correlative_tree_surface(
            "И суд обязан аннулировать лицензию на оружие, либо изъять "
            "лицензию на оружие",
            compact_ground=True,
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    _CLOSED_UK_LEADING_DISCOURSE_AND_CASES,
    ids=("removable-leading-discourse-and", "explicit-comma-action-pair"),
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
def test_v3_closed_uk_leading_discourse_and_is_not_a_false_pair_owner(
    source: str,
    faithful: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    assert unsafe != faithful
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_PRODUCTIVE_SHORT_PLURAL_PREDICATIVES = (
    "актуальны",
    "важны",
    "существенны",
    "необходимы",
    "ясны",
    "верны",
    "ценны",
    "полезны",
    "вредны",
    "срочны",
    "равны",
    "полны",
    "точны",
    "стабильны",
    "новы",
    "правы",
    "редки",
    "строги",
    "широки",
    "велики",
)


@pytest.mark.parametrize(
    "predicate",
    _CLOSED_UK_PRODUCTIVE_SHORT_PLURAL_PREDICATIVES,
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
def test_v3_closed_uk_short_plural_predicative_blocks_head_omission(
    predicate: str,
    validate: RelationValidator,
) -> None:
    source = _closed_uk_boolean_condition_surface(
        f"нарушены правила хранения или правила организации {predicate}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"нарушены правила хранения или правила организации {predicate}",
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        f"нарушены правила хранения или организации {predicate}",
        compact_ground=True,
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
def test_v3_closed_uk_short_predicative_fix_keeps_nominal_ellipsis(
    validate: RelationValidator,
) -> None:
    validate(
        _CLOSED_UK_SHARED_HEAD_ELLIPSIS_SOURCE,
        _CLOSED_UK_SHARED_HEAD_ELLIPSIS_COMPACT,
    )
    validate(
        _CLOSED_UK_PERMITTED_PARKING_ELLIPSIS_CASES[0][0],
        _CLOSED_UK_PERMITTED_PARKING_ELLIPSIS_CASES[0][1],
    )


_CLOSED_UK_OPEN_PLURAL_ACTOR_CASES = (
    ("комиссии", "комиссия", "комиссиям", "обязана"),
    ("агентства", "агентство", "агентствам", "обязано"),
    ("компании", "компания", "компаниям", "обязана"),
)


@pytest.mark.parametrize(
    (
        "plural_actor",
        "singular_actor",
        "oblique_actor",
        "singular_governor",
    ),
    _CLOSED_UK_OPEN_PLURAL_ACTOR_CASES,
    ids=("commissions", "agencies", "companies"),
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
def test_v3_closed_uk_open_plural_actor_cannot_launder_shared_head(
    plural_actor: str,
    singular_actor: str,
    oblique_actor: str,
    singular_governor: str,
    validate: RelationValidator,
) -> None:
    condition = (
        "нарушены правила хранения или правила "
        f"{plural_actor} по делам обязаны действовать"
    )
    source = _closed_uk_boolean_condition_surface(
        condition,
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        condition,
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или "
        f"{plural_actor} по делам обязаны действовать",
        compact_ground=True,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)

    plural_source = (
        "Суд проверил документ и "
        f"{plural_actor} по делам лицензирования обязаны "
        "аннулировать лицензию."
    )
    plural_connector = plural_source.index(f" и {plural_actor}")
    assert any(
        start <= plural_connector < end
        for start, end in _relation_separator_spans(plural_source)
    )
    validate(plural_source, plural_source.rstrip("."))
    with pytest.raises(ValueError):
        validate(
            plural_source,
            plural_source.replace(f"{plural_actor} ", "", 1).rstrip("."),
        )

    singular_source = (
        "Суд проверил документ и "
        f"{singular_actor} по делам лицензирования {singular_governor} "
        "аннулировать лицензию."
    )
    singular_connector = singular_source.index(f" и {singular_actor}")
    assert any(
        start <= singular_connector < end
        for start, end in _relation_separator_spans(singular_source)
    )
    validate(singular_source, singular_source.rstrip("."))

    oblique_control = (
        "Суд проверил документ и "
        f"{oblique_actor} по делам лицензирования обязаны "
        "аннулировать лицензию."
    )
    oblique_connector = oblique_control.index(f" и {oblique_actor}")
    assert not any(
        start <= oblique_connector < end
        for start, end in _relation_separator_spans(oblique_control)
    )


_CLOSED_UK_IRREGULAR_LEFT_PREDICATE_CASES = (
    "Суд принял документ",
    "Комиссия приняла документ",
    "Суд вынес решение",
    "Гражданин ушёл из зала",
    "Суд принёс документ",
    "Суд счёл акт законным",
)


@pytest.mark.parametrize(
    "left_clause",
    _CLOSED_UK_IRREGULAR_LEFT_PREDICATE_CASES,
    ids=(
        "accepted-masculine",
        "accepted-feminine",
        "issued",
        "left",
        "brought",
        "considered",
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
def test_v3_closed_uk_irregular_left_predicate_proves_separator(
    left_clause: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"{left_clause} и прокуратура обязана аннулировать лицензию."
    )
    connector = source.index(" и прокуратура")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    unsafe = source.replace(" и прокуратура", " и", 1).rstrip(".")
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_UNSUPPORTED_SUBORDINATE_GROUND_SOURCE = (
    "За нарушение правил суд проверяет документ и после того, как жюри "
    "обсудит дело, прокуратура обязана аннулировать лицензию."
)
_CLOSED_UK_UNSUPPORTED_SUBORDINATE_GROUND_UNSAFE = (
    "За нарушение правил суд проверяет документ и после того, как жюри "
    "обсудит дело, обязана аннулировать лицензию"
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
def test_v3_closed_uk_ground_separator_finds_later_governing_predicate(
    validate: RelationValidator,
) -> None:
    source = _CLOSED_UK_UNSUPPORTED_SUBORDINATE_GROUND_SOURCE
    connector = source.index(" и после того")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, _CLOSED_UK_UNSUPPORTED_SUBORDINATE_GROUND_UNSAFE)


_CLOSED_UK_EXTERNAL_COLON_IDENTITY_CASES = (
    (
        "организация «Альфа»: "
        "(подать жалобу, нельзя исполнить решение) и санкция суда",
        "организация «Альфа», "
        "(подать жалобу, нельзя исполнить решение) и санкция суда",
    ),
    (
        "(подать жалобу, нельзя исполнить решение): "
        "организация «Альфа» и санкция суда",
        "(подать жалобу, нельзя исполнить решение), "
        "организация «Альфа» и санкция суда",
    ),
)


@pytest.mark.parametrize(
    ("condition", "changed_delimiter"),
    _CLOSED_UK_EXTERNAL_COLON_IDENTITY_CASES,
    ids=("identity-before-parentheses", "parentheses-before-identity"),
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
def test_v3_closed_uk_external_colon_next_to_identity_is_semantic(
    condition: str,
    changed_delimiter: str,
    validate: RelationValidator,
) -> None:
    source = _closed_uk_boolean_condition_surface(
        condition,
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        condition,
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        changed_delimiter,
        compact_ground=True,
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
def test_v3_closed_uk_colon_inside_quoted_identity_is_opaque(
    validate: RelationValidator,
) -> None:
    condition = (
        "уведомление организации «Альфа: Бета, Гамма» и санкция суда"
    )
    source = _closed_uk_boolean_condition_surface(
        condition,
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        condition,
        compact_ground=True,
    )
    validate(source, faithful)
    unsafe = _closed_uk_boolean_condition_surface(
        "уведомление организации «Альфа» Бета, Гамма и санкция суда",
        compact_ground=True,
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)


def _closed_uk_bridge_kinds(clause: str) -> tuple[str | None, ...]:
    records = _closed_uk_revocation_action_records(clause)
    return tuple(
        _closed_uk_action_record_bridge_kind(clause, left, right)
        for left, right in zip(records, records[1:])
    )


_CLOSED_UK_REPEATED_ROLE_INTERNAL_OR_CASES = (
    (
        "с разрешения прокурора или с разрешения губернатора полиция "
        "обязана изъять лицензию на оружие",
        "с разрешения прокурора и с разрешения губернатора полиция "
        "обязана изъять лицензию на оружие",
        "с разрешения прокурора полиция обязана изъять лицензию на оружие",
    ),
    (
        "по решению суда или по решению губернатора полиция обязана "
        "изъять лицензию на оружие",
        "по решению суда и по решению губернатора полиция обязана "
        "изъять лицензию на оружие",
        "по решению суда полиция обязана изъять лицензию на оружие",
    ),
)


@pytest.mark.parametrize(
    ("right_branch", "changed_internal_or", "removed_role"),
    _CLOSED_UK_REPEATED_ROLE_INTERNAL_OR_CASES,
    ids=("repeated-permission", "repeated-decision"),
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
def test_v3_closed_uk_repeated_role_or_cannot_outrank_outer_bridge(
    right_branch: str,
    changed_internal_or: str,
    removed_role: str,
    validate: RelationValidator,
) -> None:
    left_branch = "суд обязан аннулировать лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    validate(source, faithful)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        f"Либо {changed_internal_or}, либо {left_branch}",
        f"Либо {removed_role}, либо {left_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_DETACHED_DECISION_DISCOURSE_SOURCE = (
    _closed_uk_correlative_tree_surface(
        "И, согласно решению суда, полиция обязана аннулировать и изъять "
        "лицензию на оружие",
        compact_ground=False,
    )
)
_CLOSED_UK_DETACHED_DECISION_DISCOURSE_FAITHFUL = (
    _closed_uk_correlative_tree_surface(
        "Согласно решению суда полиция обязана аннулировать и изъять "
        "лицензию на оружие",
        compact_ground=True,
    )
)
_CLOSED_UK_TRUE_DECISION_ACTION_PAIR_SOURCE = (
    _closed_uk_correlative_tree_surface(
        "И согласно решению суда полиция обязана аннулировать лицензию на "
        "оружие, и прокуратура обязана изъять лицензию на оружие",
        compact_ground=False,
    )
)
_CLOSED_UK_TRUE_DECISION_ACTION_PAIR_FAITHFUL = (
    _closed_uk_correlative_tree_surface(
        "И прокуратура обязана изъять лицензию на оружие, и согласно "
        "решению суда полиция обязана аннулировать лицензию на оружие",
        compact_ground=True,
    )
)
_CLOSED_UK_DECISION_ACTOR_CORRELATIVE_SOURCE = (
    _closed_uk_correlative_tree_surface(
        "И полиция, и прокуратура согласно решению суда обязаны "
        "аннулировать и изъять лицензию на оружие",
        compact_ground=False,
    )
)
_CLOSED_UK_DECISION_ACTOR_CORRELATIVE_FAITHFUL = (
    _closed_uk_correlative_tree_surface(
        "И прокуратура, и полиция согласно решению суда обязаны "
        "аннулировать и изъять лицензию на оружие",
        compact_ground=True,
    )
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
def test_v3_closed_uk_detached_decision_and_has_no_pair_owner(
    validate: RelationValidator,
) -> None:
    source = _CLOSED_UK_DETACHED_DECISION_DISCOURSE_SOURCE
    records = _closed_uk_revocation_action_records(source)
    assert not _closed_uk_action_correlative_contracts(source, records)
    validate(source, _CLOSED_UK_DETACHED_DECISION_DISCOURSE_FAITHFUL)
    for unsafe in (
        _closed_uk_correlative_tree_surface(
            "Полиция обязана аннулировать и изъять лицензию на оружие",
            compact_ground=True,
        ),
        _closed_uk_correlative_tree_surface(
            "Согласно решению суда полиция обязана аннулировать лицензию "
            "на оружие",
            compact_ground=True,
        ),
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
def test_v3_closed_uk_decision_action_pair_keeps_true_markers(
    validate: RelationValidator,
) -> None:
    source = _CLOSED_UK_TRUE_DECISION_ACTION_PAIR_SOURCE
    records = _closed_uk_revocation_action_records(source)
    assert _closed_uk_action_correlative_contracts(source, records)
    validate(source, _CLOSED_UK_TRUE_DECISION_ACTION_PAIR_FAITHFUL)
    for unsafe in (
        _CLOSED_UK_TRUE_DECISION_ACTION_PAIR_FAITHFUL.replace(
            ", и согласно",
            ", либо согласно",
            1,
        ),
        _CLOSED_UK_TRUE_DECISION_ACTION_PAIR_FAITHFUL.replace(
            "согласно решению суда ",
            "",
            1,
        ),
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
def test_v3_closed_uk_decision_actor_correlative_is_not_action_pair(
    validate: RelationValidator,
) -> None:
    source = _CLOSED_UK_DECISION_ACTOR_CORRELATIVE_SOURCE
    records = _closed_uk_revocation_action_records(source)
    assert not _closed_uk_action_correlative_contracts(source, records)
    validate(source, _CLOSED_UK_DECISION_ACTOR_CORRELATIVE_FAITHFUL)
    unsafe = _CLOSED_UK_DECISION_ACTOR_CORRELATIVE_FAITHFUL.replace(
        "И прокуратура, и полиция",
        "Прокуратура",
        1,
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
def test_v3_closed_uk_means_is_not_a_weekday_scope(
    validate: RelationValidator,
) -> None:
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search(
        "при наличии средства связи"
    ) is None
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search("только по средам")
    left_branch = "суд обязан аннулировать лицензию на оружие"
    right_branch = (
        "при наличии средства связи полиция обязана изъять лицензию "
        "на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    validate(source, faithful)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        "Либо полиция обязана изъять лицензию на оружие, либо "
        f"{left_branch}",
        "Либо при наличии среды полиция обязана изъять лицензию на "
        f"оружие, либо {left_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_UNSUPPORTED_SIBLING_ACTION_SOURCE = (
    _closed_uk_correlative_tree_surface(
        "Суд обязан аннулировать лицензию на оружие, уведомить владельца "
        "и изъять лицензию на оружие",
        compact_ground=False,
    )
)
_CLOSED_UK_UNSUPPORTED_SIBLING_ACTION_FAITHFUL = (
    _closed_uk_correlative_tree_surface(
        "Суд обязан аннулировать лицензию на оружие, уведомить владельца "
        "и изъять лицензию на оружие",
        compact_ground=True,
    )
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
def test_v3_closed_uk_unsupported_sibling_action_is_fail_closed(
    validate: RelationValidator,
) -> None:
    source = _CLOSED_UK_UNSUPPORTED_SIBLING_ACTION_SOURCE
    faithful = _CLOSED_UK_UNSUPPORTED_SIBLING_ACTION_FAITHFUL
    validate(source, faithful)
    for unsafe in (
        faithful.replace(" уведомить владельца и", "", 1),
        faithful.replace("уведомить владельца", "уведомить прокурора", 1),
        faithful.replace("уведомить владельца", "проверить владельца", 1),
        _closed_uk_correlative_tree_surface(
            "Суд обязан аннулировать лицензию на оружие и изъять лицензию "
            "на оружие, а затем уведомить владельца",
            compact_ground=True,
        ),
    ):
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_OWNER_AWARE_SEPARATOR_CASES = (
    (
        "Суд проверил документ и прокуратура по материалам дела после "
        "уведомления сторон и получения заключения комиссии должна "
        "аннулировать лицензию.",
        " и прокуратура",
        (" и получения",),
        (
            "Суд проверил документ и по материалам дела после уведомления "
            "сторон и получения заключения комиссии должна аннулировать "
            "лицензию",
        ),
    ),
    (
        "Суд проверил документ и при условии решения суда и санкции "
        "прокурора прокуратура обязана аннулировать лицензию.",
        " и при условии",
        (" и санкции",),
        (
            "Суд проверил документ и при условии решения суда и санкции "
            "прокурора обязана аннулировать лицензию",
        ),
    ),
    (
        "Суд проверил документ и прокуратура (после уведомления сторон и "
        "получения заключения) обязана аннулировать лицензию.",
        " и прокуратура",
        (" и получения",),
        (
            "Суд проверил документ и (после уведомления сторон и получения "
            "заключения) обязана аннулировать лицензию",
        ),
    ),
    (
        "Суд проверил документ и прокуратура лицензию и пропуск аннулирует.",
        " и прокуратура",
        (" и пропуск",),
        (
            "Суд проверил документ и лицензию и пропуск аннулирует",
            "Суд проверил документ и прокуратура лицензию аннулирует",
        ),
    ),
    (
        "Суд проверил документ, а прокурор и губернатор лицензию "
        "аннулируют.",
        ", а прокурор",
        (" и губернатор",),
        (
            "Суд проверил документ, а губернатор лицензию аннулирует",
            "Суд проверил документ, а прокурор и губернатор аннулируют",
        ),
    ),
    (
        "Суд проверил документ, а прокурор и губернатор после уведомления "
        "сторон обязаны аннулировать лицензию.",
        ", а прокурор",
        (" и губернатор",),
        (
            "Суд проверил документ, а губернатор после уведомления сторон "
            "обязан аннулировать лицензию",
        ),
    ),
    (
        "Суд проверил документ и при условии решения суда прокуратура и "
        "губернатор обязаны аннулировать лицензию.",
        " и при условии",
        (" и губернатор",),
        (
            "Суд проверил документ и при условии решения суда губернатор "
            "обязан аннулировать лицензию",
            "Суд проверил документ и при условии решения суда прокуратура "
            "обязана аннулировать лицензию",
        ),
    ),
    (
        "Суд проверил документ и (после уведомления сторон) прокуратура и "
        "губернатор обязаны аннулировать лицензию.",
        " и (после",
        (" и губернатор",),
        (
            "Суд проверил документ и (после уведомления сторон) губернатор "
            "обязан аннулировать лицензию",
            "Суд проверил документ и (после уведомления сторон) прокуратура "
            "обязана аннулировать лицензию",
        ),
    ),
    (
        "Суд проверил документ и лицензию аннулировала прокуратура.",
        " и лицензию",
        (),
        (
            "Суд проверил документ и лицензию аннулировала",
        ),
    ),
)


@pytest.mark.parametrize(
    ("source", "outer_needle", "inner_needles", "unsafe_outputs"),
    _CLOSED_UK_OWNER_AWARE_SEPARATOR_CASES,
    ids=(
        "compound-temporal-role",
        "leading-condition-role",
        "parenthesized-semantic-role",
        "sov-patient-list",
        "actor-group-with-sov-patient",
        "actor-group-with-trailing-adjunct",
        "leading-condition-before-actor-group",
        "leading-parenthetical-before-actor-group",
        "bare-and-object-first-postposed-actor",
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
def test_v3_closed_uk_separator_layout_owns_nested_connectors(
    source: str,
    outer_needle: str,
    inner_needles: tuple[str, ...],
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    spans = _relation_separator_spans(source)
    outer = source.index(outer_needle)
    assert any(start <= outer < end for start, end in spans)
    for needle in inner_needles:
        inner = source.index(needle, outer + len(outer_needle))
        assert not any(start <= inner < end for start, end in spans)
    validate(source, source.rstrip("."))
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_RIGHT_FINITE_BOUNDARY_CASES = (
    (
        "Суд проверил документ и прокуратура вынесла решение об "
        "аннулировании лицензии.",
        "Суд проверил документ и вынесла решение об аннулировании лицензии",
    ),
    (
        "Суд проверил документ и прокуратура взяла лицензию.",
        "Суд проверил документ и взяла лицензию",
    ),
    (
        "Суд проверил документ и прокуратура дала разрешение.",
        "Суд проверил документ и дала разрешение",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    _CLOSED_UK_RIGHT_FINITE_BOUNDARY_CASES,
    ids=(
        "issued-decision",
        "took-license",
        "gave-permission",
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
def test_v3_closed_uk_right_finite_predicate_keeps_its_actor(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    connector = source.index(" и прокуратура")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_SEPARATOR_ONLY_RIGHT_FINITE_CASES = (
    (
        "Суд проверил документ и прокуратура признаёт лицензию "
        "недействительной.",
        "recognizes-license",
    ),
    (
        "Суд проверил документ и прокуратура создаёт реестр лицензий.",
        "creates-register",
    ),
    (
        "Суд проверил документ и прокуратура издаёт постановление.",
        "issues-order",
    ),
)


@pytest.mark.parametrize(
    ("source", "case_id"),
    _CLOSED_UK_SEPARATOR_ONLY_RIGHT_FINITE_CASES,
    ids=tuple(
        case_id
        for _source, case_id in _CLOSED_UK_SEPARATOR_ONLY_RIGHT_FINITE_CASES
    ),
)
def test_v3_closed_uk_legal_finite_form_proves_only_separator_boundary(
    source: str,
    case_id: str,
) -> None:
    del case_id
    connector = source.index(" и прокуратура")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )


_CLOSED_UK_FAR_PREDICATE_BORROW_CASES = (
    (
        "Суд аннулирует лицензию и служебный пропуск, либо прокуратура "
        "изымает оружие.",
        " и служебный пропуск",
        "Суд аннулирует лицензию, либо прокуратура изымает оружие",
    ),
    (
        "Суд аннулирует лицензию и пропуск, при этом прокуратура изымает "
        "оружие.",
        " и пропуск",
        "Суд аннулирует лицензию, при этом прокуратура изымает оружие",
    ),
    (
        "Суд аннулирует лицензию и пропуск, однако прокуратура изымает "
        "оружие.",
        " и пропуск",
        "Суд аннулирует лицензию, однако прокуратура изымает оружие",
    ),
)


@pytest.mark.parametrize(
    ("source", "patient_connector", "unsafe"),
    _CLOSED_UK_FAR_PREDICATE_BORROW_CASES,
    ids=("later-alternative", "later-explicit", "later-soft"),
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
def test_v3_closed_uk_patient_connector_cannot_borrow_later_predicate(
    source: str,
    patient_connector: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    connector = source.index(patient_connector)
    assert not any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize("nominal_tail", ("казны", "сосны", "весны"))
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
def test_v3_closed_uk_short_predicative_lexicon_keeps_nominal_ellipsis(
    nominal_tail: str,
    validate: RelationValidator,
) -> None:
    source = _closed_uk_boolean_condition_surface(
        f"нарушены правила хранения или правила {nominal_tail}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"нарушены правила хранения или {nominal_tail}",
        compact_ground=True,
    )
    validate(source, faithful)


def test_v3_closed_uk_shared_head_keeps_nominal_kazny_and_real_predicate(
) -> None:
    nominal_parts = (
        "нарушены правила хранения",
        "правила казны",
    )
    assert _relation_condition_shared_head_parts(nominal_parts) == (
        "нарушены правила хранения",
        "нарушены правила казны",
    )
    finite_parts = (
        "нарушены правила хранения",
        "правила казны издаёт прокуратура",
    )
    assert _relation_condition_shared_head_parts(finite_parts) == finite_parts


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
def test_v3_closed_uk_kazny_shared_head_is_exact_end_to_end(
    validate: RelationValidator,
) -> None:
    nominal_source = _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или правила казны",
        compact_ground=False,
    )
    nominal_faithful = _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или казны",
        compact_ground=True,
    )
    validate(nominal_source, nominal_faithful)

    finite_source = _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или правила казны издаёт прокуратура",
        compact_ground=False,
    )
    finite_faithful = _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или правила казны издаёт прокуратура",
        compact_ground=True,
    )
    validate(finite_source, finite_faithful)
    unsafe = _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или казны",
        compact_ground=True,
    )
    with pytest.raises(ValueError):
        validate(finite_source, unsafe)


_CLOSED_UK_CROSS_OWNER_ROLE_CASES = (
    (
        "суд обязан аннулировать оружейную или служебную лицензию с "
        "разрешения суда",
        "с разрешения прокурора полиция обязана изъять лицензию на оружие",
        "permission",
    ),
    (
        "суд обязан аннулировать оружейную или служебную лицензию по "
        "решению суда",
        "по решению прокурора полиция обязана изъять лицензию на оружие",
        "decision",
    ),
    (
        "суд обязан аннулировать оружейную или служебную лицензию при "
        "санкции суда",
        "при санкции прокурора полиция обязана изъять лицензию на оружие",
        "authorization",
    ),
)


@pytest.mark.parametrize(
    ("left_branch", "right_branch", "role_id"),
    _CLOSED_UK_CROSS_OWNER_ROLE_CASES,
    ids=tuple(case[2] for case in _CLOSED_UK_CROSS_OWNER_ROLE_CASES),
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
def test_v3_closed_uk_cross_owner_roles_do_not_hide_action_bridge(
    left_branch: str,
    right_branch: str,
    role_id: str,
    validate: RelationValidator,
) -> None:
    del role_id
    source = _closed_uk_correlative_tree_surface(
        f"{left_branch}, и {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{right_branch}, и {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    validate(source, faithful)
    swapped_left = left_branch.replace("суда", "__authority__", 1).replace(
        "прокурора",
        "суда",
        1,
    ).replace("__authority__", "прокурора", 1)
    swapped_right = right_branch.replace(
        "суда",
        "__authority__",
        1,
    ).replace(
        "прокурора",
        "суда",
        1,
    ).replace("__authority__", "прокурора", 1)
    for expression in (
        f"{left_branch}, либо {right_branch}",
        f"{swapped_left}, и {swapped_right}",
        f"{left_branch}, и "
        + right_branch.replace(
            "с разрешения прокурора ",
            "",
            1,
        ).replace(
            "по решению прокурора ",
            "",
            1,
        ).replace(
            "при санкции прокурора ",
            "",
            1,
        ),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_SAME_RIGHT_ROLE_FORMULA_CASES = (
    (
        "с разрешения суда, а также с разрешения прокурора полиция обязана "
        "изъять лицензию на оружие",
        "с разрешения суда, или с разрешения прокурора полиция обязана "
        "изъять лицензию на оружие",
        "с разрешения суда полиция обязана изъять лицензию на оружие",
        "с разрешения прокурора, а также с разрешения суда полиция обязана "
        "изъять лицензию на оружие",
        "permission-comma-also",
    ),
    (
        "по решению суда, или по решению прокурора полиция обязана изъять "
        "лицензию на оружие",
        "по решению суда, и по решению прокурора полиция обязана изъять "
        "лицензию на оружие",
        "по решению суда полиция обязана изъять лицензию на оружие",
        "по решению прокурора, или по решению суда полиция обязана изъять "
        "лицензию на оружие",
        "decision-comma-or",
    ),
    (
        "при санкции суда, а также при санкции прокурора полиция обязана "
        "изъять лицензию на оружие",
        "при санкции суда, или при санкции прокурора полиция обязана изъять "
        "лицензию на оружие",
        "при санкции суда полиция обязана изъять лицензию на оружие",
        "при санкции прокурора, а также при санкции суда полиция обязана "
        "изъять лицензию на оружие",
        "authorization-comma-also",
    ),
    (
        "только по пятницам, а также субботам полиция обязана изъять "
        "лицензию на оружие",
        "только по пятницам, или субботам полиция обязана изъять лицензию "
        "на оружие",
        "только по пятницам полиция обязана изъять лицензию на оружие",
        "только по субботам, а также пятницам полиция обязана изъять "
        "лицензию на оружие",
        "weekday-bare-continuation",
    ),
    (
        "при условии уведомления суда, или при условии санкции прокурора "
        "полиция обязана изъять лицензию на оружие",
        "при условии уведомления суда, и при условии санкции прокурора "
        "полиция обязана изъять лицензию на оружие",
        "при условии уведомления суда полиция обязана изъять лицензию на "
        "оружие",
        "при условии санкции прокурора, или при условии уведомления суда "
        "полиция обязана изъять лицензию на оружие",
        "condition-comma-or",
    ),
    (
        "по просьбе суда, или по поручению прокурора полиция обязана изъять "
        "лицензию на оружие",
        "по просьбе суда, и по поручению прокурора полиция обязана изъять "
        "лицензию на оружие",
        "по просьбе суда полиция обязана изъять лицензию на оружие",
        "по поручению прокурора, или по просьбе суда полиция обязана изъять "
        "лицензию на оружие",
        "oblique-comma-or",
    ),
)


@pytest.mark.parametrize(
    (
        "right_branch",
        "changed_formula",
        "dropped_role",
        "reordered_formula",
        "role_id",
    ),
    _CLOSED_UK_SAME_RIGHT_ROLE_FORMULA_CASES,
    ids=tuple(case[4] for case in _CLOSED_UK_SAME_RIGHT_ROLE_FORMULA_CASES),
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
def test_v3_closed_uk_same_branch_role_formula_cannot_outrank_outer_bridge(
    right_branch: str,
    changed_formula: str,
    dropped_role: str,
    reordered_formula: str,
    role_id: str,
    validate: RelationValidator,
) -> None:
    del role_id
    left_branch = "суд обязан аннулировать лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    validate(source, faithful)
    reordered = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {reordered_formula}",
        compact_ground=True,
    )
    validate(source, reordered)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        f"Либо {changed_formula}, либо {left_branch}",
        f"Либо {dropped_role}, либо {left_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
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
def test_v3_closed_uk_each_action_keeps_its_weekday_scope(
    validate: RelationValidator,
) -> None:
    left_branch = (
        "суд обязан аннулировать лицензию на оружие только по пятницам"
    )
    right_branch = (
        "в субботу полиция обязана изъять лицензию на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        f"{left_branch}, и {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{right_branch}, и {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    validate(source, faithful)
    for expression in (
        "суд обязан аннулировать лицензию на оружие, и в субботу полиция "
        "обязана изъять лицензию на оружие",
        "суд обязан аннулировать лицензию на оружие только по пятницам, и "
        "полиция обязана изъять лицензию на оружие",
        "суд обязан аннулировать лицензию на оружие, и только по пятницам "
        "в субботу полиция обязана изъять лицензию на оружие",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_v3_closed_uk_bare_weekday_is_only_a_proved_list_continuation() -> None:
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search("только по пятницам")
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search("в субботу")
    for surface in ("Среда", "Суббота"):
        assert not _closed_uk_weekday_role_families(
            surface,
            range_start=0,
            range_end=len(surface),
        )
    anchored_list = "только по пятницам и субботам"
    assert _closed_uk_weekday_role_families(
        anchored_list,
        range_start=0,
        range_end=len(anchored_list),
    )


_CLOSED_UK_LEADING_TYPED_ROLE_PREFIXES = (
    (
        "с разрешения суда и с разрешения прокурора",
        "с разрешения суда или с разрешения прокурора",
        "permission",
    ),
    (
        "по решению суда и по решению прокурора",
        "по решению суда или по решению прокурора",
        "decision",
    ),
    (
        "при санкции суда и при санкции прокурора",
        "при санкции суда или при санкции прокурора",
        "authorization",
    ),
    (
        "только по пятницам и субботам",
        "только по пятницам или субботам",
        "weekday",
    ),
    (
        "при условии уведомления и санкции суда",
        "при условии уведомления или санкции суда",
        "condition",
    ),
)


@pytest.mark.parametrize(
    ("prefix", "changed_prefix", "prefix_id"),
    _CLOSED_UK_LEADING_TYPED_ROLE_PREFIXES,
    ids=tuple(case[2] for case in _CLOSED_UK_LEADING_TYPED_ROLE_PREFIXES),
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
def test_v3_closed_uk_pre_first_role_does_not_hide_explicit_action_pair(
    prefix: str,
    changed_prefix: str,
    prefix_id: str,
    validate: RelationValidator,
) -> None:
    del prefix_id
    first_branch = (
        f"{prefix} полиция обязана аннулировать лицензию на оружие"
    )
    second_branch = "прокуратура обязана изъять лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"И {first_branch}, и {second_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"И {second_branch}, и {first_branch}",
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert _closed_uk_action_correlative_contracts(source, records)
    validate(source, faithful)
    for expression in (
        f"{first_branch}, и {second_branch}",
        f"И {first_branch}, либо {second_branch}",
        "И "
        + first_branch.replace(prefix, changed_prefix, 1)
        + f", и {second_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_LEADING_DISCOURSE_TYPED_ROLE_CASES = (
    (
        "И только по пятницам и субботам полиция обязана аннулировать и "
        "изъять лицензию на оружие",
        "Только по пятницам и субботам полиция обязана аннулировать и "
        "изъять лицензию на оружие",
        "weekday",
    ),
    (
        "И при условии уведомления и санкции суда полиция обязана "
        "аннулировать и изъять лицензию на оружие",
        "При условии уведомления и санкции суда полиция обязана "
        "аннулировать и изъять лицензию на оружие",
        "condition",
    ),
)


@pytest.mark.parametrize(
    ("source_expression", "faithful_expression", "role_id"),
    _CLOSED_UK_LEADING_DISCOURSE_TYPED_ROLE_CASES,
    ids=tuple(
        case[2] for case in _CLOSED_UK_LEADING_DISCOURSE_TYPED_ROLE_CASES
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
def test_v3_closed_uk_typed_role_internal_and_does_not_own_discourse_and(
    source_expression: str,
    faithful_expression: str,
    role_id: str,
    validate: RelationValidator,
) -> None:
    del role_id
    source = _closed_uk_correlative_tree_surface(
        source_expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        faithful_expression,
        compact_ground=True,
    )
    validate(source, faithful)
    unsafe = faithful.replace(" и изъять", "", 1)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_NESTED_AFTER_DISCOURSE_CASES = (
    (
        "И либо суд обязан аннулировать лицензию на оружие, либо полиция "
        "обязана изъять лицензию на оружие",
        "Либо полиция обязана изъять лицензию на оружие, либо суд обязан "
        "аннулировать лицензию на оружие",
        "Либо полиция обязана изъять лицензию на оружие, и суд обязан "
        "аннулировать лицензию на оружие",
        ("alternative",),
        "nested-either",
    ),
    (
        "И как суд обязан аннулировать лицензию на оружие, так и полиция "
        "обязана изъять лицензию на оружие",
        "Как полиция обязана изъять лицензию на оружие, так и суд обязан "
        "аннулировать лицензию на оружие",
        "Как полиция обязана изъять лицензию на оружие, либо суд обязан "
        "аннулировать лицензию на оружие",
        ("conjunctive",),
        "nested-both",
    ),
    (
        "И, либо суд обязан аннулировать лицензию на оружие, либо полиция "
        "обязана изъять лицензию на оружие",
        "Либо полиция обязана изъять лицензию на оружие, либо суд обязан "
        "аннулировать лицензию на оружие",
        "Либо полиция обязана изъять лицензию на оружие, и суд обязан "
        "аннулировать лицензию на оружие",
        ("alternative",),
        "punctuated-nested-either",
    ),
    (
        "И — как суд обязан аннулировать лицензию на оружие, так и полиция "
        "обязана изъять лицензию на оружие",
        "Как полиция обязана изъять лицензию на оружие, так и суд обязан "
        "аннулировать лицензию на оружие",
        "Как полиция обязана изъять лицензию на оружие, либо суд обязан "
        "аннулировать лицензию на оружие",
        ("conjunctive",),
        "punctuated-nested-both",
    ),
)


@pytest.mark.parametrize(
    (
        "source_expression",
        "faithful_expression",
        "unsafe_expression",
        "expected_bridge_kinds",
        "case_id",
    ),
    _CLOSED_UK_NESTED_AFTER_DISCOURSE_CASES,
    ids=tuple(case[4] for case in _CLOSED_UK_NESTED_AFTER_DISCOURSE_CASES),
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
def test_v3_closed_uk_discourse_and_preserves_nested_correlative(
    source_expression: str,
    faithful_expression: str,
    unsafe_expression: str,
    expected_bridge_kinds: tuple[str, ...],
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        source_expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        faithful_expression,
        compact_ground=True,
    )
    unsafe = _closed_uk_correlative_tree_surface(
        unsafe_expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert _closed_uk_bridge_kinds(source) == expected_bridge_kinds
    assert _closed_uk_action_correlative_contracts(source, records)
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
def test_v3_closed_uk_outer_bridge_keeps_conditioned_grouped_actor(
    validate: RelationValidator,
) -> None:
    left_branch = "суд обязан аннулировать лицензию на оружие"
    right_branch = (
        "при задержании прокурор и губернатор обязаны изъять лицензию "
        "на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    validate(source, faithful)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        f"Либо {left_branch}, либо прокурор и губернатор обязаны изъять "
        "лицензию на оружие",
        f"Либо {left_branch}, либо при задержании губернатор обязан изъять "
        "лицензию на оружие",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_v3_closed_uk_nary_layout_has_three_complete_action_branches() -> None:
    source = _CLOSED_UK_NARY_BRIDGE_CASES[0][0]
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 3
    assert _closed_uk_bridge_kinds(source) == (
        "conjunctive",
        "conjunctive",
    )
    correlatives = _closed_uk_action_correlative_contracts(source, records)
    assert len(correlatives) == 1
    assert correlatives[0][2] == ((0,), (1,), (2,))


_CLOSED_UK_NARY_SHARED_PATIENT_SOURCE = _closed_uk_correlative_tree_surface(
    "И суд обязан аннулировать, и полиция обязана изъять, и прокуратура "
    "обязана приостановить лицензию на оружие",
    compact_ground=False,
)
_CLOSED_UK_NARY_SHARED_PATIENT_FAITHFUL = _closed_uk_correlative_tree_surface(
    "И прокуратура обязана приостановить, и полиция обязана изъять, и суд "
    "обязан аннулировать лицензию на оружие",
    compact_ground=True,
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
def test_v3_closed_uk_nary_actions_keep_one_shared_trailing_patient(
    validate: RelationValidator,
) -> None:
    source = _CLOSED_UK_NARY_SHARED_PATIENT_SOURCE
    faithful = _CLOSED_UK_NARY_SHARED_PATIENT_FAITHFUL
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 3
    assert _closed_uk_bridge_kinds(source) == (
        "conjunctive",
        "conjunctive",
    )
    correlatives = _closed_uk_action_correlative_contracts(source, records)
    assert len(correlatives) == 1
    assert correlatives[0][2] == ((0,), (1,), (2,))
    validate(source, faithful)
    for unsafe in (
        faithful.replace("и полиция обязана изъять, ", "", 1),
        faithful.replace("прокуратура ", "", 1),
        faithful.replace(" лицензию на оружие", "", 1),
        faithful.replace("И прокуратура", "Прокуратура", 1),
        faithful.replace(", и полиция", ", либо полиция", 1),
        faithful.replace(", и полиция", ", а затем полиция", 1),
        faithful.replace(", и полиция", ", однако полиция", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_NON_BROADCAST_SHARED_PATIENT_EXPRESSIONS = (
    (
        "суд обязан аннулировать, и полиция обязана изъять, и прокуратура "
        "обязана приостановить лицензию на оружие",
        "plain-and-without-correlative",
    ),
    (
        "И суд обязан аннулировать, и полиция обязана изъять, либо "
        "прокуратура обязана приостановить лицензию на оружие",
        "mixed-and-or",
    ),
    (
        "И суд обязан аннулировать, и полиция обязана изъять, а затем "
        "прокуратура обязана приостановить лицензию на оружие",
        "and-then-sequence",
    ),
    (
        "И суд обязан аннулировать, и полиция обязана изъять, однако "
        "прокуратура обязана приостановить лицензию на оружие",
        "and-then-contrast",
    ),
)


@pytest.mark.parametrize(
    ("expression", "case_id"),
    _CLOSED_UK_NON_BROADCAST_SHARED_PATIENT_EXPRESSIONS,
    ids=tuple(
        case_id
        for _expression, case_id
        in _CLOSED_UK_NON_BROADCAST_SHARED_PATIENT_EXPRESSIONS
    ),
)
def test_v3_closed_uk_shared_patient_broadcast_requires_pure_correlative_and(
    expression: str,
    case_id: str,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 3
    correlatives = _closed_uk_action_correlative_contracts(source, records)
    assert all(
        contract[2] != ((0,), (1,), (2,))
        for contract in correlatives
    )


_CLOSED_UK_GENITIVE_AGREEMENT_CASES = (
    (
        "комиссия прокуратуры",
        "обязана",
        "обязаны",
        "singular-head",
    ),
    (
        "комиссии прокуратуры",
        "обязаны",
        "обязана",
        "plural-head",
    ),
)


@pytest.mark.parametrize(
    ("actor", "governor", "wrong_governor", "case_id"),
    _CLOSED_UK_GENITIVE_AGREEMENT_CASES,
    ids=tuple(case[3] for case in _CLOSED_UK_GENITIVE_AGREEMENT_CASES),
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
def test_v3_closed_uk_subject_agreement_uses_grammatical_head(
    actor: str,
    governor: str,
    wrong_governor: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = (
        f"Суд проверил документ и {actor} {governor} аннулировать лицензию."
    )
    connector = source.index(f" и {actor}")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    missing_actor = source.replace(f"{actor} ", "", 1).rstrip(".")
    with pytest.raises(ValueError):
        validate(source, missing_actor)
    wrong_agreement = source.replace(governor, wrong_governor, 1)
    wrong_connector = wrong_agreement.index(f" и {actor}")
    assert not any(
        start <= wrong_connector < end
        for start, end in _relation_separator_spans(wrong_agreement)
    )


_CLOSED_UK_PREPOSITIONAL_ACTOR_AGREEMENT_CASES = (
    (
        "комиссия по лицензированию и контролю",
        " и контролю",
        "обязана",
        "обязаны",
        "singular-and-modifier",
    ),
    (
        "комиссия по лицензированию или надзору",
        " или надзору",
        "обязана",
        "обязаны",
        "singular-or-modifier",
    ),
    (
        "комиссии по лицензированию и контролю",
        " и контролю",
        "обязаны",
        "обязана",
        "plural-and-modifier",
    ),
    (
        "комиссии по лицензированию или надзору",
        " или надзору",
        "обязаны",
        "обязана",
        "plural-or-modifier",
    ),
)


@pytest.mark.parametrize(
    ("actor", "inner_needle", "governor", "wrong_governor", "case_id"),
    _CLOSED_UK_PREPOSITIONAL_ACTOR_AGREEMENT_CASES,
    ids=tuple(
        case[4] for case in _CLOSED_UK_PREPOSITIONAL_ACTOR_AGREEMENT_CASES
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
def test_v3_closed_uk_agreement_uses_head_before_prepositional_modifier(
    actor: str,
    inner_needle: str,
    governor: str,
    wrong_governor: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = (
        f"Суд проверил документ и {actor} {governor} аннулировать лицензию."
    )
    spans = _relation_separator_spans(source)
    outer = source.index(f" и {actor}")
    inner = source.index(inner_needle, outer + 1)
    assert any(start <= outer < end for start, end in spans)
    assert not any(start <= inner < end for start, end in spans)
    validate(source, source.rstrip("."))
    missing_actor = source.replace(f"{actor} ", "", 1).rstrip(".")
    with pytest.raises(ValueError):
        validate(source, missing_actor)
    wrong_agreement = source.replace(governor, wrong_governor, 1)
    wrong_outer = wrong_agreement.index(f" и {actor}")
    assert not any(
        start <= wrong_outer < end
        for start, end in _relation_separator_spans(wrong_agreement)
    )


def test_v3_closed_uk_weekday_lexicon_rejects_lookalike_nouns() -> None:
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search("по субботам")
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search("в воскресенье")
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search("субботник") is None
    assert _CLOSED_UK_WEEKDAY_SCOPE_RE.search("воскресение документа") is None


_CLOSED_UK_LEFT_SAME_ROLE_FORMULA_CASES = (
    (
        "с разрешения суда, а также с разрешения прокурора",
        "с разрешения суда, или с разрешения прокурора",
        "с разрешения суда",
        "left-permission",
    ),
    (
        "при условии уведомления суда, или при условии санкции прокурора",
        "при условии уведомления суда, и при условии санкции прокурора",
        "при условии уведомления суда",
        "left-condition",
    ),
    (
        "по просьбе суда, или по поручению прокурора",
        "по просьбе суда, и по поручению прокурора",
        "по просьбе суда",
        "left-oblique",
    ),
)


@pytest.mark.parametrize(
    ("role_formula", "changed_formula", "dropped_formula", "case_id"),
    _CLOSED_UK_LEFT_SAME_ROLE_FORMULA_CASES,
    ids=tuple(case[3] for case in _CLOSED_UK_LEFT_SAME_ROLE_FORMULA_CASES),
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
def test_v3_closed_uk_left_role_formula_cannot_hide_outer_action_bridge(
    role_formula: str,
    changed_formula: str,
    dropped_formula: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    action = "суд обязан аннулировать лицензию на оружие"
    left_branch = f"{action} {role_formula}"
    right_branch = "полиция обязана изъять лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"{left_branch}, и {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{right_branch}, и {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    validate(source, faithful)
    for expression in (
        f"{left_branch}, либо {right_branch}",
        f"{action} {changed_formula}, и {right_branch}",
        f"{action} {dropped_formula}, и {right_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "outer_operator",
    ("или", "либо"),
    ids=("or", "either"),
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
def test_v3_closed_uk_cross_action_authorizations_keep_outer_or_bridge(
    outer_operator: str,
    validate: RelationValidator,
) -> None:
    left_branch = (
        "суд обязан аннулировать лицензию на оружие при санкции суда"
    )
    right_branch = (
        "при санкции прокурора полиция обязана изъять лицензию на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        f"{left_branch}, {outer_operator} {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{right_branch}, {outer_operator} {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    validate(source, faithful)
    for expression in (
        f"{left_branch}, и {right_branch}",
        "суд обязан аннулировать лицензию на оружие, "
        f"{outer_operator} {right_branch}",
        f"{left_branch}, {outer_operator} "
        "при санкции губернатора полиция обязана изъять лицензию на оружие",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_AUTHORIZATION_GROUP_CASES = (
    (
        "при санкции суда или при санкции прокурора",
        "при санкции прокурора или при санкции суда",
        "при санкции суда и при санкции прокурора",
        "при санкции суда",
        "при санкции суда или при санкции губернатора",
        "two-authorities",
    ),
    (
        "при санкции суда или при санкции прокурора или при санкции "
        "губернатора",
        "при санкции губернатора или при санкции суда или при санкции "
        "прокурора",
        "при санкции суда и при санкции прокурора или при санкции "
        "губернатора",
        "при санкции суда или при санкции губернатора",
        "при санкции суда или при санкции прокурора или при санкции мэра",
        "three-authorities",
    ),
)


@pytest.mark.parametrize(
    (
        "authorization_group",
        "reordered_group",
        "changed_operator_group",
        "dropped_authority_group",
        "changed_authority_group",
        "case_id",
    ),
    _CLOSED_UK_AUTHORIZATION_GROUP_CASES,
    ids=tuple(case[5] for case in _CLOSED_UK_AUTHORIZATION_GROUP_CASES),
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
def test_v3_closed_uk_authorization_or_group_is_complete_and_commutative(
    authorization_group: str,
    reordered_group: str,
    changed_operator_group: str,
    dropped_authority_group: str,
    changed_authority_group: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left_branch = "суд обязан аннулировать лицензию на оружие"
    right_action = "полиция обязана изъять лицензию на оружие"
    right_branch = f"{authorization_group} {right_action}"
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {reordered_group} {right_action}, либо {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    validate(source, faithful)
    for group in (
        changed_operator_group,
        dropped_authority_group,
        changed_authority_group,
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            f"Либо {group} {right_action}, либо {left_branch}",
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_v3_closed_uk_bare_sreda_before_modal_is_not_a_weekday_role() -> None:
    clause = "Среда обязана аннулировать лицензию на оружие"
    modal_start = clause.index("обязана")
    assert not _closed_uk_weekday_role_families(
        clause,
        range_start=0,
        range_end=modal_start,
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
def test_v3_closed_uk_sreda_actor_survives_outer_action_pair(
    validate: RelationValidator,
) -> None:
    left_branch = "Среда обязана аннулировать лицензию на оружие"
    right_branch = "полиция обязана изъять лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"Либо {left_branch}, либо {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"Либо {right_branch}, либо {left_branch}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    validate(source, faithful)
    for expression in (
        f"Либо {left_branch}, и {right_branch}",
        "Либо обязана аннулировать лицензию на оружие, либо "
        f"{right_branch}",
        "Либо Суд обязан аннулировать лицензию на оружие, либо "
        f"{right_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
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
def test_v3_closed_uk_coordinated_decision_authority_is_one_owned_role(
    validate: RelationValidator,
) -> None:
    action = "суд обязан аннулировать лицензию на оружие"
    authority = "по совместному решению суда и прокуратуры"
    reordered_authority = "по совместному решению прокуратуры и суда"
    left_branch = f"{action} {authority}"
    right_branch = "полиция обязана изъять лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"{left_branch}, и {right_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{right_branch}, и {action} {reordered_authority}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    validate(source, faithful)
    for expression in (
        f"{left_branch}, либо {right_branch}",
        f"{action} по совместному решению суда или прокуратуры, и "
        f"{right_branch}",
        f"{action} по совместному решению суда, и {right_branch}",
        f"{action} по совместному решению суда и губернатора, и "
        f"{right_branch}",
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_CORRELATIVE_THEN_SEQUENCE_SOURCE = (
    _closed_uk_correlative_tree_surface(
        "И суд обязан аннулировать лицензию на оружие, и полиция обязана "
        "изъять лицензию на оружие, а затем прокуратура обязана "
        "приостановить лицензию на оружие",
        compact_ground=False,
    )
)
_CLOSED_UK_CORRELATIVE_THEN_SEQUENCE_FAITHFUL = (
    _closed_uk_correlative_tree_surface(
        "И полиция обязана изъять лицензию на оружие, и суд обязан "
        "аннулировать лицензию на оружие, а затем прокуратура обязана "
        "приостановить лицензию на оружие",
        compact_ground=True,
    )
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
def test_v3_closed_uk_correlative_pair_stops_before_sequence_sibling(
    validate: RelationValidator,
) -> None:
    source = _CLOSED_UK_CORRELATIVE_THEN_SEQUENCE_SOURCE
    faithful = _CLOSED_UK_CORRELATIVE_THEN_SEQUENCE_FAITHFUL
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 3
    assert _closed_uk_bridge_kinds(source) == (
        "conjunctive",
        "sequence",
    )
    correlatives = _closed_uk_action_correlative_contracts(source, records)
    assert len(correlatives) == 1
    assert correlatives[0][2] == ((0,), (1,))
    validate(source, faithful)
    for unsafe in (
        faithful.replace(", а затем ", ", и ", 1),
        faithful.replace(
            ", а затем прокуратура обязана приостановить лицензию на оружие",
            "",
            1,
        ),
        _closed_uk_correlative_tree_surface(
            "И полиция обязана изъять лицензию на оружие, и прокуратура "
            "обязана приостановить лицензию на оружие, а затем суд обязан "
            "аннулировать лицензию на оружие",
            compact_ground=True,
        ),
    ):
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_ORDERED_ACTION_CONNECTOR_DIALECTS = (
    ("при этом", "причём", "continuation", "at-that-to-moreover"),
    ("причём", "кроме того", "continuation", "moreover-to-besides"),
    ("кроме того", "при этом", "continuation", "besides-to-at-that"),
    ("вместе с тем", "тогда как", "contrast", "nevertheless-to-whereas"),
    ("тогда как", "однако", "contrast", "whereas-to-however"),
    ("однако", "зато", "contrast", "however-to-but"),
    ("зато", "вместе с тем", "contrast", "but-to-nevertheless"),
    ("далее", "после чего", "sequence", "next-to-after-which"),
    ("после чего", "далее", "sequence", "after-which-to-next"),
)


@pytest.mark.parametrize(
    ("connector", "equivalent", "bridge_kind", "case_id"),
    _CLOSED_UK_ORDERED_ACTION_CONNECTOR_DIALECTS,
    ids=tuple(case[3] for case in _CLOSED_UK_ORDERED_ACTION_CONNECTOR_DIALECTS),
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
def test_v3_closed_uk_ordered_action_connector_dialect_keeps_operator_class(
    connector: str,
    equivalent: str,
    bridge_kind: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left = "суд округа Блейн обязан аннулировать лицензию на оружие"
    right = "полиция должна изъять лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"{left}, {connector} {right}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{left}, {equivalent} {right}",
        compact_ground=True,
    )
    assert _closed_uk_bridge_kinds(source) == (bridge_kind,)
    validate(source, faithful)
    wrong_ordered_kind = {
        "continuation": "однако",
        "contrast": "далее",
        "sequence": "при этом",
    }[bridge_kind]
    unsafe_expressions = (
        f"{left}, либо {right}",
        f"{left}, {wrong_ordered_kind} {right}",
        f"{right}, {equivalent} {left}",
        f"{left}, {equivalent} прокуратура должна изъять лицензию на оружие",
    )
    for expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_v3_closed_uk_anchored_weekday_does_not_consume_sreda_actor() -> None:
    clause = "по пятницам и Среда обязана аннулировать лицензию на оружие"
    modal_start = clause.index("обязана")
    families = _closed_uk_weekday_role_families(
        clause,
        range_start=0,
        range_end=modal_start,
    )
    assert len(families) == 1
    assert tuple(
        clause[start:end] for start, end in families[0]
    ) == ("по пятницам",)


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
def test_v3_closed_uk_anchored_weekday_and_sreda_actor_are_both_preserved(
    validate: RelationValidator,
) -> None:
    expression = (
        "по пятницам и Среда обязана аннулировать лицензию на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    validate(source, faithful)
    for changed in (
        expression.replace("Среда ", "", 1),
        expression.replace("Среда", "Суд", 1),
        expression.replace(
            "по пятницам и Среда",
            "по пятницам и субботам полиция",
            1,
        ),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            changed,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_SHARED_DECISION_ACTION_CASES = (
    (
        "и должен изъять лицензию на оружие",
        "либо должен изъять лицензию на оружие",
        "shared-modal",
    ),
    (
        "и изъять лицензию на оружие",
        "либо изъять лицензию на оружие",
        "shared-infinitive",
    ),
)


@pytest.mark.parametrize(
    ("continuation", "changed_continuation", "case_id"),
    _CLOSED_UK_SHARED_DECISION_ACTION_CASES,
    ids=tuple(case[2] for case in _CLOSED_UK_SHARED_DECISION_ACTION_CASES),
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
def test_v3_closed_uk_decision_role_stops_before_shared_action_boundary(
    continuation: str,
    changed_continuation: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_action = (
        "суд обязан аннулировать лицензию на оружие по совместному "
        "решению суда и прокуратуры"
    )
    expression = f"{first_action} {continuation}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert _closed_uk_action_logic_contract(source, records) == "conjunctive"
    validate(source, faithful)
    for unsafe_expression in (
        expression.replace(
            "решению суда и прокуратуры",
            "решению суда или прокуратуры",
            1,
        ),
        expression.replace(" и прокуратуры", "", 1),
        f"{first_action} {changed_continuation}",
        first_action,
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        assert unsafe != faithful
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
def test_v3_closed_uk_decision_role_stops_before_new_performer(
    validate: RelationValidator,
) -> None:
    expression = (
        "суд обязан аннулировать лицензию на оружие по совместному решению "
        "суда и прокуратуры и полиция должна изъять лицензию на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert _closed_uk_action_logic_contract(source, records) == "conjunctive"
    validate(source, faithful)
    for unsafe_expression in (
        expression.replace(" и полиция должна", " и должна", 1),
        expression.replace("полиция должна", "губернатор должен", 1),
        expression.replace("суда и прокуратуры", "суда", 1),
        expression.replace(" и полиция должна", " либо полиция должна", 1),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_BARE_AND_NEW_CLAUSE_CASES = (
    (
        "Суд проверил документ и лицензию должна аннулировать прокуратура.",
        "прокуратура",
        "губернатор",
        "modal-ovs",
    ),
    (
        "Суд проверил документ и лицензия аннулирована прокуратурой.",
        "прокуратурой",
        "губернатором",
        "passive-clause",
    ),
)


@pytest.mark.parametrize(
    ("source", "actor", "changed_actor", "case_id"),
    _CLOSED_UK_BARE_AND_NEW_CLAUSE_CASES,
    ids=tuple(case[3] for case in _CLOSED_UK_BARE_AND_NEW_CLAUSE_CASES),
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
def test_v3_closed_uk_bare_and_starts_complete_ovs_or_passive_clause(
    source: str,
    actor: str,
    changed_actor: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for unsafe in (
        source.replace(f" {actor}", "", 1).rstrip("."),
        source.replace(actor, changed_actor, 1).rstrip("."),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_BARE_AND_NON_SEPARATOR_CONTROLS = (
    (
        "Суд проверил документ и лицензию.",
        " и лицензию",
        "coordinated-patients",
    ),
    (
        "Суд и прокуратура должны аннулировать лицензию на оружие.",
        " и прокуратура",
        "coordinated-actors",
    ),
    (
        "Суд обязан аннулировать пропуск и лицензию на оружие.",
        " и лицензию",
        "action-patients",
    ),
    (
        "Лицензия аннулирована судом и прокуратурой.",
        " и прокуратурой",
        "passive-actors",
    ),
)


@pytest.mark.parametrize(
    ("clause", "needle", "case_id"),
    _CLOSED_UK_BARE_AND_NON_SEPARATOR_CONTROLS,
    ids=tuple(case[2] for case in _CLOSED_UK_BARE_AND_NON_SEPARATOR_CONTROLS),
)
def test_v3_closed_uk_bare_and_keeps_coordinated_role_inside_clause(
    clause: str,
    needle: str,
    case_id: str,
) -> None:
    del case_id
    connector = clause.index(needle)
    assert not any(
        start <= connector < end
        for start, end in _relation_separator_spans(clause)
    )


_CLOSED_UK_DECISION_RIGHT_BOUNDARY_CASES = (
    (
        "суд обязан аннулировать лицензию на оружие по совместному решению "
        "суда и прокуратуры и полиция изымает лицензию на оружие",
        ("полици",),
        (
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и прокуратуры и изымает лицензию на оружие",
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и прокуратуры и губернатор изымает лицензию "
            "на оружие",
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда или прокуратуры и полиция изымает лицензию "
            "на оружие",
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и полиция изымает лицензию на оружие",
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и прокуратуры либо полиция изымает лицензию "
            "на оружие",
        ),
        "finite-active-performer",
    ),
    (
        "суд обязан аннулировать лицензию на оружие по совместному решению "
        "суда и прокуратуры и лицензия изымается полицией",
        ("полици",),
        (
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и прокуратуры и лицензия изымается",
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и прокуратуры и лицензия изымается губернатором",
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и лицензия изымается полицией",
            "суд обязан аннулировать лицензию на оружие по совместному "
            "решению суда и прокуратуры либо лицензия изымается полицией",
        ),
        "passive-patient-performer",
    ),
    (
        "суд обязан аннулировать лицензию на оружие по решению суда и "
        "полиция и губернатор вправе изъять лицензию на оружие",
        ("полици", "губернатор"),
        (
            "суд обязан аннулировать лицензию на оружие по решению суда и "
            "губернатор вправе изъять лицензию на оружие",
            "суд обязан аннулировать лицензию на оружие по решению суда и "
            "полиция вправе изъять лицензию на оружие",
            "суд обязан аннулировать лицензию на оружие по решению "
            "прокуратуры и полиция и губернатор вправе изъять лицензию "
            "на оружие",
            "суд обязан аннулировать лицензию на оружие по решению суда "
            "либо полиция и губернатор вправе изъять лицензию на оружие",
        ),
        "invariant-modal-mixed-number",
    ),
)


@pytest.mark.parametrize(
    (
        "expression",
        "right_actor_fragments",
        "unsafe_expressions",
        "case_id",
    ),
    _CLOSED_UK_DECISION_RIGHT_BOUNDARY_CASES,
    ids=tuple(case[3] for case in _CLOSED_UK_DECISION_RIGHT_BOUNDARY_CASES),
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
def test_v3_closed_uk_decision_boundary_keeps_complete_right_performer(
    expression: str,
    right_actor_fragments: tuple[str, ...],
    unsafe_expressions: tuple[str, ...],
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert "суд" in "|".join(records[0].actors)
    right_actor_payload = "|".join(records[1].actors)
    assert all(fragment in right_actor_payload for fragment in right_actor_fragments)
    assert records[0].actors != records[1].actors
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert _closed_uk_action_logic_contract(source, records) == "conjunctive"
    validate(source, faithful)
    for unsafe_expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_STACKED_DECISION_ROLE_CASES = (
    (
        "при санкции прокурора",
        "при санкции губернатора",
        "authorization",
    ),
    (
        "при условии уведомления владельца",
        "при условии согласия владельца",
        "condition",
    ),
)


@pytest.mark.parametrize(
    ("secondary_role", "changed_role", "case_id"),
    _CLOSED_UK_STACKED_DECISION_ROLE_CASES,
    ids=tuple(case[2] for case in _CLOSED_UK_STACKED_DECISION_ROLE_CASES),
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
def test_v3_closed_uk_stacked_decision_role_stops_before_outer_bridge(
    secondary_role: str,
    changed_role: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left = (
        "суд обязан аннулировать лицензию на оружие по решению суда "
        f"{secondary_role}"
    )
    right = "полиция должна изъять лицензию на оружие"
    expression = f"{left} и {right}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert _closed_uk_action_logic_contract(source, records) == "conjunctive"
    validate(source, faithful)
    for unsafe_expression in (
        f"{left} либо {right}",
        expression.replace("по решению суда ", "", 1),
        expression.replace(f" {secondary_role}", "", 1),
        expression.replace(secondary_role, changed_role, 1),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_FUTURE_POSTPOSED_ACTOR_CONDITION_CASES = (
    ("правила допуска примет прокуратура", "допуска примет прокуратура", "will-adopt"),
    (
        "правила допуска станет применять прокуратура",
        "допуска станет применять прокуратура",
        "will-start-applying",
    ),
    ("правила допуска введёт прокуратура", "допуска введёт прокуратура", "will-introduce"),
    ("правила допуска вынесет прокуратура", "допуска вынесет прокуратура", "will-issue"),
    ("правила допуска возьмёт прокуратура", "допуска возьмёт прокуратура", "will-take"),
    ("правила допуска даст прокуратура", "допуска даст прокуратура", "will-give"),
    (
        "правила допуска приняты парламентом",
        "допуска приняты парламентом",
        "passive-instrumental",
    ),
)


@pytest.mark.parametrize(
    ("second_branch", "unsafe_branch", "case_id"),
    _CLOSED_UK_FUTURE_POSTPOSED_ACTOR_CONDITION_CASES,
    ids=tuple(
        case[2] for case in _CLOSED_UK_FUTURE_POSTPOSED_ACTOR_CONDITION_CASES
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
def test_v3_closed_uk_shared_head_does_not_inherit_future_right_clause(
    second_branch: str,
    unsafe_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    parts = (first_branch, second_branch)
    assert _relation_condition_shared_head_parts(parts) == parts
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {second_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {second_branch}",
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {unsafe_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_SHARED_HEAD_NOMINAL_CONTROLS = (
    "принятия решений",
    "становления организации",
    "введения допуска",
    "вынесения решений",
    "взятия образцов",
    "выдачи разрешений",
    "принятые парламентом",
)


@pytest.mark.parametrize(
    "nominal_tail",
    _CLOSED_UK_SHARED_HEAD_NOMINAL_CONTROLS,
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
def test_v3_closed_uk_shared_head_still_inherits_real_nominal_np(
    nominal_tail: str,
    validate: RelationValidator,
) -> None:
    first_branch = "нарушены правила хранения"
    second_branch = f"правила {nominal_tail}"
    assert _relation_condition_shared_head_parts(
        (first_branch, second_branch)
    ) == (first_branch, f"нарушены {second_branch}")
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {second_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {nominal_tail}",
        compact_ground=True,
    )
    validate(source, faithful)


_CLOSED_UK_LEADING_ADJUNCT_AGREEMENT_CASES = (
    (
        "(когда стороны должны уведомить владельца)",
        "прокуратура",
        "обязана",
        "обязаны",
        "parenthesized-singular",
    ),
    (
        "(когда суд должен уведомить владельца)",
        "прокуратура и полиция",
        "обязаны",
        "обязана",
        "parenthesized-plural",
    ),
    (
        "после того как стороны должны уведомить владельца,",
        "прокуратура",
        "обязана",
        "обязаны",
        "compound-temporal-singular",
    ),
    (
        "после того как суд должен уведомить владельца,",
        "прокуратура и полиция",
        "обязаны",
        "обязана",
        "compound-temporal-plural",
    ),
)


@pytest.mark.parametrize(
    ("adjunct", "actor", "governor", "wrong_governor", "case_id"),
    _CLOSED_UK_LEADING_ADJUNCT_AGREEMENT_CASES,
    ids=tuple(case[4] for case in _CLOSED_UK_LEADING_ADJUNCT_AGREEMENT_CASES),
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
def test_v3_closed_uk_agreement_ignores_modal_inside_leading_adjunct(
    adjunct: str,
    actor: str,
    governor: str,
    wrong_governor: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = (
        f"Суд проверил документ и {adjunct} {actor} {governor} "
        "аннулировать лицензию."
    )
    connector = source.index(f" и {adjunct}")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    missing_actor = source.replace(f" {actor} {governor}", f" {governor}", 1)
    with pytest.raises(ValueError):
        validate(source, missing_actor.rstrip("."))
    wrong = source.replace(
        f" {governor} аннулировать",
        f" {wrong_governor} аннулировать",
        1,
    )
    wrong_connector = wrong.index(f" и {adjunct}")
    assert not any(
        start <= wrong_connector < end
        for start, end in _relation_separator_spans(wrong)
    )


_CLOSED_UK_GLOBAL_AUTHORIZATION_FORMULA_CASES = (
    (
        "при санкции суда или при санкции прокурора правоохранители обязаны "
        "аннулировать и изъять лицензию на оружие",
        "при санкции прокурора или при санкции суда правоохранители обязаны "
        "изъять и аннулировать лицензию на оружие",
        "leading",
    ),
    (
        "правоохранители обязаны аннулировать и изъять лицензию на оружие "
        "при санкции суда или при санкции прокурора",
        "правоохранители обязаны изъять и аннулировать лицензию на оружие "
        "при санкции прокурора или при санкции суда",
        "trailing-shared-patient",
    ),
)


@pytest.mark.parametrize(
    ("expression", "reordered_expression", "case_id"),
    _CLOSED_UK_GLOBAL_AUTHORIZATION_FORMULA_CASES,
    ids=tuple(case[2] for case in _CLOSED_UK_GLOBAL_AUTHORIZATION_FORMULA_CASES),
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
def test_v3_closed_uk_global_authorization_or_reorders_as_one_formula(
    expression: str,
    reordered_expression: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        reordered_expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == 2
    assert len(contexts) == 2
    global_spans = _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert global_spans == frozenset(
        (context.start, context.end) for context in contexts
    )
    formulas = _closed_uk_authorization_formula_contracts(
        source,
        records,
        contexts,
        global_spans,
    )
    assert len(formulas) == 1
    assert ":global:alternative:" in formulas[0]
    validate(source, faithful)
    role_formula = "при санкции суда или при санкции прокурора"
    local_expression = (
        "правоохранители обязаны аннулировать и при санкции суда или при "
        "санкции прокурора изъять лицензию на оружие"
    )
    for unsafe_expression in (
        expression.replace(
            role_formula,
            "при санкции суда и при санкции прокурора",
            1,
        ),
        expression.replace(role_formula, "при санкции суда", 1),
        local_expression,
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        assert unsafe != faithful
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_NON_GLOBAL_AUTHORIZATION_CONTROLS = (
    (
        "правоохранители обязаны аннулировать и при санкции суда или при "
        "санкции прокурора изъять лицензию на оружие",
        "conjunctive",
        True,
        "local-formula",
    ),
    (
        "правоохранители обязаны аннулировать лицензию на оружие при "
        "санкции суда, или при санкции прокурора правоохранители обязаны "
        "изъять лицензию на оружие",
        "alternative",
        False,
        "cross-action-contexts",
    ),
)


@pytest.mark.parametrize(
    ("expression", "bridge_kind", "has_local_formula", "case_id"),
    _CLOSED_UK_NON_GLOBAL_AUTHORIZATION_CONTROLS,
    ids=tuple(case[3] for case in _CLOSED_UK_NON_GLOBAL_AUTHORIZATION_CONTROLS),
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
def test_v3_closed_uk_local_authorization_contexts_are_not_global(
    expression: str,
    bridge_kind: str,
    has_local_formula: bool,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == 2
    assert len(contexts) == 2
    assert _closed_uk_bridge_kinds(source) == (bridge_kind,)
    global_spans = _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert not global_spans
    formulas = _closed_uk_authorization_formula_contracts(
        source,
        records,
        contexts,
        global_spans,
    )
    assert bool(formulas) is has_local_formula
    assert all(":global:" not in formula for formula in formulas)
    validate(source, faithful)


_CLOSED_UK_NEGATIVE_PASSIVE_NEW_CLAUSE_CASES = (
    (
        "Суд проверил документ и лицензия не аннулирована прокуратурой.",
        "не аннулирована",
        "не изъята",
        "singular",
    ),
    (
        "Суд проверил документ и лицензии не аннулированы прокуратурой.",
        "не аннулированы",
        "не изъяты",
        "plural",
    ),
)


@pytest.mark.parametrize(
    ("source", "negative_predicate", "changed_predicate", "case_id"),
    _CLOSED_UK_NEGATIVE_PASSIVE_NEW_CLAUSE_CASES,
    ids=tuple(
        case[3] for case in _CLOSED_UK_NEGATIVE_PASSIVE_NEW_CLAUSE_CASES
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
def test_v3_closed_uk_bare_and_negative_passive_keeps_complete_clause(
    source: str,
    negative_predicate: str,
    changed_predicate: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for unsafe in (
        source.replace(" не ", " ", 1).rstrip("."),
        source.replace("прокуратурой", "губернатором", 1).rstrip("."),
        source.replace(" прокуратурой", "", 1).rstrip("."),
        source.replace(negative_predicate, changed_predicate, 1).rstrip("."),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_STANDALONE_SHORT_PASSIVE_CASES = (
    ("условия сняты", "условия", "условия даны", "removed"),
    ("условия даны", "условия", "условия сняты", "given"),
)


@pytest.mark.parametrize(
    ("second_branch", "dropped_predicate", "changed_branch", "case_id"),
    _CLOSED_UK_STANDALONE_SHORT_PASSIVE_CASES,
    ids=tuple(case[3] for case in _CLOSED_UK_STANDALONE_SHORT_PASSIVE_CASES),
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
def test_v3_closed_uk_shared_head_keeps_standalone_short_passive_clause(
    second_branch: str,
    dropped_predicate: str,
    changed_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    parts = (first_branch, second_branch)
    assert _relation_condition_shared_head_parts(parts) == parts
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {second_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {second_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    for unsafe_branch in (dropped_predicate, changed_branch):
        unsafe = _closed_uk_boolean_condition_surface(
            f"{first_branch} или {unsafe_branch}",
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_REQUESTED_NOMINAL_SHARED_HEAD_CASES = (
    ("нарушены правила охраны", "охраны", "guarding-rules"),
    ("нарушены правила смены", "смены", "shift-rules"),
    ("нарушены положения закона", "положения закона", "law-provisions"),
    ("нарушены законы страны", "законы страны", "country-laws"),
)


@pytest.mark.parametrize(
    ("expanded_branch", "compact_branch", "case_id"),
    _CLOSED_UK_REQUESTED_NOMINAL_SHARED_HEAD_CASES,
    ids=tuple(
        case[2] for case in _CLOSED_UK_REQUESTED_NOMINAL_SHARED_HEAD_CASES
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
def test_v3_closed_uk_shared_head_keeps_requested_nominal_np_controls(
    expanded_branch: str,
    compact_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    assert _relation_condition_shared_head_parts(
        (first_branch, compact_branch)
    ) == (first_branch, expanded_branch)
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {expanded_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {compact_branch}",
        compact_ground=True,
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
def test_v3_closed_uk_internal_authorization_authority_is_not_a_separator(
    validate: RelationValidator,
) -> None:
    source = (
        "Суды при санкции суда и прокуратуры должны аннулировать лицензию "
        "на оружие."
    )
    internal_connector = source.index(" и прокуратуры")
    assert not any(
        start <= internal_connector < end
        for start, end in _relation_separator_spans(source)
    )
    contexts = _closed_uk_authorization_contexts(source)
    assert len(contexts) == 1
    assert all(
        fragment in contexts[0].key for fragment in ("суд", "прокуратур")
    )
    validate(source, source.rstrip("."))
    for unsafe in (
        source.replace(" и прокуратуры", "", 1).rstrip("."),
        source.replace("суда и прокуратуры", "суда или прокуратуры", 1)
        .rstrip("."),
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
def test_v3_closed_uk_authorization_then_new_actor_starts_outer_bridge(
    validate: RelationValidator,
) -> None:
    expression = (
        "Суды должны аннулировать лицензию на оружие при санкции прокурора "
        "и полиция должна изъять лицензию на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    outer_connector = source.index(" и полиция должна")
    assert any(
        start <= outer_connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    validate(source, faithful)
    for unsafe_expression in (
        expression.replace(" и полиция должна", " и должна", 1),
        expression.replace(" и полиция должна", " либо полиция должна", 1),
        expression.replace(" при санкции прокурора", "", 1),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_MODAL_PASSIVE_DECISION_BOUNDARY_CASES = (
    (
        "лицензия должна быть изъята полицией",
        " и лицензия должна",
        "modal-passive",
    ),
    (
        "лицензия будет изъята полицией",
        " и лицензия будет",
        "future-passive",
    ),
    (
        "полиция должна изъять лицензию на оружие",
        " и полиция должна",
        "active-not-passive-patient",
    ),
)


@pytest.mark.parametrize(
    ("right_branch", "connector_needle", "case_id"),
    _CLOSED_UK_MODAL_PASSIVE_DECISION_BOUNDARY_CASES,
    ids=tuple(
        case[2] for case in _CLOSED_UK_MODAL_PASSIVE_DECISION_BOUNDARY_CASES
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
def test_v3_closed_uk_decision_stops_before_modal_passive_or_active_clause(
    right_branch: str,
    connector_needle: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left = (
        "суд обязан аннулировать лицензию на оружие по решению суда"
    )
    expression = f"{left} и {right_branch}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert "полици" in "|".join(records[1].actors)
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    connector = source.index(connector_needle)
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, faithful)
    for unsafe_expression in (
        expression.replace("полицией", "губернатором", 1).replace(
            "полиция",
            "губернатор",
            1,
        ),
        left,
        expression.replace(" по решению суда", "", 1),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("adjunct", "changed_adjunct", "case_id"),
    (
        ("всё ещё не", "всё ещё", "still-negative"),
        ("уже", "ещё не", "already-positive"),
    ),
    ids=("still-negative", "already-positive"),
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
def test_v3_closed_uk_bare_and_passive_adverb_keeps_full_semantics(
    adjunct: str,
    changed_adjunct: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = (
        f"Суд проверил документ и лицензия {adjunct} аннулирована "
        "прокуратурой."
    )
    connector = source.index(" и лицензия")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for unsafe in (
        source.replace(adjunct, changed_adjunct, 1).rstrip("."),
        source.replace("прокуратурой", "губернатором", 1).rstrip("."),
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
def test_v3_closed_uk_outer_modal_precedes_compound_temporal_adjunct(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд проверил документ и прокуратуры должны после того как суд "
        "может уведомить владельца аннулировать лицензию."
    )
    connector = source.index(" и прокуратуры")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for unsafe in (
        source.replace(" прокуратуры должны", " должны", 1).rstrip("."),
        source.replace("прокуратуры должны", "прокуратуры могут", 1)
        .rstrip("."),
        source.replace("суд может", "суд должен", 1).rstrip("."),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)
    wrong_agreement = source.replace(
        "прокуратуры должны",
        "прокуратуры должна",
        1,
    )
    assert not any(
        start <= connector < end
        for start, end in _relation_separator_spans(wrong_agreement)
    )


_CLOSED_UK_PRECEDENCE_ACTION_A = (
    "суд обязан аннулировать лицензию на оружие"
)
_CLOSED_UK_PRECEDENCE_ACTION_B = (
    "полиция должна изъять лицензию на оружие"
)
_CLOSED_UK_PRECEDENCE_ACTION_C = (
    "прокуратура обязана приостановить лицензию на оружие"
)
_CLOSED_UK_CORRELATIVE_PRECEDENCE_CASES = (
    (
        f"И {_CLOSED_UK_PRECEDENCE_ACTION_A}, "
        f"и {_CLOSED_UK_PRECEDENCE_ACTION_B}, "
        f"либо {_CLOSED_UK_PRECEDENCE_ACTION_C}",
        f"И {_CLOSED_UK_PRECEDENCE_ACTION_B}, "
        f"и {_CLOSED_UK_PRECEDENCE_ACTION_A}, "
        f"либо {_CLOSED_UK_PRECEDENCE_ACTION_C}",
        f"{_CLOSED_UK_PRECEDENCE_ACTION_A} и "
        f"({_CLOSED_UK_PRECEDENCE_ACTION_B} либо "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_C})",
        "and-pair-before-or",
    ),
    (
        f"Либо {_CLOSED_UK_PRECEDENCE_ACTION_A}, либо "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_B} и "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_C}",
        f"Либо {_CLOSED_UK_PRECEDENCE_ACTION_A}, либо "
        f"({_CLOSED_UK_PRECEDENCE_ACTION_C} и "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_B})",
        f"(Либо {_CLOSED_UK_PRECEDENCE_ACTION_A}, либо "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_B}) и "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_C}",
        "or-before-and-precedence",
    ),
    (
        f"Либо ({_CLOSED_UK_PRECEDENCE_ACTION_A} и "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_B}), либо "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_C}",
        f"Либо {_CLOSED_UK_PRECEDENCE_ACTION_C}, либо "
        f"({_CLOSED_UK_PRECEDENCE_ACTION_B} и "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_A})",
        f"{_CLOSED_UK_PRECEDENCE_ACTION_A} и "
        f"({_CLOSED_UK_PRECEDENCE_ACTION_B} либо "
        f"{_CLOSED_UK_PRECEDENCE_ACTION_C})",
        "explicit-parentheses",
    ),
)


@pytest.mark.parametrize(
    ("source_expression", "faithful_expression", "unsafe_expression", "case_id"),
    _CLOSED_UK_CORRELATIVE_PRECEDENCE_CASES,
    ids=tuple(case[3] for case in _CLOSED_UK_CORRELATIVE_PRECEDENCE_CASES),
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
def test_v3_closed_uk_correlative_precedence_keeps_exact_action_tree(
    source_expression: str,
    faithful_expression: str,
    unsafe_expression: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        source_expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        faithful_expression,
        compact_ground=True,
    )
    unsafe = _closed_uk_correlative_tree_surface(
        unsafe_expression,
        compact_ground=True,
    )
    source_records = _closed_uk_revocation_action_records(source)
    faithful_records = _closed_uk_revocation_action_records(faithful)
    unsafe_records = _closed_uk_revocation_action_records(unsafe)
    assert len(source_records) == len(faithful_records) == len(unsafe_records) == 3
    source_logic = _closed_uk_action_logic_contract(source, source_records)
    faithful_logic = _closed_uk_action_logic_contract(
        faithful,
        faithful_records,
    )
    unsafe_logic = _closed_uk_action_logic_contract(unsafe, unsafe_records)
    assert source_logic.startswith("tree:")
    assert source_logic == faithful_logic
    assert source_logic != unsafe_logic
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_AUTHORIZATION_SCOPE_NEGATIVE_CASES = (
    (
        "при санкции суда полиция должна аннулировать лицензию на оружие, "
        "и при санкции прокурора прокуратура должна изъять лицензию на оружие",
        "при санкции суда или при санкции прокурора полиция должна "
        "аннулировать лицензию на оружие, и прокуратура должна изъять "
        "лицензию на оружие",
        "conjunctive",
        0,
        "two-distinct-local-owners",
    ),
    (
        "полиция при санкции суда должна аннулировать лицензию на оружие, "
        "и прокуратура должна изъять лицензию на оружие",
        "при санкции суда полиция должна аннулировать лицензию на оружие, "
        "и прокуратура должна изъять лицензию на оружие",
        "conjunctive",
        0,
        "after-first-actor",
    ),
    (
        "полиция должна аннулировать лицензию на оружие при санкции суда, "
        "либо прокуратура должна изъять лицензию на оружие",
        "при санкции суда полиция должна аннулировать лицензию на оружие, "
        "либо прокуратура должна изъять лицензию на оружие",
        "alternative",
        0,
        "trailing-before-or",
    ),
    (
        "полиция должна аннулировать лицензию на оружие при санкции суда, "
        "однако прокуратура должна изъять лицензию на оружие",
        "при санкции суда полиция должна аннулировать лицензию на оружие, "
        "однако прокуратура должна изъять лицензию на оружие",
        "contrast",
        0,
        "trailing-before-contrast",
    ),
    (
        "полиция должна аннулировать лицензию на оружие при санкции суда, "
        "а затем прокуратура должна изъять лицензию на оружие",
        "при санкции суда полиция должна аннулировать лицензию на оружие, "
        "а затем прокуратура должна изъять лицензию на оружие",
        "sequence",
        0,
        "trailing-before-sequence",
    ),
)


@pytest.mark.parametrize(
    (
        "expression",
        "moved_scope_expression",
        "bridge_kind",
        "global_count",
        "case_id",
    ),
    _CLOSED_UK_AUTHORIZATION_SCOPE_NEGATIVE_CASES,
    ids=tuple(
        case[4] for case in _CLOSED_UK_AUTHORIZATION_SCOPE_NEGATIVE_CASES
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
def test_v3_closed_uk_local_authorization_is_not_promoted_to_global(
    expression: str,
    moved_scope_expression: str,
    bridge_kind: str,
    global_count: int,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == 2
    assert contexts
    assert _closed_uk_bridge_kinds(source) == (bridge_kind,)
    global_spans = _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert len(global_spans) == global_count
    formulas = _closed_uk_authorization_formula_contracts(
        source,
        records,
        contexts,
        global_spans,
    )
    assert all(":global:" not in formula for formula in formulas)
    validate(source, faithful)
    moved_scope = _closed_uk_correlative_tree_surface(
        moved_scope_expression,
        compact_ground=True,
    )
    with pytest.raises(ValueError):
        validate(source, moved_scope)


_CLOSED_UK_DUPLICATE_FAMILY_LOCAL_SOURCE_EXPRESSION = (
    "полиция вправе при санкции суда аннулировать лицензию на оружие или "
    "при санкции прокурора отозвать служебную лицензию"
)
_CLOSED_UK_DUPLICATE_FAMILY_LOCAL_UNSAFE_EXPRESSIONS = (
    (
        "полиция вправе при санкции прокурора аннулировать лицензию на "
        "оружие или при санкции суда отозвать служебную лицензию"
    ),
    (
        "полиция вправе при санкции суда аннулировать служебную лицензию "
        "или при санкции прокурора отозвать лицензию на оружие"
    ),
    (
        "при санкции суда или при санкции прокурора полиция вправе "
        "аннулировать лицензию на оружие или отозвать служебную лицензию"
    ),
    (
        "полиция вправе при санкции суда аннулировать лицензию на оружие "
        "или отозвать служебную лицензию"
    ),
    (
        "полиция вправе при санкции суда отозвать служебную лицензию или "
        "при санкции прокурора аннулировать лицензию на оружие"
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
def test_v3_closed_uk_duplicate_family_actions_keep_distinct_local_ownership(
    validate: RelationValidator,
) -> None:
    expression = _CLOSED_UK_DUPLICATE_FAMILY_LOCAL_SOURCE_EXPRESSION
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == 2
    assert len({record.family for record in records}) == 1
    assert _closed_uk_bridge_kinds(source) == ("alternative",)
    assert len(contexts) == 2
    global_spans = _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert not global_spans
    assert not _closed_uk_authorization_formula_contracts(
        source,
        records,
        contexts,
        global_spans,
    )
    validate(source, faithful)
    for unsafe_expression in (
        _CLOSED_UK_DUPLICATE_FAMILY_LOCAL_UNSAFE_EXPRESSIONS
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_PASSIVE_PATIENT_AGREEMENT_MISMATCHES = (
    (
        "лицензия",
        "аннулированы",
        "лицензия",
        "аннулирована",
        "singular-to-plural",
    ),
    (
        "лицензии",
        "аннулирована",
        "лицензии",
        "аннулированы",
        "plural-to-feminine",
    ),
    (
        "лицензия",
        "аннулирован",
        "лицензия",
        "аннулирована",
        "feminine-to-masculine",
    ),
    (
        "пропуск",
        "аннулирована",
        "пропуск",
        "аннулирован",
        "masculine-to-feminine",
    ),
    (
        "разрешение",
        "аннулирована",
        "разрешение",
        "аннулировано",
        "neuter-to-feminine",
    ),
    (
        "лицензию",
        "аннулирована",
        "лицензия",
        "аннулирована",
        "accusative-patient",
    ),
)


@pytest.mark.parametrize(
    "negation",
    ("", "не "),
    ids=("positive", "negative"),
)
@pytest.mark.parametrize(
    (
        "bad_patient",
        "bad_predicate",
        "valid_patient",
        "valid_predicate",
        "case_id",
    ),
    _CLOSED_UK_PASSIVE_PATIENT_AGREEMENT_MISMATCHES,
    ids=tuple(
        case[4]
        for case in _CLOSED_UK_PASSIVE_PATIENT_AGREEMENT_MISMATCHES
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
def test_v3_closed_uk_passive_patient_must_agree_with_predicate(
    negation: str,
    bad_patient: str,
    bad_predicate: str,
    valid_patient: str,
    valid_predicate: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        valid_patient,
        valid_predicate,
    ) is True
    assert _relation_document_patient_passive_agrees(
        bad_patient,
        bad_predicate,
    ) is False
    source = (
        f"Суд проверил документ и {valid_patient} {negation}"
        f"{valid_predicate} прокуратурой."
    )
    unsafe = (
        f"Суд проверил документ и {bad_patient} {negation}"
        f"{bad_predicate} прокуратурой"
    )
    source_connector = source.index(" и ")
    unsafe_connector = unsafe.index(" и ")
    assert any(
        start <= source_connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= unsafe_connector < end
        for start, end in _relation_separator_spans(unsafe)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_LOCAL_NEGATIVE_STATUS_CASES = (
    ("никогда не", "больше не", "never"),
    ("больше не", "до сих пор не", "no-longer"),
    ("до сих пор не", "никогда не", "still-not"),
)


@pytest.mark.parametrize(
    ("status", "changed_status", "case_id"),
    _CLOSED_UK_LOCAL_NEGATIVE_STATUS_CASES,
    ids=tuple(case[2] for case in _CLOSED_UK_LOCAL_NEGATIVE_STATUS_CASES),
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
def test_v3_closed_uk_local_negative_status_keeps_separator_and_semantics(
    status: str,
    changed_status: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = (
        f"Суд проверил документ и лицензия {status} аннулирована "
        "прокуратурой."
    )
    connector = source.index(" и лицензия")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for unsafe in (
        source.replace(f" {status} ", " ", 1).rstrip("."),
        source.replace(status, changed_status, 1).rstrip("."),
        source.replace(" не ", " ", 1).rstrip("."),
        source.replace("прокуратурой", "губернатором", 1).rstrip("."),
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
def test_v3_closed_uk_weekday_and_sanction_move_with_complete_action(
    validate: RelationValidator,
) -> None:
    action_a = (
        "полиция должна аннулировать лицензию на оружие только по пятницам "
        "или субботам при санкции суда"
    )
    action_b = "прокуратура должна изъять служебную лицензию"
    expression = f"{action_a}, и {action_b}"
    reordered = f"{action_b}, и {action_a}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        reordered,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    weekdays = _closed_uk_weekday_role_families(
        source,
        range_start=0,
        range_end=len(source),
    )
    assert len(records) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert len(contexts) == 1
    assert not _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert len(weekdays) == 1
    assert len(weekdays[0]) == 2
    validate(source, faithful)
    action_a_without_context = action_a.replace(" при санкции суда", "", 1)
    for unsafe_expression in (
        f"{action_a_without_context}, и при санкции суда {action_b}",
        f"при санкции суда {action_b}, и {action_a_without_context}",
        expression.replace("пятницам или субботам", "пятницам и субботам", 1),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_DISTINCT_LOCAL_AUTH_SOURCE_EXPRESSION = (
    "при санкции суда полиция должна аннулировать лицензию на оружие, и "
    "при санкции прокурора прокуратура должна изъять служебную лицензию"
)
_CLOSED_UK_DISTINCT_LOCAL_AUTH_REORDERED_EXPRESSION = (
    "при санкции прокурора прокуратура должна изъять служебную лицензию, и "
    "при санкции суда полиция должна аннулировать лицензию на оружие"
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
def test_v3_closed_uk_distinct_actor_authorizations_both_stay_local(
    validate: RelationValidator,
) -> None:
    expression = _CLOSED_UK_DISTINCT_LOCAL_AUTH_SOURCE_EXPRESSION
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        _CLOSED_UK_DISTINCT_LOCAL_AUTH_REORDERED_EXPRESSION,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == len(contexts) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert not _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    validate(source, faithful)
    for unsafe_expression in (
        expression.replace("санкции суда", "санкции губернатора", 1),
        (
            "при санкции прокурора полиция должна аннулировать лицензию "
            "на оружие, и при санкции суда прокуратура должна изъять "
            "служебную лицензию"
        ),
        (
            "при санкции суда или при санкции прокурора полиция должна "
            "аннулировать лицензию на оружие, и прокуратура должна изъять "
            "служебную лицензию"
        ),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
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
def test_v3_closed_uk_single_leading_authorization_is_global_for_distinct_actors(
    validate: RelationValidator,
) -> None:
    action_a = "полиция должна аннулировать лицензию на оружие"
    action_b = "прокуратура должна изъять служебную лицензию"
    source = _closed_uk_correlative_tree_surface(
        f"при санкции суда {action_a}, и {action_b}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"при санкции суда {action_b}, и {action_a}",
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == 2
    assert len(contexts) == 1
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    ) == frozenset({(contexts[0].start, contexts[0].end)})
    validate(source, faithful)


_CLOSED_UK_SHARED_ACTOR_COMMON_MODAL_AUTH_CASES = (
    (
        "Правоохранители при санкции суда обязаны аннулировать и изъять "
        "лицензию на оружие",
        "Правоохранители обязаны при санкции суда изъять и аннулировать "
        "лицензию на оружие",
        "before-common-modal",
    ),
    (
        "Правоохранители обязаны при санкции суда аннулировать и изъять "
        "лицензию на оружие",
        "Правоохранители при санкции суда обязаны изъять и аннулировать "
        "лицензию на оружие",
        "after-common-modal",
    ),
)


@pytest.mark.parametrize(
    ("expression", "reordered", "case_id"),
    _CLOSED_UK_SHARED_ACTOR_COMMON_MODAL_AUTH_CASES,
    ids=tuple(
        case[2] for case in _CLOSED_UK_SHARED_ACTOR_COMMON_MODAL_AUTH_CASES
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
def test_v3_closed_uk_shared_actor_authorization_is_global_around_common_modal(
    expression: str,
    reordered: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        reordered,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == 2
    assert len(contexts) == 1
    assert _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    ) == frozenset({(contexts[0].start, contexts[0].end)})
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
def test_v3_closed_uk_passive_clauses_keep_modal_and_context_per_family(
    validate: RelationValidator,
) -> None:
    cancellation = (
        "лицензия на оружие при санкции суда должна быть аннулирована"
    )
    seizure = (
        "служебная лицензия при санкции прокурора изымается полицией"
    )
    expression = f"{cancellation}, и {seizure}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{seizure}, и {cancellation}",
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == len(contexts) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert not _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    validate(source, faithful)
    for unsafe_expression in (
        (
            "лицензия на оружие при санкции суда должна быть изъята, и "
            "служебная лицензия при санкции прокурора аннулируется полицией"
        ),
        expression.replace("санкции суда", "санкции губернатора", 1),
        expression.replace("санкции прокурора", "санкции губернатора", 1),
    ):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
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
def test_v3_closed_uk_true_shared_passive_patient_and_modal_can_reorder(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_correlative_tree_surface(
        "лицензия на оружие при санкции суда должна быть аннулирована и "
        "изъята",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        "лицензия на оружие при санкции суда должна быть изъята и "
        "аннулирована",
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == 2
    assert len(contexts) == 1
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    assert _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    ) == frozenset({(contexts[0].start, contexts[0].end)})
    validate(source, faithful)


_CLOSED_UK_SHARED_ACTOR_PARALLEL_LOCAL_AUTH_CASES = (
    (
        "Правоохранители обязаны при санкции суда аннулировать лицензию на "
        "оружие и при санкции прокурора изъять служебную лицензию",
        "Правоохранители обязаны при санкции прокурора изъять служебную "
        "лицензию и при санкции суда аннулировать лицензию на оружие",
        "при санкции суда Правоохранители обязаны аннулировать лицензию на "
        "оружие и при санкции прокурора изъять служебную лицензию",
        "common-modal-before-contexts",
    ),
    (
        "Правоохранители при санкции суда обязаны аннулировать лицензию на "
        "оружие и при санкции прокурора изъять служебную лицензию",
        "Правоохранители при санкции прокурора обязаны изъять служебную "
        "лицензию и при санкции суда аннулировать лицензию на оружие",
        "при санкции суда Правоохранители обязаны аннулировать лицензию на "
        "оружие и при санкции прокурора изъять служебную лицензию",
        "first-context-before-common-modal",
    ),
)


@pytest.mark.parametrize(
    ("expression", "reordered", "moved_scope", "case_id"),
    _CLOSED_UK_SHARED_ACTOR_PARALLEL_LOCAL_AUTH_CASES,
    ids=tuple(
        case[3]
        for case in _CLOSED_UK_SHARED_ACTOR_PARALLEL_LOCAL_AUTH_CASES
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
def test_v3_closed_uk_shared_actor_parallel_authorizations_stay_local(
    expression: str,
    reordered: str,
    moved_scope: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        reordered,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == len(contexts) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    global_spans = _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert not global_spans
    validate(source, faithful)
    swapped_authorities = (
        expression.replace("санкции суда", "санкции __swap__", 1)
        .replace("санкции прокурора", "санкции суда", 1)
        .replace("санкции __swap__", "санкции прокурора", 1)
    )
    for unsafe_expression in (swapped_authorities, moved_scope):
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_COORDINATED_PASSIVE_AGREEMENT_CONTROLS = (
    (
        "лицензия или пропуск",
        "аннулируется",
        "пропуск или лицензия",
        "аннулируется",
        "",
        "singular-alternative",
    ),
    (
        "лицензия или пропуск",
        "аннулируется",
        "пропуск или лицензия",
        "аннулируется",
        "больше не ",
        "singular-alternative-with-status",
    ),
    (
        "лицензия или пропуск",
        "аннулирован",
        "пропуск или лицензия",
        "аннулирована",
        "",
        "nearest-gender-alternative",
    ),
    (
        "лицензии или пропуски",
        "аннулируются",
        "пропуски или лицензии",
        "аннулируются",
        "",
        "plural-alternative",
    ),
    (
        "лицензия и пропуск",
        "аннулируются",
        "пропуск и лицензия",
        "аннулируются",
        "",
        "plural-conjunction",
    ),
)


@pytest.mark.parametrize(
    (
        "patients",
        "predicate",
        "reordered_patients",
        "reordered_predicate",
        "status",
        "case_id",
    ),
    _CLOSED_UK_COORDINATED_PASSIVE_AGREEMENT_CONTROLS,
    ids=tuple(
        case[5]
        for case in _CLOSED_UK_COORDINATED_PASSIVE_AGREEMENT_CONTROLS
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
def test_v3_closed_uk_coordinated_passive_patient_agreement_is_exact(
    patients: str,
    predicate: str,
    reordered_patients: str,
    reordered_predicate: str,
    status: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patients,
        predicate,
    ) is True
    source = (
        f"Суд проверил документ и {patients} {status}{predicate} "
        "прокуратурой."
    )
    faithful = (
        f"Суд проверил документ и {reordered_patients} "
        f"{status}{reordered_predicate} прокуратурой"
    )
    spans = _relation_separator_spans(source)
    outer_connector = source.index(" и ")
    inner_needle = " или " if " или " in patients else " и "
    inner_connector = source.index(inner_needle, outer_connector + 3)
    assert any(start <= outer_connector < end for start, end in spans)
    assert not any(start <= inner_connector < end for start, end in spans)
    validate(source, faithful)


_CLOSED_UK_COORDINATED_PASSIVE_UNSAFE_SURFACES = (
    "Суд проверил документ и пропуск аннулируется прокуратурой",
    "Суд проверил документ и лицензия аннулируется прокуратурой",
    "Суд проверил документ и лицензия или пропуск аннулируется",
    "Суд проверил документ и лицензия и пропуск аннулируется прокуратурой",
    "Суд проверил документ и лицензии или пропуски аннулируется прокуратурой",
    "Суд проверил документ и лицензия или пропуск аннулирована прокуратурой",
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
def test_v3_closed_uk_coordinated_passive_cannot_drop_or_change_a_role(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд проверил документ и лицензия или пропуск аннулируется "
        "прокуратурой."
    )
    assert _relation_document_patient_passive_agrees(
        "лицензия и пропуск",
        "аннулируется",
    ) is False
    assert _relation_document_patient_passive_agrees(
        "лицензии или пропуски",
        "аннулируется",
    ) is False
    assert _relation_document_patient_passive_agrees(
        "лицензия или пропуск",
        "аннулирована",
    ) is False
    validate(source, source.rstrip("."))
    for unsafe in _CLOSED_UK_COORDINATED_PASSIVE_UNSAFE_SURFACES:
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
def test_v3_closed_uk_coordinated_passive_cannot_drop_local_status(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд проверил документ и лицензия или пропуск больше не "
        "аннулируется прокуратурой."
    )
    faithful = source.rstrip(".")
    validate(source, faithful)
    for unsafe in (
        faithful.replace(" больше не", "", 1),
        faithful.replace(" не ", " ", 1),
        faithful.replace("прокуратурой", "губернатором", 1),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_GLOBAL_FORMULA_SHARED_GOVERNOR_CASES = (
    (
        "либо",
        "alternative",
        "и",
        True,
        "action-alternative",
    ),
    (
        ", а затем",
        "sequence",
        ", однако",
        False,
        "action-sequence",
    ),
    (
        ", однако",
        "contrast",
        ", а затем",
        False,
        "action-contrast",
    ),
)


@pytest.mark.parametrize(
    (
        "action_connector",
        "bridge_kind",
        "wrong_connector",
        "action_reorder_is_safe",
        "case_id",
    ),
    _CLOSED_UK_GLOBAL_FORMULA_SHARED_GOVERNOR_CASES,
    ids=tuple(
        case[4]
        for case in _CLOSED_UK_GLOBAL_FORMULA_SHARED_GOVERNOR_CASES
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
def test_v3_closed_uk_leading_authorization_formula_is_global_over_action_path(
    action_connector: str,
    bridge_kind: str,
    wrong_connector: str,
    action_reorder_is_safe: bool,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id

    def joined_actions(left: str, connector: str, right: str) -> str:
        separator = (
            f"{connector} " if connector.startswith(",")
            else f" {connector} "
        )
        return f"{left}{separator}{right}"

    formula = "при санкции суда или при санкции прокурора"
    reordered_formula = "при санкции прокурора или при санкции суда"
    action_a = "аннулировать лицензию на оружие"
    action_b = "изъять служебную лицензию"
    expression = (
        f"{formula} правоохранители обязаны "
        + joined_actions(action_a, action_connector, action_b)
    )
    faithful_actions = (
        joined_actions(action_b, action_connector, action_a)
        if action_reorder_is_safe
        else joined_actions(action_a, action_connector, action_b)
    )
    faithful_expression = (
        f"{reordered_formula} правоохранители обязаны {faithful_actions}"
    )
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        faithful_expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == len(contexts) == 2
    assert _closed_uk_bridge_kinds(source) == (bridge_kind,)
    global_spans = _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert global_spans == frozenset(
        (context.start, context.end) for context in contexts
    )
    formulas = _closed_uk_authorization_formula_contracts(
        source,
        records,
        contexts,
        global_spans,
    )
    assert len(formulas) == 1
    assert ":global:alternative:" in formulas[0]
    validate(source, faithful)
    local_expression = (
        "правоохранители обязаны "
        + joined_actions(
            f"при санкции суда {action_a}",
            action_connector,
            f"при санкции прокурора {action_b}",
        )
    )
    unsafe_expressions = [
        expression.replace(
            formula,
            "при санкции суда и при санкции прокурора",
            1,
        ),
        expression.replace(formula, "при санкции суда", 1),
        f"{formula} правоохранители обязаны "
        + joined_actions(action_a, wrong_connector, action_b),
        local_expression,
    ]
    if not action_reorder_is_safe:
        unsafe_expressions.append(
            f"{formula} правоохранители обязаны "
            + joined_actions(action_b, action_connector, action_a)
        )
    for unsafe_expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_QUOTED_DOCUMENT_OWNER_AGREEMENT_CASES = (
    (
        "лицензия организации «Сертификат»",
        "аннулирована",
        "аннулирован",
        "лицензия организации «Паспорт»",
        "feminine-license",
    ),
    (
        "лицензии организации «Пропуск»",
        "аннулированы",
        "аннулирована",
        "лицензии организации «Допуск»",
        "plural-licenses",
    ),
    (
        "пропуск организации «Лицензия»",
        "аннулирован",
        "аннулирована",
        "пропуск организации «Разрешение»",
        "masculine-pass",
    ),
    (
        "разрешение организации «Паспорт»",
        "аннулировано",
        "аннулирован",
        "разрешение организации «Сертификат»",
        "neuter-authorization",
    ),
)


@pytest.mark.parametrize(
    (
        "patient",
        "predicate",
        "wrong_predicate",
        "changed_patient",
        "case_id",
    ),
    _CLOSED_UK_QUOTED_DOCUMENT_OWNER_AGREEMENT_CASES,
    ids=tuple(
        case[4]
        for case in _CLOSED_UK_QUOTED_DOCUMENT_OWNER_AGREEMENT_CASES
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
def test_v3_closed_uk_passive_agreement_uses_typed_head_before_quoted_owner(
    patient: str,
    predicate: str,
    wrong_predicate: str,
    changed_patient: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patient,
        predicate,
    ) is True
    assert _relation_document_patient_passive_agrees(
        patient,
        wrong_predicate,
    ) is False
    source = (
        f"Суд проверил документ и {patient} {predicate} прокуратурой."
    )
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    unsafe_outputs = (
        source.replace(patient, changed_patient, 1).rstrip("."),
        source.replace(f" {predicate} ", f" {wrong_predicate} ", 1)
        .rstrip("."),
        source.replace(
            patient[patient.index(" «"):],
            "",
            1,
        ).rstrip("."),
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_ACTIVE_OVS_DOCUMENT_CASES = (
    (
        "лицензию должна аннулировать прокуратура",
        "лицензия должна аннулировать прокуратура",
        "modal-feminine-accusative",
    ),
    (
        "лицензию не должна аннулировать прокуратура",
        "лицензия не должна аннулировать прокуратура",
        "negative-modal-feminine-accusative",
    ),
    (
        "лицензию аннулировала прокуратура",
        "лицензия аннулировала прокуратура",
        "finite-feminine-accusative",
    ),
    (
        "лицензию не аннулировала прокуратура",
        "лицензия не аннулировала прокуратура",
        "negative-finite-feminine-accusative",
    ),
    (
        "пропуск должна аннулировать прокуратура",
        "",
        "modal-syncretic-masculine",
    ),
    (
        "разрешение должна аннулировать прокуратура",
        "",
        "modal-syncretic-neuter",
    ),
    (
        "лицензии должна аннулировать прокуратура",
        "",
        "modal-syncretic-plural",
    ),
    (
        "пропуск аннулировала прокуратура",
        "",
        "finite-syncretic-masculine",
    ),
    (
        "разрешение аннулировала прокуратура",
        "",
        "finite-syncretic-neuter",
    ),
    (
        "лицензии аннулировала прокуратура",
        "",
        "finite-syncretic-plural",
    ),
)


@pytest.mark.parametrize(
    ("tail", "unsafe_tail", "case_id"),
    _CLOSED_UK_ACTIVE_OVS_DOCUMENT_CASES,
    ids=tuple(case[2] for case in _CLOSED_UK_ACTIVE_OVS_DOCUMENT_CASES),
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
def test_v3_closed_uk_active_ovs_requires_document_object_and_postposed_actor(
    tail: str,
    unsafe_tail: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = f"Суд проверил документ и {tail}."
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    if unsafe_tail:
        unsafe = f"Суд проверил документ и {unsafe_tail}"
        unsafe_connector = unsafe.index(" и ")
        assert not any(
            start <= unsafe_connector < end
            for start, end in _relation_separator_spans(unsafe)
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_FRESH_ACTIVE_ACTOR_FENCE_CASES = (
    (
        "Суд обязан аннулировать лицензию на оружие, и комиссия "
        "изымает служебный пропуск",
        ("суд", "комисси"),
        ("mandatory", "definite"),
        (
            "Комиссия обязана аннулировать лицензию на оружие, и суд "
            "изымает служебный пропуск",
            "Суд обязан аннулировать лицензию на оружие, и изымает "
            "служебный пропуск",
            "Суд обязан аннулировать лицензию на оружие, и комиссия "
            "должна изъять служебный пропуск",
        ),
        (),
        "fresh-commission-without-local-modal",
    ),
    (
        "Суд обязан аннулировать лицензию на оружие, и департамент "
        "должен изъять служебный пропуск",
        ("суд", "департамент"),
        ("mandatory", "mandatory"),
        (
            "Департамент обязан аннулировать лицензию на оружие, и суд "
            "должен изъять служебный пропуск",
            "Суд обязан аннулировать лицензию на оружие, и должен "
            "изъять служебный пропуск",
            "Суд обязан аннулировать лицензию на оружие, и департамент "
            "изымает служебный пропуск",
        ),
        (),
        "fresh-department-with-local-modal",
    ),
    (
        "Суд обязан аннулировать лицензию на оружие, и суд изымает "
        "служебный пропуск",
        ("суд", "суд"),
        ("mandatory", "definite"),
        (
            "Суд обязан аннулировать лицензию на оружие и изъять "
            "служебный пропуск",
            "Суд обязан аннулировать лицензию на оружие, и комиссия "
            "изымает служебный пропуск",
            "Суд обязан аннулировать лицензию на оружие, и суд должен "
            "изъять служебный пропуск",
        ),
        (),
        "repeated-court-without-local-modal",
    ),
    (
        "Суд обязан аннулировать лицензию на оружие, и суд должен "
        "изъять служебный пропуск",
        ("суд", "суд"),
        ("mandatory", "mandatory"),
        (
            "Суд обязан аннулировать лицензию на оружие, и комиссия "
            "должна изъять служебный пропуск",
            "Суд обязан аннулировать лицензию на оружие, и суд изымает "
            "служебный пропуск",
        ),
        (
            "Суд обязан аннулировать лицензию на оружие и изъять "
            "служебный пропуск",
        ),
        "repeated-court-with-local-modal",
    ),
)


@pytest.mark.parametrize(
    (
        "expression",
        "actor_fragments",
        "modalities",
        "unsafe_expressions",
        "ellipsis_expressions",
        "case_id",
    ),
    _CLOSED_UK_FRESH_ACTIVE_ACTOR_FENCE_CASES,
    ids=tuple(
        case[5] for case in _CLOSED_UK_FRESH_ACTIVE_ACTOR_FENCE_CASES
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
def test_v3_closed_uk_fresh_active_actor_fences_inherited_actor_and_modal(
    expression: str,
    actor_fragments: tuple[str, str],
    modalities: tuple[str, str],
    unsafe_expressions: tuple[str, ...],
    ellipsis_expressions: tuple[str, ...],
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert tuple(record.modality for record in records) == modalities
    for record, actor_fragment in zip(records, actor_fragments):
        assert actor_fragment in "|".join(record.actors)
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    connector = source.index(", и")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    faithful_expressions = (expression, *ellipsis_expressions)
    for faithful_expression in faithful_expressions:
        faithful = _closed_uk_correlative_tree_surface(
            faithful_expression,
            compact_ground=True,
        )
        faithful_records = _closed_uk_revocation_action_records(faithful)
        assert len(faithful_records) == 2
        assert tuple(
            record.modality for record in faithful_records
        ) == modalities
        validate(source, faithful)
    for unsafe_expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_DECISION_PASSIVE_AGREEMENT_CASES = (
    (
        "лицензии",
        "изымаются",
        "лицензии",
        "изымается",
        "present-plural",
    ),
    (
        "лицензия",
        "должна быть изъята",
        "лицензия",
        "должны быть изъяты",
        "modal-singular",
    ),
)


@pytest.mark.parametrize(
    (
        "patient",
        "predicate",
        "bad_patient",
        "bad_predicate",
        "case_id",
    ),
    _CLOSED_UK_DECISION_PASSIVE_AGREEMENT_CASES,
    ids=tuple(
        case[4] for case in _CLOSED_UK_DECISION_PASSIVE_AGREEMENT_CASES
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
def test_v3_closed_uk_decision_boundary_preserves_passive_agreement(
    patient: str,
    predicate: str,
    bad_patient: str,
    bad_predicate: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patient,
        predicate,
    ) is True
    assert _relation_document_patient_passive_agrees(
        bad_patient,
        bad_predicate,
    ) is False
    left = (
        "суд обязан аннулировать лицензию на оружие по совместному "
        "решению суда и прокуратуры"
    )
    expression = f"{left} и {patient} {predicate} полицией"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert "полици" in "|".join(records[1].actors)
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    connector = source.index(f" и {patient}", source.index("решению"))
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, faithful)
    unsafe_expression = f"{left} и {bad_patient} {bad_predicate} полицией"
    unsafe = _closed_uk_correlative_tree_surface(
        unsafe_expression,
        compact_ground=True,
    )
    unsafe_connector = unsafe.index(
        f" и {bad_patient}",
        unsafe.index("решению"),
    )
    assert not any(
        start <= unsafe_connector < end
        for start, end in _relation_separator_spans(unsafe)
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_LEGAL_PLURAL_DOCUMENT_FORMS = (
    ("пропуска", "legal-access-pass"),
    ("пропуски", "regular-access-pass"),
    ("ордера", "legal-warrant"),
    ("ордеры", "regular-warrant"),
    ("паспорта", "legal-passport"),
    ("паспорты", "regular-passport"),
)


@pytest.mark.parametrize(
    ("patient", "case_id"),
    _CLOSED_UK_LEGAL_PLURAL_DOCUMENT_FORMS,
    ids=tuple(case[1] for case in _CLOSED_UK_LEGAL_PLURAL_DOCUMENT_FORMS),
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
def test_v3_closed_uk_legal_plural_forms_work_in_passive_and_active_ovs(
    patient: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patient,
        "аннулированы",
    ) is True
    assert _relation_document_patient_is_active_object(patient) is True
    passive = (
        f"Суд проверил документ и {patient} аннулированы прокуратурой."
    )
    active = (
        f"Суд проверил документ и {patient} аннулировала прокуратура."
    )
    for source in (passive, active):
        connector = source.index(" и ")
        assert any(
            start <= connector < end
            for start, end in _relation_separator_spans(source)
        )
        validate(source, source.rstrip("."))


_CLOSED_UK_MODIFIED_PLURAL_DISAMBIGUATION_CASES = (
    (
        "лицензии",
        "должны быть аннулированы",
        "действующей лицензии",
        "bare-license-modal",
    ),
    (
        "действующие лицензии",
        "аннулированы",
        "действующей лицензии",
        "modified-license-short",
    ),
    (
        "пропуска",
        "должны быть аннулированы",
        "действующего пропуска",
        "bare-access-pass-modal",
    ),
    (
        "пропуска",
        "аннулированы",
        "действующего пропуска",
        "bare-access-pass-short",
    ),
)


@pytest.mark.parametrize(
    ("patient", "predicate", "bad_patient", "case_id"),
    _CLOSED_UK_MODIFIED_PLURAL_DISAMBIGUATION_CASES,
    ids=tuple(
        case[3]
        for case in _CLOSED_UK_MODIFIED_PLURAL_DISAMBIGUATION_CASES
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
def test_v3_closed_uk_modifier_disambiguates_plural_document_patient(
    patient: str,
    predicate: str,
    bad_patient: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patient,
        predicate,
    ) is True
    assert _relation_document_patient_passive_agrees(
        bad_patient,
        predicate,
    ) is False
    source = (
        f"Суд проверил документ и {patient} {predicate} прокуратурой."
    )
    unsafe = (
        f"Суд проверил документ и {bad_patient} {predicate} прокуратурой"
    )
    connector = source.index(" и ")
    unsafe_connector = unsafe.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= unsafe_connector < end
        for start, end in _relation_separator_spans(unsafe)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_NEAREST_OR_PRESENT_AGREEMENT_CASES = (
    (
        "лицензии или пропуск",
        "аннулируется",
        "аннулируются",
        "plural-then-singular",
    ),
    (
        "пропуск или лицензии",
        "аннулируются",
        "аннулируется",
        "singular-then-plural",
    ),
    (
        "лицензии или пропуска",
        "аннулируются",
        "аннулируется",
        "plural-then-plural",
    ),
    (
        "пропуск или лицензия",
        "аннулируется",
        "аннулируются",
        "singular-then-singular",
    ),
)


@pytest.mark.parametrize(
    ("patients", "predicate", "bad_predicate", "case_id"),
    _CLOSED_UK_NEAREST_OR_PRESENT_AGREEMENT_CASES,
    ids=tuple(
        case[3] for case in _CLOSED_UK_NEAREST_OR_PRESENT_AGREEMENT_CASES
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
def test_v3_closed_uk_pure_or_present_agrees_with_nearest_branch(
    patients: str,
    predicate: str,
    bad_predicate: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patients,
        predicate,
    ) is True
    assert _relation_document_patient_passive_agrees(
        patients,
        bad_predicate,
    ) is False
    source = (
        f"Суд проверил документ и {patients} {predicate} прокуратурой."
    )
    unsafe = source.replace(predicate, bad_predicate, 1).rstrip(".")
    spans = _relation_separator_spans(source)
    outer_connector = source.index(" и ")
    inner_connector = source.index(" или ")
    assert any(
        start <= outer_connector < end for start, end in spans
    )
    assert not any(
        start <= inner_connector < end for start, end in spans
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_OPEN_ACTOR_TYPED_ROLE_CASES = (
    (
        "при санкции прокурора комиссия изымает служебный пропуск",
        "комиссия",
        "прокуратура",
        "при санкции прокурора",
        "при санкции губернатора",
        "при санкции прокурора комиссия должна изъять служебный пропуск",
        "authorization-before-actor",
    ),
    (
        "комиссия при санкции прокурора изымает служебный пропуск",
        "комиссия",
        "прокуратура",
        "при санкции прокурора",
        "при санкции губернатора",
        "комиссия при санкции прокурора должна изъять служебный пропуск",
        "authorization-after-actor",
    ),
    (
        "по пятницам департамент изымает служебный пропуск",
        "департамент",
        "губернатор",
        "по пятницам",
        "по субботам",
        "по пятницам департамент должен изъять служебный пропуск",
        "weekday-before-actor",
    ),
    (
        "департамент по пятницам изымает служебный пропуск",
        "департамент",
        "губернатор",
        "по пятницам",
        "по субботам",
        "департамент по пятницам должен изъять служебный пропуск",
        "weekday-after-actor",
    ),
    (
        "по решению суда комиссия изымает служебный пропуск",
        "комиссия",
        "прокуратура",
        "по решению суда",
        "по решению губернатора",
        "по решению суда комиссия должна изъять служебный пропуск",
        "decision-before-actor",
    ),
    (
        "комиссия по решению суда изымает служебный пропуск",
        "комиссия",
        "прокуратура",
        "по решению суда",
        "по решению губернатора",
        "комиссия по решению суда должна изъять служебный пропуск",
        "decision-after-actor",
    ),
    (
        "при условии уведомления владельца департамент изымает "
        "служебный пропуск",
        "департамент",
        "губернатор",
        "при условии уведомления владельца",
        "при условии согласия владельца",
        "при условии уведомления владельца департамент должен изъять "
        "служебный пропуск",
        "condition-before-actor",
    ),
    (
        "департамент при условии уведомления владельца изымает "
        "служебный пропуск",
        "департамент",
        "губернатор",
        "при условии уведомления владельца",
        "при условии согласия владельца",
        "департамент при условии уведомления владельца должен изъять "
        "служебный пропуск",
        "condition-after-actor",
    ),
)


@pytest.mark.parametrize(
    (
        "right_branch",
        "actor",
        "changed_actor",
        "role",
        "changed_role",
        "modalized_right_branch",
        "case_id",
    ),
    _CLOSED_UK_OPEN_ACTOR_TYPED_ROLE_CASES,
    ids=tuple(
        case[6] for case in _CLOSED_UK_OPEN_ACTOR_TYPED_ROLE_CASES
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
def test_v3_closed_uk_typed_role_keeps_fresh_open_actor_and_local_modality(
    right_branch: str,
    actor: str,
    changed_actor: str,
    role: str,
    changed_role: str,
    modalized_right_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left = "Суд обязан аннулировать лицензию на оружие"
    expression = f"{left}, и {right_branch}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert tuple(record.modality for record in records) == (
        "mandatory",
        "definite",
    )
    assert "суд" in "|".join(records[0].actors)
    assert actor[:-1] in "|".join(records[1].actors)
    assert records[0].actors != records[1].actors
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    connector = source.index(", и")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, faithful)
    unsafe_right_branches = (
        right_branch.replace(f"{actor} ", "", 1),
        right_branch.replace(actor, changed_actor, 1),
        right_branch.replace(role, "", 1),
        right_branch.replace(role, changed_role, 1),
        modalized_right_branch,
    )
    for unsafe_right_branch in unsafe_right_branches:
        unsafe_expression = f"{left}, и {unsafe_right_branch}"
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_GLOBAL_FORMULA_MIDDLE_CASES = (
    ("between-actor-and-modal", "и", "conjunctive"),
    ("between-actor-and-modal", "либо", "alternative"),
    ("between-actor-and-modal", ", а затем", "sequence"),
    ("after-modal", "и", "conjunctive"),
    ("after-modal", "либо", "alternative"),
    ("after-modal", ", а затем", "sequence"),
)


@pytest.mark.parametrize(
    ("position", "action_connector", "bridge_kind"),
    _CLOSED_UK_GLOBAL_FORMULA_MIDDLE_CASES,
    ids=tuple(
        f"{case[0]}-{case[2]}"
        for case in _CLOSED_UK_GLOBAL_FORMULA_MIDDLE_CASES
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
def test_v3_closed_uk_middle_multi_authorization_formula_is_global(
    position: str,
    action_connector: str,
    bridge_kind: str,
    validate: RelationValidator,
) -> None:
    formula = "при санкции суда или при санкции прокурора"
    action_a = "аннулировать лицензию на оружие"
    action_b = "изъять служебный пропуск"
    separator = (
        f"{action_connector} "
        if action_connector.startswith(",")
        else f" {action_connector} "
    )
    actions = f"{action_a}{separator}{action_b}"
    if position == "between-actor-and-modal":
        expression = f"Правоохранители {formula} обязаны {actions}"
    else:
        expression = f"Правоохранители обязаны {formula} {actions}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == len(contexts) == 2
    assert _closed_uk_bridge_kinds(source) == (bridge_kind,)
    global_spans = _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    assert global_spans == frozenset(
        (context.start, context.end) for context in contexts
    )
    formulas = _closed_uk_authorization_formula_contracts(
        source,
        records,
        contexts,
        global_spans,
    )
    assert len(formulas) == 1
    assert ":global:alternative:" in formulas[0]
    validate(source, faithful)
    local_formula = (
        f"Правоохранители обязаны при санкции суда {action_a}"
        f"{separator}при санкции прокурора {action_b}"
    )
    unsafe_expressions = (
        expression.replace(
            "при санкции суда или при санкции прокурора",
            "при санкции суда и при санкции прокурора",
            1,
        ),
        expression.replace(" или при санкции прокурора", "", 1),
        local_formula,
    )
    for unsafe_expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_EXPLICIT_NEW_CLAUSE_CONNECTORS = (
    (", при этом ", "discourse-continuation"),
    (", причём ", "discourse-emphasis"),
    (", и ", "comma-and"),
)


@pytest.mark.parametrize(
    ("connector", "case_id"),
    _CLOSED_UK_EXPLICIT_NEW_CLAUSE_CONNECTORS,
    ids=tuple(case[1] for case in _CLOSED_UK_EXPLICIT_NEW_CLAUSE_CONNECTORS),
)
@pytest.mark.parametrize(
    "voice",
    ("active-ovs", "passive"),
    ids=("active-ovs", "passive"),
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
def test_v3_closed_uk_explicit_connector_starts_complete_document_clause(
    connector: str,
    case_id: str,
    voice: str,
    validate: RelationValidator,
) -> None:
    del case_id
    if voice == "active-ovs":
        clause = "лицензию аннулировала прокуратура"
        wrong_case = "лицензия аннулировала прокуратура"
        wrong_agreement = "лицензию аннулировал прокуратура"
        wrong_performer = "лицензию аннулировал губернатор"
        assert _relation_document_patient_is_active_object(
            "лицензию"
        ) is True
        assert _relation_document_patient_is_active_object(
            "лицензия"
        ) is False
    else:
        clause = "лицензия аннулирована прокуратурой"
        wrong_case = "лицензию аннулирована прокуратурой"
        wrong_agreement = "лицензия аннулирован прокуратурой"
        wrong_performer = "лицензия аннулирована губернатором"
        assert _relation_document_patient_passive_agrees(
            "лицензия",
            "аннулирована",
        ) is True
        assert _relation_document_patient_passive_agrees(
            "лицензия",
            "аннулирован",
        ) is False
    source = f"Суд проверил документ{connector}{clause}."
    connector_offset = source.index(connector)
    assert any(
        start <= connector_offset < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for unsafe_clause in (
        wrong_case,
        wrong_agreement,
        wrong_performer,
    ):
        unsafe = f"Суд проверил документ{connector}{unsafe_clause}"
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
def test_v3_closed_uk_dopuski_plural_does_not_admit_genitive_dopuska(
    validate: RelationValidator,
) -> None:
    assert _relation_document_patient_passive_agrees(
        "допуски",
        "аннулированы",
    ) is True
    assert _relation_document_patient_passive_agrees(
        "допуска",
        "аннулированы",
    ) is False
    assert _relation_document_patient_is_active_object("допуски") is True
    assert _relation_document_patient_is_active_object("допуска") is False
    positive_clauses = (
        "допуски аннулированы прокуратурой",
        "допуски аннулировала прокуратура",
    )
    unsafe_clauses = (
        "допуска аннулированы прокуратурой",
        "допуска аннулировала прокуратура",
    )
    for clause, unsafe_clause in zip(positive_clauses, unsafe_clauses):
        source = f"Суд проверил документ и {clause}."
        connector = source.index(" и ")
        assert any(
            start <= connector < end
            for start, end in _relation_separator_spans(source)
        )
        validate(source, source.rstrip("."))
        unsafe = f"Суд проверил документ и {unsafe_clause}"
        unsafe_connector = unsafe.index(" и ")
        assert not any(
            start <= unsafe_connector < end
            for start, end in _relation_separator_spans(unsafe)
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_GENERIC_ACTIVE_OVS_PATIENT_CASES = (
    (
        "организацию",
        "организация",
        "закрыла",
        "закрыть",
        "прокуратура",
        "organization",
    ),
    (
        "территорию",
        "территория",
        "закрыла",
        "закрыть",
        "прокуратура",
        "territory",
    ),
    (
        "гражданина",
        "гражданин",
        "задержала",
        "задержать",
        "полиция",
        "citizen",
    ),
)
_CLOSED_UK_GENERIC_ACTIVE_OVS_CONNECTORS = (
    (" и ", "bare-and"),
    (", и ", "comma-and"),
    (", при этом ", "explicit-continuation"),
)


@pytest.mark.parametrize(
    (
        "patient",
        "nominative",
        "finite",
        "infinitive",
        "actor",
        "patient_id",
    ),
    _CLOSED_UK_GENERIC_ACTIVE_OVS_PATIENT_CASES,
    ids=tuple(
        case[5] for case in _CLOSED_UK_GENERIC_ACTIVE_OVS_PATIENT_CASES
    ),
)
@pytest.mark.parametrize(
    ("connector", "connector_id"),
    _CLOSED_UK_GENERIC_ACTIVE_OVS_CONNECTORS,
    ids=tuple(
        case[1] for case in _CLOSED_UK_GENERIC_ACTIVE_OVS_CONNECTORS
    ),
)
@pytest.mark.parametrize(
    ("modal", "adverb"),
    (
        (False, ""),
        (False, "немедленно "),
        (True, ""),
        (True, "немедленно "),
    ),
    ids=("finite", "finite-adverb", "modal", "modal-adverb"),
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
def test_v3_closed_uk_generic_active_ovs_requires_accusative_patient(
    patient: str,
    nominative: str,
    finite: str,
    infinitive: str,
    actor: str,
    patient_id: str,
    connector: str,
    connector_id: str,
    modal: bool,
    adverb: str,
    validate: RelationValidator,
) -> None:
    del patient_id, connector_id
    assert _relation_bounded_patient_expression_is_supported(patient)
    predicate = (
        f"должна {adverb}{infinitive}"
        if modal
        else f"{adverb}{finite}"
    )
    clause = f"{patient} {predicate} {actor}"
    unsafe_clause = f"{nominative} {predicate} {actor}"
    source = f"Суд проверил документ{connector}{clause}."
    unsafe = f"Суд проверил документ{connector}{unsafe_clause}"
    connector_offset = source.index(connector)
    unsafe_connector = unsafe.index(connector)
    assert any(
        start <= connector_offset < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= unsafe_connector < end
        for start, end in _relation_separator_spans(unsafe)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_COMPLETE_LEFT_CLAUSE_CASES = (
    (
        "лицензия аннулирована судом",
        "служебный пропуск изъят прокуратурой",
        "лицензия аннулирована прокуратурой",
        "служебный пропуск изъят судом",
        "служебный пропуск аннулирован судом",
        "лицензия изъята прокуратурой",
        "судом",
        "прокуратурой",
        "лицензия",
        "служебный пропуск",
        "passive-to-passive",
    ),
    (
        "лицензия аннулирована судом",
        "служебный пропуск изъяла прокуратура",
        "лицензия аннулирована прокуратурой",
        "служебный пропуск изъял суд",
        "служебный пропуск аннулирован судом",
        "лицензию изъяла прокуратура",
        "судом",
        "прокуратура",
        "лицензия",
        "служебный пропуск",
        "passive-to-active-ovs",
    ),
    (
        "лицензию аннулировал суд",
        "служебный пропуск изъят прокуратурой",
        "лицензию аннулировала прокуратура",
        "служебный пропуск изъят судом",
        "служебный пропуск аннулировал суд",
        "лицензия изъята прокуратурой",
        "суд",
        "прокуратурой",
        "лицензию",
        "служебный пропуск",
        "active-ovs-to-passive",
    ),
    (
        "лицензию аннулировал суд",
        "служебный пропуск изъяла прокуратура",
        "лицензию аннулировала прокуратура",
        "служебный пропуск изъял суд",
        "служебный пропуск аннулировал суд",
        "лицензию изъяла прокуратура",
        "суд",
        "прокуратура",
        "лицензию",
        "служебный пропуск",
        "active-ovs-to-active-ovs",
    ),
)


@pytest.mark.parametrize(
    (
        "left",
        "right",
        "actor_swapped_left",
        "actor_swapped_right",
        "patient_swapped_left",
        "patient_swapped_right",
        "left_actor",
        "right_actor",
        "left_patient",
        "right_patient",
        "case_id",
    ),
    _CLOSED_UK_COMPLETE_LEFT_CLAUSE_CASES,
    ids=tuple(
        case[10] for case in _CLOSED_UK_COMPLETE_LEFT_CLAUSE_CASES
    ),
)
@pytest.mark.parametrize(
    "connector",
    (" и ", ", и "),
    ids=("bare-and", "comma-and"),
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
def test_v3_closed_uk_complete_left_clause_survives_outer_and_boundary(
    left: str,
    right: str,
    actor_swapped_left: str,
    actor_swapped_right: str,
    patient_swapped_left: str,
    patient_swapped_right: str,
    left_actor: str,
    right_actor: str,
    left_patient: str,
    right_patient: str,
    case_id: str,
    connector: str,
    validate: RelationValidator,
) -> None:
    del case_id
    expression = f"{left}{connector}{right}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    assert len(records) == 2
    assert _closed_uk_bridge_kinds(source) == ("conjunctive",)
    connector_offset = source.index(connector)
    assert any(
        start <= connector_offset < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, faithful)
    left_without_actor = left.replace(f" {left_actor}", "", 1)
    right_without_actor = right.replace(f" {right_actor}", "", 1)
    left_without_patient = left.replace(left_patient, "", 1)
    right_without_patient = right.replace(right_patient, "", 1)
    unsafe_expressions = (
        f"{left_without_actor}{connector}{right}",
        f"{left}{connector}{right_without_actor}",
        f"{left_without_patient}{connector}{right}",
        f"{left}{connector}{right_without_patient}",
        f"{actor_swapped_left}{connector}{actor_swapped_right}",
        f"{patient_swapped_left}{connector}{patient_swapped_right}",
    )
    for unsafe_expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_TRAILING_AUTH_NON_GLOBAL_CASES = (
    (
        "Правоохранители обязаны аннулировать договор аренды и изъять "
        "лицензию на оружие при санкции суда",
        2,
        (
            "при санкции суда Правоохранители обязаны аннулировать договор "
            "аренды и изъять лицензию на оружие",
            "Правоохранители обязаны при санкции суда аннулировать договор "
            "аренды и изъять лицензию на оружие",
            "Правоохранители обязаны изъять лицензию на оружие и "
            "аннулировать договор аренды при санкции суда",
            "Правоохранители обязаны аннулировать лицензию на оружие и "
            "изъять договор аренды при санкции суда",
        ),
        "two-actions-unknown-first-patient",
    ),
    (
        "Правоохранители обязаны аннулировать регистрацию организации, "
        "приостановить территорию и изъять лицензию на оружие при санкции "
        "суда",
        3,
        (
            "при санкции суда Правоохранители обязаны аннулировать "
            "регистрацию организации, приостановить территорию и изъять "
            "лицензию на оружие",
            "Правоохранители обязаны при санкции суда аннулировать "
            "регистрацию организации, приостановить территорию и изъять "
            "лицензию на оружие",
            "Правоохранители обязаны изъять лицензию на оружие, "
            "приостановить территорию и аннулировать регистрацию "
            "организации при санкции суда",
            "Правоохранители обязаны аннулировать лицензию на оружие, "
            "приостановить территорию и изъять регистрацию организации "
            "при санкции суда",
        ),
        "three-actions-open-first-patients",
    ),
    (
        "Правоохранители обязаны аннулировать по заявлению владельца и "
        "изъять лицензию на оружие при санкции суда",
        2,
        (
            "при санкции суда Правоохранители обязаны аннулировать по "
            "заявлению владельца и изъять лицензию на оружие",
            "Правоохранители обязаны аннулировать по заявлению владельца "
            "при санкции суда и изъять лицензию на оружие",
            "Правоохранители обязаны аннулировать и изъять лицензию на "
            "оружие при санкции суда",
            "Правоохранители обязаны аннулировать и изъять лицензию на "
            "оружие по заявлению владельца при санкции суда",
        ),
        "two-actions-residual-before-bridge",
    ),
    (
        "Правоохранители обязаны аннулировать по заявлению владельца, "
        "приостановить после уведомления суда и изъять лицензию на оружие "
        "при санкции суда",
        3,
        (
            "при санкции суда Правоохранители обязаны аннулировать по "
            "заявлению владельца, приостановить после уведомления суда и "
            "изъять лицензию на оружие",
            "Правоохранители обязаны аннулировать по заявлению владельца "
            "при санкции суда, приостановить после уведомления суда и "
            "изъять лицензию на оружие",
            "Правоохранители обязаны аннулировать, приостановить и изъять "
            "лицензию на оружие при санкции суда",
            "Правоохранители обязаны аннулировать, приостановить и изъять "
            "лицензию на оружие по заявлению владельца после уведомления "
            "суда при санкции суда",
        ),
        "three-actions-residuals-before-bridges",
    ),
)


@pytest.mark.parametrize(
    ("expression", "record_count", "unsafe_expressions", "case_id"),
    _CLOSED_UK_TRAILING_AUTH_NON_GLOBAL_CASES,
    ids=tuple(
        case[3] for case in _CLOSED_UK_TRAILING_AUTH_NON_GLOBAL_CASES
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
def test_v3_closed_uk_trailing_auth_is_not_global_across_unshared_patient(
    expression: str,
    record_count: int,
    unsafe_expressions: tuple[str, ...],
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == record_count
    assert len(contexts) == 1
    assert _closed_uk_bridge_kinds(source) == tuple(
        "conjunctive" for _ in range(record_count - 1)
    )
    assert not _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    )
    validate(source, faithful)
    for unsafe_expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_TRAILING_AUTH_TRUE_SHARED_CASES = (
    (
        "Правоохранители обязаны аннулировать и изъять лицензию на "
        "оружие при санкции суда",
        "Правоохранители обязаны изъять и аннулировать лицензию на "
        "оружие при санкции суда",
        "при санкции суда Правоохранители обязаны изъять и аннулировать "
        "лицензию на оружие",
        2,
        (
            "Правоохранители обязаны аннулировать лицензию на оружие при "
            "санкции суда и изъять лицензию на оружие",
            "Правоохранители обязаны аннулировать служебный пропуск и "
            "изъять лицензию на оружие при санкции суда",
        ),
        "two-actions",
    ),
    (
        "Правоохранители обязаны аннулировать, приостановить и изъять "
        "лицензию на оружие при санкции суда",
        "Правоохранители обязаны изъять, аннулировать и приостановить "
        "лицензию на оружие при санкции суда",
        "при санкции суда Правоохранители обязаны изъять, аннулировать и "
        "приостановить лицензию на оружие",
        3,
        (
            "Правоохранители обязаны аннулировать лицензию на оружие при "
            "санкции суда, приостановить лицензию на оружие и изъять "
            "лицензию на оружие",
            "Правоохранители обязаны аннулировать служебный пропуск, "
            "приостановить и изъять лицензию на оружие при санкции суда",
        ),
        "three-actions",
    ),
)


@pytest.mark.parametrize(
    (
        "expression",
        "reordered",
        "leading",
        "record_count",
        "local_or_changed",
        "case_id",
    ),
    _CLOSED_UK_TRAILING_AUTH_TRUE_SHARED_CASES,
    ids=tuple(
        case[5] for case in _CLOSED_UK_TRAILING_AUTH_TRUE_SHARED_CASES
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
def test_v3_closed_uk_true_shared_trailing_auth_is_global(
    expression: str,
    reordered: str,
    leading: str,
    record_count: int,
    local_or_changed: tuple[str, ...],
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    records = _closed_uk_revocation_action_records(source)
    contexts = _closed_uk_authorization_contexts(source)
    assert len(records) == record_count
    assert len(contexts) == 1
    assert _closed_uk_bridge_kinds(source) == tuple(
        "conjunctive" for _ in range(record_count - 1)
    )
    assert _closed_uk_global_authorization_context_spans(
        source,
        records,
        contexts,
    ) == frozenset({(contexts[0].start, contexts[0].end)})
    for faithful_expression in (expression, reordered, leading):
        faithful = _closed_uk_correlative_tree_surface(
            faithful_expression,
            compact_ground=True,
        )
        validate(source, faithful)
    unsafe_expressions = (
        expression.replace("санкции суда", "санкции губернатора", 1),
        *local_or_changed,
    )
    for unsafe_expression in unsafe_expressions:
        unsafe = _closed_uk_correlative_tree_surface(
            unsafe_expression,
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_SINGLE_PATIENT_INTERNAL_CONNECTOR_CASES = (
    (
        "лицензия суда и прокуратуры",
        " и прокуратуры",
        "coordinated-owner",
    ),
    (
        "лицензия на оружие или спецсредства",
        " или спецсредства",
        "alternative-scope",
    ),
)


@pytest.mark.parametrize(
    ("patient", "inner_needle", "case_id"),
    _CLOSED_UK_SINGLE_PATIENT_INTERNAL_CONNECTOR_CASES,
    ids=tuple(
        case[2]
        for case in _CLOSED_UK_SINGLE_PATIENT_INTERNAL_CONNECTOR_CASES
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
def test_v3_closed_uk_internal_owner_or_scope_does_not_pluralize_patient(
    patient: str,
    inner_needle: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patient,
        "аннулирована",
    ) is True
    assert _relation_document_patient_passive_agrees(
        patient,
        "аннулированы",
    ) is False
    source = (
        f"Суд проверил документ и {patient} аннулирована комиссией."
    )
    unsafe = (
        f"Суд проверил документ и {patient} аннулированы комиссией"
    )
    outer_connector = source.index(" и ")
    inner_connector = source.index(inner_needle, outer_connector + 3)
    spans = _relation_separator_spans(source)
    assert any(
        start <= outer_connector < end for start, end in spans
    )
    assert not any(
        start <= inner_connector < end for start, end in spans
    )
    unsafe_outer = unsafe.index(" и ")
    assert not any(
        start <= unsafe_outer < end
        for start, end in _relation_separator_spans(unsafe)
    )
    validate(source, source.rstrip("."))
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
def test_v3_closed_uk_two_owned_document_patients_require_plural_predicate(
    validate: RelationValidator,
) -> None:
    patient = "лицензия суда и пропуск прокуратуры"
    assert _relation_document_patient_passive_agrees(
        patient,
        "аннулированы",
    ) is True
    assert _relation_document_patient_passive_agrees(
        patient,
        "аннулирована",
    ) is False
    source = (
        f"Суд проверил документ и {patient} аннулированы комиссией."
    )
    outer_connector = source.index(" и ")
    inner_connector = source.index(" и пропуск", outer_connector + 3)
    spans = _relation_separator_spans(source)
    assert any(
        start <= outer_connector < end for start, end in spans
    )
    assert not any(
        start <= inner_connector < end for start, end in spans
    )
    validate(source, source.rstrip("."))
    unsafe = source.replace("аннулированы", "аннулирована", 1).rstrip(".")
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_MODIFIED_GENERIC_ACTIVE_OVS_CASES = (
    (
        "действующую организацию",
        "действующая организация",
        "закрыла",
        "закрыть",
        "прокуратура",
        "modified-organization",
    ),
    (
        "охраняемую территорию",
        "охраняемая территория",
        "закрыла",
        "закрыть",
        "прокуратура",
        "modified-territory",
    ),
    (
        "разыскиваемого гражданина",
        "разыскиваемый гражданин",
        "задержала",
        "задержать",
        "полиция",
        "wanted-citizen",
    ),
    (
        "данные владельца",
        "данных владельца",
        "передала",
        "передать",
        "прокуратура",
        "information-data",
    ),
)


@pytest.mark.parametrize(
    (
        "patient",
        "wrong_case",
        "finite",
        "infinitive",
        "actor",
        "case_id",
    ),
    _CLOSED_UK_MODIFIED_GENERIC_ACTIVE_OVS_CASES,
    ids=tuple(
        case[5] for case in _CLOSED_UK_MODIFIED_GENERIC_ACTIVE_OVS_CASES
    ),
)
@pytest.mark.parametrize(
    "modal",
    (False, True),
    ids=("finite", "modal"),
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
def test_v3_closed_uk_modified_generic_active_ovs_keeps_patient_case(
    patient: str,
    wrong_case: str,
    finite: str,
    infinitive: str,
    actor: str,
    case_id: str,
    modal: bool,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_bounded_patient_expression_is_supported(patient)
    predicate = f"должна {infinitive}" if modal else finite
    source = (
        f"Суд проверил документ и {patient} {predicate} {actor}."
    )
    unsafe = (
        f"Суд проверил документ и {wrong_case} {predicate} {actor}"
    )
    connector = source.index(" и ")
    unsafe_connector = unsafe.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= unsafe_connector < end
        for start, end in _relation_separator_spans(unsafe)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_DECISION_MODAL_PASSIVE_AGREEMENT_CASES = (
    (
        "лицензия",
        "",
        "должна",
        "быть",
        "аннулирована",
        "лицензию",
        "должны",
        "аннулирован",
        "singular",
    ),
    (
        "лицензия",
        "",
        "не должна",
        "быть",
        "аннулирована",
        "лицензию",
        "не должны",
        "аннулирован",
        "negative-singular",
    ),
    (
        "лицензии",
        "",
        "должны",
        "быть",
        "аннулированы",
        "лицензий",
        "должна",
        "аннулирована",
        "plural",
    ),
    (
        "лицензия",
        "немедленно ",
        "должна",
        "быть",
        "аннулирована",
        "лицензию",
        "должны",
        "аннулирован",
        "neutral-before-modal",
    ),
    (
        "лицензия",
        "",
        "должна",
        "немедленно быть",
        "аннулирована",
        "лицензию",
        "должны",
        "аннулирован",
        "neutral-after-modal",
    ),
)


@pytest.mark.parametrize(
    (
        "patient",
        "premodal",
        "governor",
        "auxiliary",
        "participle",
        "wrong_patient",
        "wrong_governor",
        "wrong_participle",
        "case_id",
    ),
    _CLOSED_UK_DECISION_MODAL_PASSIVE_AGREEMENT_CASES,
    ids=tuple(
        case[8]
        for case in _CLOSED_UK_DECISION_MODAL_PASSIVE_AGREEMENT_CASES
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
def test_v3_closed_uk_decision_boundary_requires_complete_modal_passive(
    patient: str,
    premodal: str,
    governor: str,
    auxiliary: str,
    participle: str,
    wrong_patient: str,
    wrong_governor: str,
    wrong_participle: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left = "суд обязан изъять пропуск по решению суда"

    def right_branch(
        *,
        branch_patient: str = patient,
        branch_governor: str = governor,
        branch_participle: str = participle,
    ) -> str:
        return (
            f"{branch_patient} {premodal}{branch_governor} {auxiliary} "
            f"{branch_participle} прокуратурой"
        )

    expression = f"{left} и {right_branch()}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    governor_surface = governor.removeprefix("не ")
    wrong_governor_surface = wrong_governor.removeprefix("не ")
    assert _relation_document_patient_passive_agrees(
        patient,
        governor_surface,
    ) is True
    assert _relation_document_patient_passive_agrees(
        patient,
        participle,
    ) is True
    assert _relation_document_patient_passive_agrees(
        wrong_patient,
        governor_surface,
    ) is False
    assert _relation_document_patient_passive_agrees(
        patient,
        wrong_governor_surface,
    ) is False
    assert _relation_document_patient_passive_agrees(
        patient,
        wrong_participle,
    ) is False
    validate(source, faithful)

    malformed_branches = (
        right_branch(branch_patient=wrong_patient),
        right_branch(branch_governor=wrong_governor),
        right_branch(branch_participle=wrong_participle),
    )
    for malformed_branch in malformed_branches:
        malformed_expression = f"{left} и {malformed_branch}"
        malformed = _closed_uk_correlative_tree_surface(
            malformed_expression,
            compact_ground=True,
        )
        malformed_connector = malformed.index(" и ")
        assert not any(
            start <= malformed_connector < end
            for start, end in _relation_separator_spans(malformed)
        )
        with pytest.raises(ValueError):
            validate(source, malformed)

    polarity_governor = (
        governor.removeprefix("не ")
        if governor.startswith("не ")
        else f"не {governor}"
    )
    changed_polarity = _closed_uk_correlative_tree_surface(
        f"{left} и {right_branch(branch_governor=polarity_governor)}",
        compact_ground=True,
    )
    with pytest.raises(ValueError):
        validate(source, changed_polarity)


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
def test_v3_closed_uk_negated_noun_is_not_a_decision_boundary_actor(
    validate: RelationValidator,
) -> None:
    left = "суд обязан изъять пропуск по решению суда"
    right = "комиссия должна аннулировать лицензию на оружие"
    source = _closed_uk_correlative_tree_surface(
        f"{left} и {right}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{left} и {right}",
        compact_ground=True,
    )
    unsafe = _closed_uk_correlative_tree_surface(
        f"{left} и не {right}",
        compact_ground=True,
    )
    source_connector = source.index(" и комиссия")
    unsafe_connector = unsafe.index(" и не комиссия")
    assert any(
        start <= source_connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= unsafe_connector < end
        for start, end in _relation_separator_spans(unsafe)
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_GENERIC_PASSIVE_PATIENT_CASES = (
    (
        "организация",
        "закрыта",
        "прокуратурой",
        "организацию",
        "закрыт",
        "закрыты",
        "organization",
    ),
    (
        "территория",
        "закрыта",
        "прокуратурой",
        "территорию",
        "закрыт",
        "закрыты",
        "territory",
    ),
    (
        "гражданин",
        "задержан",
        "полицией",
        "гражданина",
        "задержана",
        "задержаны",
        "person",
    ),
)


@pytest.mark.parametrize(
    (
        "patient",
        "predicate",
        "performer",
        "wrong_case",
        "wrong_gender",
        "wrong_number",
        "case_id",
    ),
    _CLOSED_UK_GENERIC_PASSIVE_PATIENT_CASES,
    ids=tuple(case[6] for case in _CLOSED_UK_GENERIC_PASSIVE_PATIENT_CASES),
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
def test_v3_closed_uk_generic_passive_requires_nominative_and_agreement(
    patient: str,
    predicate: str,
    performer: str,
    wrong_case: str,
    wrong_gender: str,
    wrong_number: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    clause = f"{patient} {predicate} {performer}"
    source = f"Суд проверил документ и {clause}."
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    for malformed_clause in (
        f"{wrong_case} {predicate} {performer}",
        f"{patient} {wrong_gender} {performer}",
        f"{patient} {wrong_number} {performer}",
    ):
        unsafe = f"Суд проверил документ и {malformed_clause}"
        unsafe_connector = unsafe.index(" и ")
        assert not any(
            start <= unsafe_connector < end
            for start, end in _relation_separator_spans(unsafe)
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_COMPOUND_TEMPORAL_WRAPPERS = (
    ("", "", "plain"),
    ("(", ")", "parenthesized"),
)


@pytest.mark.parametrize(
    ("open_wrapper", "close_wrapper", "case_id"),
    _CLOSED_UK_COMPOUND_TEMPORAL_WRAPPERS,
    ids=tuple(case[2] for case in _CLOSED_UK_COMPOUND_TEMPORAL_WRAPPERS),
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
def test_v3_compound_temporal_keeps_both_subordinate_events_and_operator(
    open_wrapper: str,
    close_wrapper: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    body = (
        "суд уведомил владельца и прокуратура аннулировала лицензию"
    )

    def sentence(inner: str) -> str:
        return (
            f"После того как {open_wrapper}{inner}{close_wrapper}, "
            "полиция обязана закрыть дело"
        )

    source = sentence(body) + "."
    spans = _relation_compound_temporal_subordinate_spans(source)
    assert spans
    for needle in (
        "уведомил",
        " и прокуратура",
        "аннулировала",
    ):
        offset = source.index(needle)
        assert any(start <= offset < end for start, end in spans)
    validate(source, source.rstrip("."))
    unsafe_bodies = (
        "прокуратура аннулировала лицензию",
        "суд уведомил владельца",
        "суд уведомил владельца или прокуратура аннулировала лицензию",
        "суд аннулировал лицензию и прокуратура уведомила владельца",
        "прокуратура уведомила владельца и суд аннулировал лицензию",
    )
    for unsafe_body in unsafe_bodies:
        with pytest.raises(ValueError):
            validate(source, sentence(unsafe_body))


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
def test_v3_malformed_compound_temporal_intro_does_not_mask_whole_clause(
    validate: RelationValidator,
) -> None:
    source = (
        "После того как, суд аннулировал лицензию и прокуратура изъяла "
        "пропуск."
    )
    assert not _relation_compound_temporal_subordinate_spans(source)
    connector = source.index(" и прокуратура")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    unsafe = source.replace(" и прокуратура", " и суд", 1).rstrip(".")
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_PRODUCTIVE_IRREGULAR_SHORT_PASSIVE_CASES = (
    (
        "требования отвергнуты",
        "требования",
        "требования затронуты",
        "rejected-requirements",
    ),
    (
        "нормы затронуты",
        "нормы",
        "нормы развернуты",
        "affected-norms",
    ),
    (
        "положения развернуты",
        "положения",
        "положения отвергнуты",
        "expanded-provisions",
    ),
)


@pytest.mark.parametrize(
    ("second_branch", "dropped_predicate", "changed_branch", "case_id"),
    _CLOSED_UK_PRODUCTIVE_IRREGULAR_SHORT_PASSIVE_CASES,
    ids=tuple(
        case[3]
        for case in _CLOSED_UK_PRODUCTIVE_IRREGULAR_SHORT_PASSIVE_CASES
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
def test_v3_productive_short_passive_is_an_independent_condition_clause(
    second_branch: str,
    dropped_predicate: str,
    changed_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    parts = (first_branch, second_branch)
    assert _relation_condition_shared_head_parts(parts) == parts
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {second_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {second_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    for unsafe_branch in (dropped_predicate, changed_branch):
        unsafe = _closed_uk_boolean_condition_surface(
            f"{first_branch} или {unsafe_branch}",
            compact_ground=True,
        )
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_IRREGULAR_PASSIVE_NOMINAL_CONTROLS = (
    (
        "установлены маршруты движения",
        "маршруты эвакуации",
        "routes",
    ),
    (
        "установлены институты контроля",
        "институты надзора",
        "institutions",
    ),
)


@pytest.mark.parametrize(
    ("first_branch", "elliptical_branch", "case_id"),
    _CLOSED_UK_IRREGULAR_PASSIVE_NOMINAL_CONTROLS,
    ids=tuple(
        case[2]
        for case in _CLOSED_UK_IRREGULAR_PASSIVE_NOMINAL_CONTROLS
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
def test_v3_irregular_passive_lexicon_keeps_nominal_head_ellipsis(
    first_branch: str,
    elliptical_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    predicate = first_branch.split(maxsplit=1)[0]
    expanded_branch = f"{predicate} {elliptical_branch}"
    assert _relation_condition_shared_head_parts(
        (first_branch, elliptical_branch)
    ) == (first_branch, expanded_branch)
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {expanded_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {elliptical_branch}",
        compact_ground=True,
    )
    validate(source, faithful)


_CLOSED_UK_TRAILING_PATIENT_FIRST_ROLE_CASES = (
    (
        "лицензия аннулирована",
        "при санкции прокурора",
        "при санкции губернатора",
        " и ",
        "passive-authorization",
    ),
    (
        "разрешение изъято",
        "при задержании",
        "после задержания",
        " и ",
        "passive-detention",
    ),
    (
        "лицензия аннулирована",
        "по решению суда",
        "по решению губернатора",
        " и ",
        "passive-decision",
    ),
    (
        "лицензия аннулирована",
        "в пятницу",
        "в субботу",
        ", при этом ",
        "explicit-weekday",
    ),
)


@pytest.mark.parametrize(
    (
        "patient_clause",
        "role",
        "changed_role",
        "connector",
        "case_id",
    ),
    _CLOSED_UK_TRAILING_PATIENT_FIRST_ROLE_CASES,
    ids=tuple(
        case[4]
        for case in _CLOSED_UK_TRAILING_PATIENT_FIRST_ROLE_CASES
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
def test_v3_patient_first_passive_owns_its_complete_trailing_role(
    patient_clause: str,
    role: str,
    changed_role: str,
    connector: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = f"Суд проверил документ{connector}{patient_clause} {role}."
    outer_connector = source.index(connector)
    assert any(
        start <= outer_connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    unsafe_outputs = (
        f"Суд проверил документ{connector}{patient_clause}",
        f"Суд проверил документ{connector}{patient_clause} {changed_role}",
        f"Суд {role} проверил документ{connector}{patient_clause}",
        (
            f"Суд {role} проверил документ{connector}{patient_clause} "
            f"{role}"
        ),
        (
            f"Суд проверил документ{connector}{patient_clause} {role} "
            "при условии согласия владельца"
        ),
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_LEADING_PATIENT_ROLE_CASES = (
    (
        "при санкции суда",
        "лицензия аннулирована прокуратурой",
        "при санкции губернатора",
        "лицензию аннулирована прокуратурой",
        "passive-authorization",
    ),
    (
        "при задержании",
        "разрешение изъято полицией",
        "после задержания",
        "разрешению изъято полицией",
        "passive-detention",
    ),
    (
        "(после уведомления сторон)",
        "лицензия аннулирована прокуратурой",
        "(после согласия сторон)",
        "лицензию аннулирована прокуратурой",
        "wrapped-temporal",
    ),
    (
        "при санкции суда",
        "лицензию должна аннулировать прокуратура",
        "при санкции губернатора",
        "лицензия должна аннулировать прокуратура",
        "prospective-active-ovs",
    ),
    (
        "при санкции суда",
        "лицензию аннулировала прокуратура",
        "при санкции губернатора",
        "лицензия аннулировала прокуратура",
        "finite-active-ovs",
    ),
)


@pytest.mark.parametrize(
    (
        "role",
        "patient_clause",
        "changed_role",
        "wrong_patient_clause",
        "case_id",
    ),
    _CLOSED_UK_LEADING_PATIENT_ROLE_CASES,
    ids=tuple(case[4] for case in _CLOSED_UK_LEADING_PATIENT_ROLE_CASES),
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
def test_v3_leading_role_is_owned_before_patient_first_passive_or_ovs(
    role: str,
    patient_clause: str,
    changed_role: str,
    wrong_patient_clause: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = f"Суд проверил документ и {role} {patient_clause}."
    outer_connector = source.index(" и ")
    assert any(
        start <= outer_connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    unsafe_outputs = (
        f"Суд проверил документ и {patient_clause}",
        f"Суд проверил документ и {changed_role} {patient_clause}",
        f"Суд {role} проверил документ и {patient_clause}",
        f"Суд {role} проверил документ и {role} {patient_clause}",
        f"Суд проверил документ и {role} {wrong_patient_clause}",
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_PATIENT_FIRST_INTERNAL_ROLE_CASES = (
    (
        "при санкции суда и прокуратуры",
        "при санкции суда или прокуратуры",
        " и прокуратуры",
        " и прокуратуры",
        "leading-joint-authority",
    ),
    (
        "при санкции суда или при санкции прокурора",
        "при санкции суда и при санкции прокурора",
        " или при санкции прокурора",
        " или при санкции прокурора",
        "leading-alternative-authority",
    ),
    (
        "при санкции суда и прокуратуры",
        "при санкции суда или прокуратуры",
        " и прокуратуры",
        " и прокуратуры",
        "trailing-joint-authority",
    ),
    (
        "при санкции суда или при санкции прокурора",
        "при санкции суда и при санкции прокурора",
        " или при санкции прокурора",
        " или при санкции прокурора",
        "trailing-alternative-authority",
    ),
)


@pytest.mark.parametrize(
    (
        "role",
        "changed_role",
        "inner_needle",
        "dropped_member",
        "case_id",
    ),
    _CLOSED_UK_PATIENT_FIRST_INTERNAL_ROLE_CASES,
    ids=tuple(case[4] for case in _CLOSED_UK_PATIENT_FIRST_INTERNAL_ROLE_CASES),
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
def test_v3_patient_first_role_formula_keeps_its_inner_operator_internal(
    role: str,
    changed_role: str,
    inner_needle: str,
    dropped_member: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    trailing = case_id.startswith("trailing")
    patient_clause = (
        "лицензия аннулирована"
        if trailing
        else "лицензия аннулирована прокуратурой"
    )

    def right_branch(role_surface: str) -> str:
        if trailing:
            return f"{patient_clause} {role_surface}"
        return f"{role_surface} {patient_clause}"

    source = f"Суд проверил документ и {right_branch(role)}."
    spans = _relation_separator_spans(source)
    outer_connector = source.index(" и ")
    inner_connector = source.index(inner_needle, outer_connector + 3)
    assert any(
        start <= outer_connector < end for start, end in spans
    )
    assert not any(
        start <= inner_connector < end for start, end in spans
    )
    validate(source, source.rstrip("."))
    dropped_role = role.replace(dropped_member, "", 1)
    unsafe_outputs = (
        f"Суд проверил документ и {right_branch(changed_role)}",
        f"Суд проверил документ и {right_branch(dropped_role)}",
        f"Суд {role} проверил документ и {patient_clause}",
        f"Суд {role} проверил документ и {right_branch(role)}",
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_MALFORMED_LEADING_PATIENT_ROLE_CASES = (
    (
        "при санкции суда лицензия аннулирована прокуратурой",
        "при санкции, лицензия аннулирована прокуратурой",
        "missing-authority",
    ),
    (
        "при санкции суда или при санкции прокурора лицензия "
        "аннулирована прокуратурой",
        "при санкции суда или лицензия аннулирована прокуратурой",
        "unfinished-alternative",
    ),
    (
        "(после уведомления сторон) лицензия аннулирована прокуратурой",
        "(после уведомления сторон лицензия аннулирована прокуратурой",
        "unclosed-wrapper",
    ),
)


@pytest.mark.parametrize(
    ("valid_right", "malformed_right", "case_id"),
    _CLOSED_UK_MALFORMED_LEADING_PATIENT_ROLE_CASES,
    ids=tuple(
        case[2]
        for case in _CLOSED_UK_MALFORMED_LEADING_PATIENT_ROLE_CASES
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
def test_v3_malformed_leading_role_is_not_blank_masked_as_owned(
    valid_right: str,
    malformed_right: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = f"Суд проверил документ и {valid_right}."
    unsafe = f"Суд проверил документ и {malformed_right}"
    source_connector = source.index(" и ")
    unsafe_connector = unsafe.index(" и ")
    assert any(
        start <= source_connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= unsafe_connector < end
        for start, end in _relation_separator_spans(unsafe)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_PRODUCTIVE_SHORT_PASSIVE_AGREEMENT_CASES = (
    (
        "требования отвергнуты",
        "требования отвергнуто",
        "requirements-neuter",
    ),
    (
        "норма затронута",
        "норма затронуты",
        "norm-plural",
    ),
    (
        "положение развернуто",
        "положение развернута",
        "provision-feminine",
    ),
)


def test_v3_productive_short_passive_requires_subject_agreement_direct(
) -> None:
    first_branch = "нарушены правила хранения"
    for correct_branch, malformed_branch, _case_id in (
        _CLOSED_UK_PRODUCTIVE_SHORT_PASSIVE_AGREEMENT_CASES
    ):
        assert _relation_condition_shared_head_parts(
            (first_branch, correct_branch)
        ) == (first_branch, correct_branch)
        assert _relation_condition_shared_head_parts(
            (first_branch, malformed_branch)
        ) == (first_branch, f"нарушены {malformed_branch}")


@pytest.mark.parametrize(
    ("correct_branch", "malformed_branch", "case_id"),
    _CLOSED_UK_PRODUCTIVE_SHORT_PASSIVE_AGREEMENT_CASES,
    ids=tuple(
        case[2]
        for case in _CLOSED_UK_PRODUCTIVE_SHORT_PASSIVE_AGREEMENT_CASES
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
def test_v3_productive_short_passive_rejects_wrong_agreement_end_to_end(
    correct_branch: str,
    malformed_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {correct_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {correct_branch}",
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {malformed_branch}",
        compact_ground=True,
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
def test_v3_minuty_noun_keeps_shared_condition_head_inheritance(
    validate: RelationValidator,
) -> None:
    first_branch = "нарушены условия хранения"
    elliptical_branch = "условия минуты молчания"
    expanded_branch = "нарушены условия минуты молчания"
    assert _relation_condition_shared_head_parts(
        (first_branch, elliptical_branch)
    ) == (first_branch, expanded_branch)
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {expanded_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {elliptical_branch}",
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        f"{first_branch} или условия молчания",
        compact_ground=True,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_AUDITED_PASSIVE_AUXILIARY_CASES = (
    (
        "лицензия",
        "будет",
        "",
        "аннулирована",
        ("будут",),
        "аннулированы",
        "future-singular",
    ),
    (
        "лицензии",
        "будут",
        "",
        "аннулированы",
        ("будет",),
        "аннулирована",
        "future-plural",
    ),
    (
        "лицензия",
        "была",
        "",
        "аннулирована",
        ("были", "был"),
        "аннулированы",
        "past-singular",
    ),
    (
        "лицензии",
        "были",
        "",
        "аннулированы",
        ("была",),
        "аннулирована",
        "past-plural",
    ),
    (
        "лицензия",
        "имеет право",
        "быть",
        "аннулирована",
        ("имеют право",),
        "аннулирован",
        "entitlement-singular",
    ),
    (
        "лицензии",
        "имеют право",
        "быть",
        "аннулированы",
        ("имеет право",),
        "аннулирована",
        "entitlement-plural",
    ),
)


@pytest.mark.parametrize(
    (
        "patient",
        "governor",
        "linker",
        "participle",
        "wrong_governors",
        "wrong_participle",
        "case_id",
    ),
    _CLOSED_UK_AUDITED_PASSIVE_AUXILIARY_CASES,
    ids=tuple(
        case[6] for case in _CLOSED_UK_AUDITED_PASSIVE_AUXILIARY_CASES
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
def test_v3_decision_boundary_requires_passive_auxiliary_agreement(
    patient: str,
    governor: str,
    linker: str,
    participle: str,
    wrong_governors: tuple[str, ...],
    wrong_participle: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left = "суд обязан изъять пропуск по решению суда"

    def right_branch(
        *,
        branch_governor: str = governor,
        branch_participle: str = participle,
    ) -> str:
        predicate = " ".join(
            part
            for part in (branch_governor, linker, branch_participle)
            if part
        )
        return f"{patient} {predicate} прокуратурой"

    expression = f"{left} и {right_branch()}"
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert _relation_document_patient_passive_agrees(
        patient,
        governor,
    ) is True
    assert _relation_document_patient_passive_agrees(
        patient,
        participle,
    ) is True
    for wrong_governor in wrong_governors:
        assert _relation_document_patient_passive_agrees(
            patient,
            wrong_governor,
        ) is False
    assert _relation_document_patient_passive_agrees(
        patient,
        wrong_participle,
    ) is False
    validate(source, faithful)

    malformed_branches = (
        *(right_branch(branch_governor=item) for item in wrong_governors),
        right_branch(branch_participle=wrong_participle),
    )
    for malformed_branch in malformed_branches:
        malformed = _closed_uk_correlative_tree_surface(
            f"{left} и {malformed_branch}",
            compact_ground=True,
        )
        malformed_connector = malformed.index(" и ")
        assert not any(
            start <= malformed_connector < end
            for start, end in _relation_separator_spans(malformed)
        )
        with pytest.raises(ValueError):
            validate(source, malformed)


_CLOSED_UK_PRODUCTIVE_PASSIVE_ORDER_AND_GAP_CASES = (
    (
        "отвергнуты требования допуска",
        "отвергнуто требования допуска",
        "predicate-first",
    ),
    (
        "затронута норма",
        "затронуты норма",
        "predicate-first-feminine",
    ),
    (
        "требования ранее отвергнуты",
        "требования ранее отвергнуто",
        "bounded-status-gap",
    ),
    (
        "требования не отвергнуты",
        "требования не отвергнуто",
        "bounded-negation-gap",
    ),
)


@pytest.mark.parametrize(
    ("correct_branch", "malformed_branch", "case_id"),
    _CLOSED_UK_PRODUCTIVE_PASSIVE_ORDER_AND_GAP_CASES,
    ids=tuple(
        case[2]
        for case in _CLOSED_UK_PRODUCTIVE_PASSIVE_ORDER_AND_GAP_CASES
    ),
)
def test_v3_productive_passive_order_and_gap_require_agreement_direct(
    correct_branch: str,
    malformed_branch: str,
    case_id: str,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    assert _relation_condition_shared_head_parts(
        (first_branch, correct_branch)
    ) == (first_branch, correct_branch)
    assert _relation_condition_shared_head_parts(
        (first_branch, malformed_branch)
    ) == (first_branch, f"нарушены {malformed_branch}")


@pytest.mark.parametrize(
    ("correct_branch", "malformed_branch", "case_id"),
    _CLOSED_UK_PRODUCTIVE_PASSIVE_ORDER_AND_GAP_CASES,
    ids=tuple(
        case[2]
        for case in _CLOSED_UK_PRODUCTIVE_PASSIVE_ORDER_AND_GAP_CASES
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
def test_v3_productive_passive_order_and_gap_fail_closed_end_to_end(
    correct_branch: str,
    malformed_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {correct_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {correct_branch}",
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {malformed_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_STANDALONE_SHORT_PASSIVE_AGREEMENT_CASES = (
    ("условия сняты", "условия снят", "conditions-removed"),
    ("условия даны", "условия дан", "conditions-given"),
)


@pytest.mark.parametrize(
    ("correct_branch", "malformed_branch", "case_id"),
    _CLOSED_UK_STANDALONE_SHORT_PASSIVE_AGREEMENT_CASES,
    ids=tuple(
        case[2]
        for case in _CLOSED_UK_STANDALONE_SHORT_PASSIVE_AGREEMENT_CASES
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
def test_v3_standalone_short_passive_requires_agreement_end_to_end(
    correct_branch: str,
    malformed_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    first_branch = "нарушены правила хранения"
    assert _relation_condition_shared_head_parts(
        (first_branch, correct_branch)
    ) == (first_branch, correct_branch)
    assert _relation_condition_shared_head_parts(
        (first_branch, malformed_branch)
    ) == (first_branch, f"нарушены {malformed_branch}")
    source = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {correct_branch}",
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {correct_branch}",
        compact_ground=True,
    )
    unsafe = _closed_uk_boolean_condition_surface(
        f"{first_branch} или {malformed_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "predicate",
    ("закрыт", "закрыта", "закрыто", "закрыты"),
)
def test_v3_closed_passive_forms_are_exact_bounded_finite_candidates(
    predicate: str,
) -> None:
    surface = f"территория {predicate} прокуратурой"
    candidates = tuple(
        match.group(0).casefold()
        for match, irregular in _relation_bounded_predicate_candidates(
            surface
        )
        if irregular
    )
    assert predicate in candidates
    attributive = f"закрытая территория {predicate} прокуратурой"
    attributive_candidates = tuple(
        match.group(0).casefold()
        for match, irregular in _relation_bounded_predicate_candidates(
            attributive
        )
        if irregular
    )
    assert "закрытая" not in attributive_candidates


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
def test_v3_compound_temporal_main_ovs_keeps_all_roles_and_operator(
    validate: RelationValidator,
) -> None:
    source = (
        "После того как суд уведомил владельца и прокуратура проверила "
        "документ, лицензию аннулировала прокуратура."
    )
    faithful = source.rstrip(".")
    faithful_svo = faithful.replace(
        "лицензию аннулировала прокуратура",
        "прокуратура аннулировала лицензию",
        1,
    )
    validate(source, faithful)
    validate(source, faithful_svo)
    unsafe_outputs = (
        faithful.replace(
            "лицензию аннулировала прокуратура",
            "лицензию аннулировал суд",
            1,
        ),
        faithful.replace(
            "лицензию аннулировала",
            "пропуск аннулировала",
            1,
        ),
        faithful.replace(
            "уведомил владельца и прокуратура",
            "уведомил владельца или прокуратура",
            1,
        ),
    )
    for unsafe in unsafe_outputs:
        with pytest.raises(ValueError):
            validate(source, unsafe)


_CLOSED_UK_ACTIVE_OVS_ACTOR_GENDER_CASES = (
    (
        "лицензию на оружие",
        "аннулировала",
        "аннулировал",
        "прокуратура",
        "finite-license-feminine",
    ),
    (
        "организацию",
        "закрыла",
        "закрыл",
        "прокуратура",
        "finite-organization-feminine",
    ),
    (
        "лицензию на оружие",
        "должна аннулировать",
        "должен аннулировать",
        "прокуратура",
        "modal-prosecution-feminine",
    ),
    (
        "лицензию на оружие",
        "должна аннулировать",
        "должен аннулировать",
        "комиссия",
        "modal-commission-feminine",
    ),
    (
        "лицензию на оружие",
        "аннулировал",
        "аннулировала",
        "суд",
        "finite-court-masculine",
    ),
)


@pytest.mark.parametrize(
    ("patient", "governor", "wrong_governor", "actor", "case_id"),
    _CLOSED_UK_ACTIVE_OVS_ACTOR_GENDER_CASES,
    ids=tuple(case[4] for case in _CLOSED_UK_ACTIVE_OVS_ACTOR_GENDER_CASES),
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
def test_v3_active_ovs_known_actor_requires_gender_agreement(
    patient: str,
    governor: str,
    wrong_governor: str,
    actor: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = f"Суд проверил документ и {patient} {governor} {actor}."
    faithful = source.rstrip(".")
    malformed = (
        f"Суд проверил документ и {patient} {wrong_governor} {actor}"
    )
    source_connector = source.index(" и ")
    malformed_connector = malformed.index(" и ")
    assert any(
        start <= source_connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= malformed_connector < end
        for start, end in _relation_separator_spans(malformed)
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, malformed)


@pytest.mark.parametrize(
    ("patient", "auxiliary", "modifier", "participle", "case_id"),
    (
        (
            "лицензия",
            "была",
            "немедленно",
            "аннулирована",
            "singular-neutral-adverb",
        ),
        (
            "лицензии",
            "были",
            "ранее",
            "аннулированы",
            "plural-local-status",
        ),
    ),
    ids=("singular-neutral-adverb", "plural-local-status"),
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
def test_v3_negated_past_passive_keeps_post_auxiliary_status_polarity(
    patient: str,
    auxiliary: str,
    modifier: str,
    participle: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = (
        f"Суд проверил документ и {patient} не {auxiliary} {modifier} "
        f"{participle} прокуратурой."
    )
    positive = source.replace(f" не {auxiliary}", f" {auxiliary}", 1).rstrip(
        "."
    )
    records = _closed_uk_revocation_action_records(source)
    assert tuple(record.polarity for record in records) == ("negative",)
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, positive)


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
def test_v3_pre_auxiliary_ne_ranee_does_not_negate_past_passive(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд проверил документ и лицензия не ранее была аннулирована "
        "прокуратурой."
    )
    negative = source.replace("не ранее была", "не была ранее", 1).rstrip(
        "."
    )
    records = _closed_uk_revocation_action_records(source)
    assert tuple(record.polarity for record in records) == ("positive",)
    negative_records = _closed_uk_revocation_action_records(negative)
    assert tuple(record.polarity for record in negative_records) == (
        "negative",
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, negative)


@pytest.mark.parametrize(
    ("governor", "actor"),
    (
        ("аннулировал", "FIB"),
        ("аннулировала", "FIB"),
        ("аннулировало", "бюро"),
        ("аннулировали", "бюро"),
        ("аннулировало", "федеральное бюро"),
        ("аннулировали", "федеральные бюро"),
    ),
    ids=(
        "ascii-masculine-context",
        "ascii-feminine-context",
        "bare-bureau-singular-context",
        "bare-bureau-plural-context",
        "modified-bureau-singular-context",
        "modified-bureau-plural-context",
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
def test_v3_active_ovs_unknown_or_number_neutral_actor_stays_open(
    governor: str,
    actor: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Суд проверил документ и лицензию на оружие "
        f"{governor} {actor}."
    )
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))


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
    "actor_group",
    ("прокуратура и суд", "прокуратура и FIB"),
    ids=("known-group", "mixed-ascii-group"),
)
def test_v3_active_ovs_joint_actor_keeps_existing_plural_rule(
    actor_group: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Суд проверил документ и лицензию на оружие аннулировали "
        f"{actor_group}."
    )
    connector = source.index(" и лицензию")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))


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
    ("correct_right", "malformed_right", "case_id"),
    (
        (
            "комиссия должна аннулировать лицензию на оружие",
            "комиссия должен аннулировать лицензию на оружие",
            "gendered-modal",
        ),
        (
            "прокуратура аннулировала лицензию на оружие",
            "прокуратура аннулировал лицензию на оружие",
            "finite-past",
        ),
    ),
    ids=("gendered-modal", "finite-past"),
)
def test_v3_decision_postposed_actor_requires_gender_agreement(
    correct_right: str,
    malformed_right: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    left = "суд обязан изъять пропуск по решению суда"
    source = _closed_uk_correlative_tree_surface(
        f"{left} и {correct_right}",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        f"{left} и {correct_right}",
        compact_ground=True,
    )
    malformed = _closed_uk_correlative_tree_surface(
        f"{left} и {malformed_right}",
        compact_ground=True,
    )
    source_connector = source.index(" и ")
    malformed_connector = malformed.index(" и ")
    assert any(
        start <= source_connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= malformed_connector < end
        for start, end in _relation_separator_spans(malformed)
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, malformed)


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
def test_v3_punctuated_decision_actor_boundary_remains_supported(
    validate: RelationValidator,
) -> None:
    expression = (
        "суд обязан изъять пропуск по решению суда, а комиссия должна "
        "аннулировать лицензию на оружие"
    )
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    connector = source.index(", а комиссия")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, faithful)


@pytest.mark.parametrize(
    ("patient", "auxiliary", "participle", "case_id"),
    (
        ("лицензия", "была", "аннулирована", "singular"),
        ("лицензии", "были", "аннулированы", "plural"),
    ),
    ids=("singular", "plural"),
)
@pytest.mark.parametrize(
    "negative",
    (False, True),
    ids=("positive", "negative"),
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
def test_v3_past_passive_auxiliary_keeps_local_polarity(
    patient: str,
    auxiliary: str,
    participle: str,
    case_id: str,
    negative: bool,
    validate: RelationValidator,
) -> None:
    del case_id
    prefix = "не " if negative else ""
    changed_prefix = "" if negative else "не "
    source = (
        f"Суд проверил документ и {patient} {prefix}{auxiliary} "
        f"{participle} прокуратурой."
    )
    changed = (
        f"Суд проверил документ и {patient} {changed_prefix}{auxiliary} "
        f"{participle} прокуратурой"
    )
    source_records = _closed_uk_revocation_action_records(source)
    assert tuple(record.polarity for record in source_records) == (
        "negative" if negative else "positive",
    )
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, changed)


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
def test_v3_single_ambiguous_decision_member_cannot_become_actor(
    validate: RelationValidator,
) -> None:
    source = _closed_uk_correlative_tree_surface(
        "прокуратура вправе изъять лицензию на оружие по решению суда",
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        "прокуратура вправе изъять лицензию на оружие по решению суда",
        compact_ground=True,
    )
    laundering = _closed_uk_correlative_tree_surface(
        "полиция по решению суда и прокуратуры вправе изъять "
        "лицензию на оружие",
        compact_ground=True,
    )
    ambiguous_connector = laundering.index(" и прокуратуры")
    assert not any(
        start <= ambiguous_connector < end
        for start, end in _relation_separator_spans(laundering)
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, laundering)


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
def test_v3_complete_compound_decision_authority_remains_supported(
    validate: RelationValidator,
) -> None:
    expression = (
        "полиция вправе изъять лицензию на оружие по совместному решению "
        "суда и прокуратуры"
    )
    source = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=False,
    )
    faithful = _closed_uk_correlative_tree_surface(
        expression,
        compact_ground=True,
    )
    authority_connector = source.index(" и прокуратуры")
    assert not any(
        start <= authority_connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, faithful)


@pytest.mark.parametrize(
    "malformed_patient",
    ("данными владельца", "данным владельца", "данных владельца"),
    ids=("instrumental", "dative", "genitive"),
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
def test_v3_data_patient_wrong_case_has_no_none_bypass(
    malformed_patient: str,
    validate: RelationValidator,
) -> None:
    patient = "данные владельца"
    source = f"Суд проверил документ и {patient} передала прокуратура."
    malformed = (
        f"Суд проверил документ и {malformed_patient} передала прокуратура"
    )
    assert _relation_document_patient_is_active_object(patient) is True
    assert (
        _relation_document_patient_is_active_object(malformed_patient)
        is False
    )
    connector = source.index(" и ")
    malformed_connector = malformed.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= malformed_connector < end
        for start, end in _relation_separator_spans(malformed)
    )

    def information_contract(branch: str) -> str:
        action = _RELATION_ACTION_RE.search(branch)
        assert action is not None
        projection = _predicate_relation(
            branch,
            action.start(),
            action.end(),
        )
        return _relation_patient_surface_contract(
            projection,
            action.start(),
            action.end(),
            direct_object="information",
            event="past",
        )

    valid_contract = information_contract(f"{patient} передала прокуратура")
    malformed_contract = information_contract(
        f"{malformed_patient} передала прокуратура"
    )
    assert valid_contract and not valid_contract.startswith("unresolved:")
    assert malformed_contract == "unresolved:information-patient-case"
    malformed_information_truths = tuple(
        truth
        for truth in _extract_relation_truths(malformed)
        if truth.predicate == "inform" and truth.direct_object == "information"
    )
    assert not malformed_information_truths or all(
        truth.patient_surface_contract.startswith("unresolved:")
        for truth in malformed_information_truths
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, malformed)


@pytest.mark.parametrize(
    "unsafe_patient",
    ("данные", "данные владельца"),
    ids=("owner-drop", "owner-change"),
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
def test_v3_data_patient_owner_is_exact_surface_contract(
    unsafe_patient: str,
    validate: RelationValidator,
) -> None:
    source = "Суд проверил документ и данные компании передала прокуратура."
    faithful = source.rstrip(".")
    unsafe = (
        f"Суд проверил документ и {unsafe_patient} передала прокуратура"
    )
    def information_contracts(value: str) -> frozenset[str]:
        return frozenset(
            truth.patient_surface_contract
            for truth in _extract_relation_truths(value)
            if (
                truth.predicate == "inform"
                and truth.direct_object == "information"
            )
        )

    source_contracts = information_contracts(source)
    unsafe_contracts = information_contracts(unsafe)
    assert source_contracts
    assert unsafe_contracts
    assert all(
        contract and not contract.startswith("unresolved:")
        for contract in source_contracts
    )
    assert source_contracts.isdisjoint(unsafe_contracts)
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, unsafe)


_CLOSED_UK_PATIENT_MODIFIER_AGREEMENT_CASES = (
    (
        "действующую организацию закрыла прокуратура",
        "действующий организацию закрыла прокуратура",
        "active-feminine-organization",
    ),
    (
        "разыскиваемого гражданина задержала полиция",
        "разыскиваемый гражданина задержала полиция",
        "active-animate-person",
    ),
    (
        "данная лицензия аннулирована прокуратурой",
        "данный лицензия аннулирована прокуратурой",
        "passive-feminine-license",
    ),
    (
        "охраняемая территория закрыта прокуратурой",
        "охраняемый территория закрыта прокуратурой",
        "passive-feminine-territory",
    ),
)


@pytest.mark.parametrize(
    ("right_branch", "malformed_branch", "case_id"),
    _CLOSED_UK_PATIENT_MODIFIER_AGREEMENT_CASES,
    ids=tuple(case[2] for case in _CLOSED_UK_PATIENT_MODIFIER_AGREEMENT_CASES),
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
def test_v3_modified_patient_requires_modifier_head_agreement(
    right_branch: str,
    malformed_branch: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = f"Суд проверил документ и {right_branch}."
    malformed = f"Суд проверил документ и {malformed_branch}"
    connector = source.index(" и ")
    malformed_connector = malformed.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= malformed_connector < end
        for start, end in _relation_separator_spans(malformed)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, malformed)


_CLOSED_UK_ACTIVE_PATIENT_INTERNAL_CONNECTOR_CASES = (
    (
        "лицензию суда и прокуратуры",
        "лицензию суда",
        "аннулировала комиссия",
        "joint-owner",
    ),
    (
        "лицензию на оружие или спецсредства",
        "лицензию на оружие",
        "аннулировала комиссия",
        "alternative-scope",
    ),
    (
        "данные владельца и заявителя",
        "данные владельца",
        "передала прокуратура",
        "joint-data-owner",
    ),
)


@pytest.mark.parametrize(
    ("patient", "changed_patient", "predicate_actor", "case_id"),
    _CLOSED_UK_ACTIVE_PATIENT_INTERNAL_CONNECTOR_CASES,
    ids=tuple(
        case[3] for case in _CLOSED_UK_ACTIVE_PATIENT_INTERNAL_CONNECTOR_CASES
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
def test_v3_active_patient_internal_connector_is_not_a_list_split(
    patient: str,
    changed_patient: str,
    predicate_actor: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    source = (
        f"Суд проверил документ и {patient} {predicate_actor}."
    )
    changed = (
        f"Суд проверил документ и {changed_patient} {predicate_actor}"
    )
    assert _relation_document_patient_is_active_object(patient) is True
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, changed)


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
def test_v3_nominative_feminine_institution_is_not_a_data_owner(
    validate: RelationValidator,
) -> None:
    source = "Суд проверил документ и данные прокуратуры передала комиссия."
    faithful = source.rstrip(".")
    laundering = "Суд проверил документ и данные прокуратура передала комиссия"
    assert (
        _relation_document_patient_is_active_object("данные прокуратуры")
        is True
    )
    assert (
        _relation_document_patient_is_active_object("данные прокуратура")
        is False
    )
    connector = faithful.index(" и ")
    laundering_connector = laundering.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(faithful)
    )
    assert not any(
        start <= laundering_connector < end
        for start, end in _relation_separator_spans(laundering)
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, laundering)


_CLOSED_UK_GENERIC_PASSIVE_PATIENT_LIST_CASES = (
    (
        "машина или территория",
        "задержана",
        "машина или участки",
        "задержана",
        "alternative-nearest-singular",
    ),
    (
        "организация и территория",
        "закрыты",
        "организация и территория",
        "закрыта",
        "conjunctive-plural",
    ),
)


@pytest.mark.parametrize(
    (
        "patient",
        "predicate",
        "wrong_patient",
        "wrong_predicate",
        "case_id",
    ),
    _CLOSED_UK_GENERIC_PASSIVE_PATIENT_LIST_CASES,
    ids=tuple(case[4] for case in _CLOSED_UK_GENERIC_PASSIVE_PATIENT_LIST_CASES),
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
def test_v3_generic_passive_patient_list_uses_every_typed_head(
    patient: str,
    predicate: str,
    wrong_patient: str,
    wrong_predicate: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    assert _relation_document_patient_passive_agrees(
        patient,
        predicate,
    ) is True
    source = (
        f"Суд проверил документ и {patient} {predicate} полицией."
    )
    connector = source.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    validate(source, source.rstrip("."))
    assert _relation_document_patient_passive_agrees(
        wrong_patient,
        wrong_predicate,
    ) is False
    malformed = (
        f"Суд проверил документ и {wrong_patient} {wrong_predicate} полицией"
    )
    malformed_connector = malformed.index(" и ")
    assert not any(
        start <= malformed_connector < end
        for start, end in _relation_separator_spans(malformed)
    )
    with pytest.raises(ValueError):
        validate(source, malformed)


@pytest.mark.parametrize(
    ("patient", "wrong_patient"),
    (
        ("организация", "организацию"),
        ("территория", "территорию"),
    ),
    ids=("organization", "territory"),
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
def test_v3_generic_passive_patient_pre_gate_rejects_accusative(
    patient: str,
    wrong_patient: str,
    validate: RelationValidator,
) -> None:
    predicate = "закрыта"
    assert _relation_document_patient_passive_agrees(
        patient,
        predicate,
    ) is True
    assert _relation_document_patient_passive_agrees(
        wrong_patient,
        predicate,
    ) is False
    source = (
        f"Суд проверил документ и {patient} {predicate} прокуратурой."
    )
    malformed = (
        f"Суд проверил документ и {wrong_patient} {predicate} прокуратурой"
    )
    connector = source.index(" и ")
    malformed_connector = malformed.index(" и ")
    assert any(
        start <= connector < end
        for start, end in _relation_separator_spans(source)
    )
    assert not any(
        start <= malformed_connector < end
        for start, end in _relation_separator_spans(malformed)
    )
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, malformed)


_V3_STANDALONE_ACTIVE_ACTOR_AGREEMENT_CASES = (
    (
        "Лицензию на оружие аннулировала федеральная прокуратура.",
        "Лицензию на оружие аннулировал федеральная прокуратура",
        "finite-governor-gender",
    ),
    (
        "Лицензию на оружие должна аннулировать федеральная прокуратура.",
        "Лицензию на оружие должен аннулировать федеральная прокуратура",
        "modal-governor-gender",
    ),
    (
        "Лицензию на оружие аннулировала федеральная прокуратура.",
        "Лицензию на оружие аннулировала федеральный прокуратура",
        "finite-actor-modifier",
    ),
    (
        "Лицензию на оружие должна аннулировать федеральная прокуратура.",
        "Лицензию на оружие должна аннулировать федеральный прокуратура",
        "modal-actor-modifier",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "case_id"),
    _V3_STANDALONE_ACTIVE_ACTOR_AGREEMENT_CASES,
    ids=tuple(
        case[2] for case in _V3_STANDALONE_ACTIVE_ACTOR_AGREEMENT_CASES
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
def test_v3_standalone_active_actor_agreement_is_fail_closed(
    source: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_V3_STANDALONE_PATIENT_MORPHOLOGY_CASES = (
    (
        "Данная лицензия аннулирована прокуратурой.",
        "Данный лицензия аннулирована прокуратурой",
        "passive-document-modifier",
    ),
    (
        "Разыскиваемого гражданина задержала полиция.",
        "Разыскиваемый гражданина задержала полиция",
        "active-animate-modifier",
    ),
    (
        "Данные владельца передала прокуратура.",
        "Данные владелец передала прокуратура",
        "single-owner-case",
    ),
    (
        "Данные владельца и заявителя передала прокуратура.",
        "Данные владельца и заявитель передала прокуратура",
        "coordinated-owner-case",
    ),
    (
        "Данные владельца и заявителя передала прокуратура.",
        "Данные владельца передала прокуратура",
        "coordinated-owner-member-drop",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "case_id"),
    _V3_STANDALONE_PATIENT_MORPHOLOGY_CASES,
    ids=tuple(
        case[2] for case in _V3_STANDALONE_PATIENT_MORPHOLOGY_CASES
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
def test_v3_standalone_patient_morphology_is_fail_closed(
    source: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_V3_STANDALONE_PASSIVE_AGREEMENT_CASES = (
    (
        "Организация закрыта прокуратурой.",
        "Организацию закрыта прокуратурой",
        "organization-patient-case",
    ),
    (
        "Организация закрыта прокуратурой.",
        "Организация закрыт прокуратурой",
        "organization-predicate-gender",
    ),
    (
        "Организации закрыты прокуратурой.",
        "Организации закрыта прокуратурой",
        "organization-predicate-number",
    ),
    (
        "Лицензия аннулирована прокуратурой.",
        "Лицензия аннулирован прокуратурой",
        "license-predicate-gender",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "case_id"),
    _V3_STANDALONE_PASSIVE_AGREEMENT_CASES,
    ids=tuple(
        case[2] for case in _V3_STANDALONE_PASSIVE_AGREEMENT_CASES
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
def test_v3_standalone_passive_patient_and_predicate_agree(
    source: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_V3_STANDALONE_GENERIC_PASSIVE_LIST_CASES = (
    (
        "Организация и территория закрыты прокуратурой.",
        "Организация закрыта прокуратурой",
        "conjunctive-member-drop",
    ),
    (
        "Организация и территория закрыты прокуратурой.",
        "Организация и машина закрыты прокуратурой",
        "conjunctive-member-replacement",
    ),
    (
        "Организация и территория закрыты прокуратурой.",
        "Организация и территория закрыта прокуратурой",
        "conjunctive-predicate-number",
    ),
    (
        "Машина или территория задержана полицией.",
        "Машина задержана полицией",
        "alternative-member-drop",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "case_id"),
    _V3_STANDALONE_GENERIC_PASSIVE_LIST_CASES,
    ids=tuple(
        case[2] for case in _V3_STANDALONE_GENERIC_PASSIVE_LIST_CASES
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
def test_v3_standalone_generic_passive_list_is_atomic(
    source: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_V3_STANDALONE_NEGATIVE_PAST_PASSIVE_CASES = (
    (
        "Лицензия фактически не была аннулирована прокуратурой.",
        "Лицензия фактически была аннулирована прокуратурой",
        "pre-negation-adverb",
    ),
    (
        "Лицензия не была фактически аннулирована прокуратурой.",
        "Лицензия была фактически аннулирована прокуратурой",
        "post-auxiliary-adverb",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "case_id"),
    _V3_STANDALONE_NEGATIVE_PAST_PASSIVE_CASES,
    ids=tuple(
        case[2] for case in _V3_STANDALONE_NEGATIVE_PAST_PASSIVE_CASES
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
def test_v3_standalone_negative_past_passive_keeps_polarity(
    source: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "active_branch",
    (
        "суды затронут требования допуска",
        "требования допуска затронут суды",
    ),
    ids=("subject-first-active", "object-first-active"),
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
def test_v3_productive_bare_nut_active_clause_cannot_replace_passive_condition(
    active_branch: str,
    validate: RelationValidator,
) -> None:
    source_condition = "нарушены правила хранения или требования затронуты"
    source = _closed_uk_boolean_condition_surface(
        source_condition,
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        source_condition,
        compact_ground=True,
    )
    laundering = _closed_uk_boolean_condition_surface(
        f"нарушены правила хранения или {active_branch}",
        compact_ground=True,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, laundering)


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
def test_v3_productive_bare_nut_keeps_generic_subject_first_active_clause(
    validate: RelationValidator,
) -> None:
    source_condition = "нарушены правила хранения или требования затронуты"
    source = _closed_uk_boolean_condition_surface(
        source_condition,
        compact_ground=False,
    )
    faithful = _closed_uk_boolean_condition_surface(
        source_condition,
        compact_ground=True,
    )
    laundering = _closed_uk_boolean_condition_surface(
        "нарушены правила хранения или требования вернут заявителю",
        compact_ground=True,
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, laundering)


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
def test_v3_active_self_state_uses_nominative_patient_subject(
    validate: RelationValidator,
) -> None:
    singular = "Лицензия прекращает свое действие."
    plural = "Все пропуски прекращают свое действие."
    validate(singular, singular.rstrip("."))
    validate(plural, plural.rstrip("."))
    with pytest.raises(ValueError):
        validate(singular, "Лицензию прекращает свое действие")
    with pytest.raises(ValueError):
        validate(plural, "Все пропуски прекращает свое действие")


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
def test_v3_standalone_inanimate_plural_patient_modifier_agrees(
    validate: RelationValidator,
) -> None:
    source = "Все новые лицензии аннулировала прокуратура."
    unsafe = "Все новых лицензии аннулировала прокуратура"
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)


_V3_STANDALONE_MODIFIED_BUREAU_AGREEMENT_CASES = (
    (
        "Лицензию на оружие аннулировало федеральное бюро.",
        "Лицензию на оружие аннулировали федеральное бюро",
        "singular-modifier",
    ),
    (
        "Лицензию на оружие аннулировали федеральные бюро.",
        "Лицензию на оружие аннулировало федеральные бюро",
        "plural-modifier",
    ),
)


@pytest.mark.parametrize(
    ("source", "unsafe", "case_id"),
    _V3_STANDALONE_MODIFIED_BUREAU_AGREEMENT_CASES,
    ids=tuple(
        case[2]
        for case in _V3_STANDALONE_MODIFIED_BUREAU_AGREEMENT_CASES
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
def test_v3_modified_bureau_actor_requires_resolved_number_agreement(
    source: str,
    unsafe: str,
    case_id: str,
    validate: RelationValidator,
) -> None:
    del case_id
    validate(source, source.rstrip("."))
    with pytest.raises(ValueError):
        validate(source, unsafe)
