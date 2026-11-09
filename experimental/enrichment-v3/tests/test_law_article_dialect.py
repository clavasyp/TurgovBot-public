from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

import pytest

import services.indexing_service as indexing_service
import services.law_catalog_service as catalog_service
from scripts.backfill_law_servers import _analyse_headers
from services.indexing_service import (
    _bare_header_matches,
    _header_match_plan,
    _structural_header_matches,
)
from services.law_article_dialect import classify_article_dialect
from services.law_catalog_service import _catalog_rows, _match_existing_articles


SEPARATOR = "\u200b"


def _classify(text: str):
    structural = _structural_header_matches(text)
    bare = [
        match
        for match in _bare_header_matches(text)
        if not any(
            header.start() <= match.start() < header.end()
            for header in structural
        )
    ]
    return classify_article_dialect(
        text,
        structural=structural,
        bare_candidates=bare,
    ), structural, bare


def _chapter(number: int, *parts: str) -> str:
    return "\n".join(
        [
            f"Глава {number}. Проверяемая глава",
            SEPARATOR,
            *parts,
        ]
    )


def _strong_parent(code: int, label: str = "Самостоятельная норма") -> str:
    return f"{code}. {label} {code}\n{SEPARATOR}\nСодержательное тело нормы."


def _weak_parent(code: int, label: str = "Самостоятельная норма") -> str:
    return f"{code}.\n{label} {code}.\nСодержательное тело нормы."


def _primary_dotted(code: str, label: str = "Самостоятельная подстатья") -> str:
    return f"{code} {label} {code}\n{SEPARATOR}\nСодержательное тело нормы."


def _child_dotted(code: str) -> str:
    return f"{code}\nВнутренний пункт родительской нормы."


def _refs(result) -> list[str]:
    return [boundary.group(1) for boundary in result.article_boundaries]


def _assert_integrated_catalog(
    text: str,
    *,
    mode: str,
    rows: int,
    articles: int,
):
    plan = _header_match_plan(text)
    catalog_rows = _catalog_rows(text)

    assert plan.article_dialect_status == "resolved"
    assert plan.article_dialect_mode == mode
    assert plan.article_dialect_reasons == ()
    assert len(catalog_rows) == rows
    assert sum(row["kind"] == "article" for row in catalog_rows) == articles
    return plan, catalog_rows


def test_chapter_local_fixture_recovers_exact_57_articles():
    chapter_articles = {
        1: list(range(1, 3)),
        2: [1, 3, 4, 5, 6],
        3: [],
        4: list(range(1, 5)),
        5: list(range(1, 4)),
        6: list(range(1, 5)),
        7: list(range(1, 3)),
        8: list(range(1, 6)),
        9: [2, 3, 4],
        10: list(range(1, 3)),
        11: list(range(1, 7)),
        12: list(range(1, 3)),
        13: list(range(1, 17)),
    }
    chapters = []
    for chapter_number, article_codes in chapter_articles.items():
        parts = []
        for code in article_codes:
            parts.append(_strong_parent(code))
            parts.append(_child_dotted(f"{code}.9"))
        if chapter_number == 2:
            # Three independently formatted dotted parents.  They occupy the
            # outer path without inventing a missing Article 2.  Article 1 is
            # deliberately weak-formatted; its unique position before 1.1 is
            # the independent proof that it is the visible parent.
            parts[0] = _weak_parent(1)
            insert_after_one = [
                _primary_dotted("1.1"),
                _primary_dotted("2.1"),
                _primary_dotted("2.2"),
            ]
            parts = [parts[0], *insert_after_one, *parts[1:]]
        chapters.append(_chapter(chapter_number, *parts))
    text = "\n".join(["Процессуальный кодекс", *chapters])

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "chapter_local"
    assert len(result.article_boundaries) == 57
    assert any(ref.strip().startswith("2.1") for ref in _refs(result))
    assert not any(ref.startswith("Статья 1.9") for ref in _refs(result))
    # The absent Article 1 in chapter 9 is not synthesized.
    chapter_nine_start = text.index("Глава 9.")
    chapter_ten_start = text.index("Глава 10.")
    starts = [
        item.start()
        for item in result.article_boundaries
        if chapter_nine_start < item.start() < chapter_ten_start
    ]
    assert len(starts) == 3
    _assert_integrated_catalog(
        text,
        mode="chapter_local",
        rows=71,
        articles=57,
    )


