#!/usr/bin/env python3
"""Build one immutable full-server reviewed enrichment apply envelope.

The builder is deliberately offline.  It verifies the native-v3 export manifest,
every immutable source batch, and every matching reviewed batch before it
writes anything.  The resulting versioned envelope is accepted directly by
``_apply_claude_enrich.py`` so the whole reviewed server can be applied in one
transaction instead of allowing per-batch collision order to affect results.

Historical v1 exports remain readable here only for the explicit online
upgrade workflow.  They can no longer be emitted as a bare apply array.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._apply_claude_enrich import _validated_items
from scripts.export_law_enrichment_batches import (
    EXPORT_SCHEMA,
    EXPORT_SCHEMA_V1,
    _payload_hash,
)
from scripts.validate_law_enrichment_batch import (
    OfflineBatchValidationError,
    validate_batch,
)
from services.law_enrichment_artifact import (
    build_apply_envelope_evidence,
    build_law_enrichment_apply_envelope,
    validate_law_source_fingerprint,
)
from services.law_catalog_service import (
    derive_catalog_fields,
    source_article_hash,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    validate_law_source_catalog_binding,
)
from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_enrichment_review_contract import (
    ReviewSourceContractError,
    combine_review_source_projections,
    validate_reviewed_source_projection,
)
from services.law_synonym_service import (
    SynonymWriteError,
    canonical_synonym_term,
    is_explicitly_empty_or_repealed_article,
)


EVIDENCE_SCHEMA_V1 = "law-full-enrichment-aggregate-evidence-v1"
EVIDENCE_SCHEMA = "law-full-enrichment-aggregate-evidence-v3"
_MANIFEST_REQUIRED_FIELDS_V1 = {
    "schema",
    "server_id",
    "created_at",
    "source_fingerprint",
    "batch_size",
    "pending_articles",
    "instructions",
    "batches",
    "manifest_hash",
}
_MANIFEST_OPTIONAL_FIELDS_V1 = {
    "selection_mode",
    "requested_article_ids",
}
_MANIFEST_REQUIRED_FIELDS_V3 = (
    _MANIFEST_REQUIRED_FIELDS_V1
    | _MANIFEST_OPTIONAL_FIELDS_V1
    | {
        "context_contract",
        "source_catalog_binding",
        "generation_snapshot",
        "exported_article_ids",
        "review_source_projection_hash",
    }
)
_MANIFEST_BATCH_FIELDS = {
    "file",
    "batch_hash",
    "articles",
    "article_ids",
}
_SOURCE_BATCH_NAME_RE = re.compile(r"source-batch-(\d{5})\.json")
_REVIEWED_BATCH_NAME_RE = re.compile(r"reviewed-batch-(\d{5})\.json")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
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


class AggregatePlanError(ValueError):
    """The full export/review set cannot safely become one apply plan."""


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AggregatePlanError(f"cannot read {path}: {exc}") from exc


def _read_json_bytes(path: Path, encoded: bytes) -> Any:
    try:
        return json.loads(encoded.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise AggregatePlanError(f"{path} is not UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AggregatePlanError(f"{path} is not valid JSON: {exc}") from exc


def _file_payload(path: Path) -> tuple[Any, str]:
    encoded = _read_bytes(path)
    return _read_json_bytes(path, encoded), hashlib.sha256(encoded).hexdigest()


def _positive_integer(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AggregatePlanError(f"{field} must be a positive integer")
    return value


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AggregatePlanError(f"{field} must be a lowercase SHA-256")
    return value


def _validate_manifest(
    manifest: Any,
    *,
    expected_server_id: str,
) -> tuple[list[Mapping[str, Any]], int, str]:
    if not isinstance(manifest, dict):
        raise AggregatePlanError("manifest must contain one JSON object")
    schema = manifest.get("schema")
    if schema == EXPORT_SCHEMA:
        required_fields = _MANIFEST_REQUIRED_FIELDS_V3
        allowed_fields = required_fields
    elif schema == EXPORT_SCHEMA_V1:
        required_fields = _MANIFEST_REQUIRED_FIELDS_V1
        allowed_fields = (
            required_fields | _MANIFEST_OPTIONAL_FIELDS_V1
        )
    else:
        raise AggregatePlanError(
            f"unsupported manifest schema {schema!r}"
        )
    fields = set(manifest)
    missing_fields = required_fields - fields
    unknown_fields = fields - allowed_fields
    if missing_fields or unknown_fields:
        raise AggregatePlanError(
            "manifest fields do not match export schema: "
            f"missing={sorted(missing_fields)}, "
            f"unknown={sorted(unknown_fields)}"
        )
    selection_fields = fields.intersection(_MANIFEST_OPTIONAL_FIELDS_V1)
    if (
        schema == EXPORT_SCHEMA_V1
        and selection_fields
        not in (set(), _MANIFEST_OPTIONAL_FIELDS_V1)
    ):
        raise AggregatePlanError(
            "manifest v1 selection fields must be both present or both absent"
        )
    if manifest.get("server_id") != expected_server_id:
        raise AggregatePlanError(
            "manifest belongs to another server: "
            f"expected={expected_server_id!r}, "
            f"actual={manifest.get('server_id')!r}"
        )
    if not isinstance(manifest.get("created_at"), str) or not manifest["created_at"]:
        raise AggregatePlanError("manifest created_at must be a non-empty string")
    try:
        source_fingerprint = validate_law_source_fingerprint(
            manifest.get("source_fingerprint")
        )
    except ValueError as exc:
        raise AggregatePlanError(
            f"manifest source_fingerprint is invalid: {exc}"
        ) from exc
    if schema == EXPORT_SCHEMA and source_fingerprint[
        "snapshot_version"
    ] != 3:
        raise AggregatePlanError(
            "manifest v3 source fingerprint must bind actual content v3"
        )
    _positive_integer(manifest.get("batch_size"), field="manifest batch_size")
    pending_articles = _positive_integer(
        manifest.get("pending_articles"),
        field="manifest pending_articles",
    )
    selection_mode = manifest.get("selection_mode", "pending_enrichment")
    if selection_mode not in {
        "pending_enrichment",
        "explicit_articles",
    }:
        raise AggregatePlanError("manifest selection_mode is invalid")
    requested_ids = manifest.get("requested_article_ids")
    if requested_ids is not None:
        if not isinstance(requested_ids, list):
            raise AggregatePlanError(
                "manifest requested_article_ids must be an array or null"
            )
        normalized_requested = [
            _positive_integer(value, field="manifest requested article id")
            for value in requested_ids
        ]
        if (
            normalized_requested != sorted(set(normalized_requested))
            or selection_mode != "explicit_articles"
        ):
            raise AggregatePlanError(
                "manifest requested_article_ids must be sorted unique IDs "
                "for explicit_articles mode"
            )
    elif selection_mode == "explicit_articles":
        raise AggregatePlanError(
            "explicit_articles manifest must contain requested_article_ids"
        )
    if schema == EXPORT_SCHEMA:
        try:
            source_catalog_binding = validate_law_source_catalog_binding(
                manifest.get("source_catalog_binding"),
                expected_server_id=expected_server_id,
            )
        except ValueError as exc:
            raise AggregatePlanError(
                f"manifest source_catalog_binding is invalid: {exc}"
            ) from exc
        if source_catalog_binding["source_fingerprint"] != source_fingerprint:
            raise AggregatePlanError(
                "manifest source/catalog binding differs from its exact "
                "source fingerprint"
            )
        if manifest.get("context_contract") != _CONTEXT_CONTRACT:
            raise AggregatePlanError(
                "manifest context contract differs from the active "
                "generation policy"
            )
        if not isinstance(manifest.get("generation_snapshot"), Mapping):
            raise AggregatePlanError(
                "manifest generation_snapshot must be an object"
            )
        if (
            manifest["generation_snapshot"].get(
                "source_catalog_binding"
            )
            != source_catalog_binding
        ):
            raise AggregatePlanError(
                "manifest generation snapshot differs from its "
                "source/catalog binding"
            )
        exported_ids = manifest.get("exported_article_ids")
        if not isinstance(exported_ids, list):
            raise AggregatePlanError(
                "manifest exported_article_ids must be an array"
            )
        normalized_exported = [
            _positive_integer(
                value,
                field="manifest exported article id",
            )
            for value in exported_ids
        ]
        if len(normalized_exported) != len(set(normalized_exported)):
            raise AggregatePlanError(
                "manifest exported_article_ids contains duplicates"
            )
        _sha256(
            manifest.get("review_source_projection_hash"),
            field="manifest review_source_projection_hash",
        )
    if not isinstance(manifest.get("instructions"), Mapping):
        raise AggregatePlanError("manifest instructions must be an object")

    stored_hash = _sha256(
        manifest.get("manifest_hash"),
        field="manifest manifest_hash",
    )
    hash_body = dict(manifest)
    hash_body.pop("manifest_hash")
    actual_hash = _payload_hash(hash_body)
    if stored_hash != actual_hash:
        raise AggregatePlanError(
            "manifest_hash mismatch: "
            f"stored={stored_hash!r}, actual={actual_hash!r}"
        )

    batches = manifest.get("batches")
    if not isinstance(batches, list) or not batches:
        raise AggregatePlanError("manifest batches must be a non-empty array")
    return batches, pending_articles, schema


def _reviewed_name(source_name: str) -> str:
    match = _SOURCE_BATCH_NAME_RE.fullmatch(source_name)
    if match is None:
        raise AggregatePlanError(
            f"manifest source batch filename is invalid: {source_name!r}"
        )
    return f"reviewed-batch-{match.group(1)}.json"


def _matching_files(directory: Path, pattern: re.Pattern[str]) -> set[str]:
    try:
        return {
            path.name
            for path in directory.iterdir()
            if pattern.fullmatch(path.name)
        }
    except OSError as exc:
        raise AggregatePlanError(
            f"cannot inspect directory {directory}: {exc}"
        ) from exc


def build_plan(
    server_id: str,
    *,
    export_dir: Path,
    reviewed_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate all legacy-v1/native-v3 inputs and return items plus evidence."""

    if not isinstance(server_id, str) or not server_id.strip():
        raise AggregatePlanError("server_id must be a non-empty string")
    export_dir = Path(export_dir)
    reviewed_dir = Path(reviewed_dir)
    if not export_dir.is_dir():
        raise AggregatePlanError(f"export directory does not exist: {export_dir}")
    if not reviewed_dir.is_dir():
        raise AggregatePlanError(
            f"reviewed directory does not exist: {reviewed_dir}"
        )

    manifest_path = export_dir / "manifest.json"
    manifest, manifest_file_sha256 = _file_payload(manifest_path)
    batches, pending_articles, manifest_schema = _validate_manifest(
        manifest,
        expected_server_id=server_id,
    )

    expected_source_names: list[str] = []
    expected_reviewed_names: list[str] = []
    manifest_entries: list[tuple[Mapping[str, Any], str, str]] = []
    for ordinal, entry in enumerate(batches, 1):
        if not isinstance(entry, Mapping):
            raise AggregatePlanError(
                f"manifest batch entry {ordinal} must be an object"
            )
        fields = set(entry)
        if fields != _MANIFEST_BATCH_FIELDS:
            raise AggregatePlanError(
                f"manifest batch entry {ordinal} fields do not match schema: "
                f"missing={sorted(_MANIFEST_BATCH_FIELDS - fields)}, "
                f"unknown={sorted(fields - _MANIFEST_BATCH_FIELDS)}"
            )
        source_name = entry.get("file")
        if not isinstance(source_name, str):
            raise AggregatePlanError(
                f"manifest batch entry {ordinal} file must be a string"
            )
        match = _SOURCE_BATCH_NAME_RE.fullmatch(source_name)
        if match is None or int(match.group(1)) != ordinal:
            raise AggregatePlanError(
                f"manifest batch entry {ordinal} has non-sequential filename "
                f"{source_name!r}"
            )
        reviewed_name = _reviewed_name(source_name)
        if source_name in expected_source_names:
            raise AggregatePlanError(
                f"manifest contains duplicate source filename {source_name!r}"
            )
        expected_source_names.append(source_name)
        expected_reviewed_names.append(reviewed_name)
        manifest_entries.append((entry, source_name, reviewed_name))

    actual_source_names = _matching_files(
        export_dir,
        _SOURCE_BATCH_NAME_RE,
    )
    actual_reviewed_names = _matching_files(
        reviewed_dir,
        _REVIEWED_BATCH_NAME_RE,
    )
    expected_source_set = set(expected_source_names)
    expected_reviewed_set = set(expected_reviewed_names)
    if actual_source_names != expected_source_set:
        raise AggregatePlanError(
            "source batch file coverage differs from manifest: "
            f"missing={sorted(expected_source_set - actual_source_names)}, "
            f"unexpected={sorted(actual_source_names - expected_source_set)}"
        )
    if actual_reviewed_names != expected_reviewed_set:
        raise AggregatePlanError(
            "reviewed batch file coverage differs from manifest: "
            f"missing={sorted(expected_reviewed_set - actual_reviewed_names)}, "
            f"unexpected={sorted(actual_reviewed_names - expected_reviewed_set)}"
        )

    combined: list[dict[str, Any]] = []
    global_source_ids: set[int] = set()
    evidence_batches: list[dict[str, Any]] = []
    ordered_article_ids: list[int] = []
    generation_context_hashes: dict[int, str] = {}
    batch_review_source_projections: list[Mapping[str, Any]] = []
    for ordinal, (entry, source_name, reviewed_name) in enumerate(
        manifest_entries,
        1,
    ):
        source_path = export_dir / source_name
        reviewed_path = reviewed_dir / reviewed_name
        if not source_path.is_file():
            raise AggregatePlanError(f"source batch is not a file: {source_path}")
        if not reviewed_path.is_file():
            raise AggregatePlanError(
                f"reviewed batch is not a file: {reviewed_path}"
            )
        source_batch, source_file_sha256 = _file_payload(source_path)
        reviewed_batch, reviewed_file_sha256 = _file_payload(reviewed_path)

        try:
            result = validate_batch(server_id, source_batch, reviewed_batch)
            normalized_reviewed = _validated_items(reviewed_batch)
        except (OfflineBatchValidationError, SynonymWriteError) as exc:
            raise AggregatePlanError(
                f"batch {ordinal} validation failed: {exc}"
            ) from exc

        entry_hash = _sha256(
            entry.get("batch_hash"),
            field=f"manifest batch {ordinal} batch_hash",
        )
        if result["batch_number"] != ordinal:
            raise AggregatePlanError(
                f"source batch_number mismatch for {source_name}: "
                f"expected={ordinal}, actual={result['batch_number']}"
            )
        if result["batch_hash"] != entry_hash:
            raise AggregatePlanError(
                f"manifest/source batch_hash mismatch for {source_name}"
            )
        if source_batch.get("source_fingerprint") != manifest["source_fingerprint"]:
            raise AggregatePlanError(
                f"source fingerprint mismatch for {source_name}"
            )
        if (
            manifest_schema == EXPORT_SCHEMA
            and source_batch.get("context_contract")
            != manifest["context_contract"]
        ):
            raise AggregatePlanError(
                f"context contract mismatch for {source_name}"
            )
        if (
            manifest_schema == EXPORT_SCHEMA
            and source_batch.get("source_catalog_binding")
            != manifest["source_catalog_binding"]
        ):
            raise AggregatePlanError(
                f"source/catalog binding mismatch for {source_name}"
            )
        if manifest_schema == EXPORT_SCHEMA:
            review_projection = source_batch.get(
                "review_source_projection"
            )
            if not isinstance(review_projection, Mapping):
                raise AggregatePlanError(
                    f"review source projection is missing from {source_name}"
                )
            batch_review_source_projections.append(review_projection)

        article_count = _positive_integer(
            entry.get("articles"),
            field=f"manifest batch {ordinal} articles",
        )
        entry_article_ids = entry.get("article_ids")
        if not isinstance(entry_article_ids, list):
            raise AggregatePlanError(
                f"manifest batch {ordinal} article_ids must be an array"
            )
        normalized_entry_ids = [
            _positive_integer(
                value,
                field=f"manifest batch {ordinal} article id",
            )
            for value in entry_article_ids
        ]
        source_article_ids = [
            int(article["id"])
            for article in source_batch["articles"]
        ]
        if (
            article_count != len(source_article_ids)
            or normalized_entry_ids != source_article_ids
        ):
            raise AggregatePlanError(
                f"manifest article coverage/order mismatch for {source_name}"
            )
        duplicates = sorted(global_source_ids.intersection(source_article_ids))
        if duplicates:
            raise AggregatePlanError(
                "article IDs occur in more than one source batch: "
                f"{duplicates}"
            )
        global_source_ids.update(source_article_ids)
        batch_context_hashes = result["generation_context_hashes"]
        if manifest_schema == EXPORT_SCHEMA:
            if set(batch_context_hashes) != set(source_article_ids):
                raise AggregatePlanError(
                    f"context hash coverage mismatch for {source_name}"
                )
            generation_context_hashes.update(batch_context_hashes)
        elif batch_context_hashes:
            raise AggregatePlanError(
                f"legacy source batch unexpectedly exposes v2 context for "
                f"{source_name}"
            )

        reviewed_by_id = {
            int(item["id"]): item
            for item in normalized_reviewed
        }
        combined.extend(
            reviewed_by_id[article_id]
            for article_id in source_article_ids
        )
        ordered_article_ids.extend(source_article_ids)
        evidence_batches.append(
            {
                "batch_number": ordinal,
                "source_file": source_name,
                "source_file_sha256": source_file_sha256,
                "batch_hash": entry_hash,
                "reviewed_file": reviewed_name,
                "reviewed_file_sha256": reviewed_file_sha256,
                "articles": article_count,
                "article_ids": source_article_ids,
            }
        )

    if len(combined) != pending_articles:
        raise AggregatePlanError(
            "manifest pending_articles does not match complete coverage: "
            f"expected={pending_articles}, actual={len(combined)}"
        )
    requested_ids = manifest.get("requested_article_ids")
    if (
        manifest_schema == EXPORT_SCHEMA_V1
        and requested_ids is not None
        and set(requested_ids) != global_source_ids
    ):
        raise AggregatePlanError(
            "explicit requested_article_ids do not exactly match source batches"
        )
    if (
        manifest_schema == EXPORT_SCHEMA
        and manifest.get("exported_article_ids") != ordered_article_ids
    ):
        raise AggregatePlanError(
            "manifest exported_article_ids do not exactly match source "
            "batch coverage/order"
        )
    try:
        combined = _validated_items(combined)
    except SynonymWriteError as exc:
        raise AggregatePlanError(
            f"combined apply plan schema is invalid: {exc}"
        ) from exc
    review_source_projection_hash: str | None = None
    if manifest_schema == EXPORT_SCHEMA:
        try:
            combined_review_projection = (
                combine_review_source_projections(
                    batch_review_source_projections,
                    catalog_fields_resolver=derive_catalog_fields,
                    article_hash_resolver=source_article_hash,
                    is_tombstone=(
                        is_explicitly_empty_or_repealed_article
                    ),
                    canonicalize_scope=canonical_synonym_term,
                )
            )
            review_source_projection_hash = str(
                combined_review_projection["projection_hash"]
            )
            if review_source_projection_hash != manifest.get(
                "review_source_projection_hash"
            ):
                raise ReviewSourceContractError(
                    "combined review source projection hash differs from "
                    "manifest"
                )
            validate_reviewed_source_projection(
                combined_review_projection,
                combined,
                catalog_fields_resolver=derive_catalog_fields,
                article_hash_resolver=source_article_hash,
                is_tombstone=is_explicitly_empty_or_repealed_article,
                canonicalize_scope=canonical_synonym_term,
                canonicalize_suffix=canonical_synonym_term,
            )
        except ReviewSourceContractError as exc:
            raise AggregatePlanError(
                f"aggregate reviewed-source contract failed: {exc}"
            ) from exc

    evidence = {
        "schema": (
            EVIDENCE_SCHEMA
            if manifest_schema == EXPORT_SCHEMA
            else EVIDENCE_SCHEMA_V1
        ),
        "manifest_schema": manifest_schema,
        "server_id": server_id,
        "manifest_file": manifest_path.name,
        "manifest_file_sha256": manifest_file_sha256,
        "manifest_hash": manifest["manifest_hash"],
        "source_fingerprint": manifest["source_fingerprint"],
        **(
            {
                "source_catalog_binding": manifest[
                    "source_catalog_binding"
                ],
            }
            if manifest_schema == EXPORT_SCHEMA
            else {}
        ),
        "selection_mode": manifest.get(
            "selection_mode",
            "pending_enrichment",
        ),
        "requested_article_ids": manifest.get(
            "requested_article_ids"
        ),
        "batches": len(evidence_batches),
        "articles": len(combined),
        "article_ids": ordered_article_ids,
        "generation_context_hashes": {
            str(article_id): generation_context_hashes[article_id]
            for article_id in ordered_article_ids
            if article_id in generation_context_hashes
        },
        "reviewed_plan_hash": _payload_hash(combined),
        **(
            {
                "review_source_projection_hash": (
                    review_source_projection_hash
                )
            }
            if manifest_schema == EXPORT_SCHEMA
            else {}
        ),
        "inputs": evidence_batches,
    }
    return combined, evidence


