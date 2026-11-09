from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _coordinated_passive_role_projection,
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
def test_v3_every_passive_sibling_agrees_with_shared_singular_patient(
    validate: RelationValidator,
) -> None:
    source = "Лицензия аннулирована и изъята судом."

    validate(source, "Лицензия изъята судом")
    for unsafe in (
        "Лицензия аннулирована и изъят судом",
        "Лицензия аннулирована и изъят пропуск судом",
        "Лицензия аннулирована и изъята лицензию судом",
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
def test_v3_invalid_patient_owner_cannot_feed_a_passive_sibling(
    validate: RelationValidator,
) -> None:
    malformed_source = "Лицензия аннулирован и изъята судом."

    with pytest.raises(ValueError):
        validate(malformed_source, "Лицензия изъята судом")


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_every_passive_sibling_agrees_with_shared_plural_patient(
    validate: RelationValidator,
) -> None:
    source = "Лицензии аннулированы и изъяты судом."

    validate(source, "Лицензии изъяты судом")
    with pytest.raises(ValueError):
        validate(source, "Лицензии аннулированы и изъята судом")


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_heterogeneous_physical_patient_list_is_shared_atomically(
    validate: RelationValidator,
) -> None:
    source = "Лицензия и разрешение аннулированы и изъяты судом."

    validate(source, "Лицензия и разрешение изъяты судом")


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_passive_action_or_keeps_alternative_semantics_and_agreement(
    validate: RelationValidator,
) -> None:
    source = "Лицензия аннулирована или изъята судом."

    validate(source, "Лицензия аннулирована либо изъята судом")
    with pytest.raises(ValueError):
        validate(source, "Лицензия изъята судом")


def test_v3_completed_truth_records_each_physical_predicate_agreement() -> None:
    source = "Лицензия аннулирована и изъят судом."
    truths, _qualitative = _coordinated_passive_role_projection(source)
    second_start = source.index("изъят")
    second_end = second_start + len("изъят")
    second = tuple(
        truth
        for truth in truths
        if truth.action_span == (second_start, second_end)
        and truth.direct_object == "license"
    )

    assert second
    assert all(not truth.patient_grammatical for truth in second)
    assert all(not truth.grammatical for truth in second)
    assert {
        truth.patient_grammar_contract for truth in second
    } == {"unresolved:patient-agreement"}


def test_v3_heterogeneous_list_completes_every_role_for_each_predicate(
) -> None:
    source = "Лицензия и разрешение аннулированы и изъяты судом."
    truths, _qualitative = _coordinated_passive_role_projection(source)
    second_start = source.index("изъяты")
    second = tuple(
        truth
        for truth in truths
        if truth.action_span[0] == second_start
    )

    assert {truth.direct_object for truth in second} == {
        "authorization",
        "license",
    }
    assert all(truth.patient_grammatical for truth in second)
    assert len({truth.patient_grammar_contract for truth in second}) == 1


def test_v3_scoped_document_excludes_its_nested_scope_noun() -> None:
    source = "Разрешение на оружие аннулировано и изъято судом."
    truths, _qualitative = _coordinated_passive_role_projection(source)
    completed = tuple(
        truth
        for truth in truths
        if truth.direct_object in {"authorization", "weapon"}
    )

    assert completed
    assert {truth.direct_object for truth in completed} == {"authorization"}
    assert len({truth.action_span for truth in completed}) == 2
    assert all(truth.patient_grammatical for truth in completed)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_scoped_document_not_scope_noun_is_shared_across_actions(
    validate: RelationValidator,
) -> None:
    source = "Разрешение на оружие аннулировано и изъято судом."

    validate(source, "Разрешение на оружие изъято судом")
    with pytest.raises(ValueError):
        validate(source, "Оружие изъято судом")


@pytest.mark.parametrize(
    "source",
    (
        "Лицензия аннулирована и изъят пропуск судом.",
        "Лицензия аннулирована и изъята лицензию судом.",
    ),
    ids=("different-family", "same-family-wrong-case"),
)
def test_v3_explicit_invalid_sibling_patient_is_never_erased(
    source: str,
) -> None:
    truths, _qualitative = _coordinated_passive_role_projection(source)
    second_start = source.index("изъят")
    second = tuple(
        truth
        for truth in truths
        if truth.action_span[0] == second_start
    )

    assert not any(
        truth.direct_object == "license" and truth.patient_grammatical
        for truth in second
    )


def test_v3_malformed_actor_group_is_not_copied_to_an_actorless_sibling(
) -> None:
    source = "Лицензии аннулированы и изъяты судом и прокуратура."
    truths, _qualitative = _coordinated_passive_role_projection(source)
    first_start = source.index("аннулированы")
    first_end = first_start + len("аннулированы")
    first = tuple(
        truth
        for truth in truths
        if truth.action_span == (first_start, first_end)
        and truth.direct_object == "license"
    )

    assert first
    assert all(
        not truth.operative_actor
        for truth in first
        if truth.grammatical
    )


@pytest.mark.parametrize(
    ("source", "coordination"),
    (
        (
            "Лицензии аннулированы и изъяты судом.",
            "conjunctive",
        ),
        (
            "Лицензии аннулированы либо изъяты судом.",
            "alternative",
        ),
    ),
    ids=("action-and", "action-or"),
)
def test_v3_shared_patient_action_controls_remain_grammatical(
    source: str,
    coordination: str,
) -> None:
    truths, _qualitative = _coordinated_passive_role_projection(source)
    completed = tuple(
        truth
        for truth in truths
        if truth.direct_object == "license"
    )

    assert completed
    assert len({truth.action_span for truth in completed}) == 2
    assert all(truth.patient_grammatical for truth in completed)
    assert all(truth.grammatical for truth in completed)
    assert {truth.coordination for truth in completed} == {coordination}
