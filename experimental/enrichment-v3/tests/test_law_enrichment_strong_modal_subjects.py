import pytest

from services.law_enrichment_validation import (
    validate_enrichment_title_source_evidence,
)


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (
            "Полиция при наличии оснований обязана освободить заключённого.",
            "Суд при наличии оснований обязан освободить заключённого",
        ),
        (
            "Региональное бюро обязано закрыть организацию.",
            "Федеральное бюро обязано закрыть организацию",
        ),
        (
            "Правительство штата обязано утвердить бюджет.",
            "Парламент штата обязан утвердить бюджет",
        ),
        (
            "На региональное бюро возлагается обязанность закрыть организацию.",
            "На федеральное бюро возлагается обязанность закрыть организацию",
        ),
    ],
)
def test_strong_modal_actor_phrase_cannot_be_borrowed(source, title):
    with pytest.raises(ValueError):
        validate_enrichment_title_source_evidence(
            source,
            title,
            source_title=None,
        )


@pytest.mark.parametrize(
    "value",
    [
        "Полиция при наличии оснований обязана освободить заключённого",
        "Федеральное бюро обязано закрыть организацию",
        "Правительство штата обязано утвердить бюджет",
        "На федеральное бюро возлагается обязанность закрыть организацию",
    ],
)
def test_strong_modal_actor_phrase_honest_source_passes(value):
    validate_enrichment_title_source_evidence(
        value + ".",
        value,
        source_title=None,
    )

