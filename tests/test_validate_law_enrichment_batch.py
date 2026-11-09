"""Offline contract checks for reviewed Phase A enrichment batches."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import export_law_enrichment_batches as exporter
from scripts import validate_law_enrichment_batch as validator


def _source_article(article_id: int) -> dict:
    article_hash = f"article-hash-{article_id}"
    generation_input_hash = f"{article_id:064x}"
    return {
        "id": article_id,
        "article_hash": article_hash,
        "generation_input_hash": generation_input_hash,
        "source": {
            "id": article_id,
            "article_hash": article_hash,
            "law_document_id": 90,
            "source_order": article_id,
            "code": str(article_id),
            "catalog_title": None,
            "document": "Уголовный кодекс",
            "text": f"Статья {article_id}. Проверенная норма.",
            "current_penalty_summary": None,
            "current_penalty": None,
            "current_auto_synonyms": [],
        },
        "review": {
            "status": "pending_codex_review",
            "required_output_fields": list(exporter.REVIEWED_FIELDS),
        },
    }


def _source_batch() -> dict:
    body = {
        "schema": exporter.BATCH_SCHEMA,
        "server_id": "memphis",
        "source_fingerprint": {
            "algorithm": "sha256",
            "documents": 1,
            "value": "source",
        },
        "batch_number": 3,
        "articles": [_source_article(7), _source_article(8)],
    }
    return exporter._with_hash(body, "batch_hash")


def _reviewed_item(source: dict) -> dict:
    return {
        "id": source["id"],
        "article_hash": source["article_hash"],
        "title": "Проверенная норма",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [
            "узкая разговорная формулировка",
            "точная юридическая формулировка",
        ],
        "generation_input_hash": source["generation_input_hash"],
    }


def _reviewed(source_batch: dict) -> list[dict]:
    return [
        _reviewed_item(article)
        for article in reversed(source_batch["articles"])
    ]


def test_validate_batch_accepts_exact_full_coverage_in_any_order():
    source = _source_batch()

    result = validator.validate_batch(
        "memphis",
        source,
        _reviewed(source),
    )

    assert result == {
        "ok": True,
        "schema": exporter.BATCH_SCHEMA,
        "server_id": "memphis",
        "batch_number": 3,
        "batch_hash": source["batch_hash"],
        "articles": 2,
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda source, reviewed: source.update(
                schema="wrong-source-schema"
            ),
            "expected source schema",
        ),
        (
            lambda source, reviewed: source.update(server_id="orlando"),
            "belongs to another server",
        ),
        (
            lambda source, reviewed: source.update(batch_hash="tampered"),
            "batch_hash mismatch",
        ),
        (
            lambda source, reviewed: reviewed.pop(),
            "coverage is not exact",
        ),
        (
            lambda source, reviewed: reviewed.append(
                {
                    **deepcopy(reviewed[0]),
                    "id": 999,
                    "article_hash": "unexpected",
                }
            ),
            "coverage is not exact",
        ),
        (
            lambda source, reviewed: reviewed[0].update(
                article_hash="changed"
            ),
            "differs from source",
        ),
        (
            lambda source, reviewed: reviewed[0].update(
                generation_input_hash="f" * 64
            ),
            "differs from source",
        ),
        (
            lambda source, reviewed: reviewed[0].update(extra="not allowed"),
            "reviewed artifact schema is invalid",
        ),
    ],
)
def test_validate_batch_refuses_contract_mismatch(mutate, message):
    source = _source_batch()
    reviewed = _reviewed(source)
    mutate(source, reviewed)

    with pytest.raises(validator.OfflineBatchValidationError, match=message):
        validator.validate_batch("memphis", source, reviewed)


def test_validate_batch_refuses_duplicate_source_identity_even_with_valid_hash():
    source = _source_batch()
    source["articles"][1] = deepcopy(source["articles"][0])
    body = dict(source)
    body.pop("batch_hash")
    source["batch_hash"] = exporter._payload_hash(body)

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match="duplicate article id",
    ):
        validator.validate_batch("memphis", source, _reviewed(source))


def test_validate_files_is_read_only_and_cli_prints_json_only(tmp_path, capsys):
    source = _source_batch()
    reviewed = _reviewed(source)
    source_path = tmp_path / "source.json"
    reviewed_path = tmp_path / "reviewed.json"
    source_encoded = json.dumps(source, ensure_ascii=False)
    reviewed_encoded = json.dumps(reviewed, ensure_ascii=False)
    source_path.write_text(source_encoded, encoding="utf-8")
    reviewed_path.write_text(reviewed_encoded, encoding="utf-8")
    before_paths = sorted(path.name for path in tmp_path.iterdir())

    exit_code = validator.main(
        ["memphis", str(source_path), str(reviewed_path)]
    )

    stdout = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(stdout.out)["ok"] is True
    assert stdout.err == ""
    assert source_path.read_text(encoding="utf-8") == source_encoded
    assert reviewed_path.read_text(encoding="utf-8") == reviewed_encoded
    assert sorted(path.name for path in tmp_path.iterdir()) == before_paths


def test_cli_refusal_is_json_on_stdout(tmp_path, capsys):
    source = _source_batch()
    reviewed = _reviewed(source)[:-1]
    source_path = tmp_path / "source.json"
    reviewed_path = tmp_path / "reviewed.json"
    source_path.write_text(
        json.dumps(source, ensure_ascii=False),
        encoding="utf-8",
    )
    reviewed_path.write_text(
        json.dumps(reviewed, ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = validator.main(
        ["memphis", str(source_path), str(reviewed_path)]
    )

    output = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(output.out)["ok"] is False
    assert output.err == ""


def test_cli_help_documents_all_required_inputs(capsys):
    with pytest.raises(SystemExit) as raised:
        validator.main(["--help"])

    output = capsys.readouterr()
    assert raised.value.code == 0
    assert "server_id" in output.out
    assert "source_batch" in output.out
    assert "reviewed_json" in output.out
    assert output.err == ""


def test_validator_has_no_database_network_or_file_write_path():
    source = Path(validator.__file__).read_text(encoding="utf-8")

    assert "db_manager" not in source
    assert "requests." not in source
    assert "httpx." not in source
    assert "urlopen(" not in source
    assert ".write_text(" not in source
    assert ".write_bytes(" not in source
    assert ".open(\"w\"" not in source