def _encoded_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _cleanup_reserved(outputs: Sequence[tuple[Path, int]]) -> None:
    for path, descriptor in outputs:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            path.unlink()
        except OSError:
            pass


def _write_outputs_exclusive(
    output_plan: Path,
    output_evidence: Path,
    plan_bytes: bytes,
    evidence_bytes: bytes,
) -> None:
    output_plan = Path(output_plan)
    output_evidence = Path(output_evidence)
    if output_plan.resolve() == output_evidence.resolve():
        raise AggregatePlanError("plan and evidence paths must be different")
    for path in (output_plan, output_evidence):
        if not path.parent.is_dir():
            raise AggregatePlanError(
                f"output parent directory does not exist: {path.parent}"
            )

    reserved: list[tuple[Path, int]] = []
    try:
        for path in (output_plan, output_evidence):
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            reserved.append((path, descriptor))
    except OSError as exc:
        _cleanup_reserved(reserved)
        raise AggregatePlanError(
            f"refusing to overwrite or create output: {exc}"
        ) from exc

    try:
        for (path, descriptor), encoded in zip(
            reserved,
            (plan_bytes, evidence_bytes),
        ):
            with os.fdopen(
                descriptor,
                "wb",
                closefd=True,
            ) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError as exc:
        _cleanup_reserved(reserved)
        raise AggregatePlanError(f"cannot write aggregate outputs: {exc}") from exc


