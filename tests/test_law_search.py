# START OF FILE tests/test_law_search.py
"""
Тесты нормализатора запросов умного поиска (без БД/сети).
"""

import asyncio

import pytest

import config
import services.law_search_service as law_search
from services.law_search_service import normalize_query, _fuse


# --- Срез вопросительной обёртки ---------------------------------------------------------------

@pytest.mark.parametrize("raw, expected_core", [
    ("какая статья за халатность", "халатность"),
    ("Какая статья за халатность?", "халатность"),
    ("что будет за убийство копа", "убийство копа"),
    ("сколько дают за угон транспорта", "угон транспорта"),
    ("наказание за кражу", "кражу"),
    ("подскажи пожалуйста что грозит за побег", "побег"),
    ("халатность", "халатность"),
])
def test_wrapper_stripping(raw, expected_core):
    assert normalize_query(raw).cleaned == expected_core


# --- Числительные и коды статей ----------------------------------------------------------------

def test_voice_numerals_to_code():
    nq = normalize_query("статья двенадцать точка один")
    assert "12.1" in nq.codes


def test_digit_with_dot_word_joined():
    # STT может отдать числа цифрами: «12 точка 1» обязан склеиться так же, как словесный путь
    nq = normalize_query("статья 12 точка 1")
    assert "12.1" in nq.codes and "1" not in nq.codes


def test_mixed_digit_and_word_joined():
    nq = normalize_query("статья 12 точка один")
    assert "12.1" in nq.codes


def test_voice_numerals_composite():
    nq = normalize_query("статья двадцать пять")
    assert "25" in nq.codes


def test_digit_code_direct():
    nq = normalize_query("статья 12.1")
    assert nq.codes == ["12.1"]


def test_code_from_plain_number_query():
    nq = normalize_query("12.5")
    assert nq.codes == ["12.5"]


def test_no_false_codes_in_text_query():
    nq = normalize_query("какая статья за халатность")
    assert nq.codes == []


def test_hyphen_subnumber_code():
    # Дефисная поднумерация УК/УПК RMRP: «статья 10-1» → код «10.1» (какой хранит каталог,
    # см. normalize_header). Без препроцессинга _TOKEN_RE разрезал бы на коды «10» и «1».
    # Части дефиса сохраняются как fallback: если «10.1» в каталоге нет (это был диапазон),
    # code-ноги ищут по частям — прежнее поведение.
    nq = normalize_query("статья 10-1 ук")
    assert nq.codes == ["10.1"]
    assert nq.dash_fallback_codes == ["10", "1"]
    assert "ук" in nq.cleaned


def test_hyphen_range_keeps_fallback_codes():
    # Plural syntax is authoritative even when a real subcode 10.15 exists.
    nq = normalize_query("статьи 10-15 ук")
    assert nq.codes == ["10", "15"]
    assert nq.dash_fallback_codes == []


def test_typed_hyphen_range_is_not_rewritten_as_article_subnumber():
    nq = normalize_query("при себе 10-15 грамм наркотиков")

    assert nq.codes == []
    assert nq.semantic_numbers == ["10", "15"]
    assert nq.applicability_query_ambiguous is True
    assert nq.dash_fallback_codes == []


@pytest.mark.parametrize(
    "raw",
    [
        "стоимость 5-10 тысяч долларов",
        "стоимость 5-10 тыс долларов",
        "стоимость 5-10k долларов",
        "стоимость 5-10к долларов",
    ],
)
def test_scaled_hyphen_range_keeps_two_numeric_endpoints(raw):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers == ["5", "10"]
    assert nq.applicability_query_ambiguous is True
    assert "5.10" not in nq.numeric_cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "статьи 10 и 15 УК",
        "статьи 10, 15 УК",
        "статьи 1,2,3 УК",
        "статьи с 10 по 15 УК",
        "статьи от 10 до 15 УК",
    ],
)
def test_plural_structural_lists_keep_all_legacy_code_candidates(raw):
    nq = normalize_query(raw)

    assert nq.codes == (
        ["1", "2", "3"]
        if "1,2,3" in raw
        else ["10", "15"]
    )
    assert nq.semantic_numbers == []


def test_plural_structural_words_do_not_steal_typed_quantity_range():
    nq = normalize_query("статьи о нарушении от 5 до 20 грамм")

    assert nq.codes == []
    assert nq.semantic_numbers == ["5", "20"]
    assert nq.applicability_query_ambiguous is True


@pytest.mark.parametrize(
    "raw",
    [
        "статьи №5-10",
        "статьи № 5-10",
        "статьи N/No 5-10",
        "статьи: 5-10",
        "в статьях 5-10",
        "по статьям 5-10",
        "в главах 5-10",
        "в разделах 5-10",
        "по пунктам 5-10",
        "в частях 5-10",
        "ст. 5-10",
        "статьи 5...10",
        "статьи 5 и N 10",
        "статьи с N 5 по N 10",
    ],
)
def test_plural_hyphen_reference_never_collapses_to_real_subcode(raw):
    nq = normalize_query(raw)

    assert nq.codes == ["5", "10"]
    assert "5.10" not in nq.codes


@pytest.mark.parametrize(
    "raw",
    [
        "статья №10-1",
        "ст. 10-1",
    ],
)
def test_singular_hyphen_reference_keeps_subcode(raw):
    assert normalize_query(raw).codes == ["10.1"]


def test_hyphen_group_adjacent_to_dot_untouched():
    # Смешанная группа с точкой не склеивается: прежнее поведение (split по дефису).
    nq = normalize_query("статья 10-1.5")
    assert nq.codes == ["10", "1.5"]
    assert nq.dash_fallback_codes == []


def test_hyphen_between_words_untouched():
    nq = normalize_query("какое-то нарушение")
    assert nq.codes == []
    assert nq.dash_fallback_codes == []
    assert "какое" in nq.cleaned or "нарушение" in nq.cleaned


def test_yo_normalized():
    assert "тяжелые" in normalize_query("тяжёлые повреждения").cleaned


def test_query_length_capped():
    nq = normalize_query("х" * 500)
    assert len(nq.original) <= 200


def test_empty_query():
    nq = normalize_query("")
    assert nq.cleaned == "" and nq.codes == []


def test_legal_vehicle_equivalent_keeps_original_query():
    nq = normalize_query("угон транспортного средства")
    assert nq.cleaned == "угон транспортного средства"
    assert nq.legal_variants == [
        "неправомерное завладение автомобилем или иным транспортным средством",
        "неправомерное завладение транспортным средством",
    ]


def test_vehicle_equivalent_preserves_government_qualifier():
    nq = normalize_query("угон государственного транспортного средства")
    assert all("государственного" in variant for variant in nq.legal_variants)


def test_vehicle_equivalent_preserves_no_theft_purpose_qualifier():
    nq = normalize_query("угон транспортного средства без цели хищения")
    assert all("без цели хищения" in variant for variant in nq.legal_variants)


def test_negated_vehicle_query_does_not_gain_positive_offence_variant():
    assert normalize_query("не угон транспортного средства").legal_variants == []


@pytest.mark.parametrize("raw", [
    "статья 38 человек находится в розыске",
    "статья 30 угон транспортного средства",
])
def test_explicit_article_code_disables_synthetic_rewrites(raw):
    nq = normalize_query(raw)
    assert nq.codes
    assert nq.legal_variants == []
    assert nq.intent_variants == []


@pytest.mark.parametrize(
    ("raw", "expected_kind", "expected_reason"),
    [
        ("статья 1", "article", "explicit_article_marker"),
        ("статью один", "article", "explicit_article_marker"),
        ("ст. 1", "article", "explicit_article_marker"),
        (
            "статья двенадцать точка один",
            "article",
            "explicit_article_marker",
        ),
        ("статья под номером 5", "article", "explicit_article_marker"),
        ("глава 1", "chapter", "explicit_chapter_marker"),
        ("глава номер пять", "chapter", "explicit_chapter_marker"),
        ("раздел 1", "section", "explicit_section_marker"),
        ("1 Административный Кодекс", None, "document_qualified_number"),
        (
            "в какой главе находится статья 5",
            None,
            "embedded_integer",
        ),
        ("глава 1 статья 5", None, "embedded_integer"),
    ],
)
def test_explicit_code_kind_is_narrow_and_unambiguous(
    raw,
    expected_kind,
    expected_reason,
):
    nq = normalize_query(raw)

    assert nq.codes
    assert law_search._requested_code_kind(nq) == expected_kind
    assert law_search._code_signal_reason(nq) == expected_reason


@pytest.mark.parametrize(
    ("raw", "number", "surface"),
    [
        ("в главе было 3 нарушения", "3", "3"),
        ("в разделе произошло 5 нарушений", "5", "5"),
        ("по статье предусмотрено 3 года", "3", "3"),
        ("какая статья если дают 3 года", "3", "3"),
        ("при себе ровно 5 грамм наркотиков", "5", "5"),
        ("при себе 10 г наркотиков", "10", "10"),
        ("тайно украл имущество на 5000 долларов", "5000", "5000"),
        ("тайно украл имущество на 5000$", "5000", "5000"),
        ("тайно украл имущество на $5000", "5000", "5000"),
        ("получил ровно 25% голосов", "25", "25"),
        ("когда одна партия получает все мандаты", "1", "одна"),
        ("одна партия получает все мандаты конгресса", "1", "одна"),
        ("власть разделена на три ветви", "3", "три"),
    ],
)
def test_situation_numbers_are_not_article_codes(raw, number, surface):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers == [number]
    assert surface in nq.tokens
    assert surface in nq.cleaned
    assert law_search._code_signal_reason(nq) is None


@pytest.mark.parametrize(
    ("raw", "expected_number"),
    [
        ("при себе 20,5 грамм наркотиков", "20.5"),
        ("при себе 20г наркотиков", "20"),
        ("при себе 20,5г наркотиков", "20.5"),
        ("имущество стоимостью 5000долларов", "5000"),
        ("удерживал 24часа", "24"),
        ("имущество на $50,000", "50000"),
        ("нашли 5,500 грамм", "5.5"),
    ],
)
def test_decimal_comma_and_attached_known_units_stay_one_semantic_value(
    raw,
    expected_number,
):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers == [expected_number]
    assert expected_number in nq.numeric_cleaned


def test_money_grouping_and_mass_decimal_keep_different_meanings():
    money = normalize_query("имущество на $50,000")
    mass = normalize_query("нашли 5,500 грамм")

    assert money.codes == []
    assert money.semantic_numbers == ["50000"]
    assert mass.codes == []
    assert mass.semantic_numbers == ["5.5"]
    assert law_search.evaluate_applicability(
        {
            1: {
                "title": "Стоимостью свыше $40.000",
                "full_text": "",
            }
        },
        numeric_query=money.numeric_cleaned,
        semantic_numbers=money.semantic_numbers,
    )[1]["verdict"] == "match"
    assert law_search.evaluate_applicability(
        {
            2: {
                "title": "Массой свыше 5 граммов",
                "full_text": "",
            }
        },
        numeric_query=mass.numeric_cleaned,
        semantic_numbers=mass.semantic_numbers,
    )[2]["verdict"] == "match"


def test_spoken_decimal_keeps_distinct_retrieval_surface():
    decimal_comma = normalize_query(
        "двадцать запятая пять грамм наркотиков"
    )
    decimal_point = normalize_query(
        "двадцать точка пять грамм наркотиков"
    )
    integer = normalize_query("двадцать пять грамм наркотиков")

    assert decimal_comma.semantic_numbers == ["20.5"]
    assert decimal_point.semantic_numbers == ["20.5"]
    assert "20.5" in decimal_comma.cleaned
    assert decimal_comma.cleaned == decimal_point.cleaned
    assert decimal_comma.cleaned != integer.cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "от 5 до 20 грамм наркотиков",
        "5 до 20 грамм наркотиков",
        "5 по 20 грамм наркотиков",
        "около 10 грамм наркотиков",
        "80 + 1 голос",
        "80 плюс 1 голос",
        "5 или 10 грамм наркотиков",
        "4-6 грамм наркотиков",
        "10+ грамм наркотиков",
        "~10 грамм наркотиков",
        ">10 грамм наркотиков",
        "$5-$10",
        "5$-10$",
        "~$10",
        "20,5–30,5 грамм наркотиков",
        "1 миллион 500 тысяч долларов",
        "1 миллион и 500 тысяч долларов",
    ],
)
def test_non_point_numeric_queries_disable_point_applicability(raw):
    nq = normalize_query(raw)

    assert nq.semantic_numbers
    assert nq.applicability_query_ambiguous is True
    assert nq.codes == []


def test_known_unit_split_does_not_split_general_alphanumeric_aliases():
    nq = normalize_query("правила GTA5RP")

    assert "gta5rp" in nq.tokens
    assert nq.codes == []


@pytest.mark.parametrize(
    ("raw", "expected_reason"),
    [
        ("УК 5", "document_qualified_number"),
        ("уголовный кодекс 5", "document_qualified_number"),
        ("пункт 5", "explicit_structure_marker"),
        ("часть 2", "explicit_structure_marker"),
        ("п. 5", "explicit_structure_marker"),
        ("том 2", "explicit_structure_marker"),
        ("номер 5", "explicit_structure_marker"),
    ],
)
def test_legacy_document_and_generic_structural_references_keep_code_leg(
    raw,
    expected_reason,
):
    nq = normalize_query(raw)

    assert nq.codes
    assert law_search._code_signal_reason(nq) == expected_reason


