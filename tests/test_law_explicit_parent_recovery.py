from __future__ import annotations

import pytest

import scripts.backfill_law_servers as phase_a
import services.law_catalog_service as catalog
from services.indexing_service import _header_match_plan, split_into_blocks
from services.law_catalog_service import _catalog_rows


def _document(doc_id: int, content: str) -> dict:
    return {
        "id": doc_id,
        "server_id": "test",
        "source_thread_id": 10_000 + doc_id,
        "title": f"Документ {doc_id}",
        "content": content,
        "content_hash": f"hash-{doc_id}",
        "derived_sync_pending": False,
    }


def _explicit_range(start: int, end: int) -> str:
    return "\n".join(
        f"Статья {code}. Явная норма {code}\nТекст нормы {code}."
        for code in range(start, end + 1)
    )


_IMPLICIT_19 = (
    "19.1 Авиационное подразделение специализируется на разведке сил "
    "противника, участии в контртеррористических операциях и прямых "
    "вооружённых столкновениях, а также освобождении заложников с "
    "использованием военной техники."
)
_TAIL_STANDALONE_23 = (
    "23. Федеральная академия является кадровой службой бюро, организует "
    "обучение новых агентов, проводит инспекционные проверки подразделений "
    "и внутренние расследования в пределах установленной компетенции."
)
_TAIL_STANDALONE_24 = (
    "24. Основной отдел объединяет агентов после обучения и стажировки, "
    "осуществляет правоохранительную деятельность, противодействует "
    "терроризму и обеспечивает исполнение требований законодательства."
)
_TAIL_STANDALONE_28 = (
    "28. Специальный отдел состоит из подготовленных сотрудников различных "
    "государственных структур и противодействует террористическим и "
    "криминальным угрозам общественной безопасности."
)
_TAIL_STANDALONE_30 = (
    "30. Квалификация переговорщика присваивается наиболее подготовленным "
    "агентам, которые специализируются на мирном разрешении критических "
    "инцидентов, ведении диалога и освобождении заложников."
)


def _mixed_fib_fixture() -> str:
    return "\n".join(
        [
            "Глава I. Общие положения",
            "1.1 Первая часть не получила отдельной строки Статья 1.",
            "1.2 Вторая часть той же нормы.",
            _explicit_range(2, 8),
            "Глава IV. Права сотрудников",
            "9.1 Первое право сотрудника.",
            "9.2 Второе право сотрудника.",
            "Глава V. Взаимодействие",
            "10.1 Основное правило взаимодействия.",
            "10.2 Дополнительное правило взаимодействия.",
            "Глава VI. Защита сотрудников",
            "11. Защита прав сотрудников",
            "11.1 Гарантия защиты.",
            _explicit_range(12, 20),
            "Глава IX. Профильное управление",
            "21. Положение о профильном управлении",
            "21.1 Основная задача управления.",
            "Статья 22. Права профильного управления",
            "22.1 Право управления.",
            "Глава X. Академия",
            _TAIL_STANDALONE_23,
            "24. Права и обязанности академии",
            "24.1 Академия вправе обучать сотрудников.",
            "Глава XI. Основной отдел",
            _TAIL_STANDALONE_24,
            "25. Права и обязанности основного отдела",
            "25.1 Отдел вправе проводить операции.",
            "Глава XII. Специальный отдел",
            _TAIL_STANDALONE_28,
            "29. Права и обязанности специального отдела",
            "29.1 Отдел вправе задерживать подозреваемых.",
            "Глава XIII. Квалификация",
            _TAIL_STANDALONE_30,
            "31. Права специалиста",
            "31.1 Специалист вправе вести переговоры.",
            "32. Порядок присвоения квалификации",
            "32.1 Кандидат проходит экзамен.",
        ]
    )