def build_and_write(
    server_id: str,
    *,
    export_dir: Path,
    reviewed_dir: Path,
    output_plan: Path,
    output_evidence: Path,
) -> dict[str, Any]:
    reviewed_items, aggregate_evidence = build_plan(
        server_id,
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
    )
    if aggregate_evidence["manifest_schema"] != EXPORT_SCHEMA:
        raise AggregatePlanError(
            "legacy export v1 cannot become a bare apply plan; use the "
            "explicit verified v1 upgrade workflow"
        )
    context_hashes = {
        int(article_id): value
        for article_id, value in aggregate_evidence[
            "generation_context_hashes"
        ].items()
    }
    input_evidence = build_apply_envelope_evidence(
        "native_v3",
        {
            "aggregate_schema": aggregate_evidence["schema"],
            "manifest_schema": aggregate_evidence["manifest_schema"],
            "manifest_file": aggregate_evidence["manifest_file"],
            "manifest_file_sha256": aggregate_evidence[
                "manifest_file_sha256"
            ],
            "manifest_hash": aggregate_evidence["manifest_hash"],
            "article_ids": aggregate_evidence["article_ids"],
            "inputs": aggregate_evidence["inputs"],
            "source_fingerprint": aggregate_evidence[
                "source_fingerprint"
            ],
            "source_catalog_binding": aggregate_evidence[
                "source_catalog_binding"
            ],
            "context_contract": _CONTEXT_CONTRACT,
            "generation_context_hashes": aggregate_evidence[
                "generation_context_hashes"
            ],
            "review_source_projection_hash": aggregate_evidence[
                "review_source_projection_hash"
            ],
            "reviewed_plan_hash": aggregate_evidence[
                "reviewed_plan_hash"
            ],
            "selection_mode": aggregate_evidence["selection_mode"],
            "requested_article_ids": aggregate_evidence[
                "requested_article_ids"
            ],
        },
    )
    try:
        envelope = build_law_enrichment_apply_envelope(
            server_id=server_id,
            source_fingerprint=aggregate_evidence[
                "source_fingerprint"
            ],
            source_catalog_binding=aggregate_evidence[
                "source_catalog_binding"
            ],
            context_contract=_CONTEXT_CONTRACT,
            items=reviewed_items,
            generation_context_hashes=context_hashes,
            evidence=input_evidence,
        )
    except ValueError as exc:
        raise AggregatePlanError(
            f"cannot build v3 apply envelope: {exc}"
        ) from exc
    plan_bytes = _encoded_json(envelope)
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    evidence_body = {
        **aggregate_evidence,
        "plan_file": Path(output_plan).name,
        "plan_file_sha256": plan_sha256,
        "plan_payload_hash": _payload_hash(envelope),
        "apply_envelope_hash": envelope["envelope_hash"],
        "apply_input_evidence_hash": input_evidence["evidence_hash"],
    }
    evidence_hash = _payload_hash(evidence_body)
    evidence = {
        **evidence_body,
        "evidence_hash": evidence_hash,
    }
    evidence_bytes = _encoded_json(evidence)
    evidence_file_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
    _write_outputs_exclusive(
        output_plan,
        output_evidence,
        plan_bytes,
        evidence_bytes,
    )
    return {
        "ok": True,
        "server_id": server_id,
        "batches": evidence["batches"],
        "articles": evidence["articles"],
        "apply_envelope_hash": envelope["envelope_hash"],
        "plan": str(output_plan),
        "plan_sha256": plan_sha256,
        "evidence": str(output_evidence),
        "evidence_hash": evidence_hash,
        "evidence_file_sha256": evidence_file_sha256,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline exact-coverage builder for one atomic full-server "
            "reviewed enrichment apply plan"
        )
    )
    parser.add_argument("server_id")
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--reviewed-dir", type=Path, required=True)
    parser.add_argument("--output-plan", type=Path, required=True)
    parser.add_argument("--output-evidence", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = build_and_write(
            args.server_id,
            export_dir=args.export_dir,
            reviewed_dir=args.reviewed_dir,
            output_plan=args.output_plan,
            output_evidence=args.output_evidence,
        )
    except AggregatePlanError as exc:
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
