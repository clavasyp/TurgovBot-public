from __future__ import annotations

from copy import deepcopy
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json

import pytest

import scripts.backfill_law_servers as phase_a


@asynccontextmanager
async def _fake_catalog_lock(_server_id):
    yield


def _doc(doc_id: int, content: str, **overrides):
    row = {
        "id": doc_id,
        "server_id": "las_vegas",
        "source_thread_id": 1000 + doc_id,
        "title": f"Документ {doc_id}",
        "content": content,
        "content_hash": f"hash-{doc_id}",
        "gemini_file_uri": f"gs://bucket/{doc_id}",
        "derived_sync_pending": False,
        "last_updated": "2026-07-28T00:00:00+00:00",
    }
    row.update(overrides)
    return row


LAW_TEXT = """Статья 1. Общие положения
Текст первой статьи.

Статья 2. Запрет
Текст второй статьи.

Статья 3. Ответственность
Текст третьей статьи.
"""


def test_targets_require_explicit_configured_game_servers():
    assert phase_a.validate_explicit_targets(["las_vegas", "vinewood"]) == [
        "las_vegas",
        "vinewood",
    ]
    with pytest.raises(phase_a.PhaseAError, match="явный список"):
        phase_a.validate_explicit_targets([])
    with pytest.raises(phase_a.PhaseAError, match="повторно"):
        phase_a.validate_explicit_targets(["las_vegas", "las_vegas"])
    with pytest.raises(phase_a.PhaseAError, match="игровым сервером"):
        phase_a.validate_explicit_targets(["gta5rp_rules_project"])
    with pytest.raises(phase_a.PhaseAError, match="игровым сервером"):
        phase_a.validate_explicit_targets(["typo_server"])


@pytest.mark.asyncio
async def test_enrichment_state_uses_authoritative_completion_not_hash_count(
    monkeypatch,
):
    from services import law_enrichment_completion as completion

    class _State:
        articles = 3
        # One row has a current body hash but stale structural context.
        enriched = 2

    async def evaluate(queryable, server_id):
        assert queryable is phase_a.db_manager
        assert server_id == "las_vegas"
        return _State()

    monkeypatch.setattr(
        completion,
        "evaluate_law_enrichment_completion",
        evaluate,
    )

    state = await phase_a._enrichment_state("las_vegas")

    assert state == phase_a.EnrichmentState(articles=3, enriched=2)
    assert state.complete is False


def test_bare_number_fallback_is_audited_with_core_splitter():
    content = """Глава 1
1.1 Первое нарушение
Текст.
1.2 Второе нарушение
Текст.
2.1 Третье нарушение
Текст.
"""
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )
    assert blockers == []
    assert warnings == []
    assert stats["documents_using_bare_fallback"] == 1
    assert stats["expected_articles"] == 3


def test_one_or_two_bare_headers_fail_closed():
    content = """1.1 Первое нарушение
Текст.
1.2 Второе нарушение
Текст.
"""
    _stats, blockers, _warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )
    assert any("fallback требует >=3" in blocker for blocker in blockers)


def test_large_unknown_header_format_fails_closed():
    _stats, blockers, _warnings = phase_a._analyse_headers(
        [_doc(1, "неизвестная разметка\n" + "текст " * 3000)],
        max_unstructured_chars=1_000,
    )
    assert any("без распознанной структуры" in blocker for blocker in blockers)


def test_mixed_headers_are_recorded_for_manual_audit():
    content = LAW_TEXT + "\n1.1 часть\n1.2 часть\n1.3 часть\n"
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )
    assert blockers == []
    assert stats["structured_article_headers"] == 3
    assert any("mixed headers" in warning for warning in warnings)


def test_header_audit_accepts_qualified_judicial_collection_without_article_warning():
    judicial = """Описание судебной практики.

Прецедент Окружного суда штата San-Andreas №0001 от 24.12.2024
""" + ("Подробное содержание первого судебного решения. " * 12) + """

Толкование по обращению о толковании №2
Статья 16.20 упоминается только как предмет разъяснения.
""" + ("Подробное содержание второго судебного решения. " * 12) + """

ПОСТАНОВЛЕНИЕ ВЕРХОВНОГО СУДА ШТАТА САН-АНДРЕАС
ОТ 17 АПРЕЛЯ 2026 ГОДА
ПО ДЕЛУ О ТОЛКОВАНИИ СТАТЬИ 20 ТРУДОВОГО КОДЕКСА
""" + ("Подробное содержание третьего судебного решения. " * 12)
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, LAW_TEXT), _doc(2, judicial)],
        max_unstructured_chars=12_000,
    )

    assert blockers == []
    assert warnings == []
    assert stats["documents_using_judicial_materials"] == 1
    assert stats["judicial_material_candidates"] == 3
    assert stats["accepted_judicial_materials"] == 3
    assert stats["documents_without_articles"] == 1
    assert stats["expected_articles"] == 3


def test_header_audit_accepts_explicit_section_primary_constitution():
    constitution = """Преамбула конституции.

Глава I. Основы строя
Раздел 1. Основные принципы
Текст первого раздела.
Раздел 2. Права граждан
Текст второго раздела.

Глава II. Органы власти
Раздел 3. Законодательная власть
Текст третьего раздела.
Раздел 4. Исполнительная власть
Текст четвертого раздела.
"""
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, LAW_TEXT), _doc(2, constitution)],
        max_unstructured_chars=12_000,
    )

    assert blockers == []
    assert warnings == []
    assert stats["documents_using_section_primary"] == 1
    assert stats["documents_without_articles"] == 1
    assert stats["expected_articles"] == 3


def test_header_audit_keeps_small_chapter_list_without_articles_fail_closed():
    chapter_list = """Название положения.
Глава I. Государственные награды
1. Общие положения.
2. Условия представления.
3. Церемония награждения.
"""
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, LAW_TEXT), _doc(2, chapter_list)],
        max_unstructured_chars=12_000,
    )

    assert blockers == []
    assert stats["documents_using_section_primary"] == 0
    assert stats["documents_without_articles"] == 1
    assert any("no kind='article'" in warning for warning in warnings)


def test_header_audit_rejects_section_primary_with_large_unknown_preamble():
    constitution = (
        "Неизвестная неструктурированная преамбула. " * 30
        + "\nГлава I. Основы\n"
        "Раздел 1. Первый\nТекст.\n"
        "Раздел 2. Второй\nТекст.\n"
        "Глава II. Власть\n"
        "Раздел 3. Третий\nТекст.\n"
    )
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, LAW_TEXT), _doc(2, constitution)],
        max_unstructured_chars=200,
    )

    assert blockers == []
    assert stats["documents_using_section_primary"] == 0
    assert any("no kind='article'" in warning for warning in warnings)


def test_header_audit_blocks_unqualified_judicial_contents():
    contents = """Прецедент Окружного суда штата SA №1
Коротко.
Толкование по обращению о толковании №2
Коротко.
Толкование по обращению в ВС №3
Коротко.
"""
    _stats, blockers, _warnings = phase_a._analyse_headers(
        [_doc(1, LAW_TEXT), _doc(2, contents)],
        max_unstructured_chars=12_000,
    )
    assert any("too short" in blocker for blocker in blockers)


def test_header_audit_does_not_double_count_multiline_article_codes_as_bare():
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
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )
    assert blockers == []
    assert warnings == []
    assert stats["structured_article_headers"] == 3
    assert stats["bare_header_candidates"] == 0
    assert stats["expected_articles"] == 3


def test_header_audit_counts_split_article_keyword_as_real_boundary():
    content = """Глава 11
С
татья 58.
Общие положения.
58.1.
Внутренний пункт.
Статья 59.
Следующая норма.
"""
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )
    assert blockers == []
    assert warnings == []
    assert stats["structured_article_headers"] == 2
    assert stats["expected_articles"] == 2


def test_mixed_special_headers_are_counted_separately_and_accepted():
    content = """Статья 1. Общая норма
Тело.
Особенная часть
Глава 10
10.6
.1
(F)
Разбойное ограбление
10.7
(F/R)
Неправомерное завладение
10.7.1
(F/R)
Неправомерное завладение специальным транспортом
"""
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )

    assert blockers == []
    assert warnings == []
    assert stats["structured_article_headers"] == 1
    assert stats["mixed_bare_header_candidates"] == 3
    assert stats["accepted_mixed_bare_headers"] == 3
    assert stats["documents_using_mixed_bare_fallback"] == 1
    assert stats["expected_articles"] == 4


def test_mixed_special_underfilled_chapter_fails_closed_per_chapter():
    content = """Статья 1. Общая норма
Тело.
Особенная часть
Глава 6
6.1
(F)
Первая норма
6.2
(R)
Вторая норма
Глава 7
7.1
(F)
Первая статья
7.2
(R)
Вторая статья
7.3
(F/R)
Третья статья
"""
    stats, blockers, _warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )

    assert stats["mixed_bare_header_candidates"] == 5
    assert stats["accepted_mixed_bare_headers"] == 3
    assert stats["mixed_bare_underfilled_chapters"] == 1
    assert stats["expected_articles"] == 4
    assert any(
        "Глава 6" in blocker and ">=3 на главу" in blocker
        for blocker in blockers
    )