def _mixed_guard_fixture(*, stray_bare: bool = False) -> str:
    suffix = (
        "\n99.1 Посторонний номер не принадлежит статье 27."
        if stray_bare
        else ""
    )
    return "\n".join(
        [
            "Глава I. Основной закон",
            _explicit_range(1, 18),
            "Глава XI. Авиационное подразделение",
            _IMPLICIT_19,
            "Статья 20. Права подразделения",
            "20.1 Подразделение вправе использовать технику.",
            "Глава XII. Морское подразделение",
            "21. Морское подразделение выполняет специальные операции.",
            "21.1 Подразделение вправе проходить обучение.",
            "Статья 22. Следующее подразделение",
            "22.1 Подразделение выполняет задачи.",
            _explicit_range(23, 27) + suffix,
        ]
    )


def _ambiguous_recovery_fixture() -> str:
    long_payload = (
        "Подразделение осуществляет расследования и иные установленные "
        "законом полномочия для защиты общественной и государственной "
        "безопасности на всей территории штата."
    )
    return "\n".join(
        [
            "Глава I. Начало",
            _explicit_range(1, 8),
            "Глава II. Первая восстановимая норма",
            f"9. {long_payload}",
            _explicit_range(10, 18),
            "Глава III. Вторая восстановимая норма",
            f"19. {long_payload}",
            _explicit_range(20, 24),
            "99.1 Чужой номер не принадлежит статье 24.",
        ]
    )


def test_recovers_complete_bounded_gaps_and_complete_chapter_tail():
    content = _mixed_fib_fixture()

    plan = _header_match_plan(content)
    rows = _catalog_rows(content)
    article_rows = [row for row in rows if row["kind"] == "article"]
    codes = [row["code"] for row in article_rows]

    assert plan.fallback_mode == "mixed_parent_recovery"
    assert len(plan.recovered_parent_articles) == 14
    assert plan.recovery_unresolved_bare_candidates == ()
    assert len(article_rows) == 31
    assert codes == [
        *[str(code) for code in range(1, 24)],
        "24",
        "24",
        "25",
        "28",
        "29",
        "30",
        "31",
        "32",
    ]

    blocks = {
        ref: body
        for ref, body in split_into_blocks(content)
        if ref is not None
    }
    assert blocks["Статья 1"].startswith("1.1 Первая часть")
    assert blocks["Статья 11. Защита прав сотрудников"].startswith(
        "11. Защита прав сотрудников"
    )


def test_recovers_only_two_complete_single_article_gaps():
    content = _mixed_guard_fixture()

    plan = _header_match_plan(content)
    article_codes = [
        row["code"]
        for row in _catalog_rows(content)
        if row["kind"] == "article"
    ]

    assert [boundary.canonical_ref for boundary in plan.recovered_parent_articles] == [
        "Статья 19",
        (
            "Статья 21. Морское подразделение выполняет "
            "специальные операции."
        ),
    ]
    assert plan.recovery_unresolved_bare_candidates == ()
    assert article_codes == [str(code) for code in range(1, 28)]


def test_parent_recovery_supports_crlf_without_rewriting_raw_body():
    content = _mixed_guard_fixture().replace("\n", "\r\n")

    plan = _header_match_plan(content)
    blocks = dict(
        (ref, body)
        for ref, body in split_into_blocks(content)
        if ref is not None
    )

    assert [boundary.canonical_ref for boundary in plan.recovered_parent_articles] == [
        "Статья 19",
        (
            "Статья 21. Морское подразделение выполняет "
            "специальные операции."
        ),
    ]
    assert plan.recovery_unresolved_bare_candidates == ()
    assert blocks[
        (
            "Статья 21. Морское подразделение выполняет "
            "специальные операции."
        )
    ].startswith(
        "21. Морское подразделение выполняет специальные операции.\r\n"
        "21.1 Подразделение вправе проходить обучение."
    )


