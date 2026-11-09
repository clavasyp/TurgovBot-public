# START OF FILE tests/test_law_catalog.py
"""
Тесты каталога статей умного поиска: сегментация (split_into_blocks),
нормализация заголовков (normalize_header) и парсер LLM-ответа обогащения.

Включает целевые regression-тесты стабильности публичных article ID.
"""

from contextlib import asynccontextmanager
import json

import pytest

from services.indexing_service import (
    _header_match_plan,
    canonicalize_split_structural_keyword_layout,
    chunk_law_text,
    split_into_blocks,
)
from services import law_catalog_service as catalog
from services.law_catalog_service import (
    normalize_header, _parse_json_array, _title_from_body, _is_junk_title,
    _article_hash, _match_existing_articles,
)
from services.law_title_utils import recover_rating_metadata_title
from services.law_structural_code import canonical_roman_value


def _title_for(ref: str, body: str):
    """Повторяет логику build_catalog_for_document (строки 137-141): заголовок →
    если title пустой ИЛИ служебный маркер → берём название со строки тела."""
    _, _, title = normalize_header(ref)
    if (title is None or _is_junk_title(title)) and ref is not None:
        title = _title_from_body(body)
    return title


# --- normalize_header -------------------------------------------------------------------------

@pytest.mark.parametrize("ref, expected", [
    ("Статья 12.1. Халатность", ("article", "12.1", "Халатность")),
    ("Статья 12.1 Халатность", ("article", "12.1", "Халатность")),
    ("статья №5 — О мерах пресечения", ("article", "5", "О мерах пресечения")),
    ("Ст. 7 - Кража", ("article", "7", "Кража")),
    ("Глава IV. Общие положения", ("chapter", "IV", "Общие положения")),
    ("Раздел 2: Наказания", ("section", "2", "Наказания")),
    ("Пункт 3.2 Порядок задержания", ("clause", "3.2", "Порядок задержания")),
    ("§ 4 Порядок обжалования", ("clause", "4", "Порядок обжалования")),
    ("Статья 15", ("article", "15", None)),
])
def test_normalize_header_variants(ref, expected):
    assert normalize_header(ref) == expected


@pytest.mark.parametrize(
    "ref, expected_code",
    [
        ("Глава l. Общие положения", "I"),
        ("Глава lX. Гарантии", "IX"),
        ("Глава Ⅰ. Общие положения", "I"),
        ("Глава Ⅱ. Полномочия", "II"),
        ("Глава IХ. Казначейство", "IX"),
        ("Глава ХII. Контроль", "XII"),
        ("Глава L. Значение пятьдесят", "L"),
        ("Глава LX. Значение шестьдесят", "LX"),
        ("Глава lx. Значение шестьдесят", "LX"),
        ("Глава li. Значение пятьдесят один", "LI"),
        ("Глава liv. Значение пятьдесят четыре", "LIV"),
    ],
)
def test_normalize_header_canonicalizes_proven_roman_glyph_variants(
    ref,
    expected_code,
):
    kind, code, _title = normalize_header(ref)
    assert kind == "chapter"
    assert code == expected_code


@pytest.mark.parametrize("ref, expected", [
    # Дефисная поднумерация УК/УПК RMRP («Статья 10-1») → канонический код с точкой,
    # чтобы не коллизировать со «Статья 10» и находиться поиском «статья 10-1»/«10.1».
    ("Статья 10-1. Оконченное и неоконченное преступления",
     ("article", "10.1", "Оконченное и неоконченное преступления")),
    ("Статья 86-2. Судимость", ("article", "86.2", "Судимость")),
    ("Статья 22-1", ("article", "22.1", None)),
    # Тире-разделитель после номера (без прижатой цифры) — НЕ поднумерация.
    ("Статья 10 - Название через тире", ("article", "10", "Название через тире")),
])
def test_normalize_header_hyphen_subnumbering(ref, expected):
    assert normalize_header(ref) == expected


def test_normalize_header_empty_and_garbage():
    assert normalize_header(None) == ("other", None, None)
    assert normalize_header("") == ("other", None, None)
    # Нераспознанный заголовок — kind=other, текст уходит в title (для FTS)
    kind, code, title = normalize_header("Приложение А. Таблица штрафов")
    assert kind == "other" and code is None and "Приложение" in title


def test_normalize_header_does_not_treat_mayoral_chapter_text_as_roman_number():
    assert normalize_header("Глава Мэрии округа Блэйн") == (
        "other",
        None,
        "Глава Мэрии округа Блэйн",
    )
    assert normalize_header("Глава М") == ("chapter", "M", None)
    assert normalize_header("Глава IV. Полномочия") == (
        "chapter",
        "IV",
        "Полномочия",
    )
    assert normalize_header("Глава IХ. Казначейство") == (
        "chapter",
        "IX",
        "Казначейство",
    )
    assert normalize_header("Статья 1Работники системы") == (
        "article",
        "1",
        "Работники системы",
    )
    assert normalize_header("Статья. 12.") == ("article", "12", None)


def test_normalize_header_strips_quotes_and_collapses_spaces():
    kind, code, title = normalize_header('Статья 9 «Дорожный  движение»')
    assert (kind, code) == ("article", "9")
    assert title == "Дорожный движение"


# --- _title_from_body (реальный формат форума: название второй строкой) -----------------------

def test_title_from_body_next_line():
    body = "Статья 1.\nСвобода массовой информации​\n​\nч. 1 В штате..."
    assert _title_from_body(body) == "Свобода массовой информации"


def test_title_from_body_skips_zwsp_lines():
    body = "Статья 1\n​\nНациональная гвардия штата и её назначение\nч. 1 ..."
    assert _title_from_body(body) == "Национальная гвардия штата и её назначение"


def test_title_from_body_rejects_prose():
    # Вторая строка — длинное определение (уголовка) → None, название даст LLM
    body = ("Статья 6.1.\n(РЕГИОНАЛЬНЫЙ) Умышленное причинение тяжкого вреда здоровью "
            "опасного для жизни человека ((Состояние нокаута))\n— 6 до 8 лет.")
    assert _title_from_body(body) is None


def test_title_from_body_rejects_body_markers():
    assert _title_from_body("Статья 1.\nч. 1 Настоящий закон...") is None
    assert _title_from_body("Статья 1.\n1. Первый пункт") is None
    assert _title_from_body("Статья 1.\nа) первый подпункт") is None


def test_title_from_body_rejects_split_one_letter_prose():
    body = (
        "Статья 11.\n"
        "В\n"
        "целях государственного контроля запрещается нарушение порядка."
    )
    assert _is_junk_title("В")
    assert _title_from_body(body) is None


def test_title_from_body_strips_one_leading_delimiter():
    body = "Статья 1.\n. Название нормы\nОсновной текст положения."
    assert _title_from_body(body) == "Название нормы"


def test_title_from_body_rejects_inline_classification_disposition():
    body = (
        "Статья 6.1\n"
        "(A, CR) Убийство другого лица наказывается лишением свободы | 5*\n"
        "Дополнительный текст нормы."
    )
    assert _title_from_body(body) is None


def test_title_from_body_strips_trailing_dot():
    assert _title_from_body("Статья 1.\nСфера действия настоящего Закона.\n\nтекст") == \
        "Сфера действия настоящего Закона"


def test_title_from_body_skips_star_bracket_combos():
    # Denver: звёзды розыска и маркер юрисдикции НА ОДНОЙ строке
    body = "Статья 4.1\n★★★★ [Федеральная/Региональная]\nРазбой\nч. 1 ..."
    assert _title_from_body(body) == "Разбой"


@pytest.mark.parametrize("metadata", [
    "от ★ до ★★★★★ [Федеральная/Региональная]",
    "ОТ ★ ДО ★★★★★ (ФЕДЕРАЛЬНЫЙ)",
    "от ** до *****",
])
def test_title_from_body_skips_rating_range_metadata(metadata):
    body = (
        f"Статья 16.15\n{metadata}\n"
        "Уклонение от отбывания ограничения свободы или лишения свободы.\n"
        "Наказание: лишение свободы."
    )
    assert _is_junk_title(metadata)
    assert _title_from_body(body) == \
        "Уклонение от отбывания ограничения свободы или лишения свободы"


def test_title_from_body_skips_symbol_only_lines():
    for junk in ("*", "*.", "**.", "-", "★★★"):
        body = f"Статья 2\n{junk}\nКража\nч. 1 ..."
        assert _title_from_body(body) == "Кража", junk


def test_numeric_header_fragments_are_not_titles():
    assert _is_junk_title("19")
    assert _is_junk_title(".4.2")
    assert _is_junk_title("28.6.5.")
    assert not _is_junk_title("Глава 19")


def test_title_from_body_skips_jurisdiction_markers():
    # Уголовка Majestic: маркер юрисдикции и звёзды розыска между заголовком и названием
    body = "Статья 15.6\n[Федеральная/Региональная]\nХалатность\nч. 1\n★★★★\nХалатность, то есть..."
    assert _title_from_body(body) == "Халатность"
    body2 = "Статья 12.1.\n(ФЕДЕРАЛЬНЫЙ)\nТерроризм\nч. 1 ..."
    assert _title_from_body(body2) == "Терроризм"


def test_title_from_body_marker_then_prose_gives_none():
    body = "Статья 6.1.\n(РЕГИОНАЛЬНЫЙ)\nУмышленное причинение тяжкого вреда здоровью опасного для жизни человека ((Состояние нокаута)) и прочее длинное определение\n— 6 до 8 лет."
    assert _title_from_body(body) is None


# --- junk-титул из ОДНОСТРОЧНОГО заголовка не должен победить название из тела -----------------
# Регресс retitle 2026-07-03: normalize_header тянул «*.»/«★★★★ [..]» из однострочного заголовка
# как title (непустой) → фолбэк на тело не срабатывал → служебный маркер оседал в карточке.

@pytest.mark.parametrize("ref,body,expected", [
    # davis: «Статья 60*.» — звёзды-декор нумерации, название второй строкой
    ("Статья 60*.", "Статья 60*.\nКлевета\nч. 1\nКлевета в отношении лица...", "Клевета"),
    ("Статья 29**.", "Статья 29**.\nБезрассудное вождение\nч. 1 ...", "Безрассудное вождение"),
    # memphis/уголовка: код + звёзды розыска + скобка юрисдикции ОДНОЙ строкой
    ("Статья 12.8 ★★★★ [Федеральная/Региональная]",
     "Статья 12.8 ★★★★ [Федеральная/Региональная]\nНезаконный оборот оружия\nч. 1 ...",
     "Незаконный оборот оружия"),
    ("Статья 16.6 ★★★★★ [Федеральная]",
     "Статья 16.6 ★★★★★ [Федеральная]\nПосягательство на жизнь сотрудника\nч. 1 ...",
     "Посягательство на жизнь сотрудника"),
])
def test_junk_header_title_falls_through_to_body(ref, body, expected):
    assert _is_junk_title(normalize_header(ref)[2])  # заголовок дал именно служебный маркер
    assert _title_for(ref, body) == expected


def test_valid_header_title_survives():
    # Нормальный заголовок с настоящим названием НЕ уходит в тело
    assert _title_for("Статья 15.6. Халатность", "Статья 15.6. Халатность\nч. 1 ...") == "Халатность"


def test_rating_range_header_title_falls_through_to_body():
    ref = "Статья 16.15 от ★ до ★★★★★ [Федеральная/Региональная]"
    body = (
        f"{ref}\n"
        "Уклонение от отбывания ограничения свободы или лишения свободы.\n"
        "Наказание: лишение свободы."
    )
    assert _is_junk_title(normalize_header(ref)[2])
    assert _title_for(ref, body) == \
        "Уклонение от отбывания ограничения свободы или лишения свободы"


def test_runtime_recovers_old_rating_title_without_changing_article_id():
    title = "от ★ до ★★★★★ [Федеральная/Региональная]"
    body = (
        "Статья 16.15 от ★ до ★★★★★ [Федеральная/Региональная]\n"
        "Уклонение от отбывания ограничения свободы или лишения свободы.\n"
        "Наказание: лишение свободы."
    )
    assert recover_rating_metadata_title(title, body) == \
        "Уклонение от отбывания ограничения свободы или лишения свободы"


def test_runtime_recovery_skips_separate_jurisdiction_line():
    body = (
        "Статья 16.15 от ★ до ★★★★★\n"
        "[ФЕДЕРАЛЬНЫЙ/РЕГИОНАЛЬНЫЙ]\n"
        "Уклонение от отбывания ограничения свободы или лишения свободы.\n"
        "Наказание: до 50 месяцев лишения свободы."
    )
    assert recover_rating_metadata_title("от ★ до ★★★★★", body) == \
        "Уклонение от отбывания ограничения свободы или лишения свободы"


@pytest.mark.parametrize("title", [
    "от ★ до ★★★★★ [Федеральная] Уклонение от наказания",
    "Уклонение от отбывания ограничения свободы",
    "Отказ от дачи показаний",
])
def test_rating_metadata_guard_does_not_eat_meaningful_titles(title):
    assert not _is_junk_title(title)


# --- split_into_blocks -------------------------------------------------------------------------

LAW_TEXT = """Настоящий кодекс определяет порядок.

Статья 1. Общие положения
Тело первой статьи.

Статья 2.5 Халатность
Тело второй статьи.
Наказание: штраф.
"""


def test_split_into_blocks_preamble_and_articles():
    blocks = split_into_blocks(LAW_TEXT)
    refs = [ref for ref, _ in blocks]
    assert refs[0] is None  # преамбула
    assert refs[1] == "Статья 1. Общие положения"
    assert refs[2] == "Статья 2.5 Халатность"
    assert "Тело второй статьи" in blocks[2][1]
    # Заголовок остаётся в теле блока (карточка показывает полный текст статьи)
    assert blocks[1][1].startswith("Статья 1.")


def test_split_into_blocks_no_headers():
    blocks = split_into_blocks("Просто текст правил без структуры.")
    assert len(blocks) == 1
    assert blocks[0][0] is None


def test_split_into_blocks_empty():
    assert split_into_blocks("") == []
    assert split_into_blocks(None) == []


def test_split_into_blocks_ref_capped_at_120():
    long_header = "Статья 99 " + "оченьдлинноеслово " * 20
    blocks = split_into_blocks(long_header + "\nтело")
    assert blocks[0][0] is not None and len(blocks[0][0]) <= 120


def test_split_into_blocks_does_not_turn_inline_article_list_into_header():
    text = """Статья 1. Виды лицензий
Лицензия аннулируется при нарушении:
ст. 6.2, ст. 6.3, 6.5, ст. 10.6, ст. 11.2.1 УК
Перечень является частью первой статьи.

Статья 2. Ответственность
Тело второй статьи.
"""
    blocks = split_into_blocks(text)
    assert [ref for ref, _body in blocks] == [
        "Статья 1. Виды лицензий",
        "Статья 2. Ответственность",
    ]
    assert "ст. 6.2, ст. 6.3" in blocks[0][1]
    assert "Перечень является частью первой статьи" in blocks[0][1]


