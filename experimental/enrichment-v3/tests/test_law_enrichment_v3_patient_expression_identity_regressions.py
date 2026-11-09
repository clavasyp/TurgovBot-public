from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _relation_bounded_patient_expression,
    _relation_document_patient_is_active_object,
    _relation_document_patient_passive_agrees,
    _relation_patient_expression_contract,
    _relation_patient_owner_tail_contract,
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


def _contract(surface: str) -> str:
    expression = _relation_bounded_patient_expression(surface)
    assert expression is not None
    return _relation_patient_expression_contract(expression)


def test_v3_homogeneous_patient_lists_are_identity_preserving_multisets() -> None:
    alpha_beta_and = _contract(
        "организацию «Альфа» и организацию «Бета»"
    )
    beta_alpha_and = _contract(
        "организацию «Бета» и организацию «Альфа»"
    )
    alpha_beta_or = _contract(
        "организацию «Альфа» или организацию «Бета»"
    )
    beta_alpha_or = _contract(
        "организацию «Бета» либо организацию «Альфа»"
    )

    assert alpha_beta_and == beta_alpha_and
    assert alpha_beta_or == beta_alpha_or
    assert alpha_beta_and != alpha_beta_or
    assert alpha_beta_and != _contract(
        "организацию «Альфа» и организацию «Гамма»"
    )
    assert alpha_beta_and != _contract(
        "организацию «Альфа» и организацию «Альфа»"
    )
    assert alpha_beta_and != _contract("организацию «Альфа»")


def test_v3_list_member_keeps_safe_weapon_scope_word_order_equivalence() -> None:
    assert _contract(
        "оружейную лицензию и организацию «Альфа»"
    ) == _contract(
        "организацию «Альфа» и лицензию на оружие"
    )