def _las_vegas_dialect_text() -> str:
    chapters: dict[int, list[str]] = defaultdict(list)
    chapters[1] = [
        _strong_parent(1),
        _primary_dotted("1.2"),
        _primary_dotted("1.3"),
        _strong_parent(2),
        _weak_parent(3),
    ]
    chapters[2] = [
        _strong_parent(1),
        _primary_dotted("2.1"),
        _primary_dotted("2.2"),
        *[_strong_parent(code) for code in range(3, 9)],
    ]
    chapters[4] = [_strong_parent(code) for code in range(1, 5)]
    chapters[5] = [_strong_parent(1), _strong_parent(2)]
    chapters[6] = [_weak_parent(code) for code in range(1, 6)]
    chapters[7] = [_weak_parent(1), _strong_parent(2)]
    chapters[8] = [
        "1.1.\nОпределение, не являющееся внешней нормой.",
        "1.2.\nВиды розыска\n"
        f"{SEPARATOR}\n1.2.1.\nВнутренний вид розыска.",
        *[_strong_parent(code) for code in range(2, 5)],
        # It looks like a two-line heading followed by a separator, but has no
        # N.M.K subtree and therefore remains a child of Article 4.
        f"4.4.\nПоследний внутренний пункт.\n{SEPARATOR}",
    ]
    chapters[9] = [_weak_parent(code) for code in range(1, 5)]
    chapters[10] = [
        _strong_parent(1),
        _strong_parent(2),
        _primary_dotted("3.1"),
        _primary_dotted("3.2"),
        *[_strong_parent(code) for code in range(4, 7)],
    ]
    chapters[11] = [_child_dotted("1.1"), _child_dotted("1.2")]
    chapters[12] = [_child_dotted("1.1"), _child_dotted("1.2")]
    return "\n".join(
        [
            "Процессуальный кодекс",
            *[
                _chapter(number, *chapters[number])
                for number in range(1, 13)
            ],
        ]
    )


def test_chapter_local_fixture_recovers_exact_42_and_rejects_leaf_4_4():
    text = _las_vegas_dialect_text()

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "chapter_local"
    chapter_counts = {}
    for number in range(1, 13):
        start = text.index(f"Глава {number}.")
        end = (
            text.index(f"Глава {number + 1}.")
            if number < 12
            else len(text)
        )
        chapter_counts[number] = sum(
            start < boundary.start() < end
            for boundary in result.article_boundaries
        )
    assert chapter_counts == {
        1: 5,
        2: 9,
        3: 0,
        4: 4,
        5: 2,
        6: 5,
        7: 2,
        8: 4,
        9: 4,
        10: 7,
        11: 0,
        12: 0,
    }
    assert len(result.article_boundaries) == 42, chapter_counts
    refs = _refs(result)
    assert sum("4.4" in ref for ref in refs) == 0
    for code in ("1.2", "1.3", "2.1", "2.2", "3.1", "3.2"):
        assert any(code in ref for ref in refs)
    _assert_integrated_catalog(
        text,
        mode="chapter_local",
        rows=55,
        articles=42,
    )


