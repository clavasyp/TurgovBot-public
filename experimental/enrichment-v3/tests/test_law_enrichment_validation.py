"""Pure source-evidence tests for generated law penalties."""

from __future__ import annotations

import pytest

from services.law_enrichment_validation import (
    validate_enrichment_source_evidence,
    validate_penalty_source_evidence,
    validate_synonym_source_evidence,
)


def test_null_penalty_passes_without_sanction_text():
    validate_penalty_source_evidence(
        "Статья 2.9. Настоящая норма устанавливает порядок.",
        None,
        None,
    )


def test_invented_number_fails():
    with pytest.raises(ValueError, match="numeric fact '50000'"):
        validate_penalty_source_evidence(
            "Наказание: штраф в размере 40.000$.",
            "штраф 50000$",
            {"fine": "50000$", "prison": None},
        )


@pytest.mark.parametrize(
    ("source_number", "output_number"),
    [
        ("40.000", "40000"),
        ("40 000", "40.000"),
        ("40000", "40 000"),
    ],
)
def test_equivalent_money_formatting_passes(source_number, output_number):
    validate_penalty_source_evidence(
        f"Наказание: штраф в размере {source_number}$.",
        f"штраф {output_number}$",
        {"fine": f"{output_number}$"},
    )


@pytest.mark.parametrize("dash", ["-", "–", "—"])
def test_source_backed_time_range_passes_across_dash_styles(dash):
    validate_penalty_source_evidence(
        "Наказание: лишение свободы на срок от 120 до 240 минут.",
        f"лишение свободы 120{dash}240 минут",
        {"prison": f"120{dash}240 минут"},
    )


def test_citation_number_does_not_authorize_penalty_number():
    with pytest.raises(ValueError, match="citation-only output"):
        validate_penalty_source_evidence(
            "Наказание: ответственность определяется статьёй 2.9.",
            "наказание по статье 2.9",
            {"other": "статья 2.9"},
        )


def test_citation_without_sanction_cue_fails():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "Статья 2.9. Ссылка на общие правила ответственности.",
            "лишение свободы",
            {"prison": "лишение свободы"},
        )


def test_structured_wanted_header_range_does_not_invent_middle_level():
    with pytest.raises(ValueError, match="numeric fact '3'.*wanted"):
        validate_penalty_source_evidence(
            (
                "Статья 16.15 от ★ до ★★★★★ [Федеральная/Региональная]\n"
                "Наказание: штраф 5000$."
            ),
            "штраф 5000$, розыск 3 звезды",
            {"fine": "5000$", "wanted_level": "3"},
        )


def test_wanted_stars_on_sanction_line_authorize_level():
    validate_penalty_source_evidence(
        (
            "Статья 16.15 от ★ до ★★★★★ [Федеральная/Региональная]\n"
            "Приоритет розыска: ★★★"
        ),
        "розыск 3 звезды",
        {"wanted_level": "3"},
    )


def test_wanted_star_range_on_sanction_line_authorizes_range():
    validate_penalty_source_evidence(
        "Приоритет розыска: от ★ до ★★★★★",
        "розыск 1-5 звезд",
        {"wanted_level": "1-5"},
    )


def test_structured_wanted_header_authorizes_only_its_exact_range():
    validate_penalty_source_evidence(
        (
            "Статья 16.15 от ★ до ★★★★★ "
            "[Федеральная/Региональная] Уклонение от наказания."
        ),
        "розыск 1-5 звезд",
        {"wanted_level": "1-5"},
    )


def test_decorative_header_stars_are_not_wanted_evidence():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "Статья 7. ★★★ Особо важная организационная норма.",
            "розыск 3 звезды",
            {"wanted_level": "3"},
        )


def test_numeric_wanted_level_after_label_is_supported():
    validate_penalty_source_evidence(
        "Уровень розыска: 3",
        "розыск 3 звезды",
        {"wanted_level": "3"},
    )


def test_number_outside_sanction_span_does_not_authorize_output():
    with pytest.raises(ValueError, match="numeric fact '21'"):
        validate_penalty_source_evidence(
            (
                "Лицо должно достичь возраста 21 года.\n"
                "Наказание: лишение лицензии."
            ),
            "лишение лицензии на 21 день",
            {"other": "лишение лицензии на 21 день"},
        )


def test_source_backed_nonnumeric_explicit_sanction_passes():
    validate_penalty_source_evidence(
        "Нарушение карается лишением свободы.",
        "лишение свободы",
        {"prison": "лишение свободы"},
    )


def test_invented_nonnumeric_category_fails():
    with pytest.raises(ValueError, match="sanction category 'prison'"):
        validate_penalty_source_evidence(
            "Наказание: штраф.",
            "лишение свободы",
            {"prison": "лишение свободы"},
        )


@pytest.mark.parametrize(
    ("penalty_summary", "penalty", "field"),
    [
        ("увольнение", None, "penalty_summary"),
        (None, {"other": "увольнение"}, "penalty.other"),
    ],
)
def test_fine_does_not_authorize_dismissal_in_untyped_fields(
    penalty_summary,
    penalty,
    field,
):
    with pytest.raises(
        ValueError,
        match=rf"sanction category 'dismissal' from '{field}'",
    ):
        validate_penalty_source_evidence(
            "Наказание: штраф 100 долларов.",
            penalty_summary,
            penalty,
        )


@pytest.mark.parametrize(
    ("penalty_summary", "penalty", "field"),
    [
        ("лишение лицензии", None, "penalty_summary"),
        (None, {"other": "лишение лицензии"}, "penalty.other"),
    ],
)
def test_prison_does_not_authorize_license_loss_in_untyped_fields(
    penalty_summary,
    penalty,
    field,
):
    with pytest.raises(
        ValueError,
        match=rf"sanction category 'license' from '{field}'",
    ):
        validate_penalty_source_evidence(
            "Нарушение карается лишением свободы.",
            penalty_summary,
            penalty,
        )


def test_untyped_sanction_text_without_category_or_fact_fails():
    with pytest.raises(
        ValueError,
        match="contains no source-backed category or fact",
    ):
        validate_penalty_source_evidence(
            "Наказание: штраф.",
            "иная мера по решению уполномоченного органа",
            {"other": "иная мера"},
        )


def test_generic_responsibility_does_not_match_concrete_fine_only():
    with pytest.raises(
        ValueError,
        match="generic responsibility.*absent",
    ):
        validate_penalty_source_evidence(
            "Наказание: штраф.",
            "ответственность",
            {"other": "ответственность"},
        )


def test_multiline_label_authorizes_following_sanction_line():
    validate_penalty_source_evidence(
        "Наказание:\nштраф 40.000$\nПримечание: статья 9.4.",
        "штраф 40000$",
        {"fine": "40000$"},
    )


def test_numbers_cannot_be_swapped_between_penalty_categories():
    with pytest.raises(
        ValueError,
        match=r"numeric fact.*(?:fine/money|arrest/days)",
    ):
        validate_penalty_source_evidence(
            "Наказание:\nШтраф 5.000$.\nАрест на 10 суток.",
            "штраф 10$, арест 5000 суток",
            {
                "fine": "10$",
                "arrest": "5000 суток",
            },
        )


def test_numbers_cannot_be_swapped_between_bail_and_prison():
    with pytest.raises(ValueError, match=r"'30'.*bail/money"):
        validate_penalty_source_evidence(
            (
                "Выход под залог:\n75.000$\n"
                "Наказание:\nДо 30 месяцев лишения свободы."
            ),
            "залог 30$, лишение свободы 75000 месяцев",
            {
                "prison": "75000 месяцев",
                "other": "залог 30$",
            },
        )


def test_money_range_allows_currency_on_both_source_endpoints():
    validate_penalty_source_evidence(
        "Сумма штрафа — от 50.000$ до 200.000$ включительно.",
        "штраф от 50 000 до 200 000 долларов",
        {"fine": "от 50 000 до 200 000 долларов"},
    )


def test_independent_values_do_not_authorize_invented_min_max_range():
    with pytest.raises(ValueError, match="output range '5000'-'10000'"):
        validate_penalty_source_evidence(
            (
                "Наказание: штраф 5.000$.\n"
                "Наказание: штраф 8.000$.\n"
                "Наказание: штраф 10.000$."
            ),
            "штраф 5 000-10 000 долларов",
            {"fine": "5 000-10 000 долларов"},
        )


@pytest.mark.parametrize("separator", ["|", "I"])
def test_real_criminal_code_wanted_suffix_is_supported(separator):
    validate_penalty_source_evidence(
        (
            "Статья 6.1\n"
            f"(A, CR) Нанесение телесных повреждений. {separator} 3*\n"
            "Наказание:\nДо 30 месяцев лишения свободы."
        ),
        "лишение свободы до 30 месяцев, розыск 3 уровня",
        {
            "prison": "до 30 месяцев",
            "wanted_level": "3",
        },
    )


@pytest.mark.parametrize(
    ("broken_money", "expected"),
    [
        ("100. 000 $", "100000"),
        ("100.\n000 $", "100000"),
        ("10\n0 .000 $", "100000"),
        ("7\n5.000$", "75000"),
    ],
)
def test_known_forum_money_line_breaks_are_repaired(
    broken_money,
    expected,
):
    validate_penalty_source_evidence(
        f"Выход под залог:\n{broken_money}",
        f"залог {expected}$",
        {"other": f"залог {expected}$"},
    )


