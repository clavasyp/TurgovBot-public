from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_bounded_nominal_document_truths,
    _extract_relation_truths,
    _relation_actor_group_variants,
    _relation_complete_actor_group,
    _relation_decision_authority_contract,
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


def _assert_rejected(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    ("source", "supported", "changed"),
    (
        (
            "Пропуск может быть отозван судом Ради Права.",
            "Отзыв пропуска судом Ради Права",
            "Отзыв пропуска судом Ради Обязанности",
        ),
        (
            "Пропуск может быть отозван судом В Городе Альфа.",
            "Отзыв пропуска судом В Городе Альфа",
            "Отзыв пропуска судом В Городе Бета",
        ),
        (
            "Пропуск может быть отозван судом При Альфа.",
            "Отзыв пропуска судом При Альфа",
            "Отзыв пропуска судом При Бета (",
        ),
        (
            "Пропуск может быть аннулирован судом Как Мера Альфа.",
            "Аннулирование пропуска судом Как Мера Альфа",
            "Аннулирование пропуска судом Как Мера Бета",
        ),
        (
            "Пропуск может быть аннулирован судом В Качестве Меры Альфа.",
            "Аннулирование пропуска судом В Качестве Меры Альфа",
            "Аннулирование пропуска судом В Качестве Меры Бета",
        ),
    ),
    ids=(
        "unknown-purpose-connector",
        "typed-location-name",
        "malformed-title-cased-tail",
        "legal-role-kak-prefix",
        "legal-role-v-kachestve-prefix",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_actor_boundary_cannot_hide_changed_named_court(
    source: str,
    supported: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    ("source", "nominal", "expected_actor"),
    (
        (
            "Пропуск может быть отозван судом Ради Права.",
            "Отзыв пропуска судом Ради Права",
            "суд рад прав",
        ),
        (
            "Пропуск может быть отозван судом В Городе Альфа.",
            "Отзыв пропуска судом В Городе Альфа",
            "суд город альф",
        ),
        (
            "Пропуск может быть отозван судом При Альфа.",
            "Отзыв пропуска судом При Альфа",
            "суд альф",
        ),
        (
            "Пропуск может быть аннулирован судом Как Мера Альфа.",
            "Аннулирование пропуска судом Как Мера Альфа",
            "суд как мер альф",
        ),
        (
            "Пропуск может быть аннулирован судом В Качестве Меры Альфа.",
            "Аннулирование пропуска судом В Качестве Меры Альфа",
            "суд качеств мер альф",
        ),
        (
            "Пропуск может быть аннулирован судом Альфа в качестве "
            "дополнительной меры наказания.",
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания",
            "суд альф",
        ),
    ),
    ids=(
        "unknown-purpose-connector",
        "typed-location-name",
        "known-title-connector",
        "legal-role-kak-residue",
        "legal-role-v-kachestve-residue",
        "named-court-before-complete-legal-role",
    ),
)
def test_v3_named_court_identity_is_exact_in_finite_and_nominal_paths(
    source: str,
    nominal: str,
    expected_actor: str,
) -> None:
    source_relations = _extract_relation_truths(source)
    nominal_relations = _extract_bounded_nominal_document_truths(nominal)

    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor == expected_actor
        for relation in source_relations
    )
    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor == expected_actor
        for relation in nominal_relations
    )