def test_las_vegas_reconcile_preserves_all_true_dotted_ids(monkeypatch):
    text = _las_vegas_dialect_text()
    integrated_plan = _header_match_plan(text)
    new_rows = _catalog_rows(text)

    legacy_matches = sorted(
        [
            *integrated_plan.structural,
            *integrated_plan.legacy_bare_candidates,
        ],
        key=lambda boundary: boundary.start(),
    )
    legacy_plan = replace(
        integrated_plan,
        matches=tuple(legacy_matches),
        fallback_mode="bare",
        article_dialect_status="not_applicable",
        article_dialect_mode=None,
        article_dialect_reasons=(),
    )
    original_header_plan = indexing_service._header_match_plan
    monkeypatch.setattr(
        indexing_service,
        "_header_match_plan",
        lambda _text: legacy_plan,
    )
    old_rows = _catalog_rows(text)
    monkeypatch.setattr(
        indexing_service,
        "_header_match_plan",
        original_header_plan,
    )
    old_rows = [
        {
            **row,
            "id": 10_000 + index,
            "law_document_id": 501,
            "server_id": "las_vegas",
        }
        for index, row in enumerate(old_rows)
    ]

    matched, ambiguous = _match_existing_articles(new_rows, old_rows)
    true_dotted_indexes = [
        index
        for index, row in enumerate(new_rows)
        if (
            row["kind"] == "article"
            and not str(row.get("article_ref") or "").casefold().startswith(
                "статья "
            )
        )
    ]
    synthetic_parent_indexes = [
        index
        for index, row in enumerate(new_rows)
        if (
            row["kind"] == "article"
            and str(row.get("article_ref") or "").casefold().startswith(
                "статья "
            )
        )
    ]

    assert (len(old_rows), len(new_rows)) == (26, 55)
    assert sum(row["kind"] == "article" for row in old_rows) == 13
    assert sum(row["kind"] == "article" for row in new_rows) == 42
    assert len(true_dotted_indexes) == 7
    assert all(matched[index] is not None for index in true_dotted_indexes)
    assert len(synthetic_parent_indexes) == 35
    assert all(matched[index] is None for index in synthetic_parent_indexes)
    assert sum(item is not None for item in matched) == 20
    assert len(
        {
            row["id"]
            for row in old_rows
            if row["id"]
            not in {
                item["id"]
                for item in matched
                if item is not None
            }
        }
    ) == 6
    assert ambiguous == []
    # Preserved dotted articles whose now-correct right boundary changes keep
    # their IDs, but their stale enrichment/FRIDA input must be refreshed
    # alongside the newly inserted synthetic parents.
    assert sum(
        matched[index]["article_hash"] != new_rows[index]["article_hash"]
        for index in true_dotted_indexes
    ) == 4


ROAD_DOTTED = {
    1: ("1.1", "1.2", "1.3", "1.4"),
    6: ("6.1", "6.2"),
    7: ("7.1", "7.2", "7.3"),
    13: ("13.1",),
    14: ("14.1",),
    23: ("23.1",),
    26: ("26.1",),
    39: ("39.1",),
    59: ("59.1", "59.2"),
}


def test_global_fixture_recovers_66_parents_and_16_penalty_articles():
    parts = ["Дорожный кодекс"]
    chapter = 1
    for code in range(1, 67):
        if code == 1 or (code - 1) % 5 == 0:
            parts.append(f"Глава {chapter}. Дорожные нормы")
            parts.append(SEPARATOR)
            chapter += 1
        parts.extend(
            [
                f"{code}.",
                f"Самостоятельная дорожная норма {code}.",
                "Наказание: штраф.",
            ]
        )
        for dotted in ROAD_DOTTED.get(code, ()):
            parts.extend(
                [
                    f"{dotted}.",
                    f"Самостоятельная дорожная подстатья {dotted}.",
                ]
            )
            if dotted not in {"6.1", "6.2"}:
                parts.append("Наказание: штраф.")
        if code == 6:
            parts.append(
                "Наказание за нарушение процедур, описанных в статьях; "
                "6, 6.1, 6.2 - Штраф до $1200"
            )
    # Forum image captions have no payload and must not restart the global path.
    parts.extend(["1.", "2."])
    text = "\n".join(parts)

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "global"
    assert len(result.article_boundaries) == 82
    refs = _refs(result)
    assert sum(ref.startswith("Статья ") for ref in refs) == 66
    assert any(ref.strip().startswith("59.2") for ref in refs)
    assert any(ref.strip().startswith("6.1") for ref in refs)
    assert any(ref.strip().startswith("6.2") for ref in refs)
    _assert_integrated_catalog(
        text,
        mode="global",
        rows=97,
        articles=82,
    )


def test_global_foreign_citation_does_not_promote_local_dotted_child():
    parts = ["Кодекс с внешней ссылкой"]
    for code in range(1, 11):
        if code in (1, 5, 8):
            parts.append(f"Глава {(1, 5, 8).index(code) + 1}. Нормы")
        parts.extend(
            [
                f"{code}.",
                f"Самостоятельная глобальная норма {code}.",
            ]
        )
        if code == 3:
            parts.extend(
                [
                    "3.1",
                    "Ограничение применяется согласно статье 3.1 "
                    "Уголовного кодекса.",
                ]
            )
        elif code == 4:
            parts.extend(["4.1", "Внутренний пункт нормы."])
        elif code == 5:
            parts.extend(["5.1", "Внутренний пункт нормы."])
    text = "\n".join(parts)

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "global"
    assert len(result.article_boundaries) == 10
    assert not any(ref.strip().startswith("3.1") for ref in _refs(result))