def test_linked_sanction_explanation_may_supply_threshold():
    validate_penalty_source_evidence(
        (
            "Сотрудник лишается эфира на 60 минут и обязан выплатить "
            "штраф 50.000$.\n"
            "Пояснение:\n"
            "более 3-х нарушений за рабочий день."
        ),
        "лишение эфира 60 минут и штраф 50000$ за более чем 3 нарушения",
        {
            "fine": "50000$",
            "other": "лишение эфира 60 минут",
        },
    )


def test_linked_applicability_note_may_supply_exact_exception_range():
    validate_penalty_source_evidence(
        (
            "Наказание:\nштраф 15.000$\n"
            "Примечание: статья не может быть применена к рыбалке "
            "в зонах 1-2."
        ),
        "штраф 15000$, исключение для зон 1-2",
        {"fine": "15000$"},
    )


def test_worded_day_does_not_authorize_invented_24_hours():
    with pytest.raises(ValueError, match=r"'24'.*hours"):
        validate_penalty_source_evidence(
            "Нарушение влечет запрет доступа на протяжении суток.",
            "запрет доступа на 24 часа",
            {"other": "запрет доступа на 24 часа"},
        )


def test_nonsanction_fee_does_not_authorize_penalty_money():
    with pytest.raises(ValueError, match=r"'100000'.*fine/money"):
        validate_penalty_source_evidence(
            (
                "Стоимость лицензии — 100.000$.\n"
                "При поступлении на госслужбу лицензия аннулируется."
            ),
            "лицензия аннулируется; 100000 долларов — плата, не штраф",
            {"other": "аннулирование лицензии"},
        )


def test_coordinated_responsibility_and_included_fine_are_source_evidence():
    validate_penalty_source_evidence(
        (
            "Лица несут административную, дисциплинарную или уголовную "
            "ответственность в соответствии с законодательством. "
            "Контролирующий орган вправе привлечь нарушителей к "
            "ответственности, включая наложение штрафа в размере "
            "от $40,000 до $100,000."
        ),
        (
            "штраф $40,000–$100,000; административная, дисциплинарная "
            "или уголовная ответственность"
        ),
        {
            "fine": "$40,000–$100,000",
            "other": (
                "административная, дисциплинарная или уголовная "
                "ответственность"
            ),
        },
    )


def test_qualified_measures_of_criminal_responsibility_are_source_evidence():
    validate_penalty_source_evidence(
        (
            "Проникновение в воздушное пространство над объектами особой "
            "важности запрещено. В случае нарушения данных зон применяются "
            "меры уголовной ответственности согласно законодательству."
        ),
        "уголовная ответственность за проникновение в запретную зону",
        {"other": "уголовная ответственность"},
    )


def test_discussion_of_possible_fine_is_not_an_explicit_consequence():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            (
                "Комиссия обсуждает возможность наложения штрафа "
                "от $40,000 до $100,000."
            ),
            "штраф $40,000–$100,000",
            {"fine": "$40,000–$100,000"},
        )


def test_judge_ordering_security_to_detain_is_source_evidence():
    validate_penalty_source_evidence(
        (
            "При нарушении судебного этикета судья может потребовать "
            "сотрудников USSS, находящихся в зале, задержать "
            "соответствующих лиц."
        ),
        "задержание сотрудниками USSS по требованию судьи",
        {"arrest": "задержание сотрудниками USSS по требованию судьи"},
    )


def test_order_not_to_detain_is_not_a_sanction_cue():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            (
                "Судья может потребовать сотрудников USSS не задерживать "
                "участника заседания."
            ),
            "задержание сотрудниками USSS",
            {"arrest": "задержание сотрудниками USSS"},
        )


def test_removal_from_guarded_territory_is_source_evidence():
    validate_penalty_source_evidence(
        (
            "Доступ граждан обеспечивается только с законной целью, "
            "нарушение порядка может привести к удалению с охраняемой "
            "территории."
        ),
        "удаление с охраняемой территории за нарушение порядка",
        {"other": "требование покинуть охраняемую территорию"},
    )


def test_deleting_data_from_server_territory_is_not_a_sanction_cue():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            (
                "Ошибка может привести к удалению данных с территории "
                "сервера хранения."
            ),
            "удаление с территории",
            {"other": "удаление с территории"},
        )


def test_fine_field_keeps_speed_thresholds_separate_from_money():
    validate_penalty_source_evidence(
        (
            "Наказание:\n"
            "Штраф до $2000 за превышение скорости на 15-30 км/ч;\n"
            "Штраф до $2500 за превышение скорости на 31-45 км/ч."
        ),
        (
            "Штраф до 2 000 долларов за 15–30 км/ч и "
            "до 2 500 долларов за 31–45 км/ч."
        ),
        {
            "fine": (
                "до 2 000 долларов за +15–30 км/ч; "
                "до 2 500 долларов за +31–45 км/ч"
            )
        },
    )


def test_percentage_fine_is_bound_to_fine_category():
    validate_penalty_source_evidence(
        "Начисляется штраф в размере 100% от стоимости лицензии.",
        "штраф 100% от стоимости лицензии",
        {"fine": "100% от стоимости лицензии"},
    )


def test_entity_is_subject_to_percentage_fine_is_source_evidence():
    validate_penalty_source_evidence(
        (
            "При сокрытии активов нарушитель облагается штрафом в 300% "
            "к сумме утаенных активов или средств."
        ),
        "штраф 300% от суммы скрытых активов",
        {"fine": "300% от суммы скрытых активов"},
    )


def test_subject_to_tax_is_not_a_fine_sanction_cue():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "Доход организации облагается налогом по установленной ставке.",
            "штраф 300%",
            {"fine": "300%"},
        )


def test_bounded_license_suspension_duration_is_source_evidence():
    validate_penalty_source_evidence(
        (
            "Деятельность предприятия приостанавливается. Приостановка "
            "может осуществляться при незначительных нарушениях не более "
            "чем 72 часа для устранения проблем."
        ),
        "приостановка деятельности не более чем на 72 часа",
        {"other": "приостановка деятельности до 72 часов"},
    )


def test_automatic_license_freeze_is_source_evidence():
    validate_penalty_source_evidence(
        (
            "В случае подписания ордера на совершение рейда лицензия "
            "организации автоматически замораживается."
        ),
        "автоматическая заморозка лицензии при подписании ордера",
        {"other": "автоматическая заморозка лицензии"},
    )


def test_ordinary_automatic_freeze_is_not_a_license_sanction():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "При ошибке счётчик автоматически замораживается.",
            "заморозка лицензии",
            {"other": "заморозка лицензии"},
        )


def test_grammatical_penalty_word_does_not_open_multiline_label_block():
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            (
                "10.11. За нарушение правил могут предусматриваться "
                "административные наказания в соответствии с законом.\n"
                "10.12. Органы власти несут ответственность за законность.\n"
                "10.13. Военное положение включает эвакуацию населения."
            ),
            "эвакуация населения как наказание",
            {"other": "эвакуация населения"},
        )


def test_sanction_list_stops_before_next_coded_subsection():
    with pytest.raises(ValueError, match="sanction category 'prison'"):
        validate_penalty_source_evidence(
            (
                "Назначаются следующие виды наказаний:\n"
                "- судебный штраф.\n"
                "4.7.1. Отдельное правило допускает лишение свободы."
            ),
            "лишение свободы",
            {"prison": "лишение свободы"},
        )


def test_leading_subsection_code_is_not_a_penalty_number():
    with pytest.raises(ValueError, match="numeric fact '13.1'"):
        validate_penalty_source_evidence(
            (
                "13.1 Сотрудник несет ответственность за свои действия.\n"
                "13.2 Сотрудник несет ответственность за приказы."
            ),
            "ответственность 13.1",
            {"other": "ответственность 13.1"},
        )


def test_hyphenated_citation_range_is_not_a_dismissal_duration():
    with pytest.raises(ValueError, match=r"numeric fact '7'.*dismissal/days"):
        validate_penalty_source_evidence(
            "За нарушение ч. 1-2 ст. 7 следует немедленное увольнение.",
            "увольнение на 7 дней",
            {"other": "увольнение на 7 дней"},
        )


def test_half_of_statutory_maximum_cap_is_typed_source_evidence():
    validate_penalty_source_evidence(
        (
            "Уголовная ответственность за покушение не может превышать "
            "половины предельного наказания."
        ),
        "не более половины предельного наказания",
        {"other": "не более половины предельного наказания"},
    )


@pytest.mark.parametrize(
    "invented",
    [
        "не более трети предельного наказания",
        "не менее половины предельного наказания",
    ],
)
def test_penalty_fraction_and_direction_cannot_change(invented):
    with pytest.raises(ValueError, match="qualitative claim"):
        validate_penalty_source_evidence(
            (
                "Уголовная ответственность не может превышать половины "
                "предельного наказания."
            ),
            invented,
            {"other": invented},
        )


def test_release_reduction_half_and_record_omission_are_independent_claims():
    validate_penalty_source_evidence(
        (
            "Лицо может быть освобождено от уголовной ответственности "
            "полностью или ему назначается наказание меньшее, чем "
            "предусмотрено статьёй. При высокой тяжести назначается "
            "наказание в размере половины максимального и не вносится "
            "специальная отметка в базу данных."
        ),
        (
            "возможно полное освобождение или наказание ниже "
            "предусмотренного; половина максимального наказания без "
            "специальной отметки"
        ),
        {
            "other": (
                "полное освобождение; наказание ниже предусмотренного; "
                "половина максимального наказания без специальной отметки"
            )
        },
    )


def test_reduction_does_not_authorize_invented_release():
    with pytest.raises(ValueError, match="liability_release"):
        validate_penalty_source_evidence(
            "Назначается наказание меньшее, чем предусмотрено статьёй.",
            "полное освобождение от ответственности",
            {"other": "полное освобождение от ответственности"},
        )


