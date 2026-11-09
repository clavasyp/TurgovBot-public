#!/usr/bin/env python3
"""Safely attest historical law-enrichment metadata.

The migration introducing ``title_provenance`` and
``enrichment_context_hash`` cannot infer trusted historical state by itself.
This versioned, deterministic workflow classifies title ownership from
PostgreSQL evidence without inventing a missing generation-time context.
It never calls Gemini, Vertex, FRIDA, HTTP, GCS, or another network service.

Dry-run is the default.  ``--apply`` is required for a write.  Every server is
processed in one transaction under the normal law lock hierarchy.  A generated
title is trusted only when an append-only reviewed-artifact commit proves the
exact current post-write generation state.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from services.law_backfill_lock import law_backfill_global_lock
from services.law_catalog_service import (
    TITLE_PROVENANCE_GENERATED,
    TITLE_PROVENANCE_LEGACY_UNKNOWN,
    TITLE_PROVENANCE_SOURCE,
    derive_catalog_fields,
)
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
    normalize_penalty_json,
)
from services.law_synonym_service import (
    SynonymWriteError,
    acquire_synonym_write_lock,
    is_explicitly_empty_or_repealed_article,
    load_enrichment_state_for_attestation_in_transaction,
)


ATTESTATION_SCHEMA = "law-enrichment-state-attestation-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_VALUES = {
    TITLE_PROVENANCE_SOURCE,
    TITLE_PROVENANCE_GENERATED,
    TITLE_PROVENANCE_LEGACY_UNKNOWN,
}
_COUNT_FIELDS = (
    "catalog_rows",
    "article_rows",
    "enrichment_current",
    "enrichment_stale",
    "title_source",
    "title_generated",
    "title_null",
    "title_legacy_unknown",
    "generation_evidence_matched",
    "provenance_changes",
    "context_unknown_requires_regeneration",
    "context_already_current",
    "context_stale",
    "context_skipped_stale",
    "would_update",
    "updated",
)


class AttestationError(RuntimeError):
    """Historical state cannot be attested without guessing."""


def _as_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


def _validated_server_id(value: Any) -> str:
    server_id = str(value or "").strip()
    if not server_id:
        raise AttestationError("server_id must not be empty")
    return server_id


def _is_positive_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def _approved_auto_terms(
    generation_state: Mapping[str, Any],
) -> dict[int, list[str]]:
    terms: dict[int, list[str]] = defaultdict(list)
    for row in generation_state.get("auto_rows", ()):
        if (
            str(row.get("source") or "") == "auto"
            and str(row.get("status") or "") == "approved"
        ):
            terms[row["article_id"]].append(str(row["term"]))
    for article_terms in terms.values():
        article_terms.sort()
    return dict(terms)


def _valid_evidence_keys(
    rows: Iterable[Mapping[str, Any]],
) -> set[tuple[str, int, str, str]]:
    """Return only internally well-formed durable evidence ownership keys."""

    result: set[tuple[str, int, str, str]] = set()
    for raw in rows:
        row = _as_dict(raw)
        article_hash = str(row.get("article_hash") or "")
        generation_hash = str(row.get("generation_input_hash") or "")
        artifact_hash = str(row.get("artifact_hash") or "")
        post_hash = str(row.get("post_commit_generation_hash") or "")
        if not (
            article_hash
            and _SHA256_RE.fullmatch(generation_hash)
            and _SHA256_RE.fullmatch(artifact_hash)
            and _SHA256_RE.fullmatch(post_hash)
        ):
            continue
        article_id = row.get("article_id")
        if not _is_positive_int(article_id):
            continue
        result.add(
            (
                str(row.get("server_id") or ""),
                article_id,
                article_hash,
                post_hash,
            )
        )
    return result


def _same_generation_catalog_row(
    captured: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    fields = (
        "id",
        "law_document_id",
        "source_order",
        "article_ref",
        "kind",
        "code",
        "title",
        "doc_title",
        "full_text",
        "article_hash",
        "enrich_hash",
        "title_provenance",
        "enrichment_context_hash",
        "enrichment_output_hash",
    )
    return all(captured.get(field) == current.get(field) for field in fields)


def _classify_title_provenance(
    article: Mapping[str, Any],
    *,
    server_id: str,
    current_generation_hash: str,
    evidence_keys: set[tuple[str, int, str, str]],
) -> tuple[str | None, bool]:
    """Classify one title without trusting its historical provenance label."""

    derived_kind, derived_code, source_title = derive_catalog_fields(
        article.get("article_ref"),
        str(article.get("full_text") or ""),
    )
    if (
        derived_kind != str(article.get("kind") or "")
        or derived_code != article.get("code")
    ):
        raise AttestationError(
            f"{server_id}: article {article.get('id')} parser fields differ "
            "from the catalog; run normal catalog reconcile first"
        )

    title = article.get("title")
    if is_explicitly_empty_or_repealed_article(article.get("full_text")):
        if title is not None or article.get("title_provenance") is not None:
            raise AttestationError(
                f"{server_id}: article {article.get('id')} is a dirty "
                "tombstone; run normal catalog reconcile first"
            )
        return None, False
    if source_title is not None:
        if title != source_title:
            raise AttestationError(
                f"{server_id}: article {article.get('id')} source-owned title "
                "differs from the catalog; run normal catalog reconcile first"
            )
        return TITLE_PROVENANCE_SOURCE, False
    if title is None:
        return None, False

    evidence_key = (
        server_id,
        int(article["id"]),
        str(article["article_hash"]),
        current_generation_hash,
    )
    if evidence_key in evidence_keys:
        return TITLE_PROVENANCE_GENERATED, True
    return TITLE_PROVENANCE_LEGACY_UNKNOWN, False


def _plan_server_attestation(
    server_id: str,
    *,
    catalog_rows: Sequence[Mapping[str, Any]],
    articles: Sequence[Mapping[str, Any]],
    auto_rows: Sequence[Mapping[str, Any]],
    approved_synonym_counts: Mapping[int, int],
    context_hashes: Mapping[int, str],
    evidence_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Create a deterministic metadata-only plan from one transaction view."""

    server_id = _validated_server_id(server_id)
    if not isinstance(approved_synonym_counts, Mapping):
        raise AttestationError(
            f"{server_id}: approved synonym counts must be a mapping"
        )
    article_by_id: dict[int, dict[str, Any]] = {}
    for raw in articles:
        article = _as_dict(raw)
        article_id = article.get("id")
        if not _is_positive_int(article_id):
            raise AttestationError(
                f"{server_id}: article has invalid integer ownership"
            )
        if article_id in article_by_id:
            raise AttestationError(
                f"{server_id}: duplicate article id {article_id}"
            )
        if str(article.get("server_id") or "") != server_id:
            raise AttestationError(
                f"{server_id}: article {article_id} has foreign ownership"
            )
        if str(article.get("kind") or "") != "article":
            raise AttestationError(
                f"{server_id}: non-article row {article_id} entered attestation"
            )
        old_provenance = article.get("title_provenance")
        if (
            old_provenance is not None
            and old_provenance not in _PROVENANCE_VALUES
        ):
            raise AttestationError(
                f"{server_id}: article {article_id} has invalid title provenance"
            )
        old_context = article.get("enrichment_context_hash")
        if (
            old_context is not None
            and _SHA256_RE.fullmatch(str(old_context)) is None
        ):
            raise AttestationError(
                f"{server_id}: article {article_id} has invalid context hash"
            )
        old_output = article.get("enrichment_output_hash")
        if (
            old_output is not None
            and _SHA256_RE.fullmatch(str(old_output)) is None
        ):
            raise AttestationError(
                f"{server_id}: article {article_id} has invalid output hash"
            )
        article_by_id[article_id] = article

    if not isinstance(context_hashes, Mapping):
        raise AttestationError(
            f"{server_id}: computed contexts must be a mapping"
        )
    normalized_contexts: dict[int, str] = {}
    for raw_article_id, value in context_hashes.items():
        if not _is_positive_int(raw_article_id):
            raise AttestationError(
                f"{server_id}: computed context has invalid article ownership"
            )
        context_article_id = raw_article_id
        if context_article_id in normalized_contexts:
            raise AttestationError(
                f"{server_id}: computed context has duplicate article ownership"
            )
        if not isinstance(value, str):
            raise AttestationError(
                f"{server_id}: article {context_article_id} has invalid "
                "computed context"
            )
        normalized_contexts[context_article_id] = value
    context_ids = set(normalized_contexts)
    if context_ids != set(article_by_id):
        missing = sorted(set(article_by_id) - context_ids)
        unexpected = sorted(context_ids - set(article_by_id))
        raise AttestationError(
            f"{server_id}: context ownership mismatch; "
            f"missing={missing}, unexpected={unexpected}"
        )
    for article_id, value in normalized_contexts.items():
        if _SHA256_RE.fullmatch(value) is None:
            raise AttestationError(
                f"{server_id}: article {article_id} has invalid computed context"
            )

    for raw in auto_rows:
        auto_row = _as_dict(raw)
        auto_article_id = auto_row.get("article_id")
        if not _is_positive_int(auto_article_id):
            raise AttestationError(
                f"{server_id}: auto synonym has invalid article ownership"
            )
        if (
            str(auto_row.get("server_id") or "") != server_id
            or auto_article_id not in article_by_id
            or str(auto_row.get("source") or "") != "auto"
        ):
            raise AttestationError(
                f"{server_id}: auto synonym has foreign ownership"
            )
    generation_state = {"auto_rows": auto_rows}
    auto_terms = _approved_auto_terms(generation_state)
    normalized_approved_counts: dict[int, int] = {}
    for raw_article_id, raw_count in approved_synonym_counts.items():
        if (
            not isinstance(raw_article_id, int)
            or isinstance(raw_article_id, bool)
            or raw_article_id not in article_by_id
            or not isinstance(raw_count, int)
            or isinstance(raw_count, bool)
            or raw_count <= 0
        ):
            raise AttestationError(
                f"{server_id}: approved synonym counts have invalid ownership"
            )
        if raw_article_id in normalized_approved_counts:
            raise AttestationError(
                f"{server_id}: approved synonym counts have duplicate ownership"
            )
        normalized_approved_counts[raw_article_id] = raw_count
    for article_id, article_terms in auto_terms.items():
        if normalized_approved_counts.get(article_id, 0) < len(article_terms):
            raise AttestationError(
                f"{server_id}: approved synonym snapshot is inconsistent for "
                f"article {article_id}"
            )
    evidence_keys = _valid_evidence_keys(evidence_rows)
    counts = {field: 0 for field in _COUNT_FIELDS}
    counts["catalog_rows"] = len(catalog_rows)
    counts["article_rows"] = len(article_by_id)
    changed_ids: list[int] = []
    generated_ids: list[int] = []
    legacy_unknown_ids: list[int] = []
    stale_enrichment_ids: list[int] = []
    context_stale_ids: list[int] = []
    plan: list[dict[str, Any]] = []

    for article_id in sorted(article_by_id):
        article = article_by_id[article_id]
        article_auto_terms = auto_terms.get(article_id, ())
        if (
            is_explicitly_empty_or_repealed_article(
                article.get("full_text")
            )
            and (
                article.get("title") is not None
                or article.get("title_provenance") is not None
                or article.get("penalty_summary") is not None
                or article.get("penalty_json") is not None
                or normalized_approved_counts.get(article_id, 0) > 0
            )
        ):
            raise AttestationError(
                f"{server_id}: article {article_id} is a dirty tombstone; "
                "run normal catalog reconcile first"
            )
        try:
            current_generation_hash = enrichment_generation_input_hash(
                article,
                article_auto_terms,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AttestationError(
                f"{server_id}: article {article_id} generation state is invalid"
            ) from exc

        desired_provenance, evidence_matched = (
            _classify_title_provenance(
                article,
                server_id=server_id,
                current_generation_hash=current_generation_hash,
                evidence_keys=evidence_keys,
            )
        )
        if desired_provenance == TITLE_PROVENANCE_SOURCE:
            counts["title_source"] += 1
        elif desired_provenance == TITLE_PROVENANCE_GENERATED:
            counts["title_generated"] += 1
            generated_ids.append(article_id)
        elif desired_provenance == TITLE_PROVENANCE_LEGACY_UNKNOWN:
            counts["title_legacy_unknown"] += 1
            legacy_unknown_ids.append(article_id)
        else:
            counts["title_null"] += 1
        if evidence_matched:
            counts["generation_evidence_matched"] += 1

        enrichment_current = (
            article.get("enrich_hash") is not None
            and article.get("enrich_hash") == article.get("article_hash")
        )
        old_context = article.get("enrichment_context_hash")
        current_context = normalized_contexts[article_id]
        if enrichment_current:
            counts["enrichment_current"] += 1
            if old_context is None:
                # Historical evidence records the article/post-write state,
                # but not the generation-time ancestry, neighbours, exact
                # group topology or source-backed competitors.  Treating the
                # current digest as historical truth could permanently hide
                # an already-existing structural drift.  Keep NULL unknown;
                # the ordinary stale selector will regenerate only ownership
                # states that are safe to replace.
                desired_context = None
                counts["context_unknown_requires_regeneration"] += 1
            elif str(old_context) == current_context:
                desired_context = str(old_context)
                counts["context_already_current"] += 1
            else:
                # Never erase the structural stale signal on a rerun.
                desired_context = str(old_context)
                counts["context_stale"] += 1
                context_stale_ids.append(article_id)
        else:
            counts["enrichment_stale"] += 1
            counts["context_skipped_stale"] += 1
            stale_enrichment_ids.append(article_id)
            desired_context = (
                str(old_context) if old_context is not None else None
            )

        provenance_changed = (
            article.get("title_provenance") != desired_provenance
        )
        if provenance_changed:
            counts["provenance_changes"] += 1
        desired_output_hash = (
            None
            if provenance_changed
            else article.get("enrichment_output_hash")
        )
        changed = (
            provenance_changed
            or article.get("enrichment_context_hash") != desired_context
            or article.get("enrichment_output_hash") != desired_output_hash
        )
        if changed:
            counts["would_update"] += 1
            changed_ids.append(article_id)
        plan.append(
            {
                "article": article,
                "desired_title_provenance": desired_provenance,
                "desired_enrichment_context_hash": desired_context,
                "desired_enrichment_output_hash": desired_output_hash,
                "changed": changed,
            }
        )

    report = {
        "schema": ATTESTATION_SCHEMA,
        "server_id": server_id,
        **counts,
        "changed_article_ids": changed_ids,
        "generated_article_ids": generated_ids,
        "legacy_unknown_article_ids": legacy_unknown_ids,
        "stale_enrichment_article_ids": stale_enrichment_ids,
        "context_stale_article_ids": context_stale_ids,
        "context_unknown_article_ids": [
            int(item["article"]["id"])
            for item in plan
            if (
                item["article"].get("enrich_hash")
                == item["article"].get("article_hash")
                and item["article"].get("enrichment_context_hash") is None
            )
        ],
    }
    return plan, report


def _penalty_json_argument(value: Any) -> str | None:
    normalized = normalize_penalty_json(value)
    if normalized is None:
        return None
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _load_full_article_rows(
    conn: Any,
    server_id: str,
    *,
    lock_rows: bool,
) -> list[dict[str, Any]]:
    lock_clause = "FOR UPDATE" if lock_rows else ""
    rows = await conn.fetch(
        f"""
        SELECT id, server_id, law_document_id, source_order, article_ref,
               kind, code, title, doc_title, full_text, article_hash,
               enrich_hash, penalty_summary, penalty_json, title_provenance,
               enrichment_context_hash, enrichment_output_hash, updated_at
        FROM law_articles
        WHERE server_id = $1 AND kind = 'article'
        ORDER BY id
        {lock_clause}
        """,
        server_id,
    )
    return [_as_dict(row) for row in rows]


async def _load_artifact_evidence(
    conn: Any,
    server_id: str,
    article_ids: Sequence[int],
    *,
    lock_rows: bool,
) -> list[dict[str, Any]]:
    if not article_ids:
        return []
    lock_clause = "FOR SHARE" if lock_rows else ""
    rows = await conn.fetch(
        f"""
        SELECT server_id, article_id, article_hash, generation_input_hash,
               artifact_hash, post_commit_generation_hash
        FROM law_enrichment_artifact_commits
        WHERE server_id = $1
          AND article_id = ANY($2::int[])
        ORDER BY article_id, id
        {lock_clause}
        """,
        server_id,
        list(article_ids),
    )
    return [_as_dict(row) for row in rows]


async def _cas_update_article(
    conn: Any,
    server_id: str,
    item: Mapping[str, Any],
) -> None:
    article = item["article"]
    article_id = int(article["id"])
    updated_id = await conn.fetchval(
        """
        UPDATE law_articles
        SET title_provenance = $1,
            enrichment_output_hash = $2
        WHERE id = $3
          AND server_id = $4
          AND law_document_id = $5
          AND source_order = $6
          AND article_ref IS NOT DISTINCT FROM $7
          AND kind = $8
          AND code IS NOT DISTINCT FROM $9
          AND title IS NOT DISTINCT FROM $10
          AND doc_title IS NOT DISTINCT FROM $11
          AND full_text IS NOT DISTINCT FROM $12
          AND article_hash = $13
          AND enrich_hash IS NOT DISTINCT FROM $14
          AND penalty_summary IS NOT DISTINCT FROM $15
          AND penalty_json IS NOT DISTINCT FROM $16::jsonb
          AND title_provenance IS NOT DISTINCT FROM $17
          AND enrichment_context_hash IS NOT DISTINCT FROM $18
          AND enrichment_output_hash IS NOT DISTINCT FROM $19
          AND updated_at IS NOT DISTINCT FROM $20
        RETURNING id
        """,
        item["desired_title_provenance"],
        item["desired_enrichment_output_hash"],
        article_id,
        server_id,
        int(article["law_document_id"]),
        int(article["source_order"]),
        article.get("article_ref"),
        str(article["kind"]),
        article.get("code"),
        article.get("title"),
        article.get("doc_title"),
        article.get("full_text"),
        str(article["article_hash"]),
        article.get("enrich_hash"),
        article.get("penalty_summary"),
        _penalty_json_argument(article.get("penalty_json")),
        article.get("title_provenance"),
        article.get("enrichment_context_hash"),
        article.get("enrichment_output_hash"),
        article.get("updated_at"),
    )
    if updated_id != article_id:
        raise AttestationError(
            f"{server_id}: article {article_id} failed full metadata CAS"
        )


async def _attest_one_server(server_id: str, *, apply: bool) -> dict[str, Any]:
    server_id = _validated_server_id(server_id)
    async with law_document_server_lock(server_id):
        async with catalog_server_lock(server_id):
            async with db_manager.transaction() as conn:
                if not apply:
                    await conn.execute("SET TRANSACTION READ ONLY")
                await acquire_synonym_write_lock(conn, server_id)
                pending = await conn.fetchval(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM law_documents
                      WHERE server_id = $1
                        AND derived_sync_pending IS TRUE
                    )
                    """,
                    server_id,
                )
                if pending:
                    raise AttestationError(
                        f"{server_id}: unfinished derived document sync"
                    )

                generation_state = (
                    await load_enrichment_state_for_attestation_in_transaction(
                        conn,
                        server_id,
                        lock_articles=apply,
                    )
                )
                if not generation_state.get("catalog_rows"):
                    raise AttestationError(
                        f"{server_id}: no law catalog rows found"
                    )
                articles = await _load_full_article_rows(
                    conn,
                    server_id,
                    lock_rows=apply,
                )
                captured_articles = {
                    int(row["id"]): _as_dict(row)
                    for row in generation_state.get("articles", ())
                }
                current_articles = {
                    int(row["id"]): row for row in articles
                }
                if set(captured_articles) != set(current_articles):
                    raise AttestationError(
                        f"{server_id}: article ownership changed inside snapshot"
                    )
                inconsistent = [
                    article_id
                    for article_id in sorted(current_articles)
                    if not _same_generation_catalog_row(
                        captured_articles[article_id],
                        current_articles[article_id],
                    )
                ]
                if inconsistent:
                    raise AttestationError(
                        f"{server_id}: catalog changed inside snapshot for "
                        f"article ids {inconsistent}"
                    )

                evidence_rows = await _load_artifact_evidence(
                    conn,
                    server_id,
                    sorted(current_articles),
                    lock_rows=apply,
                )
                plan, report = _plan_server_attestation(
                    server_id,
                    catalog_rows=generation_state["catalog_rows"],
                    articles=articles,
                    auto_rows=generation_state["auto_rows"],
                    approved_synonym_counts=(
                        generation_state["approved_synonym_counts"]
                    ),
                    context_hashes=generation_state["context_hashes"],
                    evidence_rows=evidence_rows,
                )
                if apply:
                    for item in plan:
                        if item["changed"]:
                            await _cas_update_article(
                                conn,
                                server_id,
                                item,
                            )
                    report["updated"] = report["would_update"]
                report["mode"] = "apply" if apply else "dry-run"
                return report


def _totals(reports: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        field: sum(int(report.get(field, 0)) for report in reports)
        for field in _COUNT_FIELDS
    }


async def attest_servers(
    server_ids: Sequence[str] | None,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Attest one explicit server or every server with catalog rows."""

    if not isinstance(apply, bool):
        raise AttestationError("apply must be bool")
    explicit = (
        None
        if server_ids is None
        else [_validated_server_id(value) for value in server_ids]
    )
    if explicit is not None and len(explicit) != 1:
        raise AttestationError("exactly one server_id is required without --all")

    async with law_backfill_global_lock():
        if explicit is None:
            server_rows = await db_manager.fetch(
                """
                SELECT DISTINCT server_id
                FROM law_articles
                ORDER BY server_id
                """
            )
            selected = [
                _validated_server_id(row["server_id"])
                for row in server_rows
            ]
            if not selected:
                raise AttestationError("no servers with law catalog rows found")
        else:
            selected = explicit

        reports = []
        for server_id in selected:
            reports.append(
                await _attest_one_server(server_id, apply=apply)
            )

    return {
        "schema": ATTESTATION_SCHEMA,
        "mode": "apply" if apply else "dry-run",
        "server_count": len(reports),
        "servers": reports,
        "totals": _totals(reports),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Attest historical law title provenance/context metadata "
            "without any model or network calls"
        )
    )
    parser.add_argument(
        "server_id",
        nargs="?",
        help="one explicit server_id (mutually exclusive with --all)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="all server_id values that currently have law catalog rows",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the attested metadata; default is read-only dry-run",
    )
    return parser


async def _async_main(args: argparse.Namespace) -> dict[str, Any]:
    if bool(args.all) == bool(args.server_id):
        raise AttestationError("specify exactly one server_id or --all")
    await db_manager.connect()
    try:
        return await attest_servers(
            None if args.all else [args.server_id],
            apply=bool(args.apply),
        )
    finally:
        await db_manager.close()


def main() -> None:
    args = build_parser().parse_args()
    try:
        report = asyncio.run(_async_main(args))
    except (AttestationError, SynonymWriteError) as exc:
        raise SystemExit(f"ATTESTATION REFUSED: {exc}") from exc
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
