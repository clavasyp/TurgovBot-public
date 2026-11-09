#!/usr/bin/env python3
"""Create immutable reviewed candidates from one native-v3 source export.

The builder is deliberately network- and database-free.  It copies the exact
persisted enrichment fields embedded in every immutable source article, may
apply a hash-bound list of explicit old-to-new corrections, and then reuses
the normal per-batch and aggregate reviewed-artifact validators before it
creates any output.  Output directories and files are create-once.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.build_law_enrichment_apply_plan import (
    AggregatePlanError,
    _file_payload,
    _reviewed_name,
    _validate_manifest,
    build_plan,
)
from scripts.export_law_enrichment_batches import (
    BATCH_SCHEMA,
    EXPORT_SCHEMA,
    _payload_hash,
)


CORRECTION_SCHEMA = "law-enrichment-reviewed-corrections-v1"
REPORT_SCHEMA = "law-enrichment-reviewed-candidate-report-v1"
REPORT_FILE = "validation-report.json"
CORRECTION_COPY_FILE = "correction-manifest.json"

_CURRENT_SOURCE_FIELDS = {
    "catalog_title",
    "current_penalty_summary",
    "current_penalty",
    "current_auto_synonyms",
}
_CORRECTION_FIELDS = {
    "schema",
    "server_id",
    "source_manifest_hash",
    "source_manifest_file_sha256",
    "base_reviewed_plan_hash",
    "result_reviewed_plan_hash",
    "operations",
    "correction_manifest_hash",
}
_OPERATION_FIELDS = {
    "article_id",
    "article_hash",
    "generation_input_hash",
    "generation_context_hash",
    "path",
    "old_value",
    "new_value",
}
_SCALAR_PATHS = {"/title", "/penalty_summary", "/penalty"}
_PENALTY_PATHS = {
    "/penalty/fine",
    "/penalty/prison",
    "/penalty/arrest",
    "/penalty/wanted_level",
    "/penalty/other",
}
_SHA256_CHARS = frozenset("0123456789abcdef")


class CandidateBuildError(ValueError):
    """The current-state candidate set cannot be built safely."""


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


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_CHARS for character in value)
    )


def _exact_json_equal(first: Any, second: Any) -> bool:
    """Compare JSON values without Python's ``True == 1`` coercion."""

    return _payload_hash(first) == _payload_hash(second)


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CandidateBuildError(f"{field} must be a positive integer")
    return value


def _load_export(
    server_id: str,
    export_dir: Path,
) -> tuple[
    Mapping[str, Any],
    str,
    list[
        tuple[
            str,
            str,
            str,
            Mapping[str, Any],
            list[dict[str, Any]],
        ]
    ],
    list[dict[str, Any]],
]:
    export_dir = Path(export_dir)
    if not export_dir.is_dir():
        raise CandidateBuildError(
            f"export directory does not exist: {export_dir}"
        )
    manifest, manifest_file_sha256 = _file_payload(
        export_dir / "manifest.json"
    )
    batches, _pending_articles, schema = _validate_manifest(
        manifest,
        expected_server_id=server_id,
    )
    if schema != EXPORT_SCHEMA:
        raise CandidateBuildError(
            "reviewed candidates require a fresh native-v3 export"
        )

    loaded_batches: list[
        tuple[
            str,
            str,
            str,
            Mapping[str, Any],
            list[dict[str, Any]],
        ]
    ] = []
    ordered_candidates: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for ordinal, entry in enumerate(batches, 1):
        if not isinstance(entry, Mapping):
            raise CandidateBuildError(
                f"manifest batch entry {ordinal} must be an object"
            )
        source_name = entry.get("file")
        if not isinstance(source_name, str):
            raise CandidateBuildError(
                f"manifest batch entry {ordinal} file must be a string"
            )
        try:
            reviewed_name = _reviewed_name(source_name)
        except AggregatePlanError as exc:
            raise CandidateBuildError(str(exc)) from exc
        source_batch, source_file_sha256 = _file_payload(
            export_dir / source_name
        )
        if not isinstance(source_batch, Mapping):
            raise CandidateBuildError(
                f"source batch {source_name} must contain one object"
            )
        if source_batch.get("schema") != BATCH_SCHEMA:
            raise CandidateBuildError(
                f"source batch {source_name} is not native v3"
            )
        raw_articles = source_batch.get("articles")
        if not isinstance(raw_articles, list) or not raw_articles:
            raise CandidateBuildError(
                f"source batch {source_name} articles must be non-empty"
            )
        candidates: list[dict[str, Any]] = []
        for index, article in enumerate(raw_articles):
            if not isinstance(article, Mapping):
                raise CandidateBuildError(
                    f"{source_name} article {index} must be an object"
                )
            article_id = _positive_int(
                article.get("id"),
                field=f"{source_name} article id",
            )
            if article_id in seen_ids:
                raise CandidateBuildError(
                    f"article id {article_id} occurs in multiple batches"
                )
            seen_ids.add(article_id)
            source = article.get("source")
            if not isinstance(source, Mapping):
                raise CandidateBuildError(
                    f"source article {article_id} source must be an object"
                )
            missing_current = _CURRENT_SOURCE_FIELDS - set(source)
            if missing_current:
                raise CandidateBuildError(
                    f"source article {article_id} lacks current enrichment "
                    f"fields: {sorted(missing_current)}"
                )
            candidate = {
                "id": article_id,
                "article_hash": deepcopy(article.get("article_hash")),
                "title": deepcopy(source["catalog_title"]),
                "penalty_summary": deepcopy(
                    source["current_penalty_summary"]
                ),
                "penalty": deepcopy(source["current_penalty"]),
                "synonyms": deepcopy(source["current_auto_synonyms"]),
                "generation_input_hash": deepcopy(
                    article.get("generation_input_hash")
                ),
            }
            candidates.append(candidate)
            ordered_candidates.append(candidate)
        loaded_batches.append(
            (
                source_name,
                reviewed_name,
                source_file_sha256,
                source_batch,
                candidates,
            )
        )
    return (
        manifest,
        manifest_file_sha256,
        loaded_batches,
        ordered_candidates,
    )