@pytest.mark.parametrize(
    ("raw", "expected_kind"),
    [
        ("в 5 статье УК", "article"),
        ("в 3 главе", "chapter"),
        ("согласно 2 части", None),
        ("в пункте 2", None),
    ],
)
def test_safe_inverted_structural_reference_keeps_code(raw, expected_kind):
    nq = normalize_query(raw)

    assert nq.codes
    assert law_search._requested_code_kind(nq) == expected_kind


@pytest.mark.parametrize(
    "raw",
    [
        "2 часть",
        "в 2 части закона",
        "согласно 2 части",
    ],
)
def test_inverted_part_reference_is_not_misread_as_hours(raw):
    assert normalize_query(raw).codes == ["2"]


@pytest.mark.parametrize("raw", ["2 часа", "5 часов", "11 часов", "21 час"])
def test_real_hour_counts_do_not_gain_weak_article_code(raw):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers


@pytest.mark.parametrize(
    "raw",
    [
        "прочитал 5 статей",
        "изучил 3 главы",
        "5 грамм наркотиков",
        "5 раз нарушил",
        "в течение 5 дней",
        "пять партий участвовали",
    ],
)
def test_counts_do_not_become_structural_code_references(raw):
    assert normalize_query(raw).codes == []


@pytest.mark.parametrize(
    "raw",
    [
        "5 законных оснований для обыска",
        "3 конституционных права гражданина",
        "2 уставные цели организации",
        "4 регламентированных действия",
        "7 кодексных норм",
        "5 нарушений закона",
        "5 положений закона",
        "5 требований регламента",
        "5 разделов кодекса",
    ],
)
def test_quantity_before_legal_adjective_is_not_document_reference(raw):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers
    assert law_search._should_resolve_document_scope(nq, None) is False


def test_citizen_noun_is_quantity_but_civil_code_adjective_is_document():
    citizens = normalize_query("5 граждан задержали")
    civil_code = normalize_query("5 гражданский кодекс")
    explicit = normalize_query("статья 5 гражданского кодекса")

    assert citizens.codes == []
    assert citizens.semantic_numbers == ["5"]
    assert civil_code.codes == ["5"]
    assert law_search._should_resolve_document_scope(civil_code, None) is True
    assert explicit.codes == ["5"]
    assert law_search._requested_code_kind(explicit) == "article"


def test_inverted_number_with_adjective_and_document_noun_resolves_scope():
    nq = normalize_query("в 5 гражданском кодексе")

    assert nq.codes == ["5"]
    assert law_search._requested_code_kind(nq) is None
    assert law_search._should_resolve_document_scope(nq, None) is True
    assert law_search._document_scope_qualifier(nq, None) == (
        "гражданском кодексе"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "пункт 5 УК",
        "номер 5 УК",
    ],
)
def test_generic_structural_marker_is_removed_from_document_qualifier(raw):
    nq = normalize_query(raw)

    assert law_search._document_scope_qualifier(nq, None) == "ук"
    assert law_search._doc_keywords(
        law_search._document_scope_qualifier(nq, None)
    ) == ["уголовн"]
    assert law_search._should_resolve_document_scope(nq, None) is True


def test_ambiguous_code_signal_tracks_position_not_equal_numeric_value():
    shorthand = normalize_query("5 разбой")
    assert shorthand.codes == ["5"]
    assert shorthand.semantic_numbers == ["5"]
    assert shorthand.ambiguous_code_signal is True

    explicit_and_amount = normalize_query(
        "статья 5 штраф 5 долларов"
    )
    assert explicit_and_amount.codes == ["5"]
    assert explicit_and_amount.semantic_numbers == ["5"]
    assert explicit_and_amount.ambiguous_code_signal is False


@pytest.mark.parametrize(
    ("raw", "expected_qualifier", "expected_keywords"),
    [
        ("глава 1 ук", "ук", ["уголовн"]),
        ("раздел 1 ук", "ук", ["уголовн"]),
        (
            "статья под номером 1 конституция",
            "конституция",
            ["конституц"],
        ),
        ("статье 1 конституция", "конституция", ["конституц"]),
    ],
)
def test_explicit_kind_syntax_is_removed_from_document_scope_only(
    raw,
    expected_qualifier,
    expected_keywords,
):
    nq = normalize_query(raw)
    requested_kind = law_search._requested_code_kind(nq)

    assert requested_kind is not None
    qualifier = law_search._document_scope_qualifier(nq, requested_kind)
    assert qualifier == expected_qualifier
    assert law_search._doc_keywords(qualifier) == expected_keywords
    assert nq.cleaned != ""  # the normal hybrid representation remains available


@pytest.mark.parametrize(
    ("raw", "expected_kind"),
    [
        ("статьи с 10 по 15 УК", "article"),
        ("статьи от 10 до 15 УК", "article"),
        ("статьи 10 и 15 УК", "article"),
        ("главы с 2 по 4 УК", "chapter"),
        ("разделы 2 и 4 УК", "section"),
    ],
)
def test_plural_structural_reference_keeps_kind_and_clean_document_scope(
    raw,
    expected_kind,
):
    nq = normalize_query(raw)

    assert law_search._requested_code_kind(nq) == expected_kind
    assert law_search._document_scope_qualifier(nq, expected_kind) == "ук"


@pytest.mark.asyncio
async def test_search_diagnostics_classify_numeric_context_without_changing_result(monkeypatch):
    expected = {
        "query": "3 раза стрелял по госнику",
        "results": [],
        "related": [],
        "degraded": False,
        "search_mode": "full",
        "warnings": [],
    }

    async def fake_impl(_server_id, _nq, **kwargs):
        kwargs["diagnostics"]["search_path"] = "hybrid"
        return expected

    monkeypatch.setattr(law_search, "_search_impl", fake_impl)
    diagnostics = {}
    response = await law_search.search(
        "server-any",
        "3 раза стрелял по госнику",
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert response is expected
    assert diagnostics["algorithm_version"] == law_search.LAW_SEARCH_ALGORITHM_VERSION
    assert diagnostics["query_features"]["codes"] == []
    assert diagnostics["query_features"]["semantic_numbers"] == ["3"]
    assert diagnostics["query_features"]["code_signal_reason"] is None
    assert diagnostics["query_features"]["requested_code_kind"] is None
    assert diagnostics["search_path"] == "hybrid"


@pytest.mark.asyncio
async def test_code_leg_filters_explicit_kind_before_limit(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 82334}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)

    assert await law_search._leg_code_query(
        "detroit",
        ["1"],
        requested_kind="article",
    ) == [82334]
    assert captured["args"] == (
        "detroit",
        ["1"],
        ["1.%"],
        "article",
    )
    sql = captured["query"]
    assert "AND ($4::text IS NULL OR kind = $4)" in sql
    assert sql.index("kind = $4") < sql.index("LIMIT 20")
    assert "law_document_id" in sql
    assert "source_order" in sql


@pytest.mark.asyncio
async def test_code_title_rescue_requires_same_code_and_complete_title_signature(
    monkeypatch,
):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 48372}, {"id": 48373}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)

    assert await law_search._leg_code_title_rescue(
        "denver",
        "3",
        "соблюдение прав и свобод граждан",
    ) == [48372, 48373]
    assert captured["args"] == (
        "denver",
        "3",
        "соблюдение прав и свобод граждан",
        None,
    )
    sql = captured["query"]
    assert "article.server_id = $1" in sql
    assert "article.code = $2" in sql
    assert "($4::text IS NULL OR article.kind = $4)" in sql
    assert "article.title IS NOT NULL" in sql
    assert "query_data.normalized_title" in sql
    assert "query_data.title_signature" in sql
    assert ">= 2" in sql
    assert "similarity(" not in sql
    assert "LIMIT 20" in sql


@pytest.mark.asyncio
async def test_explicit_code_title_filters_kind_before_limit(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 77249}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)

    assert await law_search._leg_explicit_code_title(
        "las_vegas",
        "4",
        "г",
        "article",
    ) == [77249]
    assert captured["args"] == (
        "las_vegas",
        "4",
        "г",
        "article",
    )
    sql = captured["query"]
    assert "article.server_id = $1" in sql
    assert "article.code = $2" in sql
    assert "($4::text IS NULL OR article.kind = $4)" in sql
    assert sql.index("article.kind = $4") < sql.index("LIMIT 20")


def test_code_title_rescue_is_strong_but_never_an_exact_or_protected_leg():
    assert law_search._LEG_WEIGHTS["code_title_rescue"] == 2.0
    assert "code_title_rescue" not in law_search._EXACT_LEGS
    assert law_search._LEG_WEIGHTS["explicit_code_title"] == 2.0
    assert "explicit_code_title" in law_search._EXACT_LEGS


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("статья 4 г", "г"),
        ("статья четыре г", "г"),
        ("статья под номером 4 По вопросам службы", "По вопросам службы"),
        ("покажи статью 4 Наказание", "Наказание"),
        ("по 4 статье За нарушение порядка", "За нарушение порядка"),
    ],
)
def test_explicit_code_title_candidate_preserves_real_title_words(
    raw,
    expected,
):
    nq = normalize_query(raw)
    candidate = law_search._explicit_code_title_candidate(nq)

    assert candidate is not None
    assert candidate == ("4", expected, "article")


@pytest.mark.parametrize(
    ("raw", "code", "expected"),
    [
        (
            "статья 20 Губернатор является главой исполнительной власти",
            "20",
            "Губернатор является главой исполнительной власти",
        ),
        (
            "статья 84 Поправки к главам 3-7 Конституции",
            "84",
            "Поправки к главам 3-7 Конституции",
        ),
        (
            "статья 2 Часть 1",
            "2",
            "Часть 1",
        ),
        (
            "статья 11 Утратила силу 01.02.2026",
            "11",
            "Утратила силу 01.02.2026",
        ),
        (
            "статья 9 Парковка в один ряд",
            "9",
            "Парковка в один ряд",
        ),
    ],
)
def test_explicit_code_title_candidate_ignores_title_internal_structure(
    raw,
    code,
    expected,
):
    assert law_search._explicit_code_title_candidate(
        normalize_query(raw)
    ) == (code, expected, "article")


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["статья 4 г", "статья четыре г"])
async def test_explicit_code_and_complete_one_letter_title_is_top_priority(
    monkeypatch,
    raw,
):
    nq = normalize_query(raw)
    correct_id = 77249
    code_noise_id = 78392
    captured = {}

    async def code_leg(*_args, **_kwargs):
        return [code_noise_id]

    async def explicit_leg(server_id, code, title, requested_kind):
        captured["explicit"] = (
            server_id,
            code,
            title,
            requested_kind,
        )
        return [correct_id]

    async def empty_ids(*_args, **_kwargs):
        return []

    async def empty_scored(*_args, **_kwargs):
        return []

    async def cards(ids):
        return {
            article_id: {
                "id": article_id,
                "code": "4",
                "kind": "article",
                "title": "Г" if article_id == correct_id else "Другая статья",
                "doc_title": "Закон",
                "article_ref": None,
                "penalty_summary": None,
                "full_text": "Текст нормы.",
            }
            for article_id in ids
        }

    async def related(_article_id):
        return False, []

    async def no_evidence(*_args, **_kwargs):
        return "empty", {}

    monkeypatch.setattr(law_search, "_leg_code", code_leg)
    monkeypatch.setattr(
        law_search,
        "_leg_explicit_code_title",
        explicit_leg,
    )
    monkeypatch.setattr(law_search, "_leg_fts", empty_scored)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", empty_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)
    monkeypatch.setattr(law_search, "_fetch_cards", cards)
    monkeypatch.setattr(law_search, "_fetch_related", related)
    monkeypatch.setattr(
        law_search,
        "_ranking_evidence_with_timeout",
        no_evidence,
    )

    diagnostics = {}
    response = await law_search._search_impl(
        "las_vegas",
        nq,
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert captured["explicit"] == (
        "las_vegas",
        "4",
        "г",
        "article",
    )
    assert response["results"][0]["id"] == correct_id
    assert response["results"][0]["sources"] == ["explicit_code_title"]
    assert diagnostics["priority_ids"] == [correct_id]
    assert diagnostics["legs"]["explicit_code_title"]["ids"] == [correct_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "correct_id", "code", "title"),
    [
        (
            "статья 12.6 Согласно закону о Закрытых и охраняемых территориях",
            72661,
            "12.6",
            "Согласно закону о Закрытых и охраняемых территориях",
        ),
        (
            "статья 3.4 Чрезвычайное и военное положение",
            80669,
            "3.4",
            "Чрезвычайное и военное положение",
        ),
    ],
)
async def test_explicit_exact_title_precedes_misleading_document_scope(
    monkeypatch,
    raw,
    correct_id,
    code,
    title,
):
    captured = {}

    async def explicit_leg(server_id, actual_code, actual_title, kind):
        captured["explicit"] = (
            server_id,
            actual_code,
            actual_title,
            kind,
        )
        return [correct_id]

    async def forbidden_scope(*_args, **_kwargs):
        raise AssertionError(
            "strict explicit title must resolve before document scope"
        )

    async def cards(ids):
        assert ids == [correct_id]
        return (
            "ok",
            {
                correct_id: {
                    "id": correct_id,
                    "code": code,
                    "kind": "article",
                    "title": title,
                    "doc_title": "Исходный документ",
                    "article_ref": None,
                    "penalty_summary": None,
                    "full_text": "Текст нормы.",
                }
            },
        )

    async def related(article_id):
        assert article_id == correct_id
        return "ok", []

    monkeypatch.setattr(
        law_search,
        "_leg_explicit_code_title",
        explicit_leg,
    )
    monkeypatch.setattr(
        law_search,
        "_leg_document_scope",
        forbidden_scope,
    )
    monkeypatch.setattr(law_search, "_cards_with_timeout", cards)
    monkeypatch.setattr(law_search, "_related_with_timeout", related)

    diagnostics = {}
    response = await law_search._search_impl(
        "production_server",
        normalize_query(raw),
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert captured["explicit"] == (
        "production_server",
        code,
        title,
        "article",
    )
    assert response["results"][0]["id"] == correct_id
    assert response["results"][0]["sources"] == [
        "explicit_code_title"
    ]
    assert diagnostics["search_path"] == "explicit_code_title"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (asyncio.TimeoutError(), "explicit_code_title_timeout"),
        (RuntimeError("db"), "explicit_code_title_error"),
    ],
)
async def test_explicit_exact_title_failure_never_falls_back_to_wrong_code(
    monkeypatch,
    failure,
    expected_code,
):
    async def failed_explicit(*_args, **_kwargs):
        raise failure

    async def forbidden_scope(*_args, **_kwargs):
        raise AssertionError(
            "failed authoritative exact lookup must fail closed"
        )

    monkeypatch.setattr(
        law_search,
        "_leg_explicit_code_title",
        failed_explicit,
    )
    monkeypatch.setattr(
        law_search,
        "_leg_document_scope",
        forbidden_scope,
    )

    with pytest.raises(law_search.LawSearchUnavailable) as exc_info:
        await law_search._search_impl(
            "detroit",
            normalize_query(
                "статья 3.4 Чрезвычайное и военное положение"
            ),
            use_vector=False,
        )

    assert exc_info.value.code == expected_code