def test_split_into_blocks_keeps_real_abbreviated_article_header():
    blocks = split_into_blocks(
        "Ст. 7 - Кража\nТело нормы.\n\nСт. 8; Разбой\nДругая норма."
    )
    assert [ref for ref, _body in blocks] == [
        "Ст. 7 - Кража",
        "Ст. 8; Разбой",
    ]


@pytest.mark.parametrize(
    "citation",
    [
        "ст. 12.7.1 УК",
        "ст. 12.7 УК",
        "ст. 12.7.1 УК SA.",
        "ст. 3.12.1 ПК",
        "ст. 15.7 УК СА",
        "Ст. 12.14.1 УАК.",
    ],
)
def test_split_into_blocks_keeps_standalone_cross_code_citation_in_body(citation):
    text = f"""Статья 6. Использование воздушного пространства
1. Основное правило.
2. За нарушение применяется ответственность.
Примечание:
{citation}
3. Все посадочные площадки являются общедоступными.

Статья 7. Воздушное пространство
Тело следующей статьи.
"""
    blocks = split_into_blocks(text)
    assert [ref for ref, _body in blocks] == [
        "Статья 6. Использование воздушного пространства",
        "Статья 7. Воздушное пространство",
    ]
    assert citation in blocks[0][1]
    assert "3. Все посадочные площадки" in blocks[0][1]


@pytest.mark.parametrize(
    "label, citation",
    [
        ("Исключение:", "статья 25 настоящего закона."),
        (
            "Примечание:",
            "п.4 настоящей статьи реализуется исключительно актами прокурора.",
        ),
        (
            "Примечание No.2",
            "Статья 17.3 настоящего Кодекса применяется к указанным лицам.",
        ),
        (
            "Примечание № 3:",
            "пункт 1.2 настоящей главы, не распространяется на исключения.",
        ),
    ],
)
def test_split_into_blocks_keeps_labeled_self_references_in_parent_body(
    label,
    citation,
):
    text = f"""Статья 4. Основная норма
Тело основной нормы.
{label}
{citation}
Статья 5. Следующая норма
Тело следующей нормы.
"""
    blocks = split_into_blocks(text)
    assert [ref for ref, _body in blocks] == [
        "Статья 4. Основная норма",
        "Статья 5. Следующая норма",
    ]
    assert citation in blocks[0][1]


def test_split_into_blocks_preserves_real_headers_near_self_reference_shape():
    text = """Статья 4. Основная норма
Тело основной нормы.
Исключение:
Статья 25. Применение настоящего закона
Самостоятельная норма.
Примечание:
Пункт 4. Порядок реализации настоящей статьи
Самостоятельный пункт.
Статья 26. Следующая норма
Тело следующей нормы.
"""
    blocks = split_into_blocks(text)
    assert [ref for ref, _body in blocks] == [
        "Статья 4. Основная норма",
        "Статья 25. Применение настоящего закона",
        "Пункт 4. Порядок реализации настоящей статьи",
        "Статья 26. Следующая норма",
    ]

    unlabeled = split_into_blocks(
        "Статья 1. Первая норма\n"
        "Статья 9 настоящего закона\n"
        "Статья 2. Вторая норма\n"
    )
    assert [ref for ref, _body in unlabeled] == [
        "Статья 1. Первая норма",
        "Статья 9 настоящего закона",
        "Статья 2. Вторая норма",
    ]

    wrapped = split_into_blocks(
        "Статья 1. Первая норма\nИсключение:\n"
        "Статья\n25.\nСамостоятельная норма.\n"
    )
    assert [ref.replace("\n", " ") for ref, _body in wrapped] == [
        "Статья 1. Первая норма",
        "Статья 25.",
    ]


def test_rejected_self_reference_does_not_open_legacy_bare_fallback():
    text = """Примечание:
Статья 1.2 настоящего закона применяется только к исключениям.
1.1
Первый похожий пункт.
1.2
Второй похожий пункт.
1.3
Третий похожий пункт.
"""
    plan = _header_match_plan(text)
    assert plan.structured_article_count == 0
    assert len(plan.legacy_bare_candidates) == 3
    assert plan.fallback_mode is None
    assert split_into_blocks(text) == [(None, text.strip())]


@pytest.mark.parametrize(
    "newline, blank_line, revision, changelog",
    [
        (
            "\n",
            "",
            "Редакция от 22.04.2025",
            (
                "Статья 31 главы 5: уточнен контроль руководства "
                "за исполнением пункта"
            ),
        ),
        (
            "\r\n",
            "\r\n",
            "Редакции от 23.04.2025",
            "Статья 31.2 главы IV — Изменена процедура контроля",
        ),
    ],
)
def test_split_into_blocks_keeps_revision_changelog_in_previous_article_body(
    newline,
    blank_line,
    revision,
    changelog,
):
    text = (
        f"Статья 34. Действующая норма{newline}"
        f"Основное тело статьи.{newline}"
        f"{revision}{newline}"
        f"{blank_line}"
        f"{changelog}{newline}"
        f"Статья 35. Следующая норма{newline}"
        "Следующее тело."
    )

    blocks = split_into_blocks(text)

    assert [ref for ref, _body in blocks] == [
        "Статья 34. Действующая норма",
        "Статья 35. Следующая норма",
    ]
    assert f"{revision}{newline}{blank_line}{changelog}" in blocks[0][1]
    assert _header_match_plan(text).structured_article_count == 2


@pytest.mark.parametrize(
    "edit_lexeme",
    [
        "уточнён",
        "изменена",
        "добавлено",
        "удалены",
        "убран",
        "исправлена",
        "перенесено",
        "дополнены",
        "скорректирован",
        "заменена",
        "актуализировано",
        "внесены",
    ],
)
def test_revision_changelog_filter_accepts_only_bounded_edit_lexemes(
    edit_lexeme,
):
    text = (
        "Статья 30. Действующая норма\n"
        "Тело нормы.\n"
        "Редакция от 22.04.2025\n"
        f"Статья 31 главы 5: {edit_lexeme} положение закона\n"
        "Статья 32. Следующая норма\n"
        "Тело следующей нормы."
    )

    assert [ref for ref, _body in split_into_blocks(text)] == [
        "Статья 30. Действующая норма",
        "Статья 32. Следующая норма",
    ]


@pytest.mark.parametrize(
    "middle",
    [
        "Редакция от 22.04.2025\nСтатья 31. Действующая норма",
        (
            "Редакция от 22.04.2025\n"
            "Статья 31 главы 5: Порядок контроля руководства"
        ),
        "Статья 31 главы 5: уточнен порядок контроля",
        (
            "Редакция от 22.04.2025\n"
            "Пояснение редактора.\n"
            "Статья 31 главы 5: уточнен порядок контроля"
        ),
        (
            "Редакция от 22.04.2025\n\n\n"
            "Статья 31 главы 5: уточнен порядок контроля"
        ),
        (
            "Редакция от 22.04.2025\n"
            "Статья 31 главы 5: уточненный порядок контроля"
        ),
        (
            "Редакция от 22.04.2025\n"
            "Статья 31 главы 5. Уточнен порядок контроля"
        ),
    ],
)
def test_revision_changelog_filter_keeps_nearby_real_or_ambiguous_headers(
    middle,
):
    text = (
        "Статья 30. Действующая норма\n"
        "Тело нормы.\n"
        f"{middle}\n"
        "Статья 32. Следующая норма\n"
        "Тело следующей нормы."
    )

    refs = [ref for ref, _body in split_into_blocks(text)]

    assert len(refs) == 3
    assert refs[0] == "Статья 30. Действующая норма"
    assert refs[-1] == "Статья 32. Следующая норма"
    assert refs[1].startswith("Статья 31")


def test_rejected_revision_changelog_does_not_open_legacy_bare_fallback():
    text = """Редакция от 22.04.2025
Статья 31 главы 5: уточнен контроль руководства
1.1
Первый похожий пункт.
1.2
Второй похожий пункт.
1.3
Третий похожий пункт.
"""

    plan = _header_match_plan(text)

    assert plan.structured_article_count == 0
    assert len(plan.legacy_bare_candidates) == 3
    assert plan.fallback_mode is None
    assert split_into_blocks(text) == [(None, text.strip())]


def test_split_into_blocks_collapses_empty_adjacent_same_article_header():
    previous = "Статья 5. Предыдущая норма\nТело предыдущей нормы."
    real_header = (
        "Статья 6. Министр национальной безопасности и его заместители "
        "имеют беспрепятственный доступ к государственной территории."
    )
    text = (
        previous
        + "\nСтатья 6.\n"
        + real_header
        + "\nПолное продолжение шестой статьи.\n"
        + "Статья 7. Следующая норма\nТело следующей нормы."
    )
    blocks = split_into_blocks(text)
    assert [ref for ref, _body in blocks] == [
        "Статья 5. Предыдущая норма",
        real_header[:120],
        "Статья 7. Следующая норма",
    ]
    assert blocks[0][1] == previous
    assert blocks[1][1].startswith(real_header)
    assert "\nСтатья 6.\n" not in blocks[1][1]
    assert "Полное продолжение шестой статьи." in blocks[1][1]
    assert _header_match_plan(text).structured_article_count == 3


def test_split_into_blocks_collapses_zero_width_and_reverse_duplicate_layouts():
    second_rich = split_into_blocks(
        "Статья 1. Первая норма\nТело.\n"
        "Статья 2.3.\n\u200b\n"
        "Статья 2.3. Ордер Search Access\nПолное тело ордера.\n"
        "Статья 3. Следующая норма\nТело."
    )
    assert [ref for ref, _body in second_rich] == [
        "Статья 1. Первая норма",
        "Статья 2.3. Ордер Search Access",
        "Статья 3. Следующая норма",
    ]
    assert second_rich[1][1].startswith("Статья 2.3. Ордер Search Access")

    first_rich = split_into_blocks(
        "Статья 6.5 Правительство гарантирует соблюдение прав и свобод\n"
        "Статья 6.5\n"
        "Гарантии реализуются в порядке настоящего закона.\n"
        "Статья 7. Следующая норма\nТело."
    )
    assert [ref for ref, _body in first_rich] == [
        "Статья 6.5 Правительство гарантирует соблюдение прав и свобод",
        "Статья 7. Следующая норма",
    ]
    assert first_rich[0][1].splitlines() == [
        "Статья 6.5 Правительство гарантирует соблюдение прав и свобод",
        "Гарантии реализуются в порядке настоящего закона.",
    ]

    identical_titleless = split_into_blocks(
        "Статья 2.4\n"
        "Статья 2.4\n"
        "Единственное действующее тело статьи.\n"
        "Статья 3. Следующая норма\nТело."
    )
    assert [ref for ref, _body in identical_titleless] == [
        "Статья 2.4",
        "Статья 3. Следующая норма",
    ]
    assert identical_titleless[0][1].splitlines() == [
        "Статья 2.4",
        "Единственное действующее тело статьи.",
    ]


def test_split_into_blocks_does_not_collapse_semantically_separate_same_codes():
    with_body = split_into_blocks(
        "Статья 2.4\nУтратила силу.\n"
        "Статья 2.4. Новая редакция\nДействующая норма."
    )
    assert [ref for ref, _body in with_body] == [
        "Статья 2.4",
        "Статья 2.4. Новая редакция",
    ]

    two_rich = split_into_blocks(
        "Статья 2. Первая редакция\n"
        "Статья 2. Вторая редакция\n"
        "Тело второй редакции."
    )
    assert [ref for ref, _body in two_rich] == [
        "Статья 2. Первая редакция",
        "Статья 2. Вторая редакция",
    ]


@pytest.mark.parametrize(
    "header",
    [
        "ст. 1.1",
        "Ст. 7 — Кража",
        "Ст. 8; Разбой",
        "Статья 12.7.1 УК",
        "Ст. 12.7.1 УК — Название",
        "Ст. 12.7.1 Уклонение от наказания",
    ],
)
def test_split_into_blocks_preserves_real_headers_near_citation_formats(header):
    blocks = split_into_blocks(f"{header}\nТело нормы.")
    assert len(blocks) == 1
    assert blocks[0][0] == header


def test_split_into_blocks_filters_article_reference_inside_clause_sequence():
    text = """Статья 9. Аннулирование лицензии
9.2
Первое основание для аннулирования.
Статья 36
9.3
Второе основание для аннулирования.

Статья 10. Заморозка лицензии
Тело следующей статьи.
"""
    blocks = split_into_blocks(text)
    assert [ref for ref, _body in blocks] == [
        "Статья 9. Аннулирование лицензии",
        "Статья 10. Заморозка лицензии",
    ]
    assert "Статья 36\n9.3" in blocks[0][1]


def test_split_into_blocks_preserves_wrapped_and_ordinary_titleless_headers():
    wrapped = split_into_blocks(
        "Статья\n27.1.\nТело нормы.\n\nСтатья 28.\nСледующая норма."
    )
    assert [ref.replace("\n", " ") for ref, _body in wrapped] == [
        "Статья 27.1.",
        "Статья 28.",
    ]

    ordinary = split_into_blocks(
        "Статья 35.\nТело нормы.\n\nСтатья 36\n1. Первое положение.\n\n"
        "Статья 37.\nСледующая норма."
    )
    assert [ref for ref, _body in ordinary] == [
        "Статья 35.",
        "Статья 36",
        "Статья 37.",
    ]

    non_adjacent = split_into_blocks(
        "Статья 9.\n9.2\nПервое положение.\nСтатья 36\n9.4\n"
        "Отдельное положение.\nСтатья 37.\nСледующая норма."
    )
    assert [ref for ref, _body in non_adjacent] == [
        "Статья 9.",
        "Статья 36",
        "Статья 37.",
    ]


def test_split_into_blocks_repairs_only_proven_broken_article_number_layouts():
    content = """Статья 16.
Контрактная система.

Статья 16. 1.
Основные понятия.

Статья 8
.3.
Порядок подачи жалобы.

Статья
19
.1.1
Список свидетелей.

Статья 9.
Следующая норма.
"""
    blocks = split_into_blocks(content)
    assert [ref for ref, _body in blocks] == [
        "Статья 16.",
        "Статья 16.1",
        "Статья 8.3",
        "Статья 19.1.1",
        "Статья 9.",
    ]

    rows = catalog._catalog_rows(content)
    assert [row["code"] for row in rows] == [
        "16",
        "16.1",
        "8.3",
        "19.1.1",
        "9",
    ]
    assert rows[1]["title"] == "Основные понятия"
    assert rows[2]["title"] is None
    assert rows[2]["full_text"].startswith("Статья 8\n.3.")
    assert rows[3]["title"] is None
    assert rows[3]["full_text"].startswith("Статья\n19\n.1.1")


@pytest.mark.parametrize(
    "broken_keyword",
    [
        "С\nтатья",
        "Ст\nатья",
        "Ста\nтья",
        "Стат\nья",
        "Стать\nя",
    ],
)
def test_split_into_blocks_repairs_split_article_keyword(broken_keyword):
    content = (
        "Глава 11. Заключительные положения\n"
        f"{broken_keyword} 58.\n"
        "Общие положения.\n"
        "58.1.\n"
        "Внутренний пункт статьи.\n"
        "Статья 59. Следующая норма\n"
        "Тело."
    )
    blocks = split_into_blocks(content)
    assert [ref for ref, _body in blocks] == [
        "Глава 11. Заключительные положения",
        "Статья 58.",
        "Статья 59. Следующая норма",
    ]

    rows = catalog._catalog_rows(content)
    article_58 = next(row for row in rows if row["code"] == "58")
    assert article_58["title"] == "Общие положения"
    assert article_58["full_text"].startswith(
        "Статья 58.\nОбщие положения."
    )
    assert f"{broken_keyword} 58." not in article_58["full_text"]