def test_active_liability_release_is_typed_source_evidence():
    validate_penalty_source_evidence(
        (
            "Лицо, добровольно сдавшее предметы, освобождается от "
            "уголовной ответственности по соответствующей статье."
        ),
        "освобождение от уголовной ответственности",
        {"other": "освобождение от уголовной ответственности"},
    )


def test_liability_release_does_not_authorize_positive_liability():
    with pytest.raises(ValueError, match="liability_regime"):
        validate_penalty_source_evidence(
            "Лицо освобождается от уголовной ответственности.",
            "наступает уголовная ответственность",
            {"other": "уголовная ответственность"},
        )


def test_liability_release_cannot_change_regime():
    with pytest.raises(ValueError, match="liability_release"):
        validate_penalty_source_evidence(
            "Лицо освобождается от уголовной ответственности.",
            "освобождение от административной ответственности",
            {"other": "освобождение от административной ответственности"},
        )


@pytest.mark.parametrize(
    "non_release",
    [
        "Лицо не освобождается от уголовной ответственности.",
        "Сотрудник освобождается от занимаемой должности.",
        "Устанавливается порядок освобождения от ответственности.",
    ],
)
def test_non_operative_release_text_is_not_a_sanction_cue(non_release):
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            non_release,
            "освобождение от уголовной ответственности",
            {"other": "освобождение от уголовной ответственности"},
        )


def test_nominal_driving_rights_deprivation_is_typed():
    validate_penalty_source_evidence(
        "Наказание:\nлишение водительских прав.",
        "лишение водительских прав",
        {"other": "лишение водительских прав"},
    )


def test_driving_rights_do_not_authorize_occupational_ban():
    with pytest.raises(ValueError, match="right_deprivation"):
        validate_penalty_source_evidence(
            "Наказание:\nлишение водительских прав.",
            "лишение права занимать должности",
            {"other": "лишение права занимать должности"},
        )


def test_compensation_cannot_hide_corrective_work():
    with pytest.raises(ValueError, match="corrective_work"):
        validate_penalty_source_evidence(
            "Вред подлежит возмещению.",
            "возмещение вреда, исправительные работы",
            {"other": "возмещение вреда, исправительные работы"},
        )


def test_rank_compensation_and_corrective_work_each_need_source_support():
    validate_penalty_source_evidence(
        (
            "Назначаются следующие виды наказаний:\n"
            "- лишение всех званий, наград и привилегий;\n"
            "- компенсация пострадавшему;\n"
            "- исправительные работы."
        ),
        "лишение званий, компенсация, исправительные работы",
        {"other": "лишение званий, компенсация, исправительные работы"},
    )


def test_identity_hold_is_not_movement_restriction():
    with pytest.raises(ValueError, match="freedom_restriction"):
        validate_penalty_source_evidence(
            (
                "Отказ снять маску влечет временное ограничение свободы "
                "до установления личности."
            ),
            "ограничение свободы передвижения",
            {"other": "ограничение свободы передвижения"},
        )


def test_identity_hold_is_typed_source_evidence():
    validate_penalty_source_evidence(
        (
            "Отказ снять маску влечет временное ограничение свободы "
            "до установления личности."
        ),
        "временное ограничение свободы до установления личности",
        {"other": "временное ограничение свободы до установления личности"},
    )


def test_identity_hold_accepts_installation_noun_morphology():
    validate_penalty_source_evidence(
        (
            "Отказ снять маску влечет временное ограничение свободы "
            "до установки личности."
        ),
        "временное ограничение свободы до установления личности",
        {"other": "временное ограничение свободы до установления личности"},
    )


def test_identity_hold_accepts_one_possessive_before_identity():
    validate_penalty_source_evidence(
        "Наказание: Вы задержаны до установки вашей личности.",
        "задержание до установления личности",
        {"other": "задержание до установления личности"},
    )


def test_identity_hold_accepts_imperfective_detention_before_data_check():
    validate_penalty_source_evidence(
        "Наказание: Лицо задерживают до установления его данных.",
        "задержание до установления данных",
        {"other": "задержание до установления данных"},
    )


def test_identity_hold_accepts_person_is_held_paraphrase():
    validate_synonym_source_evidence(
        (
            "Лиц без документов задерживают до установления их личности "
            "с последующим доставлением в правоохранительные органы."
        ),
        ["человека без документов держат до установления личности"],
    )


@pytest.mark.parametrize(
    "neutral",
    [
        "документы держат до установления личности",
        "лицензию держат до установления личности владельца",
    ],
)
def test_non_person_hold_does_not_create_identity_restriction(neutral):
    validate_synonym_source_evidence(
        "Устанавливается порядок проверки документов.",
        [neutral],
    )


def test_negated_identity_hold_does_not_authorize_positive_restriction():
    with pytest.raises(ValueError, match="freedom_restriction"):
        validate_synonym_source_evidence(
            "Лиц без документов не задерживают до установления личности.",
            ["человека держат до установления личности"],
        )


def test_identity_hold_does_not_skip_arbitrary_endpoint_words():
    with pytest.raises(ValueError, match="freedom_restriction"):
        validate_penalty_source_evidence(
            "Наказание: Лицо задержано до установки причины личности.",
            "задержание до установления личности",
            {"other": "задержание до установления личности"},
        )


def test_unrelated_installation_does_not_invent_identity_endpoint():
    with pytest.raises(ValueError, match="freedom_restriction"):
        validate_penalty_source_evidence(
            (
                "Нарушение влечет временное ограничение свободы "
                "до установки оборудования."
            ),
            "временное ограничение свободы до установления личности",
            {"other": "временное ограничение свободы до установления личности"},
        )


def test_court_pass_revocation_is_not_license_revocation():
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            (
                "Пропуск может быть аннулирован судом в качестве "
                "дополнительной меры наказания."
            ),
            "аннулирование лицензии судом",
            {"other": "аннулирование лицензии судом"},
        )


def test_court_pass_revocation_is_typed_source_evidence():
    validate_penalty_source_evidence(
        (
            "Пропуск может быть аннулирован судом в качестве "
            "дополнительной меры наказания."
        ),
        "аннулирование пропуска судом как дополнительная мера наказания",
        {"other": "аннулирование пропуска судом"},
    )


def test_harm_payable_language_authorizes_compensation():
    validate_penalty_source_evidence(
        "Вред, причиненный гражданину, подлежит возмещению.",
        "возмещение причинённого вреда",
        {"other": "возмещение причинённого вреда"},
    )


def test_negated_harm_compensation_does_not_authorize_positive_output():
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            "Вред не подлежит возмещению.",
            "возмещение вреда",
            {"other": "возмещение вреда"},
        )


def test_prosecution_under_state_legislation_is_typed():
    validate_penalty_source_evidence(
        "Данное деяние преследуется по нормам законодательства штата.",
        "преследование по законодательству штата",
        {"other": "преследование по законодательству штата"},
    )


def test_negated_prosecution_does_not_authorize_positive_output():
    with pytest.raises(ValueError):
        validate_penalty_source_evidence(
            "Данное деяние не преследуется по нормам законодательства.",
            "преследование по законодательству",
            {"other": "преследование по законодательству"},
        )


def test_three_liability_regimes_are_checked_independently():
    validate_penalty_source_evidence(
        (
            "За нарушение законодательства сотрудник подвергается "
            "дисциплинарному, административному или уголовному наказанию."
        ),
        "дисциплинарное, административное или уголовное наказание",
        {
            "other": (
                "дисциплинарное, административное или уголовное наказание"
            )
        },
    )


def test_exact_not_a_crime_supports_negative_criminal_liability():
    validate_synonym_source_evidence(
        "Данное деяние не является преступлением.",
        ["деяние без уголовной ответственности"],
    )


def test_not_a_serious_crime_does_not_erase_criminal_liability():
    with pytest.raises(ValueError, match="liability_regime"):
        validate_synonym_source_evidence(
            "Данное деяние не является тяжким преступлением.",
            ["деяние без уголовной ответственности"],
        )


def test_prohibited_administrative_attribution_is_negative_regime():
    validate_synonym_source_evidence(
        (
            "Привлечение к административной ответственности "
            "запрещается."
        ),
        ["когда нельзя привлечь к административной ответственности"],
    )


@pytest.mark.parametrize(
    "source",
    [
        "Привлечение к административной ответственности разрешается.",
        "Освобождение от административной ответственности запрещается.",
    ],
)
def test_other_administrative_rules_do_not_prove_attribution_ban(source):
    with pytest.raises(ValueError, match="liability_regime"):
        validate_synonym_source_evidence(
            source,
            ["нельзя привлечь к административной ответственности"],
        )


@pytest.mark.parametrize(
    "source",
    [
        (
            "Гражданин добровольно сдал запрещённые средства, и уголовная "
            "ответственность по настоящему закону не предусматривается."
        ),
        (
            "Гражданин добровольно сдал запрещённые средства и к уголовной "
            "ответственности по настоящему закону не привлекается."
        ),
    ],
)
def test_postposed_negative_liability_preserves_ground_and_scope(source):
    validate_synonym_source_evidence(
        source,
        [
            (
                "гражданин добровольно сдал запрещенные средства без "
                "уголовной ответственности по этому закону"
            )
        ],
    )


