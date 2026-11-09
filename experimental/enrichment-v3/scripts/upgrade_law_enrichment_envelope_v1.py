#!/usr/bin/env python3
"""Explicitly upgrade a fully verified legacy v1 review into a v3 envelope.

This is the only supported bridge for historical immutable exports such as the
frozen San Francisco review.  It does not silently turn the current database
state into generation evidence.  First it re-verifies the original manifest,
every source/reviewed batch, the exact aggregate plan, and its aggregate
evidence.  Then, under the normal server locks and one repeatable-read
transaction, it requires the complete live law-document fingerprint to equal
the historical export and binds each target to its current source-only context
digest.  The resulting envelope is still rechecked by the live apply writer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from database.manager import db_manager
from scripts._apply_claude_enrich import (
    _validated_items,
)
from scripts.build_law_enrichment_apply_plan import (
    AggregatePlanError,
    EVIDENCE_SCHEMA_V1,
    _encoded_json,
    _file_payload,
    build_plan,
)
from scripts.export_law_enrichment_batches import (
    EXPORT_SCHEMA_V1,
    _payload_hash,
)
from services.law_backfill_lock import law_backfill_global_lock
from services.law_document_lock import law_document_server_lock
from services.law_embedding_service import catalog_server_lock
from services.law_enrichment_artifact import (
    build_apply_envelope_evidence,
    build_law_enrichment_apply_envelope,
    enrichment_generation_input_hash,
    payload_sha256,
)
from services.law_enrichment_completion import (
    evaluate_law_enrichment_completion,
)
from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
)
from services.law_source_snapshot import (
    law_source_content_fingerprint,
    law_source_fingerprint,
    verify_law_source_content_hashes,
)
from services.law_synonym_service import (
    SynonymWriteError,
    acquire_synonym_write_lock,
    load_auto_replacement_generation_state_for_targets_in_transaction,
)


class LegacyEnvelopeUpgradeError(ValueError):
    """The historical inputs or live source binding are not provably safe."""


_CONTEXT_CONTRACT = {
    "algorithm_version": ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    "generation_policy_version": ENRICHMENT_GENERATION_POLICY_VERSION,
    "source_title_derivation_policy_version": (
        SOURCE_TITLE_DERIVATION_POLICY_VERSION
    ),
    "catalog_parser_contract_version": (
        LAW_CATALOG_PARSER_CONTRACT_VERSION
    ),
}


def _legacy_aggregate_base(
    aggregate: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact evidence shape emitted by the historical v1 builder."""

    result = dict(aggregate)
    result.pop("manifest_schema", None)
    result.pop("generation_context_hashes", None)
    result.pop("reviewed_plan_hash", None)
    result.pop("selection_mode", None)
    result.pop("requested_article_ids", None)
    if result.get("schema") != EVIDENCE_SCHEMA_V1:
        raise LegacyEnvelopeUpgradeError(
            "historical aggregate is not export/evidence v1"
        )
    return result


