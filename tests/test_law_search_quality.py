import json

import pytest

from scripts import analyze_law_search_logs as quality_cli
from services.law_search_quality import (
    analyze_search_events,
    format_quality_report,
    percentile,
)


def _event(**overrides):
    event = {
        "event_id": "00000000-0000-4000-8000-000000000001",
        "server_id": "memphis",
        "query_hash": "a" * 64,
        "algorithm_version": "search-v1",
        "outcome": "success",
        "search_mode": "full",
        "degraded": False,
        "duration_ms": 100,
        "result_count": 2,
        "top_article_id": 101,
        "top_score": 0.04,
        "top_score_gap": 0.002,
        "results": [
            {"id": 101, "score": 0.04, "sources": ["fts"]},
            {"id": 102, "score": 0.038, "sources": ["vector"]},
        ],
        # The analyzer must never echo these fields.
        "query_text": "private raw question",
        "query_normalized": "private normalized question",
        "tier": "private tier",
    }
    event.update(overrides)
    return event


def test_analyze_search_events_aggregates_quality_signals_without_pii():
    events = [
        _event(),
        _event(
            top_article_id=103,
            top_score=0.05,
            top_score_gap=0.02,
            duration_ms=300,
            results=json.dumps(
                [
                    {"id": 103, "score": 0.05, "sources": ["title"]},
                    {"id": 102, "score": 0.03, "sources": ["vector"]},
                ]
            ),
        ),
        _event(
            query_hash="b" * 64,
            outcome="empty",
            search_mode="keyword_only",
            degraded=True,
            duration_ms=200,
            result_count=0,
            top_article_id=None,
            top_score=None,
            top_score_gap=None,
            results=[],
        ),
        _event(
            query_hash="c" * 64,
            duration_ms=400,
            top_article_id=104,
            top_score=0.03,
            top_score_gap=None,
            results=[{"id": 104, "score": 0.03, "sources": ["vector"]}],
        ),
    ]

    report = analyze_search_events(events, small_gap_threshold=0.10)

    assert report["total"] == 4
    assert report["outcomes"] == {"success": 3, "empty": 1}
    assert report["search_modes"] == {"full": 3, "keyword_only": 1}
    assert report["degraded"] == {"count": 1, "rate": 0.25}
    assert report["empty"] == {"count": 1, "rate": 0.25}
    assert report["latency_ms"] == {
        "observed": 4,
        "p50": 250.0,
        "p95": 385.0,
        "max": 400.0,
    }
    assert report["weak_single_leg_top"] == {
        "count": 2,
        "evaluated": 3,
        "rate": 0.6667,
        "by_source": {"fts": 1, "vector": 1},
    }
    assert report["small_relative_top_gap"] == {
        "threshold": 0.10,
        "count": 1,
        "evaluated": 2,
        "rate": 0.5,
    }
    assert report["top_sources"] == {"fts": 1, "title": 1, "vector": 1}
    assert report["review_candidates"] == {
        "count": 3,
        "returned": 3,
        "truncated": False,
        "by_reason": {
            "weak_single_leg_top": 2,
            "degraded": 1,
            "empty": 1,
            "small_relative_top_gap": 1,
        },
        "items": [
            {
                "event_id": "00000000-0000-4000-8000-000000000001",
                "reasons": ["weak_single_leg_top", "small_relative_top_gap"],
            },
            {
                "event_id": "00000000-0000-4000-8000-000000000001",
                "reasons": ["empty", "degraded"],
            },
            {
                "event_id": "00000000-0000-4000-8000-000000000001",
                "reasons": ["weak_single_leg_top"],
            },
        ],
    }

    serialized = json.dumps(report)
    assert "private raw question" not in serialized
    assert "private normalized question" not in serialized
    assert "a" * 64 not in serialized
    assert "private tier" not in serialized


def test_repeated_inconsistent_top_is_scoped_by_server_and_algorithm_version():
    events = [
        _event(top_article_id=101),
        _event(top_article_id=103),
        # A different ranking version is deliberately a different group.
        _event(algorithm_version="search-v2", top_article_id=104),
        # Repeated and stable group is evaluated but not inconsistent.
        _event(query_hash="b" * 64, top_article_id=105),
        _event(query_hash="b" * 64, top_article_id=105),
        # Same query hash on another server is also a different group.
        _event(server_id="denver", top_article_id=106),
    ]

    report = analyze_search_events(events)

    assert report["repeated_inconsistent_top"] == {
        "count": 1,
        "repeated_groups_evaluated": 2,
        "rate": 0.5,
        "events_in_inconsistent_groups": 2,
        "by_server": {"memphis": 1},
        "by_algorithm_version": {"search-v1": 1},
    }