@pytest.mark.parametrize(
    "source,synonym",
    [
        (
            (
                "Гражданам при добровольной сдаче средств уголовная "
                "ответственность за нарушения настоящего закона "
                "не предусматривается."
            ),
            (
                "добровольно сдал средства — уголовной ответственности "
                "по этому закону нет"
            ),
        ),
        (
            (
                "Привлечение к административной ответственности во время "
                "процедуры трафик-стоп запрещается в случае, "
                "предусмотренном законом о неприкосновенности."
            ),
            "во время трафик стопа нельзя привлечь к административной ответственности",
        ),
    ],
)
def test_explicit_liability_ground_or_bounded_circumstance_is_preserved(
    source,
    synonym,
):
    validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    "synonym",
    [
        (
            "добровольно сдал запрещенные средства — уголовной "
            "ответственности по этому закону нет"
        ),
        (
            "добровольно сдал запрещенные средства — по этому закону "
            "нет уголовного преследования"
        ),
        (
            "добровольно сдал запрещенные средства — по этому закону "
            "уголовно не преследуется"
        ),
    ],
)
def test_natural_negative_liability_forms_preserve_ground_and_scope(synonym):
    validate_synonym_source_evidence(
        (
            "Гражданин при добровольной сдаче запрещённых средств к "
            "уголовной ответственности по настоящему закону не "
            "привлекается."
        ),
        [synonym],
    )


def test_absence_of_liability_information_is_not_negative_liability():
    with pytest.raises(ValueError, match="liability_regime"):
        validate_synonym_source_evidence(
            "В деле нет сведений об уголовной ответственности.",
            ["уголовной ответственности нет"],
        )


@pytest.mark.parametrize(
    "source",
    [
        "Нет уголовной ответственности в истории гражданина.",
        "В истории гражданина уголовной ответственности нет.",
        "В реестре нет уголовной ответственности.",
        "В деле отмечено, что уголовной ответственности нет.",
        "Нет уголовной ответственности, указанной в реестре.",
        (
            "Уголовная ответственность упоминается в статье, "
            "но компенсация не предусматривается."
        ),
        (
            "К уголовной ответственности применяются общие правила, "
            "но свидетель не привлекается."
        ),
        (
            "Привлечение к уголовной ответственности обсуждается, "
            "но публикация запрещается."
        ),
    ],
)
def test_unrelated_negative_predicate_or_record_context_is_not_no_liability(
    source,
):
    with pytest.raises(ValueError, match="liability_regime"):
        validate_synonym_source_evidence(
            source,
            ["уголовной ответственности нет"],
        )


def test_negative_liability_scope_cannot_cross_neighbor_law_relation():
    with pytest.raises(ValueError, match="liability_regime"):
        validate_synonym_source_evidence(
            (
                "По настоящему закону ведется реестр, а по иному кодексу "
                "уголовной ответственности нет."
            ),
            ["по этому закону уголовной ответственности нет"],
        )


@pytest.mark.parametrize(
    "synonym",
    [
        "уголовная ответственность отсутствует",
        "уголовная ответственность исключается",
    ],
)
def test_epistemic_negative_liability_verbs_are_never_claimless(synonym):
    with pytest.raises(ValueError, match="liability_regime"):
        validate_synonym_source_evidence(
            "Уголовная ответственность наступает.",
            [synonym],
        )


@pytest.mark.parametrize(
    "incomplete",
    [
        "без уголовной ответственности по этому закону",
        "добровольно сдал запрещенные средства без уголовной ответственности",
    ],
)
def test_negative_liability_cannot_drop_voluntary_ground_or_law_scope(
    incomplete,
):
    with pytest.raises(ValueError, match="critical qualifier"):
        validate_synonym_source_evidence(
            (
                "Гражданин добровольно сдал запрещённые средства и к "
                "уголовной ответственности по настоящему закону не "
                "привлекается."
            ),
            [incomplete],
        )


def test_remote_this_law_antecedent_is_not_liability_scope():
    validate_synonym_source_evidence(
        (
            "Лица, предусмотренные статьей 6 настоящего закона, не могут "
            "быть привлечены к уголовной ответственности, предусмотренной "
            "статьей 12.7 Уголовного кодекса, пока не покинут территорию."
        ),
        ["не наказывают по статье 12.7 пока человек покидает территорию"],
    )


def test_discipline_does_not_authorize_invented_criminal_regime():
    with pytest.raises(ValueError, match="liability_regime"):
        validate_penalty_source_evidence(
            "Сотрудник подвергается дисциплинарному наказанию.",
            "дисциплинарное или уголовное наказание",
            {"other": "дисциплинарное или уголовное наказание"},
        )


def test_license_may_cannot_become_automatic_with_wrong_actor_and_ground():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_penalty_source_evidence(
            "Лицензия может быть аннулирована судом при мошенничестве.",
            (
                "автоматическое аннулирование лицензии полицией "
                "за опоздание"
            ),
            {
                "other": (
                    "автоматическое аннулирование лицензии полицией "
                    "за опоздание"
                )
            },
        )


def test_external_law_possibility_is_not_an_operative_penalty():
    with pytest.raises(ValueError, match="liability_regime"):
        validate_penalty_source_evidence(
            (
                "За нарушение правил могут предусматриваться "
                "административные или уголовные наказания в соответствии "
                "с законом."
            ),
            "административное или уголовное наказание",
            {"other": "административное или уголовное наказание"},
        )


def test_external_law_meta_claim_is_forbidden_in_structured_penalty():
    text = (
        "За нарушение правил могут предусматриваться административные "
        "или уголовные наказания в соответствии с законом."
    )
    with pytest.raises(ValueError, match="non-sanction qualitative claim"):
        validate_penalty_source_evidence(
            text,
            text,
            {"other": text},
        )


def test_reclassification_and_exception_do_not_belong_in_penalty_other():
    source = (
        "Деяние переквалифицируется из административного в уголовное "
        "преступление, нарушитель привлекается к ответственности по "
        "статье 16.20 УК с последующим арестом.\n"
        "Исключение: статья 6.12 АК."
    )
    with pytest.raises(ValueError, match="non-sanction qualitative claim"):
        validate_penalty_source_evidence(
            source,
            (
                "переквалификация административного нарушения в уголовное; "
                "статья 6.12 АК является исключением"
            ),
            {
                "other": (
                    "переквалификация административного нарушения в "
                    "уголовное; статья 6.12 АК является исключением"
                )
            },
        )


def test_nonnumeric_statutory_cap_is_typed():
    validate_penalty_source_evidence(
        (
            "Наказание за деяние не может превышать меры, которая "
            "предусмотрена санкцией соответствующей статьи."
        ),
        "не более санкции соответствующей статьи",
        {"other": "мера не может превышать санкцию нарушенной статьи"},
    )


def test_nonnumeric_statutory_cap_direction_cannot_reverse():
    with pytest.raises(ValueError, match="penalty_bound"):
        validate_penalty_source_evidence(
            (
                "Наказание не может превышать меры, предусмотренной "
                "санкцией статьи."
            ),
            "наказание не менее санкции статьи",
            {"other": "мера превышает санкцию статьи"},
        )


def test_ordinary_upper_bound_is_not_a_penalty_cue():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "Скорость не более 60 км/ч предусмотрена правилами.",
            "не более санкции статьи",
            {"other": "не более санкции статьи"},
        )


@pytest.mark.parametrize("role_form", ["задержанный", "задержанным"])
def test_detained_person_role_is_not_an_arrest_action(role_form):
    validate_penalty_source_evidence(
        "Наказание: лишение свободы до 20 месяцев.",
        f"лишение свободы до 20 месяцев, если нарушитель {role_form}",
        {"prison": f"до 20 месяцев, если нарушитель {role_form}"},
    )


@pytest.mark.parametrize(
    "action",
    [
        "нарушение влечет задержание",
        "сотрудник может задержать нарушителя",
        "нарушитель может быть задержан",
    ],
)
def test_real_detention_actions_remain_arrest_evidence(action):
    validate_penalty_source_evidence(
        action,
        "задержание нарушителя",
        {"arrest": "задержание нарушителя"},
    )


def test_penalty_reduction_by_factor_is_typed():
    validate_penalty_source_evidence(
        "Явка с повинной смягчает наказание по статье в 2 раза.",
        "наказание уменьшается в 2 раза",
        {"other": "уменьшение наказания в 2 раза"},
    )


def test_wrapped_penalty_reduction_by_factor_is_typed():
    validate_penalty_source_evidence(
        "Явка с повинной смягчает наказание по статье в\n2 раза\n.",
        "наказание уменьшается в 2 раза",
        {"other": "уменьшение наказания в 2 раза"},
    )


def test_wrapped_penalty_increase_cannot_become_reduction():
    with pytest.raises(ValueError, match="penalty_reduction"):
        validate_penalty_source_evidence(
            "При рецидиве наказание увеличивается в\n3 раза\n.",
            "наказание уменьшается в 3 раза",
            {"other": "уменьшение наказания в 3 раза"},
        )


def test_penalty_reduction_cannot_become_increase():
    with pytest.raises(ValueError, match="penalty_reduction"):
        validate_penalty_source_evidence(
            "Явка с повинной смягчает наказание по статье в 2 раза.",
            "наказание увеличивается в 2 раза",
            {"other": "увеличение наказания в 2 раза"},
        )


def test_non_penalty_factor_is_not_a_sanction_cue():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "Скорость обработки уменьшается в 2 раза.",
            "уменьшение наказания в 2 раза",
            {"other": "уменьшение наказания в 2 раза"},
        )


def test_wrapped_non_penalty_factor_is_not_a_sanction_cue():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "Документ составлен в\n2 экземплярах.",
            "уменьшение наказания в 2 раза",
            {"other": "уменьшение наказания в 2 раза"},
        )