def test_global_foreign_penalty_descriptor_does_not_bypass_ownership():
    parts = ["Кодекс с внешним наказанием"]
    for code in range(1, 11):
        if code in (1, 5, 8):
            parts.append(f"Глава {(1, 5, 8).index(code) + 1}. Нормы")
        parts.extend(
            [
                f"{code}.",
                f"Самостоятельная глобальная норма {code}.",
            ]
        )
        if code == 3:
            parts.extend(
                [
                    "3.1",
                    "Внутренний пункт со ссылкой на другую норму.",
                    "Наказание за нарушение положений, описанных в статьях "
                    "3.1 Уголовного кодекса - Штраф $5000.",
                    "Наказание за нарушение положений, описанных в статьях; "
                    "3, 3.1 Уголовного кодекса - Штраф $5000.",
                    "Наказание за нарушение положений Уголовного кодекса, "
                    "описанных в статьях; 3, 3.1 - Штраф $5000.",
                    "Наказание за нарушение процедур, описанных в статьях; "
                    "3, 3.1 - Штраф Уголовного кодекса $5000.",
                ]
            )
        elif code == 4:
            parts.extend(["4.1", "Внутренний пункт нормы."])
        elif code == 5:
            parts.extend(["5.1", "Внутренний пункт нормы."])
    text = "\n".join(parts)

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "global"
    assert len(result.article_boundaries) == 10
    assert not any(ref.strip().startswith("3.1") for ref in _refs(result))


def test_global_orphan_run_rejects_interleaved_competing_major():
    parts = ["Кодекс с конфликтным orphan run"]
    for code in range(1, 11):
        if code in (1, 5, 8):
            parts.append(f"Глава {(1, 5, 8).index(code) + 1}. Нормы")
        parts.extend(
            [
                f"{code}.",
                f"Самостоятельная глобальная норма {code}.",
            ]
        )
    parts.extend(
        [
            "11.1",
            "Первое положение предполагаемого orphan run.",
            "12.1",
            "Конкурирующая числовая семья.",
            "11.2",
            "Второе положение предполагаемого orphan run.",
            "11.3",
            "Третье положение предполагаемого orphan run.",
        ]
    )
    text = "\n".join(parts)

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "global"
    assert len(result.article_boundaries) == 10
    assert not any(ref.strip().startswith("11.") for ref in _refs(result))


def test_global_orphan_run_rejects_terminal_return_to_prior_family():
    parts = ["Кодекс с нетерминальным orphan run"]
    for code in range(1, 11):
        if code in (1, 5, 8):
            parts.append(f"Глава {(1, 5, 8).index(code) + 1}. Нормы")
        parts.extend(
            [
                f"{code}.",
                f"Самостоятельная глобальная норма {code}.",
            ]
        )
    parts.extend(
        [
            "11.1",
            "Первое положение предполагаемого orphan run.",
            "11.2",
            "Второе положение предполагаемого orphan run.",
            "11.3",
            "Третье положение предполагаемого orphan run.",
            "10.9",
            "Возврат к прежней числовой семье после orphan run.",
        ]
    )
    text = "\n".join(parts)

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "global"
    assert len(result.article_boundaries) == 10
    assert not any(ref.strip().startswith("11.") for ref in _refs(result))


def test_global_fixture_keeps_only_next_major_orphan_run_and_rejects_prose_chapter():
    parts = []
    for code in range(1, 14):
        if code in (1, 4, 8, 11):
            parts.extend(
                [
                    f"Глава {(1, 4, 8, 11).index(code) + 1}. Раздел кодекса",
                    SEPARATOR,
                ]
            )
        parts.extend(
            [
                f"{code}.",
                f"Самостоятельная гражданская норма {code}.",
                f"{code}.1",
                "Внутренний пункт родительской нормы.",
            ]
        )
        if code == 4:
            parts.append(
                "Глава 2 настоящего кодекса дополняется положением."
            )
    for minor in range(1, 9):
        parts.extend(
            [
                f"14.{minor}.",
                f"Самостоятельное заключительное положение {minor}.",
            ]
        )
    text = "\n".join(parts)

    result, structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "global"
    assert len(result.article_boundaries) == 21
    prose = next(
        boundary
        for boundary in structural
        if "настоящего кодекса" in boundary.group(1).casefold()
    )
    assert prose.start() in result.rejected_structural_starts
    refs = _refs(result)
    assert any(ref.strip().startswith("14.1") for ref in refs)
    assert not any(ref.strip().startswith("13.1") for ref in refs)
    assert not any(ref.startswith("Статья 14") for ref in refs)
    _assert_integrated_catalog(
        text,
        mode="global",
        rows=25,
        articles=21,
    )