def _operation_path(operation: Mapping[str, Any], *, article_id: int) -> str:
    path = operation.get("path")
    if not isinstance(path, str):
        raise CandidateBuildError(
            f"correction for article {article_id} path must be a string"
        )
    if path in _SCALAR_PATHS or path in _PENALTY_PATHS or path == "/synonyms":
        return path
    prefix = "/synonyms/"
    if path.startswith(prefix):
        suffix = path[len(prefix):]
        if suffix and suffix.isascii() and suffix.isdecimal():
            if suffix != "0" and suffix.startswith("0"):
                raise CandidateBuildError(
                    f"correction path has a non-canonical index: {path!r}"
                )
            try:
                int(suffix)
            except (ValueError, OverflowError) as exc:
                raise CandidateBuildError(
                    f"correction path index is invalid: {path!r}"
                ) from exc
            return path
    raise CandidateBuildError(
        f"correction for article {article_id} uses forbidden path {path!r}"
    )


def _read_path(item: Mapping[str, Any], path: str) -> Any:
    if path in _SCALAR_PATHS:
        return item[path[1:]]
    if path in _PENALTY_PATHS:
        penalty = item["penalty"]
        key = path.rsplit("/", 1)[-1]
        if not isinstance(penalty, Mapping) or key not in penalty:
            raise CandidateBuildError(
                f"correction path {path!r} does not exist in base candidate"
            )
        return penalty[key]
    if path == "/synonyms":
        return item["synonyms"]
    synonyms = item["synonyms"]
    index = int(path.rsplit("/", 1)[-1])
    if not isinstance(synonyms, list) or index >= len(synonyms):
        raise CandidateBuildError(
            f"correction path {path!r} does not exist in base candidate"
        )
    return synonyms[index]


def _write_path(item: dict[str, Any], path: str, value: Any) -> None:
    if path in _SCALAR_PATHS:
        item[path[1:]] = deepcopy(value)
        return
    if path in _PENALTY_PATHS:
        item["penalty"][path.rsplit("/", 1)[-1]] = deepcopy(value)
        return
    if path == "/synonyms":
        item["synonyms"] = deepcopy(value)
        return
    item["synonyms"][int(path.rsplit("/", 1)[-1])] = deepcopy(value)


def _overlapping_paths(first: str, second: str) -> bool:
    return (
        first == second
        or first.startswith(second + "/")
        or second.startswith(first + "/")
    )