@pytest.mark.asyncio
async def test_spoken_code_title_rescue_blocks_vector_boost_and_beats_correlated_noise(
    monkeypatch,
):
    nq = normalize_query("тридцать пять Уполномоченные органы")
    correct_id = 83535
    correlated_noise_id = 83001
    trgm_noise_id = 83002
    captured = {}

    async def rescue(server_id, code, title):
        captured["rescue"] = (server_id, code, title)
        return [correct_id]

    async def fts(*_args, **_kwargs):
        return [(correct_id, 1.0)]

    async def trgm(*_args, **_kwargs):
        return [(trgm_noise_id, 0.9)]

    async def empty_ids(*_args, **_kwargs):
        return []

    async def synonyms(*_args, **_kwargs):
        return [(correlated_noise_id, 1.0)]

    async def vector_leg(*_args, **_kwargs):
        return law_search._LegOutcome(
            "ok",
            ids=[correlated_noise_id],
            usable=True,
        )

    async def cards(ids):
        return {
            article_id: {
                "id": article_id,
                "code": "35" if article_id == correct_id else "99",
                "kind": "article",
                "title": (
                    "Уполномоченные органы"
                    if article_id == correct_id
                    else "Посторонняя норма"
                ),
                "doc_title": "Закон",
                "article_ref": None,
                "penalty_summary": None,
                "full_text": "Текст нормы.",
            }
            for article_id in ids
        }

    async def related(_article_id):
        return False, []

    async def no_evidence(*_args, **_kwargs):
        return "empty", {}

    monkeypatch.setattr(config, "LAW_CATALOG_EMBED_ENABLED", True)
    monkeypatch.setattr(law_search, "_leg_code_title_rescue", rescue)
    monkeypatch.setattr(law_search, "_leg_fts", fts)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", synonyms)
    monkeypatch.setattr(law_search, "_leg_trgm", trgm)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)
    monkeypatch.setattr(law_search, "_leg_vector_local", vector_leg)
    monkeypatch.setattr(law_search, "_fetch_cards", cards)
    monkeypatch.setattr(law_search, "_fetch_related", related)
    monkeypatch.setattr(
        law_search,
        "_ranking_evidence_with_timeout",
        no_evidence,
    )

    diagnostics = {}
    response = await law_search._search_impl(
        "chicago",
        nq,
        use_vector=True,
        diagnostics=diagnostics,
    )

    assert captured["rescue"] == (
        "chicago",
        "35",
        "уполномоченные органы",
    )
    assert response["results"][0]["id"] == correct_id
    assert response["results"][0]["sources"] == [
        "code_title_rescue",
        "fts",
    ]
    assert diagnostics["legs"]["code_title_rescue"]["ids"] == [correct_id]
    assert diagnostics["priority_ids"] == []
    assert diagnostics["weights_profile"] == "default"
    assert diagnostics["effective_weights"]["vector"] == 1.0
    assert response["degraded"] is False
    assert response["warnings"] == []


@pytest.mark.asyncio
async def test_code_title_rescue_timeout_fails_open_when_normal_search_is_healthy(
    monkeypatch,
):
    nq = normalize_query("35 Уполномоченные органы")
    baseline_id = 83001

    async def timed_out_rescue(*_args, **_kwargs):
        raise asyncio.TimeoutError

    async def fts(*_args, **_kwargs):
        return [(baseline_id, 0.9)]

    async def empty_ids(*_args, **_kwargs):
        return []

    async def empty_scored(*_args, **_kwargs):
        return []

    async def cards(ids):
        assert ids == [baseline_id]
        return {
            baseline_id: {
                "id": baseline_id,
                "code": "99",
                "kind": "article",
                "title": "Базовый результат",
                "doc_title": "Закон",
                "article_ref": None,
                "penalty_summary": None,
                "full_text": "Текст нормы.",
            }
        }

    async def related(_article_id):
        return False, []

    monkeypatch.setattr(
        law_search,
        "_leg_code_title_rescue",
        timed_out_rescue,
    )
    monkeypatch.setattr(law_search, "_leg_fts", fts)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", empty_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)
    monkeypatch.setattr(law_search, "_fetch_cards", cards)
    monkeypatch.setattr(law_search, "_fetch_related", related)

    diagnostics = {}
    response = await law_search._search_impl(
        "chicago",
        nq,
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert response["results"][0]["id"] == baseline_id
    assert diagnostics["legs"]["code_title_rescue"]["status"] == "timeout"
    assert response["degraded"] is True
    assert response["search_mode"] == "partial"
    assert response["warnings"] == ["code_title_rescue_timeout"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "примерно 10 дней",
        "4 г",
        "четыре г",
        "4 тыс",
        "четыре миллиона",
        "37 м",
        "пять т",
        "~4 г",
        "десять ± два грамма",
    ],
)
async def test_ineligible_quantity_never_schedules_code_title_rescue(
    monkeypatch,
    raw,
):
    nq = normalize_query(raw)
    baseline_id = 83001
    assert nq.weak_leading_code is None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("nonleading quantity must not schedule rescue")

    async def fts(*_args, **_kwargs):
        return [(baseline_id, 0.9)]

    async def empty_ids(*_args, **_kwargs):
        return []

    async def empty_scored(*_args, **_kwargs):
        return []

    async def cards(ids):
        return {
            article_id: {
                "id": article_id,
                "code": "10",
                "kind": "article",
                "title": "Срок",
                "doc_title": "Закон",
                "article_ref": None,
                "penalty_summary": None,
                "full_text": "Срок составляет 10 дней.",
            }
            for article_id in ids
        }

    async def related(_article_id):
        return False, []

    monkeypatch.setattr(law_search, "_leg_code_title_rescue", forbidden)
    monkeypatch.setattr(law_search, "_leg_code", forbidden)
    monkeypatch.setattr(law_search, "_leg_fts", fts)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", empty_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)
    monkeypatch.setattr(law_search, "_fetch_cards", cards)
    monkeypatch.setattr(law_search, "_fetch_related", related)

    diagnostics = {}
    response = await law_search._search_impl(
        "chicago",
        nq,
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert response["results"][0]["id"] == baseline_id
    assert diagnostics["legs"]["code_title_rescue"]["status"] == "skipped"


@pytest.mark.asyncio
async def test_document_scope_is_resolved_before_code_or_kind(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 10017}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)

    assert await law_search._leg_document_scope(
        "detroit",
        "административный кодекс",
        ["администрат", "кодек"],
    ) == [10017]
    assert captured["args"] == (
        "detroit",
        ["администрат", "кодек"],
        "административный кодекс",
        ["%администрат%", "%кодек%"],
    )
    sql = captured["query"]
    assert "dense_rank()" in sql
    assert "keyword_hits DESC" in sql
    assert "title_similarity" in sql
    assert "law_document_id AS id" in sql
    assert "code = ANY" not in sql
    assert "kind =" not in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("qualifier", "keywords"),
    [
        ("конституция", ["конституц"]),
        ("ук", ["уголовн"]),
        ("уголовка", ["уголовн"]),
    ],
)
async def test_document_scope_uses_similarity_for_full_words_and_aliases(
    monkeypatch,
    qualifier,
    keywords,
):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return []

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)

    assert await law_search._leg_document_scope(
        "new_york",
        qualifier,
        keywords,
    ) == []
    sql = captured["query"]
    assert "keyword_hits DESC" in sql
    assert "title_similarity DESC" in sql
    assert "$5" not in sql


@pytest.mark.parametrize(
    ("qualifier", "expected"),
    [
        ("уголовного кодекса", ["уголовн", "кодек"]),
        ("уголовному кодексу", ["уголовн", "кодек"]),
        ("административного кодекса", ["административн", "кодек"]),
        ("дорожного кодекса", ["дорожн", "кодек"]),
    ],
)
def test_document_keywords_strip_adjective_genitive_and_dative_endings(
    qualifier,
    expected,
):
    assert law_search._doc_keywords(qualifier) == expected


@pytest.mark.asyncio
async def test_authoritative_document_scope_requires_full_coverage_or_similarity(
    monkeypatch,
):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 10017}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    keywords = law_search._doc_keywords("административного кодекса")
    assert keywords == ["административн", "кодек"]

    assert await law_search._leg_document_scope_authoritative(
        "detroit",
        [10017],
        "административного кодекса",
        keywords,
    ) == [10017]
    assert captured["args"] == (
        "detroit",
        [10017],
        keywords,
        "административного кодекса",
        law_search._DOC_SCOPE_TITLE_SIMILARITY,
    )
    sql = captured["query"]
    assert "id = ANY($2::int[])" in sql
    assert "NOT EXISTS" in sql
    assert "NOT LIKE '%' || keyword || '%'" in sql
    assert "similarity(lower(coalesce(title, '')), $4) >= $5" in sql


@pytest.mark.asyncio
async def test_code_doc_leg_filters_explicit_kind_before_limit(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 82334}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)

    assert await law_search._leg_code_doc_query(
        "detroit",
        ["1"],
        [10017],
        requested_kind="article",
    ) == [82334]
    assert captured["args"][-1] == "article"
    sql = captured["query"]
    assert "law_document_id = ANY($4::int[])" in sql
    assert "AND ($5::text IS NULL OR kind = $5)" in sql
    assert sql.index("kind = $5") < sql.index("LIMIT 20")
    assert "law_document_id" in sql
    assert "source_order" in sql


@pytest.mark.asyncio
async def test_code_fallback_keeps_requested_kind(monkeypatch):
    calls = []

    async def fake_query(
        server_id,
        codes,
        *,
        requested_kind=None,
    ):
        calls.append((server_id, list(codes), requested_kind))
        return [] if len(calls) == 1 else [77]

    monkeypatch.setattr(law_search, "_leg_code_query", fake_query)

    assert await law_search._leg_code(
        "detroit",
        ["10.15"],
        fallback_codes=["10", "15"],
        requested_kind="article",
    ) == [77]
    assert calls == [
        ("detroit", ["10.15"], "article"),
        ("detroit", ["10", "15"], "article"),
    ]


@pytest.mark.asyncio
async def test_code_doc_fallback_keeps_document_scope_and_requested_kind(monkeypatch):
    calls = []

    async def fake_query(
        server_id,
        codes,
        document_ids,
        *,
        requested_kind=None,
    ):
        calls.append(
            (server_id, list(codes), list(document_ids), requested_kind)
        )
        return [] if len(calls) == 1 else [77]

    monkeypatch.setattr(law_search, "_leg_code_doc_query", fake_query)

    assert await law_search._leg_code_doc(
        "detroit",
        ["10.15"],
        "уголовный кодекс",
        ["уголовн", "кодек"],
        fallback_codes=["10", "15"],
        requested_kind="article",
        document_ids=[10082],
    ) == [77]
    assert calls == [
        ("detroit", ["10.15"], [10082], "article"),
        ("detroit", ["10", "15"], [10082], "article"),
    ]