def test_mixed_classified_chapter_tail_clears_warning_only_when_complete():
    content = """Глава 1
Статья 1.1
Первая норма.
Статья 1.2
Вторая норма.
Глава 2
Статья 2.1
Первая норма.
Статья 2.2
Вторая норма.
Глава 3
3.1
(A, CR) Первая статья. | 3*
Выход под залог: 1$.
Наказание: штраф.
3.2
(R) Вторая статья. | 2*
Выход под залог: 1$.
Наказание: штраф.
3.3
(F. CR) Третья статья. | 4*
Выход под залог: 1$.
Наказание: штраф.
Глава 4
4.1
(A,CR) Первая статья. | 3*
Выход под залог: 1$.
Наказание: штраф.
4.2
(R, CR) Вторая статья. | 3*
Выход под залог: 1$.
Наказание: штраф.
4.3
(F, CR) Третья статья. | 5*
Выход под залог: 1$.
Наказание: штраф.
Глава 5
5.1
(А, CR) Первая статья. | 3*
Выход под залог: 1$.
Наказание: штраф.
5.2
(R) Вторая статья. | 2*
Выход под залог: 1$.
Наказание: штраф.
5.3
(F, CR) Третья статья. | 4*
Выход под залог: 1$.
Наказание: штраф.
"""
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )

    assert blockers == []
    assert warnings == []
    assert stats["structured_article_headers"] == 4
    assert stats["mixed_bare_header_candidates"] == 9
    assert stats["accepted_mixed_bare_headers"] == 9
    assert stats["documents_using_mixed_bare_fallback"] == 1
    assert stats["expected_articles"] == 13


def test_header_audit_uses_same_citation_list_filter_as_catalog_parser():
    content = """Статья 1. Лицензии
Ссылки:
ст. 6.2, ст. 6.3, 6.5, ст. 10.6 УК

Статья 2. Ответственность
Тело нормы.
"""
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )
    assert blockers == []
    assert warnings == []
    assert stats["structured_article_headers"] == 2
    assert stats["expected_articles"] == 2