def test_decision_to_impose_fine_is_explicit_source_evidence():
    validate_penalty_source_evidence(
        (
            "Уполномоченным лицом может быть принято решение о наложении "
            "штрафа в размере до 40.000$."
        ),
        "штраф до 40 000 долларов",
        {"fine": "до 40 000 долларов"},
    )


def test_passive_optional_fine_is_explicit_source_evidence():
    validate_penalty_source_evidence(
        "За нарушение штраф может быть назначен судом.",
        "штраф может быть назначен судом",
        {"fine": "штраф может быть назначен судом"},
    )


def test_negated_passive_fine_is_not_imposition_evidence():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "За это нарушение штраф не может быть назначен.",
            "штраф может быть назначен",
            {"fine": "штраф может быть назначен"},
        )


def test_decision_to_edit_fine_log_is_not_a_fine_cue():
    with pytest.raises(ValueError, match="no explicit sanction cue"):
        validate_penalty_source_evidence(
            "Может быть принято решение о корректировке журнала штрафов.",
            "штраф до 40 000 долларов",
            {"fine": "до 40 000 долларов"},
        )


def test_optional_fine_cannot_become_automatic():
    with pytest.raises(ValueError, match="fine_imposition"):
        validate_penalty_source_evidence(
            (
                "Может быть принято решение о наложении штрафа "
                "до 40.000$."
            ),
            "автоматически будет наложен штраф 40 000 долларов",
            {"fine": "40 000 долларов"},
        )


def test_standalone_legal_reference_does_not_authorize_number():
    with pytest.raises(ValueError, match="numeric fact '6'"):
        validate_penalty_source_evidence(
            "Наказание: иная мера.\nИсключение: 6.12 АК.",
            "иная мера 6",
            {"other": "иная мера 6"},
        )


@pytest.mark.parametrize("wrong_reference", ["6.13 АК", "6.12 УК"])
def test_exception_reference_number_and_family_are_exact(wrong_reference):
    with pytest.raises(ValueError, match="applicability_exception"):
        validate_penalty_source_evidence(
            (
                "Нарушитель привлекается к ответственности с последующим "
                "арестом.\nИсключение: статья 6.12 АК."
            ),
            f"арест; статья {wrong_reference} является исключением",
            {"arrest": "арест"},
        )


def test_neutral_search_synonyms_are_not_treated_as_penalty_assertions():
    validate_synonym_source_evidence(
        "Наказание: штраф в размере 5000$.",
        [
            "не оплатил ранее выписанный штраф",
            "что будет за нарушение правила",
        ],
    )


def test_position_noun_does_not_make_optional_reprimand_mandatory():
    validate_penalty_source_evidence(
        (
            "Работодатель вправе применить следующие дисциплинарные "
            "взыскания: выговор или понижение в должности."
        ),
        "выговор или понижение в должности",
        {"other": "выговор или понижение в должности"},
    )


def test_maximum_penalty_accepts_no_mitigation_noun_form():
    validate_penalty_source_evidence(
        (
            "Наказание: нарушители наказываются максимально без применения "
            "смягчающих обстоятельств."
        ),
        "нарушители наказываются максимально без смягчения",
        {"other": "наказываются максимально без смягчения"},
    )


def test_maximum_penalty_references_are_bound_to_its_own_clause():
    validate_synonym_source_evidence(
        (
            "Статья 10. За нарушение ч. 1-2 ст. 7 следует увольнение. "
            "Если прокурор виновен в преступлениях, предусмотренных: "
            "15.1, 15.1.1 и 17.5 УК, он несёт максимально "
            "предусмотренную этими статьями меру наказания без применения "
            "смягчающих обстоятельств."
        ),
        [
            (
                "прокурор по ст. 15.1, 15.1.1 и 17.5 ук "
                "наказывается максимально без смягчения"
            )
        ],
    )


def test_maximum_penalty_clause_preserves_decimal_and_abbreviated_references():
    source = (
        "По ст. 15.1, 15.1.1 и 17.5 УК нарушитель наказывается "
        "максимально без смягчения."
    )
    validate_penalty_source_evidence(
        source,
        "максимальная мера по указанным статьям УК без смягчения",
        {
            "other": (
                "максимальная мера по статьям 15.1, 15.1.1, 17.5 УК "
                "без смягчения"
            )
        },
    )


def test_each_maximum_relation_is_extracted_independently():
    validate_synonym_source_evidence(
        (
            "По ст. 1 УК лицо наказывается максимально. "
            "По ст. 2 УК лицо наказывается максимально без смягчения."
        ),
        ["по ст. 2 ук наказывается максимально без смягчения"],
    )


def test_common_abbreviation_does_not_cut_maximum_reference_context():
    validate_synonym_source_evidence(
        (
            "По ст. 15.1 УК, т.е. по специальной норме, "
            "лицо наказывается максимально без смягчения."
        ),
        ["по ст. 15.1 ук наказывается максимально без смягчения"],
    )


@pytest.mark.parametrize(
    "abbreviation",
    ["т. е.", "т.н.", "т. к.", "и т. д.", "в г. Лос-Сантос"],
)
def test_spaced_or_extended_abbreviation_does_not_cut_maximum_context(
    abbreviation,
):
    validate_synonym_source_evidence(
        (
            f"По ст. 15.1 УК, {abbreviation} по специальной норме, "
            "лицо наказывается максимально без смягчения."
        ),
        ["по ст. 15.1 ук наказывается максимально без смягчения"],
    )


def test_closing_quote_ends_reference_context_before_maximum_relation():
    with pytest.raises(ValueError, match="maximum_statutory_penalty"):
        validate_synonym_source_evidence(
            (
                "«По ст. 15.1 УК действует первая норма.» "
                "Лицо наказывается максимально без смягчения."
            ),
            ["по ст. 15.1 ук наказывается максимально без смягчения"],
        )


def test_maximum_references_are_bound_after_independent_coordinated_predicate():
    validate_synonym_source_evidence(
        (
            "По ст. 1 УК определена подсудность, а по ст. 2 УК "
            "лицо наказывается максимально без смягчения."
        ),
        ["по ст. 2 ук наказывается максимально без смягчения"],
    )


def test_maximum_references_are_bound_after_coordinated_and_predicate():
    validate_synonym_source_evidence(
        (
            "По ст. 1 УК определена подсудность, и по ст. 2 УК "
            "лицо наказывается максимально без смягчения."
        ),
        ["по ст. 2 ук наказывается максимально без смягчения"],
    )


@pytest.mark.parametrize(
    "source",
    [
        "Максимальная мера социальной поддержки назначается комиссией.",
        "Максимальная мера пресечения выбирается судом.",
        (
            "Лицо наказывается максимально, и комиссия действует "
            "без применения смягчающих правил."
        ),
    ],
)
def test_non_penalty_maximum_or_neighbor_qualifier_is_not_evidence(source):
    with pytest.raises(ValueError, match="maximum_statutory_penalty"):
        validate_synonym_source_evidence(
            source,
            ["лицо наказывается максимально без смягчения"],
        )


def test_unrelated_clause_does_not_qualify_maximum_penalty():
    source = (
        "Нарушитель наказывается максимально. Отдельная процедура "
        "проводится без смягчения."
    )
    validate_synonym_source_evidence(
        source,
        ["нарушитель наказывается максимально"],
    )
    with pytest.raises(ValueError, match="maximum_statutory_penalty"):
        validate_synonym_source_evidence(
            source,
            ["нарушитель наказывается максимально без смягчения"],
        )


def test_unrelated_tone_does_not_create_no_mitigation_qualifier():
    validate_penalty_source_evidence(
        "Наказание: нарушители наказываются максимально.",
        "нарушители наказываются максимально без смягчения тона",
        {"other": "наказываются максимально без смягчения тона"},
    )


def test_official_position_noun_is_not_a_mandatory_synonym_assertion():
    validate_synonym_source_evidence(
        "Уполномоченное лицо может объявить розыск.",
        ["должностное лицо оформляет розыск"],
    )


@pytest.mark.parametrize(
    "source,synonym",
    [
        (
            "Субъект задержания сохраняет процессуальные права.",
            "субъект задержания не обязан держать дистанцию",
        ),
        (
            "Стороны соблюдают условия заключенного договора.",
            "стороны обязаны соблюдать заключенный договор",
        ),
        (
            "Ордер относится к задержанию должностного лица.",
            "обязательное судебное разбирательство по делу о задержании",
        ),
        (
            "Постановление содержит сведения о штрафе.",
            "сотрудник должен проверить постановление о штрафе",
        ),
        (
            "Материалы содержат сведения о розыске.",
            "сотрудники обязаны изучить сведения о розыске",
        ),
    ],
)
def test_mandatory_marker_must_govern_the_consequence(source, synonym):
    validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    "source,synonym,category",
    [
        (
            "Суд может наложить штраф.",
            "автоматическое наложение штрафа",
            "fine",
        ),
        (
            "Сотрудник может объявить лицо в розыск.",
            "автоматическое объявление лица в розыск",
            "wanted",
        ),
        (
            "Сотрудник вправе изъять оружие.",
            "автоматическое изъятие оружия",
            "seizure",
        ),
        (
            "Руководитель может уволить сотрудника.",
            "автоматическое увольнение сотрудника",
            "dismissal",
        ),
    ],
)
def test_optional_consequence_cannot_become_automatic(
    source,
    synonym,
    category,
):
    with pytest.raises(
        ValueError,
        match=(
            rf"mandatory consequence categories.*{category}|"
            r"fine_imposition"
        ),
    ):
        validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    "source,synonym",
    [
        (
            "Нарушение влечёт штраф.",
            "автоматическое наложение штрафа",
        ),
        (
            "Нарушение влечёт объявление в розыск.",
            "автоматическое объявление в розыск",
        ),
        (
            "Нарушение влечёт изъятие оружия.",
            "автоматическое изъятие оружия",
        ),
        (
            "Нарушение влечёт увольнение.",
            "автоматическое увольнение",
        ),
        (
            "Всё оружие изымается.",
            "автоматическое изъятие всего оружия",
        ),
        (
            "Всё оружие подлежит изъятию.",
            "автоматическое изъятие всего оружия",
        ),
        (
            "Правоохранительные органы обязаны изымать добычу.",
            "добыча подлежит изъятию правоохранителями",
        ),
    ],
)
def test_explicit_mandatory_consequence_is_preserved(source, synonym):
    validate_synonym_source_evidence(source, [synonym])