@pytest.mark.asyncio
async def test_explicit_article_code_doc_path_returns_article_card(monkeypatch):
    captured = {}

    async def fake_document_scope(server_id, qualifier, keywords):
        captured.update(
            scope_server_id=server_id,
            scope_qualifier=qualifier,
            scope_keywords=list(keywords),
        )
        return [10017]

    async def fake_code_doc(
        server_id,
        codes,
        qualifier,
        keywords,
        fallback_codes=None,
        *,
        requested_kind=None,
        document_ids=None,
    ):
        captured.update(
            server_id=server_id,
            codes=list(codes),
            qualifier=qualifier,
            keywords=list(keywords),
            fallback_codes=list(fallback_codes or ()),
            requested_kind=requested_kind,
            document_ids=list(document_ids or ()),
        )
        return [82334]

    async def fake_authoritative_scope(
        server_id,
        document_ids,
        qualifier,
        keywords,
    ):
        captured.update(
            authoritative_server_id=server_id,
            authoritative_document_ids=list(document_ids),
            authoritative_qualifier=qualifier,
            authoritative_keywords=list(keywords),
        )
        return list(document_ids)

    async def fake_cards(ids):
        assert ids == [82334]
        return (
            "ok",
            {
                82334: {
                    "id": 82334,
                    "kind": "article",
                    "code": "1",
                    "title": "Задачи административного законодательства",
                    "doc_title": "Административный Кодекс",
                    "full_text": "Статья 1. Задачи законодательства.",
                }
            },
        )

    async def fake_related(article_id):
        assert article_id == 82334
        return "ok", []

    async def empty_explicit(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        law_search,
        "_leg_explicit_code_title",
        empty_explicit,
    )
    monkeypatch.setattr(law_search, "_leg_document_scope", fake_document_scope)
    monkeypatch.setattr(
        law_search,
        "_leg_document_scope_authoritative",
        fake_authoritative_scope,
    )
    monkeypatch.setattr(law_search, "_leg_code_doc", fake_code_doc)
    monkeypatch.setattr(law_search, "_cards_with_timeout", fake_cards)
    monkeypatch.setattr(law_search, "_related_with_timeout", fake_related)

    response = await law_search._search_impl(
        "detroit",
        normalize_query("статья 1 Административный Кодекс"),
        use_vector=False,
    )

    assert captured["requested_kind"] == "article"
    assert captured["document_ids"] == [10017]
    assert captured["scope_qualifier"] == "административный кодекс"
    assert captured["scope_keywords"] == ["административн", "кодек"]
    assert captured["authoritative_document_ids"] == [10017]
    assert captured["authoritative_qualifier"] == "административный кодекс"
    assert response["results"][0]["id"] == 82334
    assert response["results"][0]["kind"] == "article"


@pytest.mark.asyncio
async def test_generic_structure_code_uses_clean_document_scope(monkeypatch):
    captured = {}

    async def document_scope(server_id, qualifier, keywords):
        captured["scope"] = (server_id, qualifier, list(keywords))
        return [10082]

    async def authoritative_scope(
        server_id,
        document_ids,
        qualifier,
        keywords,
    ):
        captured["authoritative"] = (
            server_id,
            list(document_ids),
            qualifier,
            list(keywords),
        )
        return list(document_ids)

    async def code_doc(
        server_id,
        codes,
        qualifier,
        keywords,
        fallback_codes=None,
        *,
        requested_kind=None,
        document_ids=None,
    ):
        captured["target"] = (
            server_id,
            list(codes),
            qualifier,
            list(keywords),
            requested_kind,
            list(document_ids or []),
        )
        return [82334]

    async def cards(ids):
        return (
            "ok",
            {
                ids[0]: {
                    "id": ids[0],
                    "kind": "clause",
                    "code": "5",
                    "title": "Пункт 5",
                    "doc_title": "Уголовный кодекс",
                    "full_text": "Пункт 5.",
                }
            },
        )

    async def related(_article_id):
        return "empty", []

    monkeypatch.setattr(law_search, "_leg_document_scope", document_scope)
    monkeypatch.setattr(
        law_search,
        "_leg_document_scope_authoritative",
        authoritative_scope,
    )
    monkeypatch.setattr(law_search, "_leg_code_doc", code_doc)
    monkeypatch.setattr(law_search, "_cards_with_timeout", cards)
    monkeypatch.setattr(law_search, "_related_with_timeout", related)

    diagnostics = {}
    response = await law_search._search_impl(
        "detroit",
        normalize_query("пункт 5 УК"),
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert captured["scope"] == ("detroit", "ук", ["уголовн"])
    assert captured["authoritative"] == (
        "detroit",
        [10082],
        "ук",
        ["уголовн"],
    )
    assert captured["target"] == (
        "detroit",
        ["5"],
        "ук",
        ["уголовн"],
        None,
        [10082],
    )
    assert response["results"][0]["id"] == 82334
    assert diagnostics["search_path"] == "code_doc"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "expected_kind"),
    [
        ("статья 999 Административный Кодекс", "article"),
        ("глава 999 Административный Кодекс", "chapter"),
        ("глава 999 административного кодекса", "chapter"),
        ("раздел 999 Административный Кодекс", "section"),
    ],
)
async def test_recognized_document_without_requested_target_never_falls_back_cross_document(
    monkeypatch,
    raw,
    expected_kind,
):
    captured = {}

    async def fake_document_scope(server_id, qualifier, keywords):
        captured["scope"] = (server_id, qualifier, list(keywords))
        return [10017]

    async def fake_code_doc(
        server_id,
        codes,
        qualifier,
        keywords,
        fallback_codes=None,
        *,
        requested_kind=None,
        document_ids=None,
    ):
        captured["target"] = {
            "server_id": server_id,
            "codes": list(codes),
            "qualifier": qualifier,
            "keywords": list(keywords),
            "fallback_codes": list(fallback_codes or ()),
            "requested_kind": requested_kind,
            "document_ids": list(document_ids or ()),
        }
        return []

    async def fake_authoritative_scope(
        server_id,
        document_ids,
        qualifier,
        keywords,
    ):
        captured["authoritative_scope"] = (
            server_id,
            list(document_ids),
            qualifier,
            list(keywords),
        )
        return list(document_ids)

    async def forbidden_global_code(*_args, **_kwargs):
        raise AssertionError("recognized document must not fall back to global code search")

    async def empty_explicit(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        law_search,
        "_leg_explicit_code_title",
        empty_explicit,
    )
    monkeypatch.setattr(law_search, "_leg_document_scope", fake_document_scope)
    monkeypatch.setattr(
        law_search,
        "_leg_document_scope_authoritative",
        fake_authoritative_scope,
    )
    monkeypatch.setattr(law_search, "_leg_code_doc", fake_code_doc)
    monkeypatch.setattr(law_search, "_leg_code", forbidden_global_code)

    diagnostics = {}
    response = await law_search._search_impl(
        "detroit",
        normalize_query(raw),
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert captured["target"]["requested_kind"] == expected_kind
    assert captured["target"]["document_ids"] == [10017]
    assert captured["authoritative_scope"][0:2] == ("detroit", [10017])
    assert response == {
        "query": raw,
        "results": [],
        "related": [],
        "degraded": False,
        "search_mode": "full",
        "warnings": [],
    }
    assert diagnostics["search_path"] == "code_doc_target_missing"
    assert diagnostics["legs"]["code_doc_scope"]["status"] == "ok"
    assert diagnostics["legs"]["code_doc"]["status"] == "empty"
    assert diagnostics["legs"]["code_doc_scope_authoritative"]["status"] == "ok"


@pytest.mark.asyncio
async def test_leading_numeric_shorthand_without_document_type_stays_hybrid(monkeypatch):
    article_id = 82334

    async def forbidden_scope(*_args, **_kwargs):
        raise AssertionError("shorthand without document type must not resolve scope")

    async def code_hit(*_args, **_kwargs):
        return [article_id]

    async def empty_ids(*_args, **_kwargs):
        return []

    async def empty_scored(*_args, **_kwargs):
        return []

    async def cards(ids):
        assert ids == [article_id]
        return (
            "ok",
            {
                article_id: {
                    "id": article_id,
                    "kind": "article",
                    "code": "5",
                    "title": "Разбой",
                    "doc_title": "Уголовный кодекс",
                    "full_text": "Статья 5. Разбой.",
                }
            },
        )

    async def related(_article_id):
        return "empty", []

    monkeypatch.setattr(law_search, "_leg_document_scope", forbidden_scope)
    monkeypatch.setattr(law_search, "_leg_code_doc", forbidden_scope)
    monkeypatch.setattr(law_search, "_leg_code", code_hit)
    monkeypatch.setattr(law_search, "_leg_fts", empty_scored)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", empty_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)
    monkeypatch.setattr(law_search, "_cards_with_timeout", cards)
    monkeypatch.setattr(law_search, "_related_with_timeout", related)

    diagnostics = {}
    response = await law_search._search_impl(
        "detroit",
        normalize_query("5 разбой"),
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert response["results"][0]["id"] == article_id
    assert diagnostics["search_path"] == "hybrid"
    assert "code_doc_scope" not in diagnostics["legs"]
    assert diagnostics["legs"]["code"]["ids"] == [article_id]
    assert diagnostics["weights_profile"] == "ambiguous_code"


@pytest.mark.asyncio
async def test_embedded_situation_number_does_not_run_code_or_document_scope(
    monkeypatch,
):
    article_id = 82334
    nq = normalize_query("у него 99 масок")
    assert nq.codes == []
    assert nq.semantic_numbers == ["99"]
    assert law_search._requested_code_kind(nq) is None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("semantic quantity must not run a structural code path")

    async def empty_ids(*_args, **_kwargs):
        return []

    async def empty_scored(*_args, **_kwargs):
        return []

    async def fts_hit(*_args, **_kwargs):
        return [(article_id, 1.0)]

    async def cards(ids):
        assert ids == [article_id]
        return (
            "ok",
            {
                article_id: {
                    "id": article_id,
                    "kind": "article",
                    "code": "99",
                    "title": "Релевантная норма",
                    "doc_title": "Другой документ",
                    "full_text": "Статья 99. Релевантная норма.",
                }
            },
        )

    async def related(_article_id):
        return "empty", []

    monkeypatch.setattr(law_search, "_leg_document_scope", forbidden)
    monkeypatch.setattr(law_search, "_leg_code_doc", forbidden)
    monkeypatch.setattr(law_search, "_leg_code", forbidden)
    monkeypatch.setattr(law_search, "_leg_fts", fts_hit)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", empty_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)
    monkeypatch.setattr(law_search, "_cards_with_timeout", cards)
    monkeypatch.setattr(law_search, "_related_with_timeout", related)

    diagnostics = {}
    response = await law_search._search_impl(
        "detroit",
        nq,
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert response["results"][0]["id"] == article_id
    assert diagnostics["search_path"] == "hybrid"
    assert "code_doc_scope" not in diagnostics["legs"]
    assert diagnostics["legs"]["code"]["status"] == "skipped"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "статья 38 человек находится в розыске",
        "статья 30 угон транспортного средства",
    ],
)
async def test_explicit_kind_partial_document_title_hit_preserves_hybrid(
    monkeypatch,
    raw,
):
    captured = {}

    async def partial_scope(*_args, **_kwargs):
        return [13200]

    async def forbidden_code_doc(*_args, **_kwargs):
        raise AssertionError("partial document scope must not run scoped code lookup")

    async def authoritative_scope(
        server_id,
        document_ids,
        qualifier,
        keywords,
    ):
        captured["authoritative_scope"] = (
            server_id,
            list(document_ids),
            qualifier,
            list(keywords),
        )
        return []

    async def empty_ids(*_args, **_kwargs):
        return []

    async def empty_scored(*_args, **_kwargs):
        return []

    monkeypatch.setattr(law_search, "_leg_document_scope", partial_scope)
    monkeypatch.setattr(
        law_search,
        "_leg_document_scope_authoritative",
        authoritative_scope,
    )
    monkeypatch.setattr(law_search, "_leg_code_doc", forbidden_code_doc)
    monkeypatch.setattr(law_search, "_leg_code", empty_ids)
    monkeypatch.setattr(
        law_search,
        "_leg_explicit_code_title",
        empty_ids,
    )
    monkeypatch.setattr(law_search, "_leg_fts", empty_scored)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", empty_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)

    diagnostics = {}
    response = await law_search._search_impl(
        "detroit",
        normalize_query(raw),
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert captured["authoritative_scope"][0:2] == ("detroit", [13200])
    assert response["results"] == []
    assert response["search_mode"] == "full"
    assert diagnostics["search_path"] == "hybrid"
    assert diagnostics["legs"]["code_doc_scope"]["ids"] == [13200]
    assert diagnostics["legs"]["code_doc"]["status"] == "skipped"
    assert diagnostics["legs"]["code_doc_scope_authoritative"]["status"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.TimeoutError(), RuntimeError("db down")])
async def test_recognized_document_code_lookup_failure_is_fail_closed(
    monkeypatch,
    failure,
):
    async def scope_hit(*_args, **_kwargs):
        return [10017]

    async def authoritative_scope(
        _server_id,
        document_ids,
        _qualifier,
        _keywords,
    ):
        return list(document_ids)

    async def broken_code_doc(*_args, **_kwargs):
        raise failure

    async def forbidden_global_code(*_args, **_kwargs):
        raise AssertionError("failed scoped lookup must not run global code search")

    async def empty_explicit(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        law_search,
        "_leg_explicit_code_title",
        empty_explicit,
    )
    monkeypatch.setattr(law_search, "_leg_document_scope", scope_hit)
    monkeypatch.setattr(
        law_search,
        "_leg_document_scope_authoritative",
        authoritative_scope,
    )
    monkeypatch.setattr(law_search, "_leg_code_doc", broken_code_doc)
    monkeypatch.setattr(law_search, "_leg_code", forbidden_global_code)

    with pytest.raises(law_search.LawSearchUnavailable, match="code_doc_"):
        await law_search._search_impl(
            "detroit",
            normalize_query("статья 1 Административный Кодекс"),
            use_vector=False,
        )


@pytest.mark.asyncio
async def test_semantic_count_bypasses_document_scope_without_degradation(monkeypatch):
    article_id = 82334
    nq = normalize_query("3 раза стрелял")
    assert nq.codes == []
    assert nq.semantic_numbers == ["3"]
    assert law_search._requested_code_kind(nq) is None

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("semantic count must not run structural code lookup")

    async def empty_ids(*_args, **_kwargs):
        return []

    async def empty_scored(*_args, **_kwargs):
        return []

    async def fts_hit(*_args, **_kwargs):
        return [(article_id, 1.0)]

    async def cards(ids):
        assert ids == [article_id]
        return (
            "ok",
            {
                article_id: {
                    "id": article_id,
                    "kind": "article",
                    "code": "3",
                    "title": "Стрельба",
                    "doc_title": "Уголовный кодекс",
                    "full_text": "Статья 3. Стрельба.",
                }
            },
        )

    async def related(_article_id):
        return "empty", []

    monkeypatch.setattr(law_search, "_leg_document_scope", forbidden)
    monkeypatch.setattr(law_search, "_leg_code", forbidden)
    monkeypatch.setattr(law_search, "_leg_code_title_rescue", empty_ids)
    monkeypatch.setattr(law_search, "_leg_fts", fts_hit)
    monkeypatch.setattr(law_search, "_leg_title", empty_ids)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", empty_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty_ids)
    monkeypatch.setattr(law_search, "_cards_with_timeout", cards)
    monkeypatch.setattr(law_search, "_related_with_timeout", related)

    diagnostics = {}
    response = await law_search._search_impl(
        "detroit",
        nq,
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert response["results"][0]["id"] == article_id
    assert response["degraded"] is False
    assert response["search_mode"] == "full"
    assert response["warnings"] == []
    assert diagnostics["search_path"] == "hybrid"
    assert "code_doc_scope" not in diagnostics["legs"]


@pytest.mark.parametrize("raw", [
    "человек находится в розыске",
    "лицо числится в федеральном розыске",
    "гражданин объявлен в розыск",
    "человек в розыске",
    "разыскиваемый человек",
    "человек состоит в розыске",
])
def test_wanted_person_gets_substantive_fulltext_intent(raw):
    nq = normalize_query(raw)
    assert nq.legal_variants == []
    assert nq.intent_variants == [
        "уклонение от отбывания ограничения свободы или лишения свободы",
        "уклонение от отбывания ограничения свободы либо лишения свободы",
        "уклонение лица от отбывания наказания в виде ограничения свободы либо лишения свободы",
        "уклонение лица от отбывания наказания в виде ограничения свободы или лишения свободы",
        "нахождение лица в розыске по данным системы видеонаблюдения",
        "нахождение в розыске по камерам",
    ]


def test_unrelated_person_query_does_not_gain_wanted_intent():
    nq = normalize_query("права человека")
    assert nq.legal_variants == []
    assert nq.intent_variants == []


@pytest.mark.parametrize("raw", [
    "человек не находится в розыске",
    "человек находится без розыска",
    "порядок снятия человека с розыска",
    "пропавший человек находится в розыске",
    "как объявить человека в розыск",
    "объявление человека в розыск",
    "лицензия находится в розыске",
    "права человека находящегося в розыске",
    "можно ли задержать человека который находится в розыске",
    "срок задержания человека находящегося в розыске",
    "федеральный розыск человека",
    "розыск человека",
])
def test_negated_or_procedural_wanted_query_does_not_gain_offence_intent(raw):
    nq = normalize_query(raw)
    assert nq.legal_variants == []
    assert nq.intent_variants == []


# --- Роли в ситуационном запросе ---------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "гражданский стрелял по госнику",
    "гражданин выстрелил в сотрудника полиции",
    "гражданский стрелял по полицейскому",
    "правоохранитель был обстрелян",
    "в госника стрелял гражданский",
    "госник был обстрелян гражданским",
    "госник вчера был убит гражданским",
    "стрельба по сотруднику правоохранительного органа",
    "гражданский открыл огонь по менту",
    "гражданский застрелил сотрудника полиции",
    "гражданский убил госника",
    "гражданский покушался на госника",
    "гражданский стрелял без предупреждения по госнику",
    "какой срок за то что гражданский стрелял по госнику",
    "гражданский стрелял по госнику из лицензированного гражданского оружия",
    "замочил копа",
    "убил копа",
    "замочил полицейского",
    "прикончил копа",
    "стрелял по легавому",
    "убил легавого",
    "стрелял по лспдшнику",
    "стрелял по дпснику",
    "стрелял по фибовце",
    "убил фибовку",
    "убил лспдшницу",
    "убил дпсницу",
    "стрелял по росгвардейце",
])
def test_shooting_at_protected_employee_gets_content_based_intent(raw):
    nq = normalize_query(raw)
    assert nq.intent_variants == list(law_search._PROTECTED_LIFE_INTENT_VARIANTS)
    assert nq.intent_vector_text == "стрельба по сотруднику правоохранительного органа"
    assert nq.evidence_facets == ["государственный", "сотрудник"]


