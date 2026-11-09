import pytest

from services.law_search_ranking import (
    ApplicabilityInterval,
    QueryMeasure,
    applicability_verdict,
    build_evidence_query,
    evaluate_applicability,
    extract_applicability_intervals,
    extract_query_measures,
    lexical_dominance_order,
)


def test_query_measure_keeps_value_bound_to_its_unit():
    assert extract_query_measures(
        "при себе 10 грамм наркотиков",
        ["10"],
    ) == [QueryMeasure(value=10.0, unit="mass_g")]
    assert extract_query_measures(
        "имущество на 5000 долларов",
        ["5000"],
    ) == [QueryMeasure(value=5000.0, unit="money_usd")]
    assert extract_query_measures(
        "имущество на $ 5000",
        ["5000"],
    ) == [QueryMeasure(value=5000.0, unit="money_usd")]
    assert extract_query_measures(
        "при себе 10 г наркотиков",
        ["10"],
    ) == [QueryMeasure(value=10.0, unit="mass_g")]
    assert extract_query_measures(
        "получил 25% голосов",
        ["25"],
    ) == [QueryMeasure(value=25.0, unit="percent")]


def test_spoken_number_uses_numeric_shadow_without_changing_search_surface():
    assert extract_query_measures(
        "при себе десять 10 грамм наркотиков",
        ["10"],
    ) == [QueryMeasure(value=10.0, unit="mass_g")]


def test_grouped_and_scaled_money_values_are_not_misread():
    assert extract_query_measures(
        "имущество на 5 000 долларов",
        ["5", "000"],
    ) == [QueryMeasure(value=5000.0, unit="money_usd")]
    assert extract_query_measures(
        "имущество на 10 тысяч долларов",
        ["10"],
    ) == [QueryMeasure(value=10000.0, unit="money_usd")]
    assert extract_query_measures(
        "имущество на 2 миллиарда долларов",
        ["2"],
    ) == [QueryMeasure(value=2_000_000_000.0, unit="money_usd")]
    assert extract_query_measures(
        "имущество на 10 тыс долларов",
        ["10"],
    ) == [QueryMeasure(value=10000.0, unit="money_usd")]


def test_query_measure_requires_adjacent_unit_or_single_scale():
    assert extract_query_measures(
        "нашли 5 частей и 10 грамм вещества",
        ["5", "10"],
    ) == []
    assert extract_query_measures(
        "нашли 5 и грамм вещества",
        ["5"],
    ) == []
    assert extract_query_measures(
        "стоимость долларов 5000",
        ["5000"],
    ) == []
    assert extract_query_measures(
        "стоимость $ и 5000",
        ["5000"],
    ) == []
    assert extract_query_measures(
        "получил % 25 голосов",
        ["25"],
    ) == [QueryMeasure(value=25.0, unit="percent")]


def test_query_measure_handles_typed_commas_scales_and_mixed_fail_closed():
    assert extract_query_measures(
        "имущество на $50,000",
        ["50000"],
    ) == [QueryMeasure(value=50000.0, unit="money_usd")]
    assert extract_query_measures(
        "масса 5,500 грамм",
        ["5.5"],
    ) == [QueryMeasure(value=5.5, unit="mass_g")]
    assert extract_query_measures(
        "имущество на $5 тыс",
        ["5"],
    ) == [QueryMeasure(value=5000.0, unit="money_usd")]
    assert extract_query_measures(
        "5 долларов и 10 лет",
        ["5", "10"],
    ) == []
    assert extract_query_measures(
        "80 плюс 1 голос",
        ["80", "1"],
    ) == []
    assert extract_query_measures(
        "сумма $5 рублей",
        ["5"],
    ) == []


def test_query_measure_converts_physical_units_after_semantic_number_match():
    assert extract_query_measures(
        "нашли 5 кг наркотиков",
        ["5"],
    ) == [QueryMeasure(value=5000.0, unit="mass_g")]
    assert extract_query_measures(
        "нашли 5 мг наркотиков",
        ["5"],
    ) == [QueryMeasure(value=0.005, unit="mass_g")]
    assert extract_query_measures(
        "нашли 0.005 грамма наркотиков",
        ["0.005"],
    ) == [QueryMeasure(value=0.005, unit="mass_g")]