def test_global_fixture_keeps_25_parents_and_rejects_self_reference_clause():
    parts = []
    for code in range(1, 26):
        if code in (1, 6, 12, 18, 21, 23):
            parts.extend(
                [
                    f"Глава {(1, 6, 12, 18, 21, 23).index(code) + 1}. Полномочия",
                    SEPARATOR,
                ]
            )
        child_body = (
            "Розыск лиц, уклоняющихся от отбывания уголовного наказания."
            if code == 10
            else "Внутренний пункт родительской нормы."
        )
        parts.extend(
            [
                f"{code}.",
                f"Самостоятельная норма о полномочиях {code}.",
                f"{code}.1",
                child_body,
            ]
        )
        if code == 6:
            parts.append(
                "Пункт 2 данной статьи не распространяется на особый отдел."
            )
    text = "\n".join(parts)

    result, structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "global"
    assert len(result.article_boundaries) == 25
    clause = next(
        boundary
        for boundary in structural
        if boundary.group(1).casefold().startswith("пункт 2")
    )
    assert clause.start() in result.rejected_structural_starts
    assert not any(ref.strip().startswith("6.1") for ref in _refs(result))
    # A substantive provision may mention criminal punishment without being
    # an independently labelled penalty article.
    assert not any(ref.strip().startswith("10.1") for ref in _refs(result))
    _assert_integrated_catalog(
        text,
        mode="global",
        rows=31,
        articles=25,
    )


def test_chapter_local_recovers_split_code_and_nested_outer_spine():
    chapter_one = _chapter(
        1,
        _weak_parent(1),
        "2\n. Если правило оглашено полностью, процедура продолжается.",
        _weak_parent(3),
        _weak_parent(4),
        _child_dotted("9.1"),
    )
    nested_parts = [
        _weak_parent(1),
    ]
    for major in range(2, 7):
        nested_parts.extend(
            [
                f"{major}.\nСамостоятельный раздел {major}.",
                "1.\nВнутренний пункт.",
                "2.\nВторой внутренний пункт.",
            ]
        )
    nested_parts.append(_child_dotted("9.2"))
    chapter_two = _chapter(2, *nested_parts)
    chapter_three = _chapter(
        3,
        _weak_parent(1),
        _weak_parent(2),
        _child_dotted("9.3"),
    )
    text = "\n".join(
        [
            "Кодекс со смешанной chapter-local структурой",
            chapter_one,
            chapter_two,
            chapter_three,
        ]
    )

    result, _structural, _bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "chapter_local"
    assert len(result.article_boundaries) == 12
    split_start = text.index("2\n. Если")
    split_boundary = next(
        boundary
        for boundary in result.article_boundaries
        if boundary.start() == split_start
    )
    assert split_boundary.group(1) == "Статья 2"
    chapter_two_start = text.index("Глава 2.")
    chapter_three_start = text.index("Глава 3.")
    assert sum(
        chapter_two_start < boundary.start() < chapter_three_start
        for boundary in result.article_boundaries
    ) == 6


@pytest.mark.parametrize("chapters", [3, 5, 12])
def test_true_nm_layout_preserves_original_boundaries_exactly(
    chapters,
    monkeypatch,
):
    parts = ["Кодекс с chapter.article диалектом"]
    for chapter in range(1, chapters + 1):
        parts.extend([f"Глава {chapter}. Нормы", SEPARATOR])
        for minor in range(1, 4):
            parts.extend(
                [
                    f"{chapter}.{minor}",
                    "Самостоятельная норма.",
                ]
            )
    text = "\n".join(parts)

    result, _structural, bare = _classify(text)

    assert result.status == "resolved"
    assert result.mode == "true_nm"
    assert result.article_boundaries == tuple(bare)
    assert [item.start() for item in result.article_boundaries] == [
        item.start() for item in bare
    ]
    plan, catalog_rows = _assert_integrated_catalog(
        text,
        mode="true_nm",
        rows=1 + chapters + (chapters * 3),
        articles=chapters * 3,
    )
    expected_matches = sorted(
        [*plan.structural, *plan.legacy_bare_candidates],
        key=lambda boundary: boundary.start(),
    )
    assert len(plan.matches) == len(expected_matches)
    assert all(
        actual is expected
        for actual, expected in zip(plan.matches, expected_matches)
    )
    integrated_signature = [
        (
            row["source_order"],
            row["kind"],
            row["code"],
            row["full_text"],
        )
        for row in catalog_rows
    ]
    legacy_plan = replace(
        plan,
        matches=tuple(expected_matches),
        fallback_mode="bare",
        article_dialect_status="not_applicable",
        article_dialect_mode=None,
        article_dialect_reasons=(),
    )
    monkeypatch.setattr(
        indexing_service,
        "_header_match_plan",
        lambda _text: legacy_plan,
    )
    assert integrated_signature == [
        (
            row["source_order"],
            row["kind"],
            row["code"],
            row["full_text"],
        )
        for row in _catalog_rows(text)
    ]