def test_preflight_marks_only_fully_owned_recovery_as_resolved():
    clean = _mixed_guard_fixture()
    unresolved = _mixed_guard_fixture(stray_bare=True)

    clean_stats, clean_blockers, clean_warnings = phase_a._analyse_headers(
        [_document(1, clean)],
        max_unstructured_chars=12_000,
    )
    dirty_stats, dirty_blockers, dirty_warnings = phase_a._analyse_headers(
        [_document(2, unresolved)],
        max_unstructured_chars=12_000,
    )

    assert clean_blockers == []
    assert clean_warnings == []
    assert clean_stats["recovered_parent_articles"] == 2
    assert clean_stats["recovery_unresolved_bare_headers"] == 0
    assert clean_stats["documents_using_parent_recovery"] == 1

    assert dirty_blockers == []
    assert dirty_stats["recovered_parent_articles"] == 0
    assert dirty_stats["recovery_unresolved_bare_headers"] == 1
    assert dirty_stats["documents_using_parent_recovery"] == 0
    assert len(dirty_warnings) == 1
    assert "parent recovery rejected" in dirty_warnings[0]
    assert "unresolved bare N.M=1" in dirty_warnings[0]


def test_single_bounded_gap_is_not_enough_document_level_proof():
    long_payload = (
        "Подразделение осуществляет расследования и иные установленные "
        "законом полномочия для защиты общественной и государственной "
        "безопасности на всей территории штата."
    )
    content = "\n".join(
        [
            "Глава I. Начало",
            _explicit_range(1, 8),
            "Глава II. Единственный разрыв",
            f"9. {long_payload}",
            _explicit_range(10, 18),
        ]
    )

    plan = _header_match_plan(content)

    assert plan.recovered_parent_articles == ()
    assert "Статья 9" not in {
        ref for ref, _body in split_into_blocks(content)
    }


def test_new_chapter_list_and_citation_cannot_fill_two_numeric_gaps():
    content = "\n".join(
        [
            "Глава I. Начало",
            _explicit_range(1, 8),
            "Глава II. Перечень",
            "9.1 См. комментарий к статье 8 настоящего закона.",
            _explicit_range(10, 18),
            "Глава III. Второй перечень",
            "19. Первый пункт списка приложений",
            _explicit_range(20, 24),
        ]
    )

    plan = _header_match_plan(content)

    assert plan.recovered_parent_articles == ()
    assert not {"Статья 9", "Статья 19"} & {
        ref for ref, _body in split_into_blocks(content)
    }


def test_partial_recovery_with_one_unresolved_bare_is_rejected_and_warned():
    content = _ambiguous_recovery_fixture()

    plan = _header_match_plan(content)
    stats, blockers, warnings = phase_a._analyse_headers(
        [_document(3, content)],
        max_unstructured_chars=12_000,
    )

    assert plan.recovered_parent_articles == ()
    assert len(plan.recovery_unresolved_bare_candidates) == 1
    assert not any(
        ref
        and (
            ref.startswith("Статья 9.")
            or ref.startswith("Статья 19.")
        )
        for ref, _body in split_into_blocks(content)
    )
    assert stats["recovered_parent_articles"] == 0
    assert stats["recovery_unresolved_bare_headers"] == 1
    assert blockers == []
    assert len(warnings) == 1
    assert "parent recovery rejected" in warnings[0]


@pytest.mark.asyncio
async def test_catalog_mutation_aborts_before_transaction_on_ambiguity(
    monkeypatch,
):
    def unexpected_transaction():
        pytest.fail("catalog transaction must not start for ambiguous parse")

    monkeypatch.setattr(
        catalog.db_manager,
        "transaction",
        unexpected_transaction,
    )

    with pytest.raises(
        catalog.CatalogParseAmbiguityError,
        match="parent recovery is ambiguous",
    ):
        await catalog.build_catalog_for_document(
            {
                "id": 77,
                "server_id": "test",
                "title": "Ambiguous update",
                "content": _ambiguous_recovery_fixture(),
            }
        )