def _apply_corrections(
    *,
    server_id: str,
    correction_manifest: Any,
    manifest: Mapping[str, Any],
    manifest_file_sha256: str,
    candidates: list[dict[str, Any]],
    context_hash_by_id: Mapping[int, str],
) -> dict[str, Any]:
    if not isinstance(correction_manifest, dict):
        raise CandidateBuildError(
            "correction manifest must contain one JSON object"
        )
    fields = set(correction_manifest)
    if fields != _CORRECTION_FIELDS:
        raise CandidateBuildError(
            "correction manifest fields do not match schema: "
            f"missing={sorted(_CORRECTION_FIELDS - fields)}, "
            f"unknown={sorted(fields - _CORRECTION_FIELDS)}"
        )
    if correction_manifest.get("schema") != CORRECTION_SCHEMA:
        raise CandidateBuildError(
            f"unsupported correction schema {correction_manifest.get('schema')!r}"
        )
    if correction_manifest.get("server_id") != server_id:
        raise CandidateBuildError("correction manifest belongs to another server")
    if correction_manifest.get("source_manifest_hash") != manifest.get(
        "manifest_hash"
    ):
        raise CandidateBuildError(
            "correction manifest source manifest_hash is stale or foreign"
        )
    if correction_manifest.get(
        "source_manifest_file_sha256"
    ) != manifest_file_sha256:
        raise CandidateBuildError(
            "correction manifest source manifest file hash is stale or foreign"
        )
    stored_hash = correction_manifest.get("correction_manifest_hash")
    if not _valid_sha256(stored_hash):
        raise CandidateBuildError(
            "correction_manifest_hash must be a lowercase SHA-256"
        )
    hash_body = dict(correction_manifest)
    hash_body.pop("correction_manifest_hash")
    actual_hash = _payload_hash(hash_body)
    if stored_hash != actual_hash:
        raise CandidateBuildError(
            "correction_manifest_hash mismatch: "
            f"stored={stored_hash!r}, actual={actual_hash!r}"
        )

    base_hash = _payload_hash(candidates)
    if correction_manifest.get("base_reviewed_plan_hash") != base_hash:
        raise CandidateBuildError(
            "correction manifest base reviewed plan hash is stale or foreign"
        )
    operations = correction_manifest.get("operations")
    if not isinstance(operations, list):
        raise CandidateBuildError("correction operations must be an array")

    candidate_by_id = {int(item["id"]): item for item in candidates}
    planned: list[tuple[dict[str, Any], str, Any]] = []
    paths_by_article: dict[int, list[str]] = {}
    corrected_ids: set[int] = set()
    for ordinal, raw_operation in enumerate(operations, 1):
        if not isinstance(raw_operation, dict):
            raise CandidateBuildError(
                f"correction operation {ordinal} must be an object"
            )
        operation_fields = set(raw_operation)
        if operation_fields != _OPERATION_FIELDS:
            raise CandidateBuildError(
                f"correction operation {ordinal} fields do not match schema: "
                f"missing={sorted(_OPERATION_FIELDS - operation_fields)}, "
                f"unknown={sorted(operation_fields - _OPERATION_FIELDS)}"
            )
        article_id = _positive_int(
            raw_operation.get("article_id"),
            field=f"correction operation {ordinal} article_id",
        )
        item = candidate_by_id.get(article_id)
        if item is None:
            raise CandidateBuildError(
                f"correction operation {ordinal} targets missing article "
                f"{article_id}"
            )
        if raw_operation.get("article_hash") != item["article_hash"]:
            raise CandidateBuildError(
                f"correction article_hash is stale for article {article_id}"
            )
        if raw_operation.get("generation_input_hash") != item[
            "generation_input_hash"
        ]:
            raise CandidateBuildError(
                "correction generation_input_hash is stale for article "
                f"{article_id}"
            )
        generation_context_hash = raw_operation.get(
            "generation_context_hash"
        )
        if (
            not _valid_sha256(generation_context_hash)
            or generation_context_hash != context_hash_by_id[article_id]
        ):
            raise CandidateBuildError(
                "correction generation_context_hash is stale for article "
                f"{article_id}"
            )
        path = _operation_path(raw_operation, article_id=article_id)
        prior_paths = paths_by_article.setdefault(article_id, [])
        if any(_overlapping_paths(path, prior) for prior in prior_paths):
            raise CandidateBuildError(
                f"correction paths overlap for article {article_id}: {path!r}"
            )
        prior_paths.append(path)
        actual_old = _read_path(item, path)
        if not _exact_json_equal(
            actual_old,
            raw_operation.get("old_value"),
        ):
            raise CandidateBuildError(
                f"correction old_value is stale for article {article_id} "
                f"path {path!r}"
            )
        new_value = raw_operation.get("new_value")
        if _exact_json_equal(actual_old, new_value):
            raise CandidateBuildError(
                f"correction old_value equals new_value for article "
                f"{article_id} path {path!r}"
            )
        planned.append((item, path, new_value))
        corrected_ids.add(article_id)

    # Every assertion above is evaluated against the immutable base.  Only
    # after the entire manifest is proven do mutations become visible.
    for item, path, new_value in planned:
        _write_path(item, path, new_value)

    result_hash = _payload_hash(candidates)
    if correction_manifest.get("result_reviewed_plan_hash") != result_hash:
        raise CandidateBuildError(
            "correction manifest result reviewed plan hash mismatch"
        )
    return {
        "schema": CORRECTION_SCHEMA,
        "correction_manifest_hash": stored_hash,
        "base_reviewed_plan_hash": base_hash,
        "result_reviewed_plan_hash": result_hash,
        "operations": len(operations),
        "article_ids": sorted(corrected_ids),
    }


