import pytest

from services.law_enrichment_validation import (
    validate_enrichment_source_evidence,
)


@pytest.mark.parametrize(
    "synonym",
    [
        "суд обязан освободить заключённого",
        "федеральное бюро обязано закрыть организацию",
        "лицо обязано сообщить адрес",
        "на суд возлагается обязанность освободить заключённого",
        "освободить заключённого обязан суд",
    ],
)
def test_unknown_strong_modal_synonym_rejects_neutral_source(synonym):
    with pytest.raises(ValueError):
        validate_enrichment_source_evidence(
            "Настоящая статья определяет общий порядок рассмотрения дела.",
            None,
            None,
            [synonym],
        )


@pytest.mark.parametrize(
    "synonym",
    [
        "суд обязан освободить заключённого",
        "федеральное бюро обязано закрыть организацию",
        "лицо обязано сообщить адрес",
        "на суд возлагается обязанность освободить заключённого",
        "освободить заключённого обязан суд",
    ],
)
def test_unknown_strong_modal_synonym_accepts_honest_source(synonym):
    validate_enrichment_source_evidence(
        synonym + ".",
        None,
        None,
        [synonym],
    )

