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
from scripts.export_law_enrichment_batches import (
    BATCH_SCHEMA,
    BATCH_SCHEMA_V1,
    _payload_hash,
)
from services.law_enrichment_artifact import (
    validate_law_source_fingerprint,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    validate_law_source_catalog_binding,
)
from services.law_catalog_service import (
    derive_catalog_fields,
    source_article_hash,
)
from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_enrichment_review_contract import (
    ReviewSourceContractError,
    review_source_projection_sources,
    validate_reviewed_source_projection,
)
from services.law_enrichment_validation import (
    validate_enrichment_source_evidence,
    validate_enrichment_title_source_evidence,
)
from services.law_synonym_service import (
    SynonymWriteError,
    canonical_synonym_term,
    is_explicitly_empty_or_repealed_article,
)


_SOURCE_BATCH_FIELDS_V1 = {
    "schema",
    "server_id",
    "source_fingerprint",
    "batch_number",
    "articles",
    "batch_hash",
}
_SOURCE_BATCH_FIELDS_V3 = _SOURCE_BATCH_FIELDS_V1 | {
    "context_contract",
    "source_catalog_binding",
    "review_source_projection",
}
_SOURCE_ARTICLE_FIELDS_V1 = {
    "id",
    "article_hash",
    "generation_input_hash",
    "source",
    "review",
}
_SOURCE_ARTICLE_FIELDS_V3 = _SOURCE_ARTICLE_FIELDS_V1 | {
    "generation_context_hash",
}
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
) -> tuple[dict[int, tuple[str, str]], dict[int, str], int, str]:
    if not isinstance(source_batch, dict):
        raise OfflineBatchValidationError(
            "source batch must contain one JSON object"
        )
    schema = source_batch.get("schema")
    if schema == BATCH_SCHEMA:
        expected_batch_fields = _SOURCE_BATCH_FIELDS_V3
        expected_article_fields = _SOURCE_ARTICLE_FIELDS_V3
    elif schema == BATCH_SCHEMA_V1:
        expected_batch_fields = _SOURCE_BATCH_FIELDS_V1
        expected_article_fields = _SOURCE_ARTICLE_FIELDS_V1
    else:
        raise OfflineBatchValidationError(
            f"unsupported source schema {schema!r}"
        )
    fields = set(source_batch)
    if fields != expected_batch_fields:
        raise OfflineBatchValidationError(
            "source batch fields do not match schema: "
            f"missing={sorted(expected_batch_fields - fields)}, "
            f"unknown={sorted(fields - expected_batch_fields)}"
        )
    if schema == BATCH_SCHEMA and source_batch.get(
        "context_contract"
    ) != _CONTEXT_CONTRACT:
        raise OfflineBatchValidationError(
            "source batch context contract differs from the active "
            "generation policy"
        )
    if source_batch.get("server_id") != expected_server_id:
        raise OfflineBatchValidationError(
            "source batch belongs to another server: "
            f"expected={expected_server_id!r}, "
            f"actual={source_batch.get('server_id')!r}"
        )
    try:
        source_fingerprint = validate_law_source_fingerprint(
            source_batch.get("source_fingerprint")
        )
    except ValueError as exc:
        raise OfflineBatchValidationError(
            f"source batch source_fingerprint is invalid: {exc}"
        ) from exc
    if schema == BATCH_SCHEMA and source_fingerprint[
        "snapshot_version"
    ] != 3:
        raise OfflineBatchValidationError(
            "source batch v3 fingerprint must bind actual content v3"
        )
    if schema == BATCH_SCHEMA:
        try:
            source_catalog_binding = validate_law_source_catalog_binding(
                source_batch.get("source_catalog_binding"),
                expected_server_id=expected_server_id,
            )
        except ValueError as exc:
            raise OfflineBatchValidationError(
                f"source batch source_catalog_binding is invalid: {exc}"
            ) from exc
        if source_catalog_binding["source_fingerprint"] != source_fingerprint:
            raise OfflineBatchValidationError(
                "source batch source/catalog binding differs from its exact "
                "source fingerprint"
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
    context_hashes: dict[int, str] = {}
    for index, article in enumerate(articles):
        if not isinstance(article, dict):
            raise OfflineBatchValidationError(
                f"source article at index {index} must be an object"
            )
        article_fields = set(article)
        if article_fields != expected_article_fields:
            raise OfflineBatchValidationError(
                f"source article at index {index} fields do not match schema: "
                f"missing={sorted(expected_article_fields - article_fields)}, "
                f"unknown={sorted(article_fields - expected_article_fields)}"
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
        if (
            schema == BATCH_SCHEMA
            and _payload_hash(nested_source) != generation_input_hash
        ):
            raise OfflineBatchValidationError(
                f"source article {article_id} generation_input_hash does not "
                "bind its exact nested source"
            )
        if not isinstance(article.get("review"), Mapping):
            raise OfflineBatchValidationError(
                f"source article {article_id} review must be an object"
            )
        identities[article_id] = (article_hash, generation_input_hash)
        if schema == BATCH_SCHEMA:
            generation_context_hash = article.get(
                "generation_context_hash"
            )
            if (
                not isinstance(generation_context_hash, str)
                or len(generation_context_hash) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in generation_context_hash
                )
            ):
                raise OfflineBatchValidationError(
                    f"source article {article_id} has invalid "
                    "generation_context_hash"
                )
            context_hashes[article_id] = generation_context_hash
    return identities, context_hashes, batch_number, stored_hash


def validate_batch(
    server_id: str,
    source_batch: Any,
    reviewed_artifact: Any,
) -> dict[str, Any]:
    """Validate one complete source/reviewed pair without external I/O."""

    source, context_hashes, batch_number, batch_hash = _source_identity(
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

    if source_batch["schema"] == BATCH_SCHEMA:
        try:
            projection_sources = review_source_projection_sources(
                source_batch["review_source_projection"]
            )
            source_articles = {
                int(article["id"]): article
                for article in source_batch["articles"]
            }
            if set(projection_sources) != set(source_articles):
                raise ReviewSourceContractError(
                    "batch article coverage differs from its review source "
                    "projection"
                )
            for article_id, projected_source in projection_sources.items():
                nested_source = source_articles[article_id]["source"]
                for source_field in (
                    "id",
                    "article_hash",
                    "law_document_id",
                    "source_order",
                    "code",
                    "article_ref",
                    "document",
                    "text",
                ):
                    if (
                        nested_source.get(source_field)
                        != projected_source.get(source_field)
                    ):
                        raise ReviewSourceContractError(
                            f"article {article_id} nested source field "
                            f"{source_field!r} differs from the source-only "
                            "projection"
                        )
            validate_reviewed_source_projection(
                source_batch["review_source_projection"],
                reviewed,
                catalog_fields_resolver=derive_catalog_fields,
                article_hash_resolver=source_article_hash,
                is_tombstone=is_explicitly_empty_or_repealed_article,
                canonicalize_scope=canonical_synonym_term,
                canonicalize_suffix=canonical_synonym_term,
            )
        except ReviewSourceContractError as exc:
            raise OfflineBatchValidationError(
                f"reviewed-source structural contract failed: {exc}"
            ) from exc
    else:
        # Historical v1 artifacts predate the hash-bound structural
        # projection.  Keep their original article-local evidence validation;
        # only the explicit online v1 upgrade may turn them into an envelope.
        source_articles = {
            int(article["id"]): article
            for article in source_batch["articles"]
        }
        for item in reviewed:
            article_id = int(item["id"])
            nested_source = source_articles[article_id]["source"]
            full_text = nested_source.get("text")
            if not isinstance(full_text, str):
                raise OfflineBatchValidationError(
                    f"source article {article_id} text must be a string"
                )
            try:
                source_context = nested_source.get("document")
                validate_enrichment_title_source_evidence(
                    full_text,
                    item["title"],
                    # Historical v1 did not bind parser-owned provenance.
                    # Apply the semantic generated-title gate here; the
                    # explicit online upgrade/live writer separately proves
                    # exact title ownership from the current parser snapshot.
                    source_title=None,
                    source_context=(
                        source_context
                        if isinstance(source_context, str)
                        else None
                    ),
                )
                validate_enrichment_source_evidence(
                    full_text,
                    item["penalty_summary"],
                    item["penalty"],
                    item["synonyms"],
                )
            except ValueError as exc:
                evidence_kind = (
                    "title"
                    if str(exc).startswith("title evidence:")
                    else (
                        "synonym"
                        if str(exc).startswith("synonym evidence:")
                        else "penalty"
                    )
                )
                raise OfflineBatchValidationError(
                    f"reviewed article {article_id} {evidence_kind} is not "
                    f"supported by its source: {exc}"
                ) from exc

    return {
        "ok": True,
        "schema": source_batch["schema"],
        "server_id": server_id,
        "batch_number": batch_number,
        "batch_hash": batch_hash,
        "articles": len(source),
        "generation_context_hashes": context_hashes,
        **(
            {
                "source_catalog_binding": dict(
                    source_batch["source_catalog_binding"]
                ),
            }
            if source_batch["schema"] == BATCH_SCHEMA
            else {}
        ),
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