def _context_hashes(
    loaded_batches: Sequence[
        tuple[
            str,
            str,
            str,
            Mapping[str, Any],
            list[dict[str, Any]],
        ]
    ],
) -> dict[int, str]:
    result: dict[int, str] = {}
    for (
        source_name,
        _reviewed_name_value,
        _source_file_sha256,
        source_batch,
        _items,
    ) in loaded_batches:
        for article in source_batch["articles"]:
            article_id = int(article["id"])
            context_hash = article.get("generation_context_hash")
            if not _valid_sha256(context_hash):
                raise CandidateBuildError(
                    f"source article {article_id} in {source_name} has an "
                    "invalid generation_context_hash"
                )
            result[article_id] = context_hash
    return result


def _reserve_and_write_directory(
    output_dir: Path,
    files: Sequence[tuple[str, bytes]],
) -> None:
    output_dir = Path(output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        raise CandidateBuildError(
            f"refusing to overwrite or create output directory: {exc}"
        ) from exc

    reserved: list[tuple[Path, int]] = []
    try:
        for name, _encoded in files:
            path = output_dir / name
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            reserved.append((path, descriptor))
        for (path, descriptor), (_name, encoded) in zip(reserved, files):
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
    except Exception as exc:
        for path, descriptor in reserved:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                path.unlink()
            except OSError:
                pass
        try:
            output_dir.rmdir()
        except OSError:
            pass
        raise CandidateBuildError(
            f"cannot create reviewed candidate outputs: {exc}"
        ) from exc


def _verify_export_files_unchanged(
    export_dir: Path,
    *,
    manifest_file_sha256: str,
    source_file_sha256s: Mapping[str, str],
) -> None:
    try:
        current_manifest_sha256 = _sha256_bytes(
            (export_dir / "manifest.json").read_bytes()
        )
        current_source_names = {
            path.name
            for path in export_dir.glob("source-batch-*.json")
            if path.is_file()
        }
        current_source_sha256s = {
            name: _sha256_bytes((export_dir / name).read_bytes())
            for name in source_file_sha256s
        }
    except OSError as exc:
        raise CandidateBuildError(
            f"cannot recheck immutable export inputs: {exc}"
        ) from exc
    if (
        current_manifest_sha256 != manifest_file_sha256
        or current_source_names != set(source_file_sha256s)
        or current_source_sha256s != dict(source_file_sha256s)
    ):
        raise CandidateBuildError(
            "native-v3 export changed while candidates were being built"
        )


def build_and_write_current_candidates(
    server_id: str,
    *,
    export_dir: Path,
    output_dir: Path,
    correction_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Build, fully validate, and exclusively write current-state candidates."""

    if not isinstance(server_id, str) or not server_id.strip():
        raise CandidateBuildError("server_id must be a non-empty string")
    export_dir = Path(export_dir)
    output_dir = Path(output_dir)
    if output_dir.resolve() == export_dir.resolve():
        raise CandidateBuildError("output directory must differ from export")

    try:
        (
            manifest,
            manifest_file_sha256,
            loaded_batches,
            ordered_candidates,
        ) = _load_export(server_id, export_dir)
    except AggregatePlanError as exc:
        raise CandidateBuildError(str(exc)) from exc

    context_hash_by_id = _context_hashes(loaded_batches)
    source_file_sha256s = {
        source_name: source_file_sha256
        for (
            source_name,
            _reviewed_name_value,
            source_file_sha256,
            _source_batch,
            _items,
        ) in loaded_batches
    }
    correction_bytes: bytes | None = None
    correction_evidence: dict[str, Any] | None = None
    if correction_manifest_path is not None:
        correction_path = Path(correction_manifest_path)
        try:
            correction_bytes = correction_path.read_bytes()
            correction_manifest = json.loads(
                correction_bytes.decode("utf-8")
            )
        except OSError as exc:
            raise CandidateBuildError(
                f"cannot read correction manifest {correction_path}: {exc}"
            ) from exc
        except UnicodeDecodeError as exc:
            raise CandidateBuildError(
                f"correction manifest is not UTF-8: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise CandidateBuildError(
                f"correction manifest is not valid JSON: {exc}"
            ) from exc
        correction_evidence = _apply_corrections(
            server_id=server_id,
            correction_manifest=correction_manifest,
            manifest=manifest,
            manifest_file_sha256=manifest_file_sha256,
            candidates=ordered_candidates,
            context_hash_by_id=context_hash_by_id,
        )
        correction_evidence = {
            **correction_evidence,
            "file": CORRECTION_COPY_FILE,
            "file_sha256": _sha256_bytes(correction_bytes),
        }

    # Ensure per-batch lists continue to reference the corrected objects and
    # stage the exact final bytes for the authoritative aggregate validator.
    candidate_bytes: list[tuple[str, bytes]] = [
        (reviewed_name, _encoded_json(items))
        for (
            _source_name,
            reviewed_name,
            _source_file_sha256,
            _source_batch,
            items,
        )
        in loaded_batches
    ]
    try:
        with TemporaryDirectory(prefix="law-reviewed-candidates-") as temp:
            staged_dir = Path(temp)
            for name, encoded in candidate_bytes:
                (staged_dir / name).write_bytes(encoded)
            validated_items, aggregate = build_plan(
                server_id,
                export_dir=export_dir,
                reviewed_dir=staged_dir,
            )
    except (AggregatePlanError, OSError) as exc:
        raise CandidateBuildError(
            f"reviewed candidate validation failed: {exc}"
        ) from exc

    if (
        aggregate["manifest_schema"] != EXPORT_SCHEMA
        or aggregate["manifest_hash"] != manifest["manifest_hash"]
        or aggregate["manifest_file_sha256"] != manifest_file_sha256
        or {
            str(item["source_file"]): str(item["source_file_sha256"])
            for item in aggregate["inputs"]
        }
        != source_file_sha256s
    ):
        raise CandidateBuildError(
            "native-v3 export changed while candidates were being built"
        )
    if validated_items != ordered_candidates:
        raise CandidateBuildError(
            "shared validation would normalize current/corrected values; "
            "an explicit exact correction is required"
        )
    if correction_evidence is not None:
        if correction_evidence["result_reviewed_plan_hash"] != aggregate[
            "reviewed_plan_hash"
        ]:
            raise CandidateBuildError(
                "correction result hash differs from validated reviewed plan"
            )

    report_body = {
        "schema": REPORT_SCHEMA,
        "ok": True,
        "server_id": server_id,
        "source_export": {
            "schema": EXPORT_SCHEMA,
            "manifest_file": "manifest.json",
            "manifest_file_sha256": manifest_file_sha256,
            "manifest_hash": manifest["manifest_hash"],
            "source_fingerprint": manifest["source_fingerprint"],
            "source_catalog_binding": manifest["source_catalog_binding"],
            "review_source_projection_hash": manifest[
                "review_source_projection_hash"
            ],
        },
        "corrections": correction_evidence,
        "articles": len(ordered_candidates),
        "article_ids": [int(item["id"]) for item in ordered_candidates],
        "generation_context_hashes": {
            str(article_id): context_hash_by_id[article_id]
            for article_id in [
                int(item["id"]) for item in ordered_candidates
            ]
        },
        "reviewed_plan_hash": aggregate["reviewed_plan_hash"],
        "batches": aggregate["inputs"],
        "validation": {
            "native_v3": True,
            "source_and_batch_hashes": True,
            "exact_coverage_and_groups": True,
            "source_evidence_and_tombstones": True,
            "reviewed_shape_and_canonical_claims": True,
        },
    }
    report = {
        **report_body,
        "report_hash": _payload_hash(report_body),
    }
    output_files = list(candidate_bytes)
    if correction_bytes is not None:
        output_files.append((CORRECTION_COPY_FILE, correction_bytes))
    # The report is deliberately last: its presence means every candidate
    # byte was validated and successfully written in the same create-once run.
    output_files.append((REPORT_FILE, _encoded_json(report)))
    _verify_export_files_unchanged(
        export_dir,
        manifest_file_sha256=manifest_file_sha256,
        source_file_sha256s=source_file_sha256s,
    )
    _reserve_and_write_directory(output_dir, output_files)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build create-once reviewed candidates from a native-v3 export "
            "without network, model, or database access"
        )
    )
    parser.add_argument("server_id")
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--corrections",
        "--correction-manifest",
        dest="correction_manifest",
        type=Path,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = build_and_write_current_candidates(
            args.server_id,
            export_dir=args.export_dir,
            output_dir=args.output_dir,
            correction_manifest_path=args.correction_manifest,
        )
    except CandidateBuildError as exc:
        print(
            json.dumps(
                {"ok": False, "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