def test_range_uncertainty_and_compound_scale_fail_closed():
    assert extract_query_measures(
        "нашли от 5 до 10 грамм вещества",
        ["5", "10"],
    ) == []
    assert extract_query_measures(
        "нашли 5-10 грамм вещества",
        ["5", "10"],
    ) == []
    assert extract_query_measures(
        "нашли примерно 10 грамм вещества",
        ["10"],
    ) == []
    assert extract_query_measures(
        "нашли не более 10 грамм вещества",
        ["10"],
    ) == []
    assert extract_query_measures(
        "масса не превышает 10 грамм",
        ["10"],
    ) == []
    assert extract_query_measures(
        "стоимость 10+ тысяч долларов",
        ["10"],
    ) == []
    assert extract_query_measures(
        "стоимость 1 миллион 500 тысяч долларов",
        ["500"],
    ) == []
    assert extract_query_measures(
        "стоимость 1 миллион и 500 тысяч долларов",
        ["500"],
    ) == []
    assert extract_query_measures(
        "нашли 5 или 10 грамм вещества",
        ["5", "10"],
    ) == []
    assert extract_query_measures(
        "нашли 5 грамм или 10 грамм вещества",
        ["5", "10"],
    ) == []
    assert extract_query_measures(
        "нашли 5 10 грамм вещества",
        ["5", "10"],
    ) == []
    assert extract_query_measures(
        "нашли 5 грамм примерно",
        ["5"],
    ) == []
    assert extract_query_measures(
        "нашли 5 грамм около",
        ["5"],
    ) == []
    assert extract_query_measures(
        "нашли 5 грамм плюс-минус",
        ["5"],
    ) == []
    assert extract_query_measures(
        "нашли по крайней мере 10 грамм",
        ["10"],
    ) == []
    assert extract_query_measures(
        "нашли по меньшей мере 10 грамм",
        ["10"],
    ) == []
    assert extract_query_measures(
        "нашли в пределах 10 грамм",
        ["10"],
    ) == []
    assert extract_query_measures(
        "нашли в районе 10 грамм",
        ["10"],
    ) == []
    assert extract_query_measures(
        "нашли 10 грамм с половиной",
        ["10"],
    ) == []
    assert extract_query_measures(
        "масса не достигла 10 граммов",
        ["10"],
    ) == []
    assert extract_query_measures(
        "вес не достигает 10 граммов",
        ["10"],
    ) == []
    assert extract_query_measures(
        "масса достигла ровно 10 граммов",
        ["10"],
    ) == [QueryMeasure(value=10.0, unit="mass_g")]
    for query in (
        "не ровно 10 грамм",
        "точно не 10 грамм",
        "не 10 грамм",
        "10 грамм не точно",
        "не равно 10 грамм",
        "не ровно десять 10 грамм",
        "10 ± 2 грамм",
        "10±2 грамм",
    ):
        assert extract_query_measures(
            query,
            ["10", "2"] if "2" in query else ["10"],
        ) == []
    for query in (
        "нашли 5 грамм и 10 грамм",
        "нашли 5 кг и 10 кг",
        "стоимость 5 долларов и 10 долларов",
        "нашли 5 грамм плюс 10 грамм",
    ):
        assert extract_query_measures(query, ["5", "10"]) == []


def test_malformed_number_and_unrelated_d_word_fail_closed():
    assert extract_query_measures(
        "масса 1.2.3 грамма",
        ["1.2.3"],
    ) == []
    assert extract_query_measures(
        "нашли 5 доз наркотиков",
        ["5"],
    ) == []
    assert extract_query_measures(
        "документ состоит из 5 частей закона",
        ["5"],
    ) == []
    assert extract_query_measures(
        "получил 5 грамот",
        ["5"],
    ) == []


def test_drug_ranges_are_read_before_punishment_numbers():
    card = {
        "title": "Наркотики массой до 5 граммов",
        "full_text": (
            "Статья 3.7. Незаконное хранение наркотиков до 5 грамм.\n"
            "Наказание: административный арест сроком до 10 суток."
        ),
    }

    assert extract_applicability_intervals(card) == [
        ApplicabilityInterval(unit="mass_g", upper=5.0),
    ]


