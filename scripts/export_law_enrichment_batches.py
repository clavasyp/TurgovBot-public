#!/usr/bin/env python3
"""Export immutable source batches for reviewed full article enrichment.

The exporter is read-only.  It never calls a model and never writes to the
database.  Each source batch is an envelope that deliberately is *not* accepted
by ``_apply_claude_enrich.py`` as a ready artifact.  Codex reviews the source and
creates a separate JSON array with only:

``id``, ``article_hash``, ``title``, ``penalty_summary``, ``penalty``,
``synonyms``, ``generation_input_hash``.

The normal apply CLI then validates generation-time IDs/hashes and writes through
the shared catalog/synonym service path.  A Phase A rerun verifies that every
article is current before it proceeds to FRIDA and HTTP smoke.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from scripts.backfill_law_servers import PhaseAError, validate_explicit_targets
from services.law_backfill_lock import law_backfill_global_lock
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
    enrichment_generation_source,
)
from services.law_source_snapshot import law_source_fingerprint


EXPORT_SCHEMA = "law-full-enrichment-export-v1"
BATCH_SCHEMA = "law-full-enrichment-source-batch-v1"
REVIEWED_FIELDS = (
    "id",
    "article_hash",
    "title",
    "penalty_summary",
    "penalty",
    "synonyms",
    "generation_input_hash",
)


def _payload_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _with_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    result = dict(value)
    result[field] = _payload_hash(value)
    return result


async def _build_export_snapshot(
    conn,
    server_id: str,
    *,
    batch_size: int,
    article_ids: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    """Build one transactionally consistent, read-only export payload."""

    source_rows = await conn.fetch(
        """
        SELECT id, server_id, source_thread_id, title, content_hash,
               derived_sync_pending
        FROM law_documents
        WHERE server_id = $1
        ORDER BY id
        """,
        server_id,
    )
    if not source_rows:
        raise PhaseAError(f"{server_id}: no law_documents to export")
    pending_documents = [
        int(row["id"])
        for row in source_rows
        if bool(row["derived_sync_pending"])
    ]
    if pending_documents:
        raise PhaseAError(
            f"{server_id}: source documents are still pending: {pending_documents}"
        )

    requested_article_ids: Optional[list[int]] = None
    if article_ids is not None:
        requested_article_ids = sorted({int(article_id) for article_id in article_ids})
        if not requested_article_ids or any(
            article_id <= 0 for article_id in requested_article_ids
        ):
            raise PhaseAError("--article-id values must be positive integers")
        article_rows = await conn.fetch(
            """
            SELECT id, law_document_id, source_order, article_hash, code, title,
                   doc_title, full_text, penalty_summary, penalty_json
            FROM law_articles
            WHERE server_id = $1
              AND kind = 'article'
              AND id = ANY($2::int[])
            ORDER BY law_document_id, source_order, id
            """,
            server_id,
            requested_article_ids,
        )
        selected_ids = {int(row["id"]) for row in article_rows}
        missing_ids = sorted(set(requested_article_ids) - selected_ids)
        if missing_ids:
            raise PhaseAError(
                f"{server_id}: requested article IDs do not belong to an "
                f"article on this server: {missing_ids}"
            )
    else:
        article_rows = await conn.fetch(
            """
            SELECT id, law_document_id, source_order, article_hash, code, title,
                   doc_title, full_text, penalty_summary, penalty_json
            FROM law_articles
            WHERE server_id = $1
              AND kind = 'article'
              AND enrich_hash IS DISTINCT FROM article_hash
            ORDER BY law_document_id, source_order, id
            """,
            server_id,
        )
    article_ids = [int(row["id"]) for row in article_rows]
    synonym_rows = []
    if article_ids:
        synonym_rows = await conn.fetch(
            """
            SELECT article_id, term
            FROM law_synonyms
            WHERE server_id = $1
              AND article_id = ANY($2::int[])
              AND source = 'auto'
              AND status = 'approved'
            ORDER BY article_id, term
            """,
            server_id,
            article_ids,
        )
    synonyms_by_article: dict[int, list[str]] = {}
    for row in synonym_rows:
        synonyms_by_article.setdefault(int(row["article_id"]), []).append(
            str(row["term"])
        )

    articles: list[dict[str, Any]] = []
    for row in article_rows:
        article_id = int(row["id"])
        article = dict(row)
        auto_synonyms = synonyms_by_article.get(article_id, [])
        source = enrichment_generation_source(article, auto_synonyms)
        articles.append(
            {
                "id": article_id,
                "article_hash": str(row["article_hash"]),
                "generation_input_hash": enrichment_generation_input_hash(
                    article, auto_synonyms
                ),
                "source": source,
                "review": {
                    "status": "pending_codex_review",
                    "required_output_fields": list(REVIEWED_FIELDS),
                },
            }
        )

    source_fingerprint = law_source_fingerprint(
        [dict(row) for row in source_rows]
    )
    batches: list[dict[str, Any]] = []
    for offset in range(0, len(articles), batch_size):
        batch_number = len(batches) + 1
        body = {
            "schema": BATCH_SCHEMA,
            "server_id": server_id,
            "source_fingerprint": source_fingerprint,
            "batch_number": batch_number,
            "articles": articles[offset : offset + batch_size],
        }
        batches.append(_with_hash(body, "batch_hash"))

    manifest_body = {
        "schema": EXPORT_SCHEMA,
        "server_id": server_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": source_fingerprint,
        "batch_size": batch_size,
        "pending_articles": len(articles),
        "selection_mode": (
            "explicit_articles"
            if requested_article_ids is not None
            else "pending_enrichment"
        ),
        "requested_article_ids": requested_article_ids,
        "instructions": {
            "review": (
                "Codex must inspect every source article and create a separate "
                "reviewed JSON array. Do not edit source identity fields."
            ),
            "reviewed_artifact_fields": list(REVIEWED_FIELDS),
            "apply_command_template": (
                "python /app/scripts/_apply_claude_enrich.py "
                f"{server_id} REVIEWED_BATCH.json"
            ),
            "resume_command": (
                f"python /app/scripts/backfill_law_servers.py {server_id}"
            ),
        },
        "batches": [
            {
                "file": f"source-batch-{index:05d}.json",
                "batch_hash": batch["batch_hash"],
                "articles": len(batch["articles"]),
                "article_ids": [item["id"] for item in batch["articles"]],
            }
            for index, batch in enumerate(batches, 1)
        ],
    }
    return {
        "manifest": _with_hash(manifest_body, "manifest_hash"),
        "batches": batches,
    }


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(encoded + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_export(output_dir: Path, payload: Mapping[str, Any]) -> Path:
    """Write a new export directory without overwriting any prior review."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    for index, batch in enumerate(payload["batches"], 1):
        _write_json_exclusive(
            output_dir / f"source-batch-{index:05d}.json",
            batch,
        )
    manifest_path = output_dir / "manifest.json"
    _write_json_exclusive(manifest_path, payload["manifest"])
    return manifest_path


