from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _extract_explicit_ground_claims,
    _extract_relation_truths,
    _normalized_text,
    _relation_generic_object_head,
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


def _validate_full_set_with_topical_base(source: str, output: str) -> None:
    base = "Порядок отзыва пропуска"
    synonyms = [base]
    if output != base:
        synonyms.append(output)
    validate_synonym_source_evidence(source, synonyms)


MALFORMED_OUTPUT_PATHS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_single_synonym,
    _validate_full_set_with_topical_base,
)


def _assert_rejected(
    validate: RelationValidator,
    source: str,
    *unsafe_outputs: str,
) -> None:
    unexpectedly_supported: list[str] = []
    for unsafe in unsafe_outputs:
        try:
            validate(source, unsafe)
        except ValueError:
            continue
        unexpectedly_supported.append(unsafe)
    assert not unexpectedly_supported, (
        "source-unsupported outputs were accepted: "
        f"{unexpectedly_supported!r}"
    )


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "FIB/ или SANG могут отозвать пропуск.",
            "SANG может отозвать пропуск",
        ),
        (
            "(FIB/) могут отозвать пропуск.",
            "FIB может отозвать пропуск",
        ),
        (
            "FIB/() могут отозвать пропуск.",
            "FIB может отозвать пропуск",
        ),
    ),
    ids=("dangling-left-branch", "dangling-parenthesized-tail", "empty-tail"),
)
@pytest.mark.parametrize(
    "validate",
    MALFORMED_OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_malformed_slash_actor_expression_fails_closed(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    # A malformed actor expression may be unusable as generated prose.  The
    # honest control is therefore a non-assertive topical query, while the
    # malformed sibling must not be discarded to manufacture a clean actor.
    validate(source, "Порядок отзыва пропуска")
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_well_formed_slash_keeps_outer_or_branch_available(
    validate: RelationValidator,
) -> None:
    source = "FIB/USSS или SANG могут отозвать пропуск."
    validate(source, "SANG может отозвать пропуск")
    validate(source, "FIB/USSS могут отозвать пропуск")
    _assert_rejected(
        validate,
        source,
        "FIB может отозвать пропуск",
        "USSS может отозвать пропуск",
    )


@pytest.mark.parametrize(
    ("source", "honest_outputs", "unsafe_outputs"),
    (
        (
            "Организация Центр (с разрешения суда Альфа, с согласия "
            "прокуратуры Бета) или с одобрения губернатора Гамма может "
            "отозвать пропуск.",
            (
                "Организация Центр с согласия прокуратуры Бета и с "
                "разрешения суда Альфа может отозвать пропуск",
                "Организация Центр с одобрения губернатора Гамма может "
                "отозвать пропуск",
            ),
            (
                "Организация Центр с разрешения суда Альфа может отозвать "
                "пропуск",
                "Организация Центр с согласия прокуратуры Бета может "
                "отозвать пропуск",
            ),
        ),
        (
            "Организация Центр с разрешения суда Альфа или (с согласия "
            "прокуратуры Бета, с одобрения губернатора Гамма) может "
            "отозвать пропуск.",
            (
                "Организация Центр с разрешения суда Альфа может отозвать "
                "пропуск",
                "Организация Центр с одобрения губернатора Гамма и с "
                "согласия прокуратуры Бета может отозвать пропуск",
            ),
            (
                "Организация Центр с согласия прокуратуры Бета может "
                "отозвать пропуск",
                "Организация Центр с одобрения губернатора Гамма может "
                "отозвать пропуск",
            ),
        ),
    ),
    ids=("parenthesized-joint-before-or", "or-before-parenthesized-joint"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_parentheses_preserve_joint_and_or_structure(
    source: str,
    honest_outputs: tuple[str, ...],
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    for honest in honest_outputs:
        validate(source, honest)
    _assert_rejected(validate, source, *unsafe_outputs)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_permission_sibling_groups_keep_comma_joint_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр (с разрешения суда Альфа или с разрешения "
        "прокуратуры Бета) и (с разрешения губернатора Гамма, с разрешения "
        "правительства Дельта) может отозвать пропуск."
    )
    # (A OR B) AND (C AND D): the OR in the first sibling group must not
    # rewrite a comma at the same nesting depth inside the second group.
    validate(
        source,
        "Организация Центр с разрешения суда Альфа и с разрешения "
        "губернатора Гамма и с разрешения правительства Дельта может "
        "отозвать пропуск",
    )
    validate(
        source,
        "Организация Центр с разрешения прокуратуры Бета и с разрешения "
        "губернатора Гамма и с разрешения правительства Дельта может "
        "отозвать пропуск",
    )
    _assert_rejected(
        validate,
        source,
        "Организация Центр с разрешения суда Альфа может отозвать пропуск",
        "Организация Центр с разрешения губернатора Гамма может отозвать "
        "пропуск",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_open_vocabulary_ground_keeps_full_detail_across_morphology(
    validate: RelationValidator,
) -> None:
    source = (
        "Если нарушены правила хранения реагентов в лаборатории Альфа, "
        "лицензия парамедика изымается."
    )
    validate(
        source,
        "Лицензия парамедика изымается за нарушение правил хранения "
        "реагентов в лаборатории Альфа",
    )
    validate(
        source,
        "Лицензия парамедика изымается, если нарушены правила хранения "
        "реагентов в лаборатории Альфа",
    )
    _assert_rejected(
        validate,
        source,
        "Лицензия парамедика изымается за нарушение правил хранения "
        "реагентов",
        "Лицензия парамедика изымается за нарушение правил",
    )


@pytest.mark.parametrize(
    ("source", "unsafe_outputs"),
    (
        (
            "Полиция, кроме отдела «Альфа, Бета», может выдавать лицензию.",
            (
                "Полиция, кроме отдела «Альфа, Гамма», может выдавать лицензию",
                "Полиция может выдавать лицензию",
            ),
        ),
        (
            "Полиция, кроме LSPD, LSSD и LSCSD, может выдавать лицензию.",
            (
                "Полиция, кроме LSPD, может выдавать лицензию",
                "Полиция может выдавать лицензию",
            ),
        ),
        (
            "Полиция, кроме прокуратуры Бета, LSPD и LSSD, может "
            "выдавать лицензию.",
            (
                "Полиция, кроме прокуратуры Бета, может выдавать лицензию",
                "Полиция может выдавать лицензию",
            ),
        ),
    ),
    ids=("quoted-comma", "comma-list", "mixed-family-list"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_exclusion_keeps_quoted_and_list_tail(
    source: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, source.rstrip("."))
    _assert_rejected(validate, source, *unsafe_outputs)


@pytest.mark.parametrize(
    ("source", "honest", "unsafe_outputs"),
    (
        (
            "Организация Центр может отозвать пропуск до получения "
            "разрешения суда Альфа.",
            "До получения разрешения суда Альфа организация Центр может "
            "отозвать пропуск",
            (
                "Организация Центр после получения разрешения суда Альфа "
                "может отозвать пропуск",
                "Организация Центр может отозвать пропуск",
            ),
        ),
        (
            "Организация Центр может отозвать пропуск после истечения "
            "разрешения суда Альфа.",
            "После истечения разрешения суда Альфа организация Центр может "
            "отозвать пропуск",
            (
                "Организация Центр после получения разрешения суда Альфа "
                "может отозвать пропуск",
                "Организация Центр может отозвать пропуск",
            ),
        ),
        (
            "Организация Центр может отозвать пропуск после того как "
            "обсуждено разрешение суда Альфа.",
            "Организация Центр может отозвать пропуск после того, как "
            "обсуждено разрешение суда Альфа",
            (
                "Организация Центр после получения разрешения суда Альфа "
                "может отозвать пропуск",
                "Организация Центр может отозвать пропуск",
            ),
        ),
    ),
    ids=("before-acquisition", "after-expiry", "compact-after-discussion"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nonacquisition_temporal_clause_cannot_become_permission(
    source: str,
    honest: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, *unsafe_outputs)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_finite_issuer_keeps_bare_current_recipient(
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того, как суд Альфа "
        "выдаст организации разрешение."
    )
    # Bare ``организации`` is the current holder in the finite clause, so it
    # may bridge to the equivalent nominal prerequisite owned by Центр.
    validate(
        source,
        "Организация Центр после получения разрешения суда Альфа может "
        "отозвать пропуск",
    )
    _assert_rejected(
        validate,
        source,
        "Организация Центр может отозвать пропуск после того, как суд Альфа "
        "выдаст разрешение",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_regulated_object_must_be_direct_after_purpose_phrase(
    validate: RelationValidator,
) -> None:
    source = (
        "Полиция выписывает предписание для получения разрешения "
        "организацией."
    )
    validate(source, source.rstrip("."))
    validate(source, "Полиция выписывает предписание")
    _assert_rejected(
        validate,
        source,
        "Полиция выписывает разрешение",
    )


@pytest.mark.parametrize(
    ("source", "honest", "changed_condition"),
    (
        (
            "После того как обсуждено разрешение суда Альфа "
            "организация Центр может отозвать пропуск.",
            "Организация Центр может отозвать пропуск после того, "
            "как обсуждено разрешение суда Альфа",
            "Организация Центр может отозвать пропуск после того, "
            "как обсуждено разрешение суда Бета",
        ),
        (
            "После того как организация Бета обсудила разрешение "
            "суда Альфа организация Центр может отозвать пропуск.",
            "Организация Центр может отозвать пропуск после того, "
            "как организация Бета обсудила разрешение суда Альфа",
            "Организация Центр может отозвать пропуск после того, "
            "как организация Гамма обсудила разрешение суда Альфа",
        ),
    ),
    ids=("leading-no-comma", "leading-subordinate-actor"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_compound_temporal_condition_keeps_main_actor_and_full_detail(
    source: str,
    honest: str,
    changed_condition: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    truths = _extract_relation_truths(_normalized_text(source))
    assert truths
    assert all(truth.operative_actor == "организаци центр" for truth in truths)
    _assert_rejected(
        validate,
        source,
        changed_condition,
        "Организация Центр может отозвать пропуск",
    )


@pytest.mark.parametrize(
    "tail",
    (
        "(обсуждено разрешение суда Альфа",
        ", обсуждено разрешение суда Альфа",
        ("очень " * 45) + "обсуждено разрешение суда Альфа",
    ),
    ids=("malformed-delimiter", "double-comma", "over-window"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_unresolved_compound_temporal_condition_cannot_disappear(
    tail: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Организация Центр может отозвать пропуск после того как "
        f"{tail}."
    )
    _assert_rejected(
        validate,
        source,
        "Организация Центр может отозвать пропуск",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_beneficiary_before_object_is_not_a_purpose_object(
    validate: RelationValidator,
) -> None:
    source = "Полиция выписывает для заявителя разрешение."
    validate(source, "Полиция выписывает разрешение")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_ground_keeps_complete_open_vocabulary_detail(
    validate: RelationValidator,
) -> None:
    source = (
        "Разглашение государственной тайны особо охраняемого объекта "
        "является основанием для изъятия лицензии."
    )
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "Разглашение является основанием для изъятия лицензии",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_exclusion_is_order_invariant_but_unknown_residual_is_kept(
    validate: RelationValidator,
) -> None:
    source = "Полиция, кроме LSPD и LSSD, может выдавать лицензию."
    validate(
        source,
        "Полиция, кроме LSSD и LSPD, может выдавать лицензию",
    )
    disjoint = "Полиция, кроме прокуратуры Бета, может выдавать лицензию."
    validate(disjoint, "Полиция может выдавать лицензию")
    mixed = (
        "Полиция, кроме прокуратуры Бета и отдела Альфа, "
        "может выдавать лицензию."
    )
    _assert_rejected(
        validate,
        mixed,
        "Полиция может выдавать лицензию",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_compound_temporal_marker_and_subordinate_fact_are_preserved(
    validate: RelationValidator,
) -> None:
    source = (
        "После того как комиссия завершила проверку полиция может "
        "отозвать лицензию."
    )
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "До того как комиссия завершила проверку полиция может отозвать лицензию",
        "Перед тем как комиссия завершила проверку полиция может отозвать лицензию",
        "Если комиссия завершила проверку полиция может отозвать лицензию",
        "Полиция может отозвать лицензию",
        "Комиссия завершила проверку",
    )


@pytest.mark.parametrize(
    ("source", "honest"),
    (
        (
            "Полиция выдаёт лицензию после того как комиссия завершила "
            "проверку, а суд отзывает пропуск.",
            "Суд отзывает пропуск",
        ),
        (
            "Полиция выдаёт лицензию, а после того как комиссия завершила "
            "проверку суд отзывает пропуск.",
            "Полиция выдаёт лицензию",
        ),
    ),
    ids=("postfix-first-action", "prefix-second-action"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_compound_condition_is_local_to_its_governed_action(
    source: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    if validate is _validate_single_synonym:
        # The synonym-set validator also enforces campaign-level coverage of
        # every critical authority in this deliberately two-action source.
        # Include the source-owned companion surface so this test isolates
        # condition locality rather than aggregate coverage.
        validate_synonym_source_evidence(source, [source.rstrip("."), honest])
    else:
        validate(source, honest)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_compound_condition_keeps_embedded_permission_role(
    validate: RelationValidator,
) -> None:
    source = (
        "После того как комиссия с разрешения суда Альфа завершит проверку "
        "полиция может отозвать лицензию."
    )
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "После того как комиссия завершит проверку полиция может отозвать лицензию",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_compound_marker_inside_actor_name_is_not_a_condition(
    validate: RelationValidator,
) -> None:
    source = "Организация «После того как Альфа» может отозвать пропуск."
    validate(source, "Организация может отозвать пропуск")


@pytest.mark.parametrize(
    ("source", "honest", "unsafe_outputs"),
    (
        (
            "В случае нападения сотрудника на гражданина суд может "
            "отозвать лицензию.",
            "Суд может отозвать лицензию за нападение сотрудника на гражданина",
            (
                "Суд может отозвать лицензию за нападение гражданина на сотрудника",
            ),
        ),
        (
            "В случае передачи данных сотрудником сотруднику суд может "
            "отозвать лицензию.",
            "Суд может отозвать лицензию за передачу данных сотрудником сотруднику",
            (
                "Суд может отозвать лицензию за передачу данных сотрудником",
                "Суд может отозвать лицензию за передачу данных сотрудником "
                "сотруднику сотруднику",
            ),
        ),
        (
            "Если нарушены правила хранения реагентов в лаборатории Альфа "
            "или правила перевозки оружия в автомобиле Бета, полиция может "
            "изъять лицензию.",
            "Полиция может изъять лицензию за нарушение правил перевозки "
            "оружия в автомобиле Бета",
            (
                "Полиция может изъять лицензию за нарушение правил хранения",
                "Полиция может изъять лицензию за нарушение правил хранения "
                "оружия в автомобиле Бета",
                "Полиция может изъять лицензию за нарушение правил перевозки "
                "реагентов в лаборатории Альфа",
            ),
        ),
        (
            "Если нарушены правила хранения реагентов и правила перевозки "
            "токсинов, лицензия изымается.",
            "Лицензия изымается за нарушение правил хранения реагентов и "
            "правил перевозки токсинов",
            (
                "Лицензия изымается за нарушение правил хранения реагентов",
            ),
        ),
    ),
    ids=("role-order", "multiplicity", "alternative-branches", "conjunction"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_condition_ground_bridge_keeps_complete_ordered_branches(
    source: str,
    honest: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, *unsafe_outputs)


@pytest.mark.parametrize(
    "source",
    (
        "Если правила хранения не нарушены, лицензия изымается.",
        "Если нарушение правил хранения не выявлено, лицензия изымается.",
    ),
    ids=("not-violated", "not-detected"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_negated_condition_cannot_become_positive_ground(
    source: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(
        validate,
        source,
        "Лицензия изымается за нарушение правил хранения",
    )


@pytest.mark.parametrize(
    ("honest", "unsafe"),
    (
        (
            "Разглашение государственной тайны особо охраняемого объекта "
            "служит основанием для изъятия лицензии",
            "Разглашение служит основанием для изъятия лицензии",
        ),
        (
            "Разглашение государственной тайны особо охраняемого объекта "
            "представляет собой основание для изъятия лицензии",
            "Разглашение представляет собой основание для изъятия лицензии",
        ),
        (
            "Основанием для изъятия лицензии является разглашение "
            "государственной тайны особо охраняемого объекта",
            "Основанием для изъятия лицензии является разглашение",
        ),
    ),
    ids=("serves", "represents", "inverted"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_ground_common_copulas_keep_full_detail(
    honest: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Разглашение государственной тайны особо охраняемого объекта "
        "является основанием для изъятия лицензии."
    )
    validate(source, honest)
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "honest", "unsafe_outputs"),
    (
        (
            "Акт МВД является основанием для изъятия лицензии.",
            "Акт МВД служит основанием для изъятия лицензии",
            ("Акт ФСБ является основанием для изъятия лицензии",),
        ),
        (
            "Передача данных сотрудника гражданину является основанием "
            "для изъятия лицензии.",
            "Передача данных сотрудника гражданину служит основанием для "
            "изъятия лицензии",
            (
                "Передача данных гражданина сотруднику является основанием "
                "для изъятия лицензии",
            ),
        ),
        (
            "Передача данных сотрудником сотруднику является основанием "
            "для изъятия лицензии.",
            "Передача данных сотрудником сотруднику — основание для "
            "изъятия лицензии",
            (
                "Передача данных сотрудником является основанием для "
                "изъятия лицензии",
            ),
        ),
    ),
    ids=("short-agency", "role-order", "multiplicity"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_ground_keeps_short_tokens_order_and_multiplicity(
    source: str,
    honest: str,
    unsafe_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, *unsafe_outputs)


@pytest.mark.parametrize(
    ("source", "honest", "unsafe"),
    (
        (
            "Кража или мошенничество являются основанием для изъятия лицензии.",
            "Кража является основанием для изъятия лицензии",
            "Кража и мошенничество являются основанием для изъятия лицензии",
        ),
        (
            "Кража и мошенничество являются основанием для изъятия лицензии.",
            "Мошенничество и кража являются основанием для изъятия лицензии",
            "Кража или мошенничество являются основанием для изъятия лицензии",
        ),
    ),
    ids=("source-or", "source-and"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_ground_preserves_boolean_logic(
    source: str,
    honest: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    if validate is _validate_single_synonym:
        source_surface = source.rstrip(".")
        validate_synonym_source_evidence(source, [source_surface, honest])
        with pytest.raises(ValueError):
            validate_synonym_source_evidence(source, [source_surface, unsafe])
    else:
        validate(source, honest)
        _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_heading_does_not_split_quoted_colon_or_comma(
    validate: RelationValidator,
) -> None:
    source = (
        "Причины для аннулирования сертификата «Альфа: Бета»: "
        "разглашение организации «Рога, Копыта»; утрата допуска."
    )
    validate(
        source,
        "Разглашение организации «Рога, Копыта» является основанием для "
        "аннулирования сертификата «Альфа: Бета»",
    )
    _assert_rejected(
        validate,
        source,
        "Копыта является основанием для аннулирования сертификата "
        "«Альфа: Бета»",
        "Разглашение организации Рога является основанием для аннулирования "
        "сертификата «Альфа: Бета»",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nominal_multiline_heading_keeps_every_punctuated_item(
    validate: RelationValidator,
) -> None:
    source = (
        "Причины для аннулирования сертификата частного адвоката:\n"
        "Нарушение закона «О Коллегии адвокатов»\n"
        "Нарушение Уголовного Кодекса.\n"
        "Наличие судимости."
    )
    validate(
        source,
        "Судимость — основание для аннулирования сертификата "
        "частного адвоката",
    )
    _assert_rejected(
        validate,
        source,
        "Мошенничество — основание для аннулирования сертификата "
        "частного адвоката",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_bare_ground_heading_remains_a_topical_search_label(
    validate: RelationValidator,
) -> None:
    source = "AS ордер выдается судом."
    validate(source, "Основания для выдачи AS ордера")
    _assert_rejected(
        validate,
        source,
        "Кража — основание для выдачи AS ордера",
    )


def test_v3_topical_ground_adjunct_is_not_an_opaque_assertion() -> None:
    assert not _extract_explicit_ground_claims(
        "вызов прокурора при основаниях для освобождения"
    )
    assert _extract_explicit_ground_claims(
        "Кража создает основание для аннулирования лицензии"
    )


@pytest.mark.parametrize(
    "symbol",
    ("/", "&", "+"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_actor_exclusion_mixed_unknown_member_cannot_disappear(
    symbol: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Полиция, кроме прокуратуры Бета {symbol} отдела Альфа, "
        "может выдавать лицензию."
    )
    _assert_rejected(validate, source, "Полиция может выдавать лицензию")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_open_actor_and_ground_exclusion_sets_are_order_invariant(
    validate: RelationValidator,
) -> None:
    validate(
        "Полиция, кроме отдела Альфа и отдела Бета, может выдавать лицензию.",
        "Полиция, кроме отдела Бета и отдела Альфа, может выдавать лицензию",
    )
    validate(
        "Лицензия изымается за нарушение правил, кроме убийства и "
        "мошенничества.",
        "Лицензия изымается за нарушение правил, кроме мошенничества и убийства",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_quoted_actor_exclusion_keeps_modal_and_action_words_as_identity(
    validate: RelationValidator,
) -> None:
    modal_source = (
        "Полиция, кроме отдела «Может быть», может выдавать лицензию."
    )
    _assert_rejected(
        validate,
        modal_source,
        "Полиция, кроме отдела «Вправе быть», может выдавать лицензию",
    )
    action_source = (
        "Полиция, кроме отдела «Выдает Альфа», может выдавать лицензию."
    )
    validate(action_source, action_source.rstrip("."))
    _assert_rejected(
        validate,
        action_source,
        "Полиция выдаёт лицензию",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_exclusion_keeps_quoted_and_complete_list_tail(
    validate: RelationValidator,
) -> None:
    quoted = (
        "Лицензия изымается за нарушение правил, кроме тайны «Альфа, Бета»."
    )
    _assert_rejected(
        validate,
        quoted,
        "Лицензия изымается за нарушение правил, кроме тайны «Альфа, Гамма»",
    )
    listed = (
        "Лицензия изымается за нарушение правил, кроме Альфа, Бета и Гамма."
    )
    _assert_rejected(
        validate,
        listed,
        "Лицензия изымается за нарушение правил, кроме Альфа",
    )


@pytest.mark.parametrize(
    ("source", "honest", "unsafe"),
    (
        (
            "Полиция выписывает предписание для продления лицензии.",
            "Полиция выписывает предписание",
            "Полиция выписывает лицензию",
        ),
        (
            "Полиция выписывает предписание для получения заявителем "
            "разрешения.",
            "Полиция выписывает предписание",
            "Полиция выписывает разрешение",
        ),
        (
            "Полиция выписывает предписание для освобождения дома.",
            "Полиция выписывает предписание",
            "Полиция выписывает дом",
        ),
        (
            "Полиция выдаёт предписание для регистрации организации.",
            "Полиция выдаёт предписание",
            "Полиция выдаёт организацию",
        ),
    ),
    ids=("renewal", "receipt-holder", "release", "registration"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_open_vocabulary_purpose_cannot_become_direct_object(
    source: str,
    honest: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_direct_object_after_purpose_adjunct_remains_direct(
    validate: RelationValidator,
) -> None:
    source = "Полиция оформляет для проверки разрешение."
    validate(source, "Полиция оформляет разрешение")


@pytest.mark.parametrize(
    "date",
    ("2026/05/01", "2026.05.01", "01-05-2026", "1 мая 2026 года"),
    ids=("iso-slash", "iso-dot", "day-first", "named"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_complete_calendar_date_is_facet_not_actor_group(
    date: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд {date} может отозвать пропуск."
    validate(source, source.rstrip("."))
    _assert_rejected(
        validate,
        source,
        "Суд может отозвать пропуск",
        "Суд 2026/05/02 может отозвать пропуск",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ampersand_can_be_joint_actor_or_part_of_complete_name(
    validate: RelationValidator,
) -> None:
    joint = "FIB & USSS могут отозвать пропуск."
    validate(joint, joint.rstrip("."))
    named = "Организация Рога & Копыта может отозвать пропуск."
    validate(named, named.rstrip("."))
    _assert_rejected(
        validate,
        named,
        "Организация Рога & Крылья может отозвать пропуск",
    )


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Если нарушены правила организации «Альфа либо Бета», "
            "лицензия изымается.",
            "Лицензия изымается за нарушение правил организации «Альфа»",
        ),
        (
            "Если поврежден какой-либо документ, суд может отозвать лицензию.",
            "Суд может отозвать лицензию за повреждение документа",
        ),
        (
            "В случае выстрела по сотруднику суд может отозвать лицензию.",
            "Суд может отозвать лицензию за выстрел сотрудника",
        ),
    ),
    ids=("quoted-or", "hyphenated-libo", "semantic-preposition"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_condition_contract_cannot_drop_literal_or_role_detail(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Полиция, кроме отдела «Лицензия Альфа», может выдавать пособия.",
            "Полиция, кроме отдела «Лицензия Альфа», может выдавать лицензию",
        ),
        (
            "(по поручению отдела «Лицензия Альфа») полиция может "
            "выдавать пособия.",
            "(по поручению отдела «Лицензия Альфа») полиция может "
            "выдавать лицензию",
        ),
    ),
    ids=("actor-exclusion", "oblique-aside"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_nonpatient_role_name_cannot_supply_direct_object(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_heading_comma_cannot_drop_restrictive_modifier(
    validate: RelationValidator,
) -> None:
    source = (
        "Основания для изъятия лицензии:\n"
        "нападение сотрудника на гражданина, находящегося при исполнении."
    )
    _assert_rejected(
        validate,
        source,
        "Нападение сотрудника на гражданина является основанием для "
        "изъятия лицензии",
    )


@pytest.mark.parametrize(
    ("source_name", "invented_name"),
    (("Музей", "Муза"), ("Герой", "Гера"), ("Покров", "ПОКР")),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_exact_ground_name_has_no_stem_collision(
    source_name: str,
    invented_name: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Акт «{source_name}» является основанием для изъятия лицензии."
    )
    _assert_rejected(
        validate,
        source,
        f"Акт «{invented_name}» является основанием для изъятия лицензии",
    )


@pytest.mark.parametrize(
    "unsafe",
    (
        "Суд отзовёт лицензию",
        "Суд изымет лицензию",
        "Суд закроет организацию",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_common_future_action_cannot_escape_relation_gate(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, "Полиция выдаёт лицензию.", unsafe)


@pytest.mark.parametrize(
    "source",
    (
        "Если убийство отсутствует, лицензия изымается.",
        "Если убийство предотвращено, лицензия изымается.",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_semantic_absence_cannot_become_positive_ground(
    source: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, "Лицензия изымается за убийство")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_single_typed_ground_cannot_drop_actor_qualifier(
    validate: RelationValidator,
) -> None:
    source = (
        "Если совершено убийство сотрудником Альфа, лицензия изымается."
    )
    _assert_rejected(validate, source, "Лицензия изымается за убийство")


@pytest.mark.parametrize(
    "source",
    (
        "Основанием для аннулирования лицензии не является разглашение.",
        "Основанием для аннулирования лицензии может являться разглашение.",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_inverted_nominal_ground_keeps_polarity_and_modality(
    source: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(
        validate,
        source,
        "Разглашение является основанием для аннулирования лицензии",
    )


@pytest.mark.parametrize(
    "purpose_marker",
    ("с целью", "с намерением", "путём", "в ходе"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_open_purpose_marker_cannot_become_direct_object(
    purpose_marker: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Суд выписывает предписание {purpose_marker} получения лицензии."
    )
    validate(source, "Суд выписывает предписание")
    _assert_rejected(validate, source, "Суд выписывает лицензию")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_but_not_ground_exclusion_cannot_disappear(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может отозвать лицензию за нарушение закона, но не убийство."
    )
    _assert_rejected(
        validate,
        source,
        "Суд может отозвать лицензию за нарушение закона",
    )


@pytest.mark.parametrize("date", ("01.05.26", "1 мая"))
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_short_or_yearless_calendar_date_remains_required(
    date: str,
    validate: RelationValidator,
) -> None:
    source = f"Суд {date} может отозвать лицензию."
    validate(source, source.rstrip("."))
    _assert_rejected(validate, source, "Суд может отозвать лицензию")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_immediate_compound_temporal_qualifier_cannot_disappear(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может отозвать лицензию сразу после того как полиция "
        "подтвердит нарушение."
    )
    _assert_rejected(
        validate,
        source,
        "Суд может отозвать лицензию после того как полиция подтвердит "
        "нарушение",
    )


@pytest.mark.parametrize(
    ("source_code", "invented_code"),
    (("C&A", "C/A"), ("C++", "C#")),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_operator_is_part_of_exact_identity(
    source_code: str,
    invented_code: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Разглашение кода {source_code} является основанием для "
        "изъятия лицензии."
    )
    _assert_rejected(
        validate,
        source,
        f"Разглашение кода {invented_code} является основанием для "
        "изъятия лицензии",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_compact_plus_actor_group_keeps_both_members(
    validate: RelationValidator,
) -> None:
    source = "FIB+USSS могут отозвать пропуск."
    validate(source, "USSS может отозвать пропуск")


@pytest.mark.parametrize(
    ("source", "honest_outputs"),
    (
        (
            "Полиция выдаёт для заявителей лицензии.",
            ("Полиция выдаёт лицензии",),
        ),
        (
            "Если совершено (убийство или мошенничество) и похищение, "
            "лицензия изымается.",
            (
                "Лицензия изымается за убийство и похищение",
                "Лицензия изымается за мошенничество и похищение",
            ),
        ),
        (
            "Если совершены (убийство и мошенничество) или похищение, "
            "лицензия изымается.",
            (
                "Лицензия изымается за убийство и мошенничество",
                "Лицензия изымается за похищение",
            ),
        ),
        (
            "(Кража или мошенничество) и похищение являются основанием "
            "для изъятия лицензии.",
            (
                "Кража и похищение являются основанием для изъятия "
                "лицензии",
            ),
        ),
        (
            "Если совершено убийство или мошенничество не совершено, "
            "лицензия изымается.",
            ("Лицензия изымается за убийство",),
        ),
        (
            "Лицензия изымается за нарушение правил, но не убийство.",
            (
                "Лицензия изымается за нарушение правил, кроме убийства",
            ),
        ),
        (
            "Если нарушены правила хранения реагентов или перевозки оружия, "
            "лицензия изымается.",
            (
                "Лицензия изымается за нарушение правил перевозки оружия",
            ),
        ),
        (
            "Если нарушены правила, кроме убийства, лицензия изымается.",
            (
                "Лицензия изымается за нарушение правил, кроме убийства",
            ),
        ),
    ),
    ids=(
        "plural-beneficiary",
        "condition-dnf-or-and",
        "condition-dnf-and-or",
        "explicit-leading-dnf",
        "branch-local-negation",
        "ground-exclusion-equivalence",
        "shared-head-ellipsis",
        "condition-exclusion-equivalence",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_adjacent_honest_semantic_controls_remain_supported(
    source: str,
    honest_outputs: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    for output in honest_outputs:
        validate(source, output)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Если совершены (убийство и мошенничество) или похищение, "
            "лицензия изымается.",
            "Лицензия изымается за убийство",
        ),
        (
            "Если совершено (убийство или мошенничество) и похищение, "
            "лицензия изымается.",
            "Лицензия изымается за убийство",
        ),
        (
            "Если совершено убийство в музее или мошенничество, "
            "лицензия изымается.",
            "Лицензия изымается за убийство",
        ),
        (
            "Основаниями для изъятия лицензии являются: "
            "(кража или мошенничество) и похищение.",
            "Похищение является основанием для изъятия лицензии",
        ),
    ),
    ids=(
        "and-member-inside-or",
        "and-member-outside-or",
        "alternative-branch-qualifier",
        "explicit-heading-group",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_dnf_branch_must_remain_complete(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "patient",
    (
        "компанию",
        "предприятие",
        "учреждение",
        "банк",
        "ООО Альфа",
        "бизнес",
        "филиал",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_future_close_keeps_open_vocabulary_organization_patient(
    patient: str,
    validate: RelationValidator,
) -> None:
    unsafe = f"Суд закроет {patient}"
    _assert_rejected(validate, "Полиция выдаёт лицензию.", unsafe)
    validate(unsafe + ".", unsafe)


@pytest.mark.parametrize(
    "unsafe",
    (
        "Организация закроется по решению суда",
        "Компания закрывается по решению суда",
        "Банк закрылся по решению суда",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_reflexive_close_keeps_organization_as_patient(
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, "Полиция выдаёт лицензию.", unsafe)
    validate(unsafe + ".", unsafe)


@pytest.mark.parametrize(
    ("source", "unsafe"),
    (
        (
            "Лицензия изымается за кражу, но не убийство.",
            "Лицензия изымается за кражу",
        ),
        (
            "Лицензия изымается за кражу, кроме деяния Альфа.",
            "Лицензия изымается за кражу",
        ),
        (
            "Лицензия изымается за кражу.",
            "Лицензия изымается за кражу, но не убийство",
        ),
        (
            "Лицензия изымается за кражу, но не убийство.",
            "Лицензия изымается за кражу, но не мошенничество",
        ),
        (
            "Лицензия изымается за кражу, кроме деяния Альфа.",
            "Лицензия изымается за кражу, кроме деяния Бета",
        ),
        (
            "Лицензия изымается за кражу, но не убийство, "
            "мошенничество и коррупцию.",
            "Лицензия изымается за кражу, кроме убийства",
        ),
        (
            "Лицензия изымается за нарушение закона, но не акт "
            "«Суд выдаёт разрешение».",
            "Лицензия изымается за нарушение закона",
        ),
        (
            "Если нарушены правила, кроме убийства, лицензия изымается.",
            "Лицензия изымается за нарушение правил",
        ),
        (
            "Если совершена кража, кроме деяния Альфа, "
            "лицензия изымается.",
            "Лицензия изымается за кражу, кроме деяния Бета",
        ),
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_exclusion_cannot_be_dropped_added_or_replaced(
    source: str,
    unsafe: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_long_ground_exclusion_never_truncates_fail_open(
    validate: RelationValidator,
) -> None:
    shared = " ".join(["акт"] * 48)
    source = (
        f"Лицензия изымается за кражу, но не {shared} Альфа."
    )
    unsafe = f"Лицензия изымается за кражу, кроме {shared} Бета"
    _assert_rejected(validate, source, unsafe)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_true_negative_modal_contrast_stays_negative(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может отозвать лицензию за нарушение закона, "
        "но не обязан закрывать организацию."
    )
    _assert_rejected(
        validate,
        source,
        "Суд обязан закрыть организацию",
        "Суд может закрыть организацию",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_exclusion_parentheses_and_marker_are_equivalent(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия изымается за нарушение закона "
        "(кроме случаев убийства)."
    )
    validate(
        source,
        "Лицензия изымается за нарушение закона, но не убийство",
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_except_that_discourse_does_not_create_ground_exclusion(
    validate: RelationValidator,
) -> None:
    source = (
        "Суд может отозвать лицензию за нарушение закона, "
        "кроме того, полиция выдаёт ордер."
    )
    validate(source, "Суд может отозвать лицензию за нарушение закона")
    # A one-item synonym set deliberately has a separate whole-article
    # coverage rule.  The title path proves that the second clause itself is
    # no longer contaminated by the first relation.
    _validate_title(source, "Полиция выдаёт ордер")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_formula_owned_ground_blocks_partial_qualitative_bypass(
    validate: RelationValidator,
) -> None:
    source = (
        "Если совершены (убийство и мошенничество) или похищение, "
        "лицензия изымается."
    )
    _assert_rejected(
        validate,
        source,
        "Мошенничество является основанием для изъятия лицензии",
    )


@pytest.mark.parametrize(
    "negative_branch",
    (
        "факт убийства опровергнут",
        "совершение убийства запрещено",
        "совершение убийства невозможно",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_semantically_negative_dnf_branch_cannot_become_ground(
    negative_branch: str,
    validate: RelationValidator,
) -> None:
    source = (
        f"Если {negative_branch} или совершено мошенничество, "
        "лицензия изымается."
    )
    _assert_rejected(validate, source, "Лицензия изымается за убийство")


@pytest.mark.parametrize(
    ("source", "honest"),
    (
        (
            "Если не соблюдены требования перевозки, лицензия изымается.",
            "Лицензия изымается за несоблюдение требований перевозки",
        ),
        (
            "Если нарушен закон «Не убий», лицензия изымается.",
            "Лицензия изымается за нарушение закона «Не убий»",
        ),
        (
            "Если нарушены правила хранения реагентов или требования "
            "перевозки оружия, лицензия изымается.",
            "Лицензия изымается за нарушение требований перевозки оружия",
        ),
    ),
    ids=("positive-noncompliance", "quoted-law-name", "generic-head-switch"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_honest_condition_branch_forms_remain_searchable(
    source: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_terminal_group_keeps_complete_boolean_branch(
    validate: RelationValidator,
) -> None:
    source = (
        "Если совершено убийство или (мошенничество и похищение), "
        "лицензия изымается."
    )
    validate(source, "Лицензия изымается за убийство")
    validate(source, "Лицензия изымается за мошенничество и похищение")
    _assert_rejected(validate, source, "Лицензия изымается за мошенничество")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_conjunction_is_commutative_but_not_flattened(
    validate: RelationValidator,
) -> None:
    source = (
        "Если совершено (убийство или мошенничество) и похищение, "
        "лицензия изымается."
    )
    validate(source, "Лицензия изымается за похищение и убийство")
    _assert_rejected(validate, source, "Лицензия изымается за похищение")


@pytest.mark.parametrize(
    ("source", "honest"),
    (
        (
            "Если совершено (убийство или мошенничество) и похищение, "
            "лицензия изымается.",
            "Убийство и похищение являются основанием для изъятия "
            "лицензии",
        ),
        (
            "(Убийство или мошенничество) и похищение являются "
            "основанием для изъятия лицензии.",
            "Лицензия изымается за похищение и убийство",
        ),
    ),
    ids=("condition-to-explicit", "explicit-to-relation"),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_ground_formula_is_equivalent_across_surface_forms(
    source: str,
    honest: str,
    validate: RelationValidator,
) -> None:
    validate(source, honest)


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_formula_bridge_does_not_strengthen_may_to_definite(
    validate: RelationValidator,
) -> None:
    source = (
        "Убийство может являться основанием для изъятия лицензии."
    )
    _assert_rejected(validate, source, "Лицензия изымается за убийство")


@pytest.mark.parametrize(
    "source",
    (
        "Суд обязан проверить документы и после этого освободить заключённого.",
        "Суд обязан проверить документы, а затем освободить заключённого.",
        "Суд обязан проверить документы, затем освободить заключённого.",
        "Суд обязан проверить документы, после чего освободить заключённого.",
        "Суд обязан проверить документы, а затем немедленно освободить "
        "заключённого.",
        "Суд обязан проверить документы, освободить заключённого.",
    ),
    ids=(
        "after-that",
        "and-then",
        "then",
        "after-which",
        "then-neutral-adverb",
        "comma-list",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_one_modal_keeps_each_sequential_action_searchable(
    source: str,
    validate: RelationValidator,
) -> None:
    validate(source, "Суд обязан освободить заключённого")


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_organization_noun_does_not_capture_public_adjective(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор своим указом уполномочен закрыть любую территорию."
    )
    validate(
        source,
        "закрытие территорий указом губернатора для проведения мероприятий",
    )
    _assert_rejected(
        validate,
        source,
        "закрыли общественный участок по губернаторскому указу",
    )


@pytest.mark.parametrize(
    "surface",
    (
        "общественный участок",
        "банковский счёт",
        "фирменный магазин",
        "организационный отдел",
    ),
)
def test_v3_organization_patient_classifier_requires_a_noun(
    surface: str,
) -> None:
    assert (
        _relation_generic_object_head(surface, allow_actor_patient=True)
        != "organization"
    )


@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_access_to_territory_is_not_territory_closure(
    validate: RelationValidator,
) -> None:
    output = "Губернатор может закрыть доступ на любую территорию"
    assert not any(
        relation.predicate == "закры"
        and relation.direct_object == "territory"
        for relation in _extract_relation_truths(output)
    )
    validate(output + ".", output)


@pytest.mark.parametrize(
    "source",
    (
        "Суд обязан проверить документы, а затем прокуратуре освободить "
        "заключённого.",
        "Суд обязан проверить документы, а затем при наличии ордера "
        "освободить заключённого.",
        "Суд обязан проверить документы, а затем не освободить "
        "заключённого.",
        "Суд обязан проверить документы. Затем освободить заключённого.",
        "Суд может проверить документы, но не обязан освободить "
        "заключённого.",
    ),
    ids=(
        "fresh-actor",
        "new-condition",
        "negated-action",
        "hard-sentence",
        "next-modal",
    ),
)
@pytest.mark.parametrize(
    "validate",
    OUTPUT_PATHS,
    ids=("title", "single-synonym", "full-set"),
)
def test_v3_modal_scope_does_not_cross_an_unsafe_sequence_boundary(
    source: str,
    validate: RelationValidator,
) -> None:
    _assert_rejected(
        validate,
        source,
        "Суд обязан освободить заключённого",
    )