@pytest.mark.asyncio
async def test_catalog_preview_reports_same_ambiguity_before_db_read(
    monkeypatch,
):
    async def unexpected_fetch(*_args, **_kwargs):
        pytest.fail("preview must reject before reading mutable catalog state")

    monkeypatch.setattr(catalog.db_manager, "fetch", unexpected_fetch)

    with pytest.raises(catalog.CatalogParseAmbiguityError):
        await catalog.preview_catalog_for_document(
            {
                "id": 78,
                "server_id": "test",
                "title": "Ambiguous preview",
                "content": _ambiguous_recovery_fixture(),
            }
        )


def test_tail_parent_requires_child_or_full_disposition_payload():
    baseline = _header_match_plan(_mixed_fib_fixture())
    content = _mixed_fib_fixture().replace(
        _TAIL_STANDALONE_28,
        "28. Первый пункт списка",
    )
    assert any(
        boundary.canonical_ref.startswith("Статья 28")
        for boundary in baseline.recovered_parent_articles
    )

    plan = _header_match_plan(content)
    tail_codes = {"23", "24", "25", "28", "29", "30", "31", "32"}

    assert not tail_codes & {
        boundary.canonical_ref.split()[1].rstrip(".")
        for boundary in plan.recovered_parent_articles
    }


def test_nested_dotted_line_cannot_prove_an_implicit_parent():
    content = "\n".join(
        [
            "Глава I. Начало",
            _explicit_range(1, 8),
            "Глава II. Неполный переход",
            "9.1.1 Внутренняя часть не является доказательством статьи 9.",
            _explicit_range(10, 12),
        ]
    )

    plan = _header_match_plan(content)

    assert plan.recovered_parent_articles == ()
    assert "Статья 9" not in {
        ref for ref, _body in split_into_blocks(content)
    }


def test_numbered_list_inside_existing_article_does_not_fill_gap():
    content = "\n".join(
        [
            "Глава I. Начало",
            _explicit_range(1, 8),
            "9. Элемент перечня внутри статьи 8.",
            "9.1 Вложенный элемент того же перечня.",
            _explicit_range(10, 12),
        ]
    )

    plan = _header_match_plan(content)

    assert plan.recovered_parent_articles == ()
    assert not any(
        ref and ref.startswith("Статья 9")
        for ref, _body in split_into_blocks(content)
    )


def test_incomplete_gap_is_rejected_all_or_nothing():
    content = "\n".join(
        [
            "Глава I. Начало",
            _explicit_range(1, 8),
            "Глава II. Девятая норма",
            "9.1 Первая часть девятой нормы.",
            "Глава III. Одиннадцатая норма",
            "11.1 Первая часть одиннадцатой нормы.",
            _explicit_range(12, 14),
        ]
    )

    plan = _header_match_plan(content)

    assert plan.recovered_parent_articles == ()
    assert not {"Статья 9", "Статья 11"} & {
        ref for ref, _body in split_into_blocks(content)
    }


def test_pure_bare_chapter_dialect_remains_on_legacy_path():
    content = """Глава I. Первая глава
1. Родительская норма
1.1 Внутренний пункт
1.2 Ещё один внутренний пункт
Глава II. Вторая глава
2.1 Самостоятельная норма
2.2 Самостоятельная норма
2.3 Самостоятельная норма
"""

    plan = _header_match_plan(content)

    assert plan.recovered_parent_articles == ()
    assert plan.fallback_mode == "bare"


def test_dotted_explicit_articles_are_disjoint_from_parent_recovery():
    content = "\n".join(
        [
            "Глава 1. Дробная нумерация",
            *[
                f"Статья 1.{minor}. Самостоятельная статья\nТекст."
                for minor in range(1, 11)
            ],
            "2. Родитель другого формата",
            "2.1 Его возможный пункт.",
        ]
    )

    plan = _header_match_plan(content)

    assert plan.recovered_parent_articles == ()
    assert plan.fallback_mode is None
