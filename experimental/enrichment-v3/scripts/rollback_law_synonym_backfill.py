#!/usr/bin/env python3
"""Audit or exactly roll back one server from an additive synonym run."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Mapping

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from services.law_enrichment_artifact import normalize_penalty_json
from services.law_enrichment_output import (
    build_enrichment_output_hash,
    foreign_auto_ownership_article_ids,
)
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_synonym_service import acquire_synonym_write_lock


async def _evaluate_affected_completion(
    conn,
    server_id: str,
    article_ids: list[int],
):
    """Resolve the shared evaluator lazily and reuse this write snapshot."""

    from services.law_enrichment_completion import (
        evaluate_law_enrichment_completion,
    )

    return await evaluate_law_enrichment_completion(
        conn,
        server_id,
        article_ids=article_ids,
    )


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
                   log.status, log.weight, log.suggested_by
            FROM law_synonym_backfill_inserts log
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

        article_ids = sorted({int(row["article_id"]) for row in rows})
        metadata_rows = await conn.fetch(
            """
            SELECT id, server_id, kind, article_hash, title,
                   title_provenance, penalty_summary, penalty_json,
                   enrich_hash, enrichment_context_hash,
                   enrichment_output_hash
            FROM law_articles
            WHERE id = ANY($1::int[])
            ORDER BY id
            FOR UPDATE
            """,
            article_ids,
        )
        metadata_by_article = {
            int(row["id"]): dict(row)
            for row in metadata_rows
        }
        pre_auto_rows = await conn.fetch(
            """
            SELECT id, server_id, article_id, term, weight, source, status,
                   suggested_by
            FROM law_synonyms
            WHERE article_id = ANY($1::int[]) AND source = 'auto'
            ORDER BY article_id, id
            FOR UPDATE
            """,
            article_ids,
        )
        pre_auto_by_id = {
            int(row["id"]): dict(row)
            for row in pre_auto_rows
        }
        pre_auto_by_article: dict[int, list[Mapping[str, Any]]] = defaultdict(
            list
        )
        for row in pre_auto_rows:
            pre_auto_by_article[int(row["article_id"])].append(dict(row))

        safe_ids: list[int] = []
        mismatches: list[dict[str, Any]] = []
        for row in rows:
            article_id = int(row["article_id"])
            synonym_id = int(row["synonym_id"])
            metadata = metadata_by_article.get(article_id)
            current_synonym = pre_auto_by_id.get(synonym_id)
            expected = {
                "server_id": str(row["server_id"]),
                "article_id": article_id,
                "term": str(row["term"]),
                "article_hash": str(row["article_hash"]),
                "source": str(row["source"]),
                "status": str(row["status"]),
                "weight": float(row["weight"]),
                "suggested_by": row["suggested_by"],
            }
            actual = {
                "server_id": (
                    current_synonym.get("server_id")
                    if current_synonym is not None
                    else None
                ),
                "article_id": (
                    current_synonym.get("article_id")
                    if current_synonym is not None
                    else None
                ),
                "term": (
                    current_synonym.get("term")
                    if current_synonym is not None
                    else None
                ),
                "article_hash": (
                    metadata.get("article_hash")
                    if metadata is not None
                    else None
                ),
                "source": (
                    current_synonym.get("source")
                    if current_synonym is not None
                    else None
                ),
                "status": (
                    current_synonym.get("status")
                    if current_synonym is not None
                    else None
                ),
                "weight": (
                    float(current_synonym["weight"])
                    if (
                        current_synonym is not None
                        and current_synonym.get("weight") is not None
                    )
                    else None
                ),
                "suggested_by": (
                    current_synonym.get("suggested_by")
                    if current_synonym is not None
                    else None
                ),
            }
            if (
                current_synonym is not None
                and actual == expected
                and expected["server_id"] == server_id
                and metadata is not None
                and metadata.get("server_id") == server_id
                and metadata.get("kind") == "article"
            ):
                safe_ids.append(synonym_id)
            else:
                mismatches.append({
                    "synonym_id": synonym_id,
                    "expected": expected,
                    "actual": actual,
                })

        try:
            foreign_auto_ids = foreign_auto_ownership_article_ids(
                server_id=server_id,
                auto_rows=(dict(row) for row in pre_auto_rows),
            )
        except ValueError as exc:
            raise RuntimeError(
                "rollback auto synonym ownership cannot be validated safely"
            ) from exc
        if foreign_auto_ids:
            mismatches.append({
                "reason": "foreign_auto_ownership",
                "article_ids": list(foreign_auto_ids),
            })

        trusted_output_ids: list[int] = []
        for article_id in article_ids:
            metadata = metadata_by_article.get(article_id)
            if (
                metadata is None
                or metadata.get("server_id") != server_id
                or metadata.get("kind") != "article"
            ):
                continue
            stored_output_hash = metadata.get("enrichment_output_hash")
            if stored_output_hash is None:
                continue
            try:
                current_output_hash = build_enrichment_output_hash(
                    server_id=server_id,
                    article_id=article_id,
                    title=metadata.get("title"),
                    title_provenance=metadata.get("title_provenance"),
                    penalty_summary=metadata.get("penalty_summary"),
                    penalty_json=metadata.get("penalty_json"),
                    auto_rows=pre_auto_by_article.get(article_id, ()),
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "rollback output cannot be fingerprinted safely for "
                    f"article {article_id}"
                ) from exc
            if current_output_hash != str(stored_output_hash):
                mismatches.append({
                    "reason": "output_mismatch",
                    "article_id": article_id,
                    "expected_output_hash": str(stored_output_hash),
                    "actual_output_hash": current_output_hash,
                })
                continue
            trusted_output_ids.append(article_id)

        deleted_ids: list[int] = []
        updated_output_article_ids: list[int] = []
        pending_document_ids: list[int] = []
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

            post_auto_rows = await conn.fetch(
                """
                SELECT id, server_id, article_id, term, weight, source, status,
                       suggested_by
                FROM law_synonyms
                WHERE article_id = ANY($1::int[]) AND source = 'auto'
                ORDER BY article_id, id
                FOR UPDATE
                """,
                article_ids,
            )
            try:
                post_foreign_auto_ids = foreign_auto_ownership_article_ids(
                    server_id=server_id,
                    auto_rows=(dict(row) for row in post_auto_rows),
                )
            except ValueError as exc:
                raise RuntimeError(
                    "post-rollback auto synonym ownership cannot be "
                    "validated safely"
                ) from exc
            if post_foreign_auto_ids:
                raise RuntimeError(
                    "post-rollback foreign-server auto synonym ownership for "
                    f"article ids: {list(post_foreign_auto_ids)}"
                )
            post_auto_by_article: dict[
                int, list[Mapping[str, Any]]
            ] = defaultdict(list)
            for row in post_auto_rows:
                post_auto_by_article[int(row["article_id"])].append(dict(row))
            for article_id in sorted(trusted_output_ids):
                metadata = metadata_by_article[article_id]
                try:
                    desired_output_hash = build_enrichment_output_hash(
                        server_id=server_id,
                        article_id=article_id,
                        title=metadata.get("title"),
                        title_provenance=metadata.get("title_provenance"),
                        penalty_summary=metadata.get("penalty_summary"),
                        penalty_json=metadata.get("penalty_json"),
                        auto_rows=post_auto_by_article.get(article_id, ()),
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "post-rollback output cannot be fingerprinted safely "
                        f"for article {article_id}"
                    ) from exc
                updated_id = await conn.fetchval(
                    """
                    UPDATE law_articles
                    SET enrichment_output_hash = $2
                    WHERE id = $1
                      AND server_id = $3
                      AND kind = 'article'
                      AND article_hash = $4
                      AND title IS NOT DISTINCT FROM $5
                      AND title_provenance IS NOT DISTINCT FROM $6
                      AND penalty_summary IS NOT DISTINCT FROM $7
                      AND penalty_json IS NOT DISTINCT FROM $8::jsonb
                      AND enrich_hash IS NOT DISTINCT FROM $9
                      AND enrichment_context_hash IS NOT DISTINCT FROM $10
                      AND enrichment_output_hash IS NOT DISTINCT FROM $11
                    RETURNING id
                    """,
                    article_id,
                    desired_output_hash,
                    server_id,
                    metadata.get("article_hash"),
                    metadata.get("title"),
                    metadata.get("title_provenance"),
                    metadata.get("penalty_summary"),
                    (
                        json.dumps(
                            normalize_penalty_json(
                                metadata.get("penalty_json")
                            ),
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        if metadata.get("penalty_json") is not None
                        else None
                    ),
                    metadata.get("enrich_hash"),
                    metadata.get("enrichment_context_hash"),
                    metadata.get("enrichment_output_hash"),
                )
                if updated_id != article_id:
                    raise RuntimeError(
                        "rollback output metadata CAS failed for article "
                        f"{article_id}"
                    )
                updated_output_article_ids.append(article_id)

            completion = await _evaluate_affected_completion(
                conn,
                server_id,
                article_ids,
            )
            pending_document_ids = sorted(
                int(document_id)
                for document_id in completion.incomplete_document_ids
            )
            if pending_document_ids:
                pending_rows = await conn.fetch(
                    """
                    UPDATE law_documents
                    SET derived_sync_pending = TRUE
                    WHERE server_id = $1 AND id = ANY($2::int[])
                    RETURNING id
                    """,
                    server_id,
                    pending_document_ids,
                )
                updated_pending_ids = sorted(
                    int(row["id"]) for row in pending_rows
                )
                if updated_pending_ids != pending_document_ids:
                    raise RuntimeError(
                        "rollback could not durably schedule every "
                        "incomplete document"
                    )

        return {
            "run_id": run_id,
            "server_id": server_id,
            "mode": "apply" if apply else "audit",
            "verified_synonym_ids": sorted(safe_ids),
            "deleted_synonym_ids": deleted_ids,
            "updated_output_article_ids": updated_output_article_ids,
            "pending_document_ids": pending_document_ids,
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