def test_intent_vector_restores_relation_without_changing_lexical_query():
    nq = normalize_query("какая статья если гражданский стрелял по госнику")
    assert nq.cleaned == "гражданский стрелял сотрудник"
    assert nq.intent_vector_text == "стрельба по сотруднику правоохранительного органа"


@pytest.mark.parametrize("raw", [
    "госник стрелял по гражданскому",
    "сотрудник открыл огонь по гражданину",
    "стрельба сотрудника по гражданскому",
    "гражданский не стрелял по госнику",
    "гражданский стрелял не по госнику",
    "лицензия на гражданское оружие",
    "правила владения гражданским оружием",
    "можно ли задержать гражданского который стрелял по госнику",
    "можно ли открыть огонь по госнику",
    "когда можно открыть огонь по госнику",
    "как стрелять по госнику",
    "имеет ли право гражданский стрелять по госнику",
    "порядок расследования стрельбы по госнику",
    "гражданин выстрелил в сотрудника",
    "стрельба по сотруднику",
    "стрелял по сотруднику магазина",
    "стрелял по полицейской машине",
    "стрелял по полицейскому автомобилю",
    "стрелял по полицейскому участку",
    "стрелял по правоохранительному органу",
    "стрелял по машине госника",
    "стрелял рядом с госником",
    "стрелял в воздух при госнике",
    "стрелял в воздух а в госника попал камень",
    "стрелял из оружия госника",
    "стрелял по приказу госника",
    "госник заявил что гражданский был убит",
    "гражданский покушал на госника",
    "гражданский убирал в доме сотрудника",
    "гражданский отказался стрелять по госнику",
    "гражданский планировал выстрелить в госника",
    "гражданский хотел стрелять по госнику",
    "гражданский приказал стрелять по госнику",
    "гражданский угрожал стрелять по госнику",
    "угрожал что выстрелит в копа",
    "пригрозил выстрелом в копа",
    "боялся что гражданин выстрелит в полицейского",
    "слышал угрозу выстрела в копа",
    "предотвратил выстрел в копа",
    "стреляй по копу",
    "открывай огонь по копу",
    "открывайте огонь по копу",
    "копом был убит гражданин",
    "полицейским был убит гражданин",
    "замочил. коп приехал на место",
    "замочил одного. коп приехал",
    "коп замочил гражданского",
    "не замочил копа",
    "хочу замочить копа",
    "замочит копа",
    "замочил полицейскую машину",
    "замочил сотрудника магазина",
    "статья 6.2 замочил копа",
    "стрелял по ЛСПД",
    "стрелял по ДПС",
    "стрелял по Росгвардии",
    "стрелял по легавой собаке",
    "гражданский убирал ментальный блок",
])
def test_reverse_negated_procedural_or_indirect_queries_do_not_pin_victim_intent(raw):
    nq = normalize_query(raw)
    assert nq.intent_variants == []
    assert nq.intent_vector_text is None


def test_directed_harm_facets_require_completed_action_and_protected_target():
    nq = normalize_query("угрожал расправой полицейскому")
    assert nq.intent_variants == []
    assert nq.evidence_facets == ["государственный", "сотрудник"]

    for raw in (
        "полицейский расследует убийство",
        "стрелял в воздух рядом с полицейским",
        "гражданский убирал рядом с полицейским",
        "коп угрожал гражданину",
        "не угрожал расправой полицейскому",
    ):
        assert normalize_query(raw).evidence_facets == []


@pytest.mark.parametrize(
    "raw",
    [
        "2 бандита замочили копа",
        "3 преступника застрелили полицейского",
        "2 неизвестных стреляли по копу",
        "10 нападавших открыли огонь по госнику",
    ],
)
def test_ambiguous_actor_count_does_not_disable_protected_life_intent(raw):
    nq = normalize_query(raw)

    assert nq.intent_variants == list(
        law_search._PROTECTED_LIFE_INTENT_VARIANTS
    )
    assert nq.evidence_facets == ["государственный", "сотрудник"]


@pytest.mark.parametrize(
    "raw",
    [
        "вынес хату",
        "вынесли всю квартиру",
        "вынесли квартиру подчистую",
        "вынесли квартиру за ночь",
        "обчистил чужую квартиру",
        "обчистил дом",
    ],
)
def test_completed_dwelling_theft_gets_ordered_legal_variants(raw):
    nq = normalize_query(raw)
    assert nq.legal_variants == list(
        law_search._DWELLING_THEFT_LEGAL_VARIANTS
    )


@pytest.mark.parametrize(
    "raw",
    [
        "суд вынес решение",
        "прокурор вынес постановление",
        "вынес мусор из хаты",
        "вынес мебель из квартиры",
        "не вынес хату",
        "взломал дом чтобы проникнуть",
        "обнес дом забором",
        "пристав вынес квартиру на торги",
        "суд вынес квартиру на продажу",
        "он вынес квартиру из залога",
        "вынес. квартиру выставили на продажу",
        "вынесли квартиру по решению суда",
        "вынес квартиру по исполнительному листу",
        "вынесли квартиру за долги",
        "вынес квартиру на реализацию",
        "вынесли квартиру из реестра",
        "вынесли квартиру из описи",
        "вынесли квартиру на баланс банка",
        "вынесли квартиру в натуре",
        "вынесли квартиру в отдельный лот",
        "обчистил дом культуры",
        "обчистил дом престарелых",
        "обчистил свой дом",
        "вынес свою квартиру",
    ],
)
def test_non_theft_or_distinct_break_in_does_not_get_dwelling_theft_variants(raw):
    assert normalize_query(raw).legal_variants == []


def test_ambiguous_obnes_does_not_inject_theft_into_retrieval_surface():
    nq = normalize_query("обнес дом забором")

    assert "кража" not in nq.cleaned
    assert "обнес" in nq.cleaned


# --- Слияние RRF --------------------------------------------------------------------------------

def test_fuse_prefers_multi_leg_hits():
    legs = {
        "fts": [1, 2, 3],
        "trgm": [2, 4],
        "synonym": [2],
    }
    fused = _fuse(legs)
    assert fused[0][0] == 2  # нашли три ноги — должен быть первым
    assert set(fused[0][2]) == {"fts", "trgm", "synonym"}


def test_fuse_code_leg_outweighs_single_fts():
    legs = {"code": [10], "fts": [20]}
    fused = _fuse(legs)
    assert fused[0][0] == 10  # вес code-ноги выше


def test_fuse_empty():
    assert _fuse({"fts": []}) == []


def test_applicability_rerank_is_stable_and_source_backed():
    fused = [
        (1, 0.06, ["fts"]),
        (2, 0.05, ["vector"]),
        (3, 0.04, ["synonym"]),
    ]
    evidence = {
        1: {"verdict": "mismatch"},
        2: {"verdict": "match"},
        3: {"verdict": "unknown"},
    }
    lexical = {
        1: {
            "matched_terms": ["наркотик", "грамм"],
            "direct_terms": ["наркотик", "грамм"],
        },
        2: {
            "matched_terms": ["наркотик", "грамм"],
            "direct_terms": ["наркотик", "грамм"],
        },
        3: {
            "matched_terms": ["наркотик"],
            "direct_terms": ["наркотик"],
        },
    }

    assert [
        row[0]
        for row in law_search._prioritize_applicability(
            fused,
            evidence,
            lexical,
        )
    ] == [2, 1, 3]


def test_applicability_rerank_without_match_keeps_rrf_order():
    fused = [
        (1, 0.06, ["fts"]),
        (2, 0.05, ["vector"]),
    ]

    assert law_search._prioritize_applicability(
        fused,
        {
            1: {"verdict": "unknown"},
            2: {"verdict": "mismatch"},
        },
    ) == fused


def test_applicability_does_not_cross_unknown_or_unrelated_topic():
    fused = [
        (1, 0.06, ["vector"]),
        (2, 0.05, ["fts"]),
        (3, 0.04, ["synonym"]),
    ]
    verdicts = {
        1: {"verdict": "unknown"},
        2: {"verdict": "mismatch"},
        3: {"verdict": "match"},
    }
    lexical = {
        1: {"matched_terms": [], "direct_terms": []},
        2: {"matched_terms": ["оружие"], "direct_terms": ["оружие"]},
        3: {
            "matched_terms": ["наркотик", "грамм"],
            "direct_terms": ["наркотик", "грамм"],
        },
    }

    assert law_search._prioritize_applicability(
        fused,
        verdicts,
        lexical,
    ) == fused


def test_applicability_does_not_use_context_only_topic_cohort():
    fused = [
        (1, 0.06, ["fts"]),
        (2, 0.05, ["vector"]),
    ]
    verdicts = {
        1: {"verdict": "mismatch"},
        2: {"verdict": "match"},
    }
    lexical = {
        1: {
            "matched_terms": ["наркотик", "грамм"],
            "direct_terms": ["наркотик", "грамм"],
        },
        2: {
            "matched_terms": ["наркотик", "грамм", "палето"],
            "direct_terms": [],
        },
    }

    assert law_search._prioritize_applicability(
        fused,
        verdicts,
        lexical,
    ) == fused