def test_split_article_keyword_repair_rejects_unrelated_wrapped_word():
    content = """Глава 11
С
правка 58.
Обычный текст.
Статья 59.
Следующая норма.
"""
    refs = [ref for ref, _body in split_into_blocks(content)]
    assert refs == ["Глава 11", "Статья 59."]


@pytest.mark.parametrize(
    ("broken_header", "code"),
    [
        ("Стать\nя 5.15\n.", "5.15"),
        ("С\nтатья 35\n.", "35"),
    ],
)
def test_split_article_keyword_consumes_separate_trailing_dot(
    broken_header,
    code,
):
    content = (
        f"{broken_header}\n"
        "Название нормы.\n"
        "Тело нормы.\n"
        "Статья 99. Следующая норма\n"
        "Тело."
    )
    rows = catalog._catalog_rows(content)
    repaired = next(row for row in rows if row["code"] == code)

    assert repaired["article_ref"] == f"Статья {code}."
    assert repaired["title"] == "Название нормы"
    assert repaired["full_text"].startswith(
        f"Статья {code}.\nНазвание нормы."
    )
    assert "\n.\n" not in repaired["full_text"]


@pytest.mark.parametrize(
    ("broken_header", "expected_ref", "expected_code", "expected_title"),
    [
        (
            "Г\nлава II. Полномочия",
            "Глава II. Полномочия",
            "II",
            "Полномочия",
        ),
        (
            "ГЛАВ\nА 2\n.\nОБЩИЕ ПОЛОЖЕНИЯ",
            "Глава 2.",
            "2",
            "ОБЩИЕ ПОЛОЖЕНИЯ",
        ),
    ],
)
def test_split_chapter_keyword_exposes_canonical_ref_but_keeps_raw_body(
    broken_header,
    expected_ref,
    expected_code,
    expected_title,
):
    content = (
        "Глава I. Предыдущая глава\n"
        "Текст.\n"
        f"{broken_header}\n"
        "Содержимое восстановленной главы.\n"
        "Глава III. Следующая глава\n"
        "Текст."
    )

    blocks = split_into_blocks(content)
    rows = catalog._catalog_rows(content)
    chunks = chunk_law_text(content, "Закон")
    repaired_body = next(body for ref, body in blocks if ref == expected_ref)
    repaired_row = next(row for row in rows if row["article_ref"] == expected_ref)

    assert repaired_body.startswith(broken_header)
    assert repaired_row["kind"] == "chapter"
    assert repaired_row["code"] == expected_code
    assert repaired_row["title"] == expected_title
    assert {chunk["article_ref"] for chunk in chunks} == {
        ref for ref, _body in blocks
    }


def test_split_structural_keyword_canonicalizer_handles_article_and_chapter():
    source = (
        "С\nтатья 1. Норма\n"
        "Текст.\n"
        "ГЛАВ\nА 2\n.\n"
        "ОБЩИЕ ПОЛОЖЕНИЯ"
    )

    canonical = canonicalize_split_structural_keyword_layout(source)

    assert canonical == (
        "Статья 1. Норма\n"
        "Текст.\n"
        "Глава 2.\n"
        "ОБЩИЕ ПОЛОЖЕНИЯ"
    )


@pytest.mark.parametrize("roman_code", ["IX", "X"])
def test_wrapped_chapter_code_and_dot_are_canonical_but_body_stays_raw(
    roman_code,
):
    broken_header = f"Глава\n{roman_code}\n."
    content = (
        f"{broken_header}\n"
        "ПОРЯДОК РАБОТЫ\n"
        "Содержимое главы."
    )

    blocks = split_into_blocks(content)
    rows = catalog._catalog_rows(content)
    repaired_body = next(
        body for ref, body in blocks if ref == f"Глава {roman_code}."
    )
    repaired_row = next(row for row in rows if row["code"] == roman_code)

    assert repaired_body.startswith(broken_header)
    assert repaired_row["article_ref"] == f"Глава {roman_code}."
    assert repaired_row["kind"] == "chapter"
    assert repaired_row["title"] == "ПОРЯДОК РАБОТЫ"


@pytest.mark.parametrize(
    "broken_header",
    [
        "Глава\nIX",
        "Глава\nIIII\n.",
        "Глава\nIX Название\n.",
        "Глава\nIX\n. Название",
    ],
)
def test_wrapped_chapter_code_rejects_unproven_layouts(broken_header):
    content = (
        f"{broken_header}\n"
        "ПОРЯДОК РАБОТЫ\n"
        "Содержимое главы."
    )

    refs = [ref for ref, _body in split_into_blocks(content)]

    assert "Глава IX." not in refs


def test_structural_canonicalizer_handles_wrapped_chapter_code_and_dot():
    source = (
        "Вводный текст.\n"
        "Глава\n"
        "IX\n"
        ".\n"
        "ПОРЯДОК РАБОТЫ"
    )

    assert canonicalize_split_structural_keyword_layout(source) == (
        "Вводный текст.\n"
        "Глава IX.\n"
        "ПОРЯДОК РАБОТЫ"
    )


@pytest.mark.parametrize(
    "false_split",
    [
        "Г\nлаварь II. Подразделение",
        "Гла\nва администрации",
        "Поп\nравка II",
    ],
)
def test_split_chapter_keyword_rejects_non_exact_or_uncoded_words(false_split):
    content = (
        f"{false_split}\n"
        "Обычный текст.\n"
        "Статья 1. Настоящая норма\n"
        "Тело."
    )

    refs = [ref for ref, _body in split_into_blocks(content)]

    assert not any(ref and ref.casefold().startswith("глава") for ref in refs)
    assert refs[-1] == "Статья 1. Настоящая норма"


@pytest.mark.parametrize(
    "broken_chapter",
    [
        "Глава X\nI\nI\n. Полномочия",
        "Глава XI\nI\n. Полномочия",
    ],
)
def test_wrapped_roman_chapter_requires_and_uses_two_sided_sequence_proof(
    broken_chapter,
):
    content = (
        "Глава XI. Предыдущая глава\n"
        "Текст.\n"
        f"{broken_chapter}\n"
        "Содержимое восстановленной главы.\n"
        "Глава XIII. Следующая глава\n"
        "Текст."
    )

    blocks = split_into_blocks(content)
    rows = catalog._catalog_rows(content)
    repaired_body = next(
        body
        for ref, body in blocks
        if ref == "Глава XII. Полномочия"
    )
    repaired_row = next(row for row in rows if row["code"] == "XII")

    assert repaired_body.startswith(broken_chapter)
    assert repaired_row["kind"] == "chapter"
    assert repaired_row["title"] == "Полномочия"
    assert "Глава XII. Полномочия" in [
        ref for ref, _body in blocks
    ]


def test_wrapped_roman_chapter_two_sided_proof_supports_crlf_source():
    content = (
        "Глава XI. Предыдущая глава\r\n"
        "Текст.\r\n"
        "Глава XI\r\n"
        "I\r\n"
        ". Полномочия\r\n"
        "Содержимое восстановленной главы.\r\n"
        "Глава XIII. Следующая глава\r\n"
        "Текст."
    )

    refs = [ref for ref, _body in split_into_blocks(content)]

    assert "Глава XII. Полномочия" in refs


@pytest.mark.parametrize(
    ("content", "forbidden_ref"),
    [
        # Missing right-hand proof.
        (
            "Глава XI. Предыдущая\nТекст.\n"
            "Глава XI\nI\n. Полномочия\nТекст.",
            "Глава XII. Полномочия",
        ),
        # The next chapter proves XIV, not the reconstructed XII.
        (
            "Глава XI. Предыдущая\nТекст.\n"
            "Глава XI\nI\n. Полномочия\nТекст.\n"
            "Глава XIV. Следующая\nТекст.",
            "Глава XII. Полномочия",
        ),
        # ``IIII`` is not a canonical Roman numeral.
        (
            "Глава III. Предыдущая\nТекст.\n"
            "Глава I\nI\nI\nI\n. Полномочия\nТекст.\n"
            "Глава V. Следующая\nТекст.",
            "Глава IV. Полномочия",
        ),
        # Four physical fragments exceed the narrow repair grammar.
        (
            "Глава XIII. Предыдущая\nТекст.\n"
            "Глава X\nI\nI\nI\nI\n. Полномочия\nТекст.\n"
            "Глава XV. Следующая\nТекст.",
            "Глава XIV. Полномочия",
        ),
        # A blank physical line breaks the wrapped layout.
        (
            "Глава XI. Предыдущая\nТекст.\n"
            "Глава XI\n\nI\n. Полномочия\nТекст.\n"
            "Глава XIII. Следующая\nТекст.",
            "Глава XII. Полномочия",
        ),
    ],
)
def test_wrapped_roman_chapter_rejects_incomplete_or_ambiguous_proof(
    content,
    forbidden_ref,
):
    refs = [ref for ref, _body in split_into_blocks(content)]

    assert forbidden_ref not in refs


def test_split_keyword_terminal_roman_suffix_has_narrow_end_of_document_proof():
    broken = "Г\nлава I\nI."
    content = (
        "Глава I. Предыдущая глава\n"
        "Текст.\n"
        f"{broken}\n"
        "Порядок работы подразделения\n"
        "Содержимое."
    )

    blocks = split_into_blocks(content)
    rows = catalog._catalog_rows(content)
    repaired_body = next(body for ref, body in blocks if ref == "Глава II.")
    repaired_row = next(row for row in rows if row["code"] == "II")

    assert repaired_body.startswith(broken)
    assert repaired_row["kind"] == "chapter"
    assert repaired_row["title"] == "Порядок работы подразделения"


def test_split_keyword_terminal_roman_suffix_supports_crlf_source():
    broken = "Г\r\nлава I\r\nI."
    content = (
        "Глава I. Предыдущая глава\r\n"
        "Текст.\r\n"
        f"{broken}\r\n"
        "Порядок работы подразделения\r\n"
        "Содержимое."
    )

    blocks = split_into_blocks(content)
    repaired_body = next(body for ref, body in blocks if ref == "Глава II.")

    assert repaired_body.startswith(broken)


def test_terminal_roman_suffix_exception_rejects_unsplit_or_nonterminal_layout():
    ordinary = (
        "Глава I. Предыдущая\nТекст.\n"
        "Глава I\nI.\nПорядок работы\nТекст."
    )
    nonterminal_split = (
        "Глава I. Предыдущая\nТекст.\n"
        "Г\nлава I\nI.\nПорядок работы\nТекст.\n"
        "Глава III. Следующая\nТекст."
    )

    assert "Глава II." not in [
        ref for ref, _body in split_into_blocks(ordinary)
    ]
    assert "Глава II." not in [
        ref for ref, _body in split_into_blocks(nonterminal_split)
    ]


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("XII", 12),
        ("ХII", 12),
        ("Ⅻ", 12),
        ("lx", 60),
        ("liv", 54),
    ],
)
def test_canonical_roman_value_accepts_only_strict_valid_tokens(token, expected):
    assert canonical_roman_value(token) == expected


@pytest.mark.parametrize("token", ["", "IIII", "IIV", "VX", "IC", "roman"])
def test_canonical_roman_value_rejects_noncanonical_tokens(token):
    assert canonical_roman_value(token) is None


def test_wrapped_suffix_repair_does_not_consume_ordinary_article_body():
    blocks = split_into_blocks(
        "Статья 8.\n.3.\nОбычный текст.\n\n"
        "Статья 9\n3. Пункт тела.\n\n"
        "Статья 10.\nСледующая норма."
    )
    assert [ref for ref, _body in blocks] == [
        "Статья 8.",
        "Статья 9",
        "Статья 10.",
    ]


def test_wrapped_article_code_does_not_promote_broken_provision_to_title():
    rows = catalog._catalog_rows(
        "Статья 9\n"
        ".3\n"
        "Р\n"
        "азглашать информацию, полученную при исполнении полномочий, запрещено.\n\n"
        "Статья 10. Следующая норма\n"
        "Тело."
    )

    assert rows[0]["code"] == "9.3"
    assert rows[0]["title"] is None
    assert "\nР\nазглашать информацию" in rows[0]["full_text"]


def test_structural_multiline_article_codes_are_not_bare_candidates_twice():
    content = """Статья
1.1.
Первая норма.

Статья
1.2.
Вторая норма.

Статья
1.3.
Третья норма.
"""
    plan = _header_match_plan(content)
    assert plan.structured_article_count == 3
    assert plan.legacy_bare_candidates == ()
    assert plan.fallback_mode is None
    assert [ref for ref, _body in split_into_blocks(content)] == [
        "Статья 1.1.",
        "Статья 1.2.",
        "Статья 1.3.",
    ]


def test_split_into_blocks_filters_only_qualified_full_article_reference_list():
    content = """Статья 13. Полномочия
ч. 2.
Сотрудники вправе применять положения следующих статей Дорожного кодекса:

Статья 1 — Обязанность предъявления документов;
Статья 2 — Обязанность остановиться по требованию сотрудника;
ч. 3.
Применение положений допускается только в рамках компетенции.

Статья 14. Следующая норма
Тело.
"""
    blocks = split_into_blocks(content)
    assert [ref for ref, _body in blocks] == [
        "Статья 13. Полномочия",
        "Статья 14. Следующая норма",
    ]
    assert "Статья 1 — Обязанность" in blocks[0][1]
    assert "Статья 2 — Обязанность" in blocks[0][1]
    assert "ч. 3." in blocks[0][1]


def test_full_article_headers_with_dashes_remain_structural_without_full_context():
    content = """Статья 1 — Первая настоящая норма;
Тело первой нормы.

Статья 2 — Вторая настоящая норма;
Тело второй нормы.
"""
    assert [ref for ref, _body in split_into_blocks(content)] == [
        "Статья 1 — Первая настоящая норма;",
        "Статья 2 — Вторая настоящая норма;",
    ]


JUDICIAL_COLLECTION_TEXT = """Вводное описание судебной практики.

Прецедент Окружного суда штата San-Andreas №0001 от 24.12.2024
Статья 16.14 Уголовного кодекса касается исполнения решения, но это ссылка.
""" + ("Подробное обоснование первого решения. " * 12) + """

Толкование по обращению о толковании №2
Пункт 2 имеет значение только внутри этого толкования.
2.1
Внутренний нумерованный вывод, который не является статьёй.
""" + ("Подробное обоснование второго решения. " * 12) + """

ВЕРХОВНЫЙ СУД ШТАТА САН-АНДРЕАС
ПОСТАНОВЛЕНИЕ
ВЕРХОВНОГО СУДА ШТАТА SAN-ANDREAS
от 22 января 2026 года
ПО ДЕЛУ О ТОЛКОВАНИИ СТАТЬИ 17.6 УГОЛОВНОГО КОДЕКСА
Статья 17.6 Уголовного кодекса применяется только при указанных условиях.
""" + ("Подробное обоснование третьего решения. " * 12)


