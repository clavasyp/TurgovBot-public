from __future__ import annotations

from copy import deepcopy

import pytest

import scripts.backfill_law_servers as phase_a
from services.indexing_service import _header_match_plan
from services.law_catalog_service import _catalog_rows


ARTICLE_BASELINE = """Статья 1. Общие положения
Текст первой статьи.

Статья 2. Запрет
Текст второй статьи.

Статья 3. Ответственность
Текст третьей статьи.
"""

SUBSTANTIVE_BODY = (
    "Настоящая глава устанавливает общий порядок исполнения закона, "
    "права участников, обязанности государственных органов и гарантии "
    "соблюдения установленной процедуры на всей территории штата. "
)


def _doc(doc_id: int, content: str) -> dict[str, object]:
    return {
        "id": doc_id,
        "server_id": "test_server",
        "source_thread_id": 10_000 + doc_id,
        "title": f"Документ {doc_id}",
        "content": content,
        "content_hash": f"hash-{doc_id}",
        "derived_sync_pending": False,
    }


def _analyse(content: str):
    return phase_a._analyse_headers(
        [_doc(1, ARTICLE_BASELINE), _doc(2, content)],
        max_unstructured_chars=12_000,
    )


def _chapter(code: str, *, body: str = SUBSTANTIVE_BODY) -> str:
    return f"Глава {code}. Основные положения\n1. {body}\n"


def _assert_manual_chapter_audit(content: str) -> None:
    stats, _blockers, warnings = _analyse(content)

    assert stats["documents_using_chapter_primary"] == 0
    assert stats["documents_without_articles"] == 1
    assert any(
        "doc=2: parser produced no kind='article'" in warning
        for warning in warnings
    )


def test_chapter_primary_accepts_audited_split_roman_layout():
    content = (
        "Закон о государственных наградах.\n"
        + _chapter("I")
        + "Г\nлава II. Представление к государственным наградам\n"
        + "2. "
        + SUBSTANTIVE_BODY
    )

    stats, blockers, warnings = _analyse(content)

    assert blockers == []
    assert warnings == []
    assert stats["documents_using_chapter_primary"] == 1
    assert stats["documents_using_section_primary"] == 0
    assert stats["documents_without_articles"] == 1
    assert stats["expected_articles"] == 3
    assert [
        (row["kind"], row.get("code"))
        for row in _catalog_rows(content)
    ] == [
        ("other", None),
        ("chapter", "I"),
        ("chapter", "II"),
    ]


def test_chapter_primary_accepts_audited_numeric_layout_without_preamble():
    content = "\n".join(
        _chapter(str(code), body=SUBSTANTIVE_BODY).strip()
        for code in range(1, 6)
    )

    stats, blockers, warnings = _analyse(content)

    assert blockers == []
    assert warnings == []
    assert stats["documents_using_chapter_primary"] == 1
    assert stats["documents_using_section_primary"] == 0
    assert stats["documents_without_articles"] == 1


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            _chapter("I"),
            id="one-chapter",
        ),
        pytest.param(
            _chapter("I") + "Глава II. Пустая глава\nКоротко.\n",
            id="thin-chapter-body",
        ),
        pytest.param(
            _chapter("II") + _chapter("I"),
            id="descending-codes",
        ),
        pytest.param(
            _chapter("I") + _chapter("I"),
            id="duplicate-codes",
        ),
        pytest.param(
            _chapter("I") + _chapter("2"),
            id="mixed-code-families",
        ),
        pytest.param(
            _chapter("I")
            + _chapter("II", body=("1234567890 " * 20)),
            id="long-non-prose-body",
        ),
        pytest.param(
            _chapter("I")
            + "Пункт 1. Отдельная структурная единица\n"
            + SUBSTANTIVE_BODY
            + _chapter("II"),
            id="foreign-clause-kind",
        ),
    ],
)
def test_chapter_primary_rejects_ambiguous_structural_shapes(content):
    _assert_manual_chapter_audit(content)