def test_long_passive_compensation_relation_is_mandatory():
    validate_synonym_source_evidence(
        (
            "Вред, причиненный гражданам и организациям противоправными "
            "действиями сотрудника при выполнении служебных обязанностей, "
            "подлежит возмещению."
        ),
        ["сотрудник обязан компенсировать причиненный ущерб"],
    )


def test_notification_duty_does_not_make_seizure_mandatory():
    validate_synonym_source_evidence(
        "Лицензия вправе быть изъята по решению суда.",
        [
            "суд может изъять лицензию",
            "владельца обязаны уведомить об изъятии лицензии",
        ],
    )


def test_should_follow_immediate_dismissal_is_mandatory():
    validate_synonym_source_evidence(
        "За нарушение следует немедленное увольнение.",
        ["нарушение влечет немедленное увольнение"],
    )


def test_duty_to_consider_dismissal_is_not_mandatory_dismissal():
    with pytest.raises(
        ValueError,
        match="mandatory consequence categories.*dismissal",
    ):
        validate_synonym_source_evidence(
            "Комиссии следует рассмотреть увольнение сотрудника.",
            ["автоматическое увольнение сотрудника"],
        )


@pytest.mark.parametrize(
    "source,synonym",
    [
        (
            "Сотрудник не обязан изымать оружие.",
            "автоматическое изъятие оружия",
        ),
        (
            "Руководитель не должен увольнять сотрудника.",
            "автоматическое увольнение сотрудника",
        ),
        (
            "Штраф не будет наложен.",
            "штраф автоматически наложен",
        ),
        (
            "Оружие не подлежит изъятию.",
            "автоматическое изъятие оружия",
        ),
    ],
)
def test_negated_mandatory_relation_does_not_authorize_consequence(
    source,
    synonym,
):
    with pytest.raises(ValueError, match="mandatory consequence categories"):
        validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    "synonym",
    [
        "автоматическая отмена штрафа",
        "автоматическое снятие розыска",
        "автоматический возврат изъятого",
        "обязательное заседание по задержанию",
    ],
)
def test_opposite_direction_is_not_a_mandatory_consequence(synonym):
    validate_synonym_source_evidence("Устанавливается общий порядок.", [synonym])


def test_alternative_dismissal_does_not_authorize_automatic_dismissal():
    with pytest.raises(
        ValueError,
        match="mandatory consequence categories.*dismissal",
    ):
        validate_synonym_source_evidence(
            "Нарушение влечёт увольнение или выговор.",
            ["автоматическое увольнение"],
        )


def test_relative_prosecutor_fine_actor_is_clause_local():
    validate_synonym_source_evidence(
        (
            "Нарушения рассматриваются прокуратурой штата, которая "
            "при выявлении нарушения применяет штрафные санкции "
            "соответствующим постановлением."
        ),
        ["прокуратура применяет штрафные санкции при нарушении"],
    )


@pytest.mark.parametrize(
    "source",
    [
        "Судебный штраф может назначаться как дополнительное наказание.",
        "Штрафные санкции выписываются судом своим постановлением.",
    ],
)
def test_explicit_judicial_fine_relation_preserves_court_authority(source):
    validate_synonym_source_evidence(
        source,
        ["суд может назначить штраф"],
    )


def test_participial_passive_fine_preserves_each_local_authority():
    source = (
        "Невыполнение принципов несет штрафные санкции, "
        "выписываемые судом, прокуратурой или кабинетом губернатора."
    )
    validate_synonym_source_evidence(
        source,
        [
            "суд выписывает штрафную санкцию",
            "прокуратура выписывает штрафную санкцию",
        ],
    )


@pytest.mark.parametrize(
    "source",
    [
        "Прокуратура рассматривает материалы о штрафах.",
        "Прокуратура не применяет штрафные санкции.",
    ],
)
def test_prosecutor_mention_without_fine_action_is_not_authority(source):
    with pytest.raises(ValueError, match="fine_imposition"):
        validate_synonym_source_evidence(
            source,
            ["прокуратура применяет штрафные санкции"],
        )


def test_pass_issuer_anaphora_is_a_local_actor_relation():
    validate_synonym_source_evidence(
        (
            "Пропуск, выданный руководителем государственной организации, "
            "может быть им отозван в любое время."
        ),
        ["руководитель может отозвать выданный им пропуск"],
    )


def test_issuer_without_anaphora_does_not_become_revocation_actor():
    with pytest.raises(ValueError, match="document_revocation"):
        validate_synonym_source_evidence(
            "Пропуск, выданный руководителем, отозван судом.",
            ["руководитель отозвал пропуск"],
        )


def test_affected_organization_is_not_pass_revocation_actor():
    with pytest.raises(ValueError, match="document_revocation"):
        validate_synonym_source_evidence(
            (
                "Губернатор вправе аннулировать пропуск, если нахождение "
                "лица нарушает работу государственной организации."
            ),
            ["организация отменяет пропуск"],
        )


def test_real_organization_subject_can_revoke_pass():
    validate_synonym_source_evidence(
        "Организация может аннулировать выданный ею пропуск.",
        ["организация может аннулировать пропуск"],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            "Организация может аннулировать выданный ею пропуск.",
            ["организация аннулирует пропуск"],
        )


def test_remote_prosecutor_does_not_own_license_revocation():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            (
                "Ордер подписан прокурором. После третьего предупреждения "
                "лицензия становится недействительной."
            ),
            ["прокурор аннулирует лицензию"],
        )


@pytest.mark.parametrize(
    "source",
    [
        (
            "Лицензия аннулирована организацией, а решение затем "
            "подтверждено судом."
        ),
        (
            "Лицензия аннулирована организацией, а по решению суда "
            "выплачивается компенсация."
        ),
        "Лицензия аннулирована организацией и затем подтверждена судом.",
        "Лицензия аннулирована организацией после проверки судом.",
        (
            "Лицензия аннулирована организацией и по решению суда "
            "выплачивается компенсация."
        ),
    ],
)
def test_neighbor_predicate_court_does_not_own_license_revocation(source):
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            source,
            ["суд аннулирует лицензию"],
        )


def test_coordinated_decision_remains_an_authority_for_same_license_action():
    validate_synonym_source_evidence(
        (
            "Лицензия аннулирована организацией, "
            "а также по решению суда."
        ),
        ["суд аннулирует лицензию"],
    )


def test_conviction_word_does_not_create_court_authority():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Лицензия становится недействительной при активной судимости.",
            ["суд аннулирует лицензию"],
        )


def test_weapon_seizure_near_license_is_not_license_seizure():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Оружие изымается при наличии лицензии.",
            ["суд изымает лицензию"],
        )


def test_hunting_yield_seizure_without_license_is_not_license_seizure():
    validate_synonym_source_evidence(
        (
            "Осуществление охоты при отсутствии действующей лицензии. "
            "Наказание: штраф, а также полное изъятие всей добычи."
        ),
        ["штраф и изъятие добычи за охоту без лицензии"],
    )


def test_license_mention_before_weapon_seizure_is_not_license_seizure():
    validate_synonym_source_evidence(
        "Норма устанавливает порядок учёта оружия и лицензий.",
        ["лицензия указана в материалах, затем изъятие оружия"],
    )


def test_license_holder_is_not_the_object_of_weapon_seizure():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            (
                "Правоохранители изымают оружие у лиц, имеющих "
                "действующую лицензию."
            ),
            ["правоохранители изымают лицензию"],
        )


@pytest.mark.parametrize(
    "source",
    [
        "Суд изъял оружие и затем проверил у гражданина лицензию.",
        "Лицензия проверяется сотрудником и оружие изымается судом.",
        "Суд изымает документ, подтверждающий наличие лицензии.",
        "Суд изымает оружие, зарегистрированное по лицензии.",
        "Для лицензии документ был изъят судом.",
        "По лицензии оружие изъято судом.",
        "Суд изъял копию лицензии.",
        "Произведено изъятие копии лицензии.",
        (
            "Лицензия, указанная в деле, затем оружие "
            "изымается судом."
        ),
    ],
)
def test_license_mention_outside_seizure_object_relation_is_rejected(source):
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            source,
            ["суд изымает лицензию"],
        )


def test_distant_passive_object_after_comma_is_not_license_seizure():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Лицензия указана в деле, затем оружие изъято судом.",
            ["суд изъял лицензию"],
        )


def test_licensing_process_is_not_an_individual_license():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Порядок лицензирования: оружие изымается судом.",
            ["суд изымает лицензию"],
        )