def test_judicial_collection_uses_materials_not_cited_law_articles():
    plan = _header_match_plan(JUDICIAL_COLLECTION_TEXT)
    assert plan.fallback_mode == "judicial_materials"
    assert len(plan.judicial_material_candidates) == 3
    assert len(plan.accepted_judicial_materials) == 3
    assert plan.structured_article_count == 0
    assert plan.legacy_bare_candidates == ()

    blocks = split_into_blocks(JUDICIAL_COLLECTION_TEXT)
    assert len(blocks) == 4
    assert blocks[0][0] is None
    assert blocks[1][0].startswith("Прецедент Окружного суда")
    assert blocks[2][0] == "Толкование по обращению о толковании №2"
    assert blocks[3][0].startswith("Толкование от 22 января 2026 года")
    assert "Статья 16.14" in blocks[1][1]
    assert "Пункт 2" in blocks[2][1]
    assert "Статья 17.6" in blocks[3][1]

    rows = catalog._catalog_rows(JUDICIAL_COLLECTION_TEXT)
    assert len(rows) == 4
    assert {row["kind"] for row in rows} == {"other"}
    chunks = chunk_law_text(JUDICIAL_COLLECTION_TEXT, "Судебные толкования")
    assert {chunk["article_ref"] for chunk in chunks} == {
        ref for ref, _body in blocks
    }


def test_judicial_collection_mode_rejects_contents_and_legislative_prefix():
    short_contents = """Прецедент Окружного суда штата SA №1
Коротко.
Толкование по обращению о толковании №2
Коротко.
Толкование по обращению в ВС №3
Коротко.
"""
    contents_plan = _header_match_plan(short_contents)
    assert contents_plan.fallback_mode != "judicial_materials"
    assert len(contents_plan.judicial_material_candidates) == 3
    assert contents_plan.accepted_judicial_materials == ()
    assert any(
        "too short" in reason
        for reason in contents_plan.judicial_rejection_reasons
    )

    legislative = (
        "Статья 1. Настоящая норма\nТело.\n\n"
        + JUDICIAL_COLLECTION_TEXT
    )
    legislative_plan = _header_match_plan(legislative)
    assert legislative_plan.fallback_mode != "judicial_materials"
    assert any(
        "legislative structural header" in reason
        for reason in legislative_plan.judicial_rejection_reasons
    )
    assert "Статья 1. Настоящая норма" in [
        ref for ref, _body in split_into_blocks(legislative)
    ]


def test_split_into_blocks_rejects_mayoral_text_but_preserves_roman_chapters():
    content = """Преамбула.
Глава Мэрии округа Блэйн занимается администрацией.
Глава Мэрии города занимается бюджетом.
Статьями 5.7 и 5.8 предусмотрена ответственность.
Пункт может быть пропущен при опасности.

Глава М
Тело настоящей главы.

Глава IV. Полномочия
Тело следующей главы.

Глава IХ. Казначейство
Тело главы со смешанными римскими символами.

Глава Ⅱ. Неприкосновенность
Тело главы с Unicode-символом римского числа.

Статья 1Работники системы
Тело настоящей статьи без пробела после номера.

Статья. 12.
Тело статьи с точкой после ключевого слова.
"""
    blocks = split_into_blocks(content)
    assert [ref for ref, _body in blocks] == [
        None,
        "Глава М",
        "Глава IV. Полномочия",
        "Глава IХ. Казначейство",
        "Глава Ⅱ. Неприкосновенность",
        "Статья 1Работники системы",
        "Статья. 12.",
    ]
    assert "Глава Мэрии округа" in blocks[0][1]
    assert "Статьями 5.7" in blocks[0][1]
    assert "Пункт может быть пропущен" in blocks[0][1]


def test_split_into_blocks_filters_wrapped_bare_cross_references():
    text = """3.1
Первое правило.
Лица, перечисленные в п.
3.5
, а также защищённые лица не подлежат досмотру.

3.2
Лица, не имеющие доступа по п.
3.4
или п.
3.5
настоящего кодекса, допускаются при служебной необходимости.

3.3
Третье правило.

3.4
Настоящий самостоятельный пункт 3.4.

3.5
Настоящий самостоятельный пункт 3.5.
"""
    blocks = split_into_blocks(text)
    assert [ref for ref, _body in blocks] == [
        "3.1",
        "3.2",
        "3.3",
        "3.4",
        "3.5",
    ]
    assert "3.5\n, а также защищённые лица" in blocks[0][1]
    assert "3.4\nили п.\n3.5\nнастоящего кодекса" in blocks[1][1]


def test_split_into_blocks_preserves_bare_header_before_genitive_without_connector():
    blocks = split_into_blocks(
        "1.1\nПервая норма.\n\n1.2\nнастоящего кодекса касается эта самостоятельная "
        "норма.\n\n1.3 Общие положения\n, заголовок с названием тоже нельзя "
        "фильтровать."
    )
    assert [ref for ref, _body in blocks] == [
        "1.1",
        "1.2",
        "1.3 Общие положения",
    ]


def test_split_into_blocks_keeps_wrapped_article_lists_inside_bare_owner():
    text = """5.1
Первая самостоятельная норма.

5.2
Вторая самостоятельная норма.

5.4
К лицам, совершившим преступления, предусмотренные
главой 14
, статьями
6.2, 6.3, 12.1 - 12.5, 12.11, 15.1, 16.2, 17.1
настоящего кодекса, сроки давности не применяются.

5.5
Лицо, добровольно сдавшее предметы, указанные в статьях
12.8, 12.8.1, 13.2, 13.3, 13.5, 13.6
настоящего кодекса, освобождается от уголовной ответственности.
"""
    blocks = split_into_blocks(text)

    assert [ref for ref, _body in blocks] == ["5.1", "5.2", "5.4", "5.5"]
    bodies = dict(blocks)
    assert "\n6.2, 6.3, 12.1 - 12.5" in bodies["5.4"]
    assert "\n12.8, 12.8.1, 13.2" in bodies["5.5"]


def test_split_into_blocks_keeps_single_wrapped_citations_inside_bare_owner():
    text = """5.1
Первая самостоятельная норма.

5.2
Объекты из статьи
5.5
обладающие особым статусом и находятся под охраной полиции.
5.2.1
Объекты из пунктов 1-2, статьи
5.5
обладающие особым статусом и находятся под охраной секретной службы.

5.3
Нахождение на охраняемой территории запрещено.

5.4
Ответственность наступает по статье
13.2
.
"""
    blocks = split_into_blocks(text)

    assert [ref for ref, _body in blocks] == ["5.1", "5.2", "5.3", "5.4"]
    bodies = dict(blocks)
    assert bodies["5.2"].count("\n5.5\n") == 2
    assert "\n13.2\n." in bodies["5.4"]


def test_split_into_blocks_filters_labeled_bare_cross_code_reference():
    text = """4.5
Первая самостоятельная норма.

4.6
Повторное нарушение переквалифицируется в преступление.
Исключение:
6.12АК

5.1
Следующая самостоятельная норма.
"""
    blocks = split_into_blocks(text)

    assert [ref for ref, _body in blocks] == ["4.5", "4.6", "5.1"]
    assert dict(blocks)["4.6"].endswith("Исключение:\n6.12АК")


def test_split_into_blocks_filters_short_passive_and_such_articles_references():
    text = """5.1
Первая самостоятельная норма.

5.2
Гражданам запрещена ловля в местах, которые не указаны в статьях
2.4
и 2.5 настоящего закона.

5.3
Запрос оформляется с доказательствами нарушения таких статей, как ст.
6.2, ст. 6.3 и ст. 7.1 УК
штата Сан-Андреас.

5.4
Последняя самостоятельная норма.
"""
    blocks = split_into_blocks(text)

    assert [ref for ref, _body in blocks] == ["5.1", "5.2", "5.3", "5.4"]
    bodies = dict(blocks)
    assert "\n2.4\nи 2.5" in bodies["5.2"]
    assert "\n6.2, ст. 6.3" in bodies["5.3"]


def test_split_into_blocks_preserves_real_bare_header_after_exception_label():
    blocks = split_into_blocks(
        "6.10\nПервая норма.\n\n6.11\nВторая норма.\nИсключение:\n\n"
        "6.12 Запрет доступа\nСамостоятельная норма.\n\n6.13\nСледующая норма."
    )

    assert [ref for ref, _body in blocks] == [
        "6.10",
        "6.11",
        "6.12 Запрет доступа",
        "6.13",
    ]


@pytest.mark.parametrize(
    "completed_previous_line",
    [
        "Все необходимые определения приведены в настоящей статье",
        "Решение принимается по этой статье",
        "Название следующей статьи",
    ],
)
def test_bare_reference_guard_does_not_consume_completed_prose(
    completed_previous_line,
):
    blocks = split_into_blocks(
        "1.1\nПервая норма.\n\n"
        f"1.2\n{completed_previous_line}\n\n"
        "1.3\nСамостоятельная следующая норма.\n\n"
        "1.4\nПоследняя самостоятельная норма."
    )

    assert [ref for ref, _body in blocks] == [
        "1.1",
        "1.2",
        "1.3",
        "1.4",
    ]


MIXED_SPECIAL_TEXT = """Статья 1. Общие положения
Общая норма.

Статья 5.9. Последняя статья общей части
Текст статьи 5.9.

Особенная часть
Раздел IV. Преступления
Глава 10. Преступления против собственности
10.6
.1
(F)
Разбойное ограбление
Текст статьи 10.6.1.

10.7
(F/R)
Неправомерное завладение транспортом
Текст статьи 10.7.

10.7.1
(F/R)
Неправомерное завладение специальным транспортом
Текст статьи 10.7.1.

Глава 11. Экономические преступления
11.1
(R)
Незаконная сделка
Текст статьи 11.1.

11.2
(
F
)
Принуждение к совершению сделки
Текст статьи 11.2.

11.3
(F/R)
Фиктивная сделка
Текст статьи 11.3.
"""


def test_mixed_special_part_supports_exact_and_wrapped_article_codes():
    blocks = split_into_blocks(MIXED_SPECIAL_TEXT)
    refs = [ref for ref, _body in blocks]

    assert refs.count("Статья 5.9. Последняя статья общей части") == 1
    assert "Статья 10.6.1" in refs
    assert "Статья 10.7" in refs
    assert "Статья 10.7.1" in refs
    assert "Статья 11.2" in refs
    assert "Статья 10.6" not in refs

    bodies = dict((ref, body) for ref, body in blocks if ref is not None)
    assert bodies["Статья 10.6.1"].startswith(
        "Статья 10.6.1\n(F)\nРазбойное ограбление"
    )
    assert bodies["Статья 11.2"].startswith(
        "Статья 11.2\n(F)\nПринуждение к совершению сделки"
    )
    assert bodies["Статья 10.6.1"].endswith(
        "Разбойное ограбление\nТекст статьи 10.6.1."
    )
    assert bodies["Статья 11.2"].endswith(
        "Принуждение к совершению сделки\nТекст статьи 11.2."
    )


def test_mixed_special_chunk_and_catalog_refs_share_exact_codes():
    blocks = split_into_blocks(MIXED_SPECIAL_TEXT)
    rows = catalog._catalog_rows(MIXED_SPECIAL_TEXT)
    chunks = chunk_law_text(MIXED_SPECIAL_TEXT, "Уголовный кодекс")

    block_refs = {ref for ref, _body in blocks}
    assert {chunk["article_ref"] for chunk in chunks} == block_refs
    assert {row["article_ref"] for row in rows} == block_refs

    articles = {
        row["code"]: row
        for row in rows
        if row["kind"] == "article"
    }
    assert {
        "1",
        "5.9",
        "10.6.1",
        "10.7",
        "10.7.1",
        "11.1",
        "11.2",
        "11.3",
    } <= set(articles)
    assert articles["10.6.1"]["title"] == "Разбойное ограбление"
    assert articles["11.2"]["title"] == "Принуждение к совершению сделки"


def test_mixed_special_does_not_recut_general_article_citations():
    general_body = """Статья 5.9. Освобождение от ответственности
Суд учитывает обстоятельства, перечисленные в п.
5.10
, а также основания из пунктов
5.11
и
5.12.1
настоящего кодекса.

Особенная часть"""
    content = general_body + """
Раздел IV. Преступления
Глава 6. Преступления против личности
6.1
(F)
Первая статья
6.2
(R)
Вторая статья
6.3
(F/R)
Третья статья
"""
    blocks = split_into_blocks(content)
    refs = [ref for ref, _body in blocks]
    rows = catalog._catalog_rows(content)
    article_59 = next(row for row in rows if row["code"] == "5.9")

    assert "Статья 5.10" not in refs
    assert "Статья 5.11" not in refs
    assert "Статья 5.12.1" not in refs
    assert ["Статья 6.1", "Статья 6.2", "Статья 6.3"] == [
        ref for ref in refs if ref and ref.startswith("Статья 6.")
    ]
    assert article_59["full_text"] == general_body
    assert article_59["article_hash"] == _article_hash(general_body)


@pytest.mark.parametrize(
    "special_part, expected_tail",
    [
        # No jurisdiction markers: ordinary numbered clauses stay in the chapter.
        ("""Глава 6
6.1
Первый пункт.
6.2
Второй пункт.
6.3
Третий пункт.""", ["Глава 6"]),
        # The number major must agree with the nearest structural chapter.
        ("""Глава 6
7.1
(F)
Первая норма.
7.2
(F)
Вторая норма.
7.3
(F)
Третья норма.""", ["Глава 6"]),
        # A later structural section means the chapter is no longer the nearest
        # structural header.
        ("""Глава 6
Раздел 7
6.1
(F)
Первая норма.
6.2
(F)
Вторая норма.
6.3
(F)
Третья норма.""", ["Глава 6", "Раздел 7"]),
        # A jurisdiction token must occupy its whole line, not prefix prose.
        ("""Глава 6
6.1
(F) ссылка
Первая норма.
6.2
(R) ссылка
Вторая норма.
6.3
(F/R) ссылка
Третья норма.""", ["Глава 6"]),
        # The nearest structural boundary must itself be the matching chapter.
        ("""Глава 6
Статья 2. Вставная структурная норма
6.1
(F)
Первая норма.
6.2
(R)
Вторая норма.
6.3
(F/R)
Третья норма.""", ["Глава 6", "Статья 2. Вставная структурная норма"]),
    ],
)
def test_mixed_special_rejects_ambiguous_numbered_lines(
    special_part,
    expected_tail,
):
    text = (
        "Статья 1. Общая норма\nТело.\n\n"
        f"Особенная часть\n{special_part}"
    )
    refs = [ref for ref, _body in split_into_blocks(text)]
    assert refs == ["Статья 1. Общая норма", *expected_tail]


def test_mixed_special_threshold_is_applied_per_chapter():
    text = """Статья 1. Общая норма
Тело.
Особенная часть
Глава 6
6.1
(F)
Первая норма.
6.2
(R)
Вторая норма.
Глава 7
7.1
(F)
Первая статья.
7.2
(R)
Вторая статья.
7.3
(F/R)
Третья статья.
"""
    refs = [ref for ref, _body in split_into_blocks(text)]
    assert "Статья 6.1" not in refs
    assert "Статья 6.2" not in refs
    assert ["Статья 7.1", "Статья 7.2", "Статья 7.3"] == [
        ref for ref in refs if ref and ref.startswith("Статья 7.")
    ]


