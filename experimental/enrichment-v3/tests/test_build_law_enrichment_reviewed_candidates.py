"""Create-once current-state reviewed candidate builder contracts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from scripts import build_law_enrichment_reviewed_candidates as builder
from scripts import export_law_enrichment_batches as exporter
from scripts import validate_law_enrichment_batch as batch_validator
from services.law_catalog_service import derive_catalog_fields, source_article_hash
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
    normalize_synonym_term,
)
from tests.test_build_law_enrichment_apply_plan import (
    _CONTEXT_CONTRACT,
    _fixture,
    _write_json,
)
from tests.test_validate_law_enrichment_batch import _exact_v2_pair


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _base_candidates(export_dir: Path) -> list[dict]:
    manifest = _read(export_dir / "manifest.json")
    result = []
    for entry in manifest["batches"]:
        source_batch = _read(export_dir / entry["file"])
        for article in source_batch["articles"]:
            source = article["source"]
            result.append(
                {
                    "id": article["id"],
                    "article_hash": article["article_hash"],
                    "title": deepcopy(source["catalog_title"]),
                    "penalty_summary": deepcopy(
                        source["current_penalty_summary"]
                    ),
                    "penalty": deepcopy(source["current_penalty"]),
                    "synonyms": deepcopy(source["current_auto_synonyms"]),
                    "generation_input_hash": article[
                        "generation_input_hash"
                    ],
                }
            )
    return result


def _correction_manifest(
    export_dir: Path,
    *,
    operations: list[dict],
    result_candidates: list[dict],
) -> dict:
    manifest_path = export_dir / "manifest.json"
    manifest = _read(manifest_path)
    base = _base_candidates(export_dir)
    body = {
        "schema": builder.CORRECTION_SCHEMA,
        "server_id": "memphis",
        "source_manifest_hash": manifest["manifest_hash"],
        "source_manifest_file_sha256": hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest(),
        "base_reviewed_plan_hash": exporter._payload_hash(base),
        "result_reviewed_plan_hash": exporter._payload_hash(
            result_candidates
        ),
        "operations": operations,
    }
    return exporter._with_hash(body, "correction_manifest_hash")


def _supported_title_correction(
    export_dir: Path,
    reviewed_dir: Path,
    manifest: dict,
) -> tuple[list[dict], dict]:
    base = _base_candidates(export_dir)
    article_id = manifest["exported_article_ids"][0]
    source_batch = _read(export_dir / manifest["batches"][0]["file"])
    source_article = next(
        article
        for article in source_batch["articles"]
        if article["id"] == article_id
    )
    reviewed_source = _read(
        reviewed_dir / "reviewed-batch-00001.json"
    )
    supported_title = next(
        item["title"] for item in reviewed_source if item["id"] == article_id
    )
    result = deepcopy(base)
    next(item for item in result if item["id"] == article_id)[
        "title"
    ] = supported_title
    operation = {
        "article_id": article_id,
        "article_hash": source_article["article_hash"],
        "generation_input_hash": source_article["generation_input_hash"],
        "generation_context_hash": source_article[
            "generation_context_hash"
        ],
        "path": "/title",
        "old_value": None,
        "new_value": supported_title,
    }
    return result, operation


def _rehash_current_source(
    export_dir: Path,
    *,
    article_id: int,
    field: str,
    value,
) -> None:
    manifest_path = export_dir / "manifest.json"
    manifest = _read(manifest_path)
    for entry in manifest["batches"]:
        batch_path = export_dir / entry["file"]
        batch = _read(batch_path)
        target = next(
            (
                article
                for article in batch["articles"]
                if int(article["id"]) == article_id
            ),
            None,
        )
        if target is None:
            continue
        target["source"][field] = deepcopy(value)
        target["generation_input_hash"] = exporter._payload_hash(
            target["source"]
        )
        batch_body = dict(batch)
        batch_body.pop("batch_hash")
        batch = exporter._with_hash(batch_body, "batch_hash")
        _write_json(batch_path, batch)
        entry["batch_hash"] = batch["batch_hash"]
        break
    else:
        raise AssertionError(f"missing fixture article {article_id}")
    manifest.pop("manifest_hash")
    _write_json(
        manifest_path,
        exporter._with_hash(manifest, "manifest_hash"),
    )


def _write_single_batch_export(tmp_path: Path, source_batch: dict) -> Path:
    export_dir = tmp_path / "single-export"
    export_dir.mkdir()
    source_name = "source-batch-00001.json"
    _write_json(export_dir / source_name, source_batch)
    article_ids = [
        int(article["id"]) for article in source_batch["articles"]
    ]
    binding = source_batch["source_catalog_binding"]
    manifest_body = {
        "schema": exporter.EXPORT_SCHEMA,
        "server_id": source_batch["server_id"],
        "created_at": "2026-08-04T00:00:00+00:00",
        "source_fingerprint": source_batch["source_fingerprint"],
        "source_catalog_binding": binding,
        "context_contract": source_batch["context_contract"],
        "review_source_projection_hash": source_batch[
            "review_source_projection"
        ]["projection_hash"],
        "generation_snapshot": {
            "algorithm": "sha256",
            "source_catalog_binding": binding,
            "required_target_ids": sorted(article_ids),
            "value": "e" * 64,
        },
        "batch_size": len(article_ids),
        "pending_articles": len(article_ids),
        "selection_mode": "pending_enrichment",
        "requested_article_ids": None,
        "exported_article_ids": article_ids,
        "instructions": {
            "review": "review",
            "reviewed_artifact_fields": list(exporter.REVIEWED_FIELDS),
            "apply_command_template": "apply",
            "resume_command": "resume",
        },
        "batches": [
            {
                "file": source_name,
                "batch_hash": source_batch["batch_hash"],
                "articles": len(article_ids),
                "article_ids": article_ids,
            }
        ],
    }
    _write_json(
        export_dir / "manifest.json",
        exporter._with_hash(manifest_body, "manifest_hash"),
    )
    return export_dir


def _tombstone_source_batch(*, current_title: str | None) -> dict:
    full_text = "Статья 9\nУпразднена."
    article_hash = source_article_hash(full_text)
    row = {
        "id": 9,
        "law_document_id": 90,
        "source_order": 1,
        "article_ref": "Статья 9",
        "kind": "article",
        "code": "9",
        "title": current_title,
        "doc_title": "Уголовный кодекс",
        "full_text": full_text,
        "article_hash": article_hash,
        "penalty_summary": None,
        "penalty_json": None,
    }
    projection = build_review_source_projection(
        [row],
        [9],
        catalog_fields_resolver=derive_catalog_fields,
        article_hash_resolver=source_article_hash,
        is_tombstone=is_explicitly_empty_or_repealed_article,
        canonicalize_scope=canonical_synonym_term,
    )
    source = {
        "id": 9,
        "article_hash": article_hash,
        "law_document_id": 90,
        "source_order": 1,
        "code": "9",
        "article_ref": "Статья 9",
        "catalog_title": current_title,
        "document": "Уголовный кодекс",
        "text": full_text,
        "current_penalty_summary": None,
        "current_penalty": None,
        "current_auto_synonyms": [],
    }
    source_fingerprint = {
        "algorithm": "sha256",
        "snapshot_version": 3,
        "documents": 1,
        "value": "f" * 64,
    }
    binding = {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": LAW_CATALOG_PARSER_CONTRACT_VERSION,
        "server_id": "memphis",
        "source_fingerprint": source_fingerprint,
        "documents": 1,
        "catalog_rows": 1,
        "article_rows": 1,
        "tombstone_rows": 1,
        "source_projection_hash": "a" * 64,
        "catalog_projection_hash": "b" * 64,
        "value": "c" * 64,
    }
    body = {
        "schema": exporter.BATCH_SCHEMA,
        "server_id": "memphis",
        "source_fingerprint": source_fingerprint,
        "source_catalog_binding": binding,
        "context_contract": _CONTEXT_CONTRACT,
        "batch_number": 1,
        "review_source_projection": projection,
        "articles": [
            {
                "id": 9,
                "article_hash": article_hash,
                "generation_input_hash": exporter._payload_hash(source),
                "generation_context_hash": "9" * 64,
                "source": source,
                "review": {
                    "status": "pending_codex_review",
                    "required_output_fields": list(exporter.REVIEWED_FIELDS),
                },
            }
        ],
    }
    return exporter._with_hash(body, "batch_hash")


def _replace_source_and_current_fields(
    export_dir: Path,
    *,
    article_id: int,
    full_text: str,
    penalty_summary,
    penalty,
) -> None:
    manifest_path = export_dir / "manifest.json"
    manifest = _read(manifest_path)
    batches = [
        _read(export_dir / entry["file"])
        for entry in manifest["batches"]
    ]
    target = next(
        article
        for batch in batches
        for article in batch["articles"]
        if int(article["id"]) == article_id
    )
    article_hash = source_article_hash(full_text)
    target["article_hash"] = article_hash
    target["source"]["article_hash"] = article_hash
    target["source"]["text"] = full_text
    target["source"]["current_penalty_summary"] = penalty_summary
    target["source"]["current_penalty"] = deepcopy(penalty)
    target["generation_input_hash"] = exporter._payload_hash(
        target["source"]
    )

    catalog_rows = [
        {
            "id": article["id"],
            "law_document_id": article["source"]["law_document_id"],
            "source_order": article["source"]["source_order"],
            "article_ref": article["source"]["article_ref"],
            "kind": "article",
            "code": article["source"]["code"],
            "title": None,
            "doc_title": article["source"]["document"],
            "full_text": article["source"]["text"],
            "article_hash": article["article_hash"],
        }
        for batch in batches
        for article in batch["articles"]
    ]
    full_projection = build_review_source_projection(
        catalog_rows,
        manifest["exported_article_ids"],
        catalog_fields_resolver=derive_catalog_fields,
        article_hash_resolver=source_article_hash,
        is_tombstone=is_explicitly_empty_or_repealed_article,
        canonicalize_scope=canonical_synonym_term,
    )
    batch_projections = []
    for entry, batch in zip(manifest["batches"], batches, strict=True):
        projection = select_review_source_projection(
            full_projection,
            entry["article_ids"],
        )
        batch["review_source_projection"] = projection
        batch_body = dict(batch)
        batch_body.pop("batch_hash")
        batch = exporter._with_hash(batch_body, "batch_hash")
        _write_json(export_dir / entry["file"], batch)
        entry["batch_hash"] = batch["batch_hash"]
        batch_projections.append(projection)
    manifest["review_source_projection_hash"] = (
        combine_review_source_projections(
            batch_projections,
            catalog_fields_resolver=derive_catalog_fields,
            article_hash_resolver=source_article_hash,
            is_tombstone=is_explicitly_empty_or_repealed_article,
            canonicalize_scope=canonical_synonym_term,
        )["projection_hash"]
    )
    manifest.pop("manifest_hash")
    _write_json(
        manifest_path,
        exporter._with_hash(manifest, "manifest_hash"),
    )


def test_copies_exact_current_fields_and_emits_validation_report(tmp_path):
    export_dir, _reviewed_dir, manifest = _fixture(tmp_path)
    source_bytes_before = {
        path.name: path.read_bytes()
        for path in export_dir.iterdir()
        if path.is_file()
    }
    output_dir = tmp_path / "current-reviewed"

    report = builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
    )

    assert report["ok"] is True
    assert report["schema"] == builder.REPORT_SCHEMA
    assert report["article_ids"] == manifest["exported_article_ids"]
    assert report["corrections"] is None
    assert _read(output_dir / builder.REPORT_FILE) == report
    report_body = dict(report)
    report_hash = report_body.pop("report_hash")
    assert report_hash == exporter._payload_hash(report_body)
    assert source_bytes_before == {
        path.name: path.read_bytes()
        for path in export_dir.iterdir()
        if path.is_file()
    }

    expected = _base_candidates(export_dir)
    actual = []
    for entry in manifest["batches"]:
        source_batch = _read(export_dir / entry["file"])
        reviewed_name = entry["file"].replace(
            "source-batch-", "reviewed-batch-"
        )
        reviewed = _read(output_dir / reviewed_name)
        actual.extend(reviewed)
        assert batch_validator.validate_batch(
            "memphis", source_batch, reviewed
        )["ok"] is True
    assert actual == expected


def test_copies_nonempty_current_title_and_auto_synonyms_byte_exact(tmp_path):
    export_dir, reviewed_dir, manifest = _fixture(tmp_path)
    reviewed_by_id = {
        int(item["id"]): item
        for entry in manifest["batches"]
        for item in _read(
            reviewed_dir
            / entry["file"].replace("source-batch-", "reviewed-batch-")
        )
    }
    for article_id in manifest["exported_article_ids"]:
        current = reviewed_by_id[article_id]
        _rehash_current_source(
            export_dir,
            article_id=article_id,
            field="catalog_title",
            value=current["title"],
        )
        _rehash_current_source(
            export_dir,
            article_id=article_id,
            field="current_auto_synonyms",
            value=[
                normalize_synonym_term(value)
                for value in current["synonyms"]
            ],
        )
    output_dir = tmp_path / "current-reviewed"

    builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
    )

    actual = [
        item
        for entry in manifest["batches"]
        for item in _read(
            output_dir
            / entry["file"].replace("source-batch-", "reviewed-batch-")
        )
    ]
    assert actual == _base_candidates(export_dir)
    assert all(item["title"] is not None for item in actual)
    assert all(item["synonyms"] for item in actual)


def test_copies_source_backed_current_penalty_object_byte_exact(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    penalty_summary = "штраф от $40,000 до $100,000"
    penalty = {"fine": "$40,000–$100,000"}
    _replace_source_and_current_fields(
        export_dir,
        article_id=20,
        full_text=(
            "Статья 20. Наказание: штраф от $40,000 "
            "до $100,000."
        ),
        penalty_summary=penalty_summary,
        penalty=penalty,
    )
    output_dir = tmp_path / "current-reviewed"

    builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
    )

    item = next(
        item
        for item in _read(output_dir / "reviewed-batch-00001.json")
        if item["id"] == 20
    )
    assert item["penalty_summary"] == penalty_summary
    assert item["penalty"] == penalty


def test_correction_manifest_can_replace_all_reviewed_value_families(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    _replace_source_and_current_fields(
        export_dir,
        article_id=20,
        full_text=(
            "Статья 20. Наказание: штраф от $40,000 "
            "до $100,000."
        ),
        penalty_summary="наказание: штраф от $40,000 до $100,000",
        penalty={"fine": "$40,000–$100,000"},
    )
    base = _base_candidates(export_dir)
    result = deepcopy(base)
    result_item = next(item for item in result if item["id"] == 20)
    result_item["title"] = "Штрафное наказание"
    result_item["penalty_summary"] = "штраф от $40,000 до $100,000"
    result_item["penalty"]["fine"] = "от $40,000 до $100,000"
    result_item["synonyms"] = ["штрафное наказание"]
    manifest = _read(export_dir / "manifest.json")
    source_batch = _read(export_dir / manifest["batches"][0]["file"])
    source_article = next(
        article for article in source_batch["articles"] if article["id"] == 20
    )
    identity = {
        "article_id": 20,
        "article_hash": source_article["article_hash"],
        "generation_input_hash": source_article["generation_input_hash"],
        "generation_context_hash": source_article[
            "generation_context_hash"
        ],
    }
    operations = [
        {
            **identity,
            "path": "/title",
            "old_value": None,
            "new_value": result_item["title"],
        },
        {
            **identity,
            "path": "/penalty_summary",
            "old_value": "наказание: штраф от $40,000 до $100,000",
            "new_value": result_item["penalty_summary"],
        },
        {
            **identity,
            "path": "/penalty/fine",
            "old_value": "$40,000–$100,000",
            "new_value": result_item["penalty"]["fine"],
        },
        {
            **identity,
            "path": "/synonyms",
            "old_value": [],
            "new_value": result_item["synonyms"],
        },
    ]
    correction = _correction_manifest(
        export_dir,
        operations=operations,
        result_candidates=result,
    )
    correction_path = tmp_path / "all-fields.json"
    _write_json(correction_path, correction)
    output_dir = tmp_path / "corrected-reviewed"

    report = builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
        correction_manifest_path=correction_path,
    )

    assert report["corrections"]["operations"] == 4
    actual = next(
        item
        for item in _read(output_dir / "reviewed-batch-00001.json")
        if item["id"] == 20
    )
    assert actual == result_item


def test_accepts_strict_hash_bound_empty_correction_manifest(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    base = _base_candidates(export_dir)
    correction = _correction_manifest(
        export_dir,
        operations=[],
        result_candidates=base,
    )
    correction_path = tmp_path / "corrections.json"
    _write_json(correction_path, correction)
    output_dir = tmp_path / "current-reviewed"

    report = builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
        correction_manifest_path=correction_path,
    )

    assert report["corrections"]["operations"] == 0
    assert report["corrections"]["article_ids"] == []
    assert (output_dir / builder.CORRECTION_COPY_FILE).read_bytes() == (
        correction_path.read_bytes()
    )


def test_applies_exact_hash_and_context_bound_old_to_new_correction(tmp_path):
    export_dir, reviewed_dir, manifest = _fixture(tmp_path)
    result, operation = _supported_title_correction(
        export_dir, reviewed_dir, manifest
    )
    article_id = operation["article_id"]
    correction = _correction_manifest(
        export_dir,
        operations=[operation],
        result_candidates=result,
    )
    correction_path = tmp_path / "corrections.json"
    _write_json(correction_path, correction)

    report = builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=tmp_path / "reviewed-current",
        correction_manifest_path=correction_path,
    )

    assert report["corrections"]["article_ids"] == [article_id]
    assert report["corrections"]["operations"] == 1
    assert report["reviewed_plan_hash"] == exporter._payload_hash(result)


def test_preserves_exact_group_as_one_fully_validated_candidate_unit(tmp_path):
    source_batch, reviewed = _exact_v2_pair()
    reviewed_by_id = {int(item["id"]): item for item in reviewed}
    for article in source_batch["articles"]:
        current = reviewed_by_id[int(article["id"])]
        article["source"]["catalog_title"] = current["title"]
        article["source"]["current_auto_synonyms"] = current["synonyms"]
        article["generation_input_hash"] = exporter._payload_hash(
            article["source"]
        )
    body = dict(source_batch)
    body.pop("batch_hash")
    source_batch = exporter._with_hash(body, "batch_hash")
    export_dir = _write_single_batch_export(tmp_path, source_batch)
    output_dir = tmp_path / "exact-reviewed"

    report = builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
    )

    assert report["article_ids"] == [7, 8]
    assert _read(output_dir / "reviewed-batch-00001.json") == (
        _base_candidates(export_dir)
    )


def test_one_sided_exact_group_correction_is_rejected_before_write(tmp_path):
    source_batch, reviewed = _exact_v2_pair()
    reviewed_by_id = {int(item["id"]): item for item in reviewed}
    for article in source_batch["articles"]:
        current = reviewed_by_id[int(article["id"])]
        article["source"]["catalog_title"] = current["title"]
        article["source"]["current_auto_synonyms"] = current["synonyms"]
        article["generation_input_hash"] = exporter._payload_hash(
            article["source"]
        )
    body = dict(source_batch)
    body.pop("batch_hash")
    source_batch = exporter._with_hash(body, "batch_hash")
    export_dir = _write_single_batch_export(tmp_path, source_batch)
    base = _base_candidates(export_dir)
    first, second = base
    result = deepcopy(base)
    result[0]["title"] = second["title"]
    source_article = source_batch["articles"][0]
    operation = {
        "article_id": first["id"],
        "article_hash": first["article_hash"],
        "generation_input_hash": first["generation_input_hash"],
        "generation_context_hash": source_article[
            "generation_context_hash"
        ],
        "path": "/title",
        "old_value": first["title"],
        "new_value": second["title"],
    }
    correction = _correction_manifest(
        export_dir,
        operations=[operation],
        result_candidates=result,
    )
    correction_path = tmp_path / "one-sided.json"
    _write_json(correction_path, correction)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="exact-body reviewed response failed",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
            correction_manifest_path=correction_path,
        )

    assert not output_dir.exists()


def test_clean_source_owned_tombstone_is_copied_as_empty_candidate(tmp_path):
    export_dir = _write_single_batch_export(
        tmp_path,
        _tombstone_source_batch(current_title=None),
    )
    output_dir = tmp_path / "tombstone-reviewed"

    report = builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
    )

    assert report["article_ids"] == [9]
    item = _read(output_dir / "reviewed-batch-00001.json")[0]
    assert item["title"] is None
    assert item["penalty_summary"] is None
    assert item["penalty"] is None
    assert item["synonyms"] == []


def test_dirty_persisted_tombstone_is_rejected_before_write(tmp_path):
    export_dir = _write_single_batch_export(
        tmp_path,
        _tombstone_source_batch(current_title="Упраздненная норма"),
    )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="article 9 is a tombstone.*null title/penalty",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: {**value, "server_id": "foreign"}, "another server"),
        (
            lambda value: {
                **value,
                "source_manifest_file_sha256": "0" * 64,
            },
            "file hash is stale or foreign",
        ),
        (
            lambda value: {
                **value,
                "correction_manifest_hash": "0" * 64,
            },
            "correction_manifest_hash mismatch",
        ),
    ],
)
def test_rejects_foreign_or_hash_drifted_correction_without_output(
    tmp_path,
    mutate,
    match,
):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    base = _base_candidates(export_dir)
    correction = _correction_manifest(
        export_dir,
        operations=[],
        result_candidates=base,
    )
    correction = mutate(correction)
    correction_path = tmp_path / "corrections.json"
    _write_json(correction_path, correction)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(builder.CandidateBuildError, match=match):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
            correction_manifest_path=correction_path,
        )

    assert not output_dir.exists()


def test_rejects_stale_old_value_before_writing(tmp_path):
    export_dir, _reviewed_dir, manifest = _fixture(tmp_path)
    base = _base_candidates(export_dir)
    article_id = manifest["exported_article_ids"][0]
    source_batch = _read(export_dir / manifest["batches"][0]["file"])
    source_article = next(
        item for item in source_batch["articles"] if item["id"] == article_id
    )
    operation = {
        "article_id": article_id,
        "article_hash": source_article["article_hash"],
        "generation_input_hash": source_article["generation_input_hash"],
        "generation_context_hash": source_article[
            "generation_context_hash"
        ],
        "path": "/title",
        "old_value": "stale title",
        "new_value": "new title",
    }
    correction = _correction_manifest(
        export_dir,
        operations=[operation],
        result_candidates=base,
    )
    correction_path = tmp_path / "corrections.json"
    _write_json(correction_path, correction)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(builder.CandidateBuildError, match="old_value is stale"):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
            correction_manifest_path=correction_path,
        )

    assert not output_dir.exists()


@pytest.mark.parametrize(
    ("operation_mutation", "manifest_mutation", "match"),
    [
        (
            lambda operation: {
                **operation,
                "generation_context_hash": "0" * 64,
            },
            lambda manifest: manifest,
            "generation_context_hash is stale",
        ),
        (
            lambda operation: {**operation, "article_id": 999999},
            lambda manifest: manifest,
            "targets missing article",
        ),
        (
            lambda operation: operation,
            lambda manifest: {
                **manifest,
                "result_reviewed_plan_hash": "0" * 64,
            },
            "result reviewed plan hash mismatch",
        ),
    ],
)
def test_rejects_stale_missing_or_result_drifted_operations(
    tmp_path,
    operation_mutation,
    manifest_mutation,
    match,
):
    export_dir, reviewed_dir, manifest = _fixture(tmp_path)
    result, operation = _supported_title_correction(
        export_dir, reviewed_dir, manifest
    )
    correction = _correction_manifest(
        export_dir,
        operations=[operation_mutation(operation)],
        result_candidates=result,
    )
    correction = manifest_mutation(correction)
    correction.pop("correction_manifest_hash")
    correction = exporter._with_hash(
        correction, "correction_manifest_hash"
    )
    correction_path = tmp_path / "corrections.json"
    _write_json(correction_path, correction)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(builder.CandidateBuildError, match=match):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
            correction_manifest_path=correction_path,
        )

    assert not output_dir.exists()


def test_rejects_duplicate_or_ancestor_correction_paths(tmp_path):
    export_dir, _reviewed_dir, manifest = _fixture(tmp_path)
    base = _base_candidates(export_dir)
    article_id = manifest["exported_article_ids"][0]
    source_batch = _read(export_dir / manifest["batches"][0]["file"])
    source_article = next(
        item for item in source_batch["articles"] if item["id"] == article_id
    )
    identity = {
        "article_id": article_id,
        "article_hash": source_article["article_hash"],
        "generation_input_hash": source_article["generation_input_hash"],
        "generation_context_hash": source_article[
            "generation_context_hash"
        ],
    }
    operations = [
        {
            **identity,
            "path": "/synonyms",
            "old_value": [],
            "new_value": ["проверенная норма"],
        },
        {
            **identity,
            "path": "/synonyms/0",
            "old_value": None,
            "new_value": "другая норма",
        },
    ]
    result = deepcopy(base)
    next(item for item in result if item["id"] == article_id)[
        "synonyms"
    ] = ["проверенная норма"]
    correction = _correction_manifest(
        export_dir,
        operations=operations,
        result_candidates=result,
    )
    correction_path = tmp_path / "corrections.json"
    _write_json(correction_path, correction)

    with pytest.raises(builder.CandidateBuildError, match="paths overlap"):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=tmp_path / "must-not-exist",
            correction_manifest_path=correction_path,
        )


def test_old_value_assertion_is_json_type_exact(tmp_path):
    export_dir, reviewed_dir, _manifest = _fixture(tmp_path)
    _rehash_current_source(
        export_dir,
        article_id=20,
        field="catalog_title",
        value=1,
    )
    base = _base_candidates(export_dir)
    supported_title = next(
        item["title"]
        for item in _read(reviewed_dir / "reviewed-batch-00001.json")
        if item["id"] == 20
    )
    result = deepcopy(base)
    next(item for item in result if item["id"] == 20)[
        "title"
    ] = supported_title
    current_manifest = _read(export_dir / "manifest.json")
    source_batch = _read(
        export_dir / current_manifest["batches"][0]["file"]
    )
    source_article = next(
        item for item in source_batch["articles"] if item["id"] == 20
    )
    operation = {
        "article_id": 20,
        "article_hash": source_article["article_hash"],
        "generation_input_hash": source_article["generation_input_hash"],
        "generation_context_hash": source_article[
            "generation_context_hash"
        ],
        "path": "/title",
        # JSON boolean must never assert an integer old value.
        "old_value": True,
        "new_value": supported_title,
    }
    correction = _correction_manifest(
        export_dir,
        operations=[operation],
        result_candidates=result,
    )
    correction_path = tmp_path / "typed-old.json"
    _write_json(correction_path, correction)

    with pytest.raises(builder.CandidateBuildError, match="old_value is stale"):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=tmp_path / "must-not-exist",
            correction_manifest_path=correction_path,
        )


def test_huge_synonym_index_is_machine_readable_candidate_error(
    tmp_path,
    capsys,
):
    export_dir, _reviewed_dir, manifest = _fixture(tmp_path)
    base = _base_candidates(export_dir)
    source_batch = _read(export_dir / manifest["batches"][0]["file"])
    source_article = source_batch["articles"][0]
    operation = {
        "article_id": source_article["id"],
        "article_hash": source_article["article_hash"],
        "generation_input_hash": source_article["generation_input_hash"],
        "generation_context_hash": source_article[
            "generation_context_hash"
        ],
        "path": "/synonyms/" + ("9" * 5000),
        "old_value": None,
        "new_value": "проверенная норма",
    }
    correction = _correction_manifest(
        export_dir,
        operations=[operation],
        result_candidates=base,
    )
    correction_path = tmp_path / "huge-index.json"
    _write_json(correction_path, correction)

    exit_code = builder.main(
        [
            "memphis",
            "--export-dir",
            str(export_dir),
            "--output-dir",
            str(tmp_path / "must-not-exist"),
            "--corrections",
            str(correction_path),
        ]
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "path index is invalid" in payload["error"]


def test_shared_shape_gate_rejects_sixteen_current_synonyms_without_output(
    tmp_path,
):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    _rehash_current_source(
        export_dir,
        article_id=20,
        field="current_auto_synonyms",
        value=[f"distinct current synonym {index}" for index in range(16)],
    )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="more than 15 distinct auto synonyms",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


@pytest.mark.parametrize(
    ("synonyms", "match"),
    [
        (["x" * 81], "synonym must be non-empty and within shared bounds"),
        (
            ["что будет за убийство", "убийство"],
            "duplicate runtime-canonical synonym groups",
        ),
    ],
)
def test_shared_length_and_canonical_shape_fail_before_writing(
    tmp_path,
    synonyms,
    match,
):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    _rehash_current_source(
        export_dir,
        article_id=20,
        field="current_auto_synonyms",
        value=synonyms,
    )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(builder.CandidateBuildError, match=match):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_combined_gate_rejects_cross_batch_canonical_owner_collision(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    for article_id in (20, 30):
        _rehash_current_source(
            export_dir,
            article_id=article_id,
            field="current_auto_synonyms",
            value=["проверенная норма"],
        )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="reuse canonical synonym groups across articles",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_missing_current_source_field_is_a_hard_blocker(tmp_path):
    export_dir, _reviewed_dir, manifest = _fixture(tmp_path)
    batch_path = export_dir / manifest["batches"][0]["file"]
    batch = _read(batch_path)
    batch["articles"][0]["source"].pop("current_penalty")
    batch["articles"][0]["generation_input_hash"] = exporter._payload_hash(
        batch["articles"][0]["source"]
    )
    body = dict(batch)
    body.pop("batch_hash")
    batch = exporter._with_hash(body, "batch_hash")
    _write_json(batch_path, batch)
    manifest["batches"][0]["batch_hash"] = batch["batch_hash"]
    manifest.pop("manifest_hash")
    _write_json(
        export_dir / "manifest.json",
        exporter._with_hash(manifest, "manifest_hash"),
    )

    with pytest.raises(
        builder.CandidateBuildError,
        match="lacks current enrichment fields.*current_penalty",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=tmp_path / "must-not-exist",
        )


def test_source_batch_hash_drift_is_rejected_without_output(tmp_path):
    export_dir, _reviewed_dir, manifest = _fixture(tmp_path)
    batch_path = export_dir / manifest["batches"][0]["file"]
    batch = _read(batch_path)
    batch["articles"][0]["source"]["catalog_title"] = "unbound drift"
    _write_json(batch_path, batch)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="batch_hash mismatch",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_generation_input_hash_drift_is_rejected_after_outer_rehash(tmp_path):
    export_dir, _reviewed_dir, manifest = _fixture(tmp_path)
    batch_path = export_dir / manifest["batches"][0]["file"]
    batch = _read(batch_path)
    batch["articles"][0]["source"]["catalog_title"] = "unbound drift"
    batch_body = dict(batch)
    batch_body.pop("batch_hash")
    batch = exporter._with_hash(batch_body, "batch_hash")
    _write_json(batch_path, batch)
    manifest["batches"][0]["batch_hash"] = batch["batch_hash"]
    manifest.pop("manifest_hash")
    _write_json(
        export_dir / "manifest.json",
        exporter._with_hash(manifest, "manifest_hash"),
    )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="generation_input_hash does not bind its exact nested source",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_unexpected_source_batch_file_is_rejected_without_output(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    (export_dir / "source-batch-99999.json").write_bytes(
        (export_dir / "source-batch-00001.json").read_bytes()
    )
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="source batch file coverage differs from manifest",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_export_change_after_validation_is_detected_before_publish(
    tmp_path,
    monkeypatch,
):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    real_build_plan = builder.build_plan

    def mutating_build_plan(*args, **kwargs):
        result = real_build_plan(*args, **kwargs)
        manifest_path = export_dir / "manifest.json"
        manifest = _read(manifest_path)
        manifest["created_at"] = "2026-08-04T01:02:03+00:00"
        manifest.pop("manifest_hash")
        _write_json(
            manifest_path,
            exporter._with_hash(manifest, "manifest_hash"),
        )
        return result

    monkeypatch.setattr(builder, "build_plan", mutating_build_plan)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(
        builder.CandidateBuildError,
        match="export changed while candidates were being built",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_create_once_second_run_preserves_every_output_byte(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    output_dir = tmp_path / "current-reviewed"
    builder.build_and_write_current_candidates(
        "memphis",
        export_dir=export_dir,
        output_dir=output_dir,
    )
    before = {
        path.name: path.read_bytes()
        for path in output_dir.iterdir()
        if path.is_file()
    }

    with pytest.raises(builder.CandidateBuildError, match="refusing to overwrite"):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert before == {
        path.name: path.read_bytes()
        for path in output_dir.iterdir()
        if path.is_file()
    }


def test_same_immutable_inputs_produce_byte_identical_directories(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    first = tmp_path / "first-current"
    second = tmp_path / "second-current"

    builder.build_and_write_current_candidates(
        "memphis", export_dir=export_dir, output_dir=first
    )
    builder.build_and_write_current_candidates(
        "memphis", export_dir=export_dir, output_dir=second
    )

    first_files = {
        path.name: path.read_bytes()
        for path in first.iterdir()
        if path.is_file()
    }
    second_files = {
        path.name: path.read_bytes()
        for path in second.iterdir()
        if path.is_file()
    }
    assert first_files == second_files


@pytest.mark.parametrize("fail_on_call", [1, 2])
def test_exclusive_reservation_or_write_failure_leaves_no_partial_output(
    tmp_path,
    monkeypatch,
    fail_on_call,
):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    output_dir = tmp_path / "must-not-remain"
    real_fsync = builder.os.fsync
    calls = 0

    def failing_fsync(descriptor):
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise OSError("injected fsync failure")
        return real_fsync(descriptor)

    monkeypatch.setattr(builder.os, "fsync", failing_fsync)

    with pytest.raises(
        builder.CandidateBuildError,
        match="injected fsync failure",
    ):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_output_directory_cannot_alias_export(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)

    with pytest.raises(builder.CandidateBuildError, match="differ from export"):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=export_dir,
        )


def test_rejects_legacy_v1_export_without_output(tmp_path):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path, legacy_v1=True)
    output_dir = tmp_path / "must-not-exist"

    with pytest.raises(builder.CandidateBuildError, match="native-v3 export"):
        builder.build_and_write_current_candidates(
            "memphis",
            export_dir=export_dir,
            output_dir=output_dir,
        )

    assert not output_dir.exists()


def test_cli_prints_machine_readable_success_and_failure(tmp_path, capsys):
    export_dir, _reviewed_dir, _manifest = _fixture(tmp_path)
    output_dir = tmp_path / "current-reviewed"
    args = [
        "memphis",
        "--export-dir",
        str(export_dir),
        "--output-dir",
        str(output_dir),
    ]

    assert builder.main(args) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["ok"] is True
    assert builder.main(args) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["ok"] is False
    assert "refusing to overwrite" in failure["error"]
