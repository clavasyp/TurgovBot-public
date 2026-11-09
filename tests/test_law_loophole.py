# START OF FILE tests/test_law_loophole.py
"""
Unit-тесты для тестового анализатора дыр законки.

Покрывают:
- Source Selection Policy (4 режима);
- Model Policy;
- парсинг JSON-ответов модели;
- валидацию находок;
- дедупликацию;
- форматирование отчета.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

import config
from services.law_loophole.bundle import (
    LawContextBundle,
    LawContextDocument,
    build_law_context_bundle,
)
from services.law_loophole.model_policy import (
    choose_model_profile,
    get_loophole_models,
    reconcile_bundle_profile,
)
from services.law_loophole.analyzer import (
    parse_json_response,
    validate_findings,
    _dedup_findings,
    build_summary,
    analyze_loopholes,
    LoopholeModelUnavailable,
    _extract_all_norms,
    format_norms_context,
    _run_goal_directed_roles,
    format_role_plans,
    _ROLE_GOALS,
    _build_exploit_chains,
    _red_team_findings,
    _llm_synthesize,
)
from services.law_loophole.report import format_loophole_report
from services.law_loophole.metrics import (
    chain_rate,
    grounding_rate,
    summarize_report_metrics,
    compare_reports,
)


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def _doc(law_id: int, server_id: str = "srv1", title: str = None, size: int = 1000) -> LawContextDocument:
    return LawContextDocument(
        id=law_id,
        server_id=server_id,
        title=title or f"Закон {law_id}",
        source_url=None,
        gemini_file_uri=f"gs://bucket/doc{law_id}.txt",
        content_hash=None,
        last_updated=None,
        estimated_size=size,
    )


class _FakeDB:
    """Имитация db_manager.fetch для bundle."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, query, *args, **kwargs):
        # Игнорируем сам SQL — фильтруем по server_id.
        server_id = args[0] if args else None
        return [r for r in self._rows if r["server_id"] == server_id]


_MISSING = object()


def _row(law_id, server_id="srv1", title=None, uri=_MISSING, content="x" * 500):
    return {
        "id": law_id,
        "server_id": server_id,
        "title": title or f"Закон {law_id}",
        "source_url": None,
        "gemini_file_uri": f"gs://b/d{law_id}.txt" if uri is _MISSING else uri,
        "content_hash": None,
        "last_updated": None,
        "content_len": len(content),
    }


# ---------------------------------------------------------------------------
# Source Selection Policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_server_returns_all_docs_with_uri():
    rows = [
        _row(1), _row(2), _row(3),
        _row(4, uri=None),  # должен быть отфильтрован SQL-запросом
    ]
    # Имитируем поведение SQL "WHERE gemini_file_uri IS NOT NULL".
    fake = _FakeDB([r for r in rows if r["gemini_file_uri"]])
    with patch("services.law_loophole.bundle.db_manager", fake):
        bundle = await build_law_context_bundle(
            server_id="srv1",
            project_id=None,
            selection_mode="full_server",
            model_profile="balanced",
        )
    assert bundle.selection_mode == "full_server"
    assert {d.id for d in bundle.selected_laws} == {1, 2, 3}
    assert all(d.selection_source == "full_server" for d in bundle.selected_laws)


@pytest.mark.asyncio
async def test_empty_db_raises_value_error():
    fake = _FakeDB([])
    with patch("services.law_loophole.bundle.db_manager", fake):
        with pytest.raises(ValueError):
            await build_law_context_bundle(
                server_id="srv1",
                project_id=None,
                selection_mode="full_server",
            )


@pytest.mark.asyncio
async def test_manual_select_rejects_other_server_doc():
    rows = [_row(1, server_id="srv1"), _row(2, server_id="srv2")]
    fake = _FakeDB(rows)
    with patch("services.law_loophole.bundle.db_manager", fake):
        with pytest.raises(ValueError):
            await build_law_context_bundle(
                server_id="srv1",
                project_id=None,
                selection_mode="manual_select",
                manual_law_ids=[1, 2],
            )