def test_chapter_primary_rejects_uncoded_catalog_chapter():
    content = _chapter("I") + _chapter("II")
    plan = _header_match_plan(content)
    rows = deepcopy(_catalog_rows(content))
    rows[0]["code"] = None

    assert not phase_a._is_chapter_primary_document(
        content,
        rows,
        plan,
        max_unstructured_chars=12_000,
    )


def test_chapter_primary_rejects_large_unknown_preamble_even_below_global_limit():
    content = (
        ("Неизвестная неструктурированная преамбула без правовых норм. " * 45)
        + "\n"
        + _chapter("I")
        + _chapter("II")
    )
    assert 2_048 < len(content) < 12_000

    _assert_manual_chapter_audit(content)


def test_chapter_primary_does_not_count_word_joiners_as_visible_body():
    formatting_padded_body = ("а" * 60) + ("\u2060" * 100)
    assert len(formatting_padded_body) >= 120
    assert sum(character.isalpha() for character in formatting_padded_body) == 60
    assert len(phase_a._visible_source_text(formatting_padded_body)) == 60
    content = _chapter("I") + _chapter("II", body=formatting_padded_body)

    _assert_manual_chapter_audit(content)


def test_chapter_primary_rejects_bare_candidates_when_fallback_is_closed():
    content = (
        "Примечание:\n"
        "Статья 1.2 настоящего закона применяется только к исключениям.\n"
        + _chapter("1")
        + "1.1\nПервый похожий пункт.\n"
        + "1.2\nВторой похожий пункт.\n"
        + "1.3\nТретий похожий пункт.\n"
        + _chapter("2")
    )
    plan = _header_match_plan(content)
    assert len(plan.legacy_bare_candidates) == 3
    assert plan.fallback_mode is None

    _assert_manual_chapter_audit(content)


def test_chapter_primary_rejects_rainbow_like_section_and_bare_shape():
    content = (
        "Примечание:\n"
        "Статья 1.2 настоящего кодекса применяется только к исключениям.\n"
        + _chapter("1")
        + "Раздел 1. Общие определения\n"
        + SUBSTANTIVE_BODY
        + "1.1\nПервая похожая норма.\n"
        + "1.2\nВторая похожая норма.\n"
        + "1.3\nТретья похожая норма.\n"
        + "1.4\nЧетвертая похожая норма.\n"
        + _chapter("2")
    )
    plan = _header_match_plan(content)
    rows = _catalog_rows(content)
    assert len(plan.legacy_bare_candidates) == 3
    assert any(row["kind"] == "section" for row in rows)

    _assert_manual_chapter_audit(content)


def test_section_primary_classification_is_unchanged():
    content = """Преамбула конституции.

Глава I. Основы строя
Раздел 1. Основные принципы
Текст первого раздела.
Раздел 2. Права граждан
Текст второго раздела.

Глава II. Органы власти
Раздел 3. Законодательная власть
Текст третьего раздела.
"""

    stats, blockers, warnings = _analyse(content)

    assert blockers == []
    assert warnings == []
    assert stats["documents_using_section_primary"] == 1
    assert stats["documents_using_chapter_primary"] == 0


def test_judicial_collection_does_not_count_as_chapter_primary():
    content = (
        "Описание судебной практики.\n\n"
        "Прецедент Окружного суда штата San-Andreas №0001 от 24.12.2024\n"
        + ("Подробное содержание первого судебного решения. " * 12)
        + "\n\nТолкование по обращению о толковании №2\n"
        + ("Подробное содержание второго судебного решения. " * 12)
        + "\n\nПОСТАНОВЛЕНИЕ ВЕРХОВНОГО СУДА ШТАТА САН-АНДРЕАС\n"
        "ОТ 17 АПРЕЛЯ 2026 ГОДА\n"
        "ПО ДЕЛУ О ТОЛКОВАНИИ СТАТЬИ 20 ТРУДОВОГО КОДЕКСА\n"
        + ("Подробное содержание третьего судебного решения. " * 12)
    )

    stats, blockers, warnings = _analyse(content)

    assert blockers == []
    assert warnings == []
    assert stats["documents_using_judicial_materials"] == 1
    assert stats["documents_using_chapter_primary"] == 0