def test_fuse_title_leg_beats_synonym_noise():
    # Кейс richman «халатность»: шумные авто-синонимы («халатность с медкартой») ставят
    # чужую статью 65 выше нужной 46 в synonym-ноге, а FTS отрыв RRF сглаживает.
    # БЕЗ title-ноги побеждала бы медкарта (65); с ней — статья «Халатность» (46).
    noisy = {"synonym": [65, 46], "fts": [46, 65]}
    assert _fuse(noisy)[0][0] == 65  # воспроизводим баг: медкарта наверху

    fixed = {"title": [46], "synonym": [65, 46], "fts": [46, 65]}
    top = _fuse(fixed)[0]
    assert top[0] == 46 and "title" in top[2]  # title-нога выносит нужную статью наверх


def test_fuse_exact_synonym_beats_weak_multileg():
    # Кейс «нелегальное оружие»: у 128 точный синоним (фраза целиком), у 5 — рыхлый
    # триграм в synonym + fts. БЕЗ exact-ноги 5 обходит по сумме RRF; С ней — 128 наверху.
    weak = {"synonym": [5, 128], "fts": [5]}
    assert _fuse(weak)[0][0] == 5  # воспроизводим баг

    fixed = {"synonym_exact": [128], "synonym": [5, 128], "fts": [5]}
    top = _fuse(fixed)[0]
    assert top[0] == 128 and "synonym_exact" in top[2]


def test_fuse_weights_override_boosts_vector():
    # Условный буст: при пустых точных ногах вес вектора поднимается — vector-hit выходит вперёд
    legs = {"vector": [100], "fts": [200]}
    from services.law_search_service import _LEG_WEIGHTS
    boosted = _fuse(legs, {**_LEG_WEIGHTS, "vector": 2.0})
    assert boosted[0][0] == 100  # с бустом vector-rank-0 обгоняет fts-rank-0


def test_fuse_preserves_fuzzy_absolute_confidence():
    # Реальный класс бага: singleton fuzzy synonym с sim~0.34 раньше получал полный
    # вес 1.5 и обгонял нормальный FTS. Теперь его вклад пропорционален similarity.
    legs = {"synonym": [10], "fts": [20]}
    fused = _fuse(legs, confidences={"synonym": {10: 0.34}})
    assert fused[0][0] == 20
    assert fused[1][0] == 10


def test_fuse_deduplicates_one_leg_and_breaks_ties_by_article_id():
    single = _fuse({"fts": [20]})
    duplicated = _fuse({"fts": [20, 20]})
    assert duplicated == single
    tied = _fuse({"left": [20], "right": [10]}, {"left": 1.0, "right": 1.0})
    assert [item[0] for item in tied] == [10, 20]


def test_fuse_non_positive_confidence_does_not_vote():
    fused = _fuse(
        {"synonym": [10, 20], "fts": [30]},
        confidences={"synonym": {10: 0.0, 20: -1.0}},
    )
    assert [item[0] for item in fused] == [30]


def test_substantive_intent_outweighs_three_weak_distractor_legs():
    fused = _fuse(
        {
            "intent_phrase": [16],
            "fts": [38],
            "trgm": [38],
            "synonym": [38],
        },
        confidences={"trgm": {38: 0.35}, "synonym": {38: 0.34}},
    )
    assert fused[0][0] == 16


def test_substantive_intent_is_pinned_before_future_multileg_noise():
    fused = _fuse(
        {
            "intent_phrase": [16],
            "title": [38],
            "synonym_exact": [38],
            "synonym": [38],
            "fts": [38],
            "trgm": [38],
            "vector": [38],
        }
    )
    assert fused[0][0] == 38  # будущий шум действительно способен выиграть голоса
    prioritized = law_search._prioritize_ids(fused, [16])
    assert prioritized[0][0] == 16
    assert prioritized[1][0] == 38


def test_substantive_intent_is_kept_when_rrf_rank_is_outside_internal_pool():
    fused = [
        (article_id, 1.0 / article_id, ["fts"])
        for article_id in range(1, 22)
    ]
    prioritized_pool = law_search._prioritize_ids(fused, [21])[
        :law_search._RERANK_POOL_SIZE
    ]

    assert prioritized_pool[0][0] == 21
    assert len(prioritized_pool) == law_search._RERANK_POOL_SIZE


@pytest.mark.asyncio
async def test_search_uses_role_aware_vector_and_pins_confirmed_content_hit(monkeypatch):
    nq = normalize_query("гражданский стрелял по госнику")
    correct_id = 171
    noise_id = 210
    captured = {}

    async def empty(*_args, **_kwargs):
        return []

    async def noisy_fts(*_args, **_kwargs):
        return [(noise_id, 0.34)]

    async def noisy_scored(*_args, **_kwargs):
        return [(noise_id, 0.8)]

    async def intent_phrase(server_id, variants):
        captured["intent"] = (server_id, variants)
        return [correct_id]

    async def vector_leg(server_id, vector_text):
        captured["vector"] = (server_id, vector_text)
        return law_search._LegOutcome(
            "ok", ids=[noise_id], usable=True,
        )

    async def cards(ids):
        return {
            article_id: {
                "id": article_id,
                "code": "17.1" if article_id == correct_id else "21",
                "kind": "article",
                "title": (
                    "Посягательство на жизнь сотрудника"
                    if article_id == correct_id
                    else "Гражданское задержание"
                ),
                "doc_title": "Уголовный кодекс",
                "article_ref": None,
                "penalty_summary": None,
                "full_text": "Текст статьи",
            }
            for article_id in ids
        }

    async def related(_article_id):
        return False, []

    async def ranking_evidence(_server_id, ids, _query_text):
        return "ok", {
            article_id: {
                "matched_terms": ["сотрудник"]
                if article_id == correct_id
                else ["гражданск"],
                "direct_terms": ["сотрудник"]
                if article_id == correct_id
                else ["гражданск"],
                "context_terms": [],
                "synonym_terms": [],
            }
            for article_id in ids
        }

    monkeypatch.setattr(config, "LAW_CATALOG_EMBED_ENABLED", True)
    monkeypatch.setattr(law_search, "_leg_fts", noisy_fts)
    monkeypatch.setattr(law_search, "_leg_title", empty)
    monkeypatch.setattr(law_search, "_leg_synonyms", noisy_scored)
    monkeypatch.setattr(law_search, "_leg_trgm", noisy_scored)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty)
    monkeypatch.setattr(law_search, "_leg_intent_phrase", intent_phrase)
    monkeypatch.setattr(law_search, "_leg_vector_local", vector_leg)
    monkeypatch.setattr(law_search, "_fetch_cards", cards)
    monkeypatch.setattr(law_search, "_fetch_related", related)
    monkeypatch.setattr(
        law_search,
        "_ranking_evidence_with_timeout",
        ranking_evidence,
    )

    diagnostics = {}
    response = await law_search._search_impl(
        "server-any", nq, use_vector=True, diagnostics=diagnostics,
    )

    assert response["results"][0]["id"] == correct_id
    assert response["results"][0]["sources"] == ["intent_phrase"]
    assert captured["intent"] == (
        "server-any", list(law_search._PROTECTED_LIFE_INTENT_VARIANTS),
    )
    assert captured["vector"] == (
        "server-any", "стрельба по сотруднику правоохранительного органа",
    )
    assert diagnostics["search_path"] == "hybrid"
    assert diagnostics["vector_text_kind"] == "intent"
    assert diagnostics["legs"]["intent_phrase"]["ids"] == [correct_id]
    assert diagnostics["priority_ids"] == [correct_id]
    assert diagnostics["weights_profile"] == "vector_boost"
    assert diagnostics["evidence_status"] == "ok"
    assert response["degraded"] is False
    assert response["warnings"] == []


@pytest.mark.asyncio
async def test_evidence_timeout_keeps_rrf_order_and_marks_partial(monkeypatch):
    nq = normalize_query("кража имущества")

    async def empty(*_args, **_kwargs):
        return []

    async def fts(*_args, **_kwargs):
        return [(101, 0.9), (102, 0.8)]

    async def cards(ids):
        return {
            article_id: {
                "id": article_id,
                "code": str(article_id),
                "kind": "article",
                "title": f"Статья {article_id}",
                "doc_title": "Уголовный кодекс",
                "article_ref": None,
                "penalty_summary": None,
                "full_text": "Кража имущества",
            }
            for article_id in ids
        }

    async def related(_article_id):
        return False, []

    async def timed_out_evidence(*_args, **_kwargs):
        return "timeout", {}

    monkeypatch.setattr(law_search, "_leg_fts", fts)
    monkeypatch.setattr(law_search, "_leg_title", empty)
    monkeypatch.setattr(law_search, "_leg_synonyms", empty)
    monkeypatch.setattr(law_search, "_leg_trgm", empty)
    monkeypatch.setattr(law_search, "_leg_synonym_exact", empty)
    monkeypatch.setattr(law_search, "_fetch_cards", cards)
    monkeypatch.setattr(law_search, "_fetch_related", related)
    monkeypatch.setattr(
        law_search,
        "_ranking_evidence_with_timeout",
        timed_out_evidence,
    )

    diagnostics = {}
    response = await law_search._search_impl(
        "server-any",
        nq,
        use_vector=False,
        diagnostics=diagnostics,
    )

    assert [item["id"] for item in response["results"]] == [101, 102]
    assert response["degraded"] is True
    assert response["search_mode"] == "partial"
    assert "evidence_timeout" in response["warnings"]
    assert diagnostics["evidence_status"] == "timeout"


def _vector_state(**overrides):
    values = {
        "total": 100,
        "ready": 100,
        "queued": 0,
        "failed": 0,
        "mismatched": 0,
        "passport_found": True,
        "model_name": config.LAW_CATALOG_EMBED_MODEL_NAME,
        "model_revision": config.LAW_CATALOG_EMBED_MODEL_REVISION,
        "dimensions": config.LAW_CATALOG_EMBED_DIMS,
        "query_prefix": config.LAW_CATALOG_EMBED_QUERY_PREFIX,
        "document_prefix": config.LAW_CATALOG_EMBED_DOC_PREFIX,
        "pooling": config.LAW_CATALOG_EMBED_POOLING,
        "normalized": config.LAW_CATALOG_EMBED_NORMALIZED,
        "recipe_version": config.LAW_CATALOG_EMBED_RECIPE_VERSION,
        "runtime_compatible": True,
    }
    values.update(overrides)
    return law_search._VectorCorpusState(**values)


def test_vector_state_mismatch_is_not_complete():
    assert _vector_state().complete is True
    assert _vector_state(mismatched=1).complete is False


@pytest.mark.asyncio
async def test_incomplete_vector_corpus_never_votes(monkeypatch):
    async def corpus_state(_server_id, _space_id):
        return _vector_state(ready=10, queued=90)

    embed_called = False

    async def embed_query(_text, _cache_key):
        nonlocal embed_called
        embed_called = True
        return "ok", [0.0] * config.LAW_CATALOG_EMBED_DIMS

    monkeypatch.setattr(law_search, "_vector_corpus_state", corpus_state)
    monkeypatch.setattr(law_search, "_embed_frida_query", embed_query)

    outcome = await law_search._leg_vector_local("any-server", "любой запрос")

    assert outcome.status == "skipped"
    assert outcome.usable is False
    assert outcome.ids == []
    assert "vector_corpus_incomplete" in outcome.warnings
    assert embed_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state_overrides", [
    {"ready": 99, "queued": 0},
    {"ready": 99, "failed": 1},
    {"ready": 100, "mismatched": 1},
])
async def test_any_untrustworthy_vector_state_is_gated(monkeypatch, state_overrides):
    async def corpus_state(_server_id, _space_id):
        return _vector_state(**state_overrides)

    async def forbidden_embed(_text, _cache_key):
        raise AssertionError("FRIDA must not run for an incomplete corpus")

    monkeypatch.setattr(law_search, "_vector_corpus_state", corpus_state)
    monkeypatch.setattr(law_search, "_embed_frida_query", forbidden_embed)

    outcome = await law_search._leg_vector_local("server-x", "запрос")
    assert outcome.status == "skipped" and outcome.usable is False


@pytest.mark.asyncio
async def test_vector_rechecks_corpus_after_embedding(monkeypatch):
    states = iter([_vector_state(), _vector_state(ready=99, queued=1)])

    async def corpus_state(_server_id, _space_id):
        return next(states)

    async def embed_query(_text, _cache_key):
        return "ok", [0.0] * config.LAW_CATALOG_EMBED_DIMS

    async def forbidden_fetch(*_args, **_kwargs):
        raise AssertionError("ANN SQL must not run after corpus became incomplete")

    monkeypatch.setattr(law_search, "_vector_corpus_state", corpus_state)
    monkeypatch.setattr(law_search, "_embed_frida_query", embed_query)
    monkeypatch.setattr(law_search, "_db_fetch", forbidden_fetch)

    outcome = await law_search._leg_vector_local("server-x", "запрос")
    assert outcome.status == "skipped" and outcome.usable is False
    assert "vector_corpus_incomplete" in outcome.warnings


@pytest.mark.asyncio
async def test_atomic_vector_guard_reports_late_rebuild_as_degraded(monkeypatch):
    async def corpus_state(_server_id, _space_id):
        return _vector_state()

    async def embed_query(_text, _cache_key):
        return "ok", [0.0] * config.LAW_CATALOG_EMBED_DIMS

    captured = {}

    async def guarded_fetch(query, *_args, **_kwargs):
        captured["query"] = query
        return [{"corpus_ready": False, "id": None}]

    monkeypatch.setattr(law_search, "_vector_corpus_state", corpus_state)
    monkeypatch.setattr(law_search, "_embed_frida_query", embed_query)
    monkeypatch.setattr(law_search, "_db_fetch", guarded_fetch)

    outcome = await law_search._leg_vector_local("server-x", "запрос")

    assert outcome.status == "skipped"
    assert outcome.usable is False and outcome.degraded is True
    assert "vector_corpus_incomplete" in outcome.warnings
    assert "WITH corpus_ready AS" in captured["query"]
    assert "LEFT JOIN LATERAL" in captured["query"]


