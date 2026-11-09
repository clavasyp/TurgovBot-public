from __future__ import annotations

import pytest

from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
    validate_penalty_source_evidence,
    validate_synonym_source_evidence,
)


def reject_title(source: str, title: str) -> None:
    with pytest.raises(ValueError):
        validate_enrichment_title_source_evidence(
            source,
            title,
            source_title=None,
        )


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            "Суд обязан закрыть организацию.",
            "Прокуратура по решению суда обязана закрыть организацию",
        ),
        (
            "Полиция обязана обыскать дом.",
            "Прокуратура после уведомления полиции обязана обыскать дом",
        ),
        (
            "Суд обязан выдать лицензию.",
            "Прокуратура по решению суда обязана выдать лицензию",
        ),
    ],
)
def test_title_cannot_launder_leading_actor_through_trailing_context(
    source: str,
    title: str,
) -> None:
    reject_title(source, title)


@pytest.mark.parametrize(
    "title",
    [
        "Мэр выдал лицензию",
        "Лицензия выдана",
        "Суд закрыл организацию",
        "Организация подлежит закрытию",
        "Обязательное закрытие организации",
        "Суд автоматически закрывает организацию",
        "Суд всегда закрывает организацию",
    ],
)
def test_unsupported_finite_or_nominal_legal_event_is_not_claimless(
    title: str,
) -> None:
    reject_title(
        "Настоящая статья устанавливает порядок рассмотрения заявлений.",
        title,
    )


@pytest.mark.parametrize(
    "title",
    [
        "Мэр выдал лицензию",
        "Лицензия выдана",
        "Суд закрыл организацию",
        "Организация подлежит закрытию",
        "Обязательное закрытие организации",
        "Суд автоматически закрывает организацию",
        "Суд всегда закрывает организацию",
    ],
)
def test_same_finite_or_nominal_legal_event_remains_accepted(
    title: str,
) -> None:
    validate_enrichment_title_source_evidence(
        title + ".",
        title,
        source_title=None,
    )


@pytest.mark.parametrize(
    "synonym",
    [
        "Мэр выдал лицензию",
        "Лицензия выдана",
        "Суд закрыл организацию",
        "Организация подлежит закрытию",
        "Обязательное закрытие организации",
        "Суд автоматически закрывает организацию",
        "Суд всегда закрывает организацию",
    ],
)
def test_unsupported_finite_or_nominal_synonym_is_not_claimless(
    synonym: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            "Настоящая статья устанавливает общий порядок.",
            [synonym],
        )


@pytest.mark.parametrize(
    "synonym",
    [
        "Мэр выдал лицензию",
        "Лицензия выдана",
        "Суд закрыл организацию",
        "Организация подлежит закрытию",
        "Обязательное закрытие организации",
        "Суд автоматически закрывает организацию",
        "Суд всегда закрывает организацию",
    ],
)
def test_same_finite_or_nominal_synonym_honest_control(
    synonym: str,
) -> None:
    validate_synonym_source_evidence(synonym + ".", [synonym])


def test_duty_not_to_act_cannot_support_positive_duty_title() -> None:
    reject_title(
        "Суд обязан не освобождать заключённого.",
        "Суд обязан освобождать заключённого",
    )


