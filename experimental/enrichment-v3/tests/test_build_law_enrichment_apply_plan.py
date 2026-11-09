"""Offline aggregate full-enrichment apply-plan safety contracts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import build_law_enrichment_apply_plan as planner
from scripts import export_law_enrichment_batches as exporter
from scripts import validate_law_enrichment_batch as batch_validator
from scripts import upgrade_law_enrichment_envelope_v1 as upgrader
from services.law_enrichment_artifact import (
    APPLY_ENVELOPE_SCHEMA,
    enrichment_generation_input_hash,
)
from services.law_enrichment_context import (
    ENRICHMENT_CONTEXT_ALGORITHM_VERSION,
    ENRICHMENT_GENERATION_POLICY_VERSION,
    SOURCE_TITLE_DERIVATION_POLICY_VERSION,
)
from services.law_catalog_service import (
    derive_catalog_fields,
    source_article_hash,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LAW_SOURCE_CATALOG_BINDING_SCHEMA,
)
from services.law_enrichment_review_contract import (
    build_review_source_projection,
    combine_review_source_projections,
    select_review_source_projection,
)
from services.law_synonym_service import (
    canonical_synonym_term,
    is_explicitly_empty_or_repealed_article,
)


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


def _source_catalog_binding(
    source_fingerprint: dict,
    *,
    catalog_rows: int,
) -> dict:
    return {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": LAW_CATALOG_PARSER_CONTRACT_VERSION,
        "server_id": "memphis",
        "source_fingerprint": dict(source_fingerprint),
        "documents": source_fingerprint["documents"],
        "catalog_rows": catalog_rows,
        "article_rows": catalog_rows,
        "tombstone_rows": 0,
        "source_projection_hash": "a" * 64,
        "catalog_projection_hash": "b" * 64,
        "value": "c" * 64,
    }


def _source_article(article_id: int, *, legacy_v1: bool = False) -> dict:
    source_text = f"Статья {article_id}. Проверенная норма."
    article_hash = source_article_hash(source_text)
    source = {
        "id": article_id,
        "article_hash": article_hash,
        "law_document_id": 90,
        "source_order": article_id,
        "code": str(article_id),
        "article_ref": f"Статья {article_id}",
        "catalog_title": None,
        "document": "Уголовный кодекс",
        "text": source_text,
        "current_penalty_summary": None,
        "current_penalty": None,
        "current_auto_synonyms": [],
    }
    generation_article = {
        "id": article_id,
        "article_hash": article_hash,
        "law_document_id": 90,
        "source_order": article_id,
        "code": str(article_id),
        "article_ref": source["article_ref"],
        "title": None,
        "doc_title": source["document"],
        "full_text": source["text"],
        "penalty_summary": None,
        "penalty_json": None,
    }
    result = {
        "id": article_id,
        "article_hash": article_hash,
        "generation_input_hash": enrichment_generation_input_hash(
            generation_article,
            (),
        ),
        "source": source,
        "review": {
            "status": "pending_codex_review",
            "required_output_fields": list(exporter.REVIEWED_FIELDS),
        },
    }
    if not legacy_v1:
        result["generation_context_hash"] = f"{article_id + 100:064x}"
    return result


def _reviewed_item(source_article: dict) -> dict:
    return {
        "id": source_article["id"],
        "article_hash": source_article["article_hash"],
        "title": f"Проверенная норма {source_article['id']}",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [
            f"Нарушение нормы {source_article['id']}",
            f"Юридическая норма {source_article['id']}",
        ],
        "generation_input_hash": source_article["generation_input_hash"],
    }


def _review_projection(source_articles: list[dict]) -> dict:
    catalog_rows = [
        {
            "id": article["id"],
            "law_document_id": article["source"]["law_document_id"],
            "source_order": article["source"]["source_order"],
            "article_ref": f"Статья {article['id']}",
            "kind": "article",
            "code": article["source"]["code"],
            "title": None,
            "doc_title": article["source"]["document"],
            "full_text": article["source"]["text"],
            "article_hash": article["article_hash"],
        }
        for article in source_articles
    ]
    return build_review_source_projection(
        catalog_rows,
        [article["id"] for article in source_articles],
        catalog_fields_resolver=derive_catalog_fields,
        article_hash_resolver=source_article_hash,
        is_tombstone=is_explicitly_empty_or_repealed_article,
        canonicalize_scope=canonical_synonym_term,
    )


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _fixture(
    tmp_path: Path,
    *,
    batch_ids: tuple[tuple[int, ...], ...] = ((20, 10), (30,)),
    legacy_v1: bool = False,
) -> tuple[Path, Path, dict]:
    export_dir = tmp_path / "export"
    reviewed_dir = tmp_path / "reviewed"
    export_dir.mkdir()
    reviewed_dir.mkdir()
    source_fingerprint = {
        "algorithm": "sha256",
        "documents": 1,
        "snapshot_version": 2 if legacy_v1 else 3,
        "value": "f" * 64,
    }
    all_source_articles = [
        _source_article(
            article_id,
            legacy_v1=legacy_v1,
        )
        for ids in batch_ids
        for article_id in ids
    ]
    full_review_projection = (
        None
        if legacy_v1
        else _review_projection(all_source_articles)
    )
    all_source_by_id = {
        int(article["id"]): article
        for article in all_source_articles
    }
    source_catalog_binding = (
        None
        if legacy_v1
        else _source_catalog_binding(
            source_fingerprint,
            catalog_rows=len(all_source_articles),
        )
    )
    batches = []
    review_projections = []
    manifest_batches = []
    for number, ids in enumerate(batch_ids, 1):
        source_articles = [
            all_source_by_id[article_id]
            for article_id in ids
        ]
        review_projection = (
            None
            if legacy_v1
            else select_review_source_projection(
                full_review_projection,
                ids,
            )
        )
        batch = exporter._with_hash(
            {
                "schema": (
                    exporter.BATCH_SCHEMA_V1
                    if legacy_v1
                    else exporter.BATCH_SCHEMA
                ),
                "server_id": "memphis",
                "source_fingerprint": source_fingerprint,
                **(
                    {}
                    if legacy_v1
                    else {
                        "context_contract": _CONTEXT_CONTRACT,
                        "source_catalog_binding": source_catalog_binding,
                        "review_source_projection": review_projection,
                    }
                ),
                "batch_number": number,
                "articles": source_articles,
            },
            "batch_hash",
        )
        batches.append(batch)
        if review_projection is not None:
            review_projections.append(review_projection)
        source_name = f"source-batch-{number:05d}.json"
        reviewed_name = f"reviewed-batch-{number:05d}.json"
        _write_json(export_dir / source_name, batch)
        _write_json(
            reviewed_dir / reviewed_name,
            [
                _reviewed_item(article)
                for article in reversed(batch["articles"])
            ],
        )
        manifest_batches.append(
            {
                "file": source_name,
                "batch_hash": batch["batch_hash"],
                "articles": len(ids),
                "article_ids": list(ids),
            }
        )
    manifest = exporter._with_hash(
        {
            "schema": (
                exporter.EXPORT_SCHEMA_V1
                if legacy_v1
                else exporter.EXPORT_SCHEMA
            ),
            "server_id": "memphis",
            "created_at": "2026-07-29T00:00:00+00:00",
            "source_fingerprint": source_fingerprint,
            "batch_size": 15,
            "pending_articles": sum(len(ids) for ids in batch_ids),
            "selection_mode": "pending_enrichment",
            "requested_article_ids": None,
            **(
                {}
                if legacy_v1
                else {
                    "context_contract": _CONTEXT_CONTRACT,
                    "source_catalog_binding": source_catalog_binding,
                    "review_source_projection_hash": (
                        combine_review_source_projections(
                            review_projections,
                            catalog_fields_resolver=derive_catalog_fields,
                            article_hash_resolver=source_article_hash,
                            is_tombstone=(
                                is_explicitly_empty_or_repealed_article
                            ),
                            canonicalize_scope=canonical_synonym_term,
                        )["projection_hash"]
                    ),
                    "generation_snapshot": {
                        "algorithm": "sha256",
                        "source_catalog_binding": (
                            source_catalog_binding
                        ),
                        "required_target_ids": sorted(
                            article_id
                            for ids in batch_ids
                            for article_id in ids
                        ),
                        "value": "e" * 64,
                    },
                    "exported_article_ids": [
                        article_id
                        for ids in batch_ids
                        for article_id in ids
                    ],
                }
            ),
            "instructions": {
                "review": "review",
                "reviewed_artifact_fields": list(exporter.REVIEWED_FIELDS),
                "apply_command_template": "apply",
                "resume_command": "resume",
            },
            "batches": manifest_batches,
        },
        "manifest_hash",
    )
    _write_json(export_dir / "manifest.json", manifest)
    return export_dir, reviewed_dir, manifest


def test_build_and_write_preserves_manifest_and_source_order(tmp_path):
    export_dir, reviewed_dir, manifest = _fixture(tmp_path)
    output_plan = tmp_path / "combined.json"
    output_evidence = tmp_path / "evidence.json"
    source_before = {
        path.name: path.read_bytes()
        for path in export_dir.iterdir()
    }
    reviewed_before = {
        path.name: path.read_bytes()
        for path in reviewed_dir.iterdir()
    }

    result = planner.build_and_write(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        output_plan=output_plan,
        output_evidence=output_evidence,
    )

    plan = json.loads(output_plan.read_text(encoding="utf-8"))
    evidence = json.loads(output_evidence.read_text(encoding="utf-8"))
    assert plan["schema"] == APPLY_ENVELOPE_SCHEMA
    assert [item["id"] for item in plan["articles"]] == [20, 10, 30]
    assert plan["articles"][0]["synonyms"] == [
        "нарушение нормы 20",
        "юридическая норма 20",
    ]
    assert result["ok"] is True
    assert result["articles"] == 3
    assert result["batches"] == 2
    assert result["plan_sha256"] == hashlib.sha256(
        output_plan.read_bytes()
    ).hexdigest()
    assert evidence["schema"] == planner.EVIDENCE_SCHEMA
    assert evidence["manifest_hash"] == manifest["manifest_hash"]
    assert evidence["article_ids"] == [20, 10, 30]
    assert evidence["inputs"][0]["article_ids"] == [20, 10]
    assert evidence["inputs"][0]["source_file_sha256"] == hashlib.sha256(
        (export_dir / "source-batch-00001.json").read_bytes()
    ).hexdigest()
    assert evidence["inputs"][0]["reviewed_file_sha256"] == hashlib.sha256(
        (reviewed_dir / "reviewed-batch-00001.json").read_bytes()
    ).hexdigest()
    assert evidence["plan_file_sha256"] == result["plan_sha256"]
    assert evidence["apply_envelope_hash"] == plan["envelope_hash"]
    evidence_body = dict(evidence)
    evidence_hash = evidence_body.pop("evidence_hash")
    assert evidence_hash == exporter._payload_hash(evidence_body)
    assert result["evidence_hash"] == evidence_hash
    assert result["evidence_file_sha256"] == hashlib.sha256(
        output_evidence.read_bytes()
    ).hexdigest()
    assert {
        path.name: path.read_bytes()
        for path in export_dir.iterdir()
    } == source_before
    assert {
        path.name: path.read_bytes()
        for path in reviewed_dir.iterdir()
    } == reviewed_before


def test_each_batch_uses_existing_validator_and_item_schema(
    tmp_path,
    monkeypatch,
):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    validated_batches: list[int] = []
    original_validate_batch = batch_validator.validate_batch
    original_validated_items = planner._validated_items
    validated_item_calls: list[int] = []

    def recording_validate_batch(server_id, source, reviewed):
        validated_batches.append(source["batch_number"])
        return original_validate_batch(server_id, source, reviewed)

    def recording_validated_items(items):
        validated_item_calls.append(len(items))
        return original_validated_items(items)

    monkeypatch.setattr(planner, "validate_batch", recording_validate_batch)
    monkeypatch.setattr(planner, "_validated_items", recording_validated_items)

    plan, _evidence = planner.build_plan(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
    )

    assert [item["id"] for item in plan] == [20, 10, 30]
    assert validated_batches == [1, 2]
    # Once per reviewed batch and once again for the combined writer artifact.
    assert validated_item_calls == [2, 1, 3]


def test_aggregate_repeats_shared_review_source_validation(
    tmp_path,
    monkeypatch,
):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    original = planner.validate_reviewed_source_projection
    aggregate_sizes: list[int] = []

    def recording_validate(projection, items, **kwargs):
        aggregate_sizes.append(len(projection["articles"]))
        return original(projection, items, **kwargs)

    monkeypatch.setattr(
        planner,
        "validate_reviewed_source_projection",
        recording_validate,
    )

    planner.build_plan(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
    )

    assert aggregate_sizes == [3]


def test_manifest_binds_combined_review_source_projection(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    source_path = export_dir / "source-batch-00001.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    projection = source["review_source_projection"]
    projection["articles"][0]["structural_ancestry"]["chapter"] = {
        "id": 999,
        "source_order": 0,
        "kind": "chapter",
        "code": "X",
        "article_ref": "Injected chapter",
        "source_title": "Injected chapter",
    }
    projection_body = dict(projection)
    projection_body.pop("projection_hash")
    projection["projection_hash"] = exporter._payload_hash(
        projection_body
    )
    source.pop("batch_hash")
    source = exporter._with_hash(source, "batch_hash")
    _write_json(source_path, source)

    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["batches"][0]["batch_hash"] = source["batch_hash"]
    # Preserve the original aggregate projection hash.  Rehashing one batch
    # must not let altered structural context become a valid aggregate.
    manifest.pop("manifest_hash")
    _write_json(
        manifest_path,
        exporter._with_hash(manifest, "manifest_hash"),
    )

    with pytest.raises(
        planner.AggregatePlanError,
        match=(
            "derived fields differ|"
            "combined review source projection hash differs"
        ),
    ):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


def test_accepts_early_export_v1_without_later_selection_fields(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("selection_mode")
    manifest.pop("requested_article_ids")
    manifest.pop("manifest_hash")
    _write_json(
        manifest_path,
        exporter._with_hash(manifest, "manifest_hash"),
    )

    plan, evidence = planner.build_plan(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
    )

    assert [item["id"] for item in plan] == [20, 10, 30]
    assert evidence["articles"] == 3


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda manifest: manifest.update(server_id="orlando"),
            "manifest belongs to another server",
        ),
        (
            lambda manifest: manifest.update(schema="future-schema"),
            "unsupported manifest schema",
        ),
        (
            lambda manifest: manifest.update(pending_articles=4),
            "pending_articles does not match",
        ),
        (
            lambda manifest: manifest["batches"][0].update(
                file="../source-batch-00001.json"
            ),
            "non-sequential filename",
        ),
        (
            lambda manifest: manifest["batches"][0].update(
                article_ids=[10, 20]
            ),
            "coverage/order mismatch",
        ),
        (
            lambda manifest: manifest["batches"][0].update(
                batch_hash="0" * 64
            ),
            "manifest/source batch_hash mismatch",
        ),
        (
            lambda manifest: manifest.pop("requested_article_ids"),
            "manifest fields do not match",
        ),
    ],
)
def test_refuses_rehashed_but_inconsistent_manifest(
    tmp_path,
    mutation,
    message,
):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    manifest.pop("manifest_hash")
    _write_json(
        manifest_path,
        exporter._with_hash(manifest, "manifest_hash"),
    )

    with pytest.raises(planner.AggregatePlanError, match=message):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


def test_refuses_manifest_hash_tampering(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = "tampered"
    _write_json(manifest_path, manifest)

    with pytest.raises(planner.AggregatePlanError, match="manifest_hash mismatch"):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


def test_refuses_source_batch_hash_tampering(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    source_path = export_dir / "source-batch-00001.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["articles"][0]["source"]["text"] = "tampered"
    _write_json(source_path, source)

    with pytest.raises(planner.AggregatePlanError, match="batch_hash mismatch"):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


@pytest.mark.parametrize("kind", ["missing", "unexpected"])
def test_refuses_non_exact_reviewed_file_coverage(tmp_path, kind):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    if kind == "missing":
        (reviewed_dir / "reviewed-batch-00002.json").unlink()
    else:
        _write_json(
            reviewed_dir / "reviewed-batch-00003.json",
            [],
        )

    with pytest.raises(
        planner.AggregatePlanError,
        match="reviewed batch file coverage differs",
    ):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


def test_refuses_non_exact_source_file_coverage(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    (export_dir / "source-batch-00002.json").replace(
        export_dir / "source-batch-00003.json"
    )

    with pytest.raises(
        planner.AggregatePlanError,
        match="source batch file coverage differs",
    ):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


def test_refuses_incomplete_reviewed_batch(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    reviewed_path = reviewed_dir / "reviewed-batch-00001.json"
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    reviewed.pop()
    _write_json(reviewed_path, reviewed)

    with pytest.raises(
        planner.AggregatePlanError,
        match="coverage is not exact",
    ):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


def test_source_unsupported_synonym_blocks_full_aggregate_outputs(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    reviewed_path = reviewed_dir / "reviewed-batch-00002.json"
    reviewed = json.loads(reviewed_path.read_text(encoding="utf-8"))
    reviewed[0]["synonyms"][0] = (
        "автоматическое аннулирование лицензии полицией за опоздание"
    )
    _write_json(reviewed_path, reviewed)
    output_plan = tmp_path / "combined.json"
    output_evidence = tmp_path / "evidence.json"

    with pytest.raises(
        planner.AggregatePlanError,
        match=(
            r"batch 2 validation failed.*synonym evidence: "
            r".*source-unsupported semantic claim"
        ),
    ):
        planner.build_and_write(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
            output_plan=output_plan,
            output_evidence=output_evidence,
        )

    assert not output_plan.exists()
    assert not output_evidence.exists()


def test_refuses_article_id_repeated_across_source_batches(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    source_path = export_dir / "source-batch-00002.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["articles"][0] = _source_article(20)
    source["review_source_projection"] = _review_projection(
        source["articles"]
    )
    source.pop("batch_hash")
    source = exporter._with_hash(source, "batch_hash")
    _write_json(source_path, source)
    _write_json(
        reviewed_dir / "reviewed-batch-00002.json",
        [_reviewed_item(source["articles"][0])],
    )
    manifest_path = export_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["batches"][1]["batch_hash"] = source["batch_hash"]
    manifest["batches"][1]["article_ids"] = [20]
    manifest.pop("manifest_hash")
    _write_json(
        manifest_path,
        exporter._with_hash(manifest, "manifest_hash"),
    )

    with pytest.raises(
        planner.AggregatePlanError,
        match="more than one source batch",
    ):
        planner.build_plan(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
        )


def test_exclusive_outputs_refuse_overwrite_without_partial_peer(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    output_plan = tmp_path / "combined.json"
    output_evidence = tmp_path / "evidence.json"
    output_evidence.write_bytes(b"keep-evidence")

    with pytest.raises(planner.AggregatePlanError, match="refusing to overwrite"):
        planner.build_and_write(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
            output_plan=output_plan,
            output_evidence=output_evidence,
        )

    assert not output_plan.exists()
    assert output_evidence.read_bytes() == b"keep-evidence"


def test_cli_prints_json_and_second_run_refuses_overwrite(tmp_path, capsys):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    output_plan = tmp_path / "combined.json"
    output_evidence = tmp_path / "evidence.json"
    args = [
        "memphis",
        "--export-dir",
        str(export_dir),
        "--reviewed-dir",
        str(reviewed_dir),
        "--output-plan",
        str(output_plan),
        "--output-evidence",
        str(output_evidence),
    ]

    assert planner.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True
    plan_before = output_plan.read_bytes()
    evidence_before = output_evidence.read_bytes()

    assert planner.main(args) == 1
    second = json.loads(capsys.readouterr().out)
    assert second["ok"] is False
    assert "overwrite" in second["error"]
    assert output_plan.read_bytes() == plan_before
    assert output_evidence.read_bytes() == evidence_before


def test_builder_contains_no_database_network_or_model_call_path():
    source = Path(planner.__file__).read_text(encoding="utf-8")
    forbidden = (
        "db_manager",
        "asyncpg",
        "requests.",
        "httpx.",
        "urlopen(",
        "google.genai",
        "execute_vertex_request",
        "generate_content",
    )
    assert all(token not in source for token in forbidden)


def _write_historical_v1_aggregate(
    tmp_path: Path,
    export_dir: Path,
    reviewed_dir: Path,
    *,
    legacy_surface_yo: bool = False,
    literal_archived_shape: bool = False,
) -> tuple[Path, Path]:
    items, aggregate = planner.build_plan(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
    )
    legacy_items = json.loads(json.dumps(items, ensure_ascii=False))
    if legacy_surface_yo:
        legacy_items[0]["synonyms"][0] = legacy_items[0][
            "synonyms"
        ][0].replace("е", "ё", 1)
    plan_path = tmp_path / "reviewed-all.json"
    evidence_path = tmp_path / "reviewed-all-evidence.json"
    plan_path.write_bytes(planner._encoded_json(legacy_items))
    plan_sha256 = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    legacy_aggregate = (
        {
            "schema": aggregate["schema"],
            "server_id": aggregate["server_id"],
            "manifest_file": aggregate["manifest_file"],
            "manifest_file_sha256": aggregate[
                "manifest_file_sha256"
            ],
            "manifest_hash": aggregate["manifest_hash"],
            "source_fingerprint": aggregate["source_fingerprint"],
            "batches": aggregate["batches"],
            "articles": aggregate["articles"],
            "article_ids": aggregate["article_ids"],
            "inputs": aggregate["inputs"],
        }
        if literal_archived_shape
        else upgrader._legacy_aggregate_base(aggregate)
    )
    evidence_body = {
        **legacy_aggregate,
        "plan_file": plan_path.name,
        "plan_file_sha256": plan_sha256,
        "plan_payload_hash": exporter._payload_hash(legacy_items),
    }
    _write_json(
        evidence_path,
        {
            **evidence_body,
            "evidence_hash": exporter._payload_hash(evidence_body),
        },
    )
    return plan_path, evidence_path


def _legacy_live_state(article_ids):
    articles = []
    for article_id in article_ids:
        source_article = _source_article(
            article_id,
            legacy_v1=True,
        )
        source = source_article["source"]
        articles.append({
            "id": article_id,
            "article_hash": source_article["article_hash"],
            "law_document_id": source["law_document_id"],
            "source_order": source["source_order"],
            "article_ref": f"Статья {article_id}",
            "kind": "article",
            "code": source["code"],
            "title": source["catalog_title"],
            "title_provenance": None,
            "doc_title": source["document"],
            "full_text": source["text"],
            "penalty_summary": source["current_penalty_summary"],
            "penalty_json": source["current_penalty"],
            "enrich_hash": None,
            "enrichment_context_hash": None,
            "enrichment_output_hash": None,
        })
    live_source_fingerprint = _live_content_fingerprint()
    return {
        "snapshot": {
            "selection_mode": "explicit",
            "source_catalog_binding": _source_catalog_binding(
                live_source_fingerprint,
                catalog_rows=len(articles),
            ),
            "required_target_ids": sorted(article_ids),
            "stale_target_ids": sorted(article_ids),
            "value": "d" * 64,
        },
        "articles": articles,
        "catalog_rows": articles,
        "auto_rows": [],
        "context_hashes": {
            article_id: f"{article_id + 1000:064x}"
            for article_id in article_ids
        },
    }


def _live_content_fingerprint(value: str = "c" * 64):
    return {
        "algorithm": "sha256",
        "snapshot_version": 3,
        "documents": 1,
        "value": value,
    }


def test_unchanged_verified_v1_upgrade_builds_bound_v2_envelope(tmp_path):
    export_dir, reviewed_dir, manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    plan_path, evidence_path = _write_historical_v1_aggregate(
        tmp_path,
        export_dir,
        reviewed_dir,
    )

    items, proof = upgrader.verify_legacy_v1_artifact_set(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        legacy_plan=plan_path,
        legacy_evidence=evidence_path,
    )
    article_ids = [item["id"] for item in items]
    envelope = upgrader.build_verified_v1_upgrade_envelope(
        "memphis",
        reviewed_items=items,
        immutable_proof=proof,
        live_source_fingerprint=manifest["source_fingerprint"],
        live_content_source_fingerprint=_live_content_fingerprint(),
        live_generation_state=_legacy_live_state(article_ids),
        live_incomplete_article_ids=sorted(article_ids),
    )

    assert envelope["schema"] == APPLY_ENVELOPE_SCHEMA
    assert envelope["evidence"]["mode"] == "verified_v1_upgrade"
    assert envelope["source_fingerprint"] == _live_content_fingerprint()
    assert [item["id"] for item in envelope["articles"]] == article_ids
    assert {
        item["article_id"] for item in envelope["generation_contexts"]
    } == set(article_ids)
    assert envelope["evidence"]["body"]["upgrade_binding"][
        "authoritative_incomplete_article_ids"
    ] == sorted(article_ids)


def test_literal_archived_v1_evidence_shape_still_verifies(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    plan_path, evidence_path = _write_historical_v1_aggregate(
        tmp_path,
        export_dir,
        reviewed_dir,
        literal_archived_shape=True,
    )
    literal_evidence = json.loads(
        evidence_path.read_text(encoding="utf-8")
    )

    assert "reviewed_plan_hash" not in literal_evidence
    items, proof = upgrader.verify_legacy_v1_artifact_set(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        legacy_plan=plan_path,
        legacy_evidence=evidence_path,
    )

    assert items
    assert proof["article_ids"] == [item["id"] for item in items]


def test_verified_v1_upgrade_accepts_legacy_yo_surface_when_semantics_match(
    tmp_path,
):
    export_dir, reviewed_dir, _manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    plan_path, evidence_path = _write_historical_v1_aggregate(
        tmp_path,
        export_dir,
        reviewed_dir,
        legacy_surface_yo=True,
    )

    items, proof = upgrader.verify_legacy_v1_artifact_set(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        legacy_plan=plan_path,
        legacy_evidence=evidence_path,
    )

    assert items
    assert proof["legacy_plan"]["file_sha256"] == hashlib.sha256(
        plan_path.read_bytes()
    ).hexdigest()
    assert all(
        "ё" not in synonym
        for item in items
        for synonym in item["synonyms"]
    )


@pytest.mark.parametrize("kind", ["missing", "tampered"])
def test_v1_upgrade_refuses_missing_or_tampered_aggregate_evidence(
    tmp_path,
    kind,
):
    export_dir, reviewed_dir, _manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    plan_path, evidence_path = _write_historical_v1_aggregate(
        tmp_path,
        export_dir,
        reviewed_dir,
    )
    if kind == "missing":
        evidence_path.unlink()
    else:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["articles"] += 1
        _write_json(evidence_path, evidence)

    with pytest.raises(
        (upgrader.LegacyEnvelopeUpgradeError, planner.AggregatePlanError),
        match="evidence|cannot read",
    ):
        upgrader.verify_legacy_v1_artifact_set(
            "memphis",
            export_dir=export_dir,
            reviewed_dir=reviewed_dir,
            legacy_plan=plan_path,
            legacy_evidence=evidence_path,
        )


def test_v1_upgrade_refuses_live_full_source_fingerprint_drift(tmp_path):
    export_dir, reviewed_dir, manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    plan_path, evidence_path = _write_historical_v1_aggregate(
        tmp_path,
        export_dir,
        reviewed_dir,
    )
    items, proof = upgrader.verify_legacy_v1_artifact_set(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        legacy_plan=plan_path,
        legacy_evidence=evidence_path,
    )
    changed_source = dict(manifest["source_fingerprint"])
    changed_source["value"] = "0" * 64

    with pytest.raises(
        upgrader.LegacyEnvelopeUpgradeError,
        match="live complete source fingerprint differs",
    ):
        upgrader.build_verified_v1_upgrade_envelope(
            "memphis",
            reviewed_items=items,
            immutable_proof=proof,
            live_source_fingerprint=changed_source,
            live_content_source_fingerprint=_live_content_fingerprint(),
            live_generation_state=_legacy_live_state(
                [item["id"] for item in items]
            ),
        )


def test_v1_upgrade_refuses_live_pending_target_coverage_drift(tmp_path):
    export_dir, reviewed_dir, manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    plan_path, evidence_path = _write_historical_v1_aggregate(
        tmp_path,
        export_dir,
        reviewed_dir,
    )
    items, proof = upgrader.verify_legacy_v1_artifact_set(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        legacy_plan=plan_path,
        legacy_evidence=evidence_path,
    )
    article_ids = [item["id"] for item in items]
    state = _legacy_live_state(article_ids)

    with pytest.raises(
        upgrader.LegacyEnvelopeUpgradeError,
        match="authoritative pending-enrichment coverage differs",
    ):
        upgrader.build_verified_v1_upgrade_envelope(
            "memphis",
            reviewed_items=items,
            immutable_proof=proof,
            live_source_fingerprint=manifest["source_fingerprint"],
            live_content_source_fingerprint=_live_content_fingerprint(),
            live_generation_state=state,
            live_incomplete_article_ids=sorted(article_ids) + [999],
        )


def test_v1_upgrade_uses_authoritative_quality_pending_not_hash_stale(
    tmp_path,
):
    export_dir, reviewed_dir, manifest = _fixture(
        tmp_path,
        legacy_v1=True,
    )
    plan_path, evidence_path = _write_historical_v1_aggregate(
        tmp_path,
        export_dir,
        reviewed_dir,
    )
    items, proof = upgrader.verify_legacy_v1_artifact_set(
        "memphis",
        export_dir=export_dir,
        reviewed_dir=reviewed_dir,
        legacy_plan=plan_path,
        legacy_evidence=evidence_path,
    )
    article_ids = [item["id"] for item in items]
    state = _legacy_live_state(article_ids)
    # A quality-only failure is deliberately absent from the old hash/context
    # stale list.  The bridge must use the authoritative completion result.
    state["snapshot"]["stale_target_ids"] = []

    envelope = upgrader.build_verified_v1_upgrade_envelope(
        "memphis",
        reviewed_items=items,
        immutable_proof=proof,
        live_source_fingerprint=manifest["source_fingerprint"],
        live_content_source_fingerprint=_live_content_fingerprint(),
        live_generation_state=state,
        live_incomplete_article_ids=sorted(article_ids),
    )

    assert envelope["evidence"]["body"]["upgrade_binding"][
        "authoritative_incomplete_article_ids"
    ] == sorted(article_ids)


@pytest.mark.asyncio
async def test_v1_live_source_proof_verifies_stored_hash_and_emits_v3():
    content = "Exact archived law source"
    stored_hash = hashlib.md5(
        content.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()

    class Conn:
        async def fetch(self, *_args):
            return [{
                "id": 9,
                "server_id": "memphis",
                "source_thread_id": 90,
                "title": "Archived law",
                "content": content,
                "content_hash": stored_hash,
            }]

    legacy, actual = (
        await upgrader._load_verified_live_source_fingerprints(
            Conn(),
            "memphis",
        )
    )

    assert legacy["snapshot_version"] == 2
    assert actual["snapshot_version"] == 3
    assert legacy["value"] != actual["value"]


@pytest.mark.asyncio
async def test_v1_live_source_proof_rejects_stale_stored_hash():
    class Conn:
        async def fetch(self, *_args):
            return [{
                "id": 9,
                "server_id": "memphis",
                "source_thread_id": 90,
                "title": "Archived law",
                "content": "Changed exact content",
                "content_hash": "0" * 32,
            }]

    with pytest.raises(
        upgrader.LegacyEnvelopeUpgradeError,
        match="does not match exact content",
    ):
        await upgrader._load_verified_live_source_fingerprints(
            Conn(),
            "memphis",
        )


@pytest.mark.asyncio
async def test_v1_pending_capture_uses_authoritative_ids_before_closure(
    monkeypatch,
):
    seen = {}

    async def completion(*_args, **_kwargs):
        class Result:
            # Evaluator order is structural, not necessarily numeric.
            incomplete_article_ids = (20, 10)

        return Result()

    async def generation_state(
        _conn,
        _server_id,
        target_ids,
        *,
        lock_articles,
    ):
        assert lock_articles is False
        seen["target_ids"] = list(target_ids)
        return {"snapshot": {"required_target_ids": [10, 20, 30]}}

    monkeypatch.setattr(
        upgrader,
        "evaluate_law_enrichment_completion",
        completion,
    )
    monkeypatch.setattr(
        upgrader,
        "load_auto_replacement_generation_state_for_targets_in_transaction",
        generation_state,
    )

    state, incomplete = (
        await upgrader._load_live_generation_state_for_upgrade(
            object(),
            "memphis",
            article_ids=[10, 20, 30],
            selection_mode="pending_enrichment",
        )
    )

    assert incomplete == [10, 20]
    assert seen["target_ids"] == [10, 20]
    assert state["snapshot"]["required_target_ids"] == [10, 20, 30]
