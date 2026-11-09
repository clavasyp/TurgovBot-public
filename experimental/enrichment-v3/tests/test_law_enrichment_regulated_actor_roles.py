import pytest

from services.law_enrichment_validation import (
    validate_enrichment_source_evidence,
    validate_enrichment_title_source_evidence,
)


@pytest.mark.parametrize(
    ("source", "forged"),
    [
        (
            "По решению суда прокуратура выдаёт ордер.",
            "Суд выдаёт ордер",
        ),
        (
            "На основании решения суда прокуратура вправе выдать ордер.",
            "Суд вправе выдать ордер",
        ),
        (
            "Согласно решению прокуратуры полиция выдаёт ордер.",
            "Прокуратура выдаёт ордер",
        ),
    ],
)
def test_decision_authority_is_not_the_operative_actor_in_title(source, forged):
    with pytest.raises(ValueError):
        validate_enrichment_title_source_evidence(
            source,
            forged,
            source_title=None,
        )


@pytest.mark.parametrize(
    ("source", "forged"),
    [
        (
            "По решению суда прокуратура выдаёт ордер.",
            "суд выдаёт ордер",
        ),
        (
            "На основании решения суда прокуратура вправе выдать ордер.",
            "суд вправе выдать ордер",
        ),
        (
            "Согласно решению прокуратуры полиция выдаёт ордер.",
            "прокуратура выдаёт ордер",
        ),
    ],
)
def test_decision_authority_is_not_the_operative_actor_in_synonym(
    source,
    forged,
):
    with pytest.raises(ValueError):
        validate_enrichment_source_evidence(
            source,
            None,
            None,
            [forged],
        )


@pytest.mark.parametrize(
    ("source", "honest"),
    [
        (
            "По решению суда прокуратура выдаёт ордер.",
            "Прокуратура выдаёт ордер",
        ),
        (
            "На основании решения суда прокуратура вправе выдать ордер.",
            "Прокуратура вправе выдать ордер",
        ),
        (
            "Согласно решению прокуратуры полиция выдаёт ордер.",
            "Полиция выдаёт ордер",
        ),
    ],
)
def test_direct_operative_actor_controls_pass(source, honest):
    validate_enrichment_title_source_evidence(
        source,
        honest,
        source_title=None,
    )
    validate_enrichment_source_evidence(
        source,
        None,
        None,
        [honest],
    )

