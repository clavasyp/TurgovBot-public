"""Shared immutable source contract for reviewed law enrichment artifacts.

The native-v3 apply envelope is deliberately stronger than a reviewed JSON
array.  It binds the reviewed output to the complete law-document source
fingerprint, the parser-replayed source-to-catalog generation, and every
target's generation-time source-only enrichment-context digest.  The live
writer must verify all of them before it is allowed to mutate the database.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_source_snapshot import SOURCE_CONTENT_FINGERPRINT_VERSION
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    validate_law_source_catalog_binding,
)


APPLY_ENVELOPE_SCHEMA = "law-full-enrichment-apply-envelope-v3"
APPLY_ENVELOPE_EVIDENCE_SCHEMA = (
    "law-full-enrichment-apply-input-evidence-v3"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_MODES = frozenset({"native_v3", "verified_v1_upgrade"})
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
_BATCH_INPUT_FIELDS = {
    "batch_number",
    "source_file",
    "source_file_sha256",
    "batch_hash",
    "reviewed_file",
    "reviewed_file_sha256",
    "articles",
    "article_ids",
}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def normalize_penalty_json(value: Any) -> dict[str, Any] | None:
    """Normalize asyncpg's default JSON string and already-decoded objects."""

    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("penalty_json is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("penalty_json must contain an object or null")
    return dict(value)


def enrichment_generation_source(
    article: Mapping[str, Any],
    auto_synonyms: Iterable[str],
) -> dict[str, Any]:
    """Return the exact current-state payload bound by a reviewed artifact."""

    article_id = article.get("id")
    document_id = article.get("law_document_id")
    source_order = article.get("source_order")
    if (
        isinstance(article_id, bool)
        or not isinstance(article_id, int)
        or article_id < 1
        or isinstance(document_id, bool)
        or not isinstance(document_id, int)
        or document_id < 1
        or isinstance(source_order, bool)
        or not isinstance(source_order, int)
        or source_order < 0
    ):
        raise ValueError(
            "generation source ids/position must be exact integers"
        )
    return {
        "id": article_id,
        "article_hash": str(article["article_hash"]),
        "law_document_id": document_id,
        "source_order": source_order,
        "code": article.get("code"),
        "article_ref": article.get("article_ref"),
        "catalog_title": article.get("title"),
        "document": article.get("doc_title"),
        "text": article.get("full_text"),
        "current_penalty_summary": article.get("penalty_summary"),
        "current_penalty": normalize_penalty_json(
            article.get("penalty_json")
        ),
        "current_auto_synonyms": sorted(
            str(term) for term in auto_synonyms
        ),
    }


def enrichment_generation_input_hash(
    article: Mapping[str, Any],
    auto_synonyms: Iterable[str],
) -> str:
    return hashlib.sha256(
        _canonical_json(
            enrichment_generation_source(article, auto_synonyms)
        )
    ).hexdigest()


def reviewed_enrichment_artifact_hash(
    item: Mapping[str, Any],
) -> str:
    """Bind replay evidence to the exact validated reviewed artifact."""

    return hashlib.sha256(_canonical_json(dict(item))).hexdigest()


def payload_sha256(value: Any) -> str:
    """Return the canonical JSON SHA-256 used by immutable artifact metadata."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _validated_sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _validated_positive_ids(value: Any, *, field: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    result: list[int] = []
    for article_id in value:
        if (
            isinstance(article_id, bool)
            or not isinstance(article_id, int)
            or article_id < 1
        ):
            raise ValueError(
                f"{field} must contain positive integer article ids"
            )
        result.append(article_id)
    if len(result) != len(set(result)):
        raise ValueError(f"{field} contains duplicate article ids")
    return result


def _validate_batch_input_evidence(
    value: Any,
    *,
    field: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    result: list[dict[str, Any]] = []
    seen_batches: set[int] = set()
    covered_ids: set[int] = set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != _BATCH_INPUT_FIELDS:
            raise ValueError(
                f"{field}[{index}] fields do not match schema"
            )
        batch_number = item.get("batch_number")
        articles = item.get("articles")
        if (
            isinstance(batch_number, bool)
            or not isinstance(batch_number, int)
            or batch_number < 1
            or batch_number in seen_batches
        ):
            raise ValueError(
                f"{field}[{index}] has invalid/duplicate batch_number"
            )
        if (
            isinstance(articles, bool)
            or not isinstance(articles, int)
            or articles < 1
        ):
            raise ValueError(f"{field}[{index}] has invalid articles")
        article_ids = _validated_positive_ids(
            item.get("article_ids"),
            field=f"{field}[{index}].article_ids",
        )
        if articles != len(article_ids):
            raise ValueError(
                f"{field}[{index}] article count/coverage mismatch"
            )
        duplicates = covered_ids.intersection(article_ids)
        if duplicates:
            raise ValueError(
                f"{field} repeats article ids across batches"
            )
        covered_ids.update(article_ids)
        seen_batches.add(batch_number)
        for name in ("source_file", "reviewed_file"):
            filename = item.get(name)
            if (
                not isinstance(filename, str)
                or not filename
                or "/" in filename
                or "\\" in filename
            ):
                raise ValueError(
                    f"{field}[{index}].{name} must be a plain filename"
                )
        for name in (
            "source_file_sha256",
            "batch_hash",
            "reviewed_file_sha256",
        ):
            _validated_sha256(
                item.get(name),
                field=f"{field}[{index}].{name}",
            )
        result.append(dict(item))
    if sorted(seen_batches) != list(range(1, len(result) + 1)):
        raise ValueError(f"{field} batch numbers must be sequential")
    return result


def validate_law_source_fingerprint(value: Any) -> dict[str, Any]:
    """Validate and normalize the complete server source fingerprint."""

    if not isinstance(value, Mapping):
        raise ValueError("source_fingerprint must be an object")
    expected_fields = {
        "algorithm",
        "snapshot_version",
        "documents",
        "value",
    }
    fields = set(value)
    if fields != expected_fields:
        raise ValueError(
            "source_fingerprint fields do not match schema: "
            f"missing={sorted(expected_fields - fields)}, "
            f"unknown={sorted(fields - expected_fields)}"
        )
    if value.get("algorithm") != "sha256":
        raise ValueError("source_fingerprint algorithm must be sha256")
    snapshot_version = value.get("snapshot_version")
    documents = value.get("documents")
    if (
        isinstance(snapshot_version, bool)
        or not isinstance(snapshot_version, int)
        or snapshot_version < 1
    ):
        raise ValueError(
            "source_fingerprint snapshot_version must be a positive integer"
        )
    if (
        isinstance(documents, bool)
        or not isinstance(documents, int)
        or documents < 1
    ):
        raise ValueError(
            "source_fingerprint documents must be a positive integer"
        )
    return {
        "algorithm": "sha256",
        "snapshot_version": snapshot_version,
        "documents": documents,
        "value": _validated_sha256(
            value.get("value"),
            field="source_fingerprint value",
        ),
    }


def _validated_context_contract(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or dict(value) != _CONTEXT_CONTRACT:
        raise ValueError(f"{field} differs from the active context contract")
    return dict(_CONTEXT_CONTRACT)


def _validated_context_hash_mapping(
    value: Any,
    *,
    article_ids: Sequence[int],
    field: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    expected_keys = {str(article_id) for article_id in article_ids}
    if set(value) != expected_keys:
        raise ValueError(f"{field} coverage differs from articles")
    result: dict[str, str] = {}
    for article_id in article_ids:
        digest = value.get(str(article_id))
        result[str(article_id)] = _validated_sha256(
            digest,
            field=f"{field} article {article_id}",
        )
    return result


def _validated_selection(
    selection_mode: Any,
    requested_article_ids: Any,
    *,
    article_ids: Sequence[int],
    field: str,
) -> tuple[str, list[int] | None]:
    if selection_mode == "pending_enrichment":
        if requested_article_ids is not None:
            raise ValueError(
                f"{field} pending selection must have null requested ids"
            )
        return selection_mode, None
    if selection_mode != "explicit_articles":
        raise ValueError(f"{field} selection_mode is not supported")
    requested = _validated_positive_ids(
        requested_article_ids,
        field=f"{field} requested_article_ids",
    )
    if requested != sorted(requested):
        raise ValueError(f"{field} requested article ids must be sorted")
    if not set(requested).issubset(article_ids):
        raise ValueError(
            f"{field} requested article ids are outside reviewed closure"
        )
    return selection_mode, requested


def _validate_named_file_hashes(
    value: Any,
    *,
    field: str,
    expected_fields: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError(f"{field} fields do not match schema")
    result = dict(value)
    for name in expected_fields:
        if name == "schema":
            continue
        if name.endswith("file"):
            filename = value.get(name)
            if (
                not isinstance(filename, str)
                or not filename
                or "/" in filename
                or "\\" in filename
            ):
                raise ValueError(f"{field}.{name} must be a plain filename")
        elif name.endswith("hash") or name.endswith("sha256"):
            _validated_sha256(
                value.get(name),
                field=f"{field}.{name}",
            )
    return result


def _validated_evidence_body(
    mode: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    if mode == "native_v3":
        expected = {
            "aggregate_schema",
            "manifest_schema",
            "manifest_file",
            "manifest_file_sha256",
            "manifest_hash",
            "article_ids",
            "inputs",
            "source_fingerprint",
            "source_catalog_binding",
            "context_contract",
            "generation_context_hashes",
            "review_source_projection_hash",
            "reviewed_plan_hash",
            "selection_mode",
            "requested_article_ids",
        }
        if set(body) != expected:
            raise ValueError(
                "native_v3 envelope evidence body fields do not match schema"
            )
        if body.get("aggregate_schema") != (
            "law-full-enrichment-aggregate-evidence-v3"
        ):
            raise ValueError(
                "native_v3 aggregate evidence schema is not supported"
            )
        if body.get("manifest_schema") != (
            "law-full-enrichment-export-v3"
        ):
            raise ValueError(
                "native_v3 manifest evidence schema is not supported"
            )
        manifest_file = body.get("manifest_file")
        if (
            not isinstance(manifest_file, str)
            or not manifest_file
            or "/" in manifest_file
            or "\\" in manifest_file
        ):
            raise ValueError(
                "native_v3 manifest_file must be a plain filename"
            )
        _validated_sha256(
            body.get("manifest_file_sha256"),
            field="native_v3 manifest_file_sha256",
        )
        _validated_sha256(
            body.get("manifest_hash"),
            field="native_v3 manifest_hash",
        )
        article_ids = _validated_positive_ids(
            body.get("article_ids"),
            field="native_v3 article_ids",
        )
        inputs = _validate_batch_input_evidence(
            body.get("inputs"),
            field="native_v3 inputs",
        )
        covered_ids = [
            article_id
            for item in inputs
            for article_id in item["article_ids"]
        ]
        if covered_ids != article_ids:
            raise ValueError(
                "native_v3 evidence input coverage/order differs from "
                "article_ids"
            )
        source_fingerprint = validate_law_source_fingerprint(
            body.get("source_fingerprint")
        )
        if (
            source_fingerprint["snapshot_version"]
            != SOURCE_CONTENT_FINGERPRINT_VERSION
        ):
            raise ValueError(
                "native_v3 source fingerprint must bind actual content v3"
            )
        source_catalog_binding = validate_law_source_catalog_binding(
            body.get("source_catalog_binding")
        )
        if (
            source_catalog_binding["source_fingerprint"]
            != source_fingerprint
        ):
            raise ValueError(
                "native_v3 source/catalog binding differs from source "
                "fingerprint"
            )
        _validated_context_contract(
            body.get("context_contract"),
            field="native_v3 context_contract",
        )
        _validated_context_hash_mapping(
            body.get("generation_context_hashes"),
            article_ids=article_ids,
            field="native_v3 generation_context_hashes",
        )
        _validated_sha256(
            body.get("review_source_projection_hash"),
            field="native_v3 review_source_projection_hash",
        )
        _validated_sha256(
            body.get("reviewed_plan_hash"),
            field="native_v3 reviewed_plan_hash",
        )
        _validated_selection(
            body.get("selection_mode"),
            body.get("requested_article_ids"),
            article_ids=article_ids,
            field="native_v3",
        )
        return dict(body)

    expected = {
        "legacy_manifest",
        "legacy_inputs",
        "legacy_plan",
        "legacy_evidence",
        "article_ids",
        "selection_mode",
        "requested_article_ids",
        "source_fingerprint",
        "upgrade_binding",
    }
    if set(body) != expected:
        raise ValueError(
            "verified_v1_upgrade evidence body fields do not match schema"
        )
    legacy_manifest = _validate_named_file_hashes(
        body.get("legacy_manifest"),
        field="verified_v1_upgrade legacy_manifest",
        expected_fields={
            "schema",
            "file",
            "file_sha256",
            "manifest_hash",
        },
    )
    if legacy_manifest.get("schema") != "law-full-enrichment-export-v1":
        raise ValueError(
            "verified_v1_upgrade manifest schema is not supported"
        )
    legacy_plan = _validate_named_file_hashes(
        body.get("legacy_plan"),
        field="verified_v1_upgrade legacy_plan",
        expected_fields={"file", "file_sha256", "payload_hash"},
    )
    legacy_evidence = _validate_named_file_hashes(
        body.get("legacy_evidence"),
        field="verified_v1_upgrade legacy_evidence",
        expected_fields={
            "schema",
            "file",
            "file_sha256",
            "evidence_hash",
        },
    )
    if legacy_evidence.get("schema") != (
        "law-full-enrichment-aggregate-evidence-v1"
    ):
        raise ValueError(
            "verified_v1_upgrade aggregate evidence schema is not supported"
        )
    article_ids = _validated_positive_ids(
        body.get("article_ids"),
        field="verified_v1_upgrade article_ids",
    )
    legacy_inputs = _validate_batch_input_evidence(
        body.get("legacy_inputs"),
        field="verified_v1_upgrade legacy_inputs",
    )
    if [
        article_id
        for item in legacy_inputs
        for article_id in item["article_ids"]
    ] != article_ids:
        raise ValueError(
            "verified_v1_upgrade input coverage/order differs from article_ids"
        )
    selection_mode, requested_ids = _validated_selection(
        body.get("selection_mode"),
        body.get("requested_article_ids"),
        article_ids=article_ids,
        field="verified_v1_upgrade",
    )
    source_fingerprint = validate_law_source_fingerprint(
        body.get("source_fingerprint")
    )
    if source_fingerprint["snapshot_version"] != 2:
        raise ValueError(
            "verified_v1_upgrade historical source fingerprint must be v2"
        )
    binding = body.get("upgrade_binding")
    if not isinstance(binding, Mapping) or set(binding) != {
        "source_fingerprint_verified",
        "generation_snapshot_value",
        "generation_context_hashes",
        "authoritative_incomplete_article_ids",
        "live_source_fingerprint",
        "source_catalog_binding",
        "context_contract",
        "reviewed_plan_hash",
    }:
        raise ValueError(
            "verified_v1_upgrade upgrade_binding fields do not match schema"
        )
    if binding.get("source_fingerprint_verified") is not True:
        raise ValueError(
            "verified_v1_upgrade source fingerprint is not attested"
        )
    _validated_sha256(
        binding.get("generation_snapshot_value"),
        field="verified_v1_upgrade generation_snapshot_value",
    )
    context_hashes = _validated_context_hash_mapping(
        binding.get("generation_context_hashes"),
        article_ids=article_ids,
        field="verified_v1_upgrade generation_context_hashes",
    )
    live_source_fingerprint = validate_law_source_fingerprint(
        binding.get("live_source_fingerprint")
    )
    if (
        live_source_fingerprint["snapshot_version"]
        != SOURCE_CONTENT_FINGERPRINT_VERSION
    ):
        raise ValueError(
            "verified_v1_upgrade live source fingerprint must bind actual "
            "content v3"
        )
    source_catalog_binding = validate_law_source_catalog_binding(
        binding.get("source_catalog_binding")
    )
    if (
        source_catalog_binding["source_fingerprint"]
        != live_source_fingerprint
    ):
        raise ValueError(
            "verified_v1_upgrade source/catalog binding differs from live "
            "source fingerprint"
        )
    _validated_context_contract(
        binding.get("context_contract"),
        field="verified_v1_upgrade context_contract",
    )
    _validated_sha256(
        binding.get("reviewed_plan_hash"),
        field="verified_v1_upgrade reviewed_plan_hash",
    )
    incomplete_ids = binding.get("authoritative_incomplete_article_ids")
    if selection_mode == "pending_enrichment":
        incomplete_ids = _validated_positive_ids(
            incomplete_ids,
            field=(
                "verified_v1_upgrade "
                "authoritative_incomplete_article_ids"
            ),
        )
        if incomplete_ids != sorted(incomplete_ids):
            raise ValueError(
                "verified_v1_upgrade authoritative incomplete article ids "
                "must be sorted"
            )
        if not set(incomplete_ids).issubset(article_ids):
            raise ValueError(
                "verified_v1_upgrade authoritative incomplete article ids "
                "are outside reviewed exact-group closure"
            )
    elif incomplete_ids is not None:
        raise ValueError(
            "explicit v1 upgrade must have null authoritative incomplete "
            "article ids"
        )
    # Touch validated nested values so accidental future mutation cannot make
    # these calls look dead to static reviewers.
    assert legacy_plan and source_fingerprint
    return dict(body)


def _validate_envelope_evidence(value: Any) -> dict[str, Any]:
    """Validate the immutable-input evidence carried inside an envelope.

    The mode-specific body intentionally remains extensible: its canonical
    hash binds every manifest/batch/plan/evidence file identity emitted by the
    offline builder or explicit legacy upgrader.  Requiring a non-empty body
    and a matching hash prevents an apply file from silently dropping that
    provenance while keeping the reviewed items.
    """

    if not isinstance(value, Mapping):
        raise ValueError("envelope evidence must be an object")
    expected_fields = {"schema", "mode", "body", "evidence_hash"}
    fields = set(value)
    if fields != expected_fields:
        raise ValueError(
            "envelope evidence fields do not match schema: "
            f"missing={sorted(expected_fields - fields)}, "
            f"unknown={sorted(fields - expected_fields)}"
        )
    if value.get("schema") != APPLY_ENVELOPE_EVIDENCE_SCHEMA:
        raise ValueError(
            "envelope evidence schema is not supported"
        )
    mode = value.get("mode")
    if mode not in _EVIDENCE_MODES:
        raise ValueError("envelope evidence mode is not supported")
    body = value.get("body")
    if not isinstance(body, Mapping) or not body:
        raise ValueError("envelope evidence body must be a non-empty object")
    normalized_body = _validated_evidence_body(mode, body)
    stored_hash = _validated_sha256(
        value.get("evidence_hash"),
        field="envelope evidence_hash",
    )
    expected_hash = payload_sha256(
        {
            "schema": APPLY_ENVELOPE_EVIDENCE_SCHEMA,
            "mode": mode,
            "body": normalized_body,
        }
    )
    if stored_hash != expected_hash:
        raise ValueError(
            "envelope evidence_hash mismatch"
        )
    return {
        "schema": APPLY_ENVELOPE_EVIDENCE_SCHEMA,
        "mode": mode,
        "body": normalized_body,
        "evidence_hash": stored_hash,
    }


def _cross_validate_envelope_evidence(
    evidence: Mapping[str, Any],
    *,
    article_ids: Sequence[int],
    articles: Sequence[Mapping[str, Any]],
    source_fingerprint: Mapping[str, Any],
    source_catalog_binding: Mapping[str, Any],
    context_contract: Mapping[str, Any],
    generation_context_hashes: Mapping[int, str],
) -> tuple[str, list[int] | None]:
    """Bind mode-specific immutable evidence to the exact outer envelope."""

    body = evidence["body"]
    if list(body.get("article_ids") or []) != list(article_ids):
        raise ValueError(
            "envelope evidence article coverage/order differs from articles"
        )
    expected_plan_hash = payload_sha256(
        [dict(article) for article in articles]
    )
    context_map = {
        str(article_id): generation_context_hashes[article_id]
        for article_id in article_ids
    }
    if evidence["mode"] == "native_v3":
        if body.get("reviewed_plan_hash") != expected_plan_hash:
            raise ValueError(
                "native envelope reviewed plan hash differs from articles"
            )
        if dict(body.get("source_fingerprint") or {}) != dict(
            source_fingerprint
        ):
            raise ValueError(
                "native envelope source fingerprint differs from evidence"
            )
        if dict(body.get("source_catalog_binding") or {}) != dict(
            source_catalog_binding
        ):
            raise ValueError(
                "native envelope source/catalog binding differs from "
                "evidence"
            )
        if dict(body.get("context_contract") or {}) != dict(
            context_contract
        ):
            raise ValueError(
                "native envelope context contract differs from evidence"
            )
        if dict(body.get("generation_context_hashes") or {}) != context_map:
            raise ValueError(
                "native envelope generation contexts differ from evidence"
            )
        return _validated_selection(
            body.get("selection_mode"),
            body.get("requested_article_ids"),
            article_ids=article_ids,
            field="native envelope",
        )

    binding = body["upgrade_binding"]
    if binding.get("reviewed_plan_hash") != expected_plan_hash:
        raise ValueError(
            "verified v1 envelope reviewed plan hash differs from articles"
        )
    if dict(binding.get("live_source_fingerprint") or {}) != dict(
        source_fingerprint
    ):
        raise ValueError(
            "verified v1 live source fingerprint differs from envelope"
        )
    if dict(binding.get("source_catalog_binding") or {}) != dict(
        source_catalog_binding
    ):
        raise ValueError(
            "verified v1 source/catalog binding differs from envelope"
        )
    if dict(binding.get("context_contract") or {}) != dict(
        context_contract
    ):
        raise ValueError(
            "verified v1 context contract differs from envelope"
        )
    if dict(binding.get("generation_context_hashes") or {}) != context_map:
        raise ValueError(
            "verified v1 generation contexts differ from envelope"
        )
    return _validated_selection(
        body.get("selection_mode"),
        body.get("requested_article_ids"),
        article_ids=article_ids,
        field="verified v1 envelope",
    )


def build_apply_envelope_evidence(
    mode: str,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """Build canonical immutable-input evidence for a v3 apply envelope."""

    base = {
        "schema": APPLY_ENVELOPE_EVIDENCE_SCHEMA,
        "mode": mode,
        "body": dict(body),
    }
    return _validate_envelope_evidence(
        {
            **base,
            "evidence_hash": payload_sha256(base),
        }
    )


def build_law_enrichment_apply_envelope(
    *,
    server_id: str,
    source_fingerprint: Mapping[str, Any],
    source_catalog_binding: Mapping[str, Any],
    context_contract: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    generation_context_hashes: Mapping[int, str],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a deterministic native-v3 envelope for one atomic server apply."""

    if not isinstance(server_id, str) or not server_id.strip():
        raise ValueError("server_id must be a non-empty string")
    if not isinstance(items, Sequence) or isinstance(
        items, (str, bytes, bytearray)
    ):
        raise ValueError("envelope articles must be an array")
    normalized_items = [dict(item) for item in items]
    if not normalized_items:
        raise ValueError("envelope articles must not be empty")
    article_ids: list[int] = []
    for item in normalized_items:
        article_id = item.get("id")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise ValueError("every envelope article must have an integer id")
        article_ids.append(article_id)
    if len(article_ids) != len(set(article_ids)):
        raise ValueError("envelope contains duplicate article ids")

    if not isinstance(generation_context_hashes, Mapping):
        raise ValueError(
            "generation_context_hashes must be an object"
        )
    actual_context_ids = set()
    for article_id in generation_context_hashes:
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise ValueError(
                "generation_context_hashes contains an invalid article id"
            )
        actual_context_ids.add(article_id)
    expected_ids = set(article_ids)
    if actual_context_ids != expected_ids:
        raise ValueError(
            "generation_context_hash coverage must exactly match articles"
        )
    contexts = [
        {
            "article_id": article_id,
            "enrichment_context_hash": _validated_sha256(
                generation_context_hashes[article_id],
                field=(
                    f"article {article_id} generation enrichment context hash"
                ),
            ),
        }
        for article_id in article_ids
    ]
    normalized_source_fingerprint = validate_law_source_fingerprint(
        source_fingerprint
    )
    if (
        normalized_source_fingerprint["snapshot_version"]
        != SOURCE_CONTENT_FINGERPRINT_VERSION
    ):
        raise ValueError(
            "apply envelope source fingerprint must bind actual content v3"
        )
    normalized_source_catalog_binding = validate_law_source_catalog_binding(
        source_catalog_binding,
        expected_server_id=server_id,
    )
    if (
        normalized_source_catalog_binding["source_fingerprint"]
        != normalized_source_fingerprint
    ):
        raise ValueError(
            "apply envelope source/catalog binding differs from its source "
            "fingerprint"
        )
    normalized_context_contract = _validated_context_contract(
        context_contract,
        field="apply envelope context_contract",
    )
    normalized_evidence = _validate_envelope_evidence(evidence)
    _cross_validate_envelope_evidence(
        normalized_evidence,
        article_ids=article_ids,
        articles=normalized_items,
        source_fingerprint=normalized_source_fingerprint,
        source_catalog_binding=normalized_source_catalog_binding,
        context_contract=normalized_context_contract,
        generation_context_hashes={
            item["article_id"]: item["enrichment_context_hash"]
            for item in contexts
        },
    )
    body = {
        "schema": APPLY_ENVELOPE_SCHEMA,
        "server_id": server_id,
        "source_fingerprint": normalized_source_fingerprint,
        "source_catalog_binding": normalized_source_catalog_binding,
        "context_contract": normalized_context_contract,
        "generation_contexts": contexts,
        "articles": normalized_items,
        "evidence": normalized_evidence,
    }
    return {
        **body,
        "envelope_hash": payload_sha256(body),
    }


def validate_law_enrichment_apply_envelope(
    value: Any,
    *,
    expected_server_id: str,
) -> dict[str, Any]:
    """Fail closed on a bare array or any pre-native-v3 apply envelope."""

    if not isinstance(value, Mapping):
        raise ValueError(
            "apply artifact must be a versioned v3 envelope; bare JSON arrays "
            "are not accepted"
        )
    expected_fields = {
        "schema",
        "server_id",
        "source_fingerprint",
        "source_catalog_binding",
        "context_contract",
        "generation_contexts",
        "articles",
        "evidence",
        "envelope_hash",
    }
    fields = set(value)
    if fields != expected_fields:
        raise ValueError(
            "apply envelope fields do not match schema: "
            f"missing={sorted(expected_fields - fields)}, "
            f"unknown={sorted(fields - expected_fields)}"
        )
    if value.get("schema") != APPLY_ENVELOPE_SCHEMA:
        raise ValueError(
            f"expected apply envelope schema {APPLY_ENVELOPE_SCHEMA!r}"
        )
    if value.get("server_id") != expected_server_id:
        raise ValueError(
            "apply envelope belongs to another server"
        )
    envelope_hash = _validated_sha256(
        value.get("envelope_hash"),
        field="envelope_hash",
    )
    hash_body = dict(value)
    hash_body.pop("envelope_hash")
    if envelope_hash != payload_sha256(hash_body):
        raise ValueError("apply envelope_hash mismatch")

    articles = value.get("articles")
    if not isinstance(articles, list) or not articles:
        raise ValueError("apply envelope articles must be a non-empty array")
    article_ids: list[int] = []
    for item in articles:
        if not isinstance(item, Mapping):
            raise ValueError("every apply envelope article must be an object")
        article_id = item.get("id")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise ValueError(
                "every apply envelope article must have an integer id"
            )
        article_ids.append(article_id)
    if len(article_ids) != len(set(article_ids)):
        raise ValueError("apply envelope contains duplicate article ids")

    raw_contexts = value.get("generation_contexts")
    if not isinstance(raw_contexts, list):
        raise ValueError("generation_contexts must be an array")
    context_hashes: dict[int, str] = {}
    for index, context in enumerate(raw_contexts):
        if not isinstance(context, Mapping) or set(context) != {
            "article_id",
            "enrichment_context_hash",
        }:
            raise ValueError(
                f"generation context at index {index} has invalid fields"
            )
        article_id = context.get("article_id")
        if isinstance(article_id, bool) or not isinstance(article_id, int):
            raise ValueError(
                f"generation context at index {index} has invalid article_id"
            )
        if article_id in context_hashes:
            raise ValueError(
                f"generation_contexts contains duplicate article {article_id}"
            )
        context_hashes[article_id] = _validated_sha256(
            context.get("enrichment_context_hash"),
            field=f"article {article_id} enrichment_context_hash",
        )
    if set(context_hashes) != set(article_ids):
        raise ValueError(
            "generation_contexts coverage does not match envelope articles"
        )

    normalized_source_fingerprint = validate_law_source_fingerprint(
        value.get("source_fingerprint")
    )
    if (
        normalized_source_fingerprint["snapshot_version"]
        != SOURCE_CONTENT_FINGERPRINT_VERSION
    ):
        raise ValueError(
            "apply envelope source fingerprint must bind actual content v3"
        )
    normalized_source_catalog_binding = validate_law_source_catalog_binding(
        value.get("source_catalog_binding"),
        expected_server_id=expected_server_id,
    )
    if (
        normalized_source_catalog_binding["source_fingerprint"]
        != normalized_source_fingerprint
    ):
        raise ValueError(
            "apply envelope source/catalog binding differs from its source "
            "fingerprint"
        )
    normalized_context_contract = _validated_context_contract(
        value.get("context_contract"),
        field="apply envelope context_contract",
    )
    normalized_evidence = _validate_envelope_evidence(
        value.get("evidence")
    )
    selection_mode, requested_article_ids = (
        _cross_validate_envelope_evidence(
            normalized_evidence,
            article_ids=article_ids,
            articles=articles,
            source_fingerprint=normalized_source_fingerprint,
            source_catalog_binding=normalized_source_catalog_binding,
            context_contract=normalized_context_contract,
            generation_context_hashes=context_hashes,
        )
    )
    return {
        "schema": APPLY_ENVELOPE_SCHEMA,
        "server_id": expected_server_id,
        "source_fingerprint": normalized_source_fingerprint,
        "source_catalog_binding": normalized_source_catalog_binding,
        "context_contract": normalized_context_contract,
        "generation_context_hashes": context_hashes,
        "articles": [dict(item) for item in articles],
        "evidence": normalized_evidence,
        "selection_mode": selection_mode,
        "requested_article_ids": requested_article_ids,
        "review_source_projection_hash": (
            normalized_evidence["body"].get(
                "review_source_projection_hash"
            )
            if normalized_evidence["mode"] == "native_v3"
            else None
        ),
        "envelope_hash": envelope_hash,
    }
