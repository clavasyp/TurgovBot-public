# START OF FILE tests/test_law_catalog.py
"""
Тесты каталога статей умного поиска: сегментация (split_into_blocks),
нормализация заголовков (normalize_header) и парсер LLM-ответа обогащения.

Включает целевые regression-тесты стабильности публичных article ID.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import sys
import importlib.util
import inspect
import json
from types import SimpleNamespace

import pytest

from services.indexing_service import (
    _header_match_plan,
    canonicalize_split_structural_keyword_layout,
    chunk_law_text,
    split_into_blocks,
)
from services import law_catalog_service as catalog
from services import law_synonym_service as synonym_service
from services.law_catalog_service import (
    normalize_header, _parse_json_array, _title_from_body, _is_junk_title,
    _article_hash, _match_existing_articles,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LAW_SOURCE_CATALOG_BINDING_SCHEMA,
    VerifiedLawCatalogTruth,
)
from services.law_synonym_service import (
    build_auto_replacement_catalog_binding,
)
from services.law_title_utils import recover_rating_metadata_title
from services.law_structural_code import canonical_roman_value
from services.law_enrichment_validation import (
    validate_enrichment_source_evidence,
    validate_enrichment_title_source_evidence,
    validate_synonym_source_evidence,
)


def _writer_generation_snapshot(catalog_rows, **fields):
    return {
        **fields,
        **build_auto_replacement_catalog_binding(
            "memphis",
            catalog_rows,
        ),
    }


def _writer_context_hashes(catalog_rows):
    return synonym_service.build_auto_replacement_context_hashes(
        "memphis",
        catalog_rows,
    )


@asynccontextmanager
async def _noop_savepoint(conn):
    yield conn


def _auto_generation_state_for_tests(articles):
    """Build a model/writer snapshot without touching a real database pool."""

    captured = []
    for source in articles:
        row = dict(source)
        article_id = int(row["id"])
        code = str(row.get("code") or article_id)
        article_ref = str(row.get("article_ref") or f"Статья {code}")
        original_text = str(row.get("full_text") or "Действующая норма")
        if not original_text.startswith(article_ref + "\n"):
            original_text = f"{article_ref}\n1) {original_text}"
        row.update({
            "law_document_id": int(row.get("law_document_id") or 10),
            "source_order": int(row.get("source_order") or article_id),
            "article_ref": article_ref,
            "kind": "article",
            "code": code,
            "doc_title": row.get("doc_title") or "Test code",
            "full_text": original_text,
            "enrich_hash": row.get("enrich_hash"),
        })
        source_title = catalog.source_catalog_title(
            article_ref,
            original_text,
        )
        if row.get("title") is None:
            row["title_provenance"] = None
        elif row.get("title") == source_title:
            row["title_provenance"] = catalog.TITLE_PROVENANCE_SOURCE
        else:
            row["title_provenance"] = catalog.TITLE_PROVENANCE_GENERATED
        row["enrichment_context_hash"] = None
        captured.append(row)
    stale = sorted(
        int(row["id"])
        for row in captured
        if row.get("enrich_hash") != row["article_hash"]
    )
    return {
        "snapshot": _writer_generation_snapshot(
            captured,
            required_target_ids=stale,
            stale_target_ids=stale,
            selection_mode="stale",
        ),
        "catalog_rows": [dict(row) for row in captured],
        "articles": [dict(row) for row in captured],
        "auto_rows": [],
        "auto_fingerprints": {
            int(row["id"]): "0" * 64 for row in captured
        },
        "context_hashes": _writer_context_hashes(captured),
    }


def _successful_auto_preview(articles):
    ids = [int(article["id"]) for article in articles]
    return SimpleNamespace(
        rejected_collisions=0,
        physical_after={article_id: 12 for article_id in ids},
        effective_after={article_id: 12 for article_id in ids},
    )


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
    def __init__(
        self,
        old_rows,
        *,
        update_id=None,
        auto_rows=(),
        source_snapshot_matches=True,
    ):
        self.old_rows = old_rows
        self.update_id = update_id
        self.auto_rows = [dict(row) for row in auto_rows]
        self.source_snapshot_matches = source_snapshot_matches
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if (
            "SELECT DISTINCT server_id" in query
            and "FROM law_articles" in query
        ):
            return [
                {"server_id": server_id}
                for server_id in sorted({
                    row["server_id"] for row in self.old_rows
                })
            ]
        if "FROM law_articles" in query and "FOR UPDATE" in query:
            return self.old_rows
        if (
            "FROM law_synonyms s" in query
            and "owning_server_id" in query
        ):
            retained_ids = {int(value) for value in args[0]}
            owning_servers = {
                int(row["id"]): str(row["server_id"])
                for row in self.old_rows
            }
            return [
                {
                    **row,
                    "owning_server_id": owning_servers[
                        int(row["article_id"])
                    ],
                }
                for row in self.auto_rows
                if int(row["article_id"]) in retained_ids
            ]
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
        if "FROM law_documents" in query and "FOR UPDATE" in query:
            if not self.source_snapshot_matches:
                return None
            return {
                "id": args[0],
                "server_id": args[1],
                "title": args[2],
                "content": args[3],
                "content_hash": args[4],
                "last_updated": args[5],
            }
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

    @asynccontextmanager
    async def document_lock(_server_id, *, manager):
        assert manager is catalog.db_manager
        yield

    @asynccontextmanager
    async def catalog_lock(_server_id):
        yield

    monkeypatch.setattr(catalog.db_manager, "transaction", transaction)
    monkeypatch.setattr(catalog, "law_document_server_lock", document_lock)
    monkeypatch.setattr(catalog, "catalog_server_lock", catalog_lock)


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
async def test_tombstone_reconcile_clears_legacy_enrichment_and_stales_auto_rows(
    monkeypatch,
):
    body = "Статья 10\nУтратила силу."
    article_hash = _article_hash(body)
    old = {
        "id": 910,
        "law_document_id": 91,
        "server_id": "denver",
        "source_order": 0,
        "article_hash": article_hash,
        "article_ref": "Статья 10",
        "kind": "article",
        "code": "10",
        "title": "Утратила силу",
        "title_provenance": catalog.TITLE_PROVENANCE_LEGACY_UNKNOWN,
        "doc_title": "УК",
        "full_text": body,
        "penalty_summary": "ошибочная старая санкция",
        "penalty_json": '{"other": "ошибочная старая санкция"}',
        "enrich_hash": article_hash,
        "enrichment_context_hash": "a" * 64,
        "has_approved_auto_synonyms": True,
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
        args
        for kind, query, args in conn.calls
        if kind == "fetchval"
        and query.lstrip().startswith("UPDATE law_articles")
    )
    assert update_args[5] is None
    assert update_args[10:15] == (None, None, None, None, None)
    assert update_args[15] is None
    assert update_args[16] is True
    assert any(
        kind == "execute"
        and "UPDATE law_synonyms SET status = 'stale'" in query
        and args == (910,)
        for kind, query, args in conn.calls
    )


@pytest.mark.asyncio
async def test_clean_current_tombstone_reconcile_is_idempotent(monkeypatch):
    body = "Статья 10\nУтратила силу."
    article_hash = _article_hash(body)
    old = {
        "id": 910,
        "law_document_id": 91,
        "server_id": "denver",
        "source_order": 0,
        "article_hash": article_hash,
        "article_ref": "Статья 10",
        "kind": "article",
        "code": "10",
        "title": None,
        "title_provenance": None,
        "doc_title": "УК",
        "full_text": body,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": article_hash,
        "enrichment_context_hash": "b" * 64,
        "has_approved_auto_synonyms": False,
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
        args
        for kind, query, args in conn.calls
        if kind == "fetchval"
        and query.lstrip().startswith("UPDATE law_articles")
    )
    assert update_args[5] is None
    assert update_args[10:15] == (
        None,
        None,
        article_hash,
        None,
        "b" * 64,
    )
    assert update_args[15] is None
    assert update_args[16] is False
    assert not any(
        kind == "execute"
        and "UPDATE law_synonyms SET status = 'stale'" in query
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
    assert update_args[13] == catalog.TITLE_PROVENANCE_SOURCE
    assert update_args[14] is None
    assert update_args[15] is None
    assert update_args[16] is auto_synonyms_deactivated
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


def _catalog_auto_row(
    *,
    row_id: int,
    article_id: int,
    server_id: str,
    term: str,
) -> dict:
    return {
        "id": row_id,
        "server_id": server_id,
        "article_id": article_id,
        "term": term,
        "weight": 1.0,
        "source": "auto",
        "status": "approved",
        "suggested_by": None,
    }


@pytest.mark.asyncio
async def test_reconcile_retained_foreign_auto_aborts_before_any_mutation(
    monkeypatch,
):
    old_body = (
        "Статья 7 Халатность\n"
        "Старое подробное описание нарушения и наказания."
    )
    new_body = (
        "Статья 7 Халатность\n"
        "Новый подробный текст нарушения и наказания."
    )
    old = {
        "id": 707,
        "law_document_id": 70,
        "server_id": "memphis",
        "source_order": 0,
        "article_hash": _article_hash(old_body),
        "article_ref": "Статья 7 Халатность",
        "kind": "article",
        "code": "7",
        "title": "Халатность",
        "doc_title": "УК",
        "full_text": old_body,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": _article_hash(old_body),
    }
    conn = _CatalogConnection(
        [old],
        update_id=707,
        auto_rows=[_catalog_auto_row(
            row_id=5001,
            article_id=707,
            server_id="another-server",
            term="foreign ownership",
        )],
    )
    _install_catalog_transaction(monkeypatch, conn)

    with pytest.raises(
        catalog.SynonymWriteError,
        match="foreign-server auto synonym ownership",
    ):
        await catalog.build_catalog_for_document({
            "id": 70,
            "server_id": "memphis",
            "title": "УК",
            "content": new_body,
        })

    assert not any(
        kind == "fetchval"
        or (
            kind == "execute"
            and not query.lstrip().startswith(
                "SELECT pg_advisory_xact_lock"
            )
        )
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_source_proven_removed_article_may_cascade_foreign_auto(
    monkeypatch,
):
    kept_body = (
        "Статья 1 Первая\n"
        "Первая действующая норма с подробным текстом."
    )
    removed_body = (
        "Статья 2 Вторая\n"
        "Вторая удалённая норма с подробным текстом."
    )

    def old_row(article_id, order, body, ref, code, title):
        return {
            "id": article_id,
            "law_document_id": 100,
            "server_id": "orlando",
            "source_order": order,
            "article_hash": _article_hash(body),
            "article_ref": ref,
            "kind": "article",
            "code": code,
            "title": title,
            "doc_title": "Кодекс",
            "full_text": body,
            "penalty_summary": None,
            "penalty_json": None,
            "enrich_hash": _article_hash(body),
        }

    conn = _CatalogConnection(
        [
            old_row(
                1001,
                0,
                kept_body,
                "Статья 1 Первая",
                "1",
                "Первая",
            ),
            old_row(
                1002,
                1,
                removed_body,
                "Статья 2 Вторая",
                "2",
                "Вторая",
            ),
        ],
        update_id=1001,
        auto_rows=[_catalog_auto_row(
            row_id=5002,
            article_id=1002,
            server_id="foreign-server",
            term="removed foreign child",
        )],
    )
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
    assert any(
        kind == "execute"
        and query.startswith("DELETE FROM law_articles ")
        and args == (100, [1002])
        for kind, query, args in conn.calls
    )


@pytest.mark.asyncio
async def test_empty_locked_source_allows_authoritative_catalog_delete(
    monkeypatch,
):
    old_body = (
        "Статья 1 Первая\n"
        "Удалённая из ставшего пустым источника правовая норма."
    )
    old = {
        "id": 1001,
        "law_document_id": 100,
        "server_id": "orlando",
        "source_order": 0,
        "article_hash": _article_hash(old_body),
        "article_ref": "Статья 1 Первая",
        "kind": "article",
        "code": "1",
        "title": "Первая",
        "doc_title": "Кодекс",
        "full_text": old_body,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": _article_hash(old_body),
    }
    conn = _CatalogConnection(
        [old],
        auto_rows=[_catalog_auto_row(
            row_id=5003,
            article_id=1001,
            server_id="foreign-server",
            term="authoritatively removed child",
        )],
    )
    _install_catalog_transaction(monkeypatch, conn)

    count = await catalog.build_catalog_for_document({
        "id": 100,
        "server_id": "orlando",
        "title": "Кодекс",
        "content": "",
    })

    assert count == 0
    assert any(
        kind == "execute"
        and query.startswith("DELETE FROM law_articles ")
        and args == (100, [1001])
        for kind, query, args in conn.calls
    )


@pytest.mark.asyncio
async def test_stale_source_snapshot_aborts_before_catalog_mutation(
    monkeypatch,
):
    old_body = (
        "Статья 1 Первая\n"
        "Существующая правовая норма с подробным текстом."
    )
    old = {
        "id": 1001,
        "law_document_id": 100,
        "server_id": "orlando",
        "source_order": 0,
        "article_hash": _article_hash(old_body),
        "article_ref": "Статья 1 Первая",
        "kind": "article",
        "code": "1",
        "title": "Первая",
        "doc_title": "Кодекс",
        "full_text": old_body,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": _article_hash(old_body),
    }
    conn = _CatalogConnection(
        [old],
        source_snapshot_matches=False,
    )
    _install_catalog_transaction(monkeypatch, conn)

    with pytest.raises(
        catalog.CatalogSourceSnapshotChangedError,
        match="disappeared before catalog write",
    ):
        await catalog.build_catalog_for_document({
            "id": 100,
            "server_id": "orlando",
            "title": "Кодекс",
            "content": old_body,
            "content_hash": "stale-content-hash",
            "last_updated": "stale-version",
        })

    assert not any(
        kind == "fetchval"
        or (
            kind == "execute"
            and not query.lstrip().startswith(
                "SELECT pg_advisory_xact_lock"
            )
        )
        for kind, query, _args in conn.calls
    )
    source_query = next(
        query
        for kind, query, _args in conn.calls
        if kind == "fetchrow" and "FROM law_documents" in query
    )
    assert "server_id IS NOT DISTINCT FROM $2" in source_query
    assert "title IS NOT DISTINCT FROM $3" in source_query
    assert "content IS NOT DISTINCT FROM $4" in source_query
    assert "content_hash IS NOT DISTINCT FROM $5" in source_query
    assert "last_updated IS NOT DISTINCT FROM $6" in source_query


@pytest.mark.asyncio
async def test_cross_server_old_article_aborts_before_any_catalog_mutation(
    monkeypatch,
):
    body = (
        "Статья 1 Первая\n"
        "Существующая правовая норма с подробным текстом."
    )
    old = {
        "id": 1001,
        "law_document_id": 100,
        "server_id": "legacy-server",
        "source_order": 0,
        "article_hash": _article_hash(body),
        "article_ref": "Статья 1 Первая",
        "kind": "article",
        "code": "1",
        "title": "Первая",
        "doc_title": "Кодекс",
        "full_text": body,
        "penalty_summary": None,
        "penalty_json": None,
        "enrich_hash": _article_hash(body),
    }
    conn = _CatalogConnection([old])
    _install_catalog_transaction(monkeypatch, conn)

    with pytest.raises(
        catalog.SynonymWriteError,
        match="cross-server catalog migration requires",
    ):
        await catalog.build_catalog_for_document({
            "id": 100,
            "server_id": "memphis",
            "title": "Кодекс",
            "content": body,
        })

    assert not any(
        kind == "fetchval"
        or (
            kind == "execute"
            and not query.lstrip().startswith(
                "SELECT pg_advisory_xact_lock"
            )
        )
        for kind, query, _args in conn.calls
    )
    synonym_lock_keys = [
        args[0]
        for kind, query, args in conn.calls
        if (
            kind == "execute"
            and "pg_advisory_xact_lock" in query
        )
    ]
    assert synonym_lock_keys == [
        "law-synonyms:legacy-server",
        "law-synonyms:memphis",
    ]


@pytest.mark.asyncio
async def test_public_full_build_holds_document_then_catalog_for_whole_run(
    monkeypatch,
):
    events = []
    held = {"document": False, "catalog": False}

    @asynccontextmanager
    async def document_lock(server_id, *, manager):
        assert server_id == "memphis"
        assert manager is catalog.db_manager
        assert not held["document"]
        events.append("document_enter")
        held["document"] = True
        try:
            yield
        finally:
            assert not held["catalog"]
            held["document"] = False
            events.append("document_exit")

    @asynccontextmanager
    async def catalog_lock(server_id):
        assert server_id == "memphis"
        assert held["document"]
        events.append("catalog_enter")
        held["catalog"] = True
        try:
            yield
        finally:
            held["catalog"] = False
            events.append("catalog_exit")

    async def build(server_id, **_kwargs):
        assert server_id == "memphis"
        assert held == {"document": True, "catalog": True}
        events.append("first_doc_reconciled")
        # Model a source updater trying to run after the first document but
        # before enrichment/report. The run-level document lock is still held.
        events.append("source_update_blocked_by_document_lock")
        assert held["document"] is True
        events.append("final_report")
        return {"documents": 2}

    monkeypatch.setattr(catalog, "law_document_server_lock", document_lock)
    monkeypatch.setattr(catalog, "catalog_server_lock", catalog_lock)
    monkeypatch.setattr(catalog, "_build_catalog_for_server", build)

    result = await catalog.build_catalog_for_server("memphis")

    assert result == {"documents": 2}
    assert events == [
        "document_enter",
        "catalog_enter",
        "first_doc_reconciled",
        "source_update_blocked_by_document_lock",
        "final_report",
        "catalog_exit",
        "document_exit",
    ]


class _EnrichmentRunConnection:
    def __init__(self, owner):
        self.owner = owner

    async def execute(self, query, *args):
        self.owner.calls.append(("execute", query, args))
        if "pg_advisory_xact_lock" in query:
            return "SELECT 1"
        if "UPDATE law_articles" in query and "enrich_hash = NULL" in query:
            if self.owner.fail_invalidation:
                raise RuntimeError("invalidation failed")
            titleless_only = "AND title IS NULL" in query
            for article in self.owner.articles:
                if (
                    article["server_id"] == args[0]
                    and article["kind"] == "article"
                    and (
                        not titleless_only
                        or article.get("title") is None
                    )
                ):
                    article["enrich_hash"] = None
            return "UPDATE"
        raise AssertionError(query)

    async def fetch(self, query, *args):
        self.owner.calls.append(("fetch", query, args))
        if (
            "FROM law_documents" in query
            and "FOR UPDATE" in query
        ):
            return [
                dict(row)
                for row in sorted(
                    self.owner.documents.values(),
                    key=lambda item: item["id"],
                )
                if row["server_id"] == args[0]
            ]
        if (
            "UPDATE law_documents" in query
            and "derived_sync_pending = TRUE" in query
        ):
            server_id = args[0]
            document_ids = (
                args[1]
                if len(args) == 2
                else sorted(self.owner.documents)
            )
            updated = []
            for document_id in document_ids:
                row = self.owner.documents.get(int(document_id))
                if row is not None and row["server_id"] == server_id:
                    row["derived_sync_pending"] = True
                    updated.append({"id": int(document_id)})
            return updated
        raise AssertionError(query)

    async def fetchval(self, query, *args):
        self.owner.calls.append(("fetchval", query, args))
        if (
            "UPDATE law_documents" in query
            and "derived_sync_pending = FALSE" in query
        ):
            (
                document_id,
                server_id,
                title,
                content,
                content_hash,
                last_updated,
            ) = args
            row = self.owner.documents.get(int(document_id))
            if (
                row is not None
                and row["server_id"] == server_id
                and row.get("title") == title
                and row.get("content") == content
                and row.get("content_hash") == content_hash
                and row.get("last_updated") == last_updated
                and row.get("derived_sync_pending") is True
            ):
                row["derived_sync_pending"] = False
                return int(document_id)
            return None
        raise AssertionError(query)


class _EnrichmentRunDB:
    def __init__(
        self,
        documents,
        articles,
        *,
        fail_invalidation=False,
    ):
        self.documents = {
            int(row["id"]): dict(row)
            for row in documents
        }
        self.articles = [dict(row) for row in articles]
        self.fail_invalidation = fail_invalidation
        self.calls = []
        self.transactions = 0
        self.connections = []

    @asynccontextmanager
    async def transaction(self):
        self.transactions += 1
        documents_before = deepcopy(self.documents)
        articles_before = deepcopy(self.articles)
        conn = _EnrichmentRunConnection(self)
        self.connections.append(conn)
        try:
            yield conn
        except Exception:
            self.documents = documents_before
            self.articles = articles_before
            raise


def _pending_source_documents():
    return [
        {
            "id": 1,
            "server_id": "memphis",
            "title": "УК",
            "content": "Статья 1",
            "content_hash": "doc-1",
            "last_updated": "v1",
            "derived_sync_pending": False,
        },
        {
            "id": 2,
            "server_id": "memphis",
            "title": "КоАП",
            "content": "Статья 2",
            "content_hash": "doc-2",
            "last_updated": "v2",
            "derived_sync_pending": True,
        },
    ]


@pytest.mark.asyncio
async def test_enrichment_run_claim_is_atomic_and_clears_only_owned_marker(
    monkeypatch,
):
    documents = _pending_source_documents()
    run_db = _EnrichmentRunDB(
        documents,
        [
            {
                "id": 10,
                "server_id": "memphis",
                "kind": "article",
                "title": None,
                "enrich_hash": "old-10",
            },
            {
                "id": 20,
                "server_id": "memphis",
                "kind": "article",
                "title": "Source title",
                "enrich_hash": "old-20",
            },
        ],
    )
    monkeypatch.setattr(
        catalog.db_manager,
        "transaction",
        run_db.transaction,
    )

    claim = await catalog.begin_enrichment_run(
        "memphis",
        invalidation="titleless",
        source_documents=documents,
    )

    assert claim.document_ids == (1,)
    assert run_db.documents[1]["derived_sync_pending"] is True
    assert run_db.documents[2]["derived_sync_pending"] is True
    assert run_db.articles[0]["enrich_hash"] is None
    assert run_db.articles[1]["enrich_hash"] == "old-20"

    await catalog._clear_enrichment_pending_claim(claim)

    assert run_db.documents[1]["derived_sync_pending"] is False
    assert run_db.documents[2]["derived_sync_pending"] is True


@pytest.mark.asyncio
async def test_enrichment_run_invalidation_failure_rolls_back_pending_claim(
    monkeypatch,
):
    documents = _pending_source_documents()
    run_db = _EnrichmentRunDB(
        documents,
        [{
            "id": 10,
            "server_id": "memphis",
            "kind": "article",
            "title": None,
            "enrich_hash": "old-10",
        }],
        fail_invalidation=True,
    )
    monkeypatch.setattr(
        catalog.db_manager,
        "transaction",
        run_db.transaction,
    )

    with pytest.raises(RuntimeError, match="invalidation failed"):
        await catalog.begin_enrichment_run(
            "memphis",
            invalidation="all",
            source_documents=documents,
        )

    assert (
        run_db.documents[1]["derived_sync_pending"]
        is documents[0]["derived_sync_pending"]
    )
    assert (
        run_db.documents[2]["derived_sync_pending"]
        is documents[1]["derived_sync_pending"]
    )
    assert run_db.articles[0]["enrich_hash"] == "old-10"


@pytest.mark.asyncio
async def test_full_build_claims_pending_before_first_catalog_mutation(
    monkeypatch,
):
    docs = [
        {
            "id": 1,
            "server_id": "memphis",
            "title": "УК",
            "content": "",
            "content_hash": "one",
            "last_updated": "v1",
        },
        {
            "id": 2,
            "server_id": "memphis",
            "title": "КоАП",
            "content": "",
            "content_hash": "two",
            "last_updated": "v2",
        },
    ]
    events = []
    claim = catalog.EnrichmentPendingClaim(
        server_id="memphis",
        documents=((1, "УК", "", "one", "v1"),),
    )

    async def fetch(_query, *_args):
        return docs

    async def begin(server_id, **kwargs):
        events.append(
            (
                "pending",
                server_id,
                kwargs["invalidation"],
                tuple(row["id"] for row in kwargs["source_documents"]),
            )
        )
        return claim

    async def build(doc):
        events.append(("catalog", doc["id"]))
        if doc["id"] == 2:
            raise RuntimeError("second document failed")
        return 1

    async def forbidden_run(*_args, **_kwargs):
        raise AssertionError("enrichment must not run after catalog failure")

    monkeypatch.setattr(catalog.db_manager, "fetch", fetch)
    monkeypatch.setattr(catalog, "begin_enrichment_run", begin)
    monkeypatch.setattr(
        catalog,
        "_build_catalog_for_document_locked",
        build,
    )
    monkeypatch.setattr(
        catalog,
        "run_verified_enrichment_for_server",
        forbidden_run,
    )

    with pytest.raises(
        RuntimeError,
        match="catalog reconcile failed for documents \\[2\\]",
    ):
        await catalog._build_catalog_for_server("memphis")

    assert events == [
        ("pending", "memphis", "none", (1, 2)),
        ("catalog", 1),
        ("catalog", 2),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retitle", "reenrich"),
    [(True, False), (False, True)],
)
async def test_full_build_rejects_invalidation_without_enrichment_before_db(
    monkeypatch,
    retitle,
    reenrich,
):
    async def forbidden_fetch(*_args, **_kwargs):
        raise AssertionError("invalid flags must fail before database access")

    monkeypatch.setattr(catalog.db_manager, "fetch", forbidden_fetch)

    with pytest.raises(ValueError, match="require enrich=True"):
        await catalog._build_catalog_for_server(
            "memphis",
            enrich=False,
            retitle=retitle,
            reenrich=reenrich,
        )


@pytest.mark.asyncio
async def test_verified_completion_rejects_empty_full_server_and_keeps_pending(
    monkeypatch,
):
    from services import law_enrichment_completion

    fallback_marks = []
    run_db = _EnrichmentRunDB(
        [{
            "id": 1,
            "server_id": "memphis",
            "title": "УК",
            "content": "",
            "content_hash": "doc",
            "last_updated": "v1",
            "derived_sync_pending": False,
        }],
        [],
    )

    async def evaluate(conn, server_id):
        assert conn is run_db.connections[0]
        assert server_id == "memphis"
        return SimpleNamespace(
            articles=0,
            complete=True,
            incomplete_article_ids=(),
            incomplete_document_ids=(),
        )

    async def mark(server_id, document_ids=None):
        fallback_marks.append((server_id, document_ids))
        return ()

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        evaluate,
    )
    monkeypatch.setattr(catalog, "_mark_enrichment_pending", mark)
    monkeypatch.setattr(
        catalog.db_manager,
        "transaction",
        run_db.transaction,
    )
    claim = catalog.EnrichmentPendingClaim("memphis", ())

    with pytest.raises(
        catalog.SynonymWriteError,
        match="empty article corpus",
    ):
        await catalog.require_successful_enrichment_completion(
            "memphis",
            {"batches_failed": 0},
            pending_claim=claim,
        )

    assert run_db.documents[1]["derived_sync_pending"] is True
    assert run_db.transactions == 1
    assert fallback_marks == []


@pytest.mark.asyncio
async def test_verified_completion_marks_known_incomplete_documents(
    monkeypatch,
):
    from services import law_enrichment_completion

    fallback_marks = []
    run_db = _EnrichmentRunDB(
        [{
            "id": 7,
            "server_id": "memphis",
            "title": "УК",
            "content": "",
            "content_hash": "doc",
            "last_updated": "v1",
            "derived_sync_pending": False,
        }],
        [],
    )

    async def evaluate(conn, _server_id):
        assert conn is run_db.connections[0]
        return SimpleNamespace(
            articles=2,
            complete=False,
            incomplete_article_ids=(22,),
            incomplete_document_ids=(7,),
        )

    async def mark(server_id, document_ids=None):
        fallback_marks.append((server_id, document_ids))
        return tuple(document_ids or ())

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        evaluate,
    )
    monkeypatch.setattr(catalog, "_mark_enrichment_pending", mark)
    monkeypatch.setattr(
        catalog.db_manager,
        "transaction",
        run_db.transaction,
    )

    with pytest.raises(
        catalog.SynonymWriteError,
        match="incomplete article ids \\[22\\]",
    ):
        await catalog.require_successful_enrichment_completion(
            "memphis",
            {"batches_failed": 0},
        )

    assert run_db.documents[7]["derived_sync_pending"] is True
    assert run_db.transactions == 1
    assert fallback_marks == []


@pytest.mark.asyncio
async def test_verified_completion_clears_run_owned_claim_only_after_success(
    monkeypatch,
):
    from services import law_enrichment_completion

    completion = SimpleNamespace(
        articles=2,
        complete=True,
        incomplete_article_ids=(),
        incomplete_document_ids=(),
    )
    run_db = _EnrichmentRunDB(
        [{
            "id": 1,
            "server_id": "memphis",
            "title": "УК",
            "content": "",
            "content_hash": "hash",
            "last_updated": "v1",
            "derived_sync_pending": True,
        }],
        [],
    )

    async def evaluate(conn, _server_id):
        assert conn is run_db.connections[0]
        assert (
            run_db.calls[0][0] == "execute"
            and "pg_advisory_xact_lock" in run_db.calls[0][1]
        )
        return completion

    async def forbidden_mark(*_args, **_kwargs):
        raise AssertionError("complete server must not be marked pending")

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        evaluate,
    )
    monkeypatch.setattr(
        catalog.db_manager,
        "transaction",
        run_db.transaction,
    )
    monkeypatch.setattr(
        catalog,
        "_mark_enrichment_pending",
        forbidden_mark,
    )
    claim = catalog.EnrichmentPendingClaim(
        "memphis",
        ((1, "УК", "", "hash", "v1"),),
    )

    result = await catalog.require_successful_enrichment_completion(
        "memphis",
        {"batches_failed": 0},
        pending_claim=claim,
    )

    assert result is completion
    assert run_db.transactions == 1
    assert run_db.documents[1]["derived_sync_pending"] is False
    assert [kind for kind, _query, _args in run_db.calls] == [
        "execute",
        "fetchval",
    ]


@pytest.mark.asyncio
async def test_verified_completion_rejects_failed_batch_without_evaluator(
    monkeypatch,
):
    marked = []

    async def mark(server_id, document_ids=None):
        marked.append((server_id, document_ids))
        return ()

    monkeypatch.setattr(catalog, "_mark_enrichment_pending", mark)

    with pytest.raises(
        catalog.SynonymWriteError,
        match="failed batches=1",
    ):
        await catalog.require_successful_enrichment_completion(
            "memphis",
            {"batches_failed": 1},
        )

    assert marked == [("memphis", None)]


@pytest.mark.asyncio
async def test_public_single_document_build_uses_self_owned_lock_order(
    monkeypatch,
):
    events = []

    @asynccontextmanager
    async def document_lock(server_id, *, manager):
        assert server_id == "memphis"
        assert manager is catalog.db_manager
        events.append("document_enter")
        try:
            yield
        finally:
            events.append("document_exit")

    @asynccontextmanager
    async def catalog_lock(server_id):
        assert server_id == "memphis"
        assert events == ["document_enter"]
        events.append("catalog_enter")
        try:
            yield
        finally:
            events.append("catalog_exit")

    async def build(doc):
        events.append(("build", doc["id"]))
        return 3

    monkeypatch.setattr(catalog, "law_document_server_lock", document_lock)
    monkeypatch.setattr(catalog, "catalog_server_lock", catalog_lock)
    monkeypatch.setattr(
        catalog,
        "_build_catalog_for_document_locked",
        build,
    )

    result = await catalog.build_catalog_for_document({
        "id": 70,
        "server_id": "memphis",
        "title": "УК",
        "content": "",
    })

    assert result == 3
    assert events == [
        "document_enter",
        "catalog_enter",
        ("build", 70),
        "catalog_exit",
        "document_exit",
    ]


@pytest.mark.asyncio
async def test_incremental_sync_self_locks_but_caller_held_path_does_not_relock(
    monkeypatch,
):
    events = []

    @asynccontextmanager
    async def document_lock(server_id, *, manager):
        assert server_id == "memphis"
        assert manager is catalog.db_manager
        events.append("document_enter")
        try:
            yield
        finally:
            events.append("document_exit")

    @asynccontextmanager
    async def catalog_lock(server_id):
        assert server_id == "memphis"
        events.append("catalog_enter")
        try:
            yield
        finally:
            events.append("catalog_exit")

    async def sync(server_id, doc_ids):
        events.append(("sync", server_id, tuple(doc_ids)))
        return {"documents": len(doc_ids)}

    monkeypatch.setattr(catalog, "law_document_server_lock", document_lock)
    monkeypatch.setattr(catalog, "catalog_server_lock", catalog_lock)
    monkeypatch.setattr(catalog, "_sync_catalog_for_documents", sync)

    standalone = await catalog.sync_catalog_for_documents(
        "memphis",
        [1, 2],
    )
    assert standalone == {"documents": 2}
    assert events == [
        "document_enter",
        "catalog_enter",
        ("sync", "memphis", (1, 2)),
        "catalog_exit",
        "document_exit",
    ]

    events.clear()
    caller_held = await catalog.sync_catalog_for_documents(
        "memphis",
        [3],
        document_lock_already_held=True,
    )
    assert caller_held == {"documents": 1}
    assert events == [
        "catalog_enter",
        ("sync", "memphis", (3,)),
        "catalog_exit",
    ]

    with pytest.raises(
        TypeError,
        match="document_lock_already_held must be bool",
    ):
        await catalog.sync_catalog_for_documents(
            "memphis",
            [4],
            document_lock_already_held=1,
        )


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
        synonym_service.MIN_REPLACEMENT_PHYSICAL,
        synonym_service.MIN_REPLACEMENT_EFFECTIVE,
    )]
    assert stats["synonyms"] == 1
    assert conn.calls[0][0] == "execute"
    assert "pg_advisory_xact_lock" in conn.calls[0][1]
    update_call = conn.calls[2]
    assert update_call[0] == "fetchval"
    assert "server_id = $5 AND article_hash = $6" in update_call[1]
    assert update_call[2][4:6] == ("memphis", "current-hash")
    assert update_call[2][6:] == (False, None, None)
    assert not any(
        kind == "execute" and (
            query.startswith("DELETE FROM law_synonyms")
            or "INSERT INTO law_synonyms" in query
        )
        for kind, query, _args in conn.calls
    )


@pytest.mark.asyncio
async def test_server_enrichment_captures_previews_and_writes_once(monkeypatch):
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

    captures = []

    async def capture(server_id, *, only_stale):
        captures.append((server_id, only_stale))
        return _auto_generation_state_for_tests(articles)

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

    previews = []

    async def preview(server_id, items, *, generation_snapshot):
        previews.append((server_id, items, generation_snapshot))
        return _successful_auto_preview(articles)

    async def write_server(server_id, batch, items, generation_state):
        assert server_id == "memphis"
        writes.extend(
            (
                int(item["id"]),
                str(article["article_hash"]),
            )
            for article, item in zip(batch, items)
        )
        assert generation_state["snapshot"]["required_target_ids"] == [1, 2]
        return {
            "articles": len(items),
            "synonyms": 24,
            "embedding_jobs": 2,
            "synonym_collisions_rejected": 0,
        }

    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state",
        capture,
    )
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(
        catalog,
        "preview_auto_synonym_replacements",
        preview,
    )
    monkeypatch.setattr(catalog, "_write_auto_enrichment_server", write_server)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 2)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats["articles"] == 2
    assert len(vertex_requests) == 2
    assert captures == [("memphis", True)]
    assert len(previews) == 1
    assert writes == [
        (1, "hash-1"),
        (2, "hash-2"),
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
    "патруль",
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


def test_auto_enrichment_payload_uses_only_source_context():
    articles = [
        {
            "id": article_id,
            "law_document_id": 10,
            "source_order": article_id,
            "kind": "article",
            "code": str(article_id),
            "article_ref": f"Article {article_id}",
            "title": f"Article {article_id}",
            "doc_title": "Criminal code",
            "full_text": f"Text {article_id}",
            "article_hash": f"hash-{article_id}",
            "current_auto_synonyms": (
                ["  OLD   TERM  "] if article_id in {2, 3} else []
            ),
        }
        for article_id in range(1, 6)
    ] + [{
        "id": 99,
        "law_document_id": 20,
        "source_order": 1,
        "kind": "article",
        "code": "99",
        "article_ref": "Article 99",
        "title": "Other document",
        "doc_title": "Other",
        "full_text": "Must not leak",
        "article_hash": "hash-99",
        "current_auto_synonyms": [],
    }]

    payload = catalog._build_auto_enrichment_payload(
        [articles[2]],
        articles,
    )

    assert payload[0]["id"] == 3
    assert "current_auto_synonyms" not in payload[0]
    assert [
        item["id"] for item in payload[0]["contrast_context"]
    ] == [1, 2, 4, 5]
    assert all(
        "current_auto_synonyms" not in item
        for item in payload[0]["contrast_context"]
    )
    assert 99 not in {
        item["id"] for item in payload[0]["contrast_context"]
    }


def test_auto_enrichment_payload_never_truncates_target_text_and_splits_batches():
    first = {
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "kind": "article",
        "code": "1",
        "article_ref": "Статья 1",
        "doc_title": "Кодекс",
        "full_text": "а" * 50_000,
        "article_hash": "hash-1",
    }
    second = {
        "id": 2,
        "law_document_id": 10,
        "source_order": 2,
        "kind": "article",
        "code": "2",
        "article_ref": "Статья 2",
        "doc_title": "Кодекс",
        "full_text": "б" * 70_000,
        "article_hash": "hash-2",
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
    assert len(payload[0]["text"]) == 50_000


def test_auto_enrichment_payload_ignores_generated_contrast_output_but_tracks_source(
    monkeypatch,
):
    articles = [
        {
            "id": 1,
            "law_document_id": 10,
            "source_order": 1,
            "kind": "article",
            "code": "1",
            "article_ref": "Статья 1",
            "title": "Generated target title",
            "doc_title": "Кодекс",
            "full_text": "Статья 1\nДействующая первая норма.",
            "article_hash": "hash-1",
            "current_auto_synonyms": ["прежняя точная фраза"],
            "penalty_summary": "generated target penalty",
            "penalty_json": {"fine": "$100"},
        },
        {
            "id": 2,
            "law_document_id": 10,
            "source_order": 2,
            "kind": "article",
            "code": "2",
            "article_ref": "Статья 2 Название источника",
            "title": "Generated neighbour title",
            "doc_title": "Кодекс",
            "full_text": "Статья 2 Название источника\nДействующая вторая норма.",
            "article_hash": "hash-2",
            "current_auto_synonyms": ["старый чужой синоним"],
            "penalty_summary": "generated neighbour penalty",
            "penalty_json": {"fine": "$200"},
        },
        {
            "id": 3,
            "law_document_id": 20,
            "source_order": 1,
            "kind": "article",
            "code": "3",
            "article_ref": "Статья 3 Конкурирующая норма",
            "title": "Generated competitor title",
            "doc_title": "Другой кодекс",
            "full_text": "Статья 3\nКонкурирующая исходная норма.",
            "article_hash": "hash-3",
            "current_auto_synonyms": ["старый синоним конкурента"],
            "penalty_summary": "generated competitor penalty",
            "penalty_json": {"fine": "$300"},
        },
    ]
    monkeypatch.setattr(
        catalog,
        "select_enrichment_competitors",
        lambda _targets, _corpus, **_kwargs: {
            1: (SimpleNamespace(article_id=3),),
        },
    )
    baseline = catalog._build_auto_enrichment_payload(
        [articles[0]],
        articles,
    )
    assert [item["id"] for item in baseline[0]["enrichment_competitors"]] == [
        3
    ]

    output_mutated = deepcopy(articles)
    output_mutated[0]["title"] = "Another generated target title"
    output_mutated[0]["current_auto_synonyms"] = [
        "another generated target synonym"
    ]
    output_mutated[0]["penalty_summary"] = "another generated penalty"
    output_mutated[0]["penalty_json"] = {"prison": "10 years"}
    output_mutated[1]["title"] = "Another generated neighbour title"
    output_mutated[1]["current_auto_synonyms"] = ["другой чужой синоним"]
    output_mutated[1]["penalty_summary"] = "changed neighbour penalty"
    output_mutated[1]["penalty_json"] = {"dismissal": "yes"}
    output_mutated[2]["title"] = "Another generated competitor title"
    output_mutated[2]["current_auto_synonyms"] = [
        "another generated competitor synonym"
    ]
    output_mutated[2]["penalty_summary"] = "changed competitor penalty"
    output_mutated[2]["penalty_json"] = {"prison": "20 years"}
    assert catalog._build_auto_enrichment_payload(
        [output_mutated[0]],
        output_mutated,
    ) == baseline

    source_mutated = deepcopy(articles)
    source_mutated[1]["full_text"] += " Новое доказанное условие."
    assert catalog._build_auto_enrichment_payload(
        [source_mutated[0]],
        source_mutated,
    ) != baseline
    competitor_source_mutated = deepcopy(articles)
    competitor_source_mutated[2]["full_text"] += (
        " Новое доказанное условие конкурента."
    )
    assert catalog._build_auto_enrichment_payload(
        [competitor_source_mutated[0]],
        competitor_source_mutated,
    ) != baseline


def test_auto_enrichment_payload_tracks_shared_structural_ancestry():
    rows = [
        {
            "id": 10,
            "law_document_id": 10,
            "source_order": 1,
            "kind": "chapter",
            "code": "I",
            "article_ref": "Глава I. Общие правила",
            "title": "Общие правила",
            "doc_title": "Кодекс",
            "full_text": "Глава I. Общие правила",
            "article_hash": "chapter-1",
        },
        {
            "id": 11,
            "law_document_id": 10,
            "source_order": 2,
            "kind": "article",
            "code": "1",
            "article_ref": "Статья 1",
            "title": "Generated title",
            "doc_title": "Кодекс",
            "full_text": "Статья 1\nОдинаковый неизменный текст нормы.",
            "article_hash": "article-1",
        },
        {
            "id": 20,
            "law_document_id": 10,
            "source_order": 3,
            "kind": "chapter",
            "code": "II",
            "article_ref": "Глава II. Специальные правила",
            "title": "Специальные правила",
            "doc_title": "Кодекс",
            "full_text": "Глава II. Специальные правила",
            "article_hash": "chapter-2",
        },
        {
            "id": 21,
            "law_document_id": 10,
            "source_order": 4,
            "kind": "article",
            "code": "2",
            "article_ref": "Статья 2",
            "title": "Other generated title",
            "doc_title": "Кодекс",
            "full_text": "Статья 2\nДругая норма.",
            "article_hash": "article-2",
        },
    ]
    baseline = catalog._build_auto_enrichment_payload(
        [rows[1]],
        rows,
    )[0]

    moved = deepcopy(rows)
    moved[1]["source_order"] = 5
    after = catalog._build_auto_enrichment_payload(
        [moved[1]],
        moved,
    )[0]

    assert baseline["text"] == after["text"]
    assert baseline["structural_ancestry"]["chapter"]["id"] == 10
    assert after["structural_ancestry"]["chapter"]["id"] == 20
    assert all(
        "structural_ancestry" in item
        for item in after["contrast_context"]
    )


def test_auto_enrichment_partition_measures_full_context_and_review_prompt():
    articles = [
        {
            "id": article_id,
            "law_document_id": 10,
            "source_order": article_id,
            "kind": "article",
            "code": str(article_id),
            "article_ref": f"Статья {article_id}",
            "doc_title": "Кодекс",
            "full_text": (
                f"Статья {article_id}\n"
                + chr(1_071 + article_id) * 5_000
            ),
            "article_hash": f"hash-{article_id}",
        }
        for article_id in range(1, 16)
    ]

    batches = catalog._partition_auto_enrichment_batches(
        articles,
        maximum_articles=15,
        all_articles=articles,
        project_context="source-only project context",
    )

    assert len(batches) > 1
    for batch in batches:
        payload = catalog._build_auto_enrichment_payload(
            batch,
            articles,
        )
        assert catalog._auto_enrichment_prompt_size(
            payload,
            project_context="source-only project context",
        ) <= catalog._AUTO_ENRICH_BATCH_TEXT_BUDGET


def test_oversized_nonexact_full_prompt_fails_before_network():
    article = {
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "kind": "article",
        "code": "1",
        "article_ref": "Статья 1",
        "doc_title": "Кодекс",
        "full_text": "а" * 115_000,
        "article_hash": "hash-1",
    }

    with pytest.raises(ValueError, match="before any network call"):
        catalog._partition_auto_enrichment_batches(
            [article],
            maximum_articles=15,
        )


def test_generation_and_review_prompts_share_source_faithfulness_rules():
    rules = catalog._ENRICH_SOURCE_FAITHFULNESS_RULES.strip()

    for prompt in (catalog._ENRICH_PROMPT, catalog._ENRICH_REVIEW_PROMPT):
        assert rules in prompt
        assert "Не усиливай модальность" in prompt
        assert "орган/лицо, основание, условие" in prompt
        assert "другому закону" in prompt
        assert "Префикс exact_body_group" in prompt


@pytest.mark.asyncio
async def test_auto_enrichment_uses_snapshot_service_and_returns_when_no_targets(
    monkeypatch,
):
    captures = []

    async def capture(server_id, *, only_stale):
        captures.append((server_id, only_stale))
        return _auto_generation_state_for_tests([])

    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state",
        capture,
    )

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats == {
        "articles": 0,
        "synonyms": 0,
        "batches_failed": 0,
        "articles_dropped": 0,
        "synonyms_dropped": 0,
        "articles_below_quality_target": 0,
        "reenrichment_article_ids": [],
    }
    assert captures == [("memphis", True)]


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
    first["synonyms"][0] = "угон автомобиля"
    second["synonyms"][0] = "угон автомобиля"

    with pytest.raises(ValueError, match="canonical synonym groups"):
        catalog._validate_auto_enrichment_response(
            [first, second],
            batch,
        )


def test_auto_enrichment_response_rejects_source_unsupported_synonym():
    full_text = (
        "Статья 1\n"
        "1) Лицензия может быть аннулирована судом при мошенничестве."
    )
    batch = [{
        "id": 1,
        "article_ref": "Статья 1",
        "full_text": full_text,
    }]
    item = _auto_enrichment_item(1, "лицензия")
    source_title = catalog.source_catalog_title("Статья 1", full_text)
    if source_title is not None:
        item["title"] = source_title
    item["synonyms"][0] = (
        "автоматическое аннулирование лицензии полицией за опоздание"
    )

    with pytest.raises(ValueError, match="instrument_revocation"):
        catalog._validate_auto_enrichment_response([item], batch)


def test_auto_enrichment_response_rejects_source_unsupported_generated_title():
    full_text = (
        "Статья 1\n"
        "1) Лицензия может быть аннулирована организацией."
    )
    batch = [{
        "id": 1,
        "article_ref": "Статья 1",
        "kind": "article",
        "code": "1",
        "doc_title": "Закон о лицензиях",
        "full_text": full_text,
    }]
    item = _auto_enrichment_item(1, "лицензия")
    item["title"] = "Обязательное аннулирование лицензии судом"

    with pytest.raises(
        ValueError,
        match=r"title evidence: source-unsupported semantic claim",
    ):
        catalog._validate_auto_enrichment_response([item], batch)


def test_auto_enrichment_response_allows_only_empty_tombstone_enrichment():
    batch = [{
        "id": 7,
        "article_ref": "Статья 7",
        "kind": "article",
        "code": "7",
        "full_text": "Статья 7\nУпразднена.",
    }]
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
        "article_ref": "Статья 11",
        "kind": "article",
        "code": "11",
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

    async def capture(_server_id, *, only_stale):
        assert only_stale is True
        return _auto_generation_state_for_tests(articles)

    class Response:
        text = json.dumps([_auto_enrichment_item(1, "first")])

    attempts = []

    async def execute_vertex_request(**_kwargs):
        attempts.append(True)
        return Response()

    async def forbidden_write(*_args, **_kwargs):
        raise AssertionError("an incomplete batch must not write any article")

    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state",
        capture,
    )
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(
        catalog,
        "_write_auto_enrichment_server",
        forbidden_write,
    )
    monkeypatch.setattr(
        catalog,
        "preview_auto_synonym_replacements",
        forbidden_write,
    )
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 2)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats == {
        "articles": 0,
        "synonyms": 0,
        "batches_failed": 1,
        "articles_dropped": 0,
        "synonyms_dropped": 0,
        "articles_below_quality_target": 0,
        "reenrichment_article_ids": [],
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

    async def capture(_server_id, *, only_stale):
        assert only_stale is True
        return _auto_generation_state_for_tests(articles)

    async def execute_vertex_request(**_kwargs):
        return type("Response", (), {"text": responses.pop(0)})()

    async def forbidden_write(*_args, **_kwargs):
        raise AssertionError("an invalid review must block every write")

    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state",
        capture,
    )
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(
        catalog,
        "_write_auto_enrichment_server",
        forbidden_write,
    )
    monkeypatch.setattr(
        catalog,
        "preview_auto_synonym_replacements",
        forbidden_write,
    )
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
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

    async def capture(_server_id, *, only_stale):
        assert only_stale is True
        return _auto_generation_state_for_tests(articles)

    async def execute_vertex_request(**_kwargs):
        return type("Response", (), {"text": responses.pop(0)})()

    writes = []

    async def preview(_server_id, _items, *, generation_snapshot):
        assert generation_snapshot["required_target_ids"] == [1]
        return _successful_auto_preview(articles)

    async def write_server(_server_id, batch, items, _generation_state):
        writes.append((
            int(items[0]["id"]),
            set(items[0]["synonyms"]),
        ))
        return {
            "articles": len(batch),
            "synonyms": 12,
            "embedding_jobs": 1,
            "synonym_collisions_rejected": 0,
        }

    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state",
        capture,
    )
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(
        catalog,
        "preview_auto_synonym_replacements",
        preview,
    )
    monkeypatch.setattr(catalog, "_write_auto_enrichment_server", write_server)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 1)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats["articles"] == 1
    assert writes[0][0] == 1
    assert writes[0][1] == set(reviewed["synonyms"])
    assert writes[0][1].isdisjoint(draft["synonyms"])


def test_derive_catalog_fields_proves_source_title_and_titleless_body():
    assert catalog.derive_catalog_fields(
        "Статья 7 Кража",
        "Статья 7 Кража\nПодробная действующая норма.",
    ) == ("article", "7", "Кража")
    assert catalog.derive_catalog_fields(
        "Статья 8",
        "Статья 8\nХалатность\n1) Подробная действующая норма.",
    ) == ("article", "8", "Халатность")
    assert catalog.derive_catalog_fields(
        "Статья 9",
        "Статья 9\n1) Подробная действующая норма.",
    ) == ("article", "9", None)
    assert catalog.derive_catalog_fields(
        "Статья 10",
        "Статья 10\nУтратила силу.",
    ) == ("article", "10", None)


def test_auto_title_provenance_preserves_source_replaces_generated_and_blocks_ambiguous():
    source_owned = {
        "id": 1,
        "article_ref": "Статья 1 Кража",
        "kind": "article",
        "code": "1",
        "title": "Кража",
        "full_text": "Статья 1 Кража\nПодробная норма.",
        "article_hash": "hash-1",
        "enrich_hash": None,
    }
    assert catalog._desired_auto_enrichment_title(
        source_owned,
        "Кража",
    ) == "Кража"
    with pytest.raises(catalog.SynonymWriteError, match="source-owned"):
        catalog._desired_auto_enrichment_title(
            source_owned,
            "Другое название",
        )

    generated = {
        "id": 2,
        "article_ref": "Статья 2",
        "kind": "article",
        "code": "2",
        "title": "Старое название",
        "full_text": "Статья 2\n1) Подробная норма.",
        "article_hash": "hash-2",
        "enrich_hash": "hash-2",
        "title_provenance": catalog.TITLE_PROVENANCE_GENERATED,
    }
    assert catalog._desired_auto_enrichment_title(
        generated,
        "Новое название",
    ) == "Новое название"
    generated["title_provenance"] = (
        catalog.TITLE_PROVENANCE_LEGACY_UNKNOWN
    )
    with pytest.raises(catalog.SynonymWriteError, match="unattested ownership"):
        catalog._desired_auto_enrichment_title(
            generated,
            "Новое название",
        )


def test_exact_duplicate_group_is_never_split_by_article_or_text_limit():
    # Two raw copies exceed the 120k prompt budget, while the shared exact
    # operative body sent once remains safely below it.
    body = "Одинаковая подробная действующая норма без санкции. " * 1_400
    rows = [
        {
            "id": 10,
            "law_document_id": 1,
            "source_order": 1,
            "article_ref": "Глава I",
            "kind": "chapter",
            "code": "I",
            "title": "Первая область",
            "doc_title": "Закон",
            "full_text": "Глава I\nПервая область",
            "article_hash": "chapter-1",
        },
        {
            "id": 11,
            "law_document_id": 1,
            "source_order": 2,
            "article_ref": "Статья 1",
            "kind": "article",
            "code": "1",
            "title": None,
            "doc_title": "Закон",
            "full_text": f"Статья 1\n{body}",
            "article_hash": "article-1",
        },
        {
            "id": 20,
            "law_document_id": 1,
            "source_order": 3,
            "article_ref": "Глава II",
            "kind": "chapter",
            "code": "II",
            "title": "Вторая область",
            "doc_title": "Закон",
            "full_text": "Глава II\nВторая область",
            "article_hash": "chapter-2",
        },
        {
            "id": 21,
            "law_document_id": 1,
            "source_order": 4,
            "article_ref": "Статья 2",
            "kind": "article",
            "code": "2",
            "title": None,
            "doc_title": "Закон",
            "full_text": f"Статья 2\n{body}",
            "article_hash": "article-2",
        },
    ]
    plan = catalog.build_exact_duplicate_plan(rows)
    articles = [row for row in rows if row["kind"] == "article"]

    batches = catalog._partition_auto_enrichment_batches(
        articles,
        maximum_articles=1,
        exact_plan=plan,
    )

    assert [[item["id"] for item in batch] for batch in batches] == [[11, 21]]

    group = plan.groups[0]
    generated = []
    for member in group.members:
        generated.append({
            "id": member.article_id,
            "title": (
                f"{member.title_scope_prefix}Общая действующая норма"
            ),
            "penalty_summary": None,
            "penalty": None,
            "synonyms": [
                f"{member.synonym_scope_prefix}"
                f"точная формулировка про {suffix}"
                for suffix in _AUTO_TEST_SUFFIXES[:12]
            ],
        })
    validated = catalog._validate_auto_enrichment_response(
        generated,
        articles,
        exact_plan=plan,
    )
    assert all(
        item["synonyms"][0].startswith("[")
        and item["synonyms"][0] == item["synonyms"][0].lower()
        for item in validated
    )

    payload = catalog._build_auto_enrichment_payload(
        articles,
        articles,
        exact_plan=plan,
    )
    exact_payloads = [item["exact_body_group"] for item in payload]
    assert sum(
        value["shared_operative_body"] is not None
        for value in exact_payloads
    ) == 1
    assert {
        "required_title_scope_prefix",
        "required_synonym_scope_prefix",
    } <= set(exact_payloads[0]["members"][0])


def test_oversized_indivisible_exact_group_fails_before_network():
    body = "Одинаковая норма с большим исходным текстом. " * 3_200
    rows = [
        {
            "id": 1,
            "law_document_id": 1,
            "source_order": 1,
            "article_ref": "Глава I",
            "kind": "chapter",
            "code": "I",
            "title": "Первая область",
            "doc_title": "Закон",
            "full_text": "Глава I\nПервая область",
            "article_hash": "chapter-1",
        },
        {
            "id": 2,
            "law_document_id": 1,
            "source_order": 2,
            "article_ref": "Статья 1",
            "kind": "article",
            "code": "1",
            "title": None,
            "doc_title": "Закон",
            "full_text": f"Статья 1\n{body}",
            "article_hash": "article-1",
        },
        {
            "id": 3,
            "law_document_id": 1,
            "source_order": 3,
            "article_ref": "Глава II",
            "kind": "chapter",
            "code": "II",
            "title": "Вторая область",
            "doc_title": "Закон",
            "full_text": "Глава II\nВторая область",
            "article_hash": "chapter-2",
        },
        {
            "id": 4,
            "law_document_id": 1,
            "source_order": 4,
            "article_ref": "Статья 2",
            "kind": "article",
            "code": "2",
            "title": None,
            "doc_title": "Закон",
            "full_text": f"Статья 2\n{body}",
            "article_hash": "article-2",
        },
    ]
    plan = catalog.build_exact_duplicate_plan(rows)
    with pytest.raises(ValueError, match="exceeds the model text budget"):
        catalog._partition_auto_enrichment_batches(
            [row for row in rows if row["kind"] == "article"],
            maximum_articles=15,
            exact_plan=plan,
        )


# --- Смена контракта атомарности (M1) --------------------------------------
#
# ЭТОТ ТЕСТ ПЕРЕПИСАН, И ЭТО НЕ ПОЧИНКА РАДИ ЗЕЛЕНИ.  Он кодифицировал
# правило «упал поздний батч — не пишем НИЧЕГО», и заказчик это правило
# отменил явным решением.
#
# Основание решения, проверенное по коду: выборка идёт с ``only_stale=True``,
# а свежесть считается по хешам статьи, контекста и вывода.  Записанная статья
# перестаёт быть устаревшей и в следующий прогон не попадает; пропущенная
# остаётся устаревшей и придёт.  Значит частичная запись ВОЗОБНОВЛЯЕМА ПО
# ПОСТРОЕНИЮ, и прежний контракт защищал от несуществующей проблемы, отдавая
# за это ноль записей: одна негодная статья роняла весь сервер.
#
# Ниже проверяется НОВЫЙ контракт, и в главном он строже прежнего: маркер
# повтора обязан подняться, иначе остаток потеряется молча.
@pytest.mark.asyncio
async def test_later_batch_failure_writes_prior_batches_and_keeps_retry_marker(
    monkeypatch,
):
    """Поздний провал больше не стирает добытое, но и не выдаёт себя за успех."""

    import rate_limiter

    articles = [
        {
            "id": article_id,
            "code": str(article_id),
            "title": None,
            "full_text": f"Distinct text {article_id}",
            "article_hash": f"hash-{article_id}",
            "enrich_hash": None,
            "title_provenance": None,
            "enrichment_context_hash": None,
        }
        for article_id in (1, 2)
    ]

    async def capture(_server_id, *, only_stale):
        assert only_stale is True
        return _auto_generation_state_for_tests(articles)

    recaptures = []

    async def capture_targets(server_id, target_ids):
        recaptures.append((server_id, list(target_ids)))
        return _auto_generation_state_for_tests(
            [row for row in articles if int(row["id"]) in set(target_ids)]
        )

    first = _auto_enrichment_item(1, "first")
    responses = [
        json.dumps([first]),
        json.dumps([first]),
        "[]",
        "[]",
    ]

    async def execute_vertex_request(**_kwargs):
        return type("Response", (), {"text": responses.pop(0)})()

    async def preview(_server_id, items, *, generation_snapshot):
        return _successful_auto_preview([{"id": item["id"]} for item in items])

    writes = []

    async def write_server(server_id, batch, items, generation_state):
        writes.append([int(item["id"]) for item in items])
        # Снимок обязан быть пересобран ровно под уцелевший набор, иначе
        # писатель честно отвергнет запись как рассинхронизированную.
        assert generation_state["snapshot"]["required_target_ids"] == [1]
        return {
            "articles": len(items), "synonyms": 12,
            "embedding_jobs": 1, "synonym_collisions_rejected": 0,
        }

    monkeypatch.setattr(
        catalog, "capture_auto_replacement_generation_state", capture,
    )
    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state_for_targets",
        capture_targets,
    )
    monkeypatch.setattr(
        rate_limiter, "execute_vertex_request", execute_vertex_request,
    )
    monkeypatch.setattr(catalog, "preview_auto_synonym_replacements", preview)
    monkeypatch.setattr(catalog, "_write_auto_enrichment_server", write_server)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 1)
    monkeypatch.setattr(
        catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0,
    )

    stats = await catalog.enrich_articles_for_server("memphis")

    # Добытое записано.
    assert writes == [[1]]
    assert stats["articles"] == 1
    assert recaptures == [("memphis", [1])]
    # МАРКЕР ПОВТОРА ЖИВ: провалившийся батч посчитан, значит сервер не будет
    # объявлен завершённым и остаток переспросят.
    assert stats["batches_failed"] == 1
    assert responses == []


@pytest.mark.asyncio
async def test_partial_write_is_resumable_and_fails_closed_until_complete(
    monkeypatch,
):
    """Возобновляемость: остаток докатывается следующим прогоном.

    Первый прогон пишет одну статью из двух и поднимает маркер повтора;
    ``require_successful_enrichment_completion`` по этому маркеру ставит
    ``derived_sync_pending`` и ВСЁ РАВНО падает — частичная запись не
    выдаётся за завершённый сервер.  Второй прогон видит устаревшей уже
    только пропущенную статью (записанная свежа по хешам) и дописывает её.
    """

    import rate_limiter

    articles = [
        {
            "id": article_id,
            "code": str(article_id),
            "title": None,
            "full_text": f"Distinct text {article_id}",
            "article_hash": f"hash-{article_id}",
            "enrich_hash": None,
            "title_provenance": None,
            "enrichment_context_hash": None,
        }
        for article_id in (1, 2)
    ]
    # Модель свежести по хешам: записанная статья получает
    # ``enrich_hash == article_hash`` и перестаёт быть устаревшей — ровно то,
    # что делает настоящий писатель, и ровно то, на чём стоит возобновляемость.
    written_fresh = set()

    def _stale_rows():
        return [
            dict(row)
            for row in articles
            if int(row["id"]) not in written_fresh
        ]

    async def capture(_server_id, *, only_stale):
        assert only_stale is True
        return _auto_generation_state_for_tests(_stale_rows())

    async def capture_targets(server_id, target_ids):
        return _auto_generation_state_for_tests(
            [row for row in _stale_rows() if int(row["id"]) in set(target_ids)]
        )

    first = _auto_enrichment_item(1, "first")
    second = _auto_enrichment_item(2, "second")
    responses = [
        json.dumps([first]), json.dumps([first]),
        "[]", "[]",
        json.dumps([second]), json.dumps([second]),
    ]

    async def execute_vertex_request(**_kwargs):
        return type("Response", (), {"text": responses.pop(0)})()

    async def preview(_server_id, items, *, generation_snapshot):
        return _successful_auto_preview([{"id": item["id"]} for item in items])

    writes = []

    async def write_server(server_id, batch, items, generation_state):
        ids = [int(item["id"]) for item in items]
        writes.append(ids)
        written_fresh.update(ids)
        return {
            "articles": len(items), "synonyms": 12 * len(items),
            "embedding_jobs": len(items), "synonym_collisions_rejected": 0,
        }

    pending_marks = []

    async def mark_pending(server_id, document_ids=None):
        pending_marks.append((server_id, document_ids))
        return ()

    monkeypatch.setattr(
        catalog, "capture_auto_replacement_generation_state", capture,
    )
    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state_for_targets",
        capture_targets,
    )
    monkeypatch.setattr(
        rate_limiter, "execute_vertex_request", execute_vertex_request,
    )
    monkeypatch.setattr(catalog, "preview_auto_synonym_replacements", preview)
    monkeypatch.setattr(catalog, "_write_auto_enrichment_server", write_server)
    monkeypatch.setattr(catalog, "_mark_enrichment_pending", mark_pending)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 1)
    monkeypatch.setattr(
        catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0,
    )

    first_stats = await catalog.enrich_articles_for_server("memphis")
    assert writes == [[1]]
    assert first_stats["batches_failed"] == 1

    # Частичная запись НЕ выдаётся за завершённый сервер: маркер повтора
    # поставлен и вызывающий получает отказ.
    with pytest.raises(catalog.SynonymWriteError, match="failed batches=1"):
        await catalog.require_successful_enrichment_completion(
            "memphis", first_stats,
        )
    assert pending_marks == [("memphis", None)]

    # Следующий прогон видит устаревшей только пропущенную статью.
    second_stats = await catalog.enrich_articles_for_server("memphis")

    assert writes == [[1], [2]]
    assert second_stats["batches_failed"] == 0
    assert second_stats["articles"] == 1
    assert written_fresh == {1, 2}
    assert responses == []


@pytest.mark.asyncio
async def test_combined_cross_batch_collision_blocks_preview_and_write(
    monkeypatch,
):
    import rate_limiter

    articles = [
        {
            "id": article_id,
            "code": str(article_id),
            "title": None,
            "full_text": f"Distinct text {article_id}",
            "article_hash": f"hash-{article_id}",
            "enrich_hash": None,
        }
        for article_id in (1, 2)
    ]

    async def capture(_server_id, *, only_stale):
        return _auto_generation_state_for_tests(articles)

    first = _auto_enrichment_item(1, "first")
    second = _auto_enrichment_item(2, "second")
    first["synonyms"][0] = "shared exact ownership phrase"
    second["synonyms"][0] = "shared exact ownership phrase"
    responses = [
        json.dumps([first]),
        json.dumps([first]),
        json.dumps([second]),
        json.dumps([second]),
    ]

    async def execute_vertex_request(**_kwargs):
        return type("Response", (), {"text": responses.pop(0)})()

    async def forbidden_write(*_args, **_kwargs):
        raise AssertionError("combined collision must block DB access")

    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state",
        capture,
    )
    monkeypatch.setattr(rate_limiter, "execute_vertex_request", execute_vertex_request)
    monkeypatch.setattr(
        catalog,
        "preview_auto_synonym_replacements",
        forbidden_write,
    )
    monkeypatch.setattr(
        catalog,
        "_write_auto_enrichment_server",
        forbidden_write,
    )
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 1)
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0)

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats == {
        "articles": 0,
        "synonyms": 0,
        "batches_failed": 1,
        "articles_dropped": 0,
        "synonyms_dropped": 0,
        "articles_below_quality_target": 0,
        "reenrichment_article_ids": [],
    }
    assert responses == []


@pytest.mark.asyncio
async def test_server_metadata_failure_rolls_back_staged_synonyms_and_jobs(
    monkeypatch,
):
    articles = [
        {
            "id": article_id,
            "law_document_id": 10,
            "source_order": article_id,
            "article_ref": f"Статья {article_id}",
            "kind": "article",
            "code": str(article_id),
            "title": None,
            "doc_title": "Кодекс",
            "full_text": f"Статья {article_id}\n1) Норма {article_id}.",
            "article_hash": f"hash-{article_id}",
            "enrich_hash": None,
        }
        for article_id in (1, 2)
    ]
    state = {
        "synonyms": ["old"],
        "titles": {1: None, 2: None},
        "jobs": [],
    }

    class Connection:
        def transaction(self):
            @asynccontextmanager
            async def savepoint():
                before = deepcopy(state)
                try:
                    yield self
                except BaseException:
                    state.clear()
                    state.update(before)
                    raise

            return savepoint()

        async def execute(self, _query, *_args):
            return "OK"

        async def fetch(self, _query, *_args):
            return []

        async def fetchval(self, query, *args):
            assert query.lstrip().startswith("UPDATE law_articles")
            article_id = int(args[0])
            if article_id == 2:
                return None
            state["titles"][article_id] = args[1]
            return article_id

    conn = Connection()

    @asynccontextmanager
    async def transaction():
        before = deepcopy(state)
        try:
            yield conn
        except BaseException:
            state.clear()
            state.update(before)
            raise

    async def bulk(_conn, _server_id, _items, **_kwargs):
        state["synonyms"] = ["new"]
        return SimpleNamespace(
            result=SimpleNamespace(
                inserted=24,
                rejected_collisions=0,
            ),
        )

    async def ensure(_conn, article_id):
        state["jobs"].append(article_id)
        return True

    monkeypatch.setattr(catalog.db_manager, "transaction", transaction)
    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )
    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)
    catalog_rows = [dict(article) for article in articles]
    generation_state = {
        "snapshot": _writer_generation_snapshot(
            catalog_rows,
            required_target_ids=[1, 2],
        ),
        "catalog_rows": catalog_rows,
        "auto_fingerprints": {1: "1" * 64, 2: "2" * 64},
        "context_hashes": _writer_context_hashes(catalog_rows),
    }
    items = [
        {
            "id": article_id,
            "title": f"Новое название {article_id}",
            "penalty_summary": None,
            "penalty": None,
            "synonyms": _auto_enrichment_item(
                article_id,
                f"article {article_id}",
            )["synonyms"],
        }
        for article_id in (1, 2)
    ]

    with pytest.raises(catalog.SynonymWriteError, match="metadata/title CAS"):
        await catalog._write_auto_enrichment_server(
            "memphis",
            articles,
            items,
            generation_state,
        )

    assert state == {
        "synonyms": ["old"],
        "titles": {1: None, 2: None},
        "jobs": [],
    }


@pytest.mark.asyncio
async def test_server_metadata_cas_rejects_penalty_only_race_and_rolls_back(
    monkeypatch,
):
    article = {
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "article_ref": "Статья 1",
        "kind": "article",
        "code": "1",
        "title": None,
        "doc_title": "Code",
        "full_text": "Статья 1\n1) Норма 1. Наказание: штраф 100$.",
        "article_hash": "hash-1",
        "enrich_hash": None,
        "title_provenance": None,
        "enrichment_context_hash": None,
        "penalty_summary": "fine $100",
        "penalty_json": {"fine": "$100"},
    }
    # Model a newer penalty-only correction committed after capture.  The
    # outer writer must preserve this state and roll back synonyms too.
    state = {
        "synonyms": ["old"],
        "title": None,
        "penalty_summary": "newer manual correction $500",
        "penalty_json": {"fine": "$500"},
        "jobs": [],
    }
    seen_update = {}

    class Connection:
        def transaction(self):
            @asynccontextmanager
            async def savepoint():
                before = deepcopy(state)
                try:
                    yield self
                except BaseException:
                    state.clear()
                    state.update(before)
                    raise

            return savepoint()

        async def execute(self, _query, *_args):
            return "OK"

        async def fetch(self, _query, *_args):
            return []

        async def fetchval(self, query, *args):
            assert query.lstrip().startswith("UPDATE law_articles")
            seen_update["query"] = query
            seen_update["args"] = args
            assert (
                "penalty_summary IS NOT DISTINCT FROM $19"
                in query
            )
            assert (
                "penalty_json IS NOT DISTINCT FROM $20::jsonb"
                in query
            )
            captured_summary = args[18]
            captured_penalty = (
                json.loads(args[19]) if args[19] is not None else None
            )
            if (
                state["penalty_summary"] != captured_summary
                or state["penalty_json"] != captured_penalty
            ):
                return None
            state["title"] = args[1]
            state["penalty_summary"] = args[2]
            state["penalty_json"] = (
                json.loads(args[3]) if args[3] is not None else None
            )
            return int(args[0])

    conn = Connection()

    @asynccontextmanager
    async def transaction():
        before = deepcopy(state)
        try:
            yield conn
        except BaseException:
            state.clear()
            state.update(before)
            raise

    async def bulk(_conn, _server_id, _items, **_kwargs):
        state["synonyms"] = ["new"]
        return SimpleNamespace(
            result=SimpleNamespace(
                inserted=12,
                rejected_collisions=0,
            ),
        )

    async def ensure(_conn, article_id):
        state["jobs"].append(article_id)
        return True

    monkeypatch.setattr(catalog.db_manager, "transaction", transaction)
    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )
    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title_state",
        lambda _article, reviewed_title: (
            reviewed_title,
            catalog.TITLE_PROVENANCE_GENERATED,
        ),
    )
    catalog_rows = [dict(article)]
    generation_state = {
        "snapshot": _writer_generation_snapshot(
            catalog_rows,
            required_target_ids=[1],
        ),
        "catalog_rows": catalog_rows,
        "auto_fingerprints": {1: "1" * 64},
        "context_hashes": _writer_context_hashes(catalog_rows),
    }
    item = {
        "id": 1,
        "title": "Reviewed title",
        "penalty_summary": "штраф 100$",
        "penalty": {"fine": "100$"},
        "synonyms": _auto_enrichment_item(1, "penalty race")[
            "synonyms"
        ],
    }
    before = deepcopy(state)

    with pytest.raises(catalog.SynonymWriteError, match="metadata/title CAS"):
        await catalog._write_auto_enrichment_server(
            "memphis",
            [article],
            [item],
            generation_state,
        )

    assert seen_update
    assert state == before


@pytest.mark.asyncio
async def test_server_writer_rechecks_source_evidence_before_bulk_mutation(
    monkeypatch,
):
    full_text = (
        "Статья 1\n"
        "1) Лицензия может быть аннулирована судом при мошенничестве."
    )
    article = {
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "article_ref": "Статья 1",
        "kind": "article",
        "code": "1",
        "title": None,
        "doc_title": "Code",
        "full_text": full_text,
        "article_hash": "hash-1",
        "enrich_hash": None,
        "title_provenance": None,
        "enrichment_context_hash": None,
    }
    item = _auto_enrichment_item(1, "лицензия")
    item["synonyms"][0] = (
        "автоматическое аннулирование лицензии полицией за опоздание"
    )
    bulk_calls = []

    async def bulk(*args, **kwargs):
        bulk_calls.append((args, kwargs))
        raise AssertionError("bulk mutation must not be reached")

    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )

    with pytest.raises(
        catalog.SynonymWriteError,
        match=r"article 1.*instrument_revocation",
    ):
        await catalog._write_auto_enrichment_server_in_transaction(
            object(),
            "memphis",
            [article],
            [item],
            {
                "snapshot": _writer_generation_snapshot(
                    [dict(article)],
                    required_target_ids=[1],
                ),
                "catalog_rows": [dict(article)],
                "auto_fingerprints": {1: "1" * 64},
                "context_hashes": _writer_context_hashes(
                    [dict(article)]
                ),
            },
            synonym_lock_already_held=True,
        )

    assert bulk_calls == []


@pytest.mark.asyncio
async def test_server_writer_rechecks_generated_title_before_bulk_mutation(
    monkeypatch,
):
    full_text = (
        "Статья 1\n"
        "1) Лицензия может быть аннулирована организацией."
    )
    article = {
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "article_ref": "Статья 1",
        "kind": "article",
        "code": "1",
        "title": None,
        "doc_title": "Закон о лицензиях",
        "full_text": full_text,
        "article_hash": "hash-1",
        "enrich_hash": None,
        "title_provenance": None,
        "enrichment_context_hash": None,
    }
    item = _auto_enrichment_item(1, "лицензия")
    item["title"] = "Обязательное аннулирование лицензии судом"
    bulk_calls = []

    async def bulk(*args, **kwargs):
        bulk_calls.append((args, kwargs))
        raise AssertionError("bulk mutation must not be reached")

    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )

    with pytest.raises(
        catalog.SynonymWriteError,
        match=r"article 1 enrichment.*title evidence",
    ):
        await catalog._write_auto_enrichment_server_in_transaction(
            object(),
            "memphis",
            [article],
            [item],
            {
                "snapshot": _writer_generation_snapshot(
                    [dict(article)],
                    required_target_ids=[1],
                ),
                "catalog_rows": [dict(article)],
                "auto_fingerprints": {1: "1" * 64},
                "context_hashes": _writer_context_hashes(
                    [dict(article)]
                ),
            },
            synonym_lock_already_held=True,
        )

    assert bulk_calls == []


def _single_server_writer_article():
    full_text = (
        "\u0421\u0442\u0430\u0442\u044c\u044f 1\n"
        "1. \u041f\u043e\u0440\u044f\u0434\u043e\u043a "
        "\u0443\u0447\u0435\u0442\u0430 "
        "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u043e\u0432."
    )
    return {
        "id": 1,
        "law_document_id": 10,
        "source_order": 1,
        "article_ref": "\u0421\u0442\u0430\u0442\u044c\u044f 1",
        "kind": "article",
        "code": "1",
        "title": None,
        "doc_title": (
            "\u0413\u0440\u0430\u0436\u0434\u0430\u043d\u0441\u043a\u0438\u0439 "
            "\u043a\u043e\u0434\u0435\u043a\u0441"
        ),
        "full_text": full_text,
        "article_hash": catalog.source_article_hash(full_text),
        "enrich_hash": None,
        "penalty_summary": None,
        "penalty_json": None,
        "title_provenance": None,
        "enrichment_context_hash": None,
        "enrichment_output_hash": None,
    }


def _single_server_writer_item():
    return {
        "id": 1,
        "title": None,
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [
            (
                "\u043f\u043e\u0440\u044f\u0434\u043e\u043a "
                "\u0443\u0447\u0435\u0442\u0430 "
                "\u0434\u043e\u043a\u0443\u043c\u0435\u043d\u0442\u043e\u0432 "
                f"\u0432\u0430\u0440\u0438\u0430\u043d\u0442 {index}"
            )
            for index in range(12)
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("article_id", "item_id"),
    (
        (True, True),
        (1.9, 1.9),
        ("1", "1"),
        (1.1, 1.9),
        (True, 1.9),
    ),
)
async def test_server_writer_rejects_coercible_non_integer_target_ids(
    article_id,
    item_id,
):
    article = _single_server_writer_article()
    state = await _service_generated_exact_group_state([article])
    detached_article = deepcopy(state["articles"][0])
    detached_article["id"] = article_id
    item = _single_server_writer_item()
    item["id"] = item_id

    with pytest.raises(
        catalog.SynonymWriteError,
        match="integer article ids",
    ):
        await catalog._write_auto_enrichment_server_in_transaction(
            object(),
            "memphis",
            [detached_article],
            [item],
            state,
            synonym_lock_already_held=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ("auto_fingerprints", "context_hashes"))
@pytest.mark.parametrize("alias_key", (True, 1.0))
async def test_server_writer_rejects_non_integer_generation_map_keys(
    field,
    alias_key,
):
    article = _single_server_writer_article()
    state = await _service_generated_exact_group_state([article])
    state[field] = {alias_key: state[field][1]}

    with pytest.raises(
        catalog.SynonymWriteError,
        match="integer article ids",
    ):
        await catalog._write_auto_enrichment_server_in_transaction(
            object(),
            "memphis",
            [state["articles"][0]],
            [_single_server_writer_item()],
            state,
            synonym_lock_already_held=True,
        )


@pytest.mark.asyncio
async def test_server_writer_uses_bound_catalog_not_detached_article_context(
    monkeypatch,
):
    article = _single_server_writer_article()
    state = await _service_generated_exact_group_state([article])
    detached_article = deepcopy(state["articles"][0])
    detached_article["doc_title"] = (
        "\u0423\u0433\u043e\u043b\u043e\u0432\u043d\u044b\u0439 "
        "\u043a\u043e\u0434\u0435\u043a\u0441"
    )
    item = _single_server_writer_item()
    item["title"] = (
        "\u0423\u0433\u043e\u043b\u043e\u0432\u043d\u0430\u044f "
        "\u043e\u0442\u0432\u0435\u0442\u0441\u0442\u0432\u0435\u043d\u043d\u043e\u0441\u0442\u044c"
    )
    bulk_calls = []

    async def bulk(*args, **kwargs):
        bulk_calls.append((args, kwargs))
        raise AssertionError("detached article must not reach mutation")

    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )

    with pytest.raises(
        catalog.SynonymWriteError,
        match="title evidence",
    ):
        await catalog._write_auto_enrichment_server_in_transaction(
            object(),
            "memphis",
            [detached_article],
            [item],
            state,
            synonym_lock_already_held=True,
        )

    assert bulk_calls == []


@pytest.mark.asyncio
async def test_server_writer_rederives_context_hashes_from_bound_catalog():
    article = _single_server_writer_article()
    state = await _service_generated_exact_group_state([article])
    item = _single_server_writer_item()
    assert state["context_hashes"][1] != "f" * 64
    state["context_hashes"][1] = "f" * 64

    with pytest.raises(
        catalog.SynonymWriteError,
        match="context hashes differ from its bound catalog",
    ):
        await catalog._write_auto_enrichment_server_in_transaction(
            object(),
            "memphis",
            [state["articles"][0]],
            [item],
            state,
            synonym_lock_already_held=True,
        )


@pytest.mark.asyncio
async def test_server_writer_freezes_reviewed_item_before_first_await(
    monkeypatch,
):
    article = _single_server_writer_article()
    state = await _service_generated_exact_group_state([article])
    item = _single_server_writer_item()
    persisted_penalties = []

    class Connection:
        def transaction(self):
            return _noop_savepoint(self)

        async def fetch(self, _query, *_args):
            return []

        async def fetchval(self, query, *args):
            assert query.lstrip().startswith("UPDATE law_articles")
            persisted_penalties.append((args[2], args[3]))
            return int(args[0])

    async def acquire_and_mutate(*_args, **_kwargs):
        item["penalty_summary"] = "\u0448\u0442\u0440\u0430\u0444 $999999"
        item["penalty"] = {"fine": "$999999"}

    async def bulk(*_args, **_kwargs):
        return SimpleNamespace(
            result=SimpleNamespace(inserted=12, rejected_collisions=0)
        )

    async def no_embedding(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        catalog,
        "acquire_synonym_write_lock",
        acquire_and_mutate,
    )
    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )
    monkeypatch.setattr(
        catalog,
        "ensure_article_embedding_target",
        no_embedding,
    )

    await catalog._write_auto_enrichment_server_in_transaction(
        Connection(),
        "memphis",
        [state["articles"][0]],
        [item],
        state,
        synonym_lock_already_held=False,
    )

    assert item["penalty"] == {"fine": "$999999"}
    assert persisted_penalties == [(None, None)]


@pytest.mark.asyncio
async def test_server_writer_savepoint_rolls_back_synonyms_if_caller_catches(
    monkeypatch,
):
    article = _single_server_writer_article()
    state = await _service_generated_exact_group_state([article])
    item = _single_server_writer_item()
    persisted = {"synonyms": ["old"], "outer_committed": False}

    class Connection:
        def transaction(self):
            @asynccontextmanager
            async def savepoint():
                before = deepcopy(persisted)
                try:
                    yield self
                except BaseException:
                    persisted.clear()
                    persisted.update(before)
                    raise

            return savepoint()

        async def fetch(self, _query, *_args):
            return []

        async def fetchval(self, query, *_args):
            assert query.lstrip().startswith("UPDATE law_articles")
            return None

    conn = Connection()

    @asynccontextmanager
    async def outer_transaction():
        yield conn
        persisted["outer_committed"] = True

    async def bulk(*_args, **_kwargs):
        persisted["synonyms"] = ["new"]
        return SimpleNamespace(
            result=SimpleNamespace(inserted=12, rejected_collisions=0)
        )

    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )

    async with outer_transaction():
        with pytest.raises(
            catalog.SynonymWriteError,
            match="metadata/title CAS",
        ):
            await catalog._write_auto_enrichment_server_in_transaction(
                conn,
                "memphis",
                [state["articles"][0]],
                [item],
                state,
                synonym_lock_already_held=True,
            )

    assert persisted == {
        "synonyms": ["old"],
        "outer_committed": True,
    }


def _server_writer_exact_group_fixture():
    body = "Лицо обязано предъявить документы."
    catalog_rows = [
        {
            "id": 100,
            "law_document_id": 10,
            "source_order": 1,
            "article_ref": "Глава I",
            "kind": "chapter",
            "code": "I",
            "title": "Первая территория",
            "doc_title": "Кодекс",
            "full_text": "Глава I. Первая территория",
            "article_hash": "structure-100",
        },
        {
            "id": 1,
            "law_document_id": 10,
            "source_order": 2,
            "article_ref": "Статья 1",
            "kind": "article",
            "code": "1",
            "title": None,
            "title_provenance": None,
            "doc_title": "Кодекс",
            "full_text": f"Статья 1\n1. {body}",
            "article_hash": "hash-1",
            "enrich_hash": None,
            "enrichment_context_hash": None,
            "enrichment_output_hash": None,
            "penalty_summary": None,
            "penalty_json": None,
        },
        {
            "id": 200,
            "law_document_id": 10,
            "source_order": 3,
            "article_ref": "Глава II",
            "kind": "chapter",
            "code": "II",
            "title": "Вторая территория",
            "doc_title": "Кодекс",
            "full_text": "Глава II. Вторая территория",
            "article_hash": "structure-200",
        },
        {
            "id": 2,
            "law_document_id": 10,
            "source_order": 4,
            "article_ref": "Статья 2",
            "kind": "article",
            "code": "2",
            "title": None,
            "title_provenance": None,
            "doc_title": "Кодекс",
            "full_text": f"Статья 2\n1. {body}",
            "article_hash": "hash-2",
            "enrich_hash": None,
            "enrichment_context_hash": None,
            "enrichment_output_hash": None,
            "penalty_summary": None,
            "penalty_json": None,
        },
    ]
    for row in catalog_rows:
        row.setdefault("enrich_hash", None)
        row.setdefault("penalty_summary", None)
        row.setdefault("penalty_json", None)
        row.setdefault("title_provenance", None)
        row.setdefault("enrichment_context_hash", None)
        row.setdefault("enrichment_output_hash", None)
    articles = [
        dict(row)
        for row in catalog_rows
        if row["kind"] == "article"
    ]
    exact_group = catalog.build_exact_duplicate_plan(
        catalog_rows,
        canonicalize_scope=catalog.canonical_synonym_term,
    ).groups[0]
    items = [
        {
            "id": member.article_id,
            "title": (
                f"{member.title_scope_prefix}"
                "Обязанность предъявить документы"
            ),
            "penalty_summary": None,
            "penalty": None,
            "synonyms": [
                (
                    f"{member.synonym_scope_prefix}"
                    f"запрос об обязанности предъявить документы {index}"
                )
                for index in range(12)
            ],
        }
        for member in exact_group.members
    ]
    generation_state = {
        "snapshot": _writer_generation_snapshot(
            catalog_rows,
            required_target_ids=[1, 2],
        ),
        "catalog_rows": catalog_rows,
        "auto_fingerprints": {1: "1" * 64, 2: "2" * 64},
        "context_hashes": _writer_context_hashes(catalog_rows),
    }
    return articles, items, generation_state


async def _service_generated_exact_group_state(catalog_rows):
    owned_catalog = []
    for source in catalog_rows:
        row = dict(source)
        row.setdefault("server_id", "memphis")
        row.setdefault("parent_server_id", "memphis")
        owned_catalog.append(row)
    fingerprint = {
        "algorithm": "sha256",
        "snapshot_version": 3,
        "documents": 1,
        "value": "a" * 64,
    }
    truth = VerifiedLawCatalogTruth(
        server_id="memphis",
        source_documents=({
            "id": 10,
            "server_id": "memphis",
            "source_thread_id": 1,
            "title": "Synthetic writer fixture",
            "content": "Synthetic writer fixture source",
            "derived_sync_pending": False,
        },),
        catalog_rows=tuple(owned_catalog),
        binding={
            "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
            "parser_contract_version": (
                LAW_CATALOG_PARSER_CONTRACT_VERSION
            ),
            "server_id": "memphis",
            "source_fingerprint": fingerprint,
            "documents": 1,
            "catalog_rows": len(owned_catalog),
            "article_rows": sum(
                row["kind"] == "article" for row in owned_catalog
            ),
            "tombstone_rows": 0,
            "source_projection_hash": "b" * 64,
            "catalog_projection_hash": "c" * 64,
            "value": "d" * 64,
        },
    )

    class CaptureConnection:
        async def fetch(self, query, *_args):
            if "FROM law_synonyms" in query:
                return []
            raise AssertionError(query)

    async def load_truth(*_args, **_kwargs):
        return truth

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            synonym_service,
            "load_verified_law_catalog_truth",
            load_truth,
        )
        captured = (
            await synonym_service._load_auto_replacement_generation_state(
                CaptureConnection(),
                "memphis",
                selection_mode="all",
            )
        )
    return synonym_service._export_auto_replacement_generation_state(
        captured
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "defect",
    (
        "asymmetric_title",
        "asymmetric_synonyms",
        "asymmetric_penalty_summary",
        "asymmetric_penalty",
        "partial_group",
    ),
)
async def test_server_writer_rejects_invalid_exact_group_before_any_mutation(
    monkeypatch,
    defect,
):
    articles, items, generation_state = (
        _server_writer_exact_group_fixture()
    )
    if defect == "asymmetric_title":
        items[1]["title"] += " иначе"
        expected_error = "asymmetric base titles"
    elif defect == "asymmetric_synonyms":
        items[1]["synonyms"][0] += " иначе"
        expected_error = "asymmetric ordered synonym suffixes"
    elif defect == "asymmetric_penalty_summary":
        items[1]["penalty_summary"] = "Другая санкция"
        expected_error = "asymmetric penalty_summary"
    elif defect == "asymmetric_penalty":
        items[1]["penalty"] = {"fine": "100$"}
        expected_error = "asymmetric penalty"
    else:
        articles = articles[:1]
        items = items[:1]
        expected_error = "incomplete exact-body group"

    transaction_state = {"committed": False, "rolled_back": False}
    mutation_calls = []
    connection_calls = []

    class Connection:
        async def execute(self, *args, **kwargs):
            connection_calls.append(("execute", args, kwargs))
            raise AssertionError("exact-group rejection must precede DB access")

        async def fetch(self, *args, **kwargs):
            connection_calls.append(("fetch", args, kwargs))
            raise AssertionError("exact-group rejection must precede DB access")

        async def fetchval(self, *args, **kwargs):
            connection_calls.append(("fetchval", args, kwargs))
            raise AssertionError("exact-group rejection must precede DB access")

    @asynccontextmanager
    async def transaction():
        try:
            yield Connection()
        except BaseException:
            transaction_state["rolled_back"] = True
            raise
        else:
            transaction_state["committed"] = True

    async def bulk(*args, **kwargs):
        mutation_calls.append((args, kwargs))
        raise AssertionError("exact-group rejection must precede mutation")

    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )
    monkeypatch.setattr(
        catalog,
        "validate_enrichment_title_source_evidence",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        catalog,
        "validate_enrichment_source_evidence",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(catalog.SynonymWriteError, match=expected_error):
        async with transaction() as conn:
            await catalog._write_auto_enrichment_server_in_transaction(
                conn,
                "memphis",
                articles,
                items,
                generation_state,
                synonym_lock_already_held=True,
            )

    assert mutation_calls == []
    assert connection_calls == []
    assert transaction_state == {"committed": False, "rolled_back": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("member_body", "structural_titles"))
async def test_server_writer_rejects_catalog_detached_from_service_snapshot(
    monkeypatch,
    tamper,
):
    _articles, honest_items, fixture_state = (
        _server_writer_exact_group_fixture()
    )
    generation_state = await _service_generated_exact_group_state(
        fixture_state["catalog_rows"]
    )
    articles = [dict(row) for row in generation_state["articles"]]
    items = deepcopy(honest_items)

    if tamper == "member_body":
        generation_state["catalog_rows"][3]["full_text"] = (
            "Статья 2\n1. Совершенно другой текст."
        )
        items[1]["title"] += " иначе"
        items[1]["synonyms"][0] += " иначе"
        items[1]["penalty_summary"] = "Иная санкция"
        items[1]["penalty"] = {"fine": "100$"}
    else:
        generation_state["catalog_rows"][0]["title"] = (
            "Поддельная зона суда"
        )
        generation_state["catalog_rows"][2]["title"] = (
            "Поддельная зона прокуратуры"
        )
        forged_group = catalog.build_exact_duplicate_plan(
            generation_state["catalog_rows"],
            canonicalize_scope=catalog.canonical_synonym_term,
        ).groups[0]
        items = [
            {
                "id": member.article_id,
                "title": (
                    f"{member.title_scope_prefix}"
                    "Обязанность предъявить документы"
                ),
                "penalty_summary": None,
                "penalty": None,
                "synonyms": [
                    (
                        f"{member.synonym_scope_prefix}"
                        f"предъявить документы {index}"
                    )
                    for index in range(12)
                ],
            }
            for member in forged_group.members
        ]

    calls = {"lock": 0, "bulk": 0, "db": 0}

    async def acquire(*_args, **_kwargs):
        calls["lock"] += 1

    async def bulk(*_args, **_kwargs):
        calls["bulk"] += 1
        raise AssertionError("detached catalog must fail before mutation")

    class Connection:
        async def execute(self, *_args, **_kwargs):
            calls["db"] += 1
            raise AssertionError("detached catalog must fail before DB access")

        async def fetch(self, *_args, **_kwargs):
            calls["db"] += 1
            raise AssertionError("detached catalog must fail before DB access")

        async def fetchval(self, *_args, **_kwargs):
            calls["db"] += 1
            raise AssertionError("detached catalog must fail before DB access")

    monkeypatch.setattr(catalog, "acquire_synonym_write_lock", acquire)
    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )
    monkeypatch.setattr(
        catalog,
        "validate_enrichment_title_source_evidence",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        catalog,
        "validate_enrichment_source_evidence",
        lambda *args, **kwargs: None,
    )

    with pytest.raises(
        catalog.SynonymWriteError,
        match="catalog differs from its captured generation snapshot",
    ):
        await catalog._write_auto_enrichment_server_in_transaction(
            Connection(),
            "memphis",
            articles,
            items,
            generation_state,
            synonym_lock_already_held=False,
        )

    assert calls == {"lock": 0, "bulk": 0, "db": 0}


@pytest.mark.asyncio
async def test_server_writer_accepts_complete_mirrored_exact_group(
    monkeypatch,
):
    _articles, items, fixture_state = (
        _server_writer_exact_group_fixture()
    )
    generation_state = await _service_generated_exact_group_state(
        fixture_state["catalog_rows"]
    )
    articles = [dict(row) for row in generation_state["articles"]]
    calls = {"bulk": [], "fetch": 0, "updates": [], "jobs": []}

    class Connection:
        def transaction(self):
            return _noop_savepoint(self)

        async def fetch(self, _query, *_args):
            calls["fetch"] += 1
            return []

        async def fetchval(self, query, *args):
            assert query.lstrip().startswith("UPDATE law_articles")
            calls["updates"].append(int(args[0]))
            return int(args[0])

    async def bulk(_conn, server_id, replacement_items, **kwargs):
        calls["bulk"].append(
            (
                server_id,
                [int(item["id"]) for item in replacement_items],
                kwargs,
            )
        )
        return SimpleNamespace(
            result=SimpleNamespace(
                inserted=24,
                rejected_collisions=0,
            ),
        )

    async def ensure(_conn, article_id):
        calls["jobs"].append(article_id)
        return False

    monkeypatch.setattr(
        catalog,
        "replace_auto_synonyms_bulk_in_transaction",
        bulk,
    )
    monkeypatch.setattr(catalog, "ensure_article_embedding_target", ensure)
    monkeypatch.setattr(
        catalog,
        "validate_enrichment_title_source_evidence",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        catalog,
        "validate_enrichment_source_evidence",
        lambda *args, **kwargs: None,
    )

    result = await catalog._write_auto_enrichment_server_in_transaction(
        Connection(),
        "memphis",
        articles,
        items,
        generation_state,
        synonym_lock_already_held=True,
    )

    assert result == {
        "articles": 2,
        "synonyms": 24,
        "embedding_jobs": 0,
        "synonym_collisions_rejected": 0,
    }
    assert len(calls["bulk"]) == 1
    assert calls["bulk"][0][0:2] == ("memphis", [1, 2])
    assert calls["fetch"] == 1
    assert calls["updates"] == [1, 2]
    assert calls["jobs"] == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ("second_write", "rejected_signature"))
async def test_auto_enrichment_db_batch_is_atomic_and_requires_the_write_floor(
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
        assert kwargs["minimum_synonym_physical"] == (
            catalog._AUTO_ENRICH_MIN_PHYSICAL
        )
        assert kwargs["minimum_synonym_effective"] == (
            catalog._AUTO_ENRICH_MIN_EFFECTIVE
        )
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


# --- Поштучная приёмка синонимов --------------------------------------------
#
# Контракт доверия не меняется: всё, что фильтр возвращает, обязано проходить
# ТОТ ЖЕ неизменённый строгий гейт.  Ниже это и проверяется — каждый тест,
# который что-то «спасает», заканчивается вызовом самого гейта.

_PER_ITEM_BAD_SYNONYM_SUFFIX = "штраф"


def _per_item_batch(article_id=1, text="Действующая норма."):
    return [{
        "id": article_id,
        "full_text": f"Статья {article_id}\n1) {text}",
        "article_ref": f"Статья {article_id}",
        "doc_title": "Кодекс",
    }]


def test_per_item_acceptance_drops_only_the_unsupported_synonym():
    batch = _per_item_batch()
    item = _auto_enrichment_item(1, "first")
    item["synonyms"] = item["synonyms"] + [
        f"first distinct search formulation about {_PER_ITEM_BAD_SYNONYM_SUFFIX}"
    ]

    with pytest.raises(ValueError, match="source evidence failed"):
        catalog._validate_auto_enrichment_response([dict(item)], batch)

    kept, report = catalog._filter_auto_enrichment_items([item], batch)

    assert report["applied"] is True
    assert report["articles_dropped"] == 0
    assert report["synonyms_dropped"] == 1
    assert len(kept) == 1
    assert kept[0]["synonyms"] == item["synonyms"][:12]
    # Последнее слово — тот же строгий гейт, не ослабленный ни на байт.
    validated = catalog._validate_auto_enrichment_response(kept, batch)
    assert len(validated[0]["synonyms"]) == 12


def test_per_item_acceptance_drops_the_article_when_penalty_is_unsupported():
    batch = _per_item_batch()
    item = _auto_enrichment_item(1, "first")
    item["penalty_summary"] = "штраф $50,000"
    item["penalty"] = {"fine": "$50,000"}

    kept, report = catalog._filter_auto_enrichment_items([item], batch)

    # Синонимы честные, но выдуманное наказание не поштучная беда: индекса в
    # сообщении нет, значит статья честно пропускается ЦЕЛИКОМ.
    assert kept == []
    assert report["articles_dropped"] == 1
    assert report["dropped_articles"][0]["article_ids"] == [1]


# M-C: фильтр обязан зеркалить ДВА правила гейта, которых он не зеркалил.
# Оба теста ниже падают на дереве без зеркала: фильтр возвращает набор,
# который тот же неизменённый гейт отвергает, и весь батч гибнет ПОСЛЕ
# фильтра — ровно та беда, ради которой фильтр и делался.

_PER_ITEM_CEILING_SUFFIXES = _AUTO_TEST_SUFFIXES + ("протокол",)


def test_per_item_acceptance_mirrors_the_gate_ceiling():
    """Шестнадцатый годный синоним срезается, а не убивает батч."""

    batch = _per_item_batch()
    item = _auto_enrichment_item(1, "first")
    item["synonyms"] = [
        f"first distinct search formulation about {suffix}"
        for suffix in _PER_ITEM_CEILING_SUFFIXES
    ]
    assert len(item["synonyms"]) == catalog._AUTO_ENRICH_MAX_PHYSICAL + 1

    with pytest.raises(ValueError, match="distinct synonyms"):
        catalog._validate_auto_enrichment_response([dict(item)], batch)

    kept, report = catalog._filter_auto_enrichment_items([dict(item)], batch)

    assert report["applied"] is True
    assert report["articles_dropped"] == 0
    assert report["synonyms_dropped"] == 1
    assert len(kept[0]["synonyms"]) == catalog._AUTO_ENRICH_MAX_PHYSICAL
    # Последнее слово — тот же строгий гейт, не ослабленный ни на байт.
    validated = catalog._validate_auto_enrichment_response(kept, batch)
    assert len(validated[0]["synonyms"]) == (
        catalog._AUTO_ENRICH_MAX_PHYSICAL
    )


@pytest.mark.parametrize(
    "bad_penalty",
    [
        {"fine": "$40,000", "bail": None},
        "штраф $40,000",
        {"fine": "$40,000 " + "и" * 500},
    ],
    ids=["unknown-field", "not-an-object", "too-long"],
)
def test_per_item_acceptance_mirrors_the_gate_penalty_structure(bad_penalty):
    """Структурно негодное наказание роняет статью, а не весь батч.

    Наказание здесь ПОДТВЕРЖДЕНО источником («штраф $40,000» есть в тексте),
    поэтому строгий валидатор к содержанию не придирается и беда остаётся
    ровно структурной — той, которую судит только гейт.  Без зеркала фильтр
    возвращал такую статью как годную (замер: kept=[1, 2]), гейт отвергал её
    следом и уносил ВЕСЬ батч, включая честную соседнюю статью.
    """

    batch = (
        _per_item_batch(1)
        + _per_item_batch(2, "Нарушение. Наказание: штраф $40,000.")
    )
    good = _auto_enrichment_item(1, "first")
    broken = _auto_enrichment_item(2, "second")
    broken["penalty_summary"] = "штраф $40,000"
    broken["penalty"] = bad_penalty

    with pytest.raises(ValueError):
        catalog._validate_auto_enrichment_response(
            [dict(good), dict(broken)], batch,
        )

    kept, report = catalog._filter_auto_enrichment_items(
        [dict(good), dict(broken)], batch,
    )

    assert [item["id"] for item in kept] == [1]
    assert report["articles_dropped"] == 1
    assert report["dropped_articles"][0]["article_ids"] == [2]
    assert "penalty structure rejected" in (
        report["dropped_articles"][0]["reason"]
    )
    # Уцелевшая статья проходит тот же неизменённый гейт.
    validated = catalog._validate_auto_enrichment_response(kept, [batch[0]])
    assert [item["id"] for item in validated] == [1]


def test_per_item_acceptance_drops_the_article_below_the_configured_floor():
    batch = _per_item_batch()
    item = _auto_enrichment_item(1, "first", synonyms_count=12)
    item["synonyms"][3] = (
        f"first distinct search formulation about {_PER_ITEM_BAD_SYNONYM_SUFFIX}"
    )

    kept, report = catalog._filter_auto_enrichment_items(
        [item], batch, minimum_physical=12, minimum_effective=12,
    )
    assert kept == []
    assert report["articles_dropped"] == 1
    assert "below floor" in report["dropped_articles"][0]["reason"]


def test_configured_floor_moves_filter_and_gate_together(monkeypatch):
    """Порог — явная настройка; фильтр и гейт обязаны двигаться вместе."""

    batch = _per_item_batch()
    item = _auto_enrichment_item(1, "first", synonyms_count=12)
    item["synonyms"][3] = (
        f"first distinct search formulation about {_PER_ITEM_BAD_SYNONYM_SUFFIX}"
    )
    monkeypatch.setattr(catalog, "_AUTO_ENRICH_MIN_PHYSICAL", 11)
    monkeypatch.setattr(catalog, "_AUTO_ENRICH_MIN_EFFECTIVE", 11)

    kept, report = catalog._filter_auto_enrichment_items([item], batch)

    assert report["articles_dropped"] == 0
    assert len(kept[0]["synonyms"]) == 11
    # И тот же неизменённый гейт теперь принимает ровно этот набор.
    catalog._validate_auto_enrichment_response(kept, batch)


def test_write_floor_and_quality_target_are_two_separate_roles(
    monkeypatch,
):
    """Одно число больше не делает две работы.

    ПОЛ ЗАПИСИ — 5: ниже него писать нельзя.  ПЛАНКА КАЧЕСТВА — 12: нехватка
    до неё не запрещает запись, а поднимает пометку на переобогащение.
    Прежнее значение 12 осталось ровно там, где оно было сигналом качества,
    и ушло оттуда, где было запретом.
    """

    assert synonym_service.BULK_REPLACEMENT_PHYSICAL == 5
    assert synonym_service.BULK_REPLACEMENT_EFFECTIVE == 5
    assert catalog._AUTO_ENRICH_MIN_PHYSICAL == 5
    assert catalog._AUTO_ENRICH_MIN_EFFECTIVE == 5
    # Планка качества — прежние 12, но уже как сигнал, а не как гейт.
    assert synonym_service.ENRICHMENT_QUALITY_TARGET == 12
    assert catalog._AUTO_ENRICH_QUALITY_TARGET == 12
    assert catalog._AUTO_ENRICH_QUALITY_TARGET > (
        catalog._AUTO_ENRICH_MIN_PHYSICAL
    )
    # Аудит завершённости не выше пола записи, иначе записанная статья тут же
    # объявляется неполной и сервер уходит в вечный цикл pending.
    assert synonym_service.MIN_REPLACEMENT_PHYSICAL <= (
        synonym_service.BULK_REPLACEMENT_PHYSICAL
    )

    # M-B: ручки у пола нет.  Прежняя ``LAW_ENRICH_MIN_SYNONYMS`` читалась из
    # окружения мимо ``config.py`` и зажималась в [2, 15], то есть позволяла
    # опустить и порог записи, и счётное правило строгого гейта НИЖЕ решения
    # заказчика.  Проверяем оба факта: имён больше нет, и модуль, загруженный
    # заново под враждебным окружением, всё равно получает пятёрку.
    assert not [
        name
        for name in dir(synonym_service)
        if "FLOOR_ENV" in name or name == "configured_enrichment_floor"
    ]
    assert "LAW_ENRICH_MIN_SYNONYMS" not in inspect.getsource(synonym_service)

    for raw in ("2", "3", "8", "12", "15", "99", "0", "", "bad"):
        monkeypatch.setenv("LAW_ENRICH_MIN_SYNONYMS", raw)
        # Свежий модуль под ОТДЕЛЬНЫМ именем: настоящий
        # ``services.law_synonym_service`` в ``sys.modules`` не трогаем, иначе
        # соседние тесты получили бы другой класс исключения.
        name = "_floor_probe_law_synonym_service"
        spec = importlib.util.spec_from_file_location(
            name, synonym_service.__file__,
        )
        probe = importlib.util.module_from_spec(spec)
        sys.modules[name] = probe
        try:
            spec.loader.exec_module(probe)
        finally:
            sys.modules.pop(name, None)
        assert probe.BULK_REPLACEMENT_PHYSICAL == 5, raw
        assert probe.BULK_REPLACEMENT_EFFECTIVE == 5, raw
        assert probe.MIN_REPLACEMENT_PHYSICAL == 5, raw
        assert probe.MIN_REPLACEMENT_EFFECTIVE == 5, raw


def test_per_item_acceptance_drops_duplicate_canonical_forms_not_the_article():
    batch = _per_item_batch()
    item = _auto_enrichment_item(1, "first", synonyms_count=13)
    item["synonyms"][12] = item["synonyms"][0].upper()

    kept, report = catalog._filter_auto_enrichment_items(
        [item], batch, minimum_physical=12, minimum_effective=12,
    )

    assert report["articles_dropped"] == 0
    assert report["synonyms_dropped"] == 1
    assert kept[0]["synonyms"] == item["synonyms"][:12]
    catalog._validate_auto_enrichment_response(kept, batch)


def test_per_item_acceptance_keeps_synonyms_a_solo_probe_would_reject():
    """Жадное удаление обязано быть строго лучше наивной одиночной пробы.

    Кросс-синонимные правила покрытия требуют покрытия от НАБОРА, поэтому
    одиночка вынуждена нести всё сама и честные синонимы поодиночке падают.
    """

    source = (
        "Пропуск может быть аннулирован судом как дополнительная мера "
        "наказания, когда преступление совершено с использованием такого "
        "пропуска."
    )
    solo_rejected = "аннулирование пропуска судом"
    solo_rejected_too = (
        "аннулирование пропуска как дополнительная мера наказания"
    )
    carrier = (
        "аннулирование пропуска судом как дополнительная мера наказания"
    )
    # Опорный факт: поодиночке оба отвергаются, в наборе с третьим — нет.
    for lonely in (solo_rejected, solo_rejected_too):
        with pytest.raises(ValueError):
            validate_synonym_source_evidence(source, [lonely])
    validate_synonym_source_evidence(
        source, [solo_rejected, solo_rejected_too, carrier],
    )

    batch = [{
        "id": 1,
        "full_text": source,
        "article_ref": "Статья 1",
        "doc_title": "Кодекс",
    }]
    item = {
        "id": 1,
        "title": None,
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [solo_rejected, solo_rejected_too, carrier],
    }

    kept, report = catalog._filter_auto_enrichment_items(
        [item], batch, minimum_physical=3, minimum_effective=3,
    )

    assert report["synonyms_dropped"] == 0
    assert kept[0]["synonyms"] == [
        solo_rejected,
        solo_rejected_too,
        carrier,
    ]


def test_per_item_acceptance_mirrors_the_verdict_across_an_exact_group():
    body = "Одинаковая действующая норма без санкции."
    rows = [
        {
            "id": 10, "law_document_id": 1, "source_order": 1,
            "article_ref": "Глава I", "kind": "chapter", "code": "I",
            "title": "Первая область", "doc_title": "Закон",
            "full_text": "Глава I\nПервая область",
            "article_hash": "chapter-1",
        },
        {
            "id": 11, "law_document_id": 1, "source_order": 2,
            "article_ref": "Статья 1", "kind": "article", "code": "1",
            "title": None, "doc_title": "Закон",
            "full_text": f"Статья 1\n{body}", "article_hash": "article-1",
        },
        {
            "id": 20, "law_document_id": 1, "source_order": 3,
            "article_ref": "Глава II", "kind": "chapter", "code": "II",
            "title": "Вторая область", "doc_title": "Закон",
            "full_text": "Глава II\nВторая область",
            "article_hash": "chapter-2",
        },
        {
            "id": 21, "law_document_id": 1, "source_order": 4,
            "article_ref": "Статья 2", "kind": "article", "code": "2",
            "title": None, "doc_title": "Закон",
            "full_text": f"Статья 2\n{body}", "article_hash": "article-2",
        },
    ]
    plan = catalog.build_exact_duplicate_plan(
        rows,
        canonicalize_scope=catalog.canonical_synonym_term,
    )
    articles = [row for row in rows if row["kind"] == "article"]
    group = plan.groups[0]
    items = []
    for position, member in enumerate(group.members):
        synonyms = [
            f"{member.synonym_scope_prefix}точная формулировка про {suffix}"
            for suffix in _AUTO_TEST_SUFFIXES[:13]
        ]
        if position == 0:
            # Негодный синоним есть ровно у ОДНОГО члена группы.
            synonyms[12] = (
                f"{member.synonym_scope_prefix}точная формулировка про "
                f"{_PER_ITEM_BAD_SYNONYM_SUFFIX}"
            )
        items.append({
            "id": member.article_id,
            "title": catalog.source_catalog_title(
                f"Статья {member.article_id - 10}",
                f"Статья {member.article_id - 10}\n{body}",
            ),
            "penalty_summary": None,
            "penalty": None,
            "synonyms": synonyms,
        })

    kept, report = catalog._filter_auto_enrichment_items(
        items, articles, exact_plan=plan,
        minimum_physical=12, minimum_effective=12,
    )

    # Решение принято на группу и применено ПО ИНДЕКСУ обоим членам, иначе
    # последовательности суффиксов разъедутся и группа упадёт на асимметрии.
    assert report["articles_dropped"] == 0
    assert report["synonyms_dropped"] == 2
    assert [len(item["synonyms"]) for item in kept] == [12, 12]
    catalog._validate_auto_enrichment_response(
        kept, articles, exact_plan=plan,
    )


def test_per_item_acceptance_never_splits_an_exact_group():
    """Член группы не может уцелеть без брата: либо вся группа, либо ничего."""

    body = "Одинаковая действующая норма без санкции."
    rows = [
        {
            "id": 10, "law_document_id": 1, "source_order": 1,
            "article_ref": "Глава I", "kind": "chapter", "code": "I",
            "title": "Первая область", "doc_title": "Закон",
            "full_text": "Глава I\nПервая область",
            "article_hash": "chapter-1",
        },
        {
            "id": 11, "law_document_id": 1, "source_order": 2,
            "article_ref": "Статья 1", "kind": "article", "code": "1",
            "title": None, "doc_title": "Закон",
            "full_text": f"Статья 1\n{body}", "article_hash": "article-1",
        },
        {
            "id": 20, "law_document_id": 1, "source_order": 3,
            "article_ref": "Глава II", "kind": "chapter", "code": "II",
            "title": "Вторая область", "doc_title": "Закон",
            "full_text": "Глава II\nВторая область",
            "article_hash": "chapter-2",
        },
        {
            "id": 21, "law_document_id": 1, "source_order": 4,
            "article_ref": "Статья 2", "kind": "article", "code": "2",
            "title": None, "doc_title": "Закон",
            "full_text": f"Статья 2\n{body}", "article_hash": "article-2",
        },
    ]
    plan = catalog.build_exact_duplicate_plan(
        rows,
        canonicalize_scope=catalog.canonical_synonym_term,
    )
    articles = [row for row in rows if row["kind"] == "article"]
    group = plan.groups[0]
    items = []
    for position, member in enumerate(group.members):
        synonyms = [
            f"{member.synonym_scope_prefix}точная формулировка про {suffix}"
            for suffix in _AUTO_TEST_SUFFIXES[:12]
        ]
        if position == 0:
            synonyms[0] = (
                f"{member.synonym_scope_prefix}точная формулировка про "
                f"{_PER_ITEM_BAD_SYNONYM_SUFFIX}"
            )
        items.append({
            "id": member.article_id,
            "title": catalog.source_catalog_title(
                f"Статья {member.article_id - 10}",
                f"Статья {member.article_id - 10}\n{body}",
            ),
            "penalty_summary": None,
            "penalty": None,
            "synonyms": synonyms,
        })

    kept, report = catalog._filter_auto_enrichment_items(
        items, articles, exact_plan=plan,
        minimum_physical=12, minimum_effective=12,
    )

    assert kept == []
    assert report["articles_dropped"] == 2
    assert report["dropped_articles"][0]["article_ids"] == [11, 21]


def test_per_item_acceptance_leaves_structural_faults_to_the_strict_gate():
    batch = _per_item_batch()
    for broken in (
        "not a list",
        [{"id": 999, "title": None, "penalty_summary": None,
          "penalty": None, "synonyms": []}],
        [_auto_enrichment_item(1, "first"), _auto_enrichment_item(1, "again")],
    ):
        kept, report = catalog._filter_auto_enrichment_items(broken, batch)
        assert report["applied"] is False
        assert kept is broken


def test_salvage_runs_the_unchanged_gate_first_and_reraises_when_nothing_left():
    batch = _per_item_batch()
    good = _auto_enrichment_item(1, "first")

    items, kept_batch, report = catalog._salvage_auto_enrichment_response(
        [dict(good)], batch, server_id="memphis", stage="generation",
    )
    assert report == {"applied": False}
    assert kept_batch == batch
    assert len(items[0]["synonyms"]) == 12

    doomed = _auto_enrichment_item(1, "first")
    doomed["penalty_summary"] = "штраф $50,000"
    doomed["penalty"] = {"fine": "$50,000"}
    # Ни одной годной статьи — ведём себя ровно как раньше, а не «успешно
    # ничего не записали».
    with pytest.raises(ValueError, match="source evidence failed"):
        catalog._salvage_auto_enrichment_response(
            [doomed], batch, server_id="memphis", stage="generation",
        )


def test_salvage_keeps_the_good_article_when_a_neighbour_is_unsalvageable():
    batch = _per_item_batch(1) + _per_item_batch(2)
    good = _auto_enrichment_item(1, "first")
    doomed = _auto_enrichment_item(2, "second")
    doomed["penalty_summary"] = "штраф $50,000"
    doomed["penalty"] = {"fine": "$50,000"}
    stats = {"articles_dropped": 0, "synonyms_dropped": 0}

    items, kept_batch, report = catalog._salvage_auto_enrichment_response(
        [good, doomed],
        batch,
        server_id="memphis",
        stage="generation",
        stats=stats,
    )

    assert [item["id"] for item in items] == [1]
    assert [article["id"] for article in kept_batch] == [1]
    assert report["articles_dropped"] == 1
    assert stats == {"articles_dropped": 1, "synonyms_dropped": 0}


@pytest.mark.asyncio
async def test_server_enrichment_skips_one_bad_article_and_writes_the_rest(
    monkeypatch,
):
    """Один негодный синоним больше не роняет прогон целого сервера."""

    import rate_limiter

    articles = [
        {"id": 1, "code": "1", "title": "One",
         "full_text": "First text", "article_hash": "hash-1"},
        {"id": 2, "code": "2", "title": "Two",
         "full_text": "Second text", "article_hash": "hash-2"},
    ]
    first = _auto_enrichment_item(1, "first")
    second = _auto_enrichment_item(2, "second")
    second["penalty_summary"] = "штраф $50,000"
    second["penalty"] = {"fine": "$50,000"}

    async def capture(_server_id, *, only_stale):
        return _auto_generation_state_for_tests(articles)

    recaptures = []

    async def capture_targets(server_id, target_ids):
        recaptures.append((server_id, list(target_ids)))
        return _auto_generation_state_for_tests(
            [row for row in articles if int(row["id"]) in set(target_ids)]
        )

    calls = []

    async def execute_vertex_request(**_kwargs):
        # Генерация видит обе статьи; ревью получает уже уцелевший набор,
        # потому что поштучная приёмка пересобрала промпт под него.
        calls.append(True)
        asked = (
            [first, second]
            if len(calls) <= catalog._AUTO_ENRICH_QUALITY_ATTEMPTS
            else [first]
        )

        class Response:
            text = json.dumps(asked)

        return Response()

    async def preview(_server_id, items, *, generation_snapshot):
        return _successful_auto_preview(
            [{"id": item["id"]} for item in items]
        )

    writes = []

    async def write_server(server_id, batch, items, generation_state):
        writes.append([int(item["id"]) for item in items])
        assert generation_state["snapshot"]["required_target_ids"] == [1]
        return {
            "articles": len(items), "synonyms": 12,
            "embedding_jobs": 1, "synonym_collisions_rejected": 0,
        }

    monkeypatch.setattr(
        catalog, "capture_auto_replacement_generation_state", capture,
    )
    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state_for_targets",
        capture_targets,
    )
    monkeypatch.setattr(
        rate_limiter, "execute_vertex_request", execute_vertex_request,
    )
    monkeypatch.setattr(catalog, "preview_auto_synonym_replacements", preview)
    monkeypatch.setattr(catalog, "_write_auto_enrichment_server", write_server)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 2)
    monkeypatch.setattr(
        catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0,
    )

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats["batches_failed"] == 0
    assert stats["articles"] == 1
    assert stats["articles_dropped"] >= 1
    assert writes == [[1]]
    assert recaptures == [("memphis", [1])]


@pytest.mark.asyncio
async def test_dropped_articles_keep_batches_failed_zero_and_still_fail_closed(
    monkeypatch,
):
    """M-D: чем именно держится fail-closed, когда статья отброшена.

    Поштучная приёмка НЕ поднимает ``batches_failed``: батч не падал, он
    честно записал уцелевшее.  Значит маркер повтора по счётчику батчей здесь
    не срабатывает, и всё, что стоит между «часть статей потеряна» и «сервер
    объявлен обогащённым», — это АУДИТ ЗАВЕРШЁННОСТИ.  Тест фиксирует обе
    половины: счётчик нулевой И аудит всё равно роняет прогон, оставляя
    pending.  Раньше первую половину проверял один тест, вторую — ни один.
    """

    from services import law_enrichment_completion

    articles = [
        {"id": 1, "code": "1", "title": "One",
         "full_text": "First text", "article_hash": "hash-1"},
        {"id": 2, "code": "2", "title": "Two",
         "full_text": "Second text", "article_hash": "hash-2"},
    ]
    first = _auto_enrichment_item(1, "first")
    second = _auto_enrichment_item(2, "second")
    second["penalty_summary"] = "штраф $50,000"
    second["penalty"] = {"fine": "$50,000"}

    import rate_limiter

    async def capture(_server_id, *, only_stale):
        return _auto_generation_state_for_tests(articles)

    async def capture_targets(_server_id, target_ids):
        return _auto_generation_state_for_tests(
            [row for row in articles if int(row["id"]) in set(target_ids)]
        )

    calls = []

    async def execute_vertex_request(**_kwargs):
        calls.append(True)
        asked = (
            [first, second]
            if len(calls) <= catalog._AUTO_ENRICH_QUALITY_ATTEMPTS
            else [first]
        )

        class Response:
            text = json.dumps(asked)

        return Response()

    async def preview(_server_id, items, *, generation_snapshot):
        return _successful_auto_preview(
            [{"id": item["id"]} for item in items]
        )

    async def write_server(_server_id, _batch, items, _generation_state):
        return {
            "articles": len(items), "synonyms": 12,
            "embedding_jobs": 1, "synonym_collisions_rejected": 0,
        }

    monkeypatch.setattr(
        catalog, "capture_auto_replacement_generation_state", capture,
    )
    monkeypatch.setattr(
        catalog,
        "capture_auto_replacement_generation_state_for_targets",
        capture_targets,
    )
    monkeypatch.setattr(
        rate_limiter, "execute_vertex_request", execute_vertex_request,
    )
    monkeypatch.setattr(catalog, "preview_auto_synonym_replacements", preview)
    monkeypatch.setattr(catalog, "_write_auto_enrichment_server", write_server)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 2)
    monkeypatch.setattr(
        catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0,
    )

    stats = await catalog.enrich_articles_for_server("memphis")

    # Половина первая: маркер повтора по батчам МОЛЧИТ, хотя статья потеряна.
    assert stats["batches_failed"] == 0
    assert stats["articles_dropped"] == 1
    assert stats["articles"] == 1

    # Половина вторая: fail-closed держит аудит завершённости — статья 2 не
    # обогащена, сервер не объявляется готовым, pending остаётся.
    run_db = _EnrichmentRunDB(
        [{
            "id": 1,
            "server_id": "memphis",
            "title": "УК",
            "content": "",
            "content_hash": "doc",
            "last_updated": "v1",
            "derived_sync_pending": False,
        }],
        [],
    )

    async def evaluate(_conn, _server_id):
        return SimpleNamespace(
            articles=2,
            complete=False,
            incomplete_article_ids=(2,),
            incomplete_document_ids=(1,),
        )

    monkeypatch.setattr(
        law_enrichment_completion,
        "evaluate_law_enrichment_completion",
        evaluate,
    )
    monkeypatch.setattr(
        catalog.db_manager, "transaction", run_db.transaction,
    )

    with pytest.raises(
        catalog.SynonymWriteError,
        match=r"incomplete article ids \[2\]",
    ) as failure:
        await catalog.require_successful_enrichment_completion(
            "memphis",
            stats,
        )

    # Именно аудит, а не счётчик батчей: путь «failed batches» не сработал.
    assert "failed batches" not in str(failure.value)
    assert run_db.documents[1]["derived_sync_pending"] is True


# --- Кэш вердиктов (M3): вердикт обязан остаться вердиктом валидатора ------

def test_verdict_cache_repeats_the_validator_byte_for_byte():
    """Кэш не смягчает и не ужесточает — он повторяет тот же ответ.

    Проверяется на обоих исходах: и на «годен», и на отказе, причём текст
    отказа сравнивается ПОБАЙТНО — сообщение несёт индекс синонима, по
    которому работает поштучная приёмка.
    """

    source = "Статья 1\n1) Действующая норма без санкции."
    good = [
        f"first distinct search formulation about {suffix}"
        for suffix in _AUTO_TEST_SUFFIXES[:12]
    ]
    bad = good[:-1] + [
        f"first distinct search formulation about {_PER_ITEM_BAD_SYNONYM_SUFFIX}"
    ]

    for synonyms in (good, bad):
        try:
            validate_enrichment_source_evidence(source, None, None, synonyms)
            raw = None
        except ValueError as exc:
            raw = str(exc)
        for _repeat in range(3):
            try:
                catalog._cached_validate_enrichment_source_evidence(
                    source, None, None, synonyms,
                )
                cached = None
            except ValueError as exc:
                cached = str(exc)
            assert cached == raw

    # И тот же договор для заголовка.
    for title in ("Действующая норма", "Пожизненное лишение свободы"):
        try:
            validate_enrichment_title_source_evidence(
                source, title, source_title=None, source_context=None,
            )
            raw = None
        except ValueError as exc:
            raw = str(exc)
        for _repeat in range(3):
            try:
                catalog._cached_validate_enrichment_title_source_evidence(
                    source, title, source_title=None, source_context=None,
                )
                cached = None
            except ValueError as exc:
                cached = str(exc)
            assert cached == raw


def test_verdict_cache_never_confuses_two_different_inputs():
    """Ключ покрывает КАЖДЫЙ аргумент — иначе кэш стал бы дырой в контракте."""

    source_a = "Статья 1\n1) Действующая норма без санкции."
    source_b = "Статья 2\n1) Наказание: штраф от $100 до $200."
    synonyms = [
        f"first distinct search formulation about {suffix}"
        for suffix in _AUTO_TEST_SUFFIXES[:12]
    ]
    keys = {
        catalog._auto_enrichment_verdict_key(
            "source", source_a, None, None, synonyms, None),
        catalog._auto_enrichment_verdict_key(
            "source", source_b, None, None, synonyms, None),
        catalog._auto_enrichment_verdict_key(
            "source", source_a, "штраф $100", None, synonyms, None),
        catalog._auto_enrichment_verdict_key(
            "source", source_a, None, {"fine": "$100"}, synonyms, None),
        catalog._auto_enrichment_verdict_key(
            "source", source_a, None, None, synonyms[:-1], None),
        catalog._auto_enrichment_verdict_key(
            "source", source_a, None, None, synonyms, "[область] "),
        catalog._auto_enrichment_verdict_key(
            "title", source_a, None, None, synonyms, None),
    }
    assert len(keys) == 7


def test_write_boundary_never_splits_an_exact_duplicate_group():
    """Граница записи не режет группу байт-идентичных тел.

    Частичная запись сервера стала возможной, и это единственное новое место,
    где группа могла бы разъехаться.  Не может, и вот почему — обе половины
    доказательства проверены здесь:

    1) разбиение на батчи держит КАЖДУЮ группу целиком внутри одного батча;
    2) принятый набор набирается ПОБАТЧЕВО (провал батча даёт ``continue``,
       успех добавляет весь ``active_batch``), а поштучная приёмка судит
       группу как единое целое.

    Значит любой принятый набор — объединение целых батчей, а каждый батч —
    объединение целых групп.
    """

    body = "Одинаковая действующая норма без санкции."
    rows = []
    for chapter in range(1, 5):
        rows.append({
            "id": chapter * 100, "law_document_id": 1,
            "source_order": chapter * 10,
            "article_ref": f"Глава {chapter}", "kind": "chapter",
            "code": str(chapter), "title": f"Область {chapter}",
            "doc_title": "Закон",
            "full_text": f"Глава {chapter}\nОбласть {chapter}",
            "article_hash": f"chapter-{chapter}",
        })
        rows.append({
            "id": chapter * 100 + 1, "law_document_id": 1,
            "source_order": chapter * 10 + 1,
            "article_ref": f"Статья {chapter}", "kind": "article",
            "code": str(chapter), "title": None, "doc_title": "Закон",
            "full_text": f"Статья {chapter}\n{body}",
            "article_hash": f"twin-{chapter}",
        })
    for solo in range(5, 12):
        rows.append({
            "id": solo * 100 + 1, "law_document_id": 1,
            "source_order": solo * 10 + 1,
            "article_ref": f"Статья {solo}", "kind": "article",
            "code": str(solo), "title": None, "doc_title": "Закон",
            "full_text": f"Статья {solo}\nСовершенно отдельная норма {solo}.",
            "article_hash": f"solo-{solo}",
        })

    plan = catalog.build_exact_duplicate_plan(
        rows,
        is_substantive_article=lambda row: not (
            catalog.is_explicitly_empty_or_repealed_article(
                row.get("full_text"))),
        canonicalize_scope=catalog.canonical_synonym_term,
    )
    assert plan.groups, "тест бессмыслен без хотя бы одной группы"
    articles = [row for row in rows if row["kind"] == "article"]

    for batch_size in (1, 2, 3, 5, 8):
        batches = catalog._partition_auto_enrichment_batches(
            articles,
            batch_size,
            exact_plan=plan,
            all_articles=rows,
        )
        assert sorted(
            int(article["id"]) for batch in batches for article in batch
        ) == sorted(int(article["id"]) for article in articles)
        batch_of = {}
        for index, batch in enumerate(batches):
            for article in batch:
                batch_of[int(article["id"])] = index
        for group in plan.groups:
            homes = {batch_of[int(aid)] for aid in group.article_ids}
            assert len(homes) == 1, (
                f"группа {list(group.article_ids)} разрезана границей батча, "
                f"а значит и границей записи (batch_size={batch_size})"
            )

        # Любой принятый набор — объединение ЦЕЛЫХ батчей, потому что провал
        # даёт ``continue`` (весь батч пропущен), а успех добавляет весь
        # ``active_batch``.  Проверяем на всех вариантах «какие батчи упали».
        for failed in range(1 << len(batches)):
            accepted = {
                int(article["id"])
                for index, batch in enumerate(batches)
                if not failed >> index & 1
                for article in batch
            }
            for group in plan.groups:
                members = {int(aid) for aid in group.article_ids}
                assert members <= accepted or not (members & accepted)


@pytest.mark.asyncio
async def test_server_enrichment_still_fails_when_no_article_survives(
    monkeypatch,
):
    import rate_limiter

    articles = [
        {"id": 1, "code": "1", "title": "One",
         "full_text": "First text", "article_hash": "hash-1"},
    ]
    doomed = _auto_enrichment_item(1, "first")
    doomed["penalty_summary"] = "штраф $50,000"
    doomed["penalty"] = {"fine": "$50,000"}

    class Response:
        text = json.dumps([doomed])

    async def capture(_server_id, *, only_stale):
        return _auto_generation_state_for_tests(articles)

    async def execute_vertex_request(**_kwargs):
        return Response()

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("nothing may be written")

    monkeypatch.setattr(
        catalog, "capture_auto_replacement_generation_state", capture,
    )
    monkeypatch.setattr(
        rate_limiter, "execute_vertex_request", execute_vertex_request,
    )
    monkeypatch.setattr(catalog, "_write_auto_enrichment_server", forbidden)
    monkeypatch.setattr(catalog, "preview_auto_synonym_replacements", forbidden)
    monkeypatch.setattr(
        catalog,
        "_desired_auto_enrichment_title",
        lambda _article, reviewed_title: reviewed_title,
    )
    monkeypatch.setattr(catalog.config, "LAW_ENRICH_BATCH_ARTICLES", 2)
    monkeypatch.setattr(
        catalog.config, "LAW_ENRICH_INTER_BATCH_DELAY_SECONDS", 0,
    )

    stats = await catalog.enrich_articles_for_server("memphis")

    assert stats["batches_failed"] == 1
    assert stats["articles"] == 0

# END OF FILE tests/test_law_catalog.py