def _ambiguous_dialect_text() -> str:
    return "\n".join(
        [
            "Смешанный кодекс",
            _chapter(
                1,
                _strong_parent(1),
                _child_dotted("7.1"),
                "1.\nВложенный список.",
                "1.\nЕщё один конфликтующий вложенный список.",
            ),
            _chapter(2, _child_dotted("8.1"), _child_dotted("8.2")),
            _chapter(3, _child_dotted("9.1"), _child_dotted("9.2")),
        ]
    )


def test_incomplete_mixed_layout_is_reported_ambiguous_not_partially_applied():
    text = _ambiguous_dialect_text()

    result, _structural, _bare = _classify(text)

    assert result.status == "ambiguous"
    assert result.mode is None
    assert result.article_boundaries == ()
    assert result.rejected_structural_starts == frozenset()
    plan = _header_match_plan(text)
    assert plan.article_dialect_status == "ambiguous"
    assert plan.article_dialect_mode is None
    assert plan.article_dialect_reasons
    assert plan.fallback_mode == "article_dialect_ambiguous"
    assert not any(
        ref and ref.casefold().startswith("статья ")
        for ref, _body in indexing_service.split_into_blocks(text)
    )


def test_preflight_reports_dialect_mode_counters_and_ambiguity_blocker():
    resolved = "\n".join(
        [
            "Кодекс с chapter.article диалектом",
            *[
                _chapter(
                    chapter,
                    *[
                        _primary_dotted(f"{chapter}.{minor}")
                        for minor in range(1, 4)
                    ],
                )
                for chapter in range(1, 4)
            ],
        ]
    )
    ambiguous = _ambiguous_dialect_text()

    totals, blockers, _warnings = _analyse_headers(
        [
            {"id": 101, "content": resolved},
            {"id": 102, "content": ambiguous},
        ],
        max_unstructured_chars=4000,
    )

    assert totals["documents_using_article_dialect"] == 1
    assert totals["documents_using_article_dialect_true_nm"] == 1
    assert totals["documents_using_article_dialect_global"] == 0
    assert totals["documents_using_article_dialect_chapter_local"] == 0
    assert totals["documents_with_ambiguous_article_dialect"] == 1
    assert any(
        "doc=102: numeric article dialect is ambiguous" in blocker
        for blocker in blockers
    )


def test_judicial_materials_take_precedence_over_dialect_classifier(
    monkeypatch,
):
    judicial = (
        "Вводное описание судебной практики.\n\n"
        "Прецедент Окружного суда штата San-Andreas №0001 от 24.12.2024\n"
        "Статья 16.14 Уголовного кодекса здесь является только ссылкой.\n"
        + ("Подробное обоснование первого решения. " * 12)
        + "\n\nТолкование по обращению о толковании №2\n"
        "2.1\nВнутренний вывод, который не является статьёй.\n"
        + ("Подробное обоснование второго решения. " * 12)
        + "\n\nВЕРХОВНЫЙ СУД ШТАТА САН-АНДРЕАС\n"
        "ПОСТАНОВЛЕНИЕ\n"
        "ВЕРХОВНОГО СУДА ШТАТА SAN-ANDREAS\n"
        "от 22 января 2026 года\n"
        "ПО ДЕЛУ О ТОЛКОВАНИИ СТАТЬИ 17.6 УГОЛОВНОГО КОДЕКСА\n"
        + ("Подробное обоснование третьего решения. " * 12)
    )

    def unexpected_classifier(*_args, **_kwargs):
        pytest.fail("judicial proof must run before numeric dialect classification")

    monkeypatch.setattr(
        indexing_service,
        "classify_article_dialect",
        unexpected_classifier,
    )

    plan = _header_match_plan(judicial)

    assert plan.fallback_mode == "judicial_materials"
    assert plan.article_dialect_status == "not_applicable"
    assert len(plan.accepted_judicial_materials) == 3


