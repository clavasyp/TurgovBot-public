#!/usr/bin/env python3
"""Audit or exactly roll back one server from an additive synonym run."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any, Dict

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_synonym_service import acquire_synonym_write_lock


async def rollback_run(
    run_id: str,
    server_id: str,
    *,
    apply: bool = False,
) -> Dict[str, Any]:
    """Verify every logged row, then optionally delete all of them atomically.

    A run may contain several servers.  Requiring an explicit server keeps the
    transaction and lock scope small and makes an operator inspect each server
    separately.  If even one row changed since insertion, nothing is deleted.
    """

    async with law_document_server_lock(server_id):
        async with catalog_server_lock(server_id):
            return await _rollback_transaction(run_id, server_id, apply=apply)


async def _rollback_transaction(
    run_id: str,
    server_id: str,
    *,
    apply: bool,
) -> Dict[str, Any]:
    async with db_manager.transaction() as conn:
        # Shared global order: synonym advisory lock before any article/synonym
        # row lock.  This matches additive and regular enrichment writers.
        await acquire_synonym_write_lock(conn, server_id)
        rows = await conn.fetch(
            """
            SELECT log.run_id::text, log.synonym_id, log.server_id,
                   log.article_id, log.term, log.article_hash, log.source,
                   log.status, log.weight, log.suggested_by,
                   synonym.id AS current_id,
                   synonym.server_id AS current_server_id,
                   synonym.article_id AS current_article_id,
                   synonym.term AS current_term,
                   synonym.source AS current_source,
                   synonym.status AS current_status,
                   synonym.weight AS current_weight,
                   synonym.suggested_by AS current_suggested_by,
                   article.article_hash AS current_article_hash
            FROM law_synonym_backfill_inserts log
            LEFT JOIN law_synonyms synonym ON synonym.id = log.synonym_id
            LEFT JOIN law_articles article ON article.id = synonym.article_id
            WHERE log.run_id = $1::uuid AND log.server_id = $2
            ORDER BY log.synonym_id
            FOR UPDATE OF log
            """,
            run_id,
            server_id,
        )
        if not rows:
            raise RuntimeError(
                f"no insert log rows for run {run_id} and server {server_id}"
            )

        safe_ids: list[int] = []
        mismatches: list[dict[str, Any]] = []
        for row in rows:
            expected = {
                "server_id": str(row["server_id"]),
                "article_id": int(row["article_id"]),
                "term": str(row["term"]),
                "article_hash": str(row["article_hash"]),
                "source": str(row["source"]),
                "status": str(row["status"]),
                "weight": float(row["weight"]),
                "suggested_by": row["suggested_by"],
            }
            actual = {
                "server_id": row["current_server_id"],
                "article_id": row["current_article_id"],
                "term": row["current_term"],
                "article_hash": row["current_article_hash"],
                "source": row["current_source"],
                "status": row["current_status"],
                "weight": (
                    float(row["current_weight"])
                    if row["current_weight"] is not None
                    else None
                ),
                "suggested_by": row["current_suggested_by"],
            }
            if (
                row["current_id"] is not None
                and actual == expected
                and expected["server_id"] == server_id
            ):
                safe_ids.append(int(row["synonym_id"]))
            else:
                mismatches.append({
                    "synonym_id": int(row["synonym_id"]),
                    "expected": expected,
                    "actual": actual,
                })

        deleted_ids: list[int] = []
        if apply and not mismatches:
            deleted = await conn.fetch(
                """
                DELETE FROM law_synonyms synonym
                USING law_synonym_backfill_inserts log, law_articles article
                WHERE log.run_id = $1::uuid
                  AND log.server_id = $2
                  AND synonym.id = log.synonym_id
                  AND synonym.id = ANY($3::int[])
                  AND article.id = synonym.article_id
                  AND synonym.server_id = log.server_id
                  AND synonym.article_id = log.article_id
                  AND synonym.term = log.term
                  AND synonym.source = log.source
                  AND synonym.status = log.status
                  AND synonym.weight IS NOT DISTINCT FROM log.weight
                  AND synonym.suggested_by IS NOT DISTINCT FROM log.suggested_by
                  AND article.article_hash = log.article_hash
                RETURNING synonym.id
                """,
                run_id,
                server_id,
                safe_ids,
            )
            deleted_ids = sorted(int(row["id"]) for row in deleted)
            if deleted_ids != sorted(safe_ids):
                # Raising aborts the transaction: a short delete can never be
                # reported as successful after only a partial mutation.
                raise RuntimeError(
                    "rollback delete count/identity mismatch; transaction aborted"
                )

        return {
            "run_id": run_id,
            "server_id": server_id,
            "mode": "apply" if apply else "audit",
            "verified_synonym_ids": sorted(safe_ids),
            "deleted_synonym_ids": deleted_ids,
            "mismatches": mismatches,
            "can_apply": not mismatches,
        }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Audit by default; use --apply to atomically delete exact rows "
            "inserted for one run/server"
        )
    )
    parser.add_argument("run_id")
    parser.add_argument("--server", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform deletion after a clean all-row audit (default: read-only audit)",
    )
    args = parser.parse_args()
    try:
        run_id = str(uuid.UUID(args.run_id))
    except ValueError:
        parser.error("run_id must be a UUID")
    server_id = str(args.server or "").strip()
    if not server_id:
        parser.error("--server must be non-empty")

    await db_manager.connect()
    try:
        result = await rollback_run(run_id, server_id, apply=args.apply)
    finally:
        await db_manager.close()

    print(json.dumps(result, ensure_ascii=False))
    if result["mismatches"]:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
