#!/usr/bin/env python3
"""Read-only aggregate analysis of persisted Overlay law-search events."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from services.law_search_quality import analyze_search_events, format_quality_report


_SELECT_COLUMNS = """
    event_id,
    server_id,
    query_hash,
    algorithm_version,
    outcome,
    search_mode,
    degraded,
    duration_ms,
    result_count,
    top_article_id,
    top_score,
    top_score_gap,
    results
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only анализ качества Overlay law search по law_search_events. "
            "Тексты запросов и данные пользователей не читаются и не выводятся."
        )
    )
    parser.add_argument(
        "--days",
        type=float,
        default=7.0,
        help="глубина анализа в днях (по умолчанию: 7)",
    )
    parser.add_argument(
        "--server",
        help="опционально ограничить анализ одним server_id",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=20_000,
        help="максимум самых свежих событий для загрузки (по умолчанию: 20000)",
    )
    parser.add_argument(
        "--small-gap-threshold",
        type=float,
        default=0.10,
        help="порог малого относительного разрыва top-1/top-2, 0..1 (по умолчанию: 0.10)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="вывести машиночитаемый JSON вместо краткого русского отчёта",
    )
    parser.add_argument(
        "--review-limit",
        type=int,
        default=50,
        help="максимум event_id в списке ручной проверки, 0..1000 (по умолчанию: 50)",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.days <= 0:
        parser.error("--days должен быть больше 0")
    if not 1 <= args.max_events <= 1_000_000:
        parser.error("--max-events должен быть в диапазоне 1..1000000")
    if not 0 <= args.small_gap_threshold <= 1:
        parser.error("--small-gap-threshold должен быть в диапазоне 0..1")
    if not 0 <= args.review_limit <= 1_000:
        parser.error("--review-limit должен быть в диапазоне 0..1000")


async def _load_events(
    *,
    days: float,
    server_id: str | None,
    max_events: int,
) -> tuple[list[dict[str, Any]], int]:
    where = "created_at >= NOW() - ($1::double precision * INTERVAL '1 day')"
    if server_id:
        query = f"""
            SELECT {_SELECT_COLUMNS}, COUNT(*) OVER () AS matching_total
            FROM law_search_events
            WHERE {where} AND server_id = $2
            ORDER BY created_at DESC
            LIMIT $3
        """
        rows = await db_manager.fetch(query, days, server_id, max_events)
    else:
        query = f"""
            SELECT {_SELECT_COLUMNS}, COUNT(*) OVER () AS matching_total
            FROM law_search_events
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT $2
        """
        rows = await db_manager.fetch(query, days, max_events)

    events = [dict(row) for row in rows]
    matching_total = int(events[0].pop("matching_total")) if events else 0
    for event in events[1:]:
        event.pop("matching_total", None)
    return events, matching_total


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    await db_manager.connect()
    try:
        events, matching_total = await _load_events(
            days=args.days,
            server_id=args.server,
            max_events=args.max_events,
        )
    finally:
        await db_manager.close()

    report = analyze_search_events(
        events,
        small_gap_threshold=args.small_gap_threshold,
        review_candidate_limit=args.review_limit,
    )
    report["window"] = {
        "days": args.days,
        "server_id": args.server,
        "matching_events": matching_total,
        "analyzed_events": len(events),
        "truncated": matching_total > len(events),
    }
    return report


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    _validate_args(parser, args)
    try:
        report = asyncio.run(_run(args))
    except Exception as exc:
        # Avoid printing DSNs, SQL arguments or raw database records.
        if exc.__class__.__name__ == "UndefinedTableError":
            parser.exit(
                2,
                "Таблица law_search_events не найдена. Сначала примените migration 022.\n",
            )
        parser.exit(1, f"Не удалось прочитать law_search_events: {exc.__class__.__name__}\n")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return

    print(format_quality_report(report))
    window = report["window"]
    scope = f", server={window['server_id']}" if window["server_id"] else ""
    print(f"Окно: {window['days']:g} дн.{scope}")
    if window["truncated"]:
        print(
            "Важно: показана свежая выборка "
            f"{window['analyzed_events']} из {window['matching_events']} событий."
        )


if __name__ == "__main__":
    main()
