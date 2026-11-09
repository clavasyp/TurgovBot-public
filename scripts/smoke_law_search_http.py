#!/usr/bin/env python3
"""Acceptance-only HTTP smoke for one completed Overlay law catalog.

This script does **not** enrich or repair data.  Phase A/Phase B perform that
work.  The smoke is the final independent acceptance gate: ten real requests
must pass through the same HTTP endpoint used by Overlay before a Phase A
checkpoint can become ``complete``.

Run it inside the API container and pass the one-time nonce printed/stored by
the corresponding ``awaiting_smoke`` checkpoint:

    python /app/scripts/smoke_law_search_http.py las_vegas \
      --run-nonce <checkpoint nonce>

The complete before/request/after window holds the shared lock order
global -> law_documents generation -> catalog generation.  Search itself does
not acquire these writer advisory locks, so the localhost HTTP requests cannot
deadlock; ordinary search telemetry is the only expected write.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import httpx

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from database.manager import db_manager
from services.device_auth import _make_access
from services.law_smoke_contract import (
    LAW_SMOKE_CASE_NAMES,
    LAW_SMOKE_CASES,
    LAW_SMOKE_MIN_SCORE,
    LAW_SMOKE_SCHEMA_VERSION,
    SLANG_RULES,
    code_to_voice_query,
    normalize_smoke_text,
    recompute_case_result,
    recompute_cases,
    top_card_evidence,
    validate_semantic_card,
)
from services.law_source_snapshot import law_source_fingerprint


SCHEMA_VERSION = LAW_SMOKE_SCHEMA_VERSION
MAX_HTTP_SEARCHES = LAW_SMOKE_CASES
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
_RUN_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_run_nonce(raw: Any) -> str:
    value = str(raw or "").strip()
    if not _RUN_NONCE_RE.fullmatch(value):
        raise ValueError("--run-nonce must be the 64-character checkpoint nonce")
    return value


def technical_full(http_status: Any, payload: Any) -> tuple[bool, str]:
    if type(http_status) is not int or http_status != 200:
        return False, f"HTTP {http_status}"
    if not isinstance(payload, Mapping):
        return False, "response is not a JSON object"
    if payload.get("search_mode") != "full":
        return False, f"search_mode={payload.get('search_mode')!r}"
    if payload.get("degraded") is not False:
        return False, f"degraded={payload.get('degraded')!r}"
    if payload.get("warnings") != []:
        return False, f"warnings={payload.get('warnings')!r}"
    results = payload.get("results")
    if not isinstance(results, list) or not results or not isinstance(
        results[0], Mapping
    ):
        return False, "results are empty or malformed"
    return True, "HTTP 200/full/non-degraded/warning-free"


def evaluate_case(
    case: Mapping[str, Any],
    *,
    http_status: Any,
    payload: Any,
    elapsed_ms: float,
    index: Optional[int] = None,
) -> dict[str, Any]:
    """Record primitive response evidence, then evaluate it via the shared contract."""

    if index is None:
        try:
            index = LAW_SMOKE_CASE_NAMES.index(str(case.get("name") or ""))
        except ValueError as exc:
            raise ValueError("unknown smoke case name") from exc
    payload_map = payload if isinstance(payload, Mapping) else {}
    results = payload_map.get("results")
    top = (
        results[0]
        if isinstance(results, list)
        and results
        and isinstance(results[0], Mapping)
        else None
    )
    evidence = {
        "name": case.get("name"),
        "kind": case.get("kind"),
        "query": case.get("query"),
        "expected": case.get("expected"),
        "http_status": http_status,
        "elapsed_ms": round(max(0.0, elapsed_ms), 1),
        "search_mode": payload_map.get("search_mode"),
        "degraded": payload_map.get("degraded"),
        "warnings": payload_map.get("warnings"),
        "top": top_card_evidence(top),
    }
    evidence.update(recompute_case_result(evidence, index))
    return evidence


def source_fingerprint(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Compatibility wrapper around the shared Phase A/smoke identity."""

    return law_source_fingerprint(rows)


