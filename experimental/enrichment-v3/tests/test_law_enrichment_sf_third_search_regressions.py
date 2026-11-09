from __future__ import annotations

from collections.abc import Callable

import pytest

from services.law_enrichment_validation import (
    _validate_relation_source_evidence,
    validate_enrichment_title_source_evidence,
)


RelationValidator = Callable[[str, str], None]


def _validate_title(source: str, output: str) -> None:
    validate_enrichment_title_source_evidence(
        source,
        output,
        source_title=None,
    )


def _validate_synonym(source: str, output: str) -> None:
    _validate_relation_source_evidence(
        source,
        output,
        field="synonym 0",
    )


RELATION_VALIDATORS: tuple[RelationValidator, ...] = (
    _validate_title,
    _validate_synonym,
)


NEGATED_ANYTIME_CASES = (
    (
        "Пропуск может быть отозван судом без возможности сделать это "
        "в любое время.",
        "пропуск можно отозвать без ожидания срока",
    ),
    (
        "Право отозвать пропуск в любое время отсутствует.",
        "пропуск можно отозвать без ожидания срока",
    ),
    (
        "Пропуск может быть отозван судом, хотя отзыв в любое время "
        "запрещён.",
        "пропуск можно отозвать без ожидания срока",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    NEGATED_ANYTIME_CASES,
    ids=("without-possibility", "absent", "later-prohibition"),
)
def test_negative_temporal_envelope_is_not_positive_anytime_evidence(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


QUESTION_HEADING_CASES = (
    "Вопрос: можно ли отозвать пропуск в любое время?",
    "Вопрос: допускается ли отозвать пропуск в любое время?",
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "source",
    QUESTION_HEADING_CASES,
    ids=("headed-can", "headed-permitted"),
)
def test_question_heading_does_not_support_anytime_assertion(
    validate: RelationValidator,
    source: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "пропуск можно отозвать без ожидания срока")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_management_alias_does_not_move_temporal_scope_between_actors(
    validate: RelationValidator,
) -> None:
    source = (
        "Руководитель может отозвать пропуск в любое время, а директор "
        "может отозвать пропуск."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "директор может отозвать пропуск без ожидания срока",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_typed_ground_does_not_drop_requirement_inside_or_branch(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим при наличии решения суда или инициирует конфликт."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "губернатор вправе аннулировать пропуск нарушителю режима",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "residual",
    (
        "во время чрезвычайного положения",
        "на территории тюрьмы",
        "с санкции суда",
    ),
    ids=("emergency", "territory", "court-sanction"),
)
def test_single_typed_ground_does_not_drop_unknown_residual_requirement(
    validate: RelationValidator,
    residual: str,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        f"нормальный режим {residual}."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "губернатор вправе аннулировать пропуск нарушителю режима",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_as_well_as_condition_is_conjunctive(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим, а также инициирует конфликт."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "губернатор вправе аннулировать пропуск нарушителю режима",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "source",
    (
        "Суд может отозвать лицензию?",
        "Вопрос: может ли суд отозвать лицензию?",
        "Суд может ли отозвать лицензию?",
    ),
    ids=("trailing-question", "headed", "modal-inversion"),
)
def test_any_question_shape_cannot_support_assertive_revocation(
    validate: RelationValidator,
    source: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "суд может отозвать лицензию")


UNSUPPORTED_ACTIVE_PARTICIPLE_CASES = (
    (
        "Суд ведёт реестр лицензий и действует законно.",
        "Аннулирующий лицензию суд действует законно.",
    ),
    (
        "Суд ведёт реестр лицензий и действует законно.",
        "Суд, лицензию аннулирующий, действует законно.",
    ),
    (
        "Суд ведёт реестр лицензий и действует законно.",
        "Суд аннулирующий лицензию действует законно.",
    ),
    (
        "Директор FIB ведёт реестр лицензий и действует законно.",
        "Директор FIB, аннулирующий лицензию, действует законно.",
    ),
    (
        "Суд ведёт реестр паспортов и действует законно.",
        "Суд, паспорт выдающий, действует законно.",
    ),
    (
        "Суд ведёт реестр паспортов и действует законно.",
        "Выдающий паспорт суд действует законно.",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    UNSUPPORTED_ACTIVE_PARTICIPLE_CASES,
    ids=(
        "inverted",
        "object-first",
        "missing-commas",
        "latin-actor",
        "issue-object-first",
        "issue-inverted",
    ),
)
def test_active_participle_word_order_cannot_bypass_source_evidence(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_management_qualifiers_are_not_laundered_by_revocation_alias(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Руководитель FIB вправе аннулировать пропуск.",
            "директор LSPD вправе аннулировать пропуск",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "source",
    (
        "Директор вправе выдать лицензию.",
        "Директор выдаёт лицензию.",
    ),
    ids=("may", "definite"),
)
def test_issue_management_actor_is_exact_not_a_revocation_alias(
    validate: RelationValidator,
    source: str,
) -> None:
    with pytest.raises(ValueError):
        validate(source, "руководитель выдаёт лицензию")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "output",
    (
        "губернатор вправе аннулировать пропуск нарушителю режима",
        "губернатор вправе отозвать допуск за провоцирование конфликта",
    ),
    ids=("regime", "conflict"),
)
def test_and_or_condition_permits_each_named_alternative(
    validate: RelationValidator,
    output: str,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим и/или инициирует конфликт."
    )

    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_matching_relation_keeps_positive_anytime_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск, выданный руководителем организации, может быть им "
        "отозван в любое время."
    )

    validate(source, "можно отозвать пропуск без ожидания срока")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_qualified_different_actor_does_not_inherit_anytime_scope(
    validate: RelationValidator,
) -> None:
    source = (
        "Руководитель FIB может отозвать пропуск в любое время, а "
        "директор LSPD может отозвать пропуск."
    )

    with pytest.raises(ValueError):
        validate(
            source,
            "директор LSPD может отозвать пропуск без ожидания срока",
        )


ALTERNATIVE_CONDITION_SOURCES = (
    (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим или инициирует конфликт.",
        "or",
    ),
    (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим, инициирует конфликт.",
        "comma",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "syntax"),
    ALTERNATIVE_CONDITION_SOURCES,
    ids=("or", "comma"),
)
@pytest.mark.parametrize(
    "output",
    (
        "губернатор вправе аннулировать пропуск нарушителю режима",
        "губернатор вправе отозвать допуск за провоцирование конфликта",
    ),
    ids=("regime", "conflict"),
)
def test_true_alternative_condition_supports_each_named_ground(
    validate: RelationValidator,
    source: str,
    syntax: str,
    output: str,
) -> None:
    del syntax
    validate(source, output)


SOURCE_BACKED_ACTIVE_PARTICIPLE_CASES = (
    (
        "Суд аннулирует лицензию и действует законно.",
        "Суд, аннулирующий лицензию, действует законно.",
    ),
    (
        "Суд аннулирует лицензию и действует законно.",
        "Аннулирующий лицензию суд действует законно.",
    ),
    (
        "Суд аннулирует лицензию и действует законно.",
        "Суд, лицензию аннулирующий, действует законно.",
    ),
    (
        "Директор FIB аннулирует лицензию и действует законно.",
        "Директор FIB, аннулирующий лицензию, действует законно.",
    ),
    (
        "Суд выдаёт паспорт и ведёт реестр.",
        "Суд, паспорт выдающий, ведёт реестр.",
    ),
    (
        "Суд выдаёт паспорт и ведёт реестр.",
        "Выдающий паспорт суд ведёт реестр.",
    ),
)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    SOURCE_BACKED_ACTIVE_PARTICIPLE_CASES,
    ids=(
        "standard",
        "inverted",
        "object-first",
        "latin-actor",
        "issue-object-first",
        "issue-inverted",
    ),
)
def test_source_backed_active_participle_word_orders_are_supported(
    validate: RelationValidator,
    source: str,
    output: str,
) -> None:
    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_ordinary_svo_inversion_still_requires_source_evidence(
    validate: RelationValidator,
) -> None:
    output = "Лицензию аннулирует суд."

    with pytest.raises(ValueError):
        validate("Прокуратура ведёт реестр лицензий.", output)
    validate("Суд аннулирует лицензию.", output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_revocation_management_roles_remain_exact(
    validate: RelationValidator,
) -> None:
    source = "Руководитель вправе аннулировать пропуск."
    with pytest.raises(ValueError):
        validate(source, "директор вправе аннулировать пропуск")
    validate(source, "руководитель вправе аннулировать пропуск")
    validate(
        "Директор выдаёт лицензию.",
        "директор выдаёт лицензию",
    )