@pytest.mark.asyncio
async def test_atomic_vector_guard_distinguishes_ready_empty_result(monkeypatch):
    async def corpus_state(_server_id, _space_id):
        return _vector_state()

    async def embed_query(_text, _cache_key):
        return "ok", [0.0] * config.LAW_CATALOG_EMBED_DIMS

    async def ready_but_empty(*_args, **_kwargs):
        return [{"corpus_ready": True, "id": None}]

    monkeypatch.setattr(law_search, "_vector_corpus_state", corpus_state)
    monkeypatch.setattr(law_search, "_embed_frida_query", embed_query)
    monkeypatch.setattr(law_search, "_db_fetch", ready_but_empty)

    outcome = await law_search._leg_vector_local("server-x", "запрос")
    assert outcome.status == "empty"
    assert outcome.usable is True and outcome.degraded is False


@pytest.mark.asyncio
async def test_fuzzy_synonym_returns_real_confidence_and_stable_sql(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [
            {"article_id": 101, "score": 0.91},
            {"article_id": 202, "score": 0.3421},
        ]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    hits = await law_search._leg_synonyms("server-any", "человек находится в розыске")

    assert hits == [(101, 0.91), (202, 0.3421)]
    assert captured["args"][:2] == ("server-any", "человек находится в розыске")
    assert "similarity(term, $2) * weight" in captured["query"]
    assert "THEN 1.0" not in captured["query"]
    assert "ORDER BY score DESC, article_id" in captured["query"]


@pytest.mark.asyncio
async def test_ranking_evidence_uses_source_owned_structural_context(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return []

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    assert await law_search._fetch_ranking_evidence(
        "chicago",
        [83509, 83516],
        "служебные помещения больницы палето",
    ) == {}

    sql = captured["query"]
    assert "candidate_base AS MATERIALIZED" in sql
    assert "document_orientation AS MATERIALIZED" in sql
    assert "candidates AS MATERIALIZED" in sql
    assert sql.count("article.kind NOT IN ('chapter', 'section')") == 2
    assert "FILTER (WHERE structural.kind = 'section')" in sql
    assert "FILTER (WHERE structural.kind = 'chapter')" in sql
    assert "THEN 'section_parent'" in sql
    assert "THEN 'chapter_parent'" in sql
    assert "chapter.source_order > section.source_order" in sql
    assert "section.source_order > chapter.source_order" in sql
    assert "THEN chapter.full_text" not in sql
    assert "THEN section.full_text" not in sql
    assert "SELECT parent.title, parent.full_text" not in sql
    assert (
        "parent.source_order > coalesce(chapter.source_order, -1)"
        not in sql
    )
    assert "candidates.context_orientation" in sql
    assert (
        "term = ANY(tsvector_to_array(candidates.direct_evidence))"
        in sql
    )
    assert (
        "OR term = ANY(tsvector_to_array(candidates.context_evidence))"
        in sql
    )
    assert "synonym_terms" in sql
    assert captured["args"] == (
        "chicago",
        [83509, 83516],
        "служебные помещения больницы палето",
    )


@pytest.mark.asyncio
async def test_relaxed_fts_votes_by_matched_query_coverage(monkeypatch):
    calls = []

    async def fake_fetch(query, *args, **kwargs):
        calls.append((query, args, kwargs))
        if len(calls) == 1:
            return []
        return [
            {"id": 101, "rank": 0.2, "confidence": 2 / 3},
            {"id": 202, "rank": 0.3, "confidence": 1 / 3},
        ]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    hits = await law_search._leg_fts(
        "server-any",
        "гражданский стрелял сотрудник",
        ["гражданский", "стрелял", "сотрудник"],
    )

    assert hits == [(101, pytest.approx(2 / 3)), (202, pytest.approx(1 / 3))]
    relaxed_sql, relaxed_args, _kwargs = calls[1]
    assert relaxed_args[-1] == "гражданский стрелял сотрудник"
    assert "tsvector_to_array(to_tsvector('russian', $7))" in relaxed_sql
    assert "source.term = ANY(tsvector_to_array(a.fts))" in relaxed_sql
    assert "AS confidence" in relaxed_sql
    assert "ORDER BY confidence DESC, rank DESC, a.id" in relaxed_sql


@pytest.mark.asyncio
async def test_strict_fts_hits_keep_full_confidence(monkeypatch):
    async def fake_fetch(*_args, **_kwargs):
        return [{"id": article_id, "rank": 0.5} for article_id in range(1, 6)]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    hits = await law_search._leg_fts(
        "server-any", "точное совпадение", ["точное", "совпадение"],
    )
    assert hits == [(article_id, 1.0) for article_id in range(1, 6)]


@pytest.mark.asyncio
async def test_exact_synonym_uses_full_nonempty_lexeme_set(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return []

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    await law_search._leg_synonym_exact("server-any", "угон транспортного средства")

    sql = captured["query"]
    assert "tsvector_to_array(to_tsvector('russian', term))" in sql
    assert "cardinality(" in sql
    assert "position(term IN $2)" not in sql
    assert "plainto_tsquery('simple', 'без')" in sql
    assert "plainto_tsquery('simple', 'не')" in sql
    assert "phraseto_tsquery('simple', 'без цели')" in sql
    assert "phraseto_tsquery('simple', 'с целью')" in sql


@pytest.mark.asyncio
async def test_intent_phrase_uses_early_fulltext_without_enrichment_dependency(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 777}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    ids = await law_search._leg_intent_phrase(
        "server-any",
        [
            "уклонение от отбывания ограничения свободы или лишения свободы",
            "нахождение лица в розыске по данным системы видеонаблюдения",
        ],
    )

    assert ids == [777]
    assert captured["args"][0] == "server-any"
    sql = captured["query"]
    assert "phraseto_tsquery('russian', raw)" in sql
    assert "a.fts @@ queries.q" in sql
    assert "a.kind = 'article'" in sql
    assert "left(coalesce(a.full_text, ''), 1000)" in sql
    assert "a.penalty_summary" not in sql and "a.penalty_json" not in sql
    assert "SELECT MIN(ord) AS ord FROM matches" in sql
    assert "matches.ord = first_supported.ord" in sql
    assert "a.title" not in sql


@pytest.mark.asyncio
async def test_negated_title_phrase_keeps_literal_stopword(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return [{"id": 36}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    ids = await law_search._leg_phrase_title(
        "server-any", "угон транспортного средства без цели хищения"
    )

    assert ids == [36]
    assert "position($2 IN lower(title)) > 0" in captured["query"]


@pytest.mark.asyncio
async def test_title_fts_preserves_without_purpose_polarity(monkeypatch):
    captured = {}

    async def fake_fetch(query, *args, **kwargs):
        captured.update(query=query, args=args, kwargs=kwargs)
        return []

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    await law_search._leg_title(
        "server-any", "угон транспортного средства без цели хищения"
    )

    assert captured["args"][2:] == (True, False, True, False)
    assert "phraseto_tsquery('simple', 'без цели')" in captured["query"]


@pytest.mark.asyncio
async def test_legal_variant_legs_are_ordered_and_polarity_aware(monkeypatch):
    calls = []

    async def fake_fetch(query, *args, **kwargs):
        calls.append((query, args, kwargs))
        return []

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    variants = [
        "неправомерное завладение автомобилем или иным транспортным средством без цели хищения",
        "неправомерное завладение транспортным средством без цели хищения",
    ]
    await law_search._leg_fts_variants("server-any", variants)
    await law_search._leg_title_variants("server-any", variants)

    for sql, args, _kwargs in calls:
        assert args[:2] == ("server-any", variants)
        assert "SELECT MIN(ord) AS ord FROM matches" in sql
        assert "matches.ord = first_supported.ord" in sql
        assert "phraseto_tsquery('simple', 'без цели')" in sql


@pytest.mark.asyncio
async def test_trigram_leg_returns_absolute_similarity(monkeypatch):
    async def fake_fetch(*_args, **_kwargs):
        return [{"id": 10, "sim": 0.91}, {"id": 20, "sim": 0.31}]

    monkeypatch.setattr(law_search, "_db_fetch", fake_fetch)
    assert await law_search._leg_trgm("server-any", "халатность") == [
        (10, 0.91),
        (20, 0.31),
    ]


@pytest.mark.asyncio
async def test_scored_sql_leg_clamps_confidence():
    async def hits():
        return [(1, 1.7), (2, -0.4)]

    outcome = await law_search._run_scored_sql_leg("scored", hits())
    assert outcome.ids == [1, 2]
    assert outcome.confidences == {1: 1.0, 2: 0.0}


def test_card_recovers_old_metadata_only_title_at_read_time():
    card = law_search._card(
        {
            "id": 123,
            "code": "16.15",
            "kind": "article",
            "title": "от ★ до ★★★★★ [Федеральная/Региональная]",
            "full_text": (
                "Статья 16.15 от ★ до ★★★★★ [Федеральная/Региональная]\n"
                "Уклонение от отбывания ограничения свободы или лишения свободы.\n"
                "Наказание: лишение свободы."
            ),
        },
        1.0,
        ["intent_phrase"],
    )
    assert card["id"] == 123
    assert card["title"] == \
        "Уклонение от отбывания ограничения свободы или лишения свободы"


def test_spoken_singular_quantity_is_not_an_inverted_article_reference():
    nq = normalize_query("одна статья регулирует порядок")

    assert nq.codes == []
    assert nq.semantic_numbers == ["1"]
    assert law_search._requested_code_kind(nq) is None


def test_literal_count_survives_slang_expansion_without_weak_code_signal():
    nq = normalize_query("2 бандита ограбили банк")

    assert nq.codes == []
    assert nq.semantic_numbers == ["2"]
    assert nq.ambiguous_code_signal is False


@pytest.mark.parametrize(
    "raw",
    [
        "1 раз",
        "2 раза",
        "5 раз",
        "2 суток",
        "5 грамм наркотиков",
        "5 килограмм наркотиков",
        "5 миллиграмм наркотиков",
        "5 руб",
        "2 уровня",
        "5 уровней",
        "2 патрона",
        "5 патронов",
        "2 человека",
        "5 человек",
    ],
)
def test_agreed_or_common_count_forms_remain_semantic_only(raw):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers
    assert nq.weak_leading_code is None
    assert nq.weak_title_remainder == ""


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("7 часовой отгул", "7"),
        ("10 голосование после обсуждения поправки", "10"),
        ("6 частота проверок", "6"),
        ("29 летальное оружие USSS", "29"),
        ("9 патроны и условие", "9"),
        ("2 уровни секретности", "2"),
        ("20 звезда героя", "20"),
        ("15 день выборов", "15"),
        ("23 неделимые вещи", "23"),
        ("26 деньги", "26"),
        ("2 человек его права", "2"),
    ],
)
def test_same_prefix_title_words_keep_weak_code_signal(raw, code):
    nq = normalize_query(raw)

    assert nq.codes == [code]
    assert nq.ambiguous_code_signal is True


def test_dotted_leading_code_precedes_generic_title_count_heuristics():
    nq = normalize_query("2.2.1 Остров Кайо-Перико закрыт")

    assert nq.codes == ["2.2.1"]
    assert nq.semantic_numbers == []
    assert normalize_query("20.5 грамма вещества").codes == []


@pytest.mark.parametrize(
    ("raw", "code", "title"),
    [
        (
            "3 Соблюдение прав и свобод граждан",
            "3",
            "соблюдение прав и свобод граждан",
        ),
        (
            "4 Общие полномочия государственных органов",
            "4",
            "общие полномочия государственных органов",
        ),
        (
            "35 Уполномоченные органы",
            "35",
            "уполномоченные органы",
        ),
        (
            "79 Приговор или решение суда",
            "79",
            "приговор или решение суда",
        ),
        (
            "три Соблюдение прав и свобод граждан",
            "3",
            "соблюдение прав и свобод граждан",
        ),
        (
            "тридцать пять Уполномоченные органы",
            "35",
            "уполномоченные органы",
        ),
        (
            "семьдесят девять Приговор или решение суда",
            "79",
            "приговор или решение суда",
        ),
    ],
)
def test_suppressed_integer_title_exposes_catalog_rescue_candidate(
    raw,
    code,
    title,
):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.weak_leading_code == code
    assert nq.weak_title_remainder == title


@pytest.mark.parametrize(
    "raw",
    [
        "примерно 10 дней",
        "примерно пять дней",
        "не пять дней",
        "статья 3 соблюдение прав",
        "статья три соблюдение прав",
    ],
)
def test_nonleading_or_explicit_number_never_requests_catalog_rescue(raw):
    nq = normalize_query(raw)

    assert nq.weak_leading_code is None
    assert nq.weak_title_remainder == ""


@pytest.mark.parametrize(
    "raw",
    [
        "4 г",
        "четыре г",
        "5 грамм наркотиков",
        "2 часа",
        "пять грамм наркотиков",
        "два часа",
        "10 тысяч долларов",
        "4 тыс",
        "4 тысячи",
        "4 миллиона",
        "4 миллиарда",
        "4 млн",
        "4 млрд",
        "4 k",
        "четыре тыс",
        "четыре тысячи",
        "четыре миллиона",
        "четыре миллиарда",
        "37 м",
        "тридцать семь м",
        "5 т",
        "пять т",
        "7 л",
        "семь л",
        "5 р",
        "пять р",
        "37м",
        "5т",
        "7л",
        "5р",
        "5 тонн",
        "5 литров",
        "5 сантиметров",
        "5 миллилитров",
    ],
)
def test_typed_leading_quantity_never_requests_catalog_rescue(raw):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers
    assert nq.weak_leading_code is None
    assert nq.weak_title_remainder == ""


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("статья 4 тысячи", "4"),
        ("статья четыре миллиона", "4"),
        ("статья 4 к", "4"),
        ("статья 37 м", "37"),
        ("статья тридцать семь м", "37"),
        ("статья 5 т", "5"),
        ("статья 7 л", "7"),
        ("статья 5 р", "5"),
        ("статья 37м", "37"),
    ],
)
def test_explicit_article_marker_still_overrides_scale_quantity_guard(
    raw,
    code,
):
    nq = normalize_query(raw)

    assert nq.codes == [code]
    assert nq.weak_leading_code is None


def test_cyrillic_k_preposition_in_real_title_is_not_a_scale():
    digit = normalize_query("4 К полномочиям органа относится контроль")
    spoken = normalize_query("четыре К полномочиям органа относится контроль")

    assert digit.codes == ["4"]
    assert digit.semantic_numbers == ["4"]
    assert spoken.codes == []
    assert spoken.weak_leading_code == "4"
    assert spoken.weak_title_remainder == (
        "к полномочиям органа относится контроль"
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("пять кража", "5"),
        ("шестнадцать задержание подозреваемого", "16"),
        ("двадцать девять получение лицензии", "29"),
    ],
)
def test_spoken_article_title_shorthand_uses_only_catalog_rescue(raw, code):
    nq = normalize_query(raw)

    assert nq.codes == []
    assert nq.semantic_numbers == [code]
    assert nq.weak_leading_code == code
    assert nq.weak_title_remainder
    assert law_search._requested_code_kind(nq) is None