def test_header_audit_uses_same_contextual_citation_filters_as_parser():
    content = """3.1
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
    stats, blockers, warnings = phase_a._analyse_headers(
        [_doc(1, content)],
        max_unstructured_chars=12_000,
    )
    assert blockers == []
    assert warnings == []
    assert stats["bare_header_candidates"] == 5
    assert stats["expected_articles"] == 5


def test_checkpoint_is_atomic_resumable_and_target_bound(tmp_path):
    path = tmp_path / "phase-a.json"
    store = phase_a.CheckpointStore(path, ["las_vegas"])
    store.mark(
        "las_vegas",
        status="in_progress",
        stage="rag",
        details={"chunks": 10},
    )
    loaded = phase_a.CheckpointStore(path, ["las_vegas"])
    assert loaded.data["servers"]["las_vegas"]["stage"] == "rag"
    assert loaded.data["servers"]["las_vegas"]["details"] == {"chunks": 10}
    with pytest.raises(phase_a.PhaseAError, match="targets"):
        phase_a.CheckpointStore(path, ["vinewood"])


def test_checkpoint_v4_is_rejected_without_mutating_file(tmp_path):
    path = tmp_path / "phase-a-v4.json"
    payload = (
        b'{"version":4,"targets":["las_vegas"],'
        b'"created_at":"2026-07-28T00:00:00+00:00",'
        b'"updated_at":"2026-07-28T00:00:00+00:00",'
        b'"servers":{"las_vegas":{"status":"awaiting_enrichment",'
        b'"stage":"awaiting_enrichment"}}}\n'
    )
    path.write_bytes(payload)
    before = path.read_bytes()

    with pytest.raises(
        phase_a.PhaseAError,
        match="неподдерживаемая версия checkpoint: 4",
    ):
        phase_a.CheckpointStore(path, ["las_vegas"])

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        (
            {
                "documents": 2,
                "articles": 10,
                "ambiguous": 1,
                "deleted": 0,
            },
            "ambiguous",
        ),
        (
            {
                "documents": 2,
                "articles": 10,
                "ambiguous": 0,
                "deleted": 1,
            },
            "удалить",
        ),
        (
            {
                "documents": 1,
                "articles": 10,
                "ambiguous": 0,
                "deleted": 0,
            },
            "documents",
        ),
        (
            {
                "documents": 2,
                "articles": 9,
                "ambiguous": 0,
                "deleted": 0,
            },
            "rows",
        ),
    ],
)
def test_catalog_dry_run_fails_closed(summary, message):
    with pytest.raises(phase_a.PhaseAError, match=message):
        phase_a.validate_catalog_dry_run(
            "las_vegas",
            summary,
            expected_documents=2,
            expected_rows=10,
        )


def test_catalog_dry_run_accepts_non_destructive_exact_plan():
    phase_a.validate_catalog_dry_run(
        "las_vegas",
        {
            "documents": 2,
            "articles": 10,
            "ambiguous": 0,
            "deleted": 0,
            "inserted": 10,
        },
        expected_documents=2,
        expected_rows=10,
    )


@pytest.mark.asyncio
async def test_catalog_build_is_overlay_only_and_waits_for_manual_enrichment(
    monkeypatch,
):
    catalog_states = iter(
        [
            phase_a.CatalogState(3, 3, 0, 0, 3),
            phase_a.CatalogState(3, 3, 3, 3, 0),
        ]
    )
    enrichment_states = iter(
        [
            phase_a.EnrichmentState(3, 0),
        ]
    )
    build_calls = []
    recovery_order = []
    lock_events = []
    held = {"document": False, "catalog": False}

    async def fake_catalog_state(_server_id, _docs):
        recovery_order.append("catalog_truth")
        return next(catalog_states)

    async def fake_enrichment_state(_server_id):
        recovery_order.append("completion")
        return next(enrichment_states)

    async def fake_build(_server_id, *, enrich, dry_run):
        assert held == {"document": True, "catalog": True}
        build_calls.append((enrich, dry_run))
        recovery_order.append(
            "catalog_dry_run" if dry_run else "catalog_reconcile"
        )
        if dry_run:
            return {
                "documents": 1,
                "articles": 3,
                "ambiguous": 0,
                "deleted": 0,
                "inserted": 3,
            }
        return {"documents": 1, "failed_documents": 0}

    async def forbidden_enrich(*_args, **_kwargs):
        raise AssertionError("default path must not call model enrichment")

    import services.law_catalog_service as catalog_service
    import services.law_document_lock as document_lock_service
    import services.law_embedding_service as embedding_service

    @asynccontextmanager
    async def document_lock(server_id, *, manager):
        assert server_id == "las_vegas"
        assert manager is phase_a.db_manager
        lock_events.append("document_enter")
        held["document"] = True
        try:
            yield
        finally:
            assert held["catalog"] is False
            held["document"] = False
            lock_events.append("document_exit")

    @asynccontextmanager
    async def catalog_lock(server_id):
        assert server_id == "las_vegas"
        assert held["document"] is True
        lock_events.append("catalog_enter")
        held["catalog"] = True
        try:
            yield
        finally:
            held["catalog"] = False
            lock_events.append("catalog_exit")

    monkeypatch.setattr(phase_a, "_catalog_state", fake_catalog_state)
    monkeypatch.setattr(phase_a, "_enrichment_state", fake_enrichment_state)
    monkeypatch.setattr(catalog_service, "_build_catalog_for_server", fake_build)
    monkeypatch.setattr(
        catalog_service,
        "enrich_articles_for_server",
        forbidden_enrich,
    )
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        catalog_lock,
    )
    monkeypatch.setattr(
        document_lock_service,
        "law_document_server_lock",
        document_lock,
    )

    catalog, enrichment, run = await phase_a.ensure_catalog_and_enrichment(
        "las_vegas",
        [_doc(1, LAW_TEXT)],
    )

    assert catalog.complete
    assert not enrichment.complete
    assert build_calls == [(False, True), (False, False)]
    assert recovery_order == [
        "catalog_truth",
        "catalog_dry_run",
        "catalog_reconcile",
        "catalog_truth",
        "completion",
    ]
    assert lock_events == [
        "document_enter",
        "catalog_enter",
        "catalog_exit",
        "document_exit",
    ]
    assert run["enrichment_mode"] == phase_a.MANUAL_ENRICHMENT_MODE
    assert run["awaiting_enrichment"] is True
    assert run["cloud_calls_allowed"] is False
    assert "enrichment_retries" not in run


@pytest.mark.asyncio
async def test_failed_post_reconcile_truth_blocks_completion_and_model(
    monkeypatch,
):
    """A failed normal repair must not continue into enrichment selection."""

    incomplete = phase_a.CatalogState(3, 3, 2, 2, 1)
    catalog_states = iter((incomplete, incomplete))
    events = []

    async def fake_catalog_state(_server_id, _docs):
        events.append("catalog_truth")
        return next(catalog_states)

    async def fake_build(_server_id, *, enrich, dry_run):
        assert enrich is False
        events.append(
            "catalog_dry_run" if dry_run else "catalog_reconcile"
        )
        if dry_run:
            return {
                "documents": 1,
                "articles": 3,
                "ambiguous": 0,
                "deleted": 0,
                "inserted": 1,
            }
        return {"documents": 1, "failed_documents": 0}

    async def forbidden_completion(*_args, **_kwargs):
        raise AssertionError(
            "completion must wait for post-reconcile catalog truth"
        )

    async def forbidden_model(*_args, **_kwargs):
        raise AssertionError("Phase A recovery must not call Gemini")

    import services.law_catalog_service as catalog_service

    monkeypatch.setattr(phase_a, "_catalog_state", fake_catalog_state)
    monkeypatch.setattr(
        phase_a,
        "_enrichment_state",
        forbidden_completion,
    )
    monkeypatch.setattr(
        catalog_service,
        "_build_catalog_for_server",
        fake_build,
    )
    monkeypatch.setattr(
        catalog_service,
        "enrich_articles_for_server",
        forbidden_model,
    )

    with pytest.raises(
        phase_a.PhaseAError,
        match="catalog verification failed",
    ):
        await phase_a._ensure_catalog_and_enrichment_locked(
            "las_vegas",
            [_doc(1, LAW_TEXT)],
        )

    assert events == [
        "catalog_truth",
        "catalog_dry_run",
        "catalog_reconcile",
        "catalog_truth",
    ]


@dataclass
class _Vector:
    total: int = 3
    ready: int = 3
    queued: int = 0
    failed: int = 0
    mismatched: int = 0
    complete: bool = True
    compatible: bool = True
    recipe_version: str = phase_a.config.LAW_CATALOG_EMBED_RECIPE_VERSION
    runtime_compatible: bool = True


@pytest.mark.asyncio
async def test_frida_gate_requires_full_vector_and_empty_related(monkeypatch):
    async def fake_vector(_server_id):
        return _Vector()

    async def fake_related(_server_id):
        return {
            "total": 0,
            "terminal": 0,
            "embedding_jobs": 0,
            "active_embeddings": 0,
            "terminal_embeddings": 0,
        }

    monkeypatch.setattr(phase_a, "_vector_state", fake_vector)
    monkeypatch.setattr(phase_a, "_related_queue_state", fake_related)
    result = await phase_a.wait_for_frida_and_related(
        "las_vegas",
        expected_articles=3,
        timeout_seconds=1,
        poll_seconds=0.01,
    )
    assert result["vector"]["ready"] == 3
    assert result["related"]["total"] == 0


@pytest.mark.asyncio
async def test_frida_terminal_job_fails_immediately(monkeypatch):
    async def fake_vector(_server_id):
        return _Vector(ready=2, queued=0, failed=1, complete=False)

    async def fake_related(_server_id):
        return {
            "total": 1,
            "terminal": 1,
            "embedding_jobs": 0,
            "active_embeddings": 0,
            "terminal_embeddings": 0,
        }

    monkeypatch.setattr(phase_a, "_vector_state", fake_vector)
    monkeypatch.setattr(phase_a, "_related_queue_state", fake_related)
    with pytest.raises(phase_a.PhaseAError, match="terminal"):
        await phase_a.wait_for_frida_and_related(
            "las_vegas",
            expected_articles=3,
            timeout_seconds=1,
            poll_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_write_run_rejects_multiple_servers_before_mutation(
    monkeypatch,
    tmp_path,
):
    calls = []

    async def fake_process(server_id, *, store, options):
        calls.append(server_id)

    targets = ["las_vegas", "vinewood"]
    store = phase_a.CheckpointStore(tmp_path / "checkpoint.json", targets)
    monkeypatch.setattr(phase_a, "process_server", fake_process)

    with pytest.raises(phase_a.PhaseAError, match="exactly one server"):
        await phase_a.run(
            targets,
            store=store,
            options=phase_a.DriverOptions(),
        )
    assert calls == []
    assert store.data["servers"] == {}


@pytest.mark.asyncio
async def test_run_is_manual_only_and_resumes_awaiting_enrichment(
    monkeypatch,
    tmp_path,
):
    calls = []
    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_enrichment",
        stage="awaiting_enrichment",
        details={"enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE},
    )

    async def fake_process(server_id, *, store, options):
        calls.append((server_id, options.enrichment_mode))

    monkeypatch.setattr(phase_a, "process_server", fake_process)

    await phase_a.run(
        ["las_vegas"],
        store=store,
        options=phase_a.DriverOptions(),
    )
    assert calls == [("las_vegas", phase_a.MANUAL_ENRICHMENT_MODE)]


def test_driver_exposes_no_legacy_rag_or_vertex_mutation_entrypoints():
    assert not hasattr(phase_a, "ensure_rag")
    assert not hasattr(phase_a, "_init_vertex")
    assert not hasattr(phase_a, "_rag_embedding_requires_sdk_client")
    assert not hasattr(phase_a, "VERTEX_ENRICHMENT_MODE")
    options = phase_a.DriverOptions()
    assert options.enrichment_mode == phase_a.MANUAL_ENRICHMENT_MODE
    assert not hasattr(options, "allow_vertex_enrichment")
    assert not hasattr(options, "enrich_retries")
    assert not hasattr(options, "enrich_retry_delay")


@pytest.mark.asyncio
async def test_run_complete_is_noop_and_awaiting_smoke_requires_finalize(
    monkeypatch,
    tmp_path,
):
    calls = []

    async def fake_process(*_args, **_kwargs):
        calls.append("process")

    monkeypatch.setattr(phase_a, "process_server", fake_process)

    complete = phase_a.CheckpointStore(
        tmp_path / "complete.json",
        ["las_vegas"],
    )
    complete.mark(
        "las_vegas",
        status="complete",
        stage="complete",
        details={"enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE},
    )
    await phase_a.run(
        ["las_vegas"],
        store=complete,
        options=phase_a.DriverOptions(),
    )

    awaiting = phase_a.CheckpointStore(
        tmp_path / "awaiting.json",
        ["las_vegas"],
    )
    awaiting.mark(
        "las_vegas",
        status="awaiting_smoke",
        stage="awaiting_smoke",
        details={"enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE},
    )
    with pytest.raises(phase_a.PhaseAError, match="--finalize-smoke"):
        await phase_a.run(
            ["las_vegas"],
            store=awaiting,
            options=phase_a.DriverOptions(),
        )
    assert calls == []
    assert awaiting.data["servers"]["las_vegas"]["status"] == "awaiting_smoke"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["complete", "awaiting_smoke"])
async def test_preflight_never_overwrites_write_checkpoint(
    monkeypatch,
    tmp_path,
    status,
):
    path = tmp_path / f"{status}.json"
    store = phase_a.CheckpointStore(path, ["las_vegas"])
    store.mark(
        "las_vegas",
        status=status,
        stage=status,
        details={
            "smoke_challenge": {
                "run_nonce": "a" * 64,
                "issued_at": "2026-07-28T00:00:00+00:00",
            },
            "proof": "must-survive-byte-for-byte",
        },
    )
    before = path.read_bytes()

    async def forbidden_process(*_args, **_kwargs):
        raise AssertionError("guarded preflight must not reach process_server")

    monkeypatch.setattr(phase_a, "process_server", forbidden_process)

    with pytest.raises(phase_a.PhaseAError, match="refuses to overwrite"):
        await phase_a.run(
            ["las_vegas"],
            store=store,
            options=phase_a.DriverOptions(preflight_only=True),
        )

    assert path.read_bytes() == before
    assert (
        store.data["servers"]["las_vegas"]["details"]["smoke_challenge"][
            "run_nonce"
        ]
        == "a" * 64
    )


@pytest.mark.asyncio
async def test_preflight_ignores_missing_legacy_rag_and_gcs(monkeypatch):
    class FakeDb:
        async def fetchrow(self, query, *args):
            assert "FROM servers" in query
            return {
                "server_id": "las_vegas",
                "project_id": "majestic_rp",
                "existing_project_id": "majestic_rp",
                "server_disabled": False,
                "project_disabled": False,
            }

    async def fake_docs(_server_id):
        return [
            _doc(
                1,
                LAW_TEXT,
                gemini_file_uri=None,
            )
        ]

    def forbidden_legacy_rag(*_args, **_kwargs):
        raise AssertionError(
            "Overlay preflight must not read or rebuild legacy law_chunks"
        )

    async def fake_catalog(_server_id, _docs):
        return phase_a.CatalogState(3, 3, 1, 1, 2)

    monkeypatch.setattr(phase_a, "db_manager", FakeDb())
    monkeypatch.setattr(phase_a, "_fetch_source_documents", fake_docs)
    monkeypatch.setattr(phase_a, "_catalog_state", fake_catalog)
    monkeypatch.setattr(
        phase_a,
        "_rag_state",
        forbidden_legacy_rag,
        raising=False,
    )

    docs, summary = await phase_a.preflight_server(
        "las_vegas",
        max_unstructured_chars=12_000,
    )
    assert docs[0]["gemini_file_uri"] is None
    assert summary["legacy_derived_sync_pending"] == 0
    assert summary["legacy_rag_observation"] == {
        "required": False,
        "mutated": False,
        "observed": False,
    }
    assert summary["catalog"]["baseline"] == "partial"


@pytest.mark.asyncio
async def test_preflight_fails_on_pending_source(monkeypatch):
    class FakeDb:
        async def fetchrow(self, query, *args):
            return {
                "server_id": "las_vegas",
                "project_id": "majestic_rp",
                "existing_project_id": "majestic_rp",
                "server_disabled": False,
                "project_disabled": False,
            }

    async def fake_docs(_server_id):
        return [_doc(1, LAW_TEXT, derived_sync_pending=True)]

    async def fake_catalog(_server_id, _docs):
        return phase_a.CatalogState(3, 3, 0, 0, 3)

    monkeypatch.setattr(phase_a, "db_manager", FakeDb())
    monkeypatch.setattr(phase_a, "_fetch_source_documents", fake_docs)
    monkeypatch.setattr(phase_a, "_catalog_state", fake_catalog)
    with pytest.raises(phase_a.PhaseAError, match="derived_sync_pending"):
        await phase_a.preflight_server(
            "las_vegas",
            max_unstructured_chars=12_000,
        )


def _actual_catalog_rows(docs):
    rows = []
    for row_id, ((doc_id, source_order), wanted) in enumerate(
        phase_a._expected_catalog_rows(docs).items(),
        start=1,
    ):
        rows.append(
            {
                "id": row_id,
                "law_document_id": doc_id,
                "server_id": wanted["server_id"],
                "parent_server_id": wanted["server_id"],
                "source_order": source_order,
                "article_ref": wanted["article_ref"],
                "kind": wanted["kind"],
                "code": wanted["code"],
                "title": wanted["title"],
                "title_provenance": (
                    "source" if wanted["title"] is not None else None
                ),
                "doc_title": wanted["doc_title"],
                "full_text": wanted["full_text"],
                "article_hash": wanted["article_hash"],
            }
        )
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("article_ref", "Статья 999. Подмена"),
        ("kind", "section"),
        ("code", "999"),
        ("doc_title", "Переименованный документ"),
        ("full_text", "Изменённый текст при прежнем hash"),
        ("article_hash", "same-looking-but-wrong-hash"),
        ("title", "Подменённое parser-derived название"),
    ],
)
async def test_catalog_state_compares_every_source_field(
    monkeypatch,
    field,
    value,
):
    docs = [_doc(1, LAW_TEXT)]
    rows = _actual_catalog_rows(docs)

    class FakeDb:
        async def fetch(self, _query, *_args):
            return rows

    monkeypatch.setattr(phase_a, "db_manager", FakeDb())
    exact = await phase_a._catalog_state("las_vegas", docs)
    assert exact.complete is True

    rows[0][field] = value
    corrupted = await phase_a._catalog_state("las_vegas", docs)
    assert corrupted.complete is False
    assert corrupted.mismatched_rows == 1


@pytest.mark.asyncio
async def test_catalog_state_accepts_only_safe_llm_title_when_parser_has_none(
    monkeypatch,
):
    docs = [
        _doc(
            1,
            """Статья 1.
