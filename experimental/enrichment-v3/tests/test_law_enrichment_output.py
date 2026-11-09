from __future__ import annotations

from copy import deepcopy

import pytest

from services.law_enrichment_output import (
    ENRICHMENT_OUTPUT_HASH_VERSION,
    build_enrichment_output_hash,
    enrichment_output_payload,
)


def _row(
    term: str,
    *,
    status: str = "approved",
    weight: float = 1.0,
    suggested_by: int | None = None,
) -> dict[str, object]:
    return {
        "id": 900,
        "server_id": "demo",
        "article_id": 7,
        "term": term,
        "source": "auto",
        "status": status,
        "weight": weight,
        "suggested_by": suggested_by,
    }


def _hash(
    rows: list[dict[str, object]],
    **overrides: object,
) -> str:
    values: dict[str, object] = {
        "server_id": "demo",
        "article_id": 7,
        "title": "Trespass",
        "title_provenance": "generated",
        "penalty_summary": "Fine $100",
        "penalty_json": {"fine": "$100"},
        "auto_rows": rows,
    }
    values.update(overrides)
    return build_enrichment_output_hash(**values)


def test_payload_is_versioned_and_row_order_or_ids_do_not_matter() -> None:
    first = _row("restricted area")
    second = _row("entering a restricted area")
    reordered = [deepcopy(second), deepcopy(first)]
    reordered[0]["id"] = 123456

    payload = enrichment_output_payload(
        server_id="demo",
        article_id=7,
        title="Trespass",
        title_provenance="generated",
        penalty_summary="Fine $100",
        penalty_json='{"fine": "$100"}',
        auto_rows=[first, second],
    )

    assert payload["version"] == ENRICHMENT_OUTPUT_HASH_VERSION
    assert _hash([first, second]) == _hash(reordered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Different title"),
        ("title_provenance", "source"),
        ("penalty_summary", "Fine $200"),
        ("penalty_json", {"fine": "$200"}),
    ],
)
def test_every_persisted_metadata_change_invalidates_hash(
    field: str,
    value: object,
) -> None:
    rows = [_row("restricted area")]

    assert _hash(rows, **{field: value}) != _hash(rows)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("term", "different term"),
        ("status", "stale"),
        ("weight", 0.5),
        ("suggested_by", 99),
    ],
)
def test_every_auto_row_semantic_change_invalidates_hash(
    field: str,
    value: object,
) -> None:
    baseline = [_row("restricted area")]
    changed = deepcopy(baseline)
    changed[0][field] = value

    assert _hash(changed) != _hash(baseline)


def test_add_or_remove_auto_row_invalidates_hash() -> None:
    first = _row("restricted area")
    second = _row("entering a restricted area")

    assert _hash([first]) != _hash([first, second])
    assert _hash([]) != _hash([first])


@pytest.mark.parametrize(
    "row_change",
    [
        {"article_id": 8},
        {"source": "owner"},
        {"weight": float("nan")},
        {"suggested_by": True},
    ],
)
def test_invalid_or_foreign_auto_rows_fail_closed(
    row_change: dict[str, object],
) -> None:
    row = _row("restricted area")
    row.update(row_change)

    with pytest.raises(ValueError):
        _hash([row])


def test_foreign_server_ownership_is_hashed_as_detectable_corruption() -> None:
    baseline = _row("restricted area")
    foreign = deepcopy(baseline)
    foreign["server_id"] = "other"

    assert _hash([foreign]) != _hash([baseline])