def test_direct_license_seizure_forms_remain_supported():
    validate_synonym_source_evidence(
        "Суд вправе изъять у гражданина лицензию.",
        ["суд может изъять у гражданина лицензию"],
    )
    validate_synonym_source_evidence(
        "Лицензия изъята судом.",
        ["суд изъял лицензию"],
    )
    validate_synonym_source_evidence(
        "Суд аннулирует выданную без ограничения срока лицензию.",
        ["суд аннулирует лицензию"],
    )
    validate_synonym_source_evidence(
        "Лицензии на оружие изымаются судом.",
        ["суд изымает лицензии на оружие"],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            "Лицензии на оружие изымаются судом.",
            ["суд изымает лицензии"],
        )
    validate_synonym_source_evidence(
        "Судом изымается лицензия.",
        ["суд изымает лицензию"],
    )
    validate_synonym_source_evidence(
        "Изъятию подлежит лицензия.",
        ["изъятие лицензии"],
    )
    validate_synonym_source_evidence(
        "Суд изъял все имеющиеся у него лицензии.",
        ["суд изъял все имеющиеся у него лицензии"],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            "Суд изъял все имеющиеся у него лицензии.",
            ["суд изъял лицензии"],
        )
    validate_synonym_source_evidence(
        "Допускается изъятие оружия, а также лицензии.",
        ["изъятие лицензии"],
    )
    validate_synonym_source_evidence(
        "При просрочке лицензия считается недействительной.",
        ["лицензия становится недействительной"],
    )


def test_license_modality_is_bound_to_its_own_action_relation():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            (
                "Суд может изъять лицензию, "
                "а оружие должно быть изъято."
            ),
            ["суд обязан изъять лицензию"],
        )


def test_license_ground_is_bound_to_its_own_action_relation():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            (
                "Суд может аннулировать лицензию "
                "и оштрафовать за мошенничество."
            ),
            ["суд аннулирует лицензию при мошенничестве"],
        )


def test_fine_actor_and_modality_are_bound_to_each_relation():
    with pytest.raises(ValueError, match="fine_imposition"):
        validate_synonym_source_evidence(
            (
                "Суд может наложить штраф, "
                "а прокурор обязан наложить штраф."
            ),
            ["суд обязан наложить штраф"],
        )


@pytest.mark.parametrize(
    ("source", "synonym"),
    [
        (
            "Суд может наложить штраф и прокурор обязан наложить штраф.",
            "суд обязан наложить штраф",
        ),
        (
            "Прокурор может наложить штраф и суд обязан наложить штраф.",
            "прокурор обязан наложить штраф",
        ),
    ],
)
def test_fine_modality_cannot_cross_unpunctuated_predicates(source, synonym):
    with pytest.raises(ValueError, match="fine_imposition"):
        validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    ("source", "synonym", "action"),
    [
        (
            "Организация может наложить штраф и решение затем "
            "подтверждено судом.",
            "суд может наложить штраф",
            "fine_imposition",
        ),
        (
            "Организация аннулировала пропуск и решение подтверждено судом.",
            "суд аннулирует пропуск",
            "document_revocation",
        ),
        (
            "Распространение продукции СМИ прекращено организацией "
            "и решение подтверждено судом.",
            "суд прекращает распространение продукции сми",
            "media_distribution_cessation",
        ),
        (
            "Суд проверил лицензию, которая затем аннулирована организацией.",
            "суд аннулирует лицензию",
            "instrument_revocation",
        ),
    ],
)
def test_neighbor_predicate_cannot_lend_actor_to_sensitive_action(
    source,
    synonym,
    action,
):
    with pytest.raises(ValueError, match=action):
        validate_synonym_source_evidence(source, [synonym])


@pytest.mark.parametrize(
    "source",
    [
        "Суд аннулировал доверенность на получение лицензии.",
        (
            "Суд рассмотрел заявление, которое предусматривает "
            "аннулирование лицензии."
        ),
    ],
)
def test_container_document_is_not_the_license_revocation_object(source):
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            source,
            ["суд аннулирует лицензию"],
        )


def test_optional_license_action_cannot_become_definite():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Лицензия может быть аннулирована судом.",
            ["суд аннулирует лицензию"],
        )


@pytest.mark.parametrize(
    "synonym",
    [
        "суд аннулирует лицензию за коррупцию",
        "суд аннулирует лицензию за убийство",
        "суд аннулирует лицензию при нарушении закона",
    ],
)
def test_explicit_unknown_license_ground_requires_matching_source(synonym):
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Лицензия может быть аннулирована судом.",
            [synonym],
        )


@pytest.mark.parametrize(
    "synonym",
    [
        "суд лишает гражданина лицензии",
        "суд отменяет лицензию",
        "суд прекращает действие лицензии",
        "лицензия утрачивает силу",
    ],
)
def test_dangerous_license_action_vocabulary_never_becomes_claimless(
    synonym,
):
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Норма устанавливает порядок подачи заявления.",
            [synonym],
        )


@pytest.mark.parametrize(
    "source",
    [
        "Лицензия аннулирована на основании решения суда.",
        "Лицензия аннулирована согласно решению суда.",
        "Суд принял решение аннулировать лицензию.",
        "Суд вынес решение об аннулировании лицензии.",
        "Суд постановил аннулировать лицензию.",
    ],
)
def test_valid_court_decision_templates_preserve_license_authority(source):
    validate_synonym_source_evidence(
        source,
        ["суд аннулирует лицензию"],
    )


@pytest.mark.parametrize(
    "source",
    [
        "Суд имеет право изъять лицензию.",
        "Суду разрешается изъять лицензию.",
    ],
)
def test_valid_court_right_templates_preserve_license_authority(source):
    validate_synonym_source_evidence(
        source,
        ["суд может изъять лицензию"],
    )


def test_passive_court_pass_revocation_preserves_authority():
    validate_synonym_source_evidence(
        "Судом аннулируется пропуск.",
        ["суд аннулирует пропуск"],
    )


def test_conditional_pass_revocation_supports_topical_after_revocation_query():
    validate_synonym_source_evidence(
        (
            "В случае аннулирования пропуска во время нахождения лица "
            "на закрытой территории лицо обязуется немедленно уйти."
        ),
        ["после отмены пропуска нужно немедленно уйти"],
    )


def test_optional_court_pass_revocation_supports_nominal_query_set():
    validate_synonym_source_evidence(
        (
            "Пропуск может быть аннулирован судом в качестве "
            "дополнительной меры наказания, когда преступление совершено "
            "с его использованием."
        ),
        [
            "отмена разрешения за совершение преступления с пропуском",
            "лишение допуска судом как дополнительное наказание",
        ],
    )


def test_license_law_violation_ground_is_relation_local_and_critical():
    source = (
        "За нарушение любого из положений статьи последует "
        "отзыв лицензии."
    )
    validate_synonym_source_evidence(
        source,
        ["отзыв лицензии за нарушение положений статьи"],
    )
    with pytest.raises(ValueError, match="critical qualifier"):
        validate_synonym_source_evidence(
            source,
            ["отзыв лицензии"],
        )


def test_weapon_license_seizure_for_offense_has_law_violation_ground():
    source = (
        "Служащие должны изъять лицензию на оружие у гражданина "
        "за правонарушения с использованием огнестрельного оружия."
    )
    validate_synonym_source_evidence(
        source,
        ["изъятие оружейной лицензии за правонарушение"],
    )


def test_explicit_warrant_authority_list_supports_each_named_actor():
    source = (
        "Список лиц, уполномоченных выписывать ордера:\n"
        "— Губернатор\n"
        "—\n"
        "Вице-Губернатор\n"
        "—\n"
        "Генеральный Прокурор\n"
        "— Заместитель Генерального прокурора\n"
        "— Председатель Верховного суда\n"
        "— Верховный Судья\n"
        "— Министр юстиции"
    )
    validate_synonym_source_evidence(
        source,
        [
            "губернатор выдает ордер",
            "генеральный прокурор выписывает ордер",
            "председатель верховного суда подписывает ордер",
            "министр юстиции оформляет ордер",
        ],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            ["полиция выдает ордер"],
        )


@pytest.mark.parametrize(
    ("source", "synonym"),
    [
        (
            "Ордер на проведение обыска выписывает губернатор штата.",
            "губернатор выдает ордер на обыск",
        ),
        (
            (
                "Ордер на проведение рейда выписывает верховный судья "
                "с дальнейшим назначением судебного заседания."
            ),
            "верховный судья подписывает ордер на рейд",
        ),
    ],
)
def test_inverted_warrant_issuer_relation_is_source_backed(
    source: str,
    synonym: str,
) -> None:
    validate_synonym_source_evidence(source, [synonym])


def test_inverted_warrant_issuer_requires_full_target_contract() -> None:
    source = (
        "Ордер на задержание членов кабинета министров, "
        "заместителей министра юстиции, мэра агломерации, судей "
        "и прокуроров выписывает министр Юстиции, верховный "
        "судья или губернатор штата."
    )
    validate_synonym_source_evidence(
        source,
        [
            (
                "министр юстиции выписывает ордер на задержание членов "
                "кабинета министров, заместителей министра юстиции, мэра "
                "агломерации, судей и прокуроров"
            )
        ],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            ["министр юстиции оформляет ордер"],
        )


def test_list_heading_modality_governs_identity_hold_item():
    source = (
        "Военнослужащие вооруженных сил вправе:\n"
        "13.1.1. Проверять документы граждан.\n"
        "13.1.2. Осуществлять личный обыск.\n"
        "13.1.3. Лиц, не имеющих при себе документов, удостоверяющих "
        "их личность, задерживать до установления их личности."
    )
    validate_synonym_source_evidence(
        source,
        ["человека без документов держат до установления личности"],
    )


