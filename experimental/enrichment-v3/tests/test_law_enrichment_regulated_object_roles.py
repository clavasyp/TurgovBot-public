import pytest

from services.law_enrichment_validation import (
    validate_enrichment_source_evidence,
    validate_enrichment_title_source_evidence,
)


CASES = [
    (
        "Суд оформляет протокол для выдачи лицензии организацией.",
        "Суд оформляет лицензию",
        "суд оформляет лицензию",
    ),
    (
        "Прокуратура подписывает протокол до выдачи ордера судом.",
        "Прокуратура подписывает ордер",
        "прокуратура подписывает ордер",
    ),
    (
        "Полиция выписывает предписание для получения разрешения организацией.",
        "Полиция выписывает разрешение",
        "полиция выписывает разрешение",
    ),
    (
        "Ордер на получение лицензии оформляет суд.",
        "Суд оформляет лицензию",
        "суд оформляет лицензию",
    ),
]


@pytest.mark.parametrize(("source", "title", "_synonym"), CASES)
def test_regulated_object_must_be_direct_in_title(source, title, _synonym):
    with pytest.raises(ValueError):
        validate_enrichment_title_source_evidence(
            source,
            title,
            source_title=None,
        )


@pytest.mark.parametrize(("source", "_title", "synonym"), CASES)
def test_regulated_object_must_be_direct_in_synonym(source, _title, synonym):
    with pytest.raises(ValueError):
        validate_enrichment_source_evidence(
            source,
            None,
            None,
            [synonym],
        )


@pytest.mark.parametrize(
    "value",
    [
        "Суд оформляет лицензию",
        "Прокуратура подписывает ордер",
        "Полиция выписывает разрешение",
    ],
)
def test_direct_regulated_object_honest_controls_pass(value):
    validate_enrichment_title_source_evidence(
        value + ".",
        value,
        source_title=None,
    )
    validate_enrichment_source_evidence(
        value + ".",
        None,
        None,
        [value],
    )

