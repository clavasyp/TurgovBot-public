#!/usr/bin/env python3
"""Offline validation for one reviewed Phase A enrichment batch.

The validator reads an immutable source batch and its reviewed JSON artifact,
then checks their complete identity contract.  It deliberately has no database
or network path and never writes a file.  Live CAS/collision checks remain the
responsibility of ``_apply_claude_enrich.py``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts._apply_claude_enrich import _validated_items
from scripts.export_law_enrichment_batches import BATCH_SCHEMA, _payload_hash
from services.law_synonym_service import SynonymWriteError


_SOURCE_BATCH_FIELDS = {
    "schema",
    "server_id",
    "source_fingerprint",
    "batch_number",
    "articles",
    "batch_hash",
}
_SOURCE_ARTICLE_FIELDS = {
    "id",
    "article_hash",
    "generation_input_hash",
    "source",
    "review",
}


class OfflineBatchValidationError(ValueError):
    """The immutable source/reviewed artifact contract is invalid."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OfflineBatchValidationError(
            f"cannot read {path}: {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OfflineBatchValidationError(
            f"{path} is not valid JSON: {exc}"
        ) from exc


def _source_identity(
    source_batch: Any,
    *,
    expected_server_id: str,
) -> tuple[dict[int, tuple[str, str]], int, str]:
    if not isinstance(source_batch, dict):
        raise OfflineBatchValidationError(
            "source batch must contain one JSON object"
        )
    fields = set(source_batch)
    if fields != _SOURCE_BATCH_FIELDS:
        raise OfflineBatchValidationError(
            "source batch fields do not match schema: "
            f"missing={sorted(_SOURCE_BATCH_FIELDS - fields)}, "
            f"unknown={sorted(fields - _SOURCE_BATCH_FIELDS)}"
        )
    if source_batch.get("schema") != BATCH_SCHEMA:
        raise OfflineBatchValidationError(
            f"expected source schema {BATCH_SCHEMA!r}, "
            f"got {source_batch.get('schema')!r}"
        )
    if source_batch.get("server_id") != expected_server_id:
        raise OfflineBatchValidationError(
            "source batch belongs to another server: "
            f"expected={expected_server_id!r}, "
            f"actual={source_batch.get('server_id')!r}"
        )
    if not isinstance(source_batch.get("source_fingerprint"), Mapping):
        raise OfflineBatchValidationError(
            "source batch source_fingerprint must be an object"
        )
    batch_number = source_batch.get("batch_number")
    if (
        isinstance(batch_number, bool)
        or not isinstance(batch_number, int)
        or batch_number < 1
    ):
        raise OfflineBatchValidationError(
            "source batch_number must be a positive integer"
        )

    stored_hash = source_batch.get("batch_hash")
    if not isinstance(stored_hash, str) or not stored_hash:
        raise OfflineBatchValidationError(
            "source batch_hash must be a non-empty string"
        )
    hash_body = dict(source_batch)
    hash_body.pop("batch_hash")
    actual_hash = _payload_hash(hash_body)
    if stored_hash != actual_hash:
        raise OfflineBatchValidationError(
            "source batch_hash mismatch: "
            f"stored={stored_hash!r}, actual={actual_hash!r}"
        )

    articles = source_batch.get("articles")
    if not isinstance(articles, list) or not articles:
        raise OfflineBatchValidationError(
            "source batch articles must be a non-empty array"
        )
    identities: dict[int, tuple[str, str]] = {}
    for index, article in enumerate(articles):
        if not isinstance(article, dict):
            raise OfflineBatchValidationError(
                f"source article at index {index} must be an object"
            )
        article_fields = set(article)
        if article_fields != _SOURCE_ARTICLE_FIELDS:
            raise OfflineBatchValidationError(
                f"source article at index {index} fields do not match schema: "
                f"missing={sorted(_SOURCE_ARTICLE_FIELDS - article_fields)}, "
                f"unknown={sorted(article_fields - _SOURCE_ARTICLE_FIELDS)}"
            )
        article_id = article.get("id")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise OfflineBatchValidationError(
                f"source article at index {index} has invalid id"
            )
        if article_id in identities:
            raise OfflineBatchValidationError(
                f"source batch contains duplicate article id {article_id}"
            )
        article_hash = article.get("article_hash")
        generation_input_hash = article.get("generation_input_hash")
        if not isinstance(article_hash, str) or not article_hash:
            raise OfflineBatchValidationError(
                f"source article {article_id} has invalid article_hash"
            )
        if (
            not isinstance(generation_input_hash, str)
            or len(generation_input_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in generation_input_hash
            )
        ):
            raise OfflineBatchValidationError(
                f"source article {article_id} has invalid generation_input_hash"
            )
        nested_source = article.get("source")
        if not isinstance(nested_source, Mapping):
            raise OfflineBatchValidationError(
                f"source article {article_id} source must be an object"
            )
        if (
            nested_source.get("id") != article_id
            or nested_source.get("article_hash") != article_hash
        ):
            raise OfflineBatchValidationError(
                f"source article {article_id} nested identity mismatch"
            )
        if not isinstance(article.get("review"), Mapping):
            raise OfflineBatchValidationError(
                f"source article {article_id} review must be an object"
            )
        identities[article_id] = (article_hash, generation_input_hash)
    return identities, batch_number, stored_hash


def validate_batch(
    server_id: str,
    source_batch: Any,
    reviewed_artifact: Any,
) -> dict[str, Any]:
    """Validate one complete source/reviewed pair without external I/O."""

    source, batch_number, batch_hash = _source_identity(
        source_batch,
        expected_server_id=server_id,
    )
    try:
        reviewed = _validated_items(reviewed_artifact)
    except SynonymWriteError as exc:
        raise OfflineBatchValidationError(
            f"reviewed artifact schema is invalid: {exc}"
        ) from exc

    reviewed_identity = {
        int(item["id"]): (
            str(item["article_hash"]),
            str(item["generation_input_hash"]),
        )
        for item in reviewed
    }
    source_ids = set(source)
    reviewed_ids = set(reviewed_identity)
    missing = sorted(source_ids - reviewed_ids)
    unexpected = sorted(reviewed_ids - source_ids)
    if missing or unexpected or len(reviewed) != len(source):
        raise OfflineBatchValidationError(
            "reviewed artifact coverage is not exact: "
            f"missing={missing}, unexpected={unexpected}, "
            f"expected_count={len(source)}, actual_count={len(reviewed)}"
        )

    mismatched = [
        article_id
        for article_id in sorted(source_ids)
        if reviewed_identity[article_id] != source[article_id]
    ]
    if mismatched:
        raise OfflineBatchValidationError(
            "reviewed article_hash/generation_input_hash differs from source "
            f"for article ids: {mismatched}"
        )

    return {
        "ok": True,
        "schema": BATCH_SCHEMA,
        "server_id": server_id,
        "batch_number": batch_number,
        "batch_hash": batch_hash,
        "articles": len(source),
    }


def validate_files(
    server_id: str,
    source_batch_path: Path,
    reviewed_artifact_path: Path,
) -> dict[str, Any]:
    return validate_batch(
        server_id,
        _read_json(source_batch_path),
        _read_json(reviewed_artifact_path),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Offline exact-coverage validation of one Phase A source batch "
            "and its reviewed enrichment JSON"
        )
    )
    parser.add_argument("server_id")
    parser.add_argument("source_batch", type=Path)
    parser.add_argument("reviewed_json", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_files(
            args.server_id,
            args.source_batch,
            args.reviewed_json,
        )
    except OfflineBatchValidationError as exc:
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