def test_inline_sanction_boundaries_hide_punishment_numbers():
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Хранение наркотиков до 5 грамм — штраф до 10.000 долларов."
            ),
        }
    ) == [ApplicabilityInterval(unit="mass_g", upper=5.0)]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Хранение наркотиков свыше 5 грамм – влечёт арест "
                "сроком до 10 суток."
            ),
        }
    ) == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=5.0,
            lower_inclusive=False,
        )
    ]


def test_closed_and_open_bounds_are_typed():
    between = extract_applicability_intervals(
        {
            "title": "Наркотики от 5 до 20 граммов включительно",
            "full_text": "",
        }
    )
    above = extract_applicability_intervals(
        {
            "title": "Наркотики свыше 20 граммов",
            "full_text": "",
        }
    )

    assert between == [
        ApplicabilityInterval(unit="mass_g", lower=5.0, upper=20.0),
    ]
    assert above == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=20.0,
            lower_inclusive=False,
        ),
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "Срок не свыше 3 дней",
            ApplicabilityInterval(unit="duration_days", upper=3.0),
        ),
        (
            "Масса не выше 10 граммов",
            ApplicabilityInterval(unit="mass_g", upper=10.0),
        ),
        (
            "Масса не ниже 5 граммов",
            ApplicabilityInterval(unit="mass_g", lower=5.0),
        ),
    ],
)
def test_negated_comparatives_keep_their_true_direction(source, expected):
    assert extract_applicability_intervals(
        {"title": source, "full_text": ""}
    ) == [expected]


@pytest.mark.parametrize(
    "source",
    [
        "Масса не ровно 10 граммов",
        "Масса не до 10 граммов",
        "Масса не от 5 до 10 граммов",
        "Стоимость не от $5 до $10",
        "Масса не в диапазоне от 5 до 10 граммов",
        "Допрос не может длиться более 1 часа",
        "Штраф не может быть менее 1000 долларов",
        "Масса не должна быть выше 10 граммов",
    ],
)
def test_unsupported_or_distributed_negated_bounds_fail_closed(source):
    assert extract_applicability_intervals(
        {"title": source, "full_text": ""}
    ) == []


@pytest.mark.parametrize(
    "source",
    [
        "Масса в диапазоне от 5 до 10 граммов",
        "Масса на уровне от 5 до 10 граммов",
    ],
)
def test_positive_range_after_word_ending_ne_is_not_false_negation(source):
    assert extract_applicability_intervals(
        {"title": source, "full_text": ""}
    ) == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=5.0,
            upper=10.0,
            upper_inclusive=False,
        )
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "Значение на уровне более 10 процентов",
            ApplicabilityInterval(
                unit="percent",
                lower=10.0,
                lower_inclusive=False,
            ),
        ),
        (
            "Значение в законе не менее 5 процентов",
            ApplicabilityInterval(unit="percent", lower=5.0),
        ),
        (
            "Значение в диапазоне не более 10 процентов",
            ApplicabilityInterval(unit="percent", upper=10.0),
        ),
    ],
)
def test_positive_single_bound_after_word_ending_ne_is_parsed(source, expected):
    assert extract_applicability_intervals(
        {"title": source, "full_text": ""}
    ) == [expected]


def test_money_threshold_understands_forum_thousands_separator():
    below = extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Кража имущества стоимостью менее 10.000$.",
        }
    )
    above = extract_applicability_intervals(
        {
            "title": "Кража стоимостью более 10 000 долларов",
            "full_text": "",
        }
    )

    assert below == [
        ApplicabilityInterval(
            unit="money_usd",
            upper=10000.0,
            upper_inclusive=False,
        ),
    ]
    assert above == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=10000.0,
            lower_inclusive=False,
        ),
    ]


def test_money_threshold_supports_prefix_dollar_bounds_and_ranges():
    above = extract_applicability_intervals(
        {
            "title": "Кража имущества стоимостью свыше $5.000",
            "full_text": "",
        }
    )
    between = extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Кража имущества стоимостью от $5.000 до $10.000 "
                "включительно."
            ),
        }
    )
    and_above = extract_applicability_intervals(
        {
            "title": "Кража имущества стоимостью $5.000 и более",
            "full_text": "",
        }
    )

    assert above == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            lower_inclusive=False,
        ),
    ]
    assert between == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            upper=10000.0,
        ),
    ]
    assert and_above == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
        ),
    ]


