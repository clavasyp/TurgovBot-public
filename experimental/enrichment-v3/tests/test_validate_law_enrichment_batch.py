"""Offline contract checks for reviewed Phase A enrichment batches."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import export_law_enrichment_batches as exporter
from scripts import validate_law_enrichment_batch as validator
from services.law_enrichment_artifact import (
    enrichment_generation_input_hash,
    enrichment_generation_source,
)
from services.law_enrichment_review_contract import (
    build_review_source_projection,
)
from services.law_catalog_service import (
    derive_catalog_fields,
    source_article_hash,
)
from services.law_catalog_truth import (
    LAW_CATALOG_PARSER_CONTRACT_VERSION,
    LAW_SOURCE_CATALOG_BINDING_SCHEMA,
)
from services.law_synonym_service import (
    canonical_synonym_term,
    is_explicitly_empty_or_repealed_article,
)


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
            "article_ref": f"Статья {article_id}",
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
        "schema": exporter.BATCH_SCHEMA_V1,
        "server_id": "memphis",
        "source_fingerprint": {
            "algorithm": "sha256",
            "snapshot_version": 2,
            "documents": 1,
            "value": "f" * 64,
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
            f"узкая разговорная формулировка нормы {source['id']}",
            f"точная юридическая формулировка нормы {source['id']}",
        ],
        "generation_input_hash": source["generation_input_hash"],
    }


def _reviewed(source_batch: dict) -> list[dict]:
    return [
        _reviewed_item(article)
        for article in reversed(source_batch["articles"])
    ]


def _rehash(source_batch: dict) -> None:
    body = dict(source_batch)
    body.pop("batch_hash")
    source_batch["batch_hash"] = exporter._payload_hash(body)


def _source_catalog_binding(source_fingerprint: dict) -> dict:
    return {
        "schema": LAW_SOURCE_CATALOG_BINDING_SCHEMA,
        "parser_contract_version": LAW_CATALOG_PARSER_CONTRACT_VERSION,
        "server_id": "memphis",
        "source_fingerprint": deepcopy(source_fingerprint),
        "documents": source_fingerprint["documents"],
        "catalog_rows": 4,
        "article_rows": 2,
        "tombstone_rows": 0,
        "source_projection_hash": "a" * 64,
        "catalog_projection_hash": "b" * 64,
        "value": "c" * 64,
    }


def _exact_v2_pair() -> tuple[dict, list[dict]]:
    shared_body = "A narrow shared application situation is regulated."
    catalog = [
        {
            "id": 101,
            "law_document_id": 90,
            "source_order": 0,
            "article_ref": "Глава 1. Scope A",
            "kind": "chapter",
            "code": "1",
            "title": "Scope A",
            "doc_title": "Criminal Code",
            "full_text": "Глава 1. Scope A",
            "article_hash": source_article_hash("Глава 1. Scope A"),
        },
        {
            "id": 7,
            "law_document_id": 90,
            "source_order": 1,
            "article_ref": "Статья 7",
            "kind": "article",
            "code": "7",
            "title": None,
            "doc_title": "Criminal Code",
            "full_text": f"Статья 7\n1. {shared_body}",
            "article_hash": source_article_hash(
                f"Статья 7\n1. {shared_body}"
            ),
            "penalty_summary": None,
            "penalty_json": None,
        },
        {
            "id": 102,
            "law_document_id": 90,
            "source_order": 2,
            "article_ref": "Глава 2. Scope B",
            "kind": "chapter",
            "code": "2",
            "title": "Scope B",
            "doc_title": "Criminal Code",
            "full_text": "Глава 2. Scope B",
            "article_hash": source_article_hash("Глава 2. Scope B"),
        },
        {
            "id": 8,
            "law_document_id": 90,
            "source_order": 3,
            "article_ref": "Статья 8",
            "kind": "article",
            "code": "8",
            "title": None,
            "doc_title": "Criminal Code",
            "full_text": f"Статья 8\n1. {shared_body}",
            "article_hash": source_article_hash(
                f"Статья 8\n1. {shared_body}"
            ),
            "penalty_summary": None,
            "penalty_json": None,
        },
    ]
    articles = [row for row in catalog if row["kind"] == "article"]
    projection = build_review_source_projection(
        catalog,
        [7, 8],
        catalog_fields_resolver=derive_catalog_fields,
        article_hash_resolver=source_article_hash,
        is_tombstone=is_explicitly_empty_or_repealed_article,
        canonicalize_scope=canonical_synonym_term,
    )
    source_articles = []
    for article in articles:
        source_articles.append({
            "id": article["id"],
            "article_hash": article["article_hash"],
            "generation_input_hash": enrichment_generation_input_hash(
                article,
                (),
            ),
            "generation_context_hash": f"{article['id']:064x}",
            "source": enrichment_generation_source(article, ()),
            "review": {
                "status": "pending_codex_review",
                "required_output_fields": list(exporter.REVIEWED_FIELDS),
            },
        })
    source_fingerprint = {
        "algorithm": "sha256",
        "snapshot_version": 3,
        "documents": 1,
        "value": "f" * 64,
    }
    source = exporter._with_hash(
        {
            "schema": exporter.BATCH_SCHEMA,
            "server_id": "memphis",
            "source_fingerprint": source_fingerprint,
            "source_catalog_binding": _source_catalog_binding(
                source_fingerprint
            ),
            "context_contract": validator._CONTEXT_CONTRACT,
            "batch_number": 1,
            "review_source_projection": projection,
            "articles": source_articles,
        },
        "batch_hash",
    )
    prefixes = {
        member["article_id"]: member
        for member in projection["exact_groups"][0]["members"]
    }
    reviewed = [
        {
            "id": article["id"],
            "article_hash": article["article_hash"],
            "title": (
                prefixes[article["id"]]["title_scope_prefix"]
                + "Shared application rule"
            ),
            "penalty_summary": None,
            "penalty": None,
            "synonyms": [
                prefixes[article["id"]]["synonym_scope_prefix"]
                + "narrow shared application situation"
            ],
            "generation_input_hash": source_article[
                "generation_input_hash"
            ],
        }
        for article, source_article in zip(
            articles,
            source_articles,
            strict=True,
        )
    ]
    return source, reviewed


def test_validate_batch_accepts_exact_full_coverage_in_any_order():
    source = _source_batch()

    result = validator.validate_batch(
        "memphis",
        source,
        _reviewed(source),
    )

    assert result == {
        "ok": True,
        "schema": exporter.BATCH_SCHEMA_V1,
        "server_id": "memphis",
        "batch_number": 3,
        "batch_hash": source["batch_hash"],
        "articles": 2,
        "generation_context_hashes": {},
    }


def test_offline_review_accepts_at_most_fifteen_auto_synonyms():
    source = _source_batch()
    reviewed = _reviewed(source)
    reviewed[0]["synonyms"] = [
        f"проверенная норма редкийтермин{index}"
        for index in range(15)
    ]

    result = validator.validate_batch("memphis", source, reviewed)

    assert result["ok"] is True


def test_offline_review_rejects_more_than_fifteen_auto_synonyms():
    source = _source_batch()
    reviewed = _reviewed(source)
    reviewed[0]["synonyms"] = [
        f"проверенная норма редкийтермин{index}"
        for index in range(16)
    ]

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match="more than 15 distinct auto synonyms",
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_v2_batch_uses_hash_bound_exact_prefix_contract():
    source, reviewed = _exact_v2_pair()

    result = validator.validate_batch("memphis", source, reviewed)

    assert result["ok"] is True
    bad = deepcopy(reviewed)
    bad[0]["title"] = bad[1]["title"]
    with pytest.raises(
        validator.OfflineBatchValidationError,
        match="exact-body reviewed response failed",
    ):
        validator.validate_batch("memphis", source, bad)


def test_v2_offline_batch_rejects_source_unsupported_generated_title():
    source, reviewed = _exact_v2_pair()
    members = {
        member["article_id"]: member
        for member in source["review_source_projection"]["exact_groups"][0][
            "members"
        ]
    }
    for item in reviewed:
        item["title"] = (
            members[item["id"]]["title_scope_prefix"]
            + "Обязательное аннулирование лицензии судом"
        )

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match=r"reviewed title.*title evidence",
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_v2_batch_refuses_rehashed_projection_source_drift():
    source, reviewed = _exact_v2_pair()
    source["articles"][0]["source"]["text"] += " changed"
    source["articles"][0]["generation_input_hash"] = exporter._payload_hash(
        source["articles"][0]["source"]
    )
    reviewed[0]["generation_input_hash"] = source["articles"][0][
        "generation_input_hash"
    ]
    _rehash(source)

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match="differs from the source-only projection",
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_v2_batch_cross_binds_article_ref_to_projection():
    source, reviewed = _exact_v2_pair()
    source_article = source["articles"][0]
    source_article["source"]["article_ref"] = "Статья 700"
    source_article["generation_input_hash"] = exporter._payload_hash(
        source_article["source"]
    )
    reviewed[0]["generation_input_hash"] = source_article[
        "generation_input_hash"
    ]
    _rehash(source)

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match="article_ref.*differs from the source-only projection",
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_v2_coherent_raw_catalog_rehash_is_offline_consistent():
    """Offline proves internal source truth; live CAS anchors that truth."""

    source, reviewed = _exact_v2_pair()
    old_projection_hash = source["review_source_projection"][
        "projection_hash"
    ]
    raw_catalog = deepcopy(
        source["review_source_projection"]["source_catalog"]
    )
    for row in raw_catalog:
        row["document"] = "Rehashed but internally coherent source"
        if int(row["id"]) in {7, 8}:
            row["text"] += "\n2. A second neutral shared sentence."
            row["article_hash"] = source_article_hash(row["text"])
    catalog_rows = [
        {
            "id": row["id"],
            "law_document_id": row["law_document_id"],
            "source_order": row["source_order"],
            "article_ref": row["article_ref"],
            "title": None,
            "doc_title": row["document"],
            "full_text": row["text"],
            "article_hash": row["article_hash"],
        }
        for row in raw_catalog
    ]
    new_projection = build_review_source_projection(
        catalog_rows,
        [7, 8],
        catalog_fields_resolver=derive_catalog_fields,
        article_hash_resolver=source_article_hash,
        is_tombstone=is_explicitly_empty_or_repealed_article,
        canonicalize_scope=canonical_synonym_term,
    )
    assert new_projection["projection_hash"] != old_projection_hash
    source["review_source_projection"] = new_projection
    reviewed_by_id = {item["id"]: item for item in reviewed}
    raw_by_id = {int(row["id"]): row for row in raw_catalog}
    for article in source["articles"]:
        raw = raw_by_id[int(article["id"])]
        article["article_hash"] = raw["article_hash"]
        article["source"]["article_hash"] = raw["article_hash"]
        article["source"]["document"] = (
            "Rehashed but internally coherent source"
        )
        article["source"]["text"] = raw["text"]
        article["generation_input_hash"] = exporter._payload_hash(
            article["source"]
        )
        reviewed_by_id[article["id"]]["article_hash"] = raw[
            "article_hash"
        ]
        reviewed_by_id[article["id"]]["generation_input_hash"] = article[
            "generation_input_hash"
        ]
    _rehash(source)

    result = validator.validate_batch("memphis", source, reviewed)

    assert result["ok"] is True


def test_validate_batch_accepts_source_backed_penalty():
    source = _source_batch()
    source["articles"][0]["source"]["text"] = (
        "Статья 7. Наказание: штраф от $40,000 до $100,000."
    )
    _rehash(source)
    reviewed = _reviewed(source)
    target = next(item for item in reviewed if item["id"] == 7)
    target["penalty_summary"] = "штраф от $40,000 до $100,000"
    target["penalty"] = {"fine": "$40,000–$100,000"}

    result = validator.validate_batch("memphis", source, reviewed)

    assert result["ok"] is True


def test_validate_batch_refuses_invented_penalty_number():
    source = _source_batch()
    source["articles"][0]["source"]["text"] = (
        "Статья 7. Наказание: штраф $40,000."
    )
    _rehash(source)
    reviewed = _reviewed(source)
    target = next(item for item in reviewed if item["id"] == 7)
    target["penalty_summary"] = "штраф $50,000"
    target["penalty"] = {"fine": "$50,000"}

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match=r"article 7 penalty.*numeric fact '50000'",
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_validate_batch_refuses_citation_only_penalty():
    source = _source_batch()
    source["articles"][0]["source"]["text"] = (
        "Нарушение влечёт ответственность согласно статье 17.5 УК."
    )
    _rehash(source)
    reviewed = _reviewed(source)
    target = next(item for item in reviewed if item["id"] == 7)
    target["penalty_summary"] = "ответственность по статье 17.5 УК"
    target["penalty"] = {
        "other": "ответственность по статье 17.5 УК"
    }

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match="citation-only output",
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_validate_batch_refuses_source_unsupported_synonym():
    source = _source_batch()
    reviewed = _reviewed(source)
    target = next(item for item in reviewed if item["id"] == 7)
    target["synonyms"][0] = (
        "автоматическое аннулирование лицензии полицией за опоздание"
    )

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match=(
            r"article 7 synonym.*synonym evidence: "
            r".*source-unsupported semantic claim"
        ),
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_v1_offline_batch_rejects_source_unsupported_generated_title():
    source = _source_batch()
    reviewed = _reviewed(source)
    target = next(item for item in reviewed if item["id"] == 7)
    target["title"] = "Обязательное аннулирование лицензии судом"

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match=r"article 7 title.*title evidence",
    ):
        validator.validate_batch("memphis", source, reviewed)


@pytest.mark.parametrize(
    "generated_title",
    [
        "Суд обязан выдать лицензию",
        "Прокуратура обязана депортировать гражданина",
        "Суд обязательно лишает гражданства",
        "Суд обязан выдать ордер",
    ],
)
def test_v1_offline_batch_rejects_unknown_assertive_generated_title(
    generated_title: str,
) -> None:
    source = _source_batch()
    reviewed = _reviewed(source)
    target = next(item for item in reviewed if item["id"] == 7)
    target["title"] = generated_title

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match=r"article 7 title.*title evidence",
    ):
        validator.validate_batch("memphis", source, reviewed)


def test_v1_structural_context_cannot_support_negative_liability_title():
    source = _source_batch()
    source["articles"][0]["source"]["document"] = (
        "Глава: уголовная ответственность отсутствует"
    )
    _rehash(source)
    reviewed = _reviewed(source)
    target = next(item for item in reviewed if item["id"] == 7)
    target["title"] = "Уголовная ответственность отсутствует"

    with pytest.raises(
        validator.OfflineBatchValidationError,
        match=r"article 7 title.*title evidence",
    ):
        validator.validate_batch("memphis", source, reviewed)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda source, reviewed: source.update(
                schema="wrong-source-schema"
            ),
            "unsupported source schema",
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
                        "synonyms": [
                            f"{value} unexpected"
                            for value in reviewed[0]["synonyms"]
                        ],
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