def test_small_gap_falls_back_to_result_scores_and_is_relative():
    event = _event(
        top_score=None,
        top_score_gap=None,
        results=[
            {"id": 101, "score": 0.020, "sources": ["fts", "vector"]},
            {"id": 102, "score": 0.019, "sources": ["vector"]},
        ],
    )

    report = analyze_search_events([event], small_gap_threshold=0.051)

    assert report["small_relative_top_gap"]["count"] == 1
    assert report["small_relative_top_gap"]["evaluated"] == 1
    # 0.001 is small relative to 0.020; the absolute RRF value is never used as
    # a probability or a global confidence threshold.
    assert report["small_relative_top_gap"]["rate"] == 1.0


def test_small_gap_skips_intent_pin_whose_raw_rrf_order_is_reversed():
    event = _event(
        top_score=0.020,
        top_score_gap=-0.010,
        results=[
            {"id": 101, "score": 0.020, "sources": ["intent_phrase"]},
            {"id": 102, "score": 0.030, "sources": ["fts", "vector"]},
        ],
    )

    report = analyze_search_events([event])

    assert report["small_relative_top_gap"]["evaluated"] == 0
    assert report["small_relative_top_gap"]["count"] == 0


def test_empty_report_and_text_formatter_do_not_fail():
    report = analyze_search_events([])

    assert report["total"] == 0
    assert report["latency_ms"]["p50"] is None
    assert report["weak_single_leg_top"]["rate"] == 0.0
    text = format_quality_report(report)
    assert "Событий: 0" in text
    assert "RRF-score не является вероятностью" in text


def test_review_candidates_are_bounded_and_show_errors_without_pii():
    events = [
        _event(
            event_id="00000000-0000-4000-8000-000000000010",
            query_hash="d" * 64,
            outcome="unavailable",
            results=[],
            top_article_id=None,
            top_score=None,
            top_score_gap=None,
        ),
        _event(
            event_id="00000000-0000-4000-8000-000000000011",
            query_hash="e" * 64,
            outcome="error",
            results=[],
            top_article_id=None,
            top_score=None,
            top_score_gap=None,
        ),
    ]

    report = analyze_search_events(events, review_candidate_limit=1)

    assert report["outcomes"] == {"error": 1, "unavailable": 1}
    assert report["review_candidates"] == {
        "count": 2,
        "returned": 1,
        "truncated": True,
        "by_reason": {"outcome_error": 1, "outcome_unavailable": 1},
        "items": [
            {
                "event_id": "00000000-0000-4000-8000-000000000010",
                "reasons": ["outcome_unavailable"],
            }
        ],
    }
    serialized = json.dumps(report)
    assert "d" * 64 not in serialized
    assert "e" * 64 not in serialized
    text = format_quality_report(report)
    assert "error=1" in text
    assert "unavailable=1" in text
    assert "outcome_unavailable" in text


def test_percentile_and_validation():
    assert percentile([100, 200, 300, 400], 0.50) == 250
    assert percentile([100, 200, 300, 400], 0.95) == pytest.approx(385)
    assert percentile([], 0.50) is None
    with pytest.raises(ValueError):
        percentile([1], 1.1)
    with pytest.raises(ValueError):
        analyze_search_events([], small_gap_threshold=-0.01)
    with pytest.raises(ValueError):
        analyze_search_events([], review_candidate_limit=1_001)


@pytest.mark.asyncio
async def test_cli_loader_does_not_select_raw_questions_or_identity_data(monkeypatch):
    calls = []

    async def fake_fetch(query, *args):
        calls.append((query, args))
        return [
            {
                "event_id": "00000000-0000-4000-8000-000000000020",
                "server_id": "memphis",
                "query_hash": "a" * 64,
                "algorithm_version": "search-v1",
                "outcome": "success",
                "search_mode": "full",
                "degraded": False,
                "duration_ms": 100,
                "result_count": 1,
                "top_article_id": 101,
                "top_score": 0.04,
                "top_score_gap": None,
                "results": [],
                "matching_total": 12,
            }
        ]

    monkeypatch.setattr(quality_cli.db_manager, "fetch", fake_fetch)

    events, matching_total = await quality_cli._load_events(
        days=7,
        server_id="memphis",
        max_events=100,
    )

    query, args = calls[0]
    assert "query_text" not in query
    assert "query_normalized" not in query
    assert "tier" not in query
    assert "user" not in query.lower()
    assert args == (7, "memphis", 100)
    assert matching_total == 12
    assert "matching_total" not in events[0]
    assert events[0]["event_id"] == "00000000-0000-4000-8000-000000000020"
