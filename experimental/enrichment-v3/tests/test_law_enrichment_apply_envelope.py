"""Versioned fail-closed manual enrichment envelope contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest

from services.law_enrichment_artifact import (
    APPLY_ENVELOPE_SCHEMA,
    build_apply_envelope_evidence,
    build_law_enrichment_apply_envelope,
    enrichment_generation_source,
    payload_sha256,
    validate_law_enrichment_apply_envelope,
)
from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LAW_SOURCE_CATALOG_BINDING_SCHEMA,
    validate_law_source_catalog_binding,
)


def _item(article_id: int = 7) -> dict:
    return {
        "id": article_id,
        "article_hash": f"article-{article_id}",
        "title": "Проверенное название",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [
            f"проверенная формулировка {article_id} номер {index}"
            for index in range(12)
        ],
        "generation_input_hash": f"{article_id:064x}",
    }


def _fingerprint(value: str = "a" * 64) -> dict:
    return {
        "algorithm": "sha256",
        "snapshot_version": 3,
        "documents": 3,
        "value": value,
    }


def _context_contract() -> dict:
    return {
        "algorithm_version": ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
        "generation_policy_version": ENRICHMENT_GENERATION_POLICY_VERSION,
        "source_title_derivation_policy_version": (
            SOURCE_TITLE_DERIVATION_POLICY_VERSION
        ),
        "catalog_parser_contract_version": (
            LAW_CATALOG_PARSER_CONTRACT_VERSION
        ),
    }


def _source_catalog_binding() -> dict:
    return {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": LAW_CATALOG_PARSER_CONTRACT_VERSION,
        "server_id": "memphis",
        "source_fingerprint": _fingerprint(),
        "documents": 3,
        "catalog_rows": 10,
        "article_rows": 7,
        "tombstone_rows": 1,
        "source_projection_hash": "3" * 64,
        "catalog_projection_hash": "4" * 64,
        "value": "5" * 64,
    }


def _envelope() -> dict:
    items = [_item()]
    contexts = {"7": "e" * 64}
    evidence = build_apply_envelope_evidence(
        "native_v3",
        {
            "aggregate_schema": (
                "law-full-enrichment-aggregate-evidence-v3"
            ),
            "manifest_schema": "law-full-enrichment-export-v3",
            "manifest_file": "manifest.json",
            "manifest_file_sha256": "b" * 64,
            "manifest_hash": "c" * 64,
            "article_ids": [7],
            "source_fingerprint": _fingerprint(),
            "source_catalog_binding": _source_catalog_binding(),
            "context_contract": _context_contract(),
            "generation_context_hashes": contexts,
            "review_source_projection_hash": "9" * 64,
            "reviewed_plan_hash": payload_sha256(items),
            "selection_mode": "pending_enrichment",
            "requested_article_ids": None,
            "inputs": [{
                "batch_number": 1,
                "source_file": "source-batch-00001.json",
                "source_file_sha256": "d" * 64,
                "batch_hash": "1" * 64,
                "reviewed_file": "reviewed-batch-00001.json",
                "reviewed_file_sha256": "2" * 64,
                "articles": 1,
                "article_ids": [7],
            }],
        },
    )
    return build_law_enrichment_apply_envelope(
        server_id="memphis",
        source_fingerprint=_fingerprint(),
        source_catalog_binding=_source_catalog_binding(),
        context_contract=_context_contract(),
        items=items,
        generation_context_hashes={7: "e" * 64},
        evidence=evidence,
    )


def _rehash_outer(envelope: dict) -> None:
    body = dict(envelope)
    body.pop("envelope_hash")
    envelope["envelope_hash"] = payload_sha256(body)


def test_v3_envelope_round_trip_binds_source_catalog_context_and_evidence():
    envelope = _envelope()

    validated = validate_law_enrichment_apply_envelope(
        envelope,
        expected_server_id="memphis",
    )

    assert validated["schema"] == APPLY_ENVELOPE_SCHEMA
    assert validated["source_fingerprint"] == _fingerprint()
    assert validated["source_catalog_binding"] == _source_catalog_binding()
    assert validated["generation_context_hashes"] == {7: "e" * 64}
    assert validated["review_source_projection_hash"] == "9" * 64
    assert validated["evidence"]["mode"] == "native_v3"


@pytest.mark.parametrize("alias", [7.0, "7", True])
def test_envelope_builder_rejects_context_id_aliases(alias):
    envelope = _envelope()

    with pytest.raises(ValueError, match="invalid article id"):
        build_law_enrichment_apply_envelope(
            server_id="memphis",
            source_fingerprint=_fingerprint(),
            source_catalog_binding=_source_catalog_binding(),
            context_contract=_context_contract(),
            items=[_item()],
            generation_context_hashes={alias: "e" * 64},
            evidence=envelope["evidence"],
        )


@pytest.mark.parametrize(
    ("field", "alias"),
    [
        ("snapshot_version", 3.0),
        ("documents", True),
        ("documents", 3.0),
    ],
)
def test_source_binding_rejects_numeric_fingerprint_aliases(field, alias):
    binding = _source_catalog_binding()
    binding["source_fingerprint"][field] = alias

    with pytest.raises(ValueError, match="exact-content fingerprint"):
        validate_law_source_catalog_binding(binding)


@pytest.mark.parametrize(
    ("field", "alias"),
    [
        ("id", 7.0),
        ("law_document_id", "3"),
        ("source_order", True),
    ],
)
def test_generation_source_rejects_identity_aliases(field, alias):
    article = {
        "id": 7,
        "article_hash": "hash",
        "law_document_id": 3,
        "source_order": 0,
    }
    article[field] = alias

    with pytest.raises(ValueError, match="exact integers"):
        enrichment_generation_source(article, [])


def test_generation_policy_v2_invalidates_v1_envelope_fail_closed():
    assert ENRICHMENT_GENERATION_POLICY_VERSION == (
        "law-enrichment-generation-policy-2026-07-31-v2"
    )
    envelope = _envelope()
    envelope["context_contract"]["generation_policy_version"] = (
        "law-enrichment-generation-policy-2026-07-30-v1"
    )
    _rehash_outer(envelope)

    with pytest.raises(ValueError, match="context_contract"):
        validate_law_enrichment_apply_envelope(
            envelope,
            expected_server_id="memphis",
        )


def test_v2_envelope_refuses_missing_evidence_even_if_outer_hash_is_rebuilt():
    envelope = _envelope()
    envelope.pop("evidence")
    _rehash_outer(envelope)

    with pytest.raises(ValueError, match="missing=.*evidence"):
        validate_law_enrichment_apply_envelope(
            envelope,
            expected_server_id="memphis",
        )


def test_v2_envelope_refuses_tampered_nested_evidence_with_new_outer_hash():
    envelope = deepcopy(_envelope())
    envelope["evidence"]["body"]["manifest_hash"] = "0" * 64
    _rehash_outer(envelope)

    with pytest.raises(ValueError, match="evidence_hash mismatch"):
        validate_law_enrichment_apply_envelope(
            envelope,
            expected_server_id="memphis",
        )


def test_v2_envelope_refuses_semantically_tampered_rehashed_evidence():
    envelope = deepcopy(_envelope())
    evidence = envelope["evidence"]
    evidence["body"]["article_ids"] = [8]
    evidence["body"]["inputs"][0]["article_ids"] = [8]
    evidence["body"]["generation_context_hashes"] = {"8": "e" * 64}
    evidence_base = {
        "schema": evidence["schema"],
        "mode": evidence["mode"],
        "body": evidence["body"],
    }
    evidence["evidence_hash"] = payload_sha256(evidence_base)
    _rehash_outer(envelope)

    with pytest.raises(
        ValueError,
        match="evidence article coverage/order differs",
    ):
        validate_law_enrichment_apply_envelope(
            envelope,
            expected_server_id="memphis",
        )


def test_v2_envelope_refuses_same_id_payload_tamper_with_hashes_rebuilt():
    envelope = deepcopy(_envelope())
    envelope["articles"][0]["title"] = "Tampered reviewed title"
    evidence = envelope["evidence"]
    evidence_base = {
        "schema": evidence["schema"],
        "mode": evidence["mode"],
        "body": evidence["body"],
    }
    evidence["evidence_hash"] = payload_sha256(evidence_base)
    _rehash_outer(envelope)

    with pytest.raises(ValueError, match="reviewed plan hash differs"):
        validate_law_enrichment_apply_envelope(
            envelope,
            expected_server_id="memphis",
        )


def test_v2_envelope_refuses_missing_full_source_fingerprint_field():
    envelope = _envelope()
    envelope["source_fingerprint"].pop("value")
    _rehash_outer(envelope)

    with pytest.raises(
        ValueError,
        match="source_fingerprint fields do not match",
    ):
        validate_law_enrichment_apply_envelope(
            envelope,
            expected_server_id="memphis",
        )


def test_coherently_rehashed_native_v2_without_binding_is_rejected() -> None:
    envelope = deepcopy(_envelope())
    envelope["schema"] = "law-full-enrichment-apply-envelope-v2"
    envelope.pop("source_catalog_binding")
    evidence = envelope["evidence"]
    evidence["schema"] = "law-full-enrichment-apply-input-evidence-v2"
    evidence["mode"] = "native_v2"
    evidence["body"]["aggregate_schema"] = (
        "law-full-enrichment-aggregate-evidence-v2"
    )
    evidence["body"]["manifest_schema"] = "law-full-enrichment-export-v2"
    evidence["body"].pop("source_catalog_binding")
    evidence["evidence_hash"] = payload_sha256({
        "schema": evidence["schema"],
        "mode": evidence["mode"],
        "body": evidence["body"],
    })
    _rehash_outer(envelope)

    with pytest.raises(ValueError, match="source_catalog_binding|schema"):
        validate_law_enrichment_apply_envelope(
            envelope,
            expected_server_id="memphis",
        )