def test_explicit_parent_recovery_takes_precedence_over_dialect_classifier(
    monkeypatch,
):
    long_payload = (
        "Подразделение осуществляет расследования и иные установленные "
        "законом полномочия для защиты общественной безопасности штата."
    )

    def explicit_range(start: int, end: int) -> str:
        return "\n".join(
            f"Статья {code}. Явная норма {code}\nТекст нормы {code}."
            for code in range(start, end + 1)
        )

    content = "\n".join(
        [
            "Глава I. Основной закон",
            explicit_range(1, 18),
            "Глава XI. Авиационное подразделение",
            f"19.1 {long_payload}",
            "Статья 20. Права подразделения",
            "20.1 Подразделение вправе использовать технику.",
            "Глава XII. Морское подразделение",
            "21. Морское подразделение выполняет специальные операции.",
            "21.1 Подразделение вправе проходить обучение.",
            "Статья 22. Следующее подразделение",
            "22.1 Подразделение выполняет задачи.",
            explicit_range(23, 27),
        ]
    )

    def unexpected_classifier(*_args, **_kwargs):
        pytest.fail(
            "accepted explicit-parent proof must own the document before dialect"
        )

    monkeypatch.setattr(
        indexing_service,
        "classify_article_dialect",
        unexpected_classifier,
    )

    plan = _header_match_plan(content)

    assert plan.fallback_mode == "mixed_parent_recovery"
    assert plan.article_dialect_status == "not_applicable"
    assert [
        boundary.canonical_ref
        for boundary in plan.recovered_parent_articles
    ] == [
        "Статья 19",
        "Статья 21. Морское подразделение выполняет специальные операции.",
    ]


@pytest.mark.asyncio
async def test_ambiguous_dialect_build_aborts_before_transaction(monkeypatch):
    def unexpected_transaction():
        pytest.fail("ambiguous dialect must not start a catalog transaction")

    monkeypatch.setattr(
        catalog_service.db_manager,
        "transaction",
        unexpected_transaction,
    )

    with pytest.raises(
        catalog_service.CatalogParseAmbiguityError,
        match="numeric article dialect is ambiguous",
    ):
        await catalog_service.build_catalog_for_document(
            {
                "id": 201,
                "server_id": "test",
                "title": "Ambiguous dialect",
                "content": _ambiguous_dialect_text(),
            }
        )


@pytest.mark.asyncio
async def test_ambiguous_dialect_preview_aborts_before_catalog_read(monkeypatch):
    async def unexpected_fetch(*_args, **_kwargs):
        pytest.fail("ambiguous dialect preview must stop before catalog read")

    monkeypatch.setattr(
        catalog_service.db_manager,
        "fetch",
        unexpected_fetch,
    )

    with pytest.raises(
        catalog_service.CatalogParseAmbiguityError,
        match="numeric article dialect is ambiguous",
    ):
        await catalog_service.preview_catalog_for_document(
            {
                "id": 202,
                "server_id": "test",
                "title": "Ambiguous dialect",
                "content": _ambiguous_dialect_text(),
            }
        )


@pytest.mark.asyncio
async def test_ambiguous_dialect_full_server_aborts_before_first_write(
    monkeypatch,
):
    async def fetch_documents(*_args, **_kwargs):
        return [
            {
                "id": 203,
                "server_id": "test",
                "title": "Ambiguous dialect",
                "content": _ambiguous_dialect_text(),
            }
        ]

    async def unexpected_execute(*_args, **_kwargs):
        pytest.fail("full server reconcile must stop before its first write")

    def unexpected_transaction():
        pytest.fail("full server reconcile must stop before its first transaction")

    monkeypatch.setattr(catalog_service.db_manager, "fetch", fetch_documents)
    monkeypatch.setattr(catalog_service.db_manager, "execute", unexpected_execute)
    monkeypatch.setattr(
        catalog_service.db_manager,
        "transaction",
        unexpected_transaction,
    )

    with pytest.raises(
        catalog_service.CatalogParseAmbiguityError,
        match="numeric article dialect is ambiguous",
    ):
        await catalog_service._build_catalog_for_server(
            "test",
            enrich=False,
            dry_run=False,
        )


