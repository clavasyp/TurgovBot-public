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


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_anytime_scope_is_not_part_of_pass_identity(
    validate: RelationValidator,
) -> None:
    source = "Пропуск №1 может быть отозван в любое время."
    validate(source, "можно отозвать пропуск №1 без ожидания срока")
    with pytest.raises(ValueError):
        validate(source, "можно отозвать пропуск без ожидания срока")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_anytime_words_inside_a_name_do_not_create_temporal_power(
    validate: RelationValidator,
) -> None:
    source = "Пропуск «Без ожидания срока» может быть отозван судом."
    validate(source, "суд может отозвать пропуск «Без ожидания срока»")
    with pytest.raises(ValueError):
        validate(
            source,
            "суд может отозвать пропуск «Без ожидания срока» "
            "без ожидания срока",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_active_participle_patient_stops_before_postposed_actor(
    validate: RelationValidator,
) -> None:
    source = "Суд выдаёт паспорт и ведёт реестр."
    validate(source, "Выдающий паспорт суд ведёт реестр")
    with pytest.raises(ValueError):
        validate(
            "Суд ведёт реестр паспортов.",
            "Выдающий паспорт суд ведёт реестр",
        )


def test_v3_inverted_active_participle_extracts_a_real_issue_truth() -> None:
    truths = _extract_relation_truths("Выдающий паспорт суд ведёт реестр.")
    assert any(
        truth.assertive
        and truth.predicate == "regulated_issue"
        and truth.direct_object == "passport"
        and truth.operative_actor == "суд"
        for truth in truths
    )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_governing_decision_actor_is_bounded(
    validate: RelationValidator,
) -> None:
    validate(
        "Суд вынес решение об аннулировании лицензии.",
        "суд аннулирует лицензию",
    )
    for unsupported_source in (
        "Суд обсудил решение об аннулировании лицензии.",
        "Суд не вынес решение об аннулировании лицензии.",
        "Прокурор вынес решение об аннулировании лицензии.",
    ):
        with pytest.raises(ValueError):
            validate(unsupported_source, "суд аннулирует лицензию")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
@pytest.mark.parametrize(
    ("positive_source", "negative_source", "output"),
    (
        (
            "Суд постановил аннулировать лицензию.",
            "Суд не постановил аннулировать лицензию.",
            "суд аннулирует лицензию",
        ),
        (
            "Суд решил отозвать лицензию.",
            "Суд не решил отозвать лицензию.",
            "суд отзывает лицензию",
        ),
    ),
)
def test_v3_negated_governing_decision_never_proves_positive_action(
    validate: RelationValidator,
    positive_source: str,
    negative_source: str,
    output: str,
) -> None:
    validate(positive_source, output)
    with pytest.raises(ValueError):
        validate(negative_source, output)
    negative_truths = _extract_relation_truths(negative_source)
    assert any(
        truth.predicate == "regulated_revocation"
        and truth.polarity == "negative"
        and truth.operative_actor == ""
        for truth in negative_truths
    )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_warrant_purpose_nominalization_is_closed_and_exact(
    validate: RelationValidator,
) -> None:
    source = "Ордер на проведение обыска выписывает губернатор штата."
    validate(source, "губернатор выдаёт ордер на обыск")
    with pytest.raises(ValueError):
        validate(source, "губернатор выдаёт ордер на рейд")
    with pytest.raises(ValueError):
        validate(source, "губернатор выдаёт ордер")


VALIDITY_SOURCE = (
    "Документ, предоставляющий допуск к государственной тайне, "
    "считается действительным при соблюдении следующих условий:\n"
    "Подписан уполномоченным должностным лицом.\n"
    "Опубликован на официальном портале."
)


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_validity_item_inherits_complete_heading_patient(
    validate: RelationValidator,
) -> None:
    validate(
        VALIDITY_SOURCE,
        "документ, предоставляющий допуск к государственной тайне, "
        "должен быть подписан уполномоченным должностным лицом",
    )
    with pytest.raises(ValueError):
        validate(
            VALIDITY_SOURCE,
            "допуск должен быть подписан уполномоченным должностным лицом",
        )


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_issue_decision_scaffold_is_one_way_history(
    validate: RelationValidator,
) -> None:
    source = "Решение о выдаче допуска оформляется приказом."
    validate(source, "решение о допуске оформляется приказом")
    with pytest.raises(ValueError):
        validate(
            "Решение о допуске оформляется приказом.",
            "решение о выдаче допуска оформляется приказом",
        )
    with pytest.raises(ValueError):
        validate(source, "выдача допуска приказом")
    with pytest.raises(ValueError):
        validate(source, "решение о выдаче допуска оформляется указом")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_detention_seizure_actor_does_not_override_explicit_actor(
    validate: RelationValidator,
) -> None:
    source = (
        "Лицензия должна быть изъята судом при задержании сотрудниками "
        "правоохранительных органов."
    )
    validate(source, "суд обязан изъять лицензию при задержании")
    with pytest.raises(ValueError):
        validate(source, "правоохранители обязаны изъять лицензию")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_detention_actor_bridge_is_not_open_vocabulary(
    validate: RelationValidator,
) -> None:
    source = "Лицензия должна быть изъята при задержании гражданином."
    validate(source, "лицензия должна быть изъята при задержании гражданином")
    with pytest.raises(ValueError):
        validate(source, "гражданин обязан изъять лицензию")


@pytest.mark.parametrize("validate", OUTPUT_PATHS)
def test_v3_nominal_subject_to_modal_gap_is_not_patient_identity(
    validate: RelationValidator,
) -> None:
    validate("Изъятию подлежит лицензия.", "изъятие лицензии")
    with pytest.raises(ValueError):
        validate("Изъятию подлежит лицензия.", "изъятие пропуска")