def test_money_source_commas_scales_and_conflicting_units_are_safe():
    assert extract_applicability_intervals(
        {
            "title": "Стоимость свыше $50,000",
            "full_text": "",
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=50000.0,
            lower_inclusive=False,
        )
    ]
    assert extract_applicability_intervals(
        {
            "title": "Стоимость свыше $5 тыс",
            "full_text": "",
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            lower_inclusive=False,
        )
    ]
    assert extract_applicability_intervals(
        {
            "title": "Стоимость от $5,000 до $10,000 включительно",
            "full_text": "",
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            upper=10000.0,
        )
    ]
    assert extract_applicability_intervals(
        {
            "title": "Стоимость от $5k до $10k включительно",
            "full_text": "",
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            upper=10000.0,
        )
    ]
    for source in (
        "Оборот свыше $5 килограмм.",
        "Оборот от $5 до 10 килограмм.",
        "Оборот от 5 долларов до 10 тысяч долларов.",
        "Оборот от 5 тысяч долларов до 10 долларов.",
    ):
        assert extract_applicability_intervals(
            {"title": source, "full_text": ""}
        ) == []


def test_source_decimal_conversion_and_reversed_ranges_fail_closed():
    assert extract_applicability_intervals(
        {
            "title": "Хранение свыше 1.500,5 граммов",
            "full_text": "",
        }
    ) == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=1500.5,
            lower_inclusive=False,
        )
    ]
    assert extract_applicability_intervals(
        {
            "title": "Хранение от 1 кг до 500 г",
            "full_text": "",
        }
    ) == []
    assert extract_applicability_intervals(
        {
            "title": "Хранение от 20 до 5 граммов",
            "full_text": "",
        }
    ) == []
    assert extract_applicability_intervals(
        {
            "title": "Стоимость от 5 до 10 тысяч долларов",
            "full_text": "",
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            upper=10000.0,
            upper_inclusive=False,
        )
    ]
    for source in (
        "Стоимость от 5 тысяч до 10 долларов",
        "Стоимость от 5 миллионов до 10 долларов",
    ):
        assert extract_applicability_intervals(
            {"title": source, "full_text": ""}
        ) == []


@pytest.mark.parametrize(
    "source",
    [
        "Свыше 5 грамматических ошибок",
        "Свыше 5 часовых периодов",
        "Свыше 5 голосований",
        "Свыше 5 долларовых купюр",
        "Свыше $5 рублей",
        "Свыше $5 минут",
        "От $5 рублей до 10 долларов",
    ],
)
def test_partial_or_unsupported_unit_words_fail_closed(source):
    assert extract_applicability_intervals(
        {"title": source, "full_text": ""}
    ) == []


@pytest.mark.parametrize(
    "full_text",
    [
        "Деяние наказывается штрафом до 50.000 долларов.",
        "Деяние карается штрафом до 50.000 долларов.",
        "Деяние влечет административный штраф до 50.000 долларов.",
        "Деяние предусматривает штраф до 50.000 долларов.",
        "Деяние влечет за собой штраф до 50.000 долларов.",
        "Деяние влечёт за собой наложение административного штрафа до $50.000.",
        "Деянию назначается штраф до 50.000 долларов.",
        "Лицо подвергается административному штрафу до 50.000 долларов.",
        "На структуру налагается штраф от 50.000 до 100.000 долларов.",
        "На структуру накладывается штраф от $20,000 до $50,000.",
        "Деяние штрафуется на сумму от 10.000 до 50.000 долларов.",
        "Наказание с назначением судебного штрафа от 10.000 до 50.000 долларов.",
        "Нарушение является основанием для взыскания судебного штрафа до 50.000 долларов.",
        "Нарушение может привести к назначению им штрафа до 10 000$.",
        "Орган может применить санкции, включая наложение штрафных санкций от $5,000 до $10,000.",
        "Орган может применить меры в отношении Конгрессменов, включая наложение штрафных санкций от $5,000 до $10,000.",
        "Лицу может быть назначен административный штраф от 10.000 до 50.000 долларов.",
        "Министр вправе назначить лицу административный штраф от 50.000 до 100.000 долларов.",
        "На лицо налагается административная штрафная санкция от 10.000 до 50.000 долларов.",
        "Лицу может быть наложен штраф до 50.000 долларов.",
        "Ответственность предусмотрена в виде штрафа, который варьируется от 10.000 до 50.000 долларов.",
        "Может быть применено дисциплинарное взыскание, а также штраф от 10.000 до 50.000 долларов.",
        "Деяние наказуемо административным штрафом от 20.000 до 50.000 долларов.",
        "Организация подлежит наказанию в виде штрафа до 50.000 долларов.",
    ],
)
def test_inline_sanction_clauses_do_not_become_applicability(full_text):
    assert extract_applicability_intervals(
        {"title": "", "full_text": full_text}
    ) == []


