from __future__ import annotations

import pytest

from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
)


def test_source_owned_title_must_be_preserved_exactly() -> None:
    validate_enrichment_title_source_evidence(
        "Статья 1. Текст нормы.",
        "Название из источника",
        source_title="Название из источника",
    )

    with pytest.raises(
        ValueError,
        match="parser-owned source title must remain unchanged",
    ):
        validate_enrichment_title_source_evidence(
            "Статья 1. Текст нормы.",
            "Сгенерированная подмена",
            source_title="Название из источника",
        )


def test_titleless_article_allows_null_and_neutral_topical_title() -> None:
    validate_enrichment_title_source_evidence(
        "Судьи в своей деятельности независимы.",
        None,
        source_title=None,
    )
    validate_enrichment_title_source_evidence(
        "Судьи в своей деятельности независимы.",
        "Независимость судей",
        source_title=None,
    )


def test_neutral_title_can_name_a_typed_topic_present_in_source() -> None:
    validate_enrichment_title_source_evidence(
        "Отказ от уплаты штрафа и возмещения ущерба.",
        "Отказ оплатить штраф или ущерб",
        source_title=None,
    )


def test_generated_title_cannot_change_action_actor_or_modality() -> None:
    with pytest.raises(
        ValueError,
        match="source-unsupported semantic claim",
    ):
        validate_enrichment_title_source_evidence(
            "Лицензия может быть аннулирована организацией.",
            "Обязательное аннулирование лицензии судом",
            source_title=None,
        )


def test_generated_title_cannot_upgrade_optional_fine_to_mandatory() -> None:
    with pytest.raises(
        ValueError,
        match="mandatory consequence categories",
    ):
        validate_enrichment_title_source_evidence(
            "Нарушение может повлечь штраф.",
            "Обязательный штраф",
            source_title=None,
        )


def test_generated_title_cannot_substitute_revocation_ground() -> None:
    with pytest.raises(
        ValueError,
        match="source-unsupported semantic claim",
    ):
        validate_enrichment_title_source_evidence(
            "Суд может аннулировать лицензию за мошенничество.",
            "Аннулирование лицензии судом за опоздание",
            source_title=None,
        )


def test_source_backed_nominal_action_actor_and_ground_are_allowed() -> None:
    validate_enrichment_title_source_evidence(
        "Суд может аннулировать лицензию за мошенничество.",
        "Аннулирование лицензии судом за мошенничество",
        source_title=None,
    )


def test_parser_owned_document_context_can_support_code_family() -> None:
    validate_enrichment_title_source_evidence(
        "Наказание является мерой государственного принуждения.",
        "Понятие уголовного наказания",
        source_title=None,
        source_context="Уголовный кодекс штата",
    )


def test_structural_context_cannot_lend_operative_claim_to_article() -> None:
    with pytest.raises(
        ValueError,
        match="source-unsupported semantic claim",
    ):
        validate_enrichment_title_source_evidence(
            "Организация может аннулировать лицензию.",
            "Обязательное аннулирование лицензии судом",
            source_title=None,
            source_context=(
                "Глава о случаях обязательного аннулирования лицензии судом"
            ),
        )


@pytest.mark.parametrize(
    "generated_title",
    [
        "Суд обязан выдать лицензию",
        "Прокуратура обязана депортировать гражданина",
        "Суд обязательно лишает гражданства",
        "Суд обязан выдать ордер",
    ],
)
def test_generated_title_rejects_unknown_assertive_legal_actions(
    generated_title: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="title evidence",
    ):
        validate_enrichment_title_source_evidence(
            "Заявление подаётся в течение десяти дней.",
            generated_title,
            source_title=None,
        )


def test_structural_context_cannot_lend_negative_liability_claim() -> None:
    with pytest.raises(
        ValueError,
        match="title evidence",
    ):
        validate_enrichment_title_source_evidence(
            "Заявление подаётся в течение десяти дней.",
            "Уголовная ответственность отсутствует",
            source_title=None,
            source_context="Глава: уголовная ответственность отсутствует",
        )


def test_exact_body_source_prefix_is_not_treated_as_generated_claim() -> None:
    prefix = (
        "[Глава: обязательное аннулирование лицензии судом] — "
    )
    validate_enrichment_title_source_evidence(
        "Организация может аннулировать лицензию.",
        f"{prefix}Порядок применения нормы",
        source_title=None,
        trusted_title_prefix=prefix,
    )

    with pytest.raises(ValueError, match="trusted source prefix"):
        validate_enrichment_title_source_evidence(
            "Организация может аннулировать лицензию.",
            "Порядок применения нормы",
            source_title=None,
            trusted_title_prefix=prefix,
        )


@pytest.mark.parametrize(
    ("source", "invented_title"),
    [
        (
            (
                "Для выдачи лицензии заявитель обязан представить "
                "медицинскую справку."
            ),
            "Обязательная выдача лицензии",
        ),
        (
            (
                "Суд обязан проверить документы перед выдачей лицензии "
                "организацией."
            ),
            "Суд обязан выдать лицензию",
        ),
        (
            (
                "Прокуратура должна проверить основания до оформления "
                "ордера судом."
            ),
            "Прокуратура должна оформить ордер",
        ),
    ],
)
def test_neighbor_obligation_cannot_upgrade_regulated_action(
    source: str,
    invented_title: str,
) -> None:
    with pytest.raises(ValueError, match="title evidence"):
        validate_enrichment_title_source_evidence(
            source,
            invented_title,
            source_title=None,
        )


