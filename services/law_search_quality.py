"""Read-only quality signals for the Overlay law-search event log.

The functions in this module intentionally do not call the search service or the
database.  They only aggregate already persisted event dictionaries, so running
an analysis can never change ranking or user-visible answers.

The signals are candidates for manual review, not automatic correctness labels:

* a result supported by one weak retrieval leg may still be correct;
* a small RRF gap means that two candidates were close *inside the same search
  version*, not that the scores are probabilities;
* an empty result may be the honest answer for a server whose catalog has no
  matching norm.
"""

from __future__ import annotations

import json
import math
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


# These legs are useful recall signals but are not exact evidence on their own.
# Keeping the set here (rather than importing the live search service) makes this
# analyzer independent from ranking code and safe to run against historical logs.
DEFAULT_WEAK_TOP_SOURCES = frozenset(
    {
        "fts",
        "legal_fts",
        "synonym",
        "trgm",
        "vector",
    }
)


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _non_negative_number(value: Any) -> float | None:
    result = _number(value)
    return result if result is not None and result >= 0 else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _json_list(value: Any) -> list[Any]:
    """Normalize JSONB/list values returned by different asyncpg codecs."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _top_result(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    results = _json_list(event.get("results"))
    if results and isinstance(results[0], Mapping):
        return results[0]
    return None


def _top_sources(event: Mapping[str, Any]) -> tuple[str, ...]:
    top = _top_result(event)
    if top is None:
        return ()
    raw_sources = _json_list(top.get("sources"))
    # One event counts at most once per source even if malformed historical JSON
    # happened to contain duplicates.
    return tuple(
        dict.fromkeys(
            source.strip()
            for source in raw_sources
            if isinstance(source, str) and source.strip()
        )
    )


def _top_article_id(event: Mapping[str, Any]) -> int | None:
    article_id = _integer(event.get("top_article_id"))
    if article_id is not None:
        return article_id
    top = _top_result(event)
    return _integer(top.get("id")) if top is not None else None


def _event_id(value: Any) -> str | None:
    """Return only canonical random event UUIDs, never arbitrary log text."""
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


def _relative_top_gap(event: Mapping[str, Any]) -> float | None:
    """Return (top - second) / abs(top), without interpreting RRF as probability."""
    top_score = _number(event.get("top_score"))
    gap = _number(event.get("top_score_gap"))
    # A strict intent may intentionally be pinned above a candidate with a
    # numerically higher raw RRF score.  That is not a "small score gap"; the
    # score order is simply not the reason for the final order.
    if gap is not None and gap < 0:
        return None

    results = _json_list(event.get("results"))
    if top_score is None and results and isinstance(results[0], Mapping):
        top_score = _number(results[0].get("score"))
    if gap is None and len(results) >= 2:
        first = results[0] if isinstance(results[0], Mapping) else {}
        second = results[1] if isinstance(results[1], Mapping) else {}
        first_score = _number(first.get("score"))
        second_score = _number(second.get("score"))
        if first_score is not None and second_score is not None:
            gap = first_score - second_score
            if gap < 0:
                return None

    if top_score is None or gap is None or top_score == 0:
        return None
    return gap / abs(top_score)


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    """Linearly interpolated percentile for a finite numeric sequence."""
    if not 0 <= percentile_value <= 1:
        raise ValueError("percentile_value must be between 0 and 1")
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile_value
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    lower = ordered[lower_index]
    upper = ordered[upper_index]
    if lower_index == upper_index:
        return float(lower)
    return float(lower + (upper - lower) * (position - lower_index))


def analyze_search_events(
    events: Iterable[Mapping[str, Any]],
    *,
    small_gap_threshold: float = 0.10,
    weak_top_sources: Iterable[str] = DEFAULT_WEAK_TOP_SOURCES,
    review_candidate_limit: int = 50,
) -> dict[str, Any]:
    """Aggregate privacy-safe quality signals from ``law_search_events`` rows.

    Query text and query hashes are never returned.  ``query_hash`` is used only
    in memory to group repeated requests within the same server and algorithm
    version.
    """
    if not 0 <= small_gap_threshold <= 1:
        raise ValueError("small_gap_threshold must be between 0 and 1")
    if not 0 <= review_candidate_limit <= 1_000:
        raise ValueError("review_candidate_limit must be between 0 and 1000")

    rows = [dict(event) for event in events]
    total = len(rows)
    weak_sources = frozenset(
        source.strip()
        for source in weak_top_sources
        if isinstance(source, str) and source.strip()
    )

    outcomes: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    top_sources: Counter[str] = Counter()
    weak_by_source: Counter[str] = Counter()
    latencies: list[float] = []
    degraded_count = 0
    empty_count = 0
    top_present_count = 0
    weak_single_leg_count = 0
    gap_evaluated_count = 0
    small_gap_count = 0
    review_candidate_count = 0
    review_reasons: Counter[str] = Counter()
    review_items: list[dict[str, Any]] = []

    # The algorithm version is part of the key on purpose: a changed top result
    # after a deliberate ranking release is not an instability within one model.
    repeated: dict[tuple[str, str, str], list[int]] = defaultdict(list)

    for event in rows:
        outcome = str(event.get("outcome") or "unknown")
        mode = str(event.get("search_mode") or "unknown")
        outcomes[outcome] += 1
        modes[mode] += 1

        is_degraded = event.get("degraded") is True
        if is_degraded:
            degraded_count += 1
        if outcome == "empty":
            empty_count += 1

        latency = _non_negative_number(event.get("duration_ms"))
        if latency is not None:
            latencies.append(latency)

        article_id = _top_article_id(event)
        sources = _top_sources(event)
        if article_id is not None:
            top_present_count += 1
        for source in sources:
            top_sources[source] += 1
        is_weak_single_leg = (
            article_id is not None
            and len(sources) == 1
            and sources[0] in weak_sources
        )
        if is_weak_single_leg:
            weak_single_leg_count += 1
            weak_by_source[sources[0]] += 1

        relative_gap = _relative_top_gap(event)
        is_small_gap = False
        if relative_gap is not None:
            gap_evaluated_count += 1
            if relative_gap <= small_gap_threshold:
                is_small_gap = True
                small_gap_count += 1

        reasons: list[str] = []
        if outcome == "empty":
            reasons.append("empty")
        if outcome == "unavailable":
            reasons.append("outcome_unavailable")
        if outcome == "error":
            reasons.append("outcome_error")
        if is_degraded:
            reasons.append("degraded")
        if is_weak_single_leg:
            reasons.append("weak_single_leg_top")
        if is_small_gap:
            reasons.append("small_relative_top_gap")
        canonical_event_id = _event_id(event.get("event_id"))
        if reasons and canonical_event_id is not None:
            review_candidate_count += 1
            review_reasons.update(reasons)
            if len(review_items) < review_candidate_limit:
                review_items.append(
                    {
                        "event_id": canonical_event_id,
                        "reasons": reasons,
                    }
                )

        server_id = event.get("server_id")
        query_hash = event.get("query_hash")
        algorithm_version = event.get("algorithm_version")
        if (
            article_id is not None
            and isinstance(server_id, str)
            and server_id
            and isinstance(query_hash, str)
            and query_hash
            and isinstance(algorithm_version, str)
            and algorithm_version
        ):
            repeated[(server_id, query_hash, algorithm_version)].append(article_id)

    repeated_groups = {
        key: article_ids
        for key, article_ids in repeated.items()
        if len(article_ids) >= 2
    }
    inconsistent_groups = {
        key: article_ids
        for key, article_ids in repeated_groups.items()
        if len(set(article_ids)) >= 2
    }
    inconsistent_by_server: Counter[str] = Counter(
        key[0] for key in inconsistent_groups
    )
    inconsistent_by_algorithm: Counter[str] = Counter(
        key[2] for key in inconsistent_groups
    )

    def sorted_counts(counter: Counter[str]) -> dict[str, int]:
        return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))

    latency_p50 = percentile(latencies, 0.50)
    latency_p95 = percentile(latencies, 0.95)

    return {
        "total": total,
        "outcomes": sorted_counts(outcomes),
        "search_modes": sorted_counts(modes),
        "degraded": {
            "count": degraded_count,
            "rate": _rate(degraded_count, total),
        },
        "latency_ms": {
            "observed": len(latencies),
            "p50": round(latency_p50, 2) if latency_p50 is not None else None,
            "p95": round(latency_p95, 2) if latency_p95 is not None else None,
            "max": round(max(latencies), 2) if latencies else None,
        },
        "empty": {
            "count": empty_count,
            "rate": _rate(empty_count, total),
        },
        "weak_single_leg_top": {
            "count": weak_single_leg_count,
            "evaluated": top_present_count,
            "rate": _rate(weak_single_leg_count, top_present_count),
            "by_source": sorted_counts(weak_by_source),
        },
        "small_relative_top_gap": {
            "threshold": small_gap_threshold,
            "count": small_gap_count,
            "evaluated": gap_evaluated_count,
            "rate": _rate(small_gap_count, gap_evaluated_count),
        },
        "repeated_inconsistent_top": {
            "count": len(inconsistent_groups),
            "repeated_groups_evaluated": len(repeated_groups),
            "rate": _rate(len(inconsistent_groups), len(repeated_groups)),
            "events_in_inconsistent_groups": sum(
                len(article_ids) for article_ids in inconsistent_groups.values()
            ),
            "by_server": sorted_counts(inconsistent_by_server),
            "by_algorithm_version": sorted_counts(inconsistent_by_algorithm),
        },
        "top_sources": sorted_counts(top_sources),
        "review_candidates": {
            "count": review_candidate_count,
            "returned": len(review_items),
            "truncated": review_candidate_count > len(review_items),
            "by_reason": sorted_counts(review_reasons),
            "items": review_items,
        },
    }


def format_quality_report(report: Mapping[str, Any]) -> str:
    """Render an aggregate report without query text, hashes or identity data."""
    degraded = report.get("degraded") or {}
    empty = report.get("empty") or {}
    latency = report.get("latency_ms") or {}
    weak = report.get("weak_single_leg_top") or {}
    gap = report.get("small_relative_top_gap") or {}
    inconsistent = report.get("repeated_inconsistent_top") or {}
    review = report.get("review_candidates") or {}

    def percent(value: Any) -> str:
        number = _number(value) or 0.0
        return f"{number * 100:.1f}%"

    def counts(value: Any) -> str:
        if not isinstance(value, Mapping) or not value:
            return "нет"
        return ", ".join(f"{key}={count}" for key, count in value.items())

    def latency_value(value: Any) -> str:
        number = _number(value)
        return "нет данных" if number is None else f"{number:.2f} мс"

    lines = [
            "Качество Overlay-поиска (агрегировано, без данных пользователей)",
            f"Событий: {int(report.get('total') or 0)}",
            f"Исходы: {counts(report.get('outcomes'))}",
            f"Режимы: {counts(report.get('search_modes'))}",
            (
                f"Деградация: {int(degraded.get('count') or 0)} "
                f"({percent(degraded.get('rate'))})"
            ),
            (
                f"Пустая выдача: {int(empty.get('count') or 0)} "
                f"({percent(empty.get('rate'))})"
            ),
            (
                "Задержка: "
                f"p50={latency_value(latency.get('p50'))}, "
                f"p95={latency_value(latency.get('p95'))}, "
                f"max={latency_value(latency.get('max'))}"
            ),
            (
                "Top-1 только от одной слабой ноги: "
                f"{int(weak.get('count') or 0)}/"
                f"{int(weak.get('evaluated') or 0)} "
                f"({percent(weak.get('rate'))}); "
                f"источники: {counts(weak.get('by_source'))}"
            ),
            (
                "Малый относительный разрыв top-1/top-2: "
                f"{int(gap.get('count') or 0)}/"
                f"{int(gap.get('evaluated') or 0)} "
                f"({percent(gap.get('rate'))}), "
                f"порог={percent(gap.get('threshold'))}"
            ),
            (
                "Повторные одинаковые запросы с разным top-1 "
                "(в одной версии алгоритма): "
                f"{int(inconsistent.get('count') or 0)}/"
                f"{int(inconsistent.get('repeated_groups_evaluated') or 0)} групп"
            ),
            f"Источники top-1: {counts(report.get('top_sources'))}",
            (
                "Кандидаты ручной проверки: "
                f"{int(review.get('count') or 0)}, "
                f"показано {int(review.get('returned') or 0)}; "
                f"причины: {counts(review.get('by_reason'))}"
            ),
            (
                "Примечание: это сигналы для ручной проверки. RRF-score не является "
                "вероятностью, а пустой или одноголосый результат не обязательно ошибочен."
            ),
        ]
    items = review.get("items")
    if isinstance(items, Sequence):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            event_id = item.get("event_id")
            reasons = item.get("reasons")
            if not isinstance(event_id, str) or not isinstance(reasons, Sequence):
                continue
            safe_reasons = [
                reason for reason in reasons
                if isinstance(reason, str) and reason
            ]
            lines.append(f"- {event_id}: {', '.join(safe_reasons)}")
    return "\n".join(lines)
