"""Route-level regression tests for fail-open law-search quality telemetry."""

from __future__ import annotations

import pytest

from api.routers import overlay_laws
from api.schemas import LawSearchRequest, LawSearchResponse
import services.law_search_analytics as analytics


def _service_response() -> dict:
    return {
        "query": "угон",
        "results": [
            {
                "id": 101,
                "code": "12.1",
                "kind": "article",
                "title": "Неправомерное завладение транспортом",
                "doc_title": "Уголовный кодекс",
                "article_ref": "Статья 12.1",
                "penalty_summary": None,
                "snippet": "Текст",
                "score": 0.04,
                "sources": ["fts", "vector"],
            }
        ],
        "related": [],
        "degraded": False,
        "search_mode": "full",
        "warnings": [],
    }


def _model_dict(model: LawSearchResponse) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return model.dict()


@pytest.mark.asyncio
async def test_inactive_telemetry_skips_diagnostics_and_preserves_response(monkeypatch):
    captured = {}
    expected = _service_response()

    async def fake_search(_server_id, _query, *, limit, diagnostics):
        captured["limit"] = limit
        captured["diagnostics"] = diagnostics
        return expected

    monkeypatch.setattr(overlay_laws, "_check_access", lambda _claims: None)
    monkeypatch.setattr(
        overlay_laws,
        "_search_quality_logging_active",
        lambda: False,
    )
    monkeypatch.setattr(overlay_laws, "law_search", fake_search)

    response = await overlay_laws.search_laws(
        LawSearchRequest(server_id="memphis", query="угон", limit=8),
        claims={"tier": "laws"},
    )

    assert isinstance(response, LawSearchResponse)
    assert captured == {"limit": 8, "diagnostics": None}
    assert _model_dict(response) == _model_dict(LawSearchResponse(**expected))


@pytest.mark.asyncio
async def test_telemetry_build_failure_never_changes_search_response(monkeypatch):
    expected = _service_response()

    async def fake_search(_server_id, _query, *, limit, diagnostics):
        assert limit == 8
        assert isinstance(diagnostics, dict)
        diagnostics["algorithm_version"] = "test-v1"
        return expected

    def broken_event_builder(**_kwargs):
        raise RuntimeError("telemetry-only failure")

    monkeypatch.setattr(overlay_laws, "_check_access", lambda _claims: None)
    monkeypatch.setattr(
        overlay_laws,
        "_search_quality_logging_active",
        lambda: True,
    )
    monkeypatch.setattr(overlay_laws, "law_search", fake_search)
    monkeypatch.setattr(
        analytics,
        "is_law_search_logging_active",
        lambda: True,
    )
    monkeypatch.setattr(analytics, "build_search_event", broken_event_builder)

    response = await overlay_laws.search_laws(
        LawSearchRequest(server_id="memphis", query="угон", limit=8),
        claims={"tier": "laws"},
    )

    assert isinstance(response, LawSearchResponse)
    assert _model_dict(response) == _model_dict(LawSearchResponse(**expected))