@pytest.mark.parametrize(
    ("source", "nominal"),
    (
        (
            "Пропуск может быть отозван судом при мошенничестве.",
            "Отзыв пропуска судом при мошенничестве",
        ),
        (
            "Пропуск может быть отозван судом за обычную жалобу.",
            "Отзыв пропуска судом за обычную жалобу",
        ),
        (
            "Пропуск может быть отозван судом за нарушение закона штата "
            "Сан-Андреас.",
            "Отзыв пропуска судом за нарушение закона штата Сан-Андреас",
        ),
        (
            "Пропуск может быть аннулирован судом в качестве дополнительной "
            "меры наказания.",
            "Аннулирование пропуска судом как дополнительная мера наказания",
        ),
    ),
    ids=(
        "typed-lowercase-ground",
        "open-lowercase-ground",
        "lowercase-ground-with-later-proper-name",
        "complete-lowercase-legal-role",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_lowercase_true_facet_remains_supported(
    source: str,
    nominal: str,
    validate: RelationValidator,
) -> None:
    validate(source, nominal)


STACKED_LEGAL_ROLE_CONDITION_SOURCE = (
    "Пропуск может быть аннулирован судом в качестве "
    "дополнительной меры наказания в случаях, когда "
    "преступление было совершено с использованием такового."
)
STACKED_LEGAL_ROLE_CONDITION_OUTPUT = (
    "Аннулирование пропуска судом как дополнительная мера "
    "наказания за преступление с использованием пропуска"
)
STACKED_NAMED_CONDITION_SOURCE = STACKED_LEGAL_ROLE_CONDITION_SOURCE.replace(
    "судом в качестве",
    "судом Альфа в качестве",
)
STACKED_NAMED_CONDITION_OUTPUT = STACKED_LEGAL_ROLE_CONDITION_OUTPUT.replace(
    "судом как",
    "судом Альфа как",
)


def test_v3_stacked_legal_role_and_condition_keep_court_and_patient_use() -> None:
    source_relations = _extract_relation_truths(
        STACKED_LEGAL_ROLE_CONDITION_SOURCE
    )
    output_relations = _extract_bounded_nominal_document_truths(
        STACKED_LEGAL_ROLE_CONDITION_OUTPUT
    )

    for relations in (source_relations, output_relations):
        assert any(
            relation.direct_object == "access_pass"
            and relation.operative_actor == "суд"
            and any(
                argument.role == "ground"
                and argument.value == "access_pass_use"
                for argument in relation.arguments
            )
            for relation in relations
        )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_stacked_legal_role_condition_remains_source_faithful(
    validate: RelationValidator,
) -> None:
    validate(
        STACKED_LEGAL_ROLE_CONDITION_SOURCE,
        STACKED_LEGAL_ROLE_CONDITION_OUTPUT,
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_stacked_condition_preserves_exact_named_court(
    validate: RelationValidator,
) -> None:
    source = STACKED_NAMED_CONDITION_SOURCE
    supported = STACKED_NAMED_CONDITION_OUTPUT
    changed = STACKED_NAMED_CONDITION_OUTPUT.replace(
        "судом Альфа",
        "судом Бета",
    )

    validate(source, supported)
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    "unsafe",
    (
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление с использованием чужого пропуска"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление с использованием поддельного пропуска"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление с использованием пропуска ночью"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление с использованием пропуска во время "
            "чрезвычайного положения"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление с использованием пропуска по приказу "
            "губернатора"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление с использованием пропуска только "
            "повторно"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление или использование пропуска"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление не с использованием пропуска"
        ),
        (
            "Аннулирование пропуска судом Альфа как дополнительная мера "
            "наказания за преступление с использованием не пропуска"
        ),
        (
            "Аннулирование пропуска как дополнительная мера наказания за "
            "преступление с использованием пропуска судом Бета"
        ),
    ),
    ids=(
        "foreign-pass",
        "counterfeit-pass",
        "night-tail",
        "emergency-tail",
        "authority-order-tail",
        "repeat-only-tail",
        "dependency-to-or",
        "negated-use-link",
        "negated-pass-patient",
        "changed-actor-after-ground",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_use_keeps_every_meaningful_residual(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, STACKED_NAMED_CONDITION_SOURCE, unsafe)


@pytest.mark.parametrize(
    ("source_ground", "nominal_ground"),
    (
        (
            "за преступление или использование пропуска",
            "за преступление или использование пропуска",
        ),
        (
            "за преступление не с использованием пропуска",
            "за преступление не с использованием пропуска",
        ),
        (
            "за преступление с использованием не пропуска",
            "за преступление с использованием не пропуска",
        ),
    ),
    ids=(
        "source-backed-or",
        "source-backed-negated-link",
        "source-backed-negated-patient",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_source_backed_access_pass_residual_is_preserved_symmetrically(
    source_ground: str,
    nominal_ground: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом Альфа "
        f"{source_ground}."
    )
    output = (
        "Аннулирование пропуска судом Альфа "
        f"{nominal_ground}"
    )

    validate(source, output)


@pytest.mark.parametrize(
    ("source_ground", "changed_ground"),
    (
        (
            "за преступление или использование пропуска",
            "за преступление и использование пропуска",
        ),
        (
            "за преступление и использование пропуска",
            "за преступление или использование пропуска",
        ),
    ),
    ids=("or-to-and", "and-to-or"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_boolean_operator_cannot_change(
    source_ground: str,
    changed_ground: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом Альфа "
        f"{source_ground}."
    )
    changed = (
        "Аннулирование пропуска судом Альфа "
        f"{changed_ground}"
    )

    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    "punctuation",
    (",", ";"),
    ids=("comma", "semicolon"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_tail_after_punctuation_cannot_be_invented_or_dropped(
    punctuation: str,
    validate: RelationValidator,
) -> None:
    invented = (
        "Аннулирование пропуска судом Альфа за преступление с "
        f"использованием пропуска{punctuation} совершённое ночью"
    )
    detailed_source = STACKED_NAMED_CONDITION_SOURCE.replace(
        "с использованием такового.",
        f"с использованием такового{punctuation} ночью.",
    )

    _assert_rejected(validate, STACKED_NAMED_CONDITION_SOURCE, invented)
    _assert_rejected(
        validate,
        detailed_source,
        STACKED_NAMED_CONDITION_OUTPUT,
    )


@pytest.mark.parametrize(
    "exclude_marker",
    ("кроме случаев", "исключая случаи"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_pass_condition_exclusion_polarity_is_exact(
    exclude_marker: str,
    validate: RelationValidator,
) -> None:
    excluded_source = (
        "Пропуск может быть аннулирован судом Альфа, "
        f"{exclude_marker}, когда преступление было совершено с "
        "использованием пропуска."
    )
    excluded_output = (
        "Аннулирование пропуска судом Альфа, "
        f"{exclude_marker}, когда преступление было совершено с "
        "использованием пропуска"
    )

    validate(excluded_source, excluded_output)
    _assert_rejected(
        validate,
        STACKED_NAMED_CONDITION_SOURCE,
        excluded_output,
    )
    _assert_rejected(
        validate,
        excluded_source,
        STACKED_NAMED_CONDITION_OUTPUT,
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_possessive_access_pass_reference_is_not_a_safe_anaphor(
    validate: RelationValidator,
) -> None:
    possessive = (
        "Аннулирование пропуска судом Альфа за преступление с "
        "использованием его пропуска"
    )
    possessive_source = (
        "Пропуск может быть аннулирован судом Альфа за преступление с "
        "использованием его пропуска."
    )

    _assert_rejected(validate, STACKED_NAMED_CONDITION_SOURCE, possessive)
    validate(possessive_source, possessive)


@pytest.mark.parametrize(
    "ground",
    (
        "преступление, совершённое с использованием пропуска",
        "совершённое с использованием пропуска преступление",
        "совершение преступления с использованием пропуска",
        "использование пропуска при совершении преступления",
        "использование пропуска для совершения преступления",
    ),
    ids=(
        "relative-comma",
        "inverted-relative",
        "commission-noun",
        "use-during-commission",
        "use-for-commission",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_complete_access_pass_dependency_paraphrases_remain_searchable(
    ground: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом Альфа в случаях, когда "
        "преступление было совершено с использованием такового."
    )
    validate(
        source,
        f"Аннулирование пропуска судом Альфа за {ground}",
    )


@pytest.mark.parametrize(
    "changed",
    (
        "Аннулирование пропуска только по решению суда Бета",
        "Аннулирование пропуска только по решению суда Альфа и суда Бета",
        "Аннулирование пропуска только по решению суда Альфа или суда Бета",
        "Аннулирование пропуска только по решению суда Альфа либо прокуратуры Бета",
    ),
    ids=("changed-single", "invented-and", "invented-or", "invented-family"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_decision_authority_identity_and_expression_are_exact(
    changed: str,
    validate: RelationValidator,
) -> None:
    source = "Пропуск может быть аннулирован только по решению суда Альфа."
    validate(source, "Аннулирование пропуска только по решению суда Альфа")
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    "decision_marker",
    ("по решению", "на основании решения", "по постановлению"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_decision_court_cannot_gain_an_identity(
    decision_marker: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован только "
        f"{decision_marker} суда."
    )
    changed = (
        "Аннулирование пропуска только "
        f"{decision_marker} суда Бета"
    )

    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    "connector",
    ("в виде", "в форме", "в порядке", "в роли"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_complete_legal_role_keeps_following_exact_court(
    connector: str,
    validate: RelationValidator,
) -> None:
    supported = (
        f"Аннулирование пропуска {connector} дополнительного наказания "
        "судом Альфа за преступление с использованием пропуска"
    )
    changed = supported.replace("судом Альфа", "судом Бета")

    validate(STACKED_NAMED_CONDITION_SOURCE, supported)
    _assert_rejected(
        validate,
        STACKED_NAMED_CONDITION_SOURCE,
        changed,
    )


def test_v3_actorless_complete_legal_role_remains_topical() -> None:
    output = (
        "Аннулирование пропуска в виде дополнительного наказания за "
        "преступление с использованием пропуска"
    )

    # A broad title or one member of a complete synonym set may be topical.
    # A lone generated synonym, however, must not erase the only critical
    # court authority from the searchable set.
    _validate_title(STACKED_NAMED_CONDITION_SOURCE, output)
    _validate_full_set(STACKED_NAMED_CONDITION_SOURCE, output)
    _assert_rejected(
        _validate_single_synonym,
        STACKED_NAMED_CONDITION_SOURCE,
        output,
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_terminal_ground_actor_is_exact_not_discarded(
    validate: RelationValidator,
) -> None:
    supported = (
        "Аннулирование пропуска как дополнительная мера наказания за "
        "преступление с использованием пропуска судом Альфа"
    )
    changed = supported.replace("судом Альфа", "судом Бета")

    validate(STACKED_NAMED_CONDITION_SOURCE, supported)
    _assert_rejected(
        validate,
        STACKED_NAMED_CONDITION_SOURCE,
        changed,
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_relative_legal_role_actor_is_exact(
    validate: RelationValidator,
) -> None:
    supported = (
        "Аннулирование пропуска как дополнительная мера наказания, "
        "назначенная судом Альфа, за преступление с использованием пропуска"
    )
    changed = supported.replace("судом Альфа", "судом Бета")

    validate(STACKED_NAMED_CONDITION_SOURCE, supported)
    _assert_rejected(
        validate,
        STACKED_NAMED_CONDITION_SOURCE,
        changed,
    )


@pytest.mark.parametrize(
    "relative",
    ("определенная", "установленная"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_adjacent_relative_legal_role_actor_is_exact(
    relative: str,
    validate: RelationValidator,
) -> None:
    supported = (
        "Аннулирование пропуска как дополнительная мера наказания, "
        f"{relative} судом Альфа, за преступление с использованием пропуска"
    )
    changed = supported.replace("судом Альфа", "судом Бета")

    validate(STACKED_NAMED_CONDITION_SOURCE, supported)
    _assert_rejected(
        validate,
        STACKED_NAMED_CONDITION_SOURCE,
        changed,
    )


@pytest.mark.parametrize(
    ("source", "supported", "changed"),
    (
        (
            STACKED_NAMED_CONDITION_SOURCE,
            "Аннулирование пропуска как дополнительная мера наказания "
            "(назначенная судом Альфа) за преступление с использованием пропуска",
            "Аннулирование пропуска как дополнительная мера наказания "
            "(назначенная судом Бета) за преступление с использованием пропуска",
        ),
        (
            STACKED_NAMED_CONDITION_SOURCE,
            "Аннулирование пропуска в качестве дополнительной меры, "
            "назначаемой судом Альфа, за преступление с использованием пропуска",
            "Аннулирование пропуска в качестве дополнительной меры, "
            "назначаемой судом Бета, за преступление с использованием пропуска",
        ),
        (
            STACKED_NAMED_CONDITION_SOURCE.replace(
                "судом Альфа",
                "LSPD Alpha",
            ),
            "Аннулирование пропуска в роли дополнительного наказания "
            "LSPD Alpha за преступление с использованием пропуска",
            "Аннулирование пропуска в роли дополнительного наказания "
            "LSPD Beta за преступление с использованием пропуска",
        ),
        (
            STACKED_NAMED_CONDITION_SOURCE,
            "Аннулирование пропуска как дополнительное наказание, судом "
            "Альфа, за преступление с использованием пропуска",
            "Аннулирование пропуска как дополнительное наказание, судом "
            "Бета, за преступление с использованием пропуска",
        ),
        (
            STACKED_NAMED_CONDITION_SOURCE,
            "Аннулирование пропуска как дополнительное наказание, которое "
            "назначено судом Альфа, за преступление с использованием пропуска",
            "Аннулирование пропуска как дополнительное наказание, которое "
            "назначено судом Бета, за преступление с использованием пропуска",
        ),
        (
            STACKED_NAMED_CONDITION_SOURCE,
            "Аннулирование пропуска как дополнительное наказание, налагаемое "
            "судом Альфа, за преступление с использованием пропуска",
            "Аннулирование пропуска как дополнительное наказание, налагаемое "
            "судом Бета, за преступление с использованием пропуска",
        ),
        (
            STACKED_NAMED_CONDITION_SOURCE,
            "Аннулирование пропуска как дополнительное наказание, применённое "
            "судом Альфа, за преступление с использованием пропуска",
            "Аннулирование пропуска как дополнительное наказание, применённое "
            "судом Бета, за преступление с использованием пропуска",
        ),
    ),
    ids=(
        "parenthesized-relative",
        "ongoing-relative",
        "ascii-legal-role",
        "comma-instrumental",
        "relative-clause",
        "imposed-participle",
        "applied-participle",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_adjacent_legal_role_actor_forms_are_exact(
    source: str,
    supported: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, supported)
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    "oblique",
    ("при участии LSPD", "при участии полиции Гамма"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_oblique_role_before_true_assigning_actor_is_local(
    oblique: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Аннулирование пропуска как дополнительное наказание, "
        "назначаемое судом Альфа, за преступление с использованием пропуска."
    )
    supported = (
        "Аннулирование пропуска как дополнительное наказание "
        f"{oblique}, назначаемое судом Альфа, за преступление с "
        "использованием пропуска"
    )
    changed = supported.replace("судом Альфа", "судом Бета")
    actorless_source = (
        "Аннулирование пропуска как дополнительное наказание "
        f"{oblique}, за преступление с использованием пропуска."
    )

    validate(source, supported)
    _assert_rejected(validate, source, changed)
    validate(actorless_source, actorless_source.rstrip("."))


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_terminal_actor_cannot_hide_an_earlier_conflicting_actor(
    validate: RelationValidator,
) -> None:
    duplicated = (
        "Аннулирование пропуска судом Альфа как дополнительная мера "
        "наказания за преступление с использованием пропуска судом Альфа"
    )
    changed = duplicated.replace("судом Альфа как", "судом Бета как")
    minimal_changed = (
        "Аннулирование пропуска судом Бета за преступление с "
        "использованием пропуска судом Альфа"
    )

    _assert_rejected(validate, STACKED_NAMED_CONDITION_SOURCE, duplicated)
    _assert_rejected(validate, STACKED_NAMED_CONDITION_SOURCE, changed)
    _assert_rejected(
        validate,
        STACKED_NAMED_CONDITION_SOURCE,
        minimal_changed,
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_generic_court_cannot_gain_terminal_identity(
    validate: RelationValidator,
) -> None:
    _assert_rejected(
        validate,
        STACKED_LEGAL_ROLE_CONDITION_SOURCE,
        "Аннулирование пропуска как дополнительная мера наказания за "
        "преступление с использованием пропуска судом Бета",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_terminal_prosecutor_identity_cannot_change(
    validate: RelationValidator,
) -> None:
    source = STACKED_NAMED_CONDITION_SOURCE.replace(
        "судом Альфа",
        "прокуратурой Альфа",
    )
    supported = (
        "Аннулирование пропуска как дополнительная мера наказания за "
        "преступление с использованием пропуска прокуратурой Альфа"
    )
    changed = supported.replace("прокуратурой Альфа", "прокуратурой Бета")

    validate(source, supported)
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    ("source", "changed"),
    (
        (
            "Пропуск может быть аннулирован судом Когда Решает Альфа.",
            "Аннулирование пропуска судом Когда Решает Бета",
        ),
        (
            "Пропуск может быть аннулирован судом Пока Действует Альфа.",
            "Аннулирование пропуска судом Пока Действует Бета",
        ),
    ),
    ids=("kogda-name", "poka-name"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_condition_words_inside_unknown_actor_name_never_collapse_to_court(
    source: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unsupported_poka_prerequisite_cannot_be_silently_dropped(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом в качестве "
        "дополнительной меры наказания пока действует "
        "чрезвычайное положение."
    )
    _assert_rejected(
        validate,
        source,
        "Суд может аннулировать пропуск как "
        "дополнительное наказание",
    )


def test_v3_incomplete_legal_role_residue_stays_unresolved() -> None:
    source = (
        "Пропуск может быть аннулирован судом в качестве "
        "дополнительной меры наказания Альфа."
    )
    relations = _extract_relation_truths(source)

    assert any(
        relation.direct_object == "access_pass"
        and relation.operative_actor.startswith("__unresolved_actor__:")
        for relation in relations
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nonexclusive_decision_authority_identity_is_exact(
    validate: RelationValidator,
) -> None:
    source = "Пропуск может быть аннулирован по решению суда Альфа."
    validate(source, "Аннулирование пропуска по решению суда Альфа")
    validate(source, "Пропуск может быть аннулирован по решению суда Альфа")
    _assert_rejected(
        validate,
        source,
        "Аннулирование пропуска по решению суда Бета",
    )
    _assert_rejected(
        validate,
        source,
        "Пропуск может быть аннулирован по решению суда Бета",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_joint_decision_authority_cannot_drop_or_change_a_member(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован по решению суда Альфа и суда Бета."
    )
    validate(
        source,
        "Аннулирование пропуска по решению суда Бета и суда Альфа",
    )
    for unsafe in (
        "Аннулирование пропуска по решению суда Альфа",
        "Аннулирование пропуска по решению суда Альфа или суда Бета",
        "Аннулирование пропуска по решению суда Альфа и суда Гамма",
        "Пропуск может быть аннулирован по решению суда Альфа",
    ):
        _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_explicit_or_decision_authority_allows_only_a_proven_branch(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован по решению суда Альфа или суда Бета."
    )
    validate(source, "Аннулирование пропуска по решению суда Альфа")
    validate(source, "Пропуск может быть аннулирован по решению суда Бета")
    _assert_rejected(
        validate,
        source,
        "Аннулирование пропуска по решению суда Гамма",
    )


@pytest.mark.parametrize(
    ("source", "supported", "changed"),
    (
        (
            (
                "Пропуск может быть аннулирован по решению суда Альфа "
                "за нарушение закона."
            ),
            "Аннулирование пропуска по решению суда Альфа",
            "Аннулирование пропуска по решению суда Бета",
        ),
        (
            (
                "Пропуск может быть аннулирован по решению суда Альфа "
                "при ЧП."
            ),
            (
                "При ЧП пропуск может быть аннулирован по решению "
                "суда Альфа"
            ),
            (
                "При ЧП пропуск может быть аннулирован по решению "
                "суда Бета"
            ),
        ),
    ),
    ids=("ground-boundary", "condition-boundary"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_decision_identity_stops_before_a_proven_external_facet(
    source: str,
    supported: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    assert _relation_decision_authority_contract(source)[0] == "суд альф"
    validate(source, supported)
    _assert_rejected(validate, source, changed)


@pytest.mark.parametrize(
    ("source", "output", "changed"),
    (
        (
            (
                "Суд Альфа совместно с (прокуратурой Бета или полицией "
                "Гамма) могут аннулировать пропуск, когда преступление "
                "совершено с использованием пропуска."
            ),
            (
                "Аннулирование пропуска за преступление с использованием "
                "пропуска судом Альфа совместно с (прокуратурой Бета или "
                "полицией Гамма)"
            ),
            (
                "Аннулирование пропуска за преступление с использованием "
                "пропуска судом Альфа совместно с (прокуратурой Бета или "
                "полицией Дельта)"
            ),
        ),
        (
            (
                "(Суд Альфа совместно с прокуратурой Бета) или полиция "
                "Гамма могут аннулировать пропуск, когда преступление "
                "совершено с использованием пропуска."
            ),
            (
                "Аннулирование пропуска за преступление с использованием "
                "пропуска (судом Альфа совместно с прокуратурой Бета) или "
                "полицией Гамма"
            ),
            (
                "Аннулирование пропуска за преступление с использованием "
                "пропуска судом Альфа совместно с (прокуратурой Бета или "
                "полицией Гамма)"
            ),
        ),
    ),
    ids=("joint-over-or", "or-over-joint"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nested_terminal_actor_expression_preserves_exact_boolean_shape(
    source: str,
    output: str,
    changed: str,
    validate: RelationValidator,
) -> None:
    validate(source, output)
    _assert_rejected(validate, source, changed)


def test_v3_nested_actor_dnf_overflow_stays_atomic_and_bounded() -> None:
    actor_surface = " совместно с ".join(
        f"(FIB A{index} или LSPD B{index})"
        for index in range(16)
    )
    group = _relation_complete_actor_group(actor_surface)

    assert group is not None
    variants = _relation_actor_group_variants(
        group,
        allow_alternative_branches=True,
    )
    assert len(variants) == 1
    assert variants[0].startswith("__actor_group__:overflow:")

    source = f"Пропуск может быть аннулирован {actor_surface}."
    validate_synonym_source_evidence(source, [source.rstrip(".")])
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            [source.rstrip(".").replace("LSPD B15", "LSPD Z15")],
        )


@pytest.mark.parametrize(
    "joint_role",
    (
        "в сотрудничестве с",
        "во взаимодействии с",
        "в координации с",
        "в тесном сотрудничестве с",
        "в непосредственном взаимодействии с",
        "при тесной координации с",
        "в постоянной координации с",
        "в сотрудничестве также с",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_productive_joint_role_keeps_every_required_actor(
    joint_role: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом Альфа "
        f"{joint_role} прокуратурой Бета."
    )
    validate(
        source,
        "Аннулирование пропуска судом Альфа "
        f"{joint_role} прокуратурой Бета",
    )
    for unsafe in (
        "Аннулирование пропуска судом Альфа",
        (
            "Аннулирование пропуска судом Альфа "
            f"{joint_role} прокуратурой Гамма"
        ),
    ):
        _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_parenthetical_productive_joint_role_is_atomic(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть аннулирован судом Альфа "
        "(в сотрудничестве с прокуратурой Бета)."
    )
    validate(
        source,
        "Аннулирование пропуска судом Альфа "
        "(в сотрудничестве с прокуратурой Бета)",
    )
    for unsafe in (
        "Аннулирование пропуска судом Альфа",
        (
            "Аннулирование пропуска судом Альфа "
            "(в сотрудничестве с прокуратурой Гамма)"
        ),
    ):
        _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_dnf_overflow_preserves_atomic_slash_semantics(
    validate: RelationValidator,
) -> None:
    atomic_actor = " совместно с ".join(
        f"(FIB A{index} / LSPD B{index})"
        for index in range(9)
    )
    source = f"Пропуск может быть аннулирован {atomic_actor}."
    unsafe = (
        "Аннулирование пропуска "
        + atomic_actor.replace(" / ", " или ")
    )

    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_opposition_target_is_not_a_second_issuer(
    validate: RelationValidator,
) -> None:
    source = (
        "Ордер на задержание министра юстиции выписывает губернатор "
        "штата или верховный судья."
    )
    validate(
        source,
        "выдача ордера губернатором против министра юстиции",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unquoted_organization_name_can_still_contain_protiv(
    validate: RelationValidator,
) -> None:
    source = "Организация Центр Против Коррупции выдаёт ордер."
    validate(
        source,
        "выдача ордера организацией Центр Против Коррупции",
    )