def test_sanction_cutoff_preserves_prior_applicability_and_safe_contrasts():
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Норма применяется при стоимости свыше $5.000 и "
                "наказывается штрафом до $50.000."
            ),
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            lower_inclusive=False,
        )
    ]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Орган может назначить срок до 5 дней.",
        }
    ) == [ApplicabilityInterval(unit="duration_days", upper=5.0)]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Нарушение может привести к ограничению оборота "
                "до 5 граммов."
            ),
        }
    ) == [ApplicabilityInterval(unit="mass_g", upper=5.0)]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Орган может применить меры к массе свыше 5 граммов.",
        }
    ) == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=5.0,
            lower_inclusive=False,
        )
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Штраф до 5.000 долларов",
        "Наказание: штраф от 5.000 до 10.000 долларов",
        "Штрафная санкция до 5.000 долларов",
    ],
)
def test_sanction_only_title_does_not_become_applicability(title):
    assert extract_applicability_intervals(
        {"title": title, "full_text": ""}
    ) == []


def test_true_applicability_title_still_keeps_its_threshold():
    assert extract_applicability_intervals(
        {
            "title": "Кража имущества стоимостью менее 10.000 долларов",
            "full_text": "",
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            upper=10000.0,
            upper_inclusive=False,
        )
    ]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Не содержит признаков уголовно наказуемого деяния; "
                "применяется к массе свыше 5 граммов."
            ),
        }
    ) == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=5.0,
            lower_inclusive=False,
        )
    ]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Закон предусматривает оборот до 5 граммов.",
        }
    ) == [ApplicabilityInterval(unit="mass_g", upper=5.0)]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Для рассмотрения назначается срок до 5 дней.",
        }
    ) == [ApplicabilityInterval(unit="duration_days", upper=5.0)]
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Может быть применено правило для массы свыше 5 граммов."
            ),
        }
    ) == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=5.0,
            lower_inclusive=False,
        )
    ]


def test_range_units_must_agree_and_sanction_cutoff_stays_first():
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Оборот имущества от $5.000 до 10 граммов.",
        }
    ) == []
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Оборот имущества свыше $5.000 граммов.",
        }
    ) == []
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Хищение имущества стоимостью свыше $5.000 — "
                "штраф от $20.000 до $30.000 включительно."
            ),
        }
    ) == [
        ApplicabilityInterval(
            unit="money_usd",
            lower=5000.0,
            lower_inclusive=False,
        )
    ]


def test_bound_operator_does_not_match_inside_another_word():
    assert extract_applicability_intervals(
        {
            "title": "",
            "full_text": "Оборот 5 граммов вещества регулируется отдельно.",
        }
    ) == []


def test_conjunctive_same_unit_bounds_are_one_interval_not_union():
    intervals = extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Норма применяется к массе свыше 5 граммов, "
                "но не более 20 граммов."
            ),
        }
    )

    assert intervals == [
        ApplicabilityInterval(
            unit="mass_g",
            lower=5.0,
            upper=20.0,
            lower_inclusive=False,
        )
    ]
    assert applicability_verdict(
        [QueryMeasure(value=3.0, unit="mass_g")],
        intervals,
    ) == "mismatch"
    assert applicability_verdict(
        [QueryMeasure(value=10.0, unit="mass_g")],
        intervals,
    ) == "match"
    assert applicability_verdict(
        [QueryMeasure(value=30.0, unit="mass_g")],
        intervals,
    ) == "mismatch"