def test_source_backed_mandatory_issue_still_passes() -> None:
    validate_enrichment_title_source_evidence(
        "Суд обязан выдать лицензию заявителю.",
        "Суд обязан выдать лицензию",
        source_title=None,
    )


def test_negated_passive_issue_cannot_support_positive_title() -> None:
    with pytest.raises(ValueError, match="title evidence"):
        validate_enrichment_title_source_evidence(
            "Лицензия не выдается судом.",
            "Суд выдает лицензию",
            source_title=None,
        )


@pytest.mark.parametrize(
    ("source", "invented_title"),
    [
        (
            "Суд может выдать лицензию заявителю.",
            "Суд выдает лицензию",
        ),
        (
            "Прокуратура вправе депортировать гражданина.",
            "Прокуратура депортирует гражданина",
        ),
        (
            "Суд может лишить гражданства.",
            "Суд лишает гражданства",
        ),
    ],
)
def test_optional_regulated_action_cannot_become_definite(
    source: str,
    invented_title: str,
) -> None:
    with pytest.raises(ValueError, match="title evidence"):
        validate_enrichment_title_source_evidence(
            source,
            invented_title,
            source_title=None,
        )


@pytest.mark.parametrize(
    "invented_title",
    [
        "Суд обязан арестовать гражданина",
        "Полиция обязана обыскать дом",
        "Суд обязан конфисковать имущество",
        "Прокуратура обязана возбудить уголовное дело",
        "Губернатор обязан закрыть организацию",
        "Суд обязан разрешить обыск",
        "Полиция обязана задержать гражданина",
        "Суд обязан освободить заключенного",
        "Суд обязан отменить приговор",
    ],
)
def test_any_unknown_mandatory_legal_action_is_fail_closed(
    invented_title: str,
) -> None:
    with pytest.raises(ValueError, match="title evidence"):
        validate_enrichment_title_source_evidence(
            "Заявление подаётся в течение десяти дней.",
            invented_title,
            source_title=None,
        )


@pytest.mark.parametrize(
    "source_backed_title",
    [
        "Полиция обязана обыскать дом",
        "Прокуратура обязана возбудить уголовное дело",
        "Губернатор обязан закрыть организацию",
        "Суд обязан разрешить обыск",
        "Суд обязан освободить заключенного",
        "Суд обязан отменить приговор",
    ],
)
def test_unknown_mandatory_action_passes_with_same_source_predicate(
    source_backed_title: str,
) -> None:
    validate_enrichment_title_source_evidence(
        f"{source_backed_title}.",
        source_backed_title,
        source_title=None,
    )


def test_neighbor_mandatory_predicate_cannot_lend_unknown_action() -> None:
    with pytest.raises(ValueError, match="title evidence"):
        validate_enrichment_title_source_evidence(
            (
                "Полиция обязана обеспечить порядок перед обыском дома "
                "судом."
            ),
            "Полиция обязана обыскать дом",
            source_title=None,
        )


_GENERIC_STRONG_MODAL_TITLES = (
    "Суд обязан незамедлительно освободить заключённого",
    "Суд обязан либо освободить заключённого",
    "Суд обязан проверить материалы и освободить заключённого",
    "Освободить заключённого обязан суд",
    "Суду надлежит освободить заключённого",
    "Суд обязан в течение суток освободить заключённого",
    "Суд не обязан освободить заключённого",
    "Суд освобождает заключённого в обязательном порядке",
    "На суд возлагается обязанность освободить заключённого",
    "Федеральное бюро обязано закрыть организацию",
    "Правительство обязано утвердить бюджет",
    "Лицо обязано сообщить адрес",
    "Заявитель обязан представить справку",
    "Сотрудник обязан предъявить удостоверение",
)


@pytest.mark.parametrize("generated_title", _GENERIC_STRONG_MODAL_TITLES)
def test_any_strong_modal_grammar_is_fail_closed_for_neutral_source(
    generated_title: str,
) -> None:
    with pytest.raises(ValueError, match="title evidence"):
        validate_enrichment_title_source_evidence(
            "Настоящая статья устанавливает общий порядок рассмотрения.",
            generated_title,
            source_title=None,
        )


@pytest.mark.parametrize("source_backed_title", _GENERIC_STRONG_MODAL_TITLES)
def test_any_strong_modal_grammar_accepts_same_source_relation(
    source_backed_title: str,
) -> None:
    validate_enrichment_title_source_evidence(
        f"{source_backed_title}.",
        source_backed_title,
        source_title=None,
    )


def test_coordinated_mandatory_predicate_supports_shorter_exact_relation():
    validate_enrichment_title_source_evidence(
        "Суд обязан проверить материалы и освободить заключённого.",
        "Суд обязан освободить заключённого",
        source_title=None,
    )