def _collapse_query_text(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def select_dynamic_cases(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Select three unique code+doc cases and one globally unique voice code."""

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            normalize_smoke_text(row.get("doc_title")),
            str(row.get("code") or ""),
            int(row["id"]),
        ),
    )
    eligible_exact: list[tuple[dict[str, Any], str, str]] = []
    for row in ordered:
        if int(row.get("pair_count") or 0) != 1:
            continue
        doc_title = _collapse_query_text(row.get("doc_title"), limit=10_000)
        if (
            not 3 <= len(doc_title) <= 120
            or not any(char.isalpha() for char in doc_title)
        ):
            continue
        eligible_exact.append((row, doc_title, normalize_smoke_text(doc_title)))

    chosen: list[tuple[dict[str, Any], str, str]] = []
    chosen_ids: set[int] = set()
    used_docs: set[str] = set()
    for distinct_docs_only in (True, False):
        for row, doc_title, doc_key in eligible_exact:
            article_id = int(row["id"])
            if article_id in chosen_ids:
                continue
            if distinct_docs_only and doc_key in used_docs:
                continue
            chosen.append((row, doc_title, doc_key))
            chosen_ids.add(article_id)
            used_docs.add(doc_key)
            if len(chosen) == 3:
                break
        if len(chosen) == 3:
            break

    exact_cases: list[dict[str, Any]] = []
    for row, doc_title, _doc_key in chosen:
        code = str(row["code"])
        exact_cases.append(
            {
                "name": f"dynamic_code_doc_{len(exact_cases) + 1}",
                "kind": "exact_code_doc",
                "query": f"статья {code} {doc_title}",
                "expected": {
                    "id": int(row["id"]),
                    "code": code,
                    "title": row.get("title"),
                    "doc_title": doc_title,
                },
            }
        )
    if len(exact_cases) != 3:
        raise RuntimeError(
            "catalog has fewer than three unambiguous code+document cases"
        )

    voice_candidates: list[tuple[int, int, dict[str, Any], str]] = []
    for row in ordered:
        if int(row.get("code_count") or 0) != 1:
            continue
        query = code_to_voice_query(str(row.get("code") or ""))
        if query is None:
            continue
        code = str(row["code"])
        voice_candidates.append(
            (0 if "." in code else 1, -len(code), row, query)
        )
    if not voice_candidates:
        raise RuntimeError(
            "catalog has no globally unique code representable by voice numerals"
        )
    _dot_rank, _length_rank, voice_row, voice_query = min(
        voice_candidates,
        key=lambda item: (item[0], item[1], int(item[2]["id"])),
    )
    voice_code = str(voice_row["code"])
    exact_cases.append(
        {
            "name": "dynamic_voice_code",
            "kind": "voice_code",
            "query": voice_query,
            "expected": {
                "id": int(voice_row["id"]),
                "code": voice_code,
                "title": voice_row.get("title"),
                "doc_title": voice_row.get("doc_title"),
            },
        }
    )
    return exact_cases


def _slang_cases() -> list[dict[str, Any]]:
    return [
        {
            "name": rule.name,
            "kind": "slang",
            "query": rule.query,
            "expected": {"semantic_validator": rule.name},
        }
        for rule in SLANG_RULES
    ]


def build_evidence(
    *,
    server_id: str,
    run_nonce: str,
    base_url: str,
    fingerprint_before: Mapping[str, Any],
    fingerprint_after: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    run_nonce = validate_run_nonce(run_nonce)
    normalized, score, all_full, manual_review = recompute_cases(cases)
    source_stable = dict(fingerprint_before) == dict(fingerprint_after)
    passed = (
        score >= LAW_SMOKE_MIN_SCORE
        and all_full
        and source_stable
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at or _utc_now(),
        "server_id": server_id,
        "run_nonce": run_nonce,
        "base_url": base_url,
        "source_fingerprint": dict(fingerprint_before),
        "source_fingerprint_after": dict(fingerprint_after),
        "source_stable": source_stable,
        "http_searches": len(normalized),
        "technical_full": all_full,
        "score": score,
        "max_score": MAX_HTTP_SEARCHES,
        "passed": passed,
        "manual_review": manual_review,
        "cases": normalized,
    }


def validate_local_base_url(raw: str) -> str:
    value = str(raw or "").rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("--base-url must be a credential-free localhost origin")
    return value


async def _read_contract(
    conn,
    server_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    await conn.execute(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    docs = await conn.fetch(
        """
        SELECT id, server_id, source_thread_id, title, content_hash
        FROM law_documents
        WHERE server_id = $1
        ORDER BY id
        """,
        server_id,
    )
    if not docs:
        raise RuntimeError(f"server {server_id!r} has no law_documents")
    candidates = await conn.fetch(
        """
        WITH numeric_articles AS (
          SELECT a.id, a.code, a.title, a.doc_title,
                 count(*) OVER (PARTITION BY a.code) AS code_count,
                 count(*) OVER (
                   PARTITION BY a.code, lower(COALESCE(a.doc_title, ''))
                 ) AS pair_count
          FROM law_articles a
          WHERE a.server_id = $1
            AND a.kind = 'article'
            AND a.code ~ '^[0-9]{1,3}([.][0-9]{1,3}){0,3}$'
        )
        SELECT id, code, title, doc_title, code_count, pair_count
        FROM numeric_articles
        ORDER BY id
        """,
        server_id,
    )
    return source_fingerprint(dict(row) for row in docs), [
        dict(row) for row in candidates
    ]


async def _read_fingerprint(conn, server_id: str) -> dict[str, Any]:
    await conn.execute(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    rows = await conn.fetch(
        """
        SELECT id, server_id, source_thread_id, title, content_hash
        FROM law_documents
        WHERE server_id = $1
        ORDER BY id
        """,
        server_id,
    )
    return source_fingerprint(dict(row) for row in rows)


def _access_tier() -> str:
    tiers = [
        str(tier).strip()
        for tier in config.LAW_SEARCH_TIERS
        if str(tier).strip()
    ]
    if "laws" in tiers:
        return "laws"
    return tiers[0] if tiers else "laws"


async def run_smoke(
    server_id: str,
    *,
    run_nonce: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    """Run the locked ten-request acceptance window for one checkpoint nonce."""

    from services.law_backfill_lock import law_backfill_global_lock
    from services.law_document_lock import law_document_server_lock
    from services.law_embedding_service import catalog_server_lock

    base_url = validate_local_base_url(base_url)
    run_nonce = validate_run_nonce(run_nonce)
    server_id = str(server_id or "").strip()
    if not server_id:
        raise ValueError("server_id must not be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")

    await db_manager.connect()
    try:
        # Lock order is the same in every Phase A/Phase B/smoke entry point.
        async with law_backfill_global_lock(manager=db_manager):
            async with law_document_server_lock(server_id, manager=db_manager):
                async with catalog_server_lock(server_id):
                    async with db_manager.transaction() as conn:
                        fingerprint_before, candidate_rows = await _read_contract(
                            conn,
                            server_id,
                        )
                    cases = _slang_cases() + select_dynamic_cases(candidate_rows)
                    if len(cases) != MAX_HTTP_SEARCHES:
                        raise RuntimeError(
                            f"internal case count is {len(cases)}, "
                            f"expected {MAX_HTTP_SEARCHES}"
                        )

                    device_id = f"law-smoke-{uuid.uuid4()}"
                    token = _make_access(
                        0,
                        device_id,
                        _access_tier(),
                        secrets.token_urlsafe(12),
                    )
                    headers = {
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "ProdBot-law-search-smoke/2",
                    }
                    timeout = httpx.Timeout(
                        timeout_seconds,
                        connect=min(5.0, timeout_seconds),
                    )
                    results: list[dict[str, Any]] = []
                    async with httpx.AsyncClient(
                        base_url=base_url,
                        headers=headers,
                        timeout=timeout,
                        trust_env=False,
                        follow_redirects=False,
                    ) as client:
                        for index, case in enumerate(cases):
                            started = asyncio.get_running_loop().time()
                            status: Any = None
                            payload: Any = None
                            try:
                                response = await client.post(
                                    "/v1/overlay/laws/search",
                                    json={
                                        "server_id": server_id,
                                        "query": case["query"],
                                        "limit": 8,
                                    },
                                )
                                status = response.status_code
                                try:
                                    payload = response.json()
                                except (ValueError, json.JSONDecodeError):
                                    payload = None
                            except httpx.HTTPError:
                                # Never include token/header/exception details.
                                status = "transport_error"
                                payload = None
                            elapsed_ms = (
                                asyncio.get_running_loop().time() - started
                            ) * 1000.0
                            results.append(
                                evaluate_case(
                                    case,
                                    http_status=status,
                                    payload=payload,
                                    elapsed_ms=elapsed_ms,
                                    index=index,
                                )
                            )

                    async with db_manager.transaction() as conn:
                        fingerprint_after = await _read_fingerprint(
                            conn,
                            server_id,
                        )
    finally:
        await db_manager.close()

    return build_evidence(
        server_id=server_id,
        run_nonce=run_nonce,
        base_url=base_url,
        fingerprint_before=fingerprint_before,
        fingerprint_after=fingerprint_after,
        cases=results,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Acceptance-only: ten real localhost HTTP searches for one completed "
            "Overlay law catalog"
        )
    )
    parser.add_argument("server_id")
    parser.add_argument("--run-nonce", required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        nonce = validate_run_nonce(args.run_nonce)
        evidence = asyncio.run(
            run_smoke(
                str(args.server_id or "").strip(),
                run_nonce=nonce,
                base_url=args.base_url,
                timeout_seconds=args.timeout,
            )
        )
    except Exception as exc:
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "server_id": str(args.server_id or "").strip(),
            "run_nonce": str(args.run_nonce or "").strip(),
            "score": 0,
            "max_score": MAX_HTTP_SEARCHES,
            "passed": False,
            "fatal_error": {"type": type(exc).__name__},
        }
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if evidence.get("passed") is True else 1)


if __name__ == "__main__":
    main()