@pytest.mark.asyncio
async def test_manual_select_enforces_doc_limit():
    rows = [_row(i) for i in range(1, 10)]
    fake = _FakeDB(rows)
    with patch("services.law_loophole.bundle.db_manager", fake):
        with pytest.raises(ValueError):
            await build_law_context_bundle(
                server_id="srv1",
                project_id=None,
                selection_mode="manual_select",
                model_profile="fast",
                manual_law_ids=list(range(1, 9)),  # 8 > FAST limit (5)
            )


@pytest.mark.asyncio
async def test_manual_select_empty_rejected():
    rows = [_row(1)]
    fake = _FakeDB(rows)
    with patch("services.law_loophole.bundle.db_manager", fake):
        with pytest.raises(ValueError):
            await build_law_context_bundle(
                server_id="srv1",
                project_id=None,
                selection_mode="manual_select",
                manual_law_ids=[],
            )


@pytest.mark.asyncio
async def test_auto_rag_router_filters_to_selected():
    rows = [_row(1), _row(2), _row(3)]
    fake = _FakeDB(rows)

    async def router_fn(user_request, idx_pairs):
        # Выбираем индексы 1 и 3
        return {1, 3}

    with patch("services.law_loophole.bundle.db_manager", fake):
        bundle = await build_law_context_bundle(
            server_id="srv1",
            project_id=None,
            selection_mode="auto_rag",
            user_request="что-то",
            router_fn=router_fn,
        )
    assert {d.id for d in bundle.selected_laws} == {1, 3}
    assert all(d.selection_source == "rag" for d in bundle.selected_laws)


@pytest.mark.asyncio
async def test_auto_rag_empty_uses_fallback():
    rows = [_row(i) for i in range(1, 4)]
    fake = _FakeDB(rows)

    async def router_fn(user_request, idx_pairs):
        return set()

    with patch("services.law_loophole.bundle.db_manager", fake):
        bundle = await build_law_context_bundle(
            server_id="srv1",
            project_id=None,
            selection_mode="auto_rag",
            user_request="x",
            router_fn=router_fn,
        )
    assert len(bundle.selected_laws) > 0
    assert all(d.selection_source == "fallback" for d in bundle.selected_laws)


@pytest.mark.asyncio
async def test_hybrid_keeps_manual_and_adds_rag_no_dups():
    rows = [_row(1), _row(2), _row(3), _row(4)]
    fake = _FakeDB(rows)

    async def router_fn(user_request, idx_pairs):
        # Возвращаем индексы 1 (=id 1, уже выбран) и 3 (=id 3)
        return {1, 3}

    with patch("services.law_loophole.bundle.db_manager", fake):
        bundle = await build_law_context_bundle(
            server_id="srv1",
            project_id=None,
            selection_mode="hybrid",
            manual_law_ids=[1, 2],
            user_request="x",
            router_fn=router_fn,
        )
    ids = [d.id for d in bundle.selected_laws]
    # 1, 2 — manual; 3 — добавлен RAG; 1 не дублируется.
    assert ids.count(1) == 1
    assert set(ids) == {1, 2, 3}
    sources = {d.id: d.selection_source for d in bundle.selected_laws}
    assert sources[1] == "manual"
    assert sources[2] == "manual"
    assert sources[3] == "rag"


@pytest.mark.asyncio
async def test_hybrid_works_when_router_adds_nothing():
    rows = [_row(1), _row(2)]
    fake = _FakeDB(rows)

    async def router_fn(user_request, idx_pairs):
        return set()

    with patch("services.law_loophole.bundle.db_manager", fake):
        bundle = await build_law_context_bundle(
            server_id="srv1",
            project_id=None,
            selection_mode="hybrid",
            manual_law_ids=[1, 2],
            user_request="x",
            router_fn=router_fn,
        )
    assert {d.id for d in bundle.selected_laws} == {1, 2}