MIXED_CLASSIFIED_CHAPTER_TAIL_TEXT = """Глава 1. Общая часть
Статья 1.1. Первая общая норма
Текст.
Статья 1.2. Вторая общая норма
Текст.

Глава 2. Назначение наказания
Статья 2.1. Первая норма о наказании
Текст.
Статья 2.2. Вторая норма о наказании
Текст.

Глава 3. Преступления против личности
3.1
(A, CR) Умышленное причинение вреда. | 3*
Выход под залог:
75.000$.
Наказание: лишение свободы.
3.2
(R) Причинение вреда по неосторожности. | 2*
Текст.
Выход под залог: 1$.
Наказание: штраф.
3.2.1
(A, CR) Повторное причинение вреда. | 3*
Текст.
Выход под залог: 1$.
Наказание: штраф.
3.3
(F. CR) Тяжкое причинение вреда. | 4*
Текст.
Выход под залог: 1$.
Наказание: штраф.

Глава 4. Преступления против собственности
4.1
(R, CR) Кража. | 3*
Текст.
Выход под залог: 1$.
Наказание: штраф.
4.2
(A,CR) Грабеж. | 4*
Текст.
Выход под залог: 1$.
Наказание: штраф.
4.3
(F, CR) Разбой. | 5*
Текст.
Выход под залог: 1$.
Наказание: штраф.

Глава 5. Преступления против порядка управления
Статья 5
Классификация специальных составов.
5.1
(R) Неподчинение. | 2*
Текст.
Выход под залог: 1$.
Наказание: штраф.
5.2
(A, CR) Побег. | 3*
Текст.
Выход под залог: 1$.
Наказание: штраф.
5.3
(F, CR) Воспрепятствование. | 4*
Текст.
Выход под залог: 1$.
Наказание: штраф.
"""


def test_mixed_classified_chapter_tail_recovers_only_exact_articles():
    plan = _header_match_plan(MIXED_CLASSIFIED_CHAPTER_TAIL_TEXT)
    blocks = split_into_blocks(MIXED_CLASSIFIED_CHAPTER_TAIL_TEXT)
    refs = [ref for ref, _body in blocks]

    assert plan.fallback_mode == "mixed_classified_chapter_tail"
    assert len(plan.mixed_special_candidates) == 10
    assert len(plan.accepted_mixed_special_candidates) == 10
    assert {
        f"Статья {major}.{minor}"
        for major in range(3, 6)
        for minor in range(1, 4)
    } <= set(refs)
    assert "Статья 3.2.1" in refs
    assert "Статья 75.000" not in refs

    bodies = dict((ref, body) for ref, body in blocks if ref is not None)
    assert bodies["Статья 3.1"].startswith(
        "Статья 3.1\n(A, CR) Умышленное причинение вреда"
    )
    # The optional root article remains its own row and is shortened only at
    # the first proven child article boundary.
    assert bodies["Статья 5"].endswith(
        "Классификация специальных составов."
    )


def test_mixed_classified_chapter_tail_requires_three_consecutive_chapters():
    text = MIXED_CLASSIFIED_CHAPTER_TAIL_TEXT.rsplit(
        "\nГлава 5.",
        maxsplit=1,
    )[0]
    plan = _header_match_plan(text)
    refs = [ref for ref, _body in split_into_blocks(text)]

    assert plan.fallback_mode is None
    assert not any(ref and ref.startswith("Статья 3.") for ref in refs)
    assert not any(ref and ref.startswith("Статья 4.") for ref in refs)


def test_mixed_classified_chapter_tail_rejects_gapped_sequences():
    text = MIXED_CLASSIFIED_CHAPTER_TAIL_TEXT.replace(
        "\n3.3\n",
        "\n3.4\n",
    ).replace(
        "\n4.3\n",
        "\n4.4\n",
    ).replace(
        "\n5.3\n",
        "\n5.4\n",
    )
    plan = _header_match_plan(text)
    refs = [ref for ref, _body in split_into_blocks(text)]

    assert plan.fallback_mode is None
    assert "Статья 3.1" not in refs
    assert "Статья 4.1" not in refs
    assert "Статья 5.1" not in refs


def test_mixed_classified_chapter_tail_rejects_valid_islands_around_gap():
    text = MIXED_CLASSIFIED_CHAPTER_TAIL_TEXT.replace(
        "\n4.3\n",
        "\n4.4\n",
    )
    plan = _header_match_plan(text)
    refs = [ref for ref, _body in split_into_blocks(text)]

    assert plan.fallback_mode is None
    # Chapters 3 and 5 are each complete, but accepting them around a broken
    # chapter 4 would create a silently partial catalog.
    assert "Статья 3.1" not in refs
    assert "Статья 4.1" not in refs
    assert "Статья 5.1" not in refs


def test_mixed_classified_chapter_tail_requires_inline_classification():
    text = (
        MIXED_CLASSIFIED_CHAPTER_TAIL_TEXT
        .replace("(A, CR) ", "")
        .replace("(R, CR) ", "")
        .replace("(A,CR) ", "")
        .replace("(F, CR) ", "")
        .replace("(F. CR) ", "")
        .replace("(R) ", "")
    )
    plan = _header_match_plan(text)
    refs = [ref for ref, _body in split_into_blocks(text)]

    assert plan.fallback_mode is None
    assert "Статья 3.1" not in refs
    assert "Статья 4.1" not in refs
    assert "Статья 5.1" not in refs


def test_pure_bare_three_level_numbers_remain_non_headers():
    text = """1.1.1
Первая часть.
1.2.1
Вторая часть.
1.3.1
Третья часть.
"""
    assert split_into_blocks(text) == [(None, text.strip())]


def test_chunker_and_catalog_segment_identically():
    """Ключ (article_ref) у чанков и статей каталога обязан совпадать — по нему связь."""
    blocks = split_into_blocks(LAW_TEXT)
    chunk_refs = {c["article_ref"] for c in chunk_law_text(LAW_TEXT, "Кодекс")}
    block_refs = {ref for ref, _ in blocks}
    assert chunk_refs == block_refs


# --- _parse_json_array -------------------------------------------------------------------------

def test_parse_json_array_plain():
    assert _parse_json_array('[{"id": 1}]') == [{"id": 1}]


def test_parse_json_array_fenced():
    raw = "```json\n[{\"id\": 2, \"synonyms\": [\"дм\"]}]\n```"
    assert _parse_json_array(raw) == [{"id": 2, "synonyms": ["дм"]}]


def test_parse_json_array_with_prose_around():
    raw = "Вот результат:\n[{\"id\": 3}]\nГотово."
    assert _parse_json_array(raw) == [{"id": 3}]


@pytest.mark.parametrize("raw", ["", "нет массива", "{\"id\": 1}"])
def test_parse_json_array_rejects_non_array(raw):
    with pytest.raises(ValueError):
        _parse_json_array(raw)


# --- stable article IDs ----------------------------------------------------------------------

def _match_row(
    *,
    ref,
    body,
    kind="article",
    code=None,
    title=None,
    source_order=0,
    article_id=None,
):
    row = {
        "article_ref": ref,
        "kind": kind,
        "code": code,
        "title": title,
        "full_text": body,
        "article_hash": _article_hash(body),
        "source_order": source_order,
    }
    if article_id is not None:
        row["id"] = article_id
    return row


def test_stable_match_preserves_id_for_metadata_only_parser_correction():
    source_body = (
        "Глава XI\n"
        "I\n"
        ". Исправленное название\n"
        "Неизменное содержимое главы."
    )
    old = [
        _match_row(
            ref="Глава XI",
            body=source_body,
            kind="chapter",
            code="XII",
            title="I",
            source_order=7,
            article_id=940,
        )
    ]
    new = [
        _match_row(
            ref="Глава XII. Исправленное название",
            body=source_body,
            kind="chapter",
            code="XII",
            title="Исправленное название",
            source_order=7,
        )
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[0]["id"] == 940
    assert ambiguous == []


def test_stable_match_uses_source_order_for_duplicate_metadata_corrections():
    source_body = "Упразднена."
    old = [
        _match_row(
            ref=f"Глава {index}. Старое название",
            body=source_body,
            kind="chapter",
            code="XII",
            title=f"Старое название {index}",
            source_order=index,
            article_id=940 + index,
        )
        for index in (7, 8)
    ]
    new = [
        _match_row(
            ref=f"Глава XII. Новое название {index}",
            body=source_body,
            kind="chapter",
            code="XII",
            title=f"Новое название {index}",
            source_order=index,
        )
        for index in (7, 8)
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] for row in matched] == [947, 948]
    assert ambiguous == []


def test_stable_match_split_chapter_keeps_owner_id_between_exact_anchors():
    left = _match_row(
        ref="Статья 1. Левый якорь",
        body="Статья 1. Левый якорь\nНеизменный подробный текст слева.",
        code="1",
        title="Левый якорь",
        source_order=0,
        article_id=950,
    )
    owner_prefix = (
        "Статья 2. Основная норма\n"
        "Неизменный подробный текст основной нормы."
    )
    broken_chapter = (
        "ГЛАВ\n"
        "А 2\n"
        ".\n"
        "ОБЩИЕ ПОЛОЖЕНИЯ\n"
        "Содержимое восстановленной структурной главы."
    )
    old_owner = _match_row(
        ref="Статья 2. Основная норма",
        body=f"{owner_prefix}\n{broken_chapter}",
        code="2",
        title="Основная норма",
        source_order=1,
        article_id=951,
    )
    right = _match_row(
        ref="Статья 3. Правый якорь",
        body="Статья 3. Правый якорь\nНеизменный подробный текст справа.",
        code="3",
        title="Правый якорь",
        source_order=2,
        article_id=952,
    )
    old = [left, old_owner, right]
    new = [
        {key: value for key, value in left.items() if key != "id"},
        _match_row(
            ref="Статья 2. Основная норма",
            body=owner_prefix,
            code="2",
            title="Основная норма",
            source_order=1,
        ),
        _match_row(
            ref="Глава 2.",
            # The stable matcher deliberately tolerates either raw source bytes
            # or the parser's exact canonical keyword layout.
            body=(
                "Глава 2.\n"
                "ОБЩИЕ ПОЛОЖЕНИЯ\n"
                "Содержимое восстановленной структурной главы."
            ),
            kind="chapter",
            code="2",
            title="ОБЩИЕ ПОЛОЖЕНИЯ",
            source_order=2,
        ),
        {
            **{key: value for key, value in right.items() if key != "id"},
            "source_order": 3,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        950,
        951,
        None,
        952,
    ]
    assert ambiguous == []


def _split_keyword_stable_fixture():
    duplicate = _match_row(
        ref="Статья 2.",
        body=(
            "Статья 2.\n"
            "Другая норма с тем же номером и достаточно длинным неизменным текстом."
        ),
        code="2",
        source_order=0,
        article_id=900,
    )
    left_anchor = _match_row(
        ref="Статья 1. Левый якорь",
        body="Статья 1. Левый якорь\nНеизменный достаточно длинный текст слева.",
        code="1",
        title="Левый якорь",
        source_order=1,
        article_id=901,
    )
    candidate_prefix = (
        "Статья 2.\n"
        "Основной достаточно длинный текст второй статьи до повреждённой границы."
    )
    recovered = (
        "Статья 3. Третья норма\n"
        "Самостоятельный достаточно длинный текст восстановленной третьей статьи."
    )
    old_candidate = _match_row(
        ref="Статья 2.",
        body=(
            f"{candidate_prefix}\n"
            "Стать\n"
            "я 3. Третья норма\n"
            "Самостоятельный достаточно длинный текст восстановленной третьей статьи."
        ),
        code="2",
        source_order=2,
        article_id=902,
    )
    right_anchor = _match_row(
        ref="Статья 4. Правый якорь",
        body="Статья 4. Правый якорь\nНеизменный достаточно длинный текст справа.",
        code="4",
        title="Правый якорь",
        source_order=3,
        article_id=903,
    )
    old = [duplicate, left_anchor, old_candidate, right_anchor]
    new = [
        {key: value for key, value in duplicate.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="Статья 2.",
            body=candidate_prefix,
            code="2",
            source_order=2,
        ),
        _match_row(
            ref="Статья 3. Третья норма",
            body=recovered,
            code="3",
            title="Третья норма",
            source_order=3,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 4,
        },
    ]
    return old, new


def test_stable_match_split_keyword_tail_keeps_preceding_id():
    old, new = _split_keyword_stable_fixture()

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        900,
        901,
        902,
        None,
        903,
    ]
    assert ambiguous == []


def test_stable_match_split_keyword_tail_supports_multiple_articles():
    old, new = _split_keyword_stable_fixture()
    old[2] = _match_row(
        ref="Статья 2.",
        body=(
            f"{new[2]['full_text']}\n"
            "Стать\n"
            "я 3. Третья норма\n"
            "Самостоятельный достаточно длинный текст восстановленной третьей статьи.\n"
            "С\n"
            "татья 3.1. Дочерняя норма\n"
            "Самостоятельный достаточно длинный текст восстановленной дочерней статьи."
        ),
        code="2",
        source_order=2,
        article_id=902,
    )
    new.insert(
        4,
        _match_row(
            ref="Статья 3.1. Дочерняя норма",
            body=(
                "Статья 3.1. Дочерняя норма\n"
                "Самостоятельный достаточно длинный текст восстановленной дочерней статьи."
            ),
            code="3.1",
            title="Дочерняя норма",
            source_order=4,
        ),
    )
    new[5]["source_order"] = 5

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        900,
        901,
        902,
        None,
        None,
        903,
    ]
    assert ambiguous == []


def test_stable_match_split_keyword_tail_requires_two_exact_anchors():
    old, new = _split_keyword_stable_fixture()
    old.pop()
    new.pop()

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[2] is None
    assert matched[3] is None
    assert 2 in ambiguous


def test_stable_match_split_keyword_tail_rejects_unproven_article_insert():
    old, new = _split_keyword_stable_fixture()
    old[2]["full_text"] = f"{new[2]['full_text']}\n{new[3]['full_text']}"
    old[2]["article_hash"] = _article_hash(old[2]["full_text"])

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[2] is None
    assert matched[3] is None
    assert 2 in ambiguous


def test_stable_match_split_keyword_tail_rejects_ref_mismatch():
    old, new = _split_keyword_stable_fixture()
    new[3]["article_ref"] = "Статья 30. Другая норма"
    new[3]["code"] = "30"

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[2] is None
    assert matched[3] is None
    assert 2 in ambiguous


def test_stable_match_split_keyword_tail_rejects_unexplained_text_change():
    old, new = _split_keyword_stable_fixture()
    new[3]["full_text"] += "\nНовый необъяснимый текст."
    new[3]["article_hash"] = _article_hash(new[3]["full_text"])

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[2] is None
    assert matched[3] is None
    assert 2 in ambiguous


def test_stable_match_split_keyword_tail_rejects_crossed_anchors():
    old, new = _split_keyword_stable_fixture()
    new = [new[0], new[4], new[2], new[3], new[1]]
    for source_order, row in enumerate(new):
        row["source_order"] = source_order

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[2] is None
    assert matched[3] is None
    assert 2 in ambiguous