@pytest.mark.parametrize(
    "source",
    [
        "масса не менее 5 и не более 20 граммов",
        "масса свыше 5, но не более 20 граммов",
        "масса свыше 5 и одновременно не более 20 граммов",
        "масса свыше 5; но не более 20 граммов",
        "масса свыше 5: но не более 20 граммов",
        "масса свыше 5 — но не более 20 граммов",
        "масса свыше 5 – но не более 20 граммов",
        "масса свыше 5 - но не более 20 граммов",
        "масса свыше 5 или не более 20 граммов",
        "масса свыше 5 граммов, но не более 20",
        "масса свыше 5 тысяч, но не более 20 тысяч долларов",
        "масса свыше 5, а не более 20 граммов",
        "масса свыше 5, однако не более 20 граммов",
        "масса свыше 5, одновременно не более 20 граммов",
        "масса свыше 5, также не более 20 граммов",
        "масса свыше 5, плюс не более 20 граммов",
        "масса свыше 5, вместе с тем не более 20 граммов",
        "масса свыше 5, в то же время не более 20 граммов",
        "масса свыше 5, причем не более 20 граммов",
        "масса свыше 5, тогда как не более 20 граммов",
        "масса свыше 5, при условии не более 20 граммов",
    ],
)
def test_shared_unit_coordinated_bounds_fail_closed_as_a_whole(source):
    assert extract_applicability_intervals(
        {"title": "", "full_text": source}
    ) == []


def test_unbound_same_unit_facts_fail_closed_instead_of_or():
    intervals = extract_applicability_intervals(
        {
            "title": "",
            "full_text": (
                "Работа длится более 6 часов, перерыв длится "
                "не более 2 часов."
            ),
        }
    )

    assert len(intervals) == 2
    assert applicability_verdict(
        [QueryMeasure(value=1.0, unit="duration_hours")],
        intervals,
    ) == "unknown"


def test_verdict_does_not_mix_grams_with_days():
    measure = [QueryMeasure(value=10.0, unit="mass_g")]

    assert applicability_verdict(
        measure,
        [ApplicabilityInterval(unit="duration_days", upper=10.0)],
    ) == "unknown"
    assert applicability_verdict(
        measure,
        [ApplicabilityInterval(unit="mass_g", upper=5.0)],
    ) == "mismatch"


def test_partial_multi_unit_evidence_fails_closed():
    measures = [
        QueryMeasure(value=10.0, unit="mass_g"),
        QueryMeasure(value=5.0, unit="duration_days"),
    ]

    assert applicability_verdict(
        measures,
        [ApplicabilityInterval(unit="mass_g", lower=5.0, upper=20.0)],
    ) == "unknown"
    assert applicability_verdict(
        measures,
        [
            ApplicabilityInterval(unit="mass_g", lower=5.0, upper=20.0),
            ApplicabilityInterval(unit="duration_days", upper=3.0),
        ],
    ) == "mismatch"


def test_source_with_extra_unit_constraint_requires_both_query_facts():
    intervals = [
        ApplicabilityInterval(unit="mass_g", upper=5.0),
        ApplicabilityInterval(unit="duration_days", lower=3.0),
    ]

    assert applicability_verdict(
        [QueryMeasure(value=4.0, unit="mass_g")],
        intervals,
    ) == "unknown"
    assert applicability_verdict(
        [
            QueryMeasure(value=4.0, unit="mass_g"),
            QueryMeasure(value=5.0, unit="duration_days"),
        ],
        intervals,
    ) == "match"


def test_overlap_at_five_preserves_both_as_matches():
    measure = [QueryMeasure(value=5.0, unit="mass_g")]

    assert applicability_verdict(
        measure,
        [ApplicabilityInterval(unit="mass_g", upper=5.0)],
    ) == "match"
    assert applicability_verdict(
        measure,
        [ApplicabilityInterval(unit="mass_g", lower=5.0, upper=20.0)],
    ) == "match"