@pytest.mark.asyncio
async def test_ambiguous_incremental_sync_keeps_pending_and_performs_no_write(
    monkeypatch,
):
    ambiguous_doc = {
        "id": 204,
        "server_id": "test",
        "source_thread_id": 12_204,
        "title": "Ambiguous dialect",
        "content": _ambiguous_dialect_text(),
        "content_hash": "source-hash",
        "last_updated": None,
        "derived_sync_pending": True,
    }
    safe_doc = {
        "id": 205,
        "server_id": "test",
        "source_thread_id": 12_205,
        "title": "Safe document in the same batch",
        "content": "Статья 1. Безопасная норма\nСодержательное тело нормы.",
        "content_hash": "safe-source-hash",
        "last_updated": None,
        "derived_sync_pending": True,
    }

    async def fake_fetch(query, *_args):
        if "source_thread_id = ANY" in query:
            return [ambiguous_doc, safe_doc]
        if "id = ANY" in query and "FROM law_documents" in query:
            return [ambiguous_doc, safe_doc]
        pytest.fail(f"unexpected read in fail-closed incremental path: {query}")

    async def fake_fetchval(query, *_args):
        if "FROM law_chunks" in query:
            return False
        if "FROM law_articles" in query:
            return True
        if "FROM law_embedding_jobs" in query:
            return 0
        pytest.fail(f"unexpected scalar read: {query}")

    async def no_related_spaces(_server_id):
        return []

    async def unlocked_sync(server_id, doc_ids):
        return await catalog_service._sync_catalog_for_documents(
            server_id,
            doc_ids,
        )

    def unexpected_transaction():
        pytest.fail(
            "ambiguous incremental sync must preserve old catalog/synonyms/FRIDA"
        )

    async def unexpected_execute(*_args, **_kwargs):
        pytest.fail("ambiguous incremental sync must not execute writes")

    async def unexpected_enrich(*_args, **_kwargs):
        pytest.fail("ambiguous incremental sync must not start enrichment")

    monkeypatch.setattr(indexing_service.db_manager, "fetch", fake_fetch)
    monkeypatch.setattr(indexing_service.db_manager, "fetchval", fake_fetchval)
    monkeypatch.setattr(
        indexing_service.db_manager,
        "execute",
        unexpected_execute,
    )
    monkeypatch.setattr(
        indexing_service.db_manager,
        "transaction",
        unexpected_transaction,
    )
    monkeypatch.setattr(
        catalog_service,
        "_queued_related_spaces",
        no_related_spaces,
    )
    monkeypatch.setattr(
        catalog_service,
        "active_space_id",
        lambda: "frida-test",
    )
    monkeypatch.setattr(
        catalog_service,
        "sync_catalog_for_documents",
        unlocked_sync,
    )
    monkeypatch.setattr(
        catalog_service,
        "enrich_articles_for_server",
        unexpected_enrich,
    )
    monkeypatch.setattr(indexing_service.config, "LAW_INDEX_AUTO_REINDEX", True)
    monkeypatch.setattr(indexing_service.config, "LAW_CATALOG_AUTO_SYNC", True)
    monkeypatch.setattr(indexing_service.config, "LAW_CATALOG_AUTO_ENRICH", True)

    stats = await indexing_service.reindex_changed_documents(
        "test",
        [
            ambiguous_doc["source_thread_id"],
            safe_doc["source_thread_id"],
        ],
    )

    assert stats["pending_cleared"] == 0
    assert stats["failed"] == 2
    assert ambiguous_doc["derived_sync_pending"] is True
    assert safe_doc["derived_sync_pending"] is True


def test_classifier_never_changes_raw_source_or_boundary_offsets():
    text = (
        "Глава 1\r\n"
        "1.1\r\nПервая норма.\r\n"
        "1.2\r\nВторая норма.\r\n"
        "1.3\r\nТретья норма.\r\n"
        "Глава 2\r\n"
        "2.1\r\nПервая норма.\r\n"
        "2.2\r\nВторая норма.\r\n"
        "2.3\r\nТретья норма.\r\n"
        "Глава 3\r\n"
        "3.1\r\nПервая норма.\r\n"
        "3.2\r\nВторая норма.\r\n"
        "3.3\r\nТретья норма."
    )
    original = text[:]

    result, _structural, bare = _classify(text)

    assert text == original
    assert result.mode == "true_nm"
    assert [boundary.start() for boundary in result.article_boundaries] == [
        match.start() for match in bare
    ]