def test_duty_not_to_act_honest_control() -> None:
    value = "Суд обязан не освобождать заключённого"
    validate_enrichment_title_source_evidence(
        value + ".",
        value,
        source_title=None,
    )


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            (
                "Если заявление одобрено, суд обязан выдать "
                "лицензию заявителю."
            ),
            "Суд обязан выдать лицензию заявителю",
        ),
        (
            (
                "При введении чрезвычайного положения губернатор "
                "обязан закрыть организацию."
            ),
            "Губернатор обязан закрыть организацию",
        ),
    ],
)
def test_condition_cannot_disappear_from_mandatory_title(
    source: str,
    title: str,
) -> None:
    reject_title(source, title)


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            (
                "Если заявление одобрено, суд обязан выдать "
                "лицензию заявителю."
            ),
            "Если заявление одобрено, суд обязан выдать лицензию заявителю",
        ),
        (
            (
                "При введении чрезвычайного положения губернатор "
                "обязан закрыть организацию."
            ),
            (
                "При введении чрезвычайного положения губернатор "
                "обязан закрыть организацию"
            ),
        ),
    ],
)
def test_condition_preserved_honest_control(
    source: str,
    title: str,
) -> None:
    validate_enrichment_title_source_evidence(
        source,
        title,
        source_title=None,
    )


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            (
                "Суд обязан проверить документы или освободить "
                "заключённого."
            ),
            "Суд обязан освободить заключённого",
        ),
        (
            (
                "Суд обязан проверить документы либо освободить "
                "заключённого."
            ),
            "Суд обязан освободить заключённого",
        ),
    ],
)
def test_alternative_duty_cannot_become_unconditional_single_duty(
    source: str,
    title: str,
) -> None:
    reject_title(source, title)


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            (
                "Суд обязан проверить документы и после этого освободить "
                "заключённого."
            ),
            "Суд обязан освободить заключённого",
        ),
        (
            (
                "Суд обязан проверить документы, а затем освободить "
                "заключённого."
            ),
            "Суд обязан освободить заключённого",
        ),
        (
            (
                "Суд обязан проверить документы, освободить "
                "заключённого."
            ),
            "Суд обязан освободить заключённого",
        ),
    ],
)
def test_mandatory_list_variants_accept_source_backed_second_action(
    source: str,
    title: str,
) -> None:
    validate_enrichment_title_source_evidence(
        source,
        title,
        source_title=None,
    )


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            "Суд согласно настоящему закону обязан закрыть организацию.",
            "Суд обязан закрыть организацию",
        ),
        (
            (
                "Прокуратура в пределах своих полномочий обязана "
                "возбудить уголовное дело."
            ),
            "Прокуратура обязана возбудить уголовное дело",
        ),
        (
            (
                "Полиция, являющаяся уполномоченным органом, обязана "
                "обыскать дом."
            ),
            "Полиция обязана обыскать дом",
        ),
    ],
)
def test_actor_with_intervening_legal_phrase_accepts_faithful_short_title(
    source: str,
    title: str,
) -> None:
    validate_enrichment_title_source_evidence(
        source,
        title,
        source_title=None,
    )


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            "Суд выдаёт лицензию.",
            "Суд выдаёт ордер на получение лицензии",
        ),
        (
            "Суд выдаёт лицензию.",
            "Суд выдаёт разрешение на обыск",
        ),
        (
            "Суд оформляет протокол для выдачи лицензии.",
            "Суд оформляет лицензию",
        ),
        (
            "Суд подписывает акт о выдаче лицензии.",
            "Суд подписывает лицензию",
        ),
    ],
)
def test_regulated_issue_object_must_be_direct(
    source: str,
    title: str,
) -> None:
    reject_title(source, title)


@pytest.mark.parametrize(
    "value",
    [
        "Суд выдаёт ордер на получение лицензии",
        "Суд выдаёт разрешение на обыск",
        "Суд оформляет лицензию",
        "Суд подписывает лицензию",
    ],
)
def test_regulated_issue_honest_controls(value: str) -> None:
    validate_enrichment_title_source_evidence(
        value + ".",
        value,
        source_title=None,
    )


