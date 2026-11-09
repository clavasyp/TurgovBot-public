#!/usr/bin/env python3
"""Export immutable source batches for reviewed full article enrichment.

The exporter is read-only.  It never calls a model and never writes to the
database.  Each source batch is an envelope that deliberately is *not* accepted
by ``_apply_claude_enrich.py`` as a ready artifact.  Codex reviews the source and
creates a separate JSON array with only:

``id``, ``article_hash``, ``title``, ``penalty_summary``, ``penalty``,
``synonyms``, ``generation_input_hash``.

The aggregate builder turns those reviewed arrays into a versioned native-v3
apply envelope.  In addition to generation-time IDs/hashes, that envelope
binds the complete server source fingerprint, its parser-replayed catalog, and
every target's source-only enrichment context hash.  A Phase A rerun verifies
that every article is current before it proceeds to FRIDA and HTTP smoke.
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
from services.law_catalog_service import (
    derive_catalog_fields,
    source_article_hash,
)
from services.law_backfill_lock import law_backfill_global_lock
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
    enrichment_generation_source,
)
from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LawCatalogTruthError,
    load_verified_law_catalog_truth,
)
from services.law_enrichment_review_contract import (
    ReviewSourceContractError,
    build_review_source_projection,
    review_source_projection_atomic_units,
    select_review_source_projection,
)
from services.law_synonym_service import (
    SynonymWriteError,
    canonical_synonym_term,
    is_explicitly_empty_or_repealed_article,
    load_auto_replacement_generation_state_for_targets_in_transaction,
)


EXPORT_SCHEMA_V1 = "law-full-enrichment-export-v1"
BATCH_SCHEMA_V1 = "law-full-enrichment-source-batch-v1"
EXPORT_SCHEMA = "law-full-enrichment-export-v3"
BATCH_SCHEMA = "law-full-enrichment-source-batch-v3"
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

    try:
        verified_truth = await load_verified_law_catalog_truth(
            conn,
            server_id,
            lock_rows=False,
        )
    except LawCatalogTruthError as exc:
        raise PhaseAError(
            f"{server_id}: source documents/catalog are not current; run "
            "normal catalog reconcile before export"
        ) from exc
    source_fingerprint = verified_truth.source_fingerprint

    requested_article_ids: Optional[list[int]] = None
    try:
        if article_ids is not None:
            if (
                isinstance(article_ids, (str, bytes))
                or not isinstance(article_ids, Sequence)
                or not article_ids
                or any(
                    isinstance(article_id, bool)
                    or not isinstance(article_id, int)
                    or article_id <= 0
                    for article_id in article_ids
                )
            ):
                raise PhaseAError(
                    "--article-id values must be positive integers"
                )
            requested_article_ids = sorted(set(article_ids))
            generation_state = (
                await load_auto_replacement_generation_state_for_targets_in_transaction(
                    conn,
                    server_id,
                    requested_article_ids,
                    lock_articles=False,
                )
            )
        else:
            from services.law_enrichment_completion import (
                evaluate_law_enrichment_completion,
            )

            try:
                completion = await evaluate_law_enrichment_completion(
                    conn,
                    server_id,
                )
            except Exception as exc:
                raise PhaseAError(
                    f"{server_id}: cannot prove authoritative enrichment "
                    "completion"
                ) from exc
            authoritative_pending_ids = list(
                completion.incomplete_article_ids
            )
            if not authoritative_pending_ids:
                raise PhaseAError(
                    f"{server_id}: no articles require a reviewed export"
                )
            generation_state = (
                await load_auto_replacement_generation_state_for_targets_in_transaction(
                    conn,
                    server_id,
                    authoritative_pending_ids,
                    lock_articles=False,
                )
            )
    except SynonymWriteError as exc:
        raise PhaseAError(
            f"{server_id}: cannot capture safe enrichment generation state: "
            f"{exc}"
        ) from exc
    if (
        generation_state["snapshot"].get("source_catalog_binding")
        != verified_truth.binding
    ):
        raise PhaseAError(
            f"{server_id}: source/catalog truth changed while building the "
            "export snapshot"
        )

    selected_ids = list(
        generation_state["snapshot"]["required_target_ids"]
    )
    if not selected_ids:
        raise PhaseAError(
            f"{server_id}: no articles require a reviewed export"
        )
    articles_by_id = {
        int(row["id"]): row
        for row in generation_state["articles"]
    }
    missing_ids = sorted(set(selected_ids) - set(articles_by_id))
    if missing_ids:
        raise PhaseAError(
            f"{server_id}: generation snapshot references missing articles: "
            f"{missing_ids}"
        )
    article_rows = sorted(
        (articles_by_id[article_id] for article_id in selected_ids),
        key=lambda row: (
            int(row["law_document_id"]),
            int(row["source_order"]),
            int(row["id"]),
        ),
    )
    if requested_article_ids is not None:
        missing_requested = sorted(
            set(requested_article_ids) - set(selected_ids)
        )
        if missing_requested:
            raise PhaseAError(
                f"{server_id}: requested article IDs do not belong to an "
                f"article on this server: {missing_requested}"
            )
    synonyms_by_article: dict[int, list[str]] = {}
    for row in generation_state["auto_rows"]:
        if row["status"] != "approved":
            continue
        synonyms_by_article.setdefault(int(row["article_id"]), []).append(
            str(row["term"])
        )
    context_hashes = generation_state["context_hashes"]
    context_contract = {
        "algorithm_version": ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
        "generation_policy_version": ENRICHMENT_GENERATION_POLICY_VERSION,
        "source_title_derivation_policy_version": (
            SOURCE_TITLE_DERIVATION_POLICY_VERSION
        ),
        "catalog_parser_contract_version": (
            LAW_CATALOG_PARSER_CONTRACT_VERSION
        ),
    }
    try:
        review_source_projection = build_review_source_projection(
            generation_state["catalog_rows"],
            selected_ids,
            catalog_fields_resolver=derive_catalog_fields,
            article_hash_resolver=source_article_hash,
            is_tombstone=is_explicitly_empty_or_repealed_article,
            canonicalize_scope=canonical_synonym_term,
        )
        atomic_units = review_source_projection_atomic_units(
            review_source_projection
        )
    except ReviewSourceContractError as exc:
        raise PhaseAError(
            f"{server_id}: cannot build safe reviewed-source projection: "
            f"{exc}"
        ) from exc

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
                "generation_context_hash": context_hashes[article_id],
                "source": source,
                "review": {
                    "status": "pending_codex_review",
                    "required_output_fields": list(REVIEWED_FIELDS),
                },
            }
        )

    articles_by_id = {
        int(article["id"]): article
        for article in articles
    }
    batch_article_ids: list[list[int]] = []
    current_batch: list[int] = []
    for unit in atomic_units:
        if len(unit) > batch_size:
            raise PhaseAError(
                f"{server_id}: exact-body group of {len(unit)} articles "
                f"exceeds --batch-size {batch_size}; increase the batch size "
                "instead of splitting the group"
            )
        if current_batch and len(current_batch) + len(unit) > batch_size:
            batch_article_ids.append(current_batch)
            current_batch = []
        current_batch.extend(unit)
    if current_batch:
        batch_article_ids.append(current_batch)

    batches: list[dict[str, Any]] = []
    for article_ids_in_batch in batch_article_ids:
        batch_number = len(batches) + 1
        try:
            batch_projection = select_review_source_projection(
                review_source_projection,
                article_ids_in_batch,
            )
        except ReviewSourceContractError as exc:
            raise PhaseAError(
                f"{server_id}: cannot build exact-group-safe batch "
                f"{batch_number}: {exc}"
            ) from exc
        body = {
            "schema": BATCH_SCHEMA,
            "server_id": server_id,
            "source_fingerprint": source_fingerprint,
            "source_catalog_binding": verified_truth.binding,
            "context_contract": context_contract,
            "batch_number": batch_number,
            "review_source_projection": batch_projection,
            "articles": [
                articles_by_id[article_id]
                for article_id in article_ids_in_batch
            ],
        }
        batches.append(_with_hash(body, "batch_hash"))

    exported_article_ids = [
        int(article["id"])
        for batch in batches
        for article in batch["articles"]
    ]
    manifest_body = {
        "schema": EXPORT_SCHEMA,
        "server_id": server_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": source_fingerprint,
        "source_catalog_binding": verified_truth.binding,
        "context_contract": context_contract,
        "review_source_projection_hash": (
            review_source_projection["projection_hash"]
        ),
        "generation_snapshot": generation_state["snapshot"],
        "batch_size": batch_size,
        "pending_articles": len(articles),
        "selection_mode": (
            "explicit_articles"
            if requested_article_ids is not None
            else "pending_enrichment"
        ),
        "requested_article_ids": requested_article_ids,
        "exported_article_ids": exported_article_ids,
        "instructions": {
            "review": (
                "Codex must inspect every source article and create a separate "
                "reviewed JSON array. Do not edit source identity fields."
            ),
            "reviewed_artifact_fields": list(REVIEWED_FIELDS),
            "apply_command_template": (
                "python /app/scripts/_apply_claude_enrich.py "
                f"{server_id} REVIEWED_ALL_ENVELOPE.json"
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
                    async with conn.transaction(
                        isolation="repeatable_read",
                        readonly=True,
                    ):
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