def test_optional_issue_is_a_topical_finite_synonym_but_not_a_title_fact():
    validate_synonym_source_evidence(
        "Руководитель вправе выдавать сотруднику пропуск.",
        ["руководитель выдаёт сотруднику пропуск"],
    )


def test_required_validity_condition_is_mandatory_document_evidence():
    source = (
        "Документ, предоставляющий допуск к государственной тайне, "
        "считается действительным при соблюдении следующих условий:\n"
        "Подписан уполномоченным должностным лицом.\n"
        "Опубликован на официальном портале."
    )
    validate_synonym_source_evidence(
        source,
        [
            (
                "документ, предоставляющий допуск к государственной тайне, "
                "должен быть подписан уполномоченным должностным лицом"
            )
        ],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            ["допуск должен быть подписан уполномоченным лицом"],
        )


def test_license_becomes_invalid_is_revocation_evidence():
    validate_synonym_source_evidence(
        (
            "После третьего предупреждения лицензия становится "
            "недействительной."
        ),
        ["третье предупреждение аннулирует лицензию"],
    )


def test_signed_warrant_participle_does_not_borrow_freeze_modality():
    validate_synonym_source_evidence(
        (
            "В случае подписания ордера на рейд лицензия организации "
            "автоматически замораживается."
        ),
        [
            (
                "подписанный ордер на рейд автоматически замораживает "
                "лицензию организации"
            )
        ],
    )


def test_named_police_departments_are_passive_license_issuers():
    validate_synonym_source_evidence(
        (
            "Лицензия на оружие — разрешение на хранение оружия. "
            "Выдается исключительно уполномоченными сотрудниками "
            "Los Santos Police Department или Los Santos County "
            "Sheriff’s Department."
        ),
        ["разрешение оформляют полиция или шерифы"],
    )


@pytest.mark.parametrize(
    "past_event",
    [
        "аннулировали допуск пока человек на закрытой территории",
        "отменили пропуск внутри режимного объекта",
        "отозвали допуск у человека на закрытой территории",
    ],
)
def test_conditional_pass_revocation_does_not_prove_past_event(
    past_event: str,
):
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            (
                "В случае аннулирования пропуска лицо обязуется "
                "немедленно покинуть закрытую территорию."
            ),
            [past_event],
        )


@pytest.mark.parametrize(
    "source",
    [
        "Суд аннулировал допуск внутри режимного объекта.",
        "Суд отменил пропуск внутри режимного объекта.",
    ],
)
def test_definite_pass_revocation_supports_past_event_query(
    source: str,
):
    validate_synonym_source_evidence(
        source,
        ["суд отменил пропуск внутри режимного объекта"],
    )


def test_definite_pass_revocation_preserves_holder_and_location_contract():
    source = "Суд отозвал допуск у человека на закрытой территории."
    validate_synonym_source_evidence(
        source,
        ["суд отозвал допуск у человека на закрытой территории"],
    )
    with pytest.raises(ValueError):
        validate_synonym_source_evidence(
            source,
            ["суд отменил пропуск внутри режимного объекта"],
        )


def test_media_ground_is_bound_to_cessation_predicate():
    with pytest.raises(ValueError, match="media_distribution_cessation"):
        validate_synonym_source_evidence(
            (
                "Распространение продукции СМИ прекращается, "
                "а комиссия обсуждает нарушение авторских прав."
            ),
            [
                (
                    "прекращение распространения продукции сми "
                    "при нарушении авторских прав"
                )
            ],
        )




def test_relative_license_subject_and_coordinated_object_remain_supported():
    validate_synonym_source_evidence(
        (
            "Лицензия, выданная ранее, может быть изъята по решению "
            "суда."
        ),
        ["суд может изъять лицензию"],
    )
    validate_synonym_source_evidence(
        "Допускается изъятие оружия и лицензии.",
        ["изъятие лицензии"],
    )


def test_court_can_seize_license_as_instrument_action():
    validate_synonym_source_evidence(
        "Лицензия может быть изъята по решению суда.",
        ["суд может изъять лицензию"],
    )


def test_adjectival_court_pass_revocation_preserves_source_authority():
    validate_synonym_source_evidence(
        (
            "Решения по аннулированию пропусков, вынесенные Верховным "
            "Судом, обжалуются в нем же."
        ),
        ["апелляция на судебное аннулирование допуска"],
    )


def test_appeal_court_is_not_pass_revocation_actor():
    with pytest.raises(ValueError, match="document_revocation"):
        validate_synonym_source_evidence(
            (
                "Решение об аннулировании пропуска организацией может "
                "быть обжаловано в Верховном Суде."
            ),
            ["судебное аннулирование пропуска"],
        )


def test_judicial_appeal_does_not_make_court_the_revocation_actor():
    validate_synonym_source_evidence(
        "Организация аннулирует пропуск; решение можно обжаловать.",
        ["судебное обжалование аннулирования пропуска"],
    )


def test_generic_license_measures_do_not_authorize_specific_revocation():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            (
                "Предпринимать меры в отношении лицензии имеют право "
                "Министерство финансов и глава Министерства юстиции."
            ),
            ["кто вправе заморозить или отменить лицензию"],
        )


def test_one_valid_authority_covers_alternative_license_actors():
    validate_synonym_source_evidence(
        (
            "Лицензия вправе быть изъята лицами, уполномоченными на "
            "выдачу, а также по решению суда."
        ),
        ["суд может изъять лицензию"],
    )


def test_wrong_actor_cannot_replace_alternative_license_actors():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            (
                "Лицензия вправе быть изъята лицами, уполномоченными на "
                "выдачу, а также по решению суда."
            ),
            ["полиция изымает лицензию"],
        )


def test_mandatory_law_enforcement_license_action_preserves_actor():
    validate_synonym_source_evidence(
        (
            "Лицензия должна быть аннулирована и изъята при задержании "
            "сотрудниками правоохранительных органов за нарушение "
            "перечисленных статей УК."
        ),
        [
            (
                "правоохранители обязаны изъять лицензию при задержании "
                "по указанным статьям ук"
            )
        ],
    )


def test_mandatory_license_action_cannot_drop_police_actor():
    with pytest.raises(ValueError, match="critical authority"):
        validate_synonym_source_evidence(
            (
                "Лицензия должна быть аннулирована и изъята при задержании "
                "сотрудниками правоохранительных органов."
            ),
            ["лицензия обязательно аннулируется при задержании"],
        )


def test_synonym_cannot_strengthen_modality_actor_and_ground():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            (
                "Лицензия может быть аннулирована судом "
                "при мошенничестве."
            ),
            [
                (
                    "автоматическое аннулирование лицензии полицией "
                    "за опоздание"
                )
            ],
        )


def test_synonym_set_preserves_critical_license_actor_and_ground():
    validate_synonym_source_evidence(
        (
            "Лицензия может быть аннулирована судом "
            "при мошенничестве."
        ),
        ["аннулирование лицензии судом при мошенничестве"],
    )


def test_synonym_set_cannot_drop_all_critical_license_qualifiers():
    with pytest.raises(ValueError, match="critical authority|critical qualifier"):
        validate_synonym_source_evidence(
            (
                "Лицензия может быть аннулирована судом "
                "при мошенничестве."
            ),
            ["аннулирование лицензии"],
        )


def test_media_distribution_cannot_become_publication_ban():
    with pytest.raises(ValueError, match="media_publication_ban"):
        validate_synonym_source_evidence(
            (
                "При нарушении прав авторов распространение продукции "
                "СМИ может быть прекращено по решению суда."
            ),
            ["запрет выпуска медиапродукции по судебному решению"],
        )


def test_media_synonym_set_preserves_court_and_each_source_ground():
    validate_synonym_source_evidence(
        (
            "При нарушении прав авторов и в иных случаях, "
            "предусмотренных законом, распространение продукции СМИ "
            "может быть прекращено по решению суда."
        ),
        [
            (
                "суд прекращает распространение продукции сми "
                "при нарушении авторских прав"
            ),
            (
                "суд может прекратить распространение продукции сми "
                "в иных предусмотренных законом случаях"
            ),
        ],
    )


def test_synonym_cannot_reverse_penalty_factor_direction():
    with pytest.raises(ValueError, match="penalty_bound"):
        validate_synonym_source_evidence(
            (
                "Уголовная ответственность не может превышать "
                "половины максимального наказания."
            ),
            ["не менее половины максимального наказания"],
        )


def test_trusted_exact_scope_prefix_is_not_used_as_body_evidence():
    validate_synonym_source_evidence(
        (
            "Лицензия может быть аннулирована судом "
            "при мошенничестве."
        ),
        [
            (
                "[лицензирующий орган] аннулирование лицензии судом "
                "при мошенничестве"
            )
        ],
        trusted_scope_prefix="[лицензирующий орган] ",
    )

    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_synonym_source_evidence(
            "Норма устанавливает порядок подачи заявления.",
            [
                (
                    "[автоматическое аннулирование лицензии полицией "
                    "за опоздание] обычный запрос"
                )
            ],
        )


def test_combined_enrichment_source_contract_checks_synonyms():
    with pytest.raises(ValueError, match="instrument_revocation"):
        validate_enrichment_source_evidence(
            (
                "Лицензия может быть аннулирована судом "
                "при мошенничестве."
            ),
            "аннулирование лицензии судом при мошенничестве",
            {"other": "аннулирование лицензии судом при мошенничестве"},
            [
                (
                    "автоматическое аннулирование лицензии полицией "
                    "за опоздание"
                )
            ],
        )
