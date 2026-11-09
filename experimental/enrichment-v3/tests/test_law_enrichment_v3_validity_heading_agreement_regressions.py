from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_relation_truths,
    _relation_validity_prior_list_item_is_proved,
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
    validate_penalty_source_evidence("Наказание: " + source, output, None)


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


def _heading(
    subject: str,
    copula: str,
    predicative: str,
    item: str,
    *,
    final_period: bool = False,
) -> str:
    value = (
        f"{subject} {copula} {predicative} при соблюдении "
        f"следующих условий:\n{item}"
    )
    return value + "." if final_period else value


@pytest.mark.parametrize(
    ("subject", "source_copula", "output_copula", "predicative", "item"),
    (
        (
            "Лицензия",
            "считается",
            "признается",
            "действительной",
            "Подписана судом",
        ),
        (
            "Документ",
            "считается",
            "является",
            "действительным",
            "Подписан судом",
        ),
        (
            "Разрешение",
            "является",
            "признается",
            "действительным",
            "Подписано судом",
        ),
    ),
    ids=("feminine", "masculine", "neuter"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_validity_heading_singular_gender_agreement(
    subject: str,
    source_copula: str,
    output_copula: str,
    predicative: str,
    item: str,
    validate: RelationValidator,
) -> None:
    source = _heading(
        subject,
        source_copula,
        predicative,
        item,
        final_period=True,
    )
    validate(source, _heading(subject, output_copula, predicative, item))

    wrong_predicative = (
        "действительной"
        if predicative == "действительным"
        else "действительным"
    )
    wrong_number_copula = {
        "признается": "признаются",
        "является": "являются",
    }[output_copula]
    for unsafe in (
        _heading(subject, output_copula, wrong_predicative, item),
        _heading(subject, wrong_number_copula, predicative, item),
        _heading(subject, "признаются", "действительными", item),
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
def test_v3_validity_heading_plural_agreement(
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензии",
        "считаются",
        "действительными",
        "Подписаны судом",
        final_period=True,
    )
    validate(
        source,
        _heading(
            "Лицензии",
            "признаются",
            "действительными",
            "Подписаны судом",
        ),
    )
    for unsafe in (
        _heading(
            "Лицензии",
            "признается",
            "действительными",
            "Подписаны судом",
        ),
        _heading(
            "Лицензии",
            "признаются",
            "действительной",
            "Подписаны судом",
        ),
        _heading(
            "Лицензии",
            "признается",
            "действительной",
            "Подписаны судом",
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
def test_v3_validity_heading_heterogeneous_subject_is_plural(
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия и разрешение",
        "считаются",
        "действительными",
        "Подписаны судом",
        final_period=True,
    )
    faithful_subject = "Лицензия и разрешение"
    validate(
        source,
        _heading(
            faithful_subject,
            "являются",
            "действительными",
            "Подписаны судом",
        ),
    )
    for unsafe in (
        _heading(
            faithful_subject,
            "является",
            "действительными",
            "Подписаны судом",
        ),
        _heading(
            faithful_subject,
            "являются",
            "действительным",
            "Подписаны судом",
        ),
        _heading(
            faithful_subject,
            "является",
            "действительным",
            "Подписаны судом",
        ),
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    "subject",
    ("Два документа", "Несколько документов"),
    ids=("two", "several"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_validity_heading_quantified_subject_is_plural(
    subject: str,
    validate: RelationValidator,
) -> None:
    source = _heading(
        subject,
        "считаются",
        "действительными",
        "Подписаны судом",
        final_period=True,
    )
    validate(
        source,
        _heading(
            subject,
            "признаются",
            "действительными",
            "Подписаны судом",
        ),
    )
    for unsafe in (
        _heading(
            subject,
            "признается",
            "действительными",
            "Подписаны судом",
        ),
        _heading(
            subject,
            "признаются",
            "действительным",
            "Подписаны судом",
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
def test_v3_validity_heading_reaches_every_proved_list_item(
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия",
        "считается",
        "действительной",
        "1. Выдана и подписана судом.\n2. Оформлена прокуратурой",
        final_period=True,
    )
    validate(
        source,
        _heading(
            "Лицензия",
            "признается",
            "действительной",
            "1. Выдана и подписана судом.\n2. Оформлена прокуратурой",
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
def test_v3_later_validity_item_supports_active_voice_paraphrase(
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия",
        "считается",
        "действительной",
        "1. Выдана и подписана судом.\n2. Оформлена прокуратурой",
        final_period=True,
    )
    validate(source, "Прокуратура оформила лицензию")


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_prior_validity_cluster_may_contain_distinct_actions(
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия",
        "считается",
        "действительной",
        "1. Выдана и заверена судом.\n2. Оформлена прокуратурой",
        final_period=True,
    )
    validate(source, "Прокуратура оформила лицензию")


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_validity_item_ignores_participial_history_in_heading_subject(
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия, выданная судом,",
        "считается",
        "действительной",
        "1. Подписана прокуратурой.\n2. Оформлена судом",
        final_period=True,
    )
    output = _heading(
        "Лицензия, выданная судом,",
        "признается",
        "действительной",
        "1. Подписана прокуратурой.\n2. Оформлена судом",
    )

    validate(source, output)


@pytest.mark.parametrize(
    "unsafe_subject",
    (
        "Лицензия, выданный судом,",
        "Лицензия, выданная письменным договором,",
        "Лицензия, возможно выданная судом,",
    ),
    ids=("wrong-agreement", "non-actor-instrument", "nonassertive-history"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_validity_participial_history_is_bounded_and_proved(
    unsafe_subject: str,
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия, выданная судом,",
        "считается",
        "действительной",
        "1. Подписана прокуратурой.\n2. Оформлена судом",
        final_period=True,
    )
    unsafe = _heading(
        unsafe_subject,
        "признается",
        "действительной",
        "1. Подписана прокуратурой.\n2. Оформлена судом",
    )

    with pytest.raises(ValueError):
        validate(source, unsafe)


def test_v3_validity_prior_item_scans_only_its_own_occurrences() -> None:
    expected = frozenset({"license"})

    assert _relation_validity_prior_list_item_is_proved(
        "1. Подписана прокуратурой",
        subject="Лицензия, выданная судом",
        expected_objects=expected,
    )
    assert not _relation_validity_prior_list_item_is_proved(
        "1. Подписан прокуратурой",
        subject="Лицензия, выданная судом",
        expected_objects=expected,
    )
    assert not _relation_validity_prior_list_item_is_proved(
        "1. Подписана письменным договором",
        subject="Лицензия, выданная судом",
        expected_objects=expected,
    )


def test_v3_later_proved_item_inherits_exact_heading_patient() -> None:
    output = _heading(
        "Лицензия",
        "считается",
        "действительной",
        "1. Выдана и подписана судом.\n2. Оформлена прокуратурой",
    )
    item_start = output.rindex("Оформлена")
    inherited = tuple(
        truth
        for truth in _extract_relation_truths(output)
        if truth.action_span[0] == item_start
        and truth.direct_object == "license"
    )

    assert inherited
    assert all(truth.patient_surface_contract for truth in inherited)
    assert all(truth.patient_grammatical for truth in inherited)
    assert all(truth.grammatical for truth in inherited)


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_malformed_coordinated_item_blocks_later_inheritance(
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия",
        "считается",
        "действительной",
        "1. Выдана и подписана судом.\n2. Оформлена прокуратурой",
        final_period=True,
    )
    unsafe = _heading(
        "Лицензия",
        "признается",
        "действительной",
        "1. Выдана и подписаны судом.\n2. Оформлена прокуратурой",
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)


def test_v3_malformed_coordinated_item_has_no_later_patient_bridge() -> None:
    output = _heading(
        "Лицензия",
        "считается",
        "действительной",
        "1. Выдана и подписаны судом.\n2. Оформлена прокуратурой",
    )
    item_start = output.rindex("Оформлена")
    inherited = tuple(
        truth
        for truth in _extract_relation_truths(output)
        if truth.action_span[0] == item_start
        and truth.direct_object == "license"
    )

    assert not inherited


@pytest.mark.parametrize(
    "unsafe_item_block",
    (
        (
            "1. Выдана прокуратурой с посторонним остатком.\n"
            "2. Подписана судом"
        ),
        (
            "1. Выдана прокуратурой.\n"
            "Справочный раздел документа:\n"
            "2. Подписана судом"
        ),
    ),
    ids=("malformed-prior-item", "foreign-section"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_validity_heading_does_not_cross_an_unsafe_list_fence(
    unsafe_item_block: str,
    validate: RelationValidator,
) -> None:
    source = _heading(
        "Лицензия",
        "считается",
        "действительной",
        "1. Выдана прокуратурой.\n2. Подписана судом",
        final_period=True,
    )
    unsafe = _heading(
        "Лицензия",
        "признается",
        "действительной",
        unsafe_item_block,
    )
    with pytest.raises(ValueError):
        validate(source, unsafe)


@pytest.mark.parametrize(
    "unsafe_item_block",
    (
        (
            "1. Выдана прокуратурой с посторонним остатком.\n"
            "2. Подписана судом"
        ),
        (
            "1. Выдана прокуратурой.\n"
            "Справочный раздел документа:\n"
            "2. Подписана судом"
        ),
    ),
    ids=("malformed-prior-item", "foreign-section"),
)
def test_v3_unsafe_list_fence_blocks_second_item_patient_inheritance(
    unsafe_item_block: str,
) -> None:
    output = _heading(
        "Лицензия",
        "считается",
        "действительной",
        unsafe_item_block,
    )
    item_start = output.rindex("Подписана")
    inherited = tuple(
        truth
        for truth in _extract_relation_truths(output)
        if truth.action_span[0] == item_start
        and truth.direct_object == "license"
    )

    assert not inherited


def test_v3_malformed_heading_keeps_an_ungrammatical_inherited_truth() -> None:
    output = _heading(
        "Лицензия",
        "считаются",
        "действительными",
        "Подписана судом",
    )
    item_start = output.index("Подписана")
    inherited = tuple(
        truth
        for truth in _extract_relation_truths(output)
        if truth.action_span[0] == item_start
        and truth.direct_object == "license"
    )

    assert inherited
    assert all(truth.patient_surface_contract for truth in inherited)
    assert all(not truth.patient_grammatical for truth in inherited)
    assert all(not truth.grammatical for truth in inherited)
    assert {
        truth.patient_grammar_contract for truth in inherited
    } == {"unresolved:validity-heading-agreement"}