def verify_legacy_v1_artifact_set(
    server_id: str,
    *,
    export_dir: Path,
    reviewed_dir: Path,
    legacy_plan: Path,
    legacy_evidence: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Re-verify every immutable v1 input and exact aggregate output."""

    try:
        reviewed_items, aggregate = build_plan(
            server_id,
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )
    except AggregatePlanError as exc:
        raise LegacyEnvelopeUpgradeError(
            f"legacy manifest/batch verification failed: {exc}"
        ) from exc
    if aggregate.get("manifest_schema") != EXPORT_SCHEMA_V1:
        raise LegacyEnvelopeUpgradeError(
            "explicit v1 upgrade requires a legacy export-v1 manifest"
        )

    legacy_plan = Path(legacy_plan)
    legacy_evidence = Path(legacy_evidence)
    try:
        plan_payload, plan_file_sha256 = _file_payload(legacy_plan)
    except AggregatePlanError as exc:
        raise LegacyEnvelopeUpgradeError(
            f"cannot read legacy aggregate plan: {exc}"
        ) from exc
    try:
        normalized_legacy_plan = _validated_items(plan_payload)
    except SynonymWriteError as exc:
        raise LegacyEnvelopeUpgradeError(
            f"legacy aggregate plan fails the current shared validator: {exc}"
        ) from exc
    if normalized_legacy_plan != reviewed_items:
        raise LegacyEnvelopeUpgradeError(
            "legacy aggregate plan semantics/order differ from the fully "
            "rebuilt reviewed set"
        )

    try:
        evidence_payload, evidence_file_sha256 = _file_payload(
            legacy_evidence
        )
    except AggregatePlanError as exc:
        raise LegacyEnvelopeUpgradeError(
            f"cannot read legacy aggregate evidence: {exc}"
        ) from exc
    if not isinstance(evidence_payload, Mapping):
        raise LegacyEnvelopeUpgradeError(
            "legacy aggregate evidence must be one JSON object"
        )
    expected_evidence_body = {
        **_legacy_aggregate_base(aggregate),
        "plan_file": legacy_plan.name,
        "plan_file_sha256": plan_file_sha256,
        "plan_payload_hash": _payload_hash(plan_payload),
    }
    expected_evidence = {
        **expected_evidence_body,
        "evidence_hash": _payload_hash(expected_evidence_body),
    }
    if dict(evidence_payload) != expected_evidence:
        raise LegacyEnvelopeUpgradeError(
            "legacy aggregate evidence is missing, stale, or tampered"
        )

    proof = {
        "legacy_manifest": {
            "schema": aggregate["manifest_schema"],
            "file": aggregate["manifest_file"],
            "file_sha256": aggregate["manifest_file_sha256"],
            "manifest_hash": aggregate["manifest_hash"],
        },
        "legacy_inputs": aggregate["inputs"],
        "legacy_plan": {
            "file": legacy_plan.name,
            "file_sha256": plan_file_sha256,
            "payload_hash": _payload_hash(plan_payload),
        },
        "legacy_evidence": {
            "schema": evidence_payload["schema"],
            "file": legacy_evidence.name,
            "file_sha256": evidence_file_sha256,
            "evidence_hash": evidence_payload["evidence_hash"],
        },
        "article_ids": aggregate["article_ids"],
        "selection_mode": aggregate["selection_mode"],
        "requested_article_ids": aggregate["requested_article_ids"],
        "source_fingerprint": aggregate["source_fingerprint"],
    }
    return reviewed_items, proof


def build_verified_v1_upgrade_envelope(
    server_id: str,
    *,
    reviewed_items: Sequence[Mapping[str, Any]],
    immutable_proof: Mapping[str, Any],
    live_source_fingerprint: Mapping[str, Any],
    live_content_source_fingerprint: Mapping[str, Any],
    live_generation_state: Mapping[str, Any],
    live_incomplete_article_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Bind a verified immutable v1 review to an unchanged live source."""

    historical_source_fingerprint = immutable_proof.get(
        "source_fingerprint"
    )
    if not isinstance(historical_source_fingerprint, Mapping):
        raise LegacyEnvelopeUpgradeError(
            "verified v1 proof lacks the historical source fingerprint"
        )
    if dict(live_source_fingerprint) != dict(
        historical_source_fingerprint
    ):
        raise LegacyEnvelopeUpgradeError(
            "live complete source fingerprint differs from the reviewed v1 "
            "export"
        )
    article_ids = [int(item["id"]) for item in reviewed_items]
    if list(immutable_proof.get("article_ids") or []) != article_ids:
        raise LegacyEnvelopeUpgradeError(
            "verified v1 proof article coverage/order differs from the plan"
        )
    snapshot = live_generation_state.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise LegacyEnvelopeUpgradeError(
            "live generation state lacks a snapshot"
        )
    if list(snapshot.get("required_target_ids") or []) != sorted(article_ids):
        raise LegacyEnvelopeUpgradeError(
            "live exact-body target closure differs from the reviewed v1 plan"
        )
    source_catalog_binding = snapshot.get("source_catalog_binding")
    if not isinstance(source_catalog_binding, Mapping):
        raise LegacyEnvelopeUpgradeError(
            "live generation state lacks the active source/catalog binding"
        )
    selection_mode = immutable_proof.get("selection_mode")
    authoritative_incomplete_ids: list[int] | None
    if selection_mode == "pending_enrichment":
        if (
            live_incomplete_article_ids is None
            or isinstance(
                live_incomplete_article_ids,
                (str, bytes, bytearray),
            )
        ):
            raise LegacyEnvelopeUpgradeError(
                "live authoritative pending-enrichment coverage is missing"
            )
        authoritative_incomplete_ids = []
        for value in live_incomplete_article_ids:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                raise LegacyEnvelopeUpgradeError(
                    "live authoritative pending-enrichment coverage contains "
                    "an invalid article id"
                )
            authoritative_incomplete_ids.append(value)
        if (
            not authoritative_incomplete_ids
            or authoritative_incomplete_ids
            != sorted(set(authoritative_incomplete_ids))
            or not set(authoritative_incomplete_ids).issubset(article_ids)
        ):
            raise LegacyEnvelopeUpgradeError(
                "live authoritative pending-enrichment coverage differs from "
                "the reviewed v1 exact-group closure"
            )
    elif selection_mode == "explicit_articles":
        if live_incomplete_article_ids is not None:
            raise LegacyEnvelopeUpgradeError(
                "explicit v1 upgrade cannot carry pending-enrichment coverage"
            )
        authoritative_incomplete_ids = None
        requested_ids = immutable_proof.get("requested_article_ids")
        if not isinstance(requested_ids, list) or not requested_ids:
            raise LegacyEnvelopeUpgradeError(
                "verified explicit v1 proof lacks requested article ids"
            )
    else:
        raise LegacyEnvelopeUpgradeError(
            "verified v1 proof has an unsupported selection mode"
        )

    articles_by_id = {
        int(row["id"]): row
        for row in live_generation_state.get("articles", [])
    }
    auto_synonyms: dict[int, list[str]] = {}
    for row in live_generation_state.get("auto_rows", []):
        if row.get("status") != "approved":
            continue
        auto_synonyms.setdefault(int(row["article_id"]), []).append(
            str(row["term"])
        )
    stale: list[int] = []
    for item in reviewed_items:
        article_id = int(item["id"])
        article = articles_by_id.get(article_id)
        if article is None or str(article.get("article_hash")) != str(
            item["article_hash"]
        ):
            stale.append(article_id)
            continue
        try:
            current_input_hash = enrichment_generation_input_hash(
                article,
                auto_synonyms.get(article_id, ()),
            )
        except ValueError as exc:
            raise LegacyEnvelopeUpgradeError(
                f"article {article_id} has invalid live generation input"
            ) from exc
        if current_input_hash != item["generation_input_hash"]:
            stale.append(article_id)
    if stale:
        raise LegacyEnvelopeUpgradeError(
            "live article/generation input differs from reviewed v1 for "
            f"article ids: {sorted(stale)}"
        )

    context_hashes = live_generation_state.get("context_hashes")
    if not isinstance(context_hashes, Mapping):
        raise LegacyEnvelopeUpgradeError(
            "live generation state lacks context hashes"
        )
    missing_context = sorted(
        set(article_ids) - {int(value) for value in context_hashes}
    )
    if missing_context:
        raise LegacyEnvelopeUpgradeError(
            "live generation state lacks target context hashes for "
            f"article ids: {missing_context}"
        )
    try:
        evidence = build_apply_envelope_evidence(
            "verified_v1_upgrade",
            {
                **dict(immutable_proof),
                "upgrade_binding": {
                    "source_fingerprint_verified": True,
                    "generation_snapshot_value": snapshot.get("value"),
                    "authoritative_incomplete_article_ids": (
                        authoritative_incomplete_ids
                    ),
                    "generation_context_hashes": {
                        str(article_id): context_hashes[article_id]
                        for article_id in article_ids
                    },
                    "live_source_fingerprint": dict(
                        live_content_source_fingerprint
                    ),
                    "source_catalog_binding": dict(
                        source_catalog_binding
                    ),
                    "context_contract": _CONTEXT_CONTRACT,
                    "reviewed_plan_hash": payload_sha256(
                        [dict(item) for item in reviewed_items]
                    ),
                },
            },
        )
        return build_law_enrichment_apply_envelope(
            server_id=server_id,
            source_fingerprint=live_content_source_fingerprint,
            source_catalog_binding=source_catalog_binding,
            context_contract=_CONTEXT_CONTRACT,
            items=reviewed_items,
            generation_context_hashes={
                article_id: context_hashes[article_id]
                for article_id in article_ids
            },
            evidence=evidence,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LegacyEnvelopeUpgradeError(
            f"cannot build verified v1 upgrade envelope: {exc}"
        ) from exc


def _write_json_exclusive(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _encoded_json(value)
    descriptor = os.open(
        Path(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            Path(path).unlink()
        except OSError:
            pass
        raise


async def _load_live_generation_state_for_upgrade(
    conn,
    server_id: str,
    *,
    article_ids: Sequence[int],
    selection_mode: str,
) -> tuple[dict[str, Any], list[int] | None]:
    """Capture the exact live selection proof used by the v1 bridge."""

    live_incomplete_article_ids: list[int] | None = None
    generation_target_ids = list(article_ids)
    if selection_mode == "pending_enrichment":
        completion = await evaluate_law_enrichment_completion(
            conn,
            server_id,
        )
        live_incomplete_article_ids = sorted(
            set(completion.incomplete_article_ids)
        )
        if not live_incomplete_article_ids:
            raise LegacyEnvelopeUpgradeError(
                "live server has no authoritative pending enrichment targets"
            )
        generation_target_ids = live_incomplete_article_ids
    elif selection_mode != "explicit_articles":
        raise LegacyEnvelopeUpgradeError(
            "verified v1 proof has an unsupported selection mode"
        )
    try:
        generation_state = (
            await load_auto_replacement_generation_state_for_targets_in_transaction(
                conn,
                server_id,
                generation_target_ids,
                # The bridge deliberately runs in a REPEATABLE READ,
                # READ ONLY transaction.  PostgreSQL rejects SELECT ... FOR
                # SHARE in that mode.  The enclosing global/document/catalog
                # and synonym locks already exclude every supported writer,
                # while the repeatable-read snapshot supplies consistency.
                lock_articles=False,
            )
        )
    except SynonymWriteError as exc:
        raise LegacyEnvelopeUpgradeError(
            f"live generation state is unsafe: {exc}"
        ) from exc
    return generation_state, live_incomplete_article_ids


async def _load_verified_live_source_fingerprints(
    conn,
    server_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return legacy-v2 compatibility proof plus exact-content v3 identity."""

    rows = [
        dict(row)
        for row in await conn.fetch(
            """
            SELECT id, server_id, source_thread_id, title, content,
                   content_hash
            FROM law_documents
            WHERE server_id = $1
            ORDER BY id
            """,
            server_id,
        )
    ]
    if not rows:
        raise LegacyEnvelopeUpgradeError(
            f"{server_id}: no law_documents exist for v1 upgrade"
        )
    try:
        verified_rows = verify_law_source_content_hashes(rows)
        legacy_fingerprint = law_source_fingerprint(
            verified_rows,
            require_content_hash=True,
        )
        content_fingerprint = law_source_content_fingerprint(
            verified_rows
        )
    except (TypeError, ValueError) as exc:
        raise LegacyEnvelopeUpgradeError(
            f"live source content/hash proof is unsafe: {exc}"
        ) from exc
    return legacy_fingerprint, content_fingerprint


async def upgrade_v1_artifacts(
    server_id: str,
    *,
    export_dir: Path,
    reviewed_dir: Path,
    legacy_plan: Path,
    legacy_evidence: Path,
    output_envelope: Path,
) -> dict[str, Any]:
    reviewed_items, immutable_proof = verify_legacy_v1_artifact_set(
        server_id,
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        legacy_plan=legacy_plan,
        legacy_evidence=legacy_evidence,
    )
    article_ids = [int(item["id"]) for item in reviewed_items]

    async with law_backfill_global_lock(manager=db_manager):
        async with law_document_server_lock(
            server_id,
            manager=db_manager,
        ):
            async with catalog_server_lock(server_id):
                async with db_manager.connection() as conn:
                    async with conn.transaction(
                        isolation="repeatable_read",
                        readonly=True,
                    ):
                        # Coordinate with direct synonym writers even though
                        # this transaction itself remains strictly read-only.
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
                            raise LegacyEnvelopeUpgradeError(
                                "server has unfinished derived document sync"
                            )
                        (
                            live_source_fingerprint,
                            live_content_source_fingerprint,
                        ) = await _load_verified_live_source_fingerprints(
                            conn,
                            server_id,
                        )
                        (
                            generation_state,
                            live_incomplete_article_ids,
                        ) = await _load_live_generation_state_for_upgrade(
                            conn,
                            server_id,
                            article_ids=article_ids,
                            selection_mode=str(
                                immutable_proof.get("selection_mode") or ""
                            ),
                        )
                        envelope = build_verified_v1_upgrade_envelope(
                            server_id,
                            reviewed_items=reviewed_items,
                            immutable_proof=immutable_proof,
                            live_source_fingerprint=live_source_fingerprint,
                            live_content_source_fingerprint=(
                                live_content_source_fingerprint
                            ),
                            live_generation_state=generation_state,
                            live_incomplete_article_ids=(
                                live_incomplete_article_ids
                            ),
                        )

    try:
        _write_json_exclusive(output_envelope, envelope)
    except OSError as exc:
        raise LegacyEnvelopeUpgradeError(
            f"refusing to overwrite or create output envelope: {exc}"
        ) from exc
    encoded = Path(output_envelope).read_bytes()
    return {
        "ok": True,
        "server_id": server_id,
        "articles": len(reviewed_items),
        "output_envelope": str(output_envelope),
        "envelope_hash": envelope["envelope_hash"],
        "file_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly bind a fully verified historical enrichment-v1 "
            "artifact set to an unchanged live source and emit envelope v3"
        )
    )
    parser.add_argument("server_id")
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--reviewed-dir", type=Path, required=True)
    parser.add_argument("--legacy-plan", type=Path, required=True)
    parser.add_argument("--legacy-evidence", type=Path, required=True)
    parser.add_argument("--output-envelope", type=Path, required=True)
    return parser


async def _async_main(args: argparse.Namespace) -> dict[str, Any]:
    await db_manager.connect()
    try:
        return await upgrade_v1_artifacts(
            args.server_id,
            export_dir=args.export_dir,
            reviewed_dir=args.reviewed_dir,
            legacy_plan=args.legacy_plan,
            legacy_evidence=args.legacy_evidence,
            output_envelope=args.output_envelope,
        )
    finally:
        await db_manager.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = asyncio.run(_async_main(args))
    except LegacyEnvelopeUpgradeError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