def test_stable_match_identical_rebuild_keeps_duplicate_ids():
    old = [
        _match_row(
            ref="Статья 1.",
            body="Статья 1.\nПервое повторяющееся положение с достаточно длинным телом.",
            code="1",
            source_order=0,
            article_id=101,
        ),
        _match_row(
            ref="Статья 1.",
            body="Статья 1.\nВторое повторяющееся положение с достаточно длинным телом.",
            code="1",
            source_order=1,
            article_id=102,
        ),
    ]
    new = [{key: value for key, value in row.items() if key != "id"} for row in old]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [101, 102]
    assert ambiguous == []


def test_stable_match_merges_false_non_article_boundary_between_exact_anchors():
    duplicate_before = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nДругая повторяющаяся норма до исправляемого блока.",
        code="1",
        source_order=0,
        article_id=801,
    )
    left_anchor = _match_row(
        ref="Статья 8. Якорь слева",
        body="Статья 8. Якорь слева\nНеизменное содержимое левого якоря.",
        code="8",
        title="Якорь слева",
        source_order=1,
        article_id=802,
    )
    candidate = _match_row(
        ref="Статья 1.",
        body="Статья 1.",
        code="1",
        source_order=2,
        article_id=803,
    )
    false_chapter = _match_row(
        ref="Глава Министерства финансов",
        body=(
            "Глава Министерства финансов\n"
            "Содержимое, ошибочно отделённое ложной границей."
        ),
        kind="chapter",
        code=None,
        source_order=3,
        article_id=804,
    )
    right_anchor = _match_row(
        ref="Статья 9. Якорь справа",
        body="Статья 9. Якорь справа\nНеизменное содержимое правого якоря.",
        code="9",
        title="Якорь справа",
        source_order=4,
        article_id=805,
    )
    duplicate_after = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nЕщё одна повторяющаяся норма после исправляемого блока.",
        code="1",
        source_order=5,
        article_id=806,
    )
    old = [
        duplicate_before,
        left_anchor,
        candidate,
        false_chapter,
        right_anchor,
        duplicate_after,
    ]
    new = [
        {key: value for key, value in duplicate_before.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="Статья 1.",
            body=f"{candidate['full_text']}\n{false_chapter['full_text']}",
            code="1",
            source_order=2,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 3,
        },
        {
            **{key: value for key, value in duplicate_after.items() if key != "id"},
            "source_order": 4,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        801,
        802,
        803,
        805,
        806,
    ]
    assert ambiguous == []


def test_stable_match_splits_true_non_article_boundary_between_exact_anchors():
    duplicate_before = _match_row(
        ref="Статья 2.",
        body="Статья 2.\nДругая повторяющаяся норма до исправляемого блока.",
        code="2",
        source_order=0,
        article_id=811,
    )
    left_anchor = _match_row(
        ref="Статья 7. Якорь слева",
        body="Статья 7. Якорь слева\nНеизменное содержимое левого якоря.",
        code="7",
        title="Якорь слева",
        source_order=1,
        article_id=812,
    )
    article_text = "Статья 2.\nОсновное содержимое исправляемой статьи."
    chapter_text = "Глава Х\nТекст настоящей структурной главы."
    combined = _match_row(
        ref="Статья 2.",
        body=f"{article_text}\n{chapter_text}",
        code="2",
        source_order=2,
        article_id=813,
    )
    right_anchor = _match_row(
        ref="Статья 8. Якорь справа",
        body="Статья 8. Якорь справа\nНеизменное содержимое правого якоря.",
        code="8",
        title="Якорь справа",
        source_order=3,
        article_id=814,
    )
    duplicate_after = _match_row(
        ref="Статья 2.",
        body="Статья 2.\nЕщё одна повторяющаяся норма после исправляемого блока.",
        code="2",
        source_order=4,
        article_id=815,
    )
    old = [
        duplicate_before,
        left_anchor,
        combined,
        right_anchor,
        duplicate_after,
    ]
    new = [
        {key: value for key, value in duplicate_before.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="Статья 2.",
            body=article_text,
            code="2",
            source_order=2,
        ),
        _match_row(
            ref="Глава Х",
            body=chapter_text,
            kind="chapter",
            code="Х",
            source_order=3,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 4,
        },
        {
            **{key: value for key, value in duplicate_after.items() if key != "id"},
            "source_order": 5,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        811,
        812,
        813,
        None,
        814,
        815,
    ]
    assert ambiguous == []


def test_stable_match_boundary_repair_requires_two_exact_anchors():
    candidate = _match_row(
        ref="Статья 1.",
        body="Статья 1.",
        code="1",
        source_order=0,
        article_id=821,
    )
    false_chapter = _match_row(
        ref="Глава Департамента",
        body="Глава Департамента\nОшибочно отделённое содержимое.",
        kind="chapter",
        source_order=1,
        article_id=822,
    )
    right_anchor = _match_row(
        ref="Статья 9. Якорь",
        body="Статья 9. Якорь\nНеизменный текст.",
        code="9",
        title="Якорь",
        source_order=2,
        article_id=823,
    )
    duplicate = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nДругая статья с тем же номером.",
        code="1",
        source_order=3,
        article_id=824,
    )
    old = [candidate, false_chapter, right_anchor, duplicate]
    new = [
        _match_row(
            ref="Статья 1.",
            body=f"{candidate['full_text']}\n{false_chapter['full_text']}",
            code="1",
            source_order=0,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 1,
        },
        {
            **{key: value for key, value in duplicate.items() if key != "id"},
            "source_order": 2,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [None, 823, 824]
    assert ambiguous == [0]


def test_stable_match_boundary_repair_rejects_extra_article():
    duplicate_before = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nДругая повторяющаяся норма.",
        code="1",
        source_order=0,
        article_id=831,
    )
    left_anchor = _match_row(
        ref="Статья 8. Слева",
        body="Статья 8. Слева\nНеизменный текст.",
        code="8",
        title="Слева",
        source_order=1,
        article_id=832,
    )
    candidate = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nПервая норма.",
        code="1",
        source_order=2,
        article_id=833,
    )
    removed_article = _match_row(
        ref="Статья 3.",
        body="Статья 3.\nДругая настоящая норма.",
        code="3",
        source_order=3,
        article_id=834,
    )
    right_anchor = _match_row(
        ref="Статья 9. Справа",
        body="Статья 9. Справа\nНеизменный текст.",
        code="9",
        title="Справа",
        source_order=4,
        article_id=835,
    )
    old = [
        duplicate_before,
        left_anchor,
        candidate,
        removed_article,
        right_anchor,
    ]
    new = [
        {key: value for key, value in duplicate_before.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="Статья 1.",
            body=f"{candidate['full_text']}\n{removed_article['full_text']}",
            code="1",
            source_order=2,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 3,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        831,
        832,
        None,
        835,
    ]
    assert ambiguous == [2]


def test_stable_match_merges_proven_bare_citation_article_boundary():
    duplicate_before = _match_row(
        ref="3.9",
        body="3.9\n.9\nДругая норма с тем же повторяющимся номером.",
        code="3.9",
        source_order=0,
        article_id=841,
    )
    left_anchor = _match_row(
        ref="3.8",
        body="3.8\nНеизменный левый якорь.",
        code="3.8",
        source_order=1,
        article_id=842,
    )
    owner = _match_row(
        ref="3.9",
        body=(
            "3.9\n"
            ".10\n"
            "Запрос оформляется с доказательствами нарушения таких "
            "статей, как ст."
        ),
        code="3.9",
        source_order=2,
        article_id=843,
    )
    false_citation = _match_row(
        ref="6.2, ст. 6.3, ст. 7.1 УК",
        body=(
            "6.2, ст. 6.3, ст. 7.1 УК\n"
            "штата Сан-Андреас."
        ),
        code="6.2",
        title=", ст. 6.3, ст. 7.1 УК",
        source_order=3,
        article_id=844,
    )
    right_anchor = _match_row(
        ref="3.10",
        body="3.10\nНеизменный правый якорь.",
        code="3.10",
        source_order=4,
        article_id=845,
    )
    duplicate_after = _match_row(
        ref="3.9",
        body="3.9\n.11\nЕщё одна норма с тем же номером.",
        code="3.9",
        source_order=5,
        article_id=846,
    )
    old = [
        duplicate_before,
        left_anchor,
        owner,
        false_citation,
        right_anchor,
        duplicate_after,
    ]
    new = [
        {key: value for key, value in duplicate_before.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="3.9",
            body=f"{owner['full_text']}\n{false_citation['full_text']}",
            code="3.9",
            source_order=2,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 3,
        },
        {
            **{key: value for key, value in duplicate_after.items() if key != "id"},
            "source_order": 4,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        841,
        842,
        843,
        845,
        846,
    ]
    assert ambiguous == []


def test_stable_match_does_not_merge_unproven_bare_article_boundary():
    duplicate_before = _match_row(
        ref="3.9",
        body="3.9\nДругая норма с повторяющимся номером.",
        code="3.9",
        source_order=0,
        article_id=851,
    )
    left_anchor = _match_row(
        ref="3.8",
        body="3.8\nНеизменный левый якорь.",
        code="3.8",
        source_order=1,
        article_id=852,
    )
    owner = _match_row(
        ref="3.9",
        body="3.9\nПолностью завершённая самостоятельная норма.",
        code="3.9",
        source_order=2,
        article_id=853,
    )
    real_article = _match_row(
        ref="6.2",
        body="6.2\nСамостоятельная новая норма.",
        code="6.2",
        source_order=3,
        article_id=854,
    )
    right_anchor = _match_row(
        ref="3.10",
        body="3.10\nНеизменный правый якорь.",
        code="3.10",
        source_order=4,
        article_id=855,
    )
    old = [
        duplicate_before,
        left_anchor,
        owner,
        real_article,
        right_anchor,
    ]
    new = [
        {key: value for key, value in duplicate_before.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="3.9",
            body=f"{owner['full_text']}\n{real_article['full_text']}",
            code="3.9",
            source_order=2,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 3,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        851,
        852,
        None,
        855,
    ]
    assert ambiguous == [2]


def test_stable_match_boundary_repair_rejects_unexplained_text_change():
    duplicate_before = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nДругая повторяющаяся норма.",
        code="1",
        source_order=0,
        article_id=841,
    )
    left_anchor = _match_row(
        ref="Статья 8. Слева",
        body="Статья 8. Слева\nНеизменный текст.",
        code="8",
        title="Слева",
        source_order=1,
        article_id=842,
    )
    candidate = _match_row(
        ref="Статья 1.",
        body="Статья 1.",
        code="1",
        source_order=2,
        article_id=843,
    )
    false_chapter = _match_row(
        ref="Глава Департамента",
        body="Глава Департамента\nСтарое содержимое.",
        kind="chapter",
        source_order=3,
        article_id=844,
    )
    right_anchor = _match_row(
        ref="Статья 9. Справа",
        body="Статья 9. Справа\nНеизменный текст.",
        code="9",
        title="Справа",
        source_order=4,
        article_id=845,
    )
    old = [
        duplicate_before,
        left_anchor,
        candidate,
        false_chapter,
        right_anchor,
    ]
    new = [
        {key: value for key, value in duplicate_before.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="Статья 1.",
            body=(
                f"{candidate['full_text']}\n"
                "Глава Департамента\nПолностью переписанное содержимое."
            ),
            code="1",
            source_order=2,
        ),
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 3,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        841,
        842,
        None,
        845,
    ]
    assert ambiguous == [2]


def test_stable_match_boundary_repair_rejects_crossed_exact_anchors():
    left_anchor = _match_row(
        ref="Статья 8. Левый якорь",
        body="Статья 8. Левый якорь\nНеизменное содержимое.",
        code="8",
        title="Левый якорь",
        source_order=0,
        article_id=851,
    )
    candidate = _match_row(
        ref="Статья 1.",
        body="Статья 1.",
        code="1",
        source_order=1,
        article_id=852,
    )
    false_chapter = _match_row(
        ref="Глава Департамента",
        body="Глава Департамента\nОшибочно отделённое содержимое.",
        kind="chapter",
        source_order=2,
        article_id=853,
    )
    right_anchor = _match_row(
        ref="Статья 9. Правый якорь",
        body="Статья 9. Правый якорь\nНеизменное содержимое.",
        code="9",
        title="Правый якорь",
        source_order=3,
        article_id=854,
    )
    duplicate = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nДругая статья с тем же номером.",
        code="1",
        source_order=4,
        article_id=855,
    )
    old = [left_anchor, candidate, false_chapter, right_anchor, duplicate]
    new = [
        {
            **{key: value for key, value in right_anchor.items() if key != "id"},
            "source_order": 0,
        },
        _match_row(
            ref="Статья 1.",
            body=f"{candidate['full_text']}\n{false_chapter['full_text']}",
            code="1",
            source_order=1,
        ),
        {
            **{key: value for key, value in left_anchor.items() if key != "id"},
            "source_order": 2,
        },
        {
            **{key: value for key, value in duplicate.items() if key != "id"},
            "source_order": 3,
        },
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        854,
        None,
        851,
        855,
    ]
    assert ambiguous == [1]


def test_stable_match_boundary_repair_rejects_extras_on_both_sides():
    duplicate_before = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nДругая повторяющаяся норма.",
        code="1",
        source_order=0,
        article_id=861,
    )
    left_anchor = _match_row(
        ref="Статья 8. Слева",
        body="Статья 8. Слева\nНеизменный текст.",
        code="8",
        title="Слева",
        source_order=1,
        article_id=862,
    )
    old_candidate = _match_row(
        ref="Статья 1.",
        body="Статья 1.\nПервая часть нормы.",
        code="1",
        source_order=2,
        article_id=863,
    )
    old_extra = _match_row(
        ref="Глава Х",
        body="Глава Х\nВторая часть нормы.",
        kind="chapter",
        code="Х",
        source_order=3,
        article_id=864,
    )
    right_anchor = _match_row(
        ref="Статья 9. Справа",
        body="Статья 9. Справа\nНеизменный текст.",
        code="9",
        title="Справа",
        source_order=4,
        article_id=865,
    )
    old = [
        duplicate_before,
        left_anchor,
        old_candidate,
        old_extra,
        right_anchor,
    ]
    new = [
        {key: value for key, value in duplicate_before.items() if key != "id"},
        {key: value for key, value in left_anchor.items() if key != "id"},
        _match_row(
            ref="Статья 1.",
            body="Статья 1.",
            code="1",
            source_order=2,
        ),
        _match_row(
            ref="Приложение",
            body="Первая часть нормы.\nГлава Х\nВторая часть нормы.",
            kind="other",
            source_order=3,
        ),
        {key: value for key, value in right_anchor.items() if key != "id"},
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [
        861,
        862,
        None,
        None,
        865,
    ]
    assert ambiguous == [2]


def test_stable_match_body_edit_with_unique_header_keeps_id():
    old = [_match_row(
        ref="Статья 12.1 Халатность",
        body="Статья 12.1 Халатность\nСтарый текст и старая санкция.",
        code="12.1",
        title="Халатность",
        article_id=201,
    )]
    new = [_match_row(
        ref="Статья 12.1 Халатность",
        body="Статья 12.1 Халатность\nПолностью обновлённый текст и новая санкция.",
        code="12.1",
        title="Халатность",
    )]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[0]["id"] == 201
    assert ambiguous == []


def test_stable_match_insert_does_not_shift_existing_ids():
    old = [
        _match_row(
            ref="Статья 1 Первая",
            body="Статья 1 Первая\nСодержательное тело первой статьи.",
            code="1",
            title="Первая",
            source_order=0,
            article_id=301,
        ),
        _match_row(
            ref="Статья 2 Вторая",
            body="Статья 2 Вторая\nСодержательное тело второй статьи.",
            code="2",
            title="Вторая",
            source_order=1,
            article_id=302,
        ),
    ]
    new = [
        _match_row(
            ref="Статья 0 Новая",
            body="Статья 0 Новая\nСовершенно новая вставленная статья.",
            code="0",
            title="Новая",
            source_order=0,
        ),
        {**{key: value for key, value in old[0].items() if key != "id"}, "source_order": 1},
        {**{key: value for key, value in old[1].items() if key != "id"}, "source_order": 2},
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert [row["id"] if row else None for row in matched] == [None, 301, 302]
    assert ambiguous == []


def test_stable_match_renumbered_article_uses_unchanged_body_anchor():
    old = [_match_row(
        ref="Статья 5 Старое название",
        body=(
            "Статья 5 Старое название\n"
            "Лицо обязано исполнить требование закона в установленный срок."
        ),
        code="5",
        title="Старое название",
        article_id=401,
    )]
    new = [_match_row(
        ref="Статья 8 Новое название",
        body=(
            "Статья 8 Новое название\n"
            "Лицо обязано исполнить требование закона в установленный срок."
        ),
        code="8",
        title="Новое название",
    )]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[0]["id"] == 401
    assert ambiguous == []


def test_stable_match_does_not_guess_edited_duplicate_articles():
    old = [
        _match_row(
            ref="Статья 1.",
            body="Статья 1.\nПервый старый и достаточно длинный самостоятельный текст нормы.",
            code="1",
            title="Общие положения",
            source_order=0,
            article_id=501,
        ),
        _match_row(
            ref="Статья 1.",
            body="Статья 1.\nВторой старый и достаточно длинный самостоятельный текст нормы.",
            code="1",
            title="Общие положения",
            source_order=1,
            article_id=502,
        ),
    ]
    new = [
        _match_row(
            ref="Статья 1.",
            body="Статья 1.\nСовершенно новый текст первого объединённого правового акта.",
            code="1",
            title="Общие положения",
            source_order=0,
        ),
        _match_row(
            ref="Статья 1.",
            body="Статья 1.\nСовершенно новый текст второго объединённого правового акта.",
            code="1",
            title="Общие положения",
            source_order=1,
        ),
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched == [None, None]
    assert ambiguous == [0, 1]


def test_stable_match_unique_uncoded_document_survives_content_edit():
    old = [_match_row(
        ref=None,
        body="Старый неструктурированный документ.",
        kind="other",
        article_id=601,
    )]
    new = [_match_row(
        ref=None,
        body="Новая полностью переписанная версия неструктурированного документа.",
        kind="other",
    )]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched[0]["id"] == 601
    assert ambiguous == []


def test_stable_match_conflicting_unique_anchors_do_not_swap_favorite_ids():
    old = [
        _match_row(
            ref="Статья 1 Альфа",
            body="Статья 1 Альфа\nСтарое самостоятельное содержание первой нормы.",
            code="1",
            title="Альфа",
            source_order=0,
            article_id=701,
        ),
        _match_row(
            ref="Статья 2 Бета",
            body="Статья 2 Бета\nСтарое самостоятельное содержание второй нормы.",
            code="2",
            title="Бета",
            source_order=1,
            article_id=702,
        ),
    ]
    new = [
        _match_row(
            ref="Статья 1 Бета",
            body="Статья 1 Бета\nПолностью переписанное содержание первой нормы.",
            code="1",
            title="Бета",
            source_order=0,
        ),
        _match_row(
            ref="Статья 2 Альфа",
            body="Статья 2 Альфа\nПолностью переписанное содержание второй нормы.",
            code="2",
            title="Альфа",
            source_order=1,
        ),
    ]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched == [None, None]
    assert ambiguous == [0, 1]


def test_stable_match_conflicting_body_and_code_do_not_reuse_id():
    old = [
        _match_row(
            ref="Статья 3 Первая",
            body="Статья 3 Первая\nУникальный длинный текст первой правовой нормы без изменений.",
            code="3",
            title="Первая",
            article_id=703,
        ),
        _match_row(
            ref="Статья 4 Вторая",
            body="Статья 4 Вторая\nУникальный длинный текст второй правовой нормы без изменений.",
            code="4",
            title="Вторая",
            source_order=1,
            article_id=704,
        ),
    ]
    new = [_match_row(
        ref="Статья 4 Новая",
        body="Статья 4 Новая\nУникальный длинный текст первой правовой нормы без изменений.",
        code="4",
        title="Новая",
    )]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched == [None]
    assert ambiguous == [0]


def test_stable_match_kind_change_gets_new_id():
    tail = "Достаточно длинное содержимое блока, тип которого изменился после разбора."
    old = [_match_row(
        ref="Статья 5 Порядок",
        body=f"Статья 5 Порядок\n{tail}",
        kind="article",
        code="5",
        title="Порядок",
        article_id=705,
    )]
    new = [_match_row(
        ref="Глава 5 Порядок",
        body=f"Глава 5 Порядок\n{tail}",
        kind="chapter",
        code="5",
        title="Порядок",
    )]

    matched, ambiguous = _match_existing_articles(new, old)

    assert matched == [None]
    assert ambiguous == []


class _CatalogConnection:
    def __init__(self, old_rows, *, update_id=None):
        self.old_rows = old_rows
        self.update_id = update_id
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if "FROM law_articles" in query and "FOR UPDATE" in query:
            return self.old_rows
        if "SELECT DISTINCT affected.space_id" in query:
            return []
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if query.lstrip().startswith("UPDATE law_articles"):
            return self.update_id
        if query.lstrip().startswith("INSERT INTO law_articles"):
            return 999
        raise AssertionError(query)

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if "SELECT id, article_hash, enrich_hash" in query:
            if self.update_id is None:
                return None
            return {
                "id": self.update_id,
                "article_hash": args[2],
                "enrich_hash": args[2],
            }
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "DELETE 1"


def _install_catalog_transaction(monkeypatch, conn):
    @asynccontextmanager
    async def transaction():
        yield conn

    monkeypatch.setattr(catalog.db_manager, "transaction", transaction)


@pytest.mark.asyncio
async def test_document_reconcile_updates_existing_id_and_clears_only_auto_enrichment(monkeypatch):
    old_body = "Статья 7 Халатность\nСтарое подробное описание нарушения и наказания."
    old_hash = _article_hash(old_body)
    old = {
        "id": 707,
        "law_document_id": 70,
        "server_id": "memphis",
        "source_order": 0,
        "article_hash": old_hash,
        "article_ref": "Статья 7 Халатность",
        "kind": "article",
        "code": "7",
        "title": "Халатность",
        "doc_title": "УК",
        "full_text": old_body,
        "penalty_summary": "старая санкция",
        "penalty_json": '{"old": true}',
        "enrich_hash": old_hash,
    }
    conn = _CatalogConnection([old], update_id=707)
    _install_catalog_transaction(monkeypatch, conn)
    ensured = []

    async def ensure(_conn, article_id):
        ensured.append(article_id)
        return True

    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)
    new_body = "Статья 7 Халатность\nНовый подробный текст нарушения и новая санкция."

    count = await catalog.build_catalog_for_document({
        "id": 70,
        "server_id": "memphis",
        "title": "УК",
        "content": new_body,
    })

    assert count == 1
    assert ensured == [707]
    assert any(
        kind == "fetchval"
        and query.lstrip().startswith("UPDATE law_articles")
        and args[-1] == 707
        for kind, query, args in conn.calls
    )
    assert not any(
        kind == "fetchval" and query.lstrip().startswith("INSERT INTO law_articles")
        for kind, query, _args in conn.calls
    )
    auto_deactivations = [
        (query, args) for kind, query, args in conn.calls
        if kind == "execute"
        and "UPDATE law_synonyms SET status = 'stale'" in query
    ]
    assert len(auto_deactivations) == 1
    assert "source = 'auto'" in auto_deactivations[0][0]
    assert "status = 'approved'" in auto_deactivations[0][0]
    assert auto_deactivations[0][1] == (707,)
    assert not any(
        kind == "execute"
        and "DELETE FROM law_articles WHERE law_document_id = $1" in query
        and len(args) == 1
        for kind, query, args in conn.calls
    )


@pytest.mark.asyncio
async def test_identical_document_reconcile_keeps_children_and_creates_no_new_article(monkeypatch):
    body = "Статья 9 Кража\nПодробное неизменившееся описание состава и санкции."
    article_hash = _article_hash(body)
    old = {
        "id": 909,
        "law_document_id": 90,
        "server_id": "denver",
        "source_order": 0,
        "article_hash": article_hash,
        "article_ref": "Статья 9 Кража",
        "kind": "article",
        "code": "9",
        "title": "Кража",
        "doc_title": "УК",
        "full_text": body,
        "penalty_summary": "лишение свободы",
        "penalty_json": '{"prison": "есть"}',
        "enrich_hash": article_hash,
    }
    conn = _CatalogConnection([old], update_id=909)
    _install_catalog_transaction(monkeypatch, conn)
    ensured = []

    async def ensure(_conn, article_id):
        ensured.append(article_id)
        return False

    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)

    count = await catalog.build_catalog_for_document({
        "id": 90,
        "server_id": "denver",
        "title": "УК",
        "content": body,
    })

    assert count == 1
    assert ensured == [909]
    assert not any(
        kind == "fetchval" and query.lstrip().startswith("INSERT INTO law_articles")
        for kind, query, _args in conn.calls
    )
    assert not any(
        kind == "execute"
        and (
            "DELETE FROM law_synonyms" in query
            or "DELETE FROM law_article_embeddings" in query
            or "DELETE FROM law_embedding_jobs" in query
        )
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enrich_hash", "expected_enrichment", "auto_synonyms_deactivated"),
    [
        (None, ("лишение свободы", '{"prison": "есть"}', None), False),
        ("stale-hash", (None, None, None), True),
    ],
)
async def test_identical_document_handles_legacy_and_stale_enrichment(
    monkeypatch,
    enrich_hash,
    expected_enrichment,
    auto_synonyms_deactivated,
):
    body = "Статья 10 Угон\nНеизменившееся подробное описание нарушения и наказания."
    article_hash = _article_hash(body)
    old = {
        "id": 910,
        "law_document_id": 91,
        "server_id": "denver",
        "source_order": 0,
        "article_hash": article_hash,
        "article_ref": "Статья 10 Угон",
        "kind": "article",
        "code": "10",
        "title": "Угон",
        "doc_title": "УК",
        "full_text": body,
        "penalty_summary": "лишение свободы",
        "penalty_json": '{"prison": "есть"}',
        "enrich_hash": enrich_hash,
    }
    conn = _CatalogConnection([old], update_id=910)
    _install_catalog_transaction(monkeypatch, conn)

    async def ensure(_conn, _article_id):
        return False

    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)

    count = await catalog.build_catalog_for_document({
        "id": 91,
        "server_id": "denver",
        "title": "УК",
        "content": body,
    })

    assert count == 1
    update_args = next(
        args for kind, query, args in conn.calls
        if kind == "fetchval" and query.lstrip().startswith("UPDATE law_articles")
    )
    assert update_args[10:13] == expected_enrichment
    assert update_args[13] is auto_synonyms_deactivated
    assert any(
        kind == "execute"
        and "UPDATE law_synonyms SET status = 'stale'" in query
        and "status = 'approved'" in query
        for kind, query, _args in conn.calls
    ) is auto_synonyms_deactivated


@pytest.mark.asyncio
async def test_document_reconcile_deletes_only_truly_missing_article(monkeypatch):
    kept_body = "Статья 1 Первая\nПервая действующая норма с подробным текстом."
    removed_body = "Статья 2 Вторая\nВторая удалённая норма с подробным текстом."

    def old_row(article_id, source_order, ref, code, title, body):
        article_hash = _article_hash(body)
        return {
            "id": article_id,
            "law_document_id": 100,
            "server_id": "orlando",
            "source_order": source_order,
            "article_hash": article_hash,
            "article_ref": ref,
            "kind": "article",
            "code": code,
            "title": title,
            "doc_title": "Кодекс",
            "full_text": body,
            "penalty_summary": None,
            "penalty_json": None,
            "enrich_hash": article_hash,
        }

    old = [
        old_row(1001, 0, "Статья 1 Первая", "1", "Первая", kept_body),
        old_row(1002, 1, "Статья 2 Вторая", "2", "Вторая", removed_body),
    ]
    conn = _CatalogConnection(old, update_id=1001)
    _install_catalog_transaction(monkeypatch, conn)

    async def ensure(_conn, _article_id):
        return False

    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)

    count = await catalog.build_catalog_for_document({
        "id": 100,
        "server_id": "orlando",
        "title": "Кодекс",
        "content": kept_body,
    })

    assert count == 1
    article_deletes = [
        args for kind, query, args in conn.calls
        if kind == "execute"
        and query.startswith("DELETE FROM law_articles ")
        and "ANY($2::int[])" in query
    ]
    assert article_deletes == [(100, [1002])]