@pytest.mark.parametrize(
    ("source", "synonym"),
    [
        (
            "Лицензия аннулирована организацией и заверена судом.",
            "суд аннулирует лицензию",
        ),
        (
            "Суд может аннулировать лицензию и обязан уведомить заявителя.",
            "суд обязан аннулировать лицензию",
        ),
        (
            (
                "Суд аннулирует лицензию и уведомляет заявителя "
                "о нарушении закона."
            ),
            "суд аннулирует лицензию за нарушение закона",
        ),
        (
            "Суд аннулирует лицензию и сообщает об убийстве.",
            "суд аннулирует лицензию за убийство",
        ),
        (
            "Штраф наложен организацией и квитанция заверена судом.",
            "суд наложил штраф",
        ),
        (
            (
                "Лицо наказывается максимально и уведомляет "
                "прокуратуру о статье 99 УК."
            ),
            "по статье 99 УК лицо наказывается максимально",
        ),
    ],
)
def test_unknown_neighbor_predicate_cannot_lend_semantics(
    source: str,
    synonym: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    ("source", "synonym"),
    [
        (
            "Лицензия аннулирована судом и заверена организацией.",
            "суд аннулирует лицензию",
        ),
        (
            "Суд обязан аннулировать лицензию и уведомить заявителя.",
            "суд обязан аннулировать лицензию",
        ),
        (
            "Суд аннулирует лицензию за нарушение закона.",
            "суд аннулирует лицензию за нарушение закона",
        ),
        (
            "Суд аннулирует лицензию за убийство.",
            "суд аннулирует лицензию за убийство",
        ),
        (
            "Штраф наложен судом.",
            "суд наложил штраф",
        ),
        (
            "По статье 99 УК лицо наказывается максимально.",
            "по статье 99 УК лицо наказывается максимально",
        ),
    ],
)
def test_neighbor_relation_honest_controls(
    source: str,
    synonym: str,
) -> None:
    validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    ("source", "synonym"),
    [
        (
            (
                "Суд аннулирует лицензию и уведомляет заявителя "
                "о нарушении закона."
            ),
            "суд аннулирует лицензию",
        ),
        (
            "Суд аннулирует лицензию и сообщает об убийстве.",
            "суд аннулирует лицензию",
        ),
        (
            "Лицензия аннулирована организацией и заверена судом.",
            "организация аннулирует лицензию",
        ),
    ],
)
def test_neighbor_fact_does_not_force_false_critical_coverage(
    source: str,
    synonym: str,
) -> None:
    validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            "Суд обязан освобождать заключённых.",
            "Суд обязан освободить заключённых",
        ),
        (
            "Сотрудник обязан предъявлять удостоверение.",
            "Сотрудник обязан предъявить удостоверение",
        ),
        (
            "Полиция обязана задерживать нарушителей.",
            "Полиция обязана задержать нарушителей",
        ),
    ],
)
def test_aspectual_paraphrase_should_not_be_false_blocked(
    source: str,
    title: str,
) -> None:
    validate_enrichment_title_source_evidence(
        source,
        title,
        source_title=None,
    )


@pytest.mark.parametrize(
    ("source", "output"),
    [
        (
            "Организация может аннулировать пропуск.",
            "Организация аннулировала пропуск",
        ),
        (
            "Организация вправе отозвать допуск.",
            "Организация отозвала допуск",
        ),
    ],
)
def test_optional_document_action_cannot_become_past_event(
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [output])


@pytest.mark.parametrize(
    ("source", "output"),
    [
        (
            "Организация может аннулировать пропуск.",
            "Организация аннулировала пропуск",
        ),
        (
            "Организация вправе отозвать допуск.",
            "Организация отозвала допуск",
        ),
    ],
)
def test_optional_document_action_cannot_become_past_title(
    source: str,
    output: str,
) -> None:
    reject_title(source, output)


@pytest.mark.parametrize(
    ("source", "output"),
    [
        (
            "Суд может выдать лицензию.",
            "Суд выдал лицензию",
        ),
        (
            "Прокуратура вправе депортировать гражданина.",
            "Прокуратура депортировала гражданина",
        ),
        (
            "Суд вправе лишить гражданства.",
            "Суд лишил гражданства",
        ),
    ],
)
def test_optional_regulated_action_cannot_become_past_synonym(
    source: str,
    output: str,
) -> None:
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(source, [output])


@pytest.mark.parametrize(
    ("source", "output"),
    [
        (
            "Организация аннулировала пропуск.",
            "Организация аннулировала пропуск",
        ),
        (
            "Организация отозвала допуск.",
            "Организация отозвала допуск",
        ),
    ],
)
def test_past_document_action_honest_control(
    source: str,
    output: str,
) -> None:
    validate_synonym_source_evidence(source, [output])
    validate_enrichment_title_source_evidence(
        source,
        output,
        source_title=None,
    )


def test_supported_penalty_cannot_hide_unrelated_mandatory_action() -> None:
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            "Наказание: штраф в размере 5000$.",
            "штраф 5000$ и суд обязан закрыть организацию",
            {"fine": "5000$"},
        )


def test_supported_penalty_honest_control() -> None:
    validate_penalty_source_evidence(
        "Наказание: штраф в размере 5000$.",
        "штраф 5000$",
        {"fine": "5000$"},
    )