async def export_server(
    server_id: str,
    *,
    output_dir: Path,
    batch_size: int,
    article_ids: Optional[Sequence[int]] = None,
) -> dict[str, Any]:
    """Freeze source/catalog writers while taking a read-only DB snapshot."""

    async with law_backfill_global_lock(manager=db_manager):
        async with law_document_server_lock(server_id, manager=db_manager):
            async with catalog_server_lock(server_id):
                async with db_manager.connection() as conn:
                    async with conn.transaction(readonly=True):
                        payload = await _build_export_snapshot(
                            conn,
                            server_id,
                            batch_size=batch_size,
                            article_ids=article_ids,
                        )
    manifest_path = write_export(output_dir, payload)
    return {
        "manifest": str(manifest_path),
        "pending_articles": payload["manifest"]["pending_articles"],
        "batches": len(payload["batches"]),
        "source_fingerprint": payload["manifest"]["source_fingerprint"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only source exporter for Codex-reviewed enrichment"
    )
    parser.add_argument("server_id")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=15)
    parser.add_argument(
        "--article-id",
        type=int,
        action="append",
        dest="article_ids",
        help=(
            "export this existing article for a reviewed correction even when "
            "its enrich_hash is current; repeat for multiple IDs"
        ),
    )
    return parser


async def _async_main(args: argparse.Namespace) -> None:
    server_id = validate_explicit_targets([args.server_id])[0]
    if args.batch_size <= 0 or args.batch_size > 100:
        raise PhaseAError("--batch-size must be between 1 and 100")
    await db_manager.connect()
    try:
        result = await export_server(
            server_id,
            output_dir=args.output_dir,
            batch_size=args.batch_size,
            article_ids=args.article_ids,
        )
    finally:
        await db_manager.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(_async_main(args))
    except Exception as exc:
        raise SystemExit(f"EXPORT REFUSED: {exc}") from exc


if __name__ == "__main__":
    main()