@pytest.mark.asyncio
async def test_stale_enrichment_result_is_rejected_before_synonyms_or_vector(monkeypatch):
    conn = _CatalogConnection([], update_id=None)
    _install_catalog_transaction(monkeypatch, conn)

    async def forbidden_ensure(*_args, **_kwargs):
        raise AssertionError("stale enrichment must not touch the vector")

    monkeypatch.setattr(catalog, "ensure_article_embedding_target", forbidden_ensure)
    stats = {"synonyms": 0}

    written = await catalog._write_enrichment(
        "memphis",
        42,
        "санкция",
        {"fine": "$100"},
        {"синоним"},
        stats,
        expected_article_hash="old-hash",
        title="Название",
    )

    assert written is False
    assert stats["synonyms"] == 0
    assert len(conn.calls) == 2
    lock_call = conn.calls[0]
    assert lock_call[0] == "execute"
    assert "pg_advisory_xact_lock" in lock_call[1]
    kind, query, args = conn.calls[1]
    assert kind == "fetchrow"
    assert "article_hash = $3" in query
    assert args[-1] == "old-hash"


@pytest.mark.asyncio
async def test_current_enrichment_updates_vector_and_replaces_only_auto_synonyms(monkeypatch):
    conn = _CatalogConnection([], update_id=42)
    _install_catalog_transaction(monkeypatch, conn)
    ensured = []

    async def ensure(actual_conn, article_id):
        ensured.append((actual_conn, article_id))
        return True

    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)
    synonym_calls = []
    signature_registry = object()

    async def replace(
        conn_arg,
        server_id,
        article_id,
        article_hash,
        synonyms,
        *,
        registry,
        lock_already_held,
        prior_enrichment_current,
        minimum_physical,
        minimum_effective,
    ):
        synonym_calls.append(
            (
                conn_arg,
                server_id,
                article_id,
                article_hash,
                set(synonyms),
                registry,
                lock_already_held,
                prior_enrichment_current,
                minimum_physical,
                minimum_effective,
            )
        )
        return type("Result", (), {
            "inserted": 1,
            "rejected_collisions": 0,
        })()

    monkeypatch.setattr(catalog, "replace_auto_synonyms", replace)
    stats = {"synonyms": 0}

    written = await catalog._write_enrichment(
        "memphis",
        42,
        "штраф $100",
        {"fine": "$100"},
        {"синоним", "x", "а" * 81},
        stats,
        expected_article_hash="current-hash",
        title="Название",
        signature_registry=signature_registry,
    )

    assert written is True
    assert ensured == [(conn, 42)]
    assert synonym_calls == [(
        conn,
        "memphis",
        42,
        "current-hash",
        {"синоним", "x", "а" * 81},
        signature_registry,
        True,
        True,
        8,
        8,
    )]
    assert stats["synonyms"] == 1
    assert conn.calls[0][0] == "execute"
    assert "pg_advisory_xact_lock" in conn.calls[0][1]
    update_call = conn.calls[2]
    assert update_call[0] == "fetchval"
    assert "server_id = $5 AND article_hash = $6" in update_call[1]
    assert update_call[2][-2:] == ("memphis", "current-hash")
    assert not any(
        kind == "execute" and (
            query.startswith("DELETE FROM law_synonyms")
            or "INSERT INTO law_synonyms" in query
        )
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_server_enrichment_loads_signature_registry_once_not_per_article(monkeypatch):
    import rate_limiter

    articles = [
        {
            "id": 1,
            "code": "1",
            "title": "One",
            "full_text": "First text",
            "article_hash": "hash-1",
        },
        {
            "id": 2,
            "code": "2",
            "title": "Two",
            "full_text": "Second text",
            "article_hash": "hash-2",
        },
    ]

    async def fetch(_query, *_args):
        return articles

    registry = object()
    registry_loads = []

    async def load_registry(queryable, server_id):
        registry_loads.append((queryable, server_id))
        return registry

    class Response:
        text = json.dumps([
            {
                "id": 1,
                "title": "One",
                "penalty_summary": None,
                "penalty": None,
                "synonyms": [
                    f"first precise phrase about {suffix}"
                    for suffix in _AUTO_TEST_SUFFIXES[:12]
                ],
            },
            {
                "id": 2,
                "title": "Two",
                "penalty_summary": None,
                "penalty": None,
                "synonyms": [
                    f"second precise phrase about {suffix}"
                    for suffix in _AUTO_TEST_SUFFIXES[:12]
                ],
            },
        ])

    vertex_requests = []

    async def execute_vertex_request(**_kwargs):
        vertex_requests.append(_kwargs)
        return Response()

    writes = []

    async def write_batch(server_id, batch, items, signature_registry):
        assert server_id == "memphis"
        writes.extend(
            (
                int(item["id"]),
                str(article["article_hash"]),
                signature_registry,
            )
            for article, item in zip(batch, items)
        )
        return {"articles": len(items), "synonyms": 24}

    monkeypatch.setattr(catalog.db_manager, "fetch", fetch)
    monkeypatch.setattr(catalog, "load_exact_signature_registry", load_registry)
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(catalog, "_write_auto_enrichment_batch", write_batch)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 2)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats["articles"] == 2
    assert len(vertex_requests) == 2
    assert registry_loads == [(catalog.db_manager, "memphis")]
    assert writes == [
        (1, "hash-1", registry),
        (2, "hash-2", registry),
    ]