@pytest.mark.parametrize(
    "raw",
    [
        "~10 грамм",
        ">10 грамм",
        "10 ± 2 грамм",
        "10+ грамм",
        "10-15 грамм",
        "10...15 грамм",
        "10/2 грамм",
        "10 и 15 грамм",
        "10 и/или 15 грамм",
        "10 до $20 долларов",
        "10 не более 15 грамм",
        "10 не менее 15 грамм",
        "10 более 15 грамм",
        "10 менее 15 грамм",
        "10 выше 15 грамм",
        "10 ниже 15 грамм",
        "10 свыше 15 грамм",
        "~десять грамм",
        "десять ± два грамма",
        "десять-пятнадцать грамм",
        "десять плюс минус два грамма",
        "десять или пятнадцать грамм",
        "десять до двадцати долларов",
        "десять не более пятнадцати грамм",
        "десять не менее пятнадцати грамм",
        "десять более пятнадцати грамм",
        "десять от трех до пяти грамм",
    ],
)
def test_leading_nonpoint_expression_never_requests_catalog_rescue(raw):
    nq = normalize_query(raw)

    assert nq.weak_leading_code is None
    assert nq.weak_title_remainder == ""


def test_catalog_rescue_guard_ignores_title_punctuation_and_later_ranges():
    dash_title = normalize_query("35 - Уполномоченные органы")
    later_range = normalize_query(
        "четыре Заседания проводятся с 14:00 до 23:59"
    )

    assert dash_title.weak_leading_code == "35"
    assert dash_title.weak_title_remainder == "- уполномоченные органы"
    assert later_range.weak_leading_code == "4"
    assert later_range.weak_title_remainder == (
        "заседания проводятся с 14:00 до 23:59"
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("5 кража", "5"),
        ("16 задержание подозреваемого", "16"),
        ("29 получение лицензии", "29"),
        ("7 правила владения оружием", "7"),
        ("62 задержание транспортного средства", "62"),
    ],
)
def test_leading_article_title_shorthand_keeps_weak_code_signal(raw, code):
    nq = normalize_query(raw)

    assert nq.codes == [code]
    assert nq.semantic_numbers == [code]
    assert nq.ambiguous_code_signal is True


@pytest.mark.parametrize(
    "raw",
    [
        "замочил, коп приехал на место",
        "не факт, что гражданин убил копа",
    ],
)
def test_separate_or_uncertain_clauses_do_not_hard_pin_protected_attack(raw):
    nq = normalize_query(raw)

    assert nq.intent_kind is None
    assert nq.intent_vector_text is None


@pytest.mark.parametrize(
    "raw",
    [
        "стрельба по копу планируется",
        "планируется стрельба по копу",
        "стрельба по копу предотвращена",
        "стрельба по копу была предотвращена",
        "возможна стрельба по копу",
        "вероятна стрельба по копу",
        "предполагается стрельба по копу",
        "намечается стрельба по копу",
        "может произойти стрельба по копу",
        "будет стрельба по копу",
        "риск стрельбы отсутствует, возможна стрельба по копу",
        "угроза стрельба по копу",
        "имитация стрельба по копу",
        "учебная стрельба по копу",
        "стрельба по копу обсуждается",
        "стрельба по копу не подтверждена",
        "стрельба по копу могла произойти",
        "стрельба по копу якобы произошла",
        "тренировка стрельба по копу",
        "репетиция стрельба по копу",
        "расследуется стрельба по копу",
    ],
)
def test_noncompleted_shooting_event_noun_does_not_hard_pin(raw):
    assert normalize_query(raw).intent_kind is None


@pytest.mark.parametrize(
    "raw",
    [
        "стрельба по копу",
        "произошла стрельба по копу",
        "случилась стрельба по копу",
        "зафиксирована стрельба по копу",
    ],
)
def test_asserted_shooting_event_noun_keeps_protected_attack_intent(raw):
    assert normalize_query(raw).intent_kind == "protected_life_attack"


@pytest.mark.parametrize(
    "raw",
    [
        "подозреваемый убил копа",
        "ученик стрелял по копу",
        "тренированный стрелок выстрелил в копа",
        "сомневался, но потом убил копа",
        "стрельбу планировали, но затем он стрелял по копу",
        "расследование установило что гражданин убил копа",
        "после угрозы преступник убил копа",
        "учения закончились, потом гражданин стрелял по копу",
        "гражданский выстрелил в копа, затем обсуждал случившееся",
        "гражданский выстрелил в копа, но потом отрицал это",
    ],
)
def test_asserted_action_clause_is_not_suppressed_by_other_context(raw):
    assert normalize_query(raw).intent_kind == "protected_life_attack"


@pytest.mark.parametrize(
    "raw",
    [
        "якобы убил копа",
        "возможно убил копа",
        "предположительно убил копа",
        "отрицает что убил копа",
        "неизвестно убил ли копа",
        "сомнительно что гражданский стрелял по копу",
        "вряд ли гражданский стрелял по копу",
        "маловероятно что гражданский стрелял по копу",
        "по слухам гражданский стрелял по копу",
        "стрельба по копу опровергнута",
        "стрельба по копу не исключена",
        "план: стрельба по копу",
        "угроза: стрельба по копу",
        "учения: стрельба по копу",
        "предотвращено: стрельба по копу",
        "суд не установил что гражданин убил копа",
        "не было доказано что гражданин убил копа",
        "не удалось доказать что гражданин убил копа",
        "не смогли доказать что гражданин убил копа",
        "нет доказательств что гражданин убил копа",
        "не было подтверждено что гражданин убил копа",
    ],
)
def test_uncertain_finite_harm_does_not_hard_pin(raw):
    assert normalize_query(raw).intent_kind is None


@pytest.mark.parametrize(
    "raw",
    [
        "гражданин убил копа по слухам",
        "гражданин убил копа — это опровергнуто",
        "гражданин убил копа, но это не подтверждено",
        "гражданин стрелял по копу, хотя событие не доказано",
        "что гражданин убил копа, не доказано",
        "гражданин убил копа не подтверждено",
        "утверждение что гражданин убил копа опровергнуто",
        "гражданин убил копа или нет",
        "гражданин убил копа, возможно",
        "гражданин убил копа, вероятно",
        "свидетели не подтвердили что гражданин убил копа",
        "утверждение что гражданин убил копа ложно",
        "сообщение что гражданин убил копа оказалось ложным",
        "версия что гражданин убил копа не соответствует действительности",
        "неверно что гражданин убил копа",
        "ошибочно полагать что гражданин убил копа",
        "нет оснований считать что гражданин убил копа",
        "отсутствуют доказательства что гражданин убил копа",
        "гражданин убил копа, однако доказательств нет",
        "гражданин убил копа, но оснований так считать нет",
        "суд установил не то, что гражданин убил копа, а что он был свидетелем",
        "суд установил не то что гражданин убил копа а что он был свидетелем",
    ],
)
def test_clause_scoped_disproof_never_hard_pins_attack(raw):
    assert normalize_query(raw).intent_kind is None


@pytest.mark.parametrize(
    "raw",
    [
        "гражданин убил копа, после чего скрылся",
        "гражданин убил копа, а по слухам его брат скрылся",
        "гражданин убил копа, после чего по слухам уехал",
        "гражданин убил копа, но это не подтверждает обвинение в ограблении",
        "гражданин убил копа, но это не доказало его причастность к краже",
        "гражданин убил копа, но потом отрицал это",
        "гражданин убил копа, возможно скрылся",
        "следствие подтвердило что гражданин убил копа",
        "свидетели подтвердили что гражданин убил копа",
        "официально доказано что гражданин убил копа",
        "было установлено что гражданин убил копа",
        "было зафиксировано что гражданин убил копа",
    ],
)
def test_unrelated_suffix_or_positive_proof_keeps_asserted_attack(raw):
    assert normalize_query(raw).intent_kind == "protected_life_attack"


@pytest.mark.parametrize(
    "raw",
    [
        "якобы следствие в ходе проверки доказало что гражданин убил копа",
        "по слухам следствие официально и публично доказало что гражданин убил копа",
        "предположительно суд после долгой проверки установил что гражданин убил копа",
    ],
)
def test_clause_wide_modal_cue_is_not_overridden_by_late_proof_word(raw):
    assert normalize_query(raw).intent_kind is None


@pytest.mark.parametrize(
    "raw",
    [
        "гражданский с законным оружием стрелял по копу",
        "гражданин законно купил оружие и стрелял по копу",
        "гражданский стрелял по копу, законность оружия подтверждена",
        "гражданин имел право на оружие но убил копа",
        "законный владелец оружия убил копа",
        "получил разрешение на оружие, затем стрелял по копу",
    ],
)
def test_unrelated_legal_context_does_not_suppress_completed_attack(raw):
    assert normalize_query(raw).intent_kind == "protected_life_attack"


@pytest.mark.parametrize(
    "raw",
    [
        "законно ли стрелять по копу",
        "когда можно стрелять по копу",
        "имеет ли право гражданин стрелять по копу",
        "порядок применения оружия по копу",
    ],
)
def test_actual_procedural_question_stays_without_hard_attack(raw):
    assert normalize_query(raw).intent_kind is None


@pytest.mark.parametrize(
    "raw",
    [
        "решение вынес, квартиру продали",
        "приговор вынес, квартиру продали",
    ],
)
def test_comma_separated_legal_action_does_not_become_dwelling_theft(raw):
    assert normalize_query(raw).legal_variants == []


@pytest.mark.parametrize(
    "raw",
    [
        "5 грамм примерно",
        "5 грамм около",
        "5 грамм плюс-минус",
        "по крайней мере 10 грамм",
        "по меньшей мере 10 грамм",
        "в пределах 10 грамм",
        "в районе 10 грамм",
        "10 грамм с половиной",
        "10 грамм максимум",
        "10 грамм минимум",
        "10 грамм и более",
        "10 грамм и/или меньше",
        "10 грамм или более",
        "10 грамм не более",
        "10 грамм не менее",
        "10 грамм с лишним",
        "10 грамм с небольшим",
        "без малого 10 грамм",
        "едва 10 грамм",
        "10 грамм порядка",
        "масса не достигла 10 граммов",
        "вес не достигает 10 граммов",
        "не ровно 10 грамм",
        "точно не 10 грамм",
        "не 10 грамм",
        "10 грамм не точно",
        "не равно 10 грамм",
        "не ровно десять грамм",
    ],
)
def test_postfix_approximate_quantity_disables_point_rerank(raw):
    nq = normalize_query(raw)

    assert nq.semantic_numbers
    assert nq.applicability_query_ambiguous is True


@pytest.mark.parametrize(
    "raw",
    [
        "5 кг - 10 кг",
        "500 г - 1 кг",
        "$5 тыс-$10 тыс",
        "$5 k-$10 k",
        "10 ± 2 грамм",
        "10±2 грамм",
        "5 грамм и 10 грамм",
        "5 кг плюс 10 кг",
    ],
)
def test_typed_ranges_and_repeated_values_disable_point_rerank(raw):
    nq = normalize_query(raw)

    assert nq.applicability_query_ambiguous is True


def test_applicability_rerank_does_not_cross_protected_code_hit():
    fused = [
        (5, 0.1, ["code", "fts"]),
        (99, 0.09, ["fts"]),
    ]
    evidence = {
        5: {"verdict": "mismatch"},
        99: {"verdict": "match"},
    }
    lexical = {
        5: {
            "matched_terms": ["штраф"],
            "direct_terms": ["штраф"],
        },
        99: {
            "matched_terms": ["штраф", "доллар"],
            "direct_terms": ["штраф", "доллар"],
        },
    }

    assert law_search._prioritize_applicability(
        fused,
        evidence,
        lexical,
        protected_ids={5},
    ) == fused

# END OF FILE tests/test_law_search.py