@pytest.mark.asyncio
async def test_unknown_selection_mode_raises():
    fake = _FakeDB([_row(1)])
    with patch("services.law_loophole.bundle.db_manager", fake):
        with pytest.raises(ValueError):
            await build_law_context_bundle(
                server_id="srv1",
                project_id=None,
                selection_mode="bogus",  # type: ignore[arg-type]
            )


@pytest.mark.asyncio
async def test_full_server_huge_context_requires_batching():
    big_content = "x" * (config.LOOPHOLE_BATCH_THRESHOLD_CHARS // 5 + 100)
    rows = [_row(i, content=big_content) for i in range(1, 8)]
    fake = _FakeDB(rows)
    with patch("services.law_loophole.bundle.db_manager", fake):
        bundle = await build_law_context_bundle(
            server_id="srv1",
            project_id=None,
            selection_mode="full_server",
            model_profile="balanced",
        )
    assert bundle.requires_batching is True


# ---------------------------------------------------------------------------
# Model Policy
# ---------------------------------------------------------------------------


def test_get_loophole_models_profiles():
    fast = get_loophole_models("fast")
    assert fast.batch_mode is False
    assert fast.analyzer_model == config.LOOPHOLE_ANALYZER_MODEL_FAST

    deep = get_loophole_models("deep")
    assert deep.analyzer_model == config.LOOPHOLE_ANALYZER_MODEL_DEEP

    expert = get_loophole_models("expert")
    assert expert.batch_mode is True


def test_get_loophole_models_unknown_raises():
    with pytest.raises(ValueError):
        get_loophole_models("alien")  # type: ignore[arg-type]


def test_choose_profile_fast_with_too_many_docs_upgrades():
    profile = choose_model_profile(
        requested_profile="fast",
        selection_mode="manual_select",
        estimated_size=1000,
        doc_count=config.LOOPHOLE_MAX_MANUAL_DOCS_FAST + 1,
    )
    assert profile == "balanced"


def test_choose_profile_full_server_large_upgrades_to_deep():
    profile = choose_model_profile(
        requested_profile="balanced",
        selection_mode="full_server",
        estimated_size=config.LOOPHOLE_BATCH_THRESHOLD_CHARS + 1,
        doc_count=config.LOOPHOLE_MAX_MANUAL_DOCS_BALANCED + 5,
    )
    assert profile in ("deep", "expert")


def test_choose_profile_huge_context_forces_expert():
    profile = choose_model_profile(
        requested_profile="balanced",
        selection_mode="full_server",
        estimated_size=config.LOOPHOLE_BATCH_THRESHOLD_CHARS * 5,
        doc_count=50,
    )
    assert profile == "expert"


def test_reconcile_bundle_profile_mutates():
    b = LawContextBundle(
        server_id="srv1",
        project_id=None,
        selection_mode="manual_select",
        model_profile="fast",
    )
    b.selected_laws = [_doc(i, size=100) for i in range(1, 12)]
    b.estimated_size = sum(d.estimated_size for d in b.selected_laws)
    reconcile_bundle_profile(b)
    assert b.model_profile == "balanced"  # 11 docs > FAST limit


# ---------------------------------------------------------------------------
# Analyzer: JSON parsing & validation
# ---------------------------------------------------------------------------


def test_parse_json_clean():
    obj = parse_json_response('{"a": 1}')
    assert obj == {"a": 1}


def test_parse_json_with_markdown_wrapper():
    obj = parse_json_response('```json\n{"a": 1}\n```')
    assert obj == {"a": 1}


def test_parse_json_with_extra_text():
    text = 'Here is the result:\n{"a": 1, "b": [2, 3]}\nThat\'s all.'
    obj = parse_json_response(text)
    assert obj == {"a": 1, "b": [2, 3]}


def test_parse_json_invalid_returns_none():
    assert parse_json_response("not a json at all") is None
    assert parse_json_response("") is None


def test_validate_findings_drops_no_source():
    raw = [
        {
            "title": "Bad",
            "vulnerability_type": "vague_term",
            "source_documents": [],
            "source_fragments": [],
        },
        {
            "title": "Good",
            "vulnerability_type": "vague_term",
            "source_documents": ["Закон 1"],
            "source_fragments": ["цитата"],
            "severity": "high",
            "confidence": "medium",
        },
    ]
    out = validate_findings(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Good"


def test_validate_findings_normalizes_severity_and_type():
    raw = [{
        "title": "X",
        "vulnerability_type": "BoGus_TyPe",
        "source_documents": ["d"],
        "source_fragments": ["f"],
        "severity": "extreme",
        "confidence": "yes",
    }]
    out = validate_findings(raw)
    assert out[0]["vulnerability_type"] == "other"
    assert out[0]["severity"] == "low"
    assert out[0]["confidence"] == "low"


def test_validate_findings_filters_unknown_titles():
    raw = [{
        "title": "X",
        "vulnerability_type": "vague_term",
        "source_documents": ["NOT_IN_BUNDLE"],
        "source_fragments": ["f"],
    }]
    out = validate_findings(raw, allowed_titles={"Real Doc"})
    assert out == []


def test_dedup_findings_merges_lists_and_keeps_higher_severity():
    findings = [
        {
            "title": "Дубль", "vulnerability_type": "vague_term",
            "source_documents": ["A"], "source_fragments": ["fa"],
            "severity": "low", "confidence": "low",
            "issue_summary": "", "abuse_scenarios": ["s1"],
            "affected_roles": [], "gameplay_impact": "", "fix_recommendation": "",
        },
        {
            "title": "Дубль", "vulnerability_type": "vague_term",
            "source_documents": ["B"], "source_fragments": ["fb"],
            "severity": "high", "confidence": "medium",
            "issue_summary": "", "abuse_scenarios": ["s2"],
            "affected_roles": [], "gameplay_impact": "", "fix_recommendation": "",
        },
    ]
    out = _dedup_findings(findings)
    assert len(out) == 1
    assert out[0]["severity"] == "high"
    assert set(out[0]["source_documents"]) == {"A", "B"}
    assert set(out[0]["abuse_scenarios"]) == {"s1", "s2"}


def test_build_summary_counts_severities():
    findings = [
        {"title": "x", "severity": "critical"},
        {"title": "y", "severity": "high"},
        {"title": "z", "severity": "high"},
        {"title": "w", "severity": "low"},
    ]
    s = build_summary(findings, coverage_note="test")
    assert s["total_findings"] == 4
    assert s["critical_count"] == 1
    assert s["high_count"] == 2
    assert s["low_count"] == 1
    assert s["coverage_note"] == "test"


# ---------------------------------------------------------------------------
# Analyzer: end-to-end with mocked model_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analyze_loopholes_with_mocked_model_call():
    bundle = LawContextBundle(
        server_id="srv1",
        project_id=None,
        selection_mode="manual_select",
        model_profile="balanced",
    )
    bundle.selected_laws = [_doc(1, title="Закон 1"), _doc(2, title="Закон 2")]

    # 2 single-doc passes + 1 cross-doc pass = 3 calls
    responses = [
        json.dumps({
            "document_title": "Закон 1",
            "findings": [{
                "title": "Размытое определение",
                "vulnerability_type": "vague_term",
                "source_documents": ["Закон 1"],
                "source_fragments": ["цитата 1"],
                "issue_summary": "x",
                "abuse_scenarios": ["a1"],
                "affected_roles": [],
                "gameplay_impact": "",
                "severity": "high",
                "confidence": "high",
                "fix_recommendation": "fix1",
            }],
        }),
        json.dumps({
            "document_title": "Закон 2",
            "findings": [{
                "title": "Нет санкции",
                "vulnerability_type": "missing_sanction",
                "source_documents": ["Закон 2"],
                "source_fragments": ["цитата 2"],
                "severity": "medium",
                "confidence": "medium",
            }],
        }),
        json.dumps({
            "findings": [{
                "title": "Конфликт",
                "vulnerability_type": "direct_contradiction",
                "source_documents": ["Закон 1", "Закон 2"],
                "source_fragments": ["A", "B"],
                "severity": "critical",
                "confidence": "high",
            }],
        }),
    ]
    call_index = [0]

    async def fake_call(**kwargs):
        i = call_index[0]
        call_index[0] += 1
        return responses[i] if i < len(responses) else "{}"

    models = get_loophole_models("balanced")
    report = await analyze_loopholes(
        bundle=bundle,
        analysis_type="quick_scan",
        models=models,
        model_call=fake_call,
    )
    titles = [f["title"] for f in report["findings"]]
    assert "Конфликт" in titles
    assert "Размытое определение" in titles
    # critical должен быть первым
    assert report["findings"][0]["severity"] == "critical"
    assert report["summary"]["critical_count"] == 1


@pytest.mark.asyncio
async def test_analyze_loopholes_invalid_json_does_not_crash():
    bundle = LawContextBundle(
        server_id="srv1", project_id=None,
        selection_mode="manual_select", model_profile="fast",
    )
    bundle.selected_laws = [_doc(1)]

    async def fake_call(**kwargs):
        return "not a json at all"

    models = get_loophole_models("fast")
    report = await analyze_loopholes(
        bundle=bundle, analysis_type="quick_scan",
        models=models, model_call=fake_call,
    )
    assert report["findings"] == []
    assert report["summary"]["total_findings"] == 0


@pytest.mark.asyncio
async def test_analyze_loopholes_quota_error_raises_unavailable():
    """429-сбой модельного вызова НЕ должен превращаться в пустой отчёт «0 дыр» —
    он сигналит LoopholeModelUnavailable (очередь молча повторит задачу)."""
    bundle = LawContextBundle(
        server_id="srv1", project_id=None,
        selection_mode="manual_select", model_profile="fast",
    )
    bundle.selected_laws = [_doc(1), _doc(2)]

    async def fake_call(**kwargs):
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    models = get_loophole_models("fast")
    with pytest.raises(LoopholeModelUnavailable):
        await analyze_loopholes(
            bundle=bundle, analysis_type="quick_scan",
            models=models, model_call=fake_call,
        )


@pytest.mark.asyncio
async def test_analyze_loopholes_nonquota_error_propagates():
    """Не-квотный сбой вызова — настоящая ошибка пайплайна, пробрасывается как есть
    (а не LoopholeModelUnavailable и не тихий пустой отчёт)."""
    bundle = LawContextBundle(
        server_id="srv1", project_id=None,
        selection_mode="manual_select", model_profile="fast",
    )
    bundle.selected_laws = [_doc(1)]

    async def fake_call(**kwargs):
        raise ValueError("boom in pipeline")

    models = get_loophole_models("fast")
    with pytest.raises(ValueError):
        await analyze_loopholes(
            bundle=bundle, analysis_type="quick_scan",
            models=models, model_call=fake_call,
        )


@pytest.mark.asyncio
async def test_analyze_loopholes_empty_bundle_raises():
    bundle = LawContextBundle(
        server_id="srv1", project_id=None,
        selection_mode="manual_select", model_profile="fast",
    )
    bundle.selected_laws = []

    async def fake_call(**kwargs):
        return "{}"

    with pytest.raises(ValueError):
        await analyze_loopholes(
            bundle=bundle, analysis_type="quick_scan",
            models=get_loophole_models("fast"), model_call=fake_call,
        )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def test_format_loophole_report_no_findings():
    report = {
        "summary": build_summary([], coverage_note="m"),
        "findings": [],
        "discarded_findings": [],
    }
    msgs = format_loophole_report(report)
    assert len(msgs) == 1
    assert "не найдено" in msgs[0].lower() or "нет" in msgs[0].lower() or "0" in msgs[0]


def test_format_loophole_report_renders_findings_in_html():
    findings = [{
        "title": "Размытый термин",
        "vulnerability_type": "vague_term",
        "source_documents": ["Закон 1"],
        "source_fragments": ["цитата"],
        "issue_summary": "что-то размытое",
        "abuse_scenarios": ["сценарий A"],
        "affected_roles": ["игрок"],
        "gameplay_impact": "плохо",
        "severity": "high",
        "confidence": "medium",
        "fix_recommendation": "уточнить термин",
    }]
    report = {
        "summary": build_summary(findings, coverage_note="cov"),
        "findings": findings,
        "discarded_findings": [],
    }
    msgs = format_loophole_report(report)
    full = "\n".join(msgs)
    assert "<b>Размытый термин</b>" in full
    assert "Закон 1" in full
    assert "цитата" in full
    assert "уточнить термин" in full


def test_format_loophole_report_escapes_html():
    findings = [{
        "title": "<script>alert(1)</script>",
        "vulnerability_type": "other",
        "source_documents": ["Doc"],
        "source_fragments": ["fragment"],
        "issue_summary": "<b>boom</b>",
        "abuse_scenarios": [],
        "affected_roles": [],
        "gameplay_impact": "",
        "severity": "low",
        "confidence": "low",
        "fix_recommendation": "",
    }]
    report = {
        "summary": build_summary(findings),
        "findings": findings,
        "discarded_findings": [],
    }
    msgs = format_loophole_report(report)
    full = "\n".join(msgs)
    assert "<script>" not in full
    assert "&lt;script&gt;" in full


# ---------------------------------------------------------------------------
# v2 pipeline: S1 norm-extraction / S2 roles / S3 chains / S4 red-team / S5 synth
# ---------------------------------------------------------------------------


def _bundle(profile: str = "balanced", n: int = 2) -> LawContextBundle:
    return LawContextBundle(
        server_id="srv1",
        project_id=None,
        selection_mode="manual_select",
        selected_laws=[_doc(i) for i in range(1, n + 1)],
        model_profile=profile,
    )


@pytest.mark.asyncio
async def test_s1_extract_norms_and_format():
    norm_json = json.dumps({
        "document_title": "X",
        "norms": [{"norm_id": "n1", "norm_type": "procedure",
                   "source_fragment": "задержание до предъявления",
                   "handles": ["нет предельного срока", "термин не определён"]}],
        "missing_structure_notes": ["нет порядка обжалования"],
    })

    async def fake(**kw):
        return norm_json

    res = await _extract_all_norms(bundle=_bundle(n=2), extraction_model="m",
                                   model_call=fake, thinking_level="medium")
    assert len(res) == 2
    ctx = format_norms_context(res)
    assert "ручки" in ctx and "задержание" in ctx and "пробелы структуры" in ctx


@pytest.mark.asyncio
async def test_s2_goal_roles_runs_all_goals_and_empty_guard():
    plan_json = json.dumps({"actor": "a", "goal": "g",
                            "plan": [{"step": 1, "intent": "i", "norm_used": "n",
                                      "source_documents": ["Закон 1"], "source_fragments": ["f"],
                                      "why_it_works": "w", "weak_link": ""}],
                            "reversibility": "high", "survives_appeal": "yes", "overall_stability": "high"})

    async def fake(**kw):
        return plan_json

    plans = await _run_goal_directed_roles(norms_context="## L\n- [proc] x", role_model="m",
                                           model_call=fake, thinking_level="high")
    assert len(plans) == len(_ROLE_GOALS)
    assert json.loads(format_role_plans(plans))  # валидный JSON
    assert await _run_goal_directed_roles(norms_context="", role_model="m", model_call=fake) == []


@pytest.mark.asyncio
async def test_s3_build_exploit_chains():
    chains_json = json.dumps({"chains": [{"title": "Цепочка", "vulnerability_type": "exploit_chain",
                                          "source_documents": ["Закон 1", "Закон 2"],
                                          "source_fragments": ["a", "b"],
                                          "abuse_scenarios": ["s1", "s2"],
                                          "severity": "critical", "confidence": "high"}],
                              "discarded": []})

    async def fake(**kw):
        return chains_json

    chains = await _build_exploit_chains(role_plans_json="[{}]", norms_context="ctx",
                                         chain_model="m", model_call=fake, thinking_level="high")
    assert len(chains) == 1 and chains[0]["vulnerability_type"] == "exploit_chain"
    assert await _build_exploit_chains(role_plans_json="", norms_context="c",
                                       chain_model="m", model_call=fake) == []


@pytest.mark.asyncio
async def test_s4_red_team_drops_and_adjusts():
    findings = [
        {"title": "A", "vulnerability_type": "exploit_chain", "severity": "critical", "confidence": "high",
         "source_documents": ["Закон 1"], "source_fragments": ["x"]},
        {"title": "B", "vulnerability_type": "regulatory_gap", "severity": "high", "confidence": "medium",
         "source_documents": ["Закон 1"], "source_fragments": ["y"]},
    ]
    verdicts = json.dumps({"verdicts": [
        {"title": "A", "holds_up": "yes", "adjusted_severity": "high", "adjusted_confidence": "high", "keep": True},
        {"title": "B", "holds_up": "no", "keep": False},
    ]})

    async def fake(**kw):
        return verdicts

    out = await _red_team_findings(findings=findings, redteam_model="m", model_call=fake, thinking_level="high")
    titles = {f["title"]: f for f in out}
    assert "B" not in titles and titles["A"]["severity"] == "high"

    async def bad(**kw):
        return "not json"

    same = await _red_team_findings(findings=findings, redteam_model="m", model_call=bad)
    assert len(same) == 2  # непарсимый вердикт -> вход без изменений


@pytest.mark.asyncio
async def test_s5_llm_synthesize_merges_and_failsafe():
    syn = json.dumps({"summary": {}, "findings": [
        {"title": "Объединённая схема", "vulnerability_type": "exploit_chain",
         "source_documents": ["Закон 1"], "source_fragments": ["x"], "abuse_scenarios": ["s"],
         "severity": "critical", "confidence": "high"}],
        "discarded_findings": [{"title": "Мелочь", "reason": "склеено"}]})

    async def fake(**kw):
        return syn

    res = await _llm_synthesize(findings=[{"title": "Мелочь", "vulnerability_type": "vague_term",
                                           "source_documents": ["Закон 1"], "source_fragments": ["a"],
                                           "severity": "low", "confidence": "low"}],
                                analysis_type="quick_scan", synthesis_model="m",
                                model_call=fake, allowed_titles={"Закон 1"})
    assert res is not None
    assert [f["title"] for f in res["findings"]] == ["Объединённая схема"]
    assert any(d["title"] == "Мелочь" for d in res["discarded"])

    async def bad(**kw):
        return "broken"

    assert await _llm_synthesize(findings=[{"title": "x"}], analysis_type="quick_scan",
                                 synthesis_model="m", model_call=bad) is None


def _full_pipeline_fake():
    seen = {"extraction": 0, "synth": 0}

    async def fake(**kw):
        tp = kw["text_prompt"]
        if "norm_type" in tp and "missing_structure_notes" in tp:
            seen["extraction"] += 1
            return json.dumps({"document_title": "X", "norms": [
                {"norm_id": "n", "norm_type": "procedure", "source_fragment": "f", "handles": ["h"]}]})
        if "Твоя цель прямо сейчас" in tp:
            return json.dumps({"actor": "a", "goal": "g", "plan": [
                {"step": 1, "intent": "i", "norm_used": "n", "source_documents": ["Закон 1"],
                 "source_fragments": ["f"], "why_it_works": "w", "weak_link": ""}],
                "reversibility": "high", "survives_appeal": "yes", "overall_stability": "high"})
        if "сборщик абуз-цепочек" in tp:
            return json.dumps({"chains": [], "discarded": []})
        if "жёсткого оппонента" in tp:
            return json.dumps({"verdicts": []})
        if "сводишь сырые находки" in tp:
            seen["synth"] += 1
            return json.dumps({"summary": {}, "findings": [
                {"title": "Схема", "vulnerability_type": "exploit_chain", "source_documents": ["Закон 1"],
                 "source_fragments": ["x"], "abuse_scenarios": ["s"], "severity": "high", "confidence": "high"}],
                "discarded_findings": []})
        if "СТЫКЕ" in tp:
            return json.dumps({"findings": []})
        return json.dumps({"document_title": "X", "findings": [
            {"title": "Мелочь", "vulnerability_type": "vague_term", "source_documents": ["Закон 1"],
             "source_fragments": ["a"], "severity": "low", "confidence": "low"}]})

    return fake, seen


@pytest.mark.asyncio
async def test_full_pipeline_gated_by_profile(monkeypatch):
    monkeypatch.setattr(config, "LOOPHOLE_FULL_PIPELINE_PROFILES", ("deep", "expert"))

    fake_d, seen_d = _full_pipeline_fake()
    await analyze_loopholes(bundle=_bundle("deep"), analysis_type="quick_scan",
                            models=get_loophole_models("deep"), model_call=fake_d)
    assert seen_d["extraction"] == 2 and seen_d["synth"] == 1  # deep -> полный конвейер

    fake_b, seen_b = _full_pipeline_fake()
    await analyze_loopholes(bundle=_bundle("balanced"), analysis_type="quick_scan",
                            models=get_loophole_models("balanced"), model_call=fake_b)
    assert seen_b["extraction"] == 0 and seen_b["synth"] == 0  # balanced -> лёгкий (CORE)


def test_exploit_chain_type_valid_and_rendered():
    from services.law_loophole.report import _format_finding
    f = {"title": "Цепочка", "vulnerability_type": "exploit_chain",
         "source_documents": ["Закон 1"], "source_fragments": ["frag"],
         "abuse_scenarios": ["шаг1", "шаг2", "шаг3", "шаг4", "шаг5"],
         "severity": "critical", "confidence": "high"}
    v = validate_findings([f], allowed_titles={"Закон 1"})
    assert v[0]["vulnerability_type"] == "exploit_chain"
    r = _format_finding(v[0])
    assert "эксплойт-цепочка" in r and r.count("• шаг") == 5


# ---------------------------------------------------------------------------
# Метрики качества (golden-харнесс)
# ---------------------------------------------------------------------------


def test_metrics_chain_and_grounding():
    findings = [
        {"source_documents": ["A", "B"], "abuse_scenarios": ["s1", "s2", "s3"],
         "source_fragments": ["задержание до предъявления"]},
        {"source_documents": ["A"], "abuse_scenarios": ["s1"],
         "source_fragments": ["выдуманный фрагмент"]},
    ]
    assert chain_rate(findings) == 0.5
    assert chain_rate([]) == 0.0
    doc_texts = "... орган вправе осуществить задержание до предъявления обвинения ..."
    assert grounding_rate(findings, doc_texts) == 0.5
    assert grounding_rate(findings, "") is None


def test_metrics_summarize_and_compare():
    rep_old = {"findings": [{"source_documents": ["A"], "abuse_scenarios": ["s"],
                             "vulnerability_type": "vague_term", "severity": "low",
                             "source_fragments": ["x"]}],
               "discarded_findings": []}
    rep_new = {"findings": [{"source_documents": ["A", "B"], "abuse_scenarios": ["s1", "s2", "s3"],
                             "vulnerability_type": "exploit_chain", "severity": "critical",
                             "source_fragments": ["x"]}],
               "discarded_findings": [{"title": "t", "reason": "r"}]}
    m = summarize_report_metrics(rep_new)
    assert m["chain_rate"] == 1.0 and m["exploit_chain_count"] == 1 and m["severity"]["critical"] == 1
    assert m["noise_rate"] == 0.5
    cmp = compare_reports(rep_old, rep_new)
    assert cmp["delta"]["chain_rate"] == 1.0 and cmp["delta"]["exploit_chain_count"] == 1


# END OF FILE tests/test_law_loophole.py