def test_v3_pure_comma_nary_patient_list_is_a_complete_multiset() -> None:
    comma_list = _contract("лицензию, разрешение, пропуск")
    assert comma_list == _contract("пропуск, лицензию, разрешение")
    assert comma_list == _contract("лицензию и разрешение и пропуск")
    assert comma_list != _contract("лицензию, лицензию, пропуск")
    assert comma_list != _contract("лицензию и разрешение")
    assert _relation_bounded_patient_expression(
        "лицензию, разрешение"
    ) is None


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_pure_comma_patient_list_reorders_but_cannot_drop_an_atom(
    validate: RelationValidator,
) -> None:
    source = "Суд аннулировал лицензию, разрешение, пропуск."
    validate(source, "Суд аннулировал пропуск, лицензию, разрешение")
    for unsafe in (
        "Суд аннулировал лицензию, разрешение",
        "Суд аннулировал лицензию, лицензию, пропуск",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source_joiner", "output_joiner"),
    (("и", "и"), ("или", "либо")),
    ids=("and", "or"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_named_patient_list_reorder_is_safe_but_identity_loss_is_not(
    source_joiner: str,
    output_joiner: str,
    validate: RelationValidator,
) -> None:
    source = (
        "Суд закрыл организацию «Альфа» "
        f"{source_joiner} организацию «Бета»."
    )
    faithful = (
        "Суд закрыл организацию «Бета» "
        f"{output_joiner} организацию «Альфа»"
    )
    validate(source, faithful)
    for unsafe in (
        (
            "Суд закрыл организацию «Альфа» "
            f"{source_joiner} организацию «Гамма»"
        ),
        (
            "Суд закрыл организацию «Альфа» "
            f"{source_joiner} организацию «Альфа»"
        ),
        "Суд закрыл организацию «Альфа»",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


@pytest.mark.parametrize(
    ("source", "faithful", "unsafe"),
    (
        (
            "Две организации были закрыты судом.",
            "Суд закрыл две организации",
            (
                "Суд закрыл организации",
                "Суд закрыл два организации",
            ),
        ),
        (
            "Несколько организаций были закрыты судом.",
            "Суд закрыл несколько организаций",
            (
                "Суд закрыл организации",
                "Суд закрыл несколько новые организации",
            ),
        ),
        (
            "Обе лицензии были аннулированы прокуратурой.",
            "Прокуратура аннулировала обе лицензии",
            (
                "Прокуратура аннулировала лицензии",
                "Прокуратура аннулировала оба лицензии",
            ),
        ),
    ),
    ids=("two-feminine", "several", "both-feminine"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_patient_cardinality_and_gender_survive_voice_paraphrase(
    source: str,
    faithful: str,
    unsafe: tuple[str, ...],
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    for output in unsafe:
        with pytest.raises(ValueError):
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
def test_v3_coordinated_patient_modifiers_remain_complete_and_agree(
    validate: RelationValidator,
) -> None:
    source = (
        "Новая и действующая лицензия была аннулирована прокуратурой."
    )
    faithful = (
        "Прокуратура аннулировала новую и действующую лицензию"
    )
    validate(source, faithful)
    for unsafe in (
        "Прокуратура аннулировала новый и действующую лицензию",
        "Прокуратура аннулировала новую и действующий лицензию",
        "Прокуратура аннулировала новую лицензию",
    ):
        with pytest.raises(ValueError):
            validate(source, unsafe)


def test_v3_number_scope_owner_tail_is_consumed_in_order() -> None:
    section_sign = _relation_patient_owner_tail_contract(
        "№ 7 на оружие владельца Альфа"
    )
    written_number = _relation_patient_owner_tail_contract(
        "номер 7 на оружие владельца Альфа"
    )
    wrong_owner_case = _relation_patient_owner_tail_contract(
        "№ 7 на оружие владельцу Альфа"
    )

    assert section_sign
    assert section_sign == written_number
    assert wrong_owner_case.startswith("unresolved:")
    assert section_sign != wrong_owner_case


@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_numbered_scoped_patient_keeps_its_exact_owner(
    validate: RelationValidator,
) -> None:
    source = (
        "Прокуратура аннулировала лицензию № 7 на оружие "
        "владельца Альфа."
    )
    faithful = (
        "Лицензия номер 7 на оружие владельца Альфа была "
        "аннулирована прокуратурой"
    )
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(
            source,
            "Прокуратура аннулировала лицензию № 7 на оружие "
            "владельцу Альфа",
        )


@pytest.mark.parametrize(
    ("source", "faithful", "wrong_modifier"),
    (
        (
            "Полиция задержала разыскиваемых лиц.",
            "Разыскиваемых лиц задержала полиция",
            "Разыскиваемые лиц задержала полиция",
        ),
        (
            "Суд назначил новые обыски.",
            "Новые обыски назначил суд",
            "Новых обыски назначил суд",
        ),
        (
            "Организация утвердила новые бюджеты.",
            "Новые бюджеты утвердила организация",
            "Новых бюджеты утвердила организация",
        ),
    ),
    ids=("animate-persons", "inanimate-searches", "inanimate-budgets"),
)
@pytest.mark.parametrize(
    "validate",
    ALL_OUTPUT_PATHS,
    ids=(
        "title",
        "single-synonym",
        "full-set",
        "penalty-summary",
        "penalty-other",
    ),
)
def test_v3_accusative_plural_patient_maps_keep_real_modifier_agreement(
    source: str,
    faithful: str,
    wrong_modifier: str,
    validate: RelationValidator,
) -> None:
    validate(source, faithful)
    with pytest.raises(ValueError):
        validate(source, wrong_modifier)


def test_v3_patient_case_helpers_cover_cardinality_and_plural_animacy() -> None:
    assert _relation_document_patient_passive_agrees(
        "Две организации",
        "закрыты",
    ) is True
    assert _relation_document_patient_passive_agrees(
        "Два организации",
        "закрыты",
    ) is False
    assert _relation_document_patient_passive_agrees(
        "Несколько организаций",
        "закрыты",
    ) is True
    assert _relation_document_patient_is_active_object(
        "разыскиваемых лиц"
    ) is True
    assert _relation_document_patient_is_active_object(
        "разыскиваемые лиц"
    ) is False
    assert _relation_document_patient_is_active_object("новые обыски") is True
    assert _relation_document_patient_is_active_object("новые бюджеты") is True