ч. 1 Норма без parser-derived названия.
""",
        )
    ]
    rows = _actual_catalog_rows(docs)
    assert phase_a._expected_catalog_rows(docs)[(1, 0)]["title"] is None
    rows[0]["title"] = "Краткое безопасное название"
    rows[0]["title_provenance"] = "generated"

    class FakeDb:
        async def fetch(self, _query, *_args):
            return rows

    monkeypatch.setattr(phase_a, "db_manager", FakeDb())
    safe = await phase_a._catalog_state("las_vegas", docs)
    assert safe.complete is True

    rows[0]["title"] = "★★★★★ [Федеральная/Региональная]"
    unsafe = await phase_a._catalog_state("las_vegas", docs)
    assert unsafe.complete is False
    assert unsafe.mismatched_rows == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "counter"),
    [
        ("missing", "missing_rows"),
        ("unexpected", "unexpected_rows"),
        ("duplicate", "duplicate_rows"),
        ("ownership", "ownership_mismatches"),
    ],
)
async def test_catalog_state_detects_shape_and_ownership_corruption(
    monkeypatch,
    mutation,
    counter,
):
    docs = [_doc(1, LAW_TEXT)]
    rows = _actual_catalog_rows(docs)
    if mutation == "missing":
        rows.pop()
    elif mutation == "unexpected":
        rows.append(
            {
                **rows[0],
                "id": 999,
                "law_document_id": 999,
                "source_order": 0,
            }
        )
    elif mutation == "duplicate":
        rows.append({**rows[0], "id": 999})
    else:
        rows[0]["server_id"] = "vinewood"

    class FakeDb:
        async def fetch(self, _query, *_args):
            return rows

    monkeypatch.setattr(phase_a, "db_manager", FakeDb())
    state = await phase_a._catalog_state("las_vegas", docs)
    assert state.complete is False
    assert getattr(state, counter) == 1


@pytest.mark.asyncio
async def test_preflight_header_warnings_block_unless_explicitly_allowed(
    monkeypatch,
):
    class FakeDb:
        async def fetchrow(self, _query, *_args):
            return {
                "server_id": "las_vegas",
                "project_id": "majestic_rp",
                "existing_project_id": "majestic_rp",
                "server_disabled": False,
                "project_disabled": False,
            }

    docs = [_doc(1, LAW_TEXT + "\n1.1 часть\n1.2 часть\n1.3 часть\n")]

    async def fake_docs(_server_id):
        return docs

    async def fake_catalog(_server_id, _docs):
        return phase_a.CatalogState(3, 3, 3, 3, 0)

    monkeypatch.setattr(phase_a, "db_manager", FakeDb())
    monkeypatch.setattr(phase_a, "_fetch_source_documents", fake_docs)
    monkeypatch.setattr(phase_a, "_catalog_state", fake_catalog)

    with pytest.raises(phase_a.PhaseAError, match="allow-header-warnings"):
        await phase_a.preflight_server(
            "las_vegas",
            max_unstructured_chars=12_000,
        )
    _docs, summary = await phase_a.preflight_server(
        "las_vegas",
        max_unstructured_chars=12_000,
        allow_header_warnings=True,
    )
    assert any("mixed headers" in item for item in summary["warnings"])


def test_source_fingerprint_covers_only_overlay_source_identity():
    row = _doc(1, LAW_TEXT)
    baseline = phase_a._source_fingerprint([row])
    assert baseline["algorithm"] == "sha256"
    assert baseline["snapshot_version"] == 2
    assert baseline["documents"] == 1
    for field, value in (
        ("server_id", "vinewood"),
        ("source_thread_id", 999),
        ("title", "Другое название"),
        ("content_hash", "changed"),
    ):
        assert (
            phase_a._source_fingerprint([{**row, field: value}])["value"]
            != baseline["value"]
        ), field
    for field, value in (
        ("gemini_file_uri", None),
        ("derived_sync_pending", True),
        ("last_updated", "2026-07-29T00:00:00+00:00"),
    ):
        assert (
            phase_a._source_fingerprint([{**row, field: value}]) == baseline
        ), field


@pytest.mark.asyncio
async def test_source_revalidation_rejects_overlay_change_and_pending(
    monkeypatch,
):
    original = _doc(1, LAW_TEXT)
    expected = phase_a._source_fingerprint([original])

    async def changed_docs(_server_id):
        return [{**original, "title": "Changed"}]

    monkeypatch.setattr(phase_a, "_fetch_source_documents", changed_docs)
    with pytest.raises(phase_a.PhaseAError, match="snapshot"):
        await phase_a._assert_source_snapshot("las_vegas", expected)

    async def legacy_changed_docs(_server_id):
        return [
            {
                **original,
                "gemini_file_uri": None,
                "last_updated": "2026-07-29T00:00:00+00:00",
            }
        ]

    monkeypatch.setattr(phase_a, "_fetch_source_documents", legacy_changed_docs)
    current = await phase_a._assert_source_snapshot("las_vegas", expected)
    assert current[0]["gemini_file_uri"] is None

    async def pending_docs(_server_id):
        return [{**original, "derived_sync_pending": True}]

    monkeypatch.setattr(phase_a, "_fetch_source_documents", pending_docs)
    with pytest.raises(phase_a.PhaseAError, match="derived_sync_pending"):
        await phase_a._assert_source_snapshot("las_vegas", expected)


@pytest.mark.asyncio
async def test_final_verify_is_overlay_only_when_legacy_rag_is_unavailable(
    monkeypatch,
):
    docs = [_doc(1, LAW_TEXT, gemini_file_uri=None)]
    fingerprint = phase_a._source_fingerprint(docs)

    async def fake_source(_server_id, expected):
        assert dict(expected) == fingerprint
        return docs

    async def fake_catalog(_server_id, _docs):
        return phase_a.CatalogState(3, 3, 3, 3, 0)

    async def fake_enrichment(_server_id):
        return phase_a.EnrichmentState(3, 3)

    async def fake_vector(_server_id):
        return _Vector()

    async def fake_related(_server_id):
        return {
            "total": 0,
            "terminal": 0,
            "embedding_jobs": 0,
            "active_embeddings": 0,
            "terminal_embeddings": 0,
        }

    def forbidden_cloud_or_legacy_rag(*_args, **_kwargs):
        raise AssertionError(
            "final Overlay verification must not touch RAG/Vertex/Gemini"
        )

    import rate_limiter
    import services.indexing_service as indexing_service
    import services.law_catalog_service as catalog_service

    monkeypatch.setattr(phase_a, "_assert_source_snapshot", fake_source)
    monkeypatch.setattr(phase_a, "_catalog_state", fake_catalog)
    monkeypatch.setattr(phase_a, "_enrichment_state", fake_enrichment)
    monkeypatch.setattr(phase_a, "_vector_state", fake_vector)
    monkeypatch.setattr(phase_a, "_related_queue_state", fake_related)
    monkeypatch.setattr(
        phase_a,
        "_rag_state",
        forbidden_cloud_or_legacy_rag,
        raising=False,
    )
    monkeypatch.setattr(
        indexing_service,
        "index_server_laws",
        forbidden_cloud_or_legacy_rag,
    )
    monkeypatch.setattr(
        catalog_service,
        "enrich_articles_for_server",
        forbidden_cloud_or_legacy_rag,
    )
    monkeypatch.setattr(
        rate_limiter,
        "init_client",
        forbidden_cloud_or_legacy_rag,
    )

    result = await phase_a.final_verify(
        "las_vegas",
        docs,
        expected_source_fingerprint=fingerprint,
    )

    assert result["legacy_rag_observation"] == {
        "required": False,
        "mutated": False,
        "observed": False,
    }
    assert result["catalog"]["actual_articles"] == 3
    assert result["vector"]["ready"] == 3


@pytest.mark.asyncio
async def test_frida_waits_until_all_active_space_job_rows_are_deleted(
    monkeypatch,
):
    related_states = iter(
        [
            {
                "total": 0,
                "terminal": 0,
                "embedding_jobs": 1,
                "active_embeddings": 0,
                "terminal_embeddings": 0,
            },
            {
                "total": 0,
                "terminal": 0,
                "embedding_jobs": 0,
                "active_embeddings": 0,
                "terminal_embeddings": 0,
            },
        ]
    )

    async def fake_vector(_server_id):
        return _Vector()

    async def fake_related(_server_id):
        return next(related_states)

    monkeypatch.setattr(phase_a, "_vector_state", fake_vector)
    monkeypatch.setattr(phase_a, "_related_queue_state", fake_related)
    result = await phase_a.wait_for_frida_and_related(
        "las_vegas",
        expected_articles=3,
        timeout_seconds=1,
        poll_seconds=0.001,
    )
    assert result["related"]["embedding_jobs"] == 0


@pytest.mark.asyncio
async def test_process_releases_catalog_lock_for_worker_and_awaits_smoke(
    monkeypatch,
    tmp_path,
):
    import services.law_document_lock as document_locks
    import services.law_embedding_service as embedding_service

    state = {"document": False, "catalog": False}
    events = []
    docs = [_doc(1, LAW_TEXT)]
    fingerprint = phase_a._source_fingerprint(docs)
    catalog = phase_a.CatalogState(3, 3, 3, 3, 0)
    enrichment = phase_a.EnrichmentState(3, 3)

    @asynccontextmanager
    async def fake_document_lock(_server_id, **_kwargs):
        assert not state["document"]
        state["document"] = True
        events.append("document_enter")
        try:
            yield
        finally:
            state["document"] = False
            events.append("document_exit")

    @asynccontextmanager
    async def fake_catalog_lock(_server_id):
        assert state["document"]
        assert not state["catalog"]
        state["catalog"] = True
        events.append("catalog_enter")
        try:
            yield
        finally:
            state["catalog"] = False
            events.append("catalog_exit")

    async def fake_preflight(_server_id, **_kwargs):
        return docs, {
            "documents": 1,
            "source_fingerprint": fingerprint,
            "headers": {"expected_articles": 3},
            "legacy_rag_observation": dict(
                phase_a.LEGACY_RAG_OBSERVATION
            ),
            "catalog": {"baseline": "empty"},
            "warnings": [],
        }

    async def fake_assert_source(_server_id, _fingerprint):
        assert state["document"]
        events.append("source")
        return docs

    async def fake_catalog(_server_id, _docs, **_kwargs):
        assert _kwargs == {}
        assert state == {"document": True, "catalog": True}
        events.append("catalog_write")
        return catalog, enrichment, {"dry_run": {}}

    async def fake_wait(_server_id, **_kwargs):
        assert state == {"document": True, "catalog": False}
        events.append("worker_wait")
        return {
            "vector": {"ready": 3},
            "related": {"total": 0, "embedding_jobs": 0},
        }

    async def fake_final(_server_id, _docs, **_kwargs):
        assert state == {"document": True, "catalog": True}
        events.append("final")
        return {
            "legacy_rag_observation": dict(
                phase_a.LEGACY_RAG_OBSERVATION
            ),
            "catalog": {"actual_articles": 3},
            "enrichment": {"enriched": 3},
            "vector": {"ready": 3},
            "related": {"total": 0},
            "source_fingerprint": fingerprint,
        }

    def forbidden_cloud_or_legacy_rag(*_args, **_kwargs):
        raise AssertionError(
            "Overlay-only process must not call RAG/Vertex/Gemini"
        )

    import rate_limiter
    import services.indexing_service as indexing_service
    import services.law_catalog_service as catalog_service

    monkeypatch.setattr(
        document_locks,
        "law_document_server_lock",
        fake_document_lock,
    )
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        fake_catalog_lock,
    )
    monkeypatch.setattr(
        indexing_service,
        "index_server_laws",
        forbidden_cloud_or_legacy_rag,
    )
    monkeypatch.setattr(
        catalog_service,
        "enrich_articles_for_server",
        forbidden_cloud_or_legacy_rag,
    )
    monkeypatch.setattr(
        rate_limiter,
        "init_client",
        forbidden_cloud_or_legacy_rag,
    )
    monkeypatch.setattr(phase_a, "preflight_server", fake_preflight)
    monkeypatch.setattr(phase_a, "_assert_source_snapshot", fake_assert_source)
    monkeypatch.setattr(
        phase_a,
        "_ensure_catalog_and_enrichment_locked",
        fake_catalog,
    )
    monkeypatch.setattr(phase_a, "wait_for_frida_and_related", fake_wait)
    monkeypatch.setattr(phase_a, "final_verify", fake_final)

    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    await phase_a.process_server(
        "las_vegas",
        store=store,
        options=phase_a.DriverOptions(),
    )
    assert store.data["servers"]["las_vegas"]["status"] == "awaiting_smoke"
    challenge = store.data["servers"]["las_vegas"]["details"]["smoke_challenge"]
    assert len(challenge["run_nonce"]) == 64
    assert challenge["issued_at"] < challenge["expires_at"]
    assert events == [
        "document_enter",
        "catalog_enter",
        "catalog_write",
        "source",
        "catalog_exit",
        "worker_wait",
        "catalog_enter",
        "final",
        "catalog_exit",
        "document_exit",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "allow_header_warnings",
    [False, True],
    ids=["default-false", "audited-warnings"],
)
async def test_process_stops_at_awaiting_enrichment_before_frida(
    monkeypatch,
    tmp_path,
    allow_header_warnings,
):
    import services.law_document_lock as document_locks
    import services.law_embedding_service as embedding_service

    docs = [_doc(1, LAW_TEXT)]
    fingerprint = phase_a._source_fingerprint(docs)
    catalog = phase_a.CatalogState(3, 3, 3, 3, 0)
    enrichment = phase_a.EnrichmentState(3, 0)

    @asynccontextmanager
    async def fake_lock(_server_id, **_kwargs):
        yield

    async def fake_preflight(_server_id, **kwargs):
        assert kwargs["allow_header_warnings"] is allow_header_warnings
        return docs, {
            "documents": 1,
            "source_fingerprint": fingerprint,
            "headers": {"expected_articles": 3},
            "legacy_rag_observation": dict(
                phase_a.LEGACY_RAG_OBSERVATION
            ),
            "catalog": {"baseline": "empty"},
            "warnings": [],
        }

    async def fake_source(_server_id, _fingerprint):
        return docs

    async def fake_catalog(_server_id, _docs, **kwargs):
        assert kwargs == {}
        return catalog, enrichment, {
            "dry_run": {},
            "enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE,
            "awaiting_enrichment": True,
        }

    async def forbidden_frida(*_args, **_kwargs):
        raise AssertionError("FRIDA wait must happen after manual enrichment")

    monkeypatch.setattr(
        document_locks,
        "law_document_server_lock",
        fake_lock,
    )
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        fake_lock,
    )
    monkeypatch.setattr(phase_a, "preflight_server", fake_preflight)
    monkeypatch.setattr(phase_a, "_assert_source_snapshot", fake_source)
    monkeypatch.setattr(
        phase_a,
        "_ensure_catalog_and_enrichment_locked",
        fake_catalog,
    )
    monkeypatch.setattr(phase_a, "wait_for_frida_and_related", forbidden_frida)

    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    await phase_a.process_server(
        "las_vegas",
        store=store,
        options=phase_a.DriverOptions(
            allow_header_warnings=allow_header_warnings,
        ),
    )

    entry = store.data["servers"]["las_vegas"]
    assert entry["status"] == "awaiting_enrichment"
    assert entry["stage"] == "awaiting_enrichment"
    assert (
        entry["details"]["enrichment_mode"]
        == phase_a.MANUAL_ENRICHMENT_MODE
    )
    assert (
        entry["details"]["allow_header_warnings"]
        is allow_header_warnings
    )
    assert (
        entry["details"]["next_step"]["allow_header_warnings"]
        is allow_header_warnings
    )
    header_warning_audit = entry["details"]["header_warning_audit"]
    if allow_header_warnings:
        assert (
            header_warning_audit["schema"]
            == phase_a.HEADER_WARNING_AUDIT_SCHEMA
        )
        assert header_warning_audit["source_fingerprint"] == fingerprint
        assert header_warning_audit["warnings"] == []
        assert len(header_warning_audit["contract_sha256"]) == 64
    else:
        assert header_warning_audit is None
    assert "export_law_enrichment_batches.py" in (
        entry["details"]["next_step"]["export_command"]
    )
    next_step = entry["details"]["next_step"]
    assert "build_law_enrichment_apply_plan.py" in (
        next_step["aggregate_command_template"]
    )
    assert "reviewed-all-envelope-v3.json" in next_step["apply_command"]
    assert "_apply_claude_enrich.py" in next_step["apply_command"]
    assert "REVIEWED_BATCH.json" not in json.dumps(
        next_step,
        ensure_ascii=False,
    )
    resume_command = entry["details"]["next_step"]["resume_command"]
    assert "backfill_law_servers.py las_vegas" in resume_command
    assert (
        "--allow-header-warnings" in resume_command
    ) is allow_header_warnings


@pytest.mark.asyncio
async def test_run_refuses_to_drop_checkpoint_header_warning_decision(
    monkeypatch,
    tmp_path,
):
    fingerprint = phase_a._source_fingerprint([_doc(1, LAW_TEXT)])
    warnings = ["doc=1: mixed headers require manual audit"]
    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_enrichment",
        stage="awaiting_enrichment",
        details={
            "enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE,
            "allow_header_warnings": True,
            "header_warning_audit": phase_a._header_warning_audit_contract(
                fingerprint,
                warnings,
            ),
        },
    )
    checkpoint_before = store.path.read_bytes()

    async def forbidden_process(*_args, **_kwargs):
        raise AssertionError("mismatched run contract must fail before process")

    monkeypatch.setattr(phase_a, "process_server", forbidden_process)

    with pytest.raises(
        phase_a.PhaseAError,
        match="checkpoint allow_header_warnings=True",
    ):
        await phase_a.run(
            ["las_vegas"],
            store=store,
            options=phase_a.DriverOptions(),
        )

    entry = store.data["servers"]["las_vegas"]
    assert entry["status"] == "awaiting_enrichment"
    assert entry["details"]["allow_header_warnings"] is True
    assert store.path.read_bytes() == checkpoint_before


@pytest.mark.asyncio
async def test_legacy_true_checkpoint_without_bound_audit_fails_closed(
    monkeypatch,
    tmp_path,
):
    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_enrichment",
        stage="awaiting_enrichment",
        details={
            "enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE,
            "allow_header_warnings": True,
        },
    )
    checkpoint_before = store.path.read_bytes()

    async def forbidden_process(*_args, **_kwargs):
        raise AssertionError("invalid legacy contract must fail before process")

    monkeypatch.setattr(phase_a, "process_server", forbidden_process)

    with pytest.raises(
        phase_a.HeaderWarningAuditError,
        match="has no bound header_warning_audit",
    ):
        await phase_a.run(
            ["las_vegas"],
            store=store,
            options=phase_a.DriverOptions(allow_header_warnings=True),
        )

    assert store.path.read_bytes() == checkpoint_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    ["source", "warnings"],
    ids=["source-fingerprint-drift", "warning-set-drift"],
)
async def test_audited_warning_resume_rejects_drift_without_checkpoint_write(
    monkeypatch,
    tmp_path,
    drift,
):
    import services.law_document_lock as document_locks
    import services.law_embedding_service as embedding_service

    stored_docs = [_doc(1, LAW_TEXT)]
    stored_fingerprint = phase_a._source_fingerprint(stored_docs)
    stored_warnings = ["doc=1: mixed headers require manual audit"]
    current_docs = stored_docs
    current_fingerprint = stored_fingerprint
    current_warnings = stored_warnings
    if drift == "source":
        current_docs = [
            _doc(1, LAW_TEXT + "\nНовая строка.", content_hash="hash-new")
        ]
        current_fingerprint = phase_a._source_fingerprint(current_docs)
    else:
        current_warnings = [
            *stored_warnings,
            "doc=2: a newly discovered warning",
        ]

    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_enrichment",
        stage="awaiting_enrichment",
        details={
            "enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE,
            "allow_header_warnings": True,
            "header_warning_audit": phase_a._header_warning_audit_contract(
                stored_fingerprint,
                stored_warnings,
            ),
        },
    )
    checkpoint_before = store.path.read_bytes()

    @asynccontextmanager
    async def fake_document_lock(_server_id, **_kwargs):
        yield

    async def fake_preflight(_server_id, **kwargs):
        assert kwargs["allow_header_warnings"] is True
        return current_docs, {
            "documents": 1,
            "source_fingerprint": current_fingerprint,
            "headers": {"expected_articles": 3},
            "catalog": {"baseline": "empty"},
            "warnings": current_warnings,
        }

    @asynccontextmanager
    async def forbidden_catalog_lock(_server_id):
        raise AssertionError("audit drift must fail before catalog work")
        yield

    async def forbidden_catalog(*_args, **_kwargs):
        raise AssertionError("audit drift must fail before catalog work")

    monkeypatch.setattr(
        document_locks,
        "law_document_server_lock",
        fake_document_lock,
    )
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        forbidden_catalog_lock,
    )
    monkeypatch.setattr(phase_a, "preflight_server", fake_preflight)
    monkeypatch.setattr(
        phase_a,
        "_ensure_catalog_and_enrichment_locked",
        forbidden_catalog,
    )

    with pytest.raises(
        phase_a.HeaderWarningAuditError,
        match="source fingerprint or exact header-warning set changed",
    ):
        await phase_a.run(
            ["las_vegas"],
            store=store,
            options=phase_a.DriverOptions(allow_header_warnings=True),
        )

    assert store.path.read_bytes() == checkpoint_before
    assert store.data["servers"]["las_vegas"]["status"] == (
        "awaiting_enrichment"
    )


@pytest.mark.asyncio
async def test_audited_warning_resume_accepts_exact_contract(
    monkeypatch,
    tmp_path,
):
    import services.law_document_lock as document_locks
    import services.law_embedding_service as embedding_service

    docs = [_doc(1, LAW_TEXT)]
    fingerprint = phase_a._source_fingerprint(docs)
    warnings = ["doc=1: mixed headers require manual audit"]
    catalog = phase_a.CatalogState(3, 3, 3, 3, 0)
    enrichment = phase_a.EnrichmentState(3, 0)
    audit = phase_a._header_warning_audit_contract(
        fingerprint,
        warnings,
    )
    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_enrichment",
        stage="awaiting_enrichment",
        details={
            "enrichment_mode": phase_a.MANUAL_ENRICHMENT_MODE,
            "allow_header_warnings": True,
            "header_warning_audit": audit,
        },
    )
    events = []

    @asynccontextmanager
    async def fake_lock(_server_id, **_kwargs):
        yield

    async def fake_preflight(_server_id, **kwargs):
        assert kwargs["allow_header_warnings"] is True
        events.append("preflight")
        return docs, {
            "documents": 1,
            "source_fingerprint": fingerprint,
            "headers": {"expected_articles": 3},
            "catalog": {"baseline": "empty"},
            "warnings": warnings,
        }

    async def fake_catalog(_server_id, _docs):
        events.append("catalog")
        return catalog, enrichment, {"dry_run": {}}

    async def fake_source(_server_id, expected):
        assert expected == fingerprint
        return docs

    async def forbidden_frida(*_args, **_kwargs):
        raise AssertionError("incomplete enrichment must stop before FRIDA")

    monkeypatch.setattr(
        document_locks,
        "law_document_server_lock",
        fake_lock,
    )
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        fake_lock,
    )
    monkeypatch.setattr(phase_a, "preflight_server", fake_preflight)
    monkeypatch.setattr(
        phase_a,
        "_ensure_catalog_and_enrichment_locked",
        fake_catalog,
    )
    monkeypatch.setattr(phase_a, "_assert_source_snapshot", fake_source)
    monkeypatch.setattr(phase_a, "wait_for_frida_and_related", forbidden_frida)

    await phase_a.run(
        ["las_vegas"],
        store=store,
        options=phase_a.DriverOptions(allow_header_warnings=True),
    )

    entry = store.data["servers"]["las_vegas"]
    assert entry["status"] == "awaiting_enrichment"
    assert entry["details"]["header_warning_audit"] == audit
    assert "--allow-header-warnings" in (
        entry["details"]["next_step"]["resume_command"]
    )
    assert events == ["preflight", "catalog"]


def _accepted_smoke_evidence(server_id, fingerprint, challenge, *, now=None):
    from scripts import smoke_law_search_http as smoke

    fixed_tops = {
        "homicide": {"title": "Убийство сотрудника полиции"},
        "residential_theft": {"title": "Кража"},
        "illicit_weapon_purchase": {
            "title": "Незаконное приобретение оружия"
        },
        "drug_possession": {"title": "Хранение наркотиков"},
        "assault_on_employee": {"title": "Не связанная статья"},
        "obscene_language": {"title": "Не связанная статья"},
    }
    descriptors = smoke._slang_cases() + smoke.select_dynamic_cases(
        [
            {
                "id": article_id,
                "code": code,
                "title": f"Article {article_id}",
                "doc_title": f"Document {article_id}",
                "pair_count": 1,
                "code_count": 1,
            }
            for article_id, code in (
                (101, "1"),
                (102, "2"),
                (103, "3"),
                (104, "12.1"),
            )
        ]
    )
    cases = []
    for index, descriptor in enumerate(descriptors):
        top = (
            fixed_tops[descriptor["name"]]
            if descriptor["kind"] == "slang"
            else {
                "id": descriptor["expected"]["id"],
                "code": descriptor["expected"]["code"],
                "title": descriptor["expected"]["title"],
                "doc_title": descriptor["expected"]["doc_title"],
            }
        )
        cases.append(
            smoke.evaluate_case(
                descriptor,
                http_status=200,
                payload={
                    "results": [top],
                    "search_mode": "full",
                    "degraded": False,
                    "warnings": [],
                },
                elapsed_ms=1.0,
                index=index,
            )
        )
    return smoke.build_evidence(
        server_id=server_id,
        run_nonce=challenge["run_nonce"],
        base_url="http://127.0.0.1:8000",
        fingerprint_before=fingerprint,
        fingerprint_after=deepcopy(fingerprint),
        cases=cases,
        generated_at=(now or datetime.now(timezone.utc)).isoformat(),
    )


def test_smoke_producer_evidence_is_accepted_and_tampering_is_rejected():
    fingerprint = phase_a._source_fingerprint([_doc(1, LAW_TEXT)])
    now = datetime.now(timezone.utc)
    challenge = phase_a._new_smoke_challenge(now=now)
    evidence = _accepted_smoke_evidence(
        "las_vegas",
        fingerprint,
        challenge,
        now=now,
    )
    accepted = phase_a.validate_smoke_evidence(
        "las_vegas",
        fingerprint,
        challenge,
        evidence,
        now=now,
    )
    assert accepted["passed"] is True
    assert accepted["score"] == 8

    wrong_fingerprint = deepcopy(evidence)
    wrong_fingerprint["source_fingerprint"]["value"] = "tampered"
    with pytest.raises(phase_a.PhaseAError, match="different source"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            wrong_fingerprint,
            now=now,
        )

    tampered_case = deepcopy(evidence)
    tampered_case["cases"][0]["technical_full"] = False
    with pytest.raises(phase_a.PhaseAError, match="technical_full"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_case,
            now=now,
        )

    tampered_score = deepcopy(evidence)
    tampered_score["cases"][0]["point"] = 0
    with pytest.raises(phase_a.PhaseAError, match="recomputed"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_score,
            now=now,
        )

    duplicated_case = deepcopy(evidence)
    duplicated_case["cases"][1]["name"] = duplicated_case["cases"][0]["name"]
    with pytest.raises(phase_a.PhaseAError, match="contract"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            duplicated_case,
            now=now,
        )

    tampered_kind = deepcopy(evidence)
    tampered_kind["cases"][0]["kind"] = "exact_code_doc"
    with pytest.raises(phase_a.PhaseAError, match="kind"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_kind,
            now=now,
        )

    tampered_query = deepcopy(evidence)
    tampered_query["cases"][0]["query"] = "другой вопрос"
    with pytest.raises(phase_a.PhaseAError, match="fixed query"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_query,
            now=now,
        )

    tampered_top = deepcopy(evidence)
    tampered_top["cases"][0]["top"]["title"] = "Не связанная статья"
    with pytest.raises(phase_a.PhaseAError, match="recomputed"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_top,
            now=now,
        )

    tampered_dynamic_top = deepcopy(evidence)
    tampered_dynamic_top["cases"][6]["top"]["id"] = 999999
    with pytest.raises(phase_a.PhaseAError, match="recomputed"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_dynamic_top,
            now=now,
        )

    tampered_status = deepcopy(evidence)
    tampered_status["cases"][0]["status"] = "manual_review"
    tampered_status["cases"][0]["point"] = 0
    with pytest.raises(phase_a.PhaseAError, match="recomputed"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_status,
            now=now,
        )

    tampered_nonce = deepcopy(evidence)
    tampered_nonce["run_nonce"] = "b" * 64
    with pytest.raises(phase_a.PhaseAError, match="nonce"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            tampered_nonce,
            now=now,
        )

    reordered = deepcopy(evidence)
    reordered["cases"][0], reordered["cases"][1] = (
        reordered["cases"][1],
        reordered["cases"][0],
    )
    with pytest.raises(phase_a.PhaseAError, match="contract"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            reordered,
            now=now,
        )


def test_smoke_evidence_expires_with_awaiting_challenge():
    fingerprint = phase_a._source_fingerprint([_doc(1, LAW_TEXT)])
    now = datetime.now(timezone.utc)
    issued = now - timedelta(
        seconds=phase_a.LAW_SMOKE_MAX_AGE_SECONDS + 1
    )
    challenge = phase_a._new_smoke_challenge(now=issued)
    evidence = _accepted_smoke_evidence(
        "las_vegas",
        fingerprint,
        challenge,
        now=issued,
    )
    with pytest.raises(phase_a.PhaseAError, match="stale"):
        phase_a.validate_smoke_evidence(
            "las_vegas",
            fingerprint,
            challenge,
            evidence,
            now=now,
        )


@pytest.mark.asyncio
async def test_finalize_accepts_real_smoke_contract_then_rechecks_structure(
    monkeypatch,
    tmp_path,
):
    import services.law_document_lock as document_locks
    import services.law_embedding_service as embedding_service

    docs = [_doc(1, LAW_TEXT)]
    fingerprint = phase_a._source_fingerprint(docs)
    challenge = phase_a._new_smoke_challenge()
    evidence = _accepted_smoke_evidence(
        "las_vegas",
        fingerprint,
        challenge,
    )
    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_smoke",
        stage="awaiting_smoke",
        details={
            "source_fingerprint": fingerprint,
            "smoke_challenge": challenge,
        },
    )
    stale_store = phase_a.CheckpointStore(
        store.path,
        ["las_vegas"],
    )
    events = []

    @asynccontextmanager
    async def fake_document_lock(_server_id, **_kwargs):
        events.append("document_enter")
        yield
        events.append("document_exit")

    @asynccontextmanager
    async def fake_catalog_lock(_server_id):
        events.append("catalog_enter")
        yield
        events.append("catalog_exit")

    async def fake_assert_source(_server_id, expected):
        assert dict(expected) == fingerprint
        events.append("source")
        return docs

    async def fake_final(_server_id, _docs, *, expected_source_fingerprint):
        assert dict(expected_source_fingerprint) == fingerprint
        events.append("final")
        return {"source_fingerprint": fingerprint, "structural": "ok"}

    monkeypatch.setattr(
        document_locks,
        "law_document_server_lock",
        fake_document_lock,
    )
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        fake_catalog_lock,
    )
    monkeypatch.setattr(phase_a, "_assert_source_snapshot", fake_assert_source)
    monkeypatch.setattr(phase_a, "final_verify", fake_final)

    await phase_a.finalize_smoke(
        "las_vegas",
        store=store,
        evidence=evidence,
    )
    entry = store.data["servers"]["las_vegas"]
    assert entry["status"] == "complete"
    assert entry["details"]["smoke"]["score"] == 8
    # Legacy checkpoints created before this field existed are explicitly
    # treated as the safe default-false case.
    assert entry["details"]["allow_header_warnings"] is False
    assert entry["details"]["header_warning_audit"] is None
    assert "run_nonce" not in entry["details"]["smoke_challenge"]
    assert len(entry["details"]["smoke_challenge"]["nonce_sha256"]) == 64
    assert events == [
        "document_enter",
        "source",
        "catalog_enter",
        "final",
        "catalog_exit",
        "document_exit",
    ]
    with pytest.raises(phase_a.PhaseAError, match="not awaiting_smoke"):
        await phase_a.finalize_smoke(
            "las_vegas",
            store=stale_store,
            evidence=evidence,
        )
    assert stale_store.data["servers"]["las_vegas"]["status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_allow",
    ["false", 1],
    ids=["truthy-string", "truthy-integer"],
)
async def test_finalize_rejects_non_bool_header_warning_decision(
    tmp_path,
    malformed_allow,
):
    fingerprint = phase_a._source_fingerprint([_doc(1, LAW_TEXT)])
    challenge = phase_a._new_smoke_challenge()
    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_smoke",
        stage="awaiting_smoke",
        details={
            "source_fingerprint": fingerprint,
            "smoke_challenge": challenge,
            "allow_header_warnings": malformed_allow,
        },
    )
    checkpoint_before = store.path.read_bytes()

    with pytest.raises(
        phase_a.HeaderWarningAuditError,
        match="must be an exact bool",
    ):
        await phase_a.finalize_smoke(
            "las_vegas",
            store=store,
            evidence={},
        )

    assert store.path.read_bytes() == checkpoint_before
    assert store.data["servers"]["las_vegas"]["status"] == "awaiting_smoke"


@pytest.mark.asyncio
async def test_finalize_revalidates_freshness_at_locked_commit_point(
    monkeypatch,
    tmp_path,
):
    import services.law_document_lock as document_locks
    import services.law_embedding_service as embedding_service

    docs = [_doc(1, LAW_TEXT)]
    fingerprint = phase_a._source_fingerprint(docs)
    challenge = phase_a._new_smoke_challenge()
    evidence = _accepted_smoke_evidence(
        "las_vegas",
        fingerprint,
        challenge,
    )
    store = phase_a.CheckpointStore(
        tmp_path / "checkpoint.json",
        ["las_vegas"],
    )
    store.mark(
        "las_vegas",
        status="awaiting_smoke",
        stage="awaiting_smoke",
        details={
            "source_fingerprint": fingerprint,
            "smoke_challenge": challenge,
        },
    )
    events = []

    @asynccontextmanager
    async def fake_document_lock(_server_id, **_kwargs):
        events.append("document_enter")
        try:
            yield
        finally:
            events.append("document_exit")

    @asynccontextmanager
    async def fake_catalog_lock(_server_id):
        events.append("catalog_enter")
        try:
            yield
        finally:
            events.append("catalog_exit")

    async def fake_assert_source(_server_id, expected):
        assert dict(expected) == fingerprint
        events.append("source")
        return docs

    async def fake_final(_server_id, _docs, *, expected_source_fingerprint):
        assert dict(expected_source_fingerprint) == fingerprint
        events.append("final")
        return {"source_fingerprint": fingerprint, "structural": "ok"}

    original_validate = phase_a.validate_smoke_evidence
    validate_calls = 0

    def expiring_validate(*args, **kwargs):
        nonlocal validate_calls
        validate_calls += 1
        events.append(f"validate_{validate_calls}")
        if validate_calls == 2:
            raise phase_a.PhaseAError("smoke evidence/challenge is stale")
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(
        document_locks,
        "law_document_server_lock",
        fake_document_lock,
    )
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        fake_catalog_lock,
    )
    monkeypatch.setattr(phase_a, "_assert_source_snapshot", fake_assert_source)
    monkeypatch.setattr(phase_a, "final_verify", fake_final)
    monkeypatch.setattr(
        phase_a,
        "validate_smoke_evidence",
        expiring_validate,
    )

    with pytest.raises(phase_a.PhaseAError, match="stale"):
        await phase_a.finalize_smoke(
            "las_vegas",
            store=store,
            evidence=evidence,
        )

    assert store.data["servers"]["las_vegas"]["status"] == "awaiting_smoke"
    assert events == [
        "validate_1",
        "document_enter",
        "source",
        "catalog_enter",
        "final",
        "validate_2",
        "catalog_exit",
        "document_exit",
    ]


@pytest.mark.asyncio
async def test_write_main_reloads_stale_checkpoint_inside_global_lock(
    monkeypatch,
    tmp_path,
):
    import services.law_backfill_lock as global_locks

    checkpoint_path = tmp_path / "checkpoint.json"
    seed = phase_a.CheckpointStore(checkpoint_path, ["las_vegas"])
    seed.mark(
        "las_vegas",
        status="awaiting_smoke",
        stage="awaiting_smoke",
    )
    events = []

    class FakeDb:
        async def connect(self):
            events.append("connect")

        async def close(self):
            events.append("close")

    @asynccontextmanager
    async def fake_global_lock(*, manager):
        assert manager is fake_db
        events.append("global_enter")
        concurrent = phase_a.CheckpointStore(
            checkpoint_path,
            ["las_vegas"],
        )
        concurrent.mark(
            "las_vegas",
            status="complete",
            stage="complete",
        )
        try:
            yield
        finally:
            events.append("global_exit")

    async def fake_run(_targets, *, store, options):
        assert options.preflight_only is False
        status = store.data["servers"]["las_vegas"]["status"]
        events.append(f"run:{status}")

    fake_db = FakeDb()
    monkeypatch.setattr(phase_a, "db_manager", fake_db)
    monkeypatch.setattr(
        global_locks,
        "law_backfill_global_lock",
        fake_global_lock,
    )
    monkeypatch.setattr(phase_a, "run", fake_run)

    args = phase_a.build_parser().parse_args(
        [
            "las_vegas",
            "--checkpoint",
            str(checkpoint_path),
        ]
    )
    await phase_a._async_main(args)

    assert events == [
        "connect",
        "global_enter",
        "run:complete",
        "global_exit",
        "close",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode_args",
    [
        ["--reset-checkpoint"],
        ["--preflight-only", "--reset-checkpoint"],
    ],
    ids=["write", "preflight"],
)
async def test_busy_write_main_cannot_reset_active_checkpoint(
    monkeypatch,
    tmp_path,
    mode_args,
):
    import services.law_backfill_lock as global_locks

    checkpoint_path = tmp_path / "checkpoint.json"
    active = phase_a.CheckpointStore(checkpoint_path, ["las_vegas"])
    active.mark(
        "las_vegas",
        status="in_progress",
        stage="rag",
        details={"sentinel": "active-run"},
    )
    events = []

    class FakeDb:
        async def connect(self):
            events.append("connect")

        async def close(self):
            events.append("close")

    @asynccontextmanager
    async def fake_busy_global_lock(*, manager):
        assert manager is fake_db
        events.append("global_busy")
        raise global_locks.LawBackfillLockBusy("already running")
        yield  # pragma: no cover

    async def forbidden_run(*_args, **_kwargs):
        raise AssertionError("busy write run must not reach the driver")

    fake_db = FakeDb()
    monkeypatch.setattr(phase_a, "db_manager", fake_db)
    monkeypatch.setattr(
        global_locks,
        "law_backfill_global_lock",
        fake_busy_global_lock,
    )
    monkeypatch.setattr(phase_a, "run", forbidden_run)

    args = phase_a.build_parser().parse_args(
        [
            "las_vegas",
            "--checkpoint",
            str(checkpoint_path),
            *mode_args,
        ]
    )
    with pytest.raises(global_locks.LawBackfillLockBusy):
        await phase_a._async_main(args)

    reloaded = phase_a.CheckpointStore(
        checkpoint_path,
        ["las_vegas"],
    )
    entry = reloaded.data["servers"]["las_vegas"]
    assert entry["status"] == "in_progress"
    assert entry["details"] == {"sentinel": "active-run"}
    assert events == ["connect", "global_busy", "close"]


def test_cli_defaults_to_three_hour_wait_and_explicit_header_override():
    args = phase_a.build_parser().parse_args(
        ["las_vegas", "--allow-header-warnings"]
    )
    assert args.frida_timeout == 10_800.0
    assert args.allow_header_warnings is True
    assert not hasattr(args, "allow_vertex_enrichment")
    assert not hasattr(args, "enrich_retries")
    assert not hasattr(args, "enrich_retry_delay")
    for forbidden_flag in (
        "--allow-vertex-enrichment",
        "--enrich-retries",
        "--enrich-retry-delay",
    ):
        with pytest.raises(SystemExit):
            phase_a.build_parser().parse_args(
                ["las_vegas", forbidden_flag]
            )
    assert (
        phase_a.default_checkpoint_path(["las_vegas"]).name
        == "law_phase_a_las_vegas_checkpoint.json"
    )