_AUTO_TEST_SUFFIXES = (
    "автомобиль",
    "документ",
    "оружие",
    "лицензия",
    "дорога",
    "имущество",
    "суд",
    "арест",
    "штраф",
    "вызов",
    "обыск",
    "розыск",
    "жалоба",
    "договор",
    "служба",
)


def _auto_enrichment_item(article_id, prefix, *, synonyms_count=12):
    return {
        "id": article_id,
        "title": f"Title {article_id}",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [
            f"{prefix} distinct search formulation about {suffix}"
            for suffix in _AUTO_TEST_SUFFIXES[:synonyms_count]
        ],
    }


def test_auto_enrichment_payload_contains_same_document_contrast_and_old_terms():
    articles = [
        {
            "id": article_id,
            "law_document_id": 10,
            "source_order": article_id,
            "code": str(article_id),
            "title": f"Article {article_id}",
            "doc_title": "Criminal code",
            "full_text": f"Text {article_id}",
            "current_auto_synonyms": (
                ["  OLD   TERM  "] if article_id in {2, 3} else []
            ),
        }
        for article_id in range(1, 6)
    ] + [{
        "id": 99,
        "law_document_id": 20,
        "source_order": 1,
        "code": "99",
        "title": "Other document",
        "doc_title": "Other",
        "full_text": "Must not leak",
        "current_auto_synonyms": [],
    }]

    payload = catalog._build_auto_enrichment_payload(
        [articles[2]],
        articles,
    )

    assert payload[0]["id"] == 3
    assert payload[0]["current_auto_synonyms"] == ["old term"]
    assert [
        item["id"] for item in payload[0]["contrast_context"]
    ] == [1, 2, 4, 5]
    assert payload[0]["contrast_context"][1]["current_auto_synonyms"] == [
        "old term"
    ]
    assert 99 not in {
        item["id"] for item in payload[0]["contrast_context"]
    }


def test_auto_enrichment_payload_never_truncates_target_text_and_splits_large_batches():
    first = {
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "full_text": "а" * 130_000,
    }
    second = {
        "id": 2,
        "law_document_id": 10,
        "source_order": 2,
        "full_text": "б" * 70_000,
    }

    batches = catalog._partition_auto_enrichment_batches(
        [first, second],
        maximum_articles=8,
    )
    payload = catalog._build_auto_enrichment_payload(
        batches[0],
        [first, second],
    )

    assert [[item["id"] for item in batch] for batch in batches] == [[1], [2]]
    assert payload[0]["text"] == first["full_text"]
    assert len(payload[0]["text"]) == 130_000


@pytest.mark.asyncio
async def test_auto_enrichment_reads_runtime_inactive_stale_terms_for_retry(monkeypatch):
    queries = []

    async def fetch(query, *_args):
        queries.append(query)
        return []

    monkeypatch.setattr(catalog.db_manager, "fetch", fetch)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats == {"articles": 0, "synonyms": 0, "batches_failed": 0}
    assert "s.status IN ('approved', 'stale')" in queries[0]


def test_auto_enrichment_response_requires_complete_exact_coverage():
    batch = [
        {"id": 1, "full_text": "Статья 1. Действующая норма."},
        {"id": 2, "full_text": "Статья 2. Действующая норма."},
    ]

    with pytest.raises(ValueError, match="coverage is not exact"):
        catalog._validate_auto_enrichment_response(
            [_auto_enrichment_item(1, "first")],
            batch,
        )


def test_auto_enrichment_response_rejects_cross_article_canonical_collision():
    batch = [
        {"id": 1, "full_text": "Статья 1. Действующая норма."},
        {"id": 2, "full_text": "Статья 2. Действующая норма."},
    ]
    first = _auto_enrichment_item(1, "first")
    second = _auto_enrichment_item(2, "second")
    first["synonyms"][0] = "угнал автомобиль"
    second["synonyms"][0] = "угнал автомобиль"

    with pytest.raises(ValueError, match="canonical synonym groups"):
        catalog._validate_auto_enrichment_response(
            [first, second],
            batch,
        )


def test_auto_enrichment_response_allows_only_empty_tombstone_enrichment():
    batch = [{"id": 7, "full_text": "Статья 7. Упразднена."}]
    item = {
        "id": 7,
        "title": None,
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [],
    }

    assert catalog._validate_auto_enrichment_response(
        [item],
        batch,
    ) == [item]

    item_with_invention = dict(item, synonyms=["отмененная статья"])
    with pytest.raises(ValueError, match="must have null"):
        catalog._validate_auto_enrichment_response(
            [item_with_invention],
            batch,
        )

    judicial_repeal = [{
        "id": 8,
        "full_text": (
            "Статья 11.\n"
            "Утратила силу в связи с решением Верховного суда "
            "по исковому заявлению №2003 от 27.01.2026\n"
            "Вступает в юридическую силу после подписания губернатором "
            "штата San Andreas Morted Monolite.\n"
            "Нормативный правовой акт был подписан губернатором штата "
            "San Andreas Morted Momolite 13.07.2026."
        ),
    }]
    judicial_item = dict(item, id=8)
    assert catalog._validate_auto_enrichment_response(
        [judicial_item],
        judicial_repeal,
    ) == [judicial_item]


@pytest.mark.asyncio
async def test_server_enrichment_never_partially_writes_incomplete_batch(monkeypatch):
    import rate_limiter

    articles = [
        {
            "id": 1,
            "law_document_id": 10,
            "source_order": 1,
            "code": "1",
            "title": "One",
            "doc_title": "Code",
            "full_text": "First text",
            "article_hash": "hash-1",
            "enrich_hash": None,
            "current_auto_synonyms": [],
        },
        {
            "id": 2,
            "law_document_id": 10,
            "source_order": 2,
            "code": "2",
            "title": "Two",
            "doc_title": "Code",
            "full_text": "Second text",
            "article_hash": "hash-2",
            "enrich_hash": None,
            "current_auto_synonyms": [],
        },
    ]

    async def fetch(_query, *_args):
        return articles

    class Response:
        text = json.dumps([_auto_enrichment_item(1, "first")])

    attempts = []

    async def execute_vertex_request(**_kwargs):
        attempts.append(True)
        return Response()

    async def load_registry(_queryable, _server_id):
        return object()

    async def forbidden_write(*_args, **_kwargs):
        raise AssertionError("an incomplete batch must not write any article")

    monkeypatch.setattr(catalog.db_manager, "fetch", fetch)
    monkeypatch.setattr(catalog, "load_exact_signature_registry", load_registry)
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(
        catalog,
        "_write_auto_enrichment_batch",
        forbidden_write,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 2)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats == {
        "articles": 0,
        "synonyms": 0,
        "batches_failed": 1,
    }
    assert len(attempts) == catalog._AUTO_ENRICH_QUALITY_ATTEMPTS


@pytest.mark.asyncio
async def test_server_enrichment_never_writes_when_gemini_review_is_invalid(monkeypatch):
    import rate_limiter

    articles = [{
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "code": "1",
        "title": "One",
        "doc_title": "Code",
        "full_text": "First text",
        "article_hash": "hash-1",
        "enrich_hash": None,
        "current_auto_synonyms": [],
    }]
    responses = [
        json.dumps([_auto_enrichment_item(1, "draft")]),
        "[]",
        "[]",
    ]

    async def fetch(_query, *_args):
        return articles

    async def execute_vertex_request(**_kwargs):
        return type("Response", (), {"text": responses.pop(0)})()

    async def load_registry(_queryable, _server_id):
        return object()

    async def forbidden_write(*_args, **_kwargs):
        raise AssertionError("an invalid review must block every write")

    monkeypatch.setattr(catalog.db_manager, "fetch", fetch)
    monkeypatch.setattr(catalog, "load_exact_signature_registry", load_registry)
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(
        catalog,
        "_write_auto_enrichment_batch",
        forbidden_write,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 1)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats["articles"] == 0
    assert stats["batches_failed"] == 1
    assert responses == []


@pytest.mark.asyncio
async def test_server_enrichment_writes_second_pass_correction_not_draft(monkeypatch):
    import rate_limiter

    articles = [{
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "code": "1",
        "title": "One",
        "doc_title": "Code",
        "full_text": "First text",
        "article_hash": "hash-1",
        "enrich_hash": None,
        "current_auto_synonyms": [],
    }]
    draft = _auto_enrichment_item(1, "draft")
    reviewed = _auto_enrichment_item(1, "reviewed")
    responses = [
        json.dumps([draft]),
        json.dumps([reviewed]),
    ]

    async def fetch(_query, *_args):
        return articles

    async def execute_vertex_request(**_kwargs):
        return type("Response", (), {"text": responses.pop(0)})()

    registry = object()

    async def load_registry(_queryable, _server_id):
        return registry

    writes = []

    async def write_batch(_server_id, batch, items, signature_registry):
        writes.append((
            int(items[0]["id"]),
            set(items[0]["synonyms"]),
            signature_registry,
        ))
        return {"articles": len(batch), "synonyms": 12}

    monkeypatch.setattr(catalog.db_manager, "fetch", fetch)
    monkeypatch.setattr(catalog, "load_exact_signature_registry", load_registry)
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(catalog, "_write_auto_enrichment_batch", write_batch)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 1)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats["articles"] == 1
    assert writes[0][0] == 1
    assert writes[0][1] == set(reviewed["synonyms"])
    assert writes[0][1].isdisjoint(draft["synonyms"])
    assert writes[0][2] is registry


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ("second_write", "rejected_signature"))
async def test_auto_enrichment_db_batch_is_atomic_and_requires_twelve_effective(
    monkeypatch,
    failure_mode,
):
    conn = _CatalogConnection([], update_id=None)
    transaction_state = {"committed": False, "rolled_back": False}

    @asynccontextmanager
    async def transaction():
        try:
            yield conn
        except Exception:
            transaction_state["rolled_back"] = True
            raise
        else:
            transaction_state["committed"] = True

    calls = []

    async def write_in_transaction(
        _conn,
        _server_id,
        article_id,
        _summary,
        _penalty,
        _synonyms,
        stats,
        **kwargs,
    ):
        calls.append(article_id)
        assert kwargs["synonym_lock_already_held"] is True
        assert kwargs["minimum_synonym_physical"] == 12
        assert kwargs["minimum_synonym_effective"] == 12
        kwargs["signature_registry"].add(
            article_id,
            "auto",
            f"term-{article_id}",
            ("fallback", f"key-{article_id}"),
        )
        if failure_mode == "second_write" and article_id == 2:
            raise catalog.SynonymWriteError("simulated DB-signature collision")
        stats["synonyms"] += 12
        if failure_mode == "rejected_signature" and article_id == 2:
            stats["synonym_collisions_rejected"] = 1
        return True

    monkeypatch.setattr(catalog.db_manager, "transaction", transaction)
    monkeypatch.setattr(
        catalog,
        "_write_enrichment_in_transaction",
        write_in_transaction,
    )
    registry = catalog.ExactSignatureRegistry()
    batch = [
        {"id": 1, "article_hash": "hash-1"},
        {"id": 2, "article_hash": "hash-2"},
    ]
    items = [
        {
            "id": article_id,
            "title": None,
            "penalty_summary": None,
            "penalty": None,
            "synonyms": [f"term {article_id}"],
        }
        for article_id in (1, 2)
    ]

    with pytest.raises(catalog.SynonymWriteError):
        await catalog._write_auto_enrichment_batch(
            "memphis",
            batch,
            items,
            registry,
        )

    assert calls == [1, 2]
    assert transaction_state == {"committed": False, "rolled_back": True}
    assert registry.records == {}

# END OF FILE tests/test_law_catalog.py
