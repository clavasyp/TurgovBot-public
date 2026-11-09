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


def _validate_synonym(source: str, output: str) -> None:
    validate_synonym_source_evidence(source, [output])


OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_synonym,
)


def _issue_truth(value: str):
    return next(
        truth
        for truth in _extract_relation_truths(value)
        if truth.predicate == "regulated_issue"
    )


def test_v3_passive_protocol_form_is_not_selected_as_the_patient() -> None:
    truth = _issue_truth("Допуск оформляется протоколом.")

    assert truth.event == "present_passive"
    assert truth.direct_object == "access_pass"
    assert truth.container_object == ""
    assert truth.operative_actor == ""
    assert {
        (argument.role, argument.value, argument.marker)
        for argument in truth.arguments
    } == {("document_form", "протокол", "form")}


def test_v3_active_protocol_remains_a_real_patient() -> None:
    truth = _issue_truth("Суд оформляет протокол.")

    assert truth.event == "present"
    assert truth.operative_actor == "суд"
    assert truth.direct_object == "protocol"
    assert truth.container_object == "protocol"
    assert not any(
        argument.role == "document_form"
        for argument in truth.arguments
    )


@pytest.mark.parametrize("validate", OUTPUT_PATHS, ids=("title", "synonym"))
def test_v3_qualified_document_form_preserves_its_modifier(
    validate: RelationValidator,
) -> None:
    source = "Допуск оформляется в виде письменного приказа."

    validate(source, "Допуск оформляется письменным приказом")
    with pytest.raises(ValueError):
        validate(source, "Допуск оформляется приказом")


@pytest.mark.parametrize("connector", ("или", "либо"))
@pytest.mark.parametrize("validate", OUTPUT_PATHS, ids=("title", "synonym"))
def test_v3_document_form_or_alternatives_keep_only_proved_branches(
    validate: RelationValidator,
    connector: str,
) -> None:
    source = (
        f"Допуск оформляется в виде приказа {connector} распоряжения."
    )

    validate(source, "Допуск оформляется приказом")
    validate(source, "Допуск оформляется в виде распоряжения")
    with pytest.raises(ValueError):
        validate(source, "Допуск оформляется протоколом")


@pytest.mark.parametrize(
    "source",
    (
        "Допуск оформляется приказом или прокурором.",
        "Допуск оформляется приказом либо прокурором.",
        "Допуск оформляется приказом и прокурором.",
        "Допуск оформляется в виде приказа или прокурора.",
        "Допуск оформляется в виде приказа либо прокурора.",
        "Допуск оформляется в виде приказа и прокурора.",
    ),
)
@pytest.mark.parametrize("validate", OUTPUT_PATHS, ids=("title", "synonym"))
def test_v3_mixed_document_form_and_actor_branch_fails_closed(
    validate: RelationValidator,
    source: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "Допуск оформляется приказом")
    with pytest.raises(ValueError):
        validate(source, "Прокурор оформляет допуск")


def test_v3_uncoordinated_form_does_not_hide_a_separate_passive_actor() -> None:
    truth = _issue_truth("Допуск оформляется приказом прокурором.")

    assert truth.direct_object == "access_pass"
    assert truth.operative_actor == "прокурор"
    assert any(
        argument.role == "document_form" and argument.value == "приказ"
        for argument in truth.arguments
    )
