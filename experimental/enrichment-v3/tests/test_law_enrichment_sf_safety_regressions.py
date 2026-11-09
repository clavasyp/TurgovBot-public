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


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_anytime_revocation_scope_does_not_cross_objects(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть отозван руководителем в любое время, а "
        "лицензия может быть отозвана судом только после решения суда."
    )

    with pytest.raises(ValueError):
        validate(source, "лицензию можно отозвать без ожидания срока")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_negated_anytime_revocation_scope_is_not_positive_evidence(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск может быть отозван не в любое время, а только после "
        "решения суда."
    )

    with pytest.raises(ValueError):
        validate(source, "пропуск можно отозвать без ожидания срока")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_anytime_revocation_question_does_not_support_an_assertion(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Можно ли отозвать пропуск в любое время?",
            "пропуск можно отозвать без ожидания срока",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_nonassertive_relation_question_does_not_support_an_assertion(
    validate: RelationValidator,
) -> None:
    with pytest.raises(ValueError):
        validate(
            "Можно ли отозвать пропуск?",
            "пропуск можно отозвать",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_typed_ground_does_not_drop_a_conjunctive_condition(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим и инициирует конфликтные ситуации."
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
def test_typed_ground_does_not_drop_an_additional_prerequisite(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим и суд вынес решение."
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
def test_typed_ground_does_not_reverse_a_negated_condition(
    validate: RelationValidator,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо не нарушает "
        "нормальный режим."
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
def test_generated_active_participle_assertion_requires_source_evidence(
    validate: RelationValidator,
) -> None:
    source = "Суд ведёт реестр лицензий и действует законно."
    output = "Суд, аннулирующий лицензию, действует законно."

    with pytest.raises(ValueError):
        validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_negative_source_participle_does_not_support_positive_action(
    validate: RelationValidator,
) -> None:
    source = "Лицо, не аннулирующее лицензию, ведёт реестр."

    with pytest.raises(ValueError):
        validate(source, "лицо аннулирует лицензию")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_matching_object_positive_anytime_scope_is_supported(
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
@pytest.mark.parametrize(
    "output",
    (
        "губернатор вправе аннулировать пропуск нарушителю режима",
        "губернатор вправе отозвать допуск за провоцирование конфликта",
    ),
    ids=("regime", "conflict"),
)
def test_pure_positive_or_condition_supports_each_named_alternative(
    validate: RelationValidator,
    output: str,
) -> None:
    source = (
        "Губернатор вправе аннулировать пропуск, если лицо нарушает "
        "нормальный режим функционирования организации или инициирует "
        "конфликтные ситуации."
    )

    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_source_backed_active_participle_assertion_is_supported(
    validate: RelationValidator,
) -> None:
    source = "Суд аннулирует лицензию и действует законно."
    output = "Суд, аннулирующий лицензию, действует законно."

    validate(source, output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_active_issue_participle_requires_source_evidence(
    validate: RelationValidator,
) -> None:
    output = "Суд, выдающий паспорт, ведёт реестр."

    with pytest.raises(ValueError):
        validate("Суд ведёт реестр паспортов.", output)
    validate("Суд выдаёт паспорт и ведёт реестр.", output)


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_source_attributive_revocation_stays_context_only(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск не может быть выписан повторно без разрешения лица, "
        "его аннулировавшего."
    )

    validate(source, "согласие аннулировавшего на восстановление допуска")
    with pytest.raises(ValueError):
        validate(source, "суд аннулировал пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
def test_anaphoric_alternative_issuers_keep_each_revocation_authority(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск, выданный руководителем или заместителем руководителя "
        "государственной организации, может быть им отозван в любое время."
    )

    validate(source, "руководитель может отозвать пропуск")
    validate(
        source,
        "заместитель руководителя может отозвать пропуск",
    )
    with pytest.raises(ValueError):
        validate(source, "руководитель может отозвать территориальный пропуск")
    with pytest.raises(ValueError):
        validate(
            source,
            "заместитель руководителя может отозвать территориальный пропуск",
        )


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    ("source", "output"),
    (
        (
            "Пропуск, выданный руководителем организации, может быть им "
            "отозван в любое время.",
            "руководитель отозвал пропуск",
        ),
        (
            "Пропуск, выданный руководителем и директором, может быть ими "
            "отозван.",
            "руководитель отозвал пропуск",
        ),
        (
            "Лицензия, выданная организацией, может быть ею отозвана.",
            "организация отозвала лицензию",
        ),
    ),
    ids=("him", "them", "her"),
)
def test_anaphoric_prospective_revocation_is_not_a_past_fact(
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
def test_anaphoric_may_revocation_is_not_a_definite_present_fact(
    validate: RelationValidator,
) -> None:
    source = (
        "Пропуск, выданный руководителем организации, может быть им "
        "отозван в любое время."
    )

    with pytest.raises(ValueError):
        validate(source, "руководитель отзывает пропуск")


@pytest.mark.parametrize(
    "validate",
    RELATION_VALIDATORS,
    ids=("title", "synonym"),
)
@pytest.mark.parametrize(
    "output",
    (
        "суд отозвал пропуск",
        "прокуратура отозвала пропуск",
    ),
    ids=("court", "prosecution"),
)
def test_passive_alternative_actor_is_not_a_definite_past_actor(
    validate: RelationValidator,
    output: str,
) -> None:
    source = "Пропуск отозван судом или прокуратурой."

    with pytest.raises(ValueError):
        validate(source, output)
    validate(source, "пропуск отозван судом или прокуратурой")