def test_evidence_marks_only_source_backed_match_and_mismatch():
    evidence = evaluate_applicability(
        {
            1: {
                "title": "До 5 граммов",
                "full_text": "Хранение до 5 грамм. Наказание: 10 суток.",
            },
            2: {
                "title": "От 5 до 20 граммов включительно",
                "full_text": "",
            },
            3: {
                "title": "Общая статья о наркотиках",
                "full_text": "",
            },
        },
        numeric_query="при себе 10 грамм наркотиков",
        semantic_numbers=["10"],
    )

    assert evidence[1]["verdict"] == "mismatch"
    assert evidence[2]["verdict"] == "match"
    assert evidence[3]["verdict"] == "unknown"


def test_evidence_query_preserves_question_type_and_typed_extra_facets():
    assert "понятие" in build_evidence_query(
        "что такое защитная миссия",
        "такое защитная миссия",
    )
    assert "предназначение" in build_evidence_query(
        "для чего вводят чрезвычайное положение",
        "для вводят чрезвычайное положение",
    )
    protected = build_evidence_query(
        "угрожал расправой полицейскому",
        "угрожал расправой полицейскому",
        ("государственный", "сотрудник"),
    )
    assert "государственный" in protected
    assert "сотрудник" in protected
    assert "полномочия" not in build_evidence_query(
        "может ли быть штраф",
        "может ли быть штраф",
    )
    assert "полномочия" in build_evidence_query(
        "имеет ли право прокурор возбудить дело",
        "имеет ли право прокурор возбудить дело",
    )


def test_lexical_dominance_moves_only_strict_superset():
    evidence = {
        1: {
            "direct_terms": ["крыша", "больница"],
            "context_terms": [],
        },
        2: {
            "direct_terms": ["крыша", "больница"],
            "context_terms": ["палето", "ems"],
        },
        3: {
            "direct_terms": ["крыша"],
            "context_terms": ["сэнди", "ems"],
        },
    }

    assert lexical_dominance_order([1, 3, 2], evidence) == [2, 1, 3]


def test_lexical_dominance_promotes_only_across_adjacent_proven_loser():
    evidence = {
        1: {"direct_terms": ["a"], "context_terms": []},
        2: {"direct_terms": ["a", "b"], "context_terms": []},
    }

    assert lexical_dominance_order([1, 2], evidence) == [2, 1]


def test_lexical_dominance_does_not_cross_protected_code_hit():
    evidence = {
        1: {"direct_terms": ["штраф"], "context_terms": []},
        2: {
            "direct_terms": ["штраф", "доллар"],
            "context_terms": [],
        },
    }

    assert lexical_dominance_order(
        [1, 2],
        evidence,
        protected_ids={1},
    ) == [1, 2]


def test_incomparable_evidence_keeps_rrf_order():
    evidence = {
        1: {
            "direct_terms": ["крыша"],
            "context_terms": ["палето"],
        },
        2: {
            "direct_terms": ["коридор"],
            "context_terms": ["сэнди"],
        },
    }

    assert lexical_dominance_order([1, 2], evidence) == [1, 2]


def test_empty_source_evidence_cannot_be_dominated_by_incidental_hit():
    evidence = {
        1: {"direct_terms": [], "context_terms": []},
        2: {"direct_terms": ["штраф"], "context_terms": []},
    }

    assert lexical_dominance_order([1, 2], evidence) == [1, 2]


def test_independent_dominance_chains_preserve_incomparable_rrf_order():
    evidence = {
        1: {"direct_terms": ["a"], "context_terms": []},
        2: {"direct_terms": ["a", "b"], "context_terms": []},
        3: {"direct_terms": ["c"], "context_terms": []},
        4: {"direct_terms": ["c", "d"], "context_terms": []},
    }

    assert lexical_dominance_order([1, 2, 3, 4], evidence) == [2, 1, 4, 3]


def test_context_only_candidate_cannot_dominate_direct_article_hit():
    evidence = {
        1: {
            "direct_terms": ["больница"],
            "context_terms": [],
        },
        2: {
            "direct_terms": [],
            "context_terms": ["больница", "палето"],
        },
    }

    assert lexical_dominance_order([1, 2], evidence) == [1, 2]


def test_structural_context_cannot_compensate_for_lost_direct_term():
    evidence = {
        1: {
            "direct_terms": ["больница", "крыша"],
            "context_terms": [],
        },
        2: {
            "direct_terms": ["крыша"],
            "context_terms": ["больница", "палето"],
        },
    }

    assert lexical_dominance_order([1, 2], evidence) == [1, 2]
