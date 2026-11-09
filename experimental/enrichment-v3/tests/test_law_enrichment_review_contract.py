"""Shared offline/live source-projection safety contracts."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json

import pytest

from services.law_enrichment_review_contract import (
    ReviewSourceContractError,
    build_review_source_projection,
    combine_review_source_projections,
    review_source_projection_atomic_units,
    select_review_source_projection,
    validate_reviewed_source_projection,
)


def _row(
    row_id: int,
    *,
    source_order: int,
    kind: str,
    article_ref: str,
    title: str | None,
    full_text: str,
) -> dict:
    return {
        "id": row_id,
        "law_document_id": 90,
        "source_order": source_order,
        "article_ref": article_ref,
        "kind": kind,
        "code": str(row_id),
        "title": title,
        "doc_title": "Criminal Code",
        "full_text": full_text,
        "article_hash": _source_hash(full_text),
    }


def _source_hash(full_text: str) -> str:
    return hashlib.md5(full_text.encode("utf-8")).hexdigest()


def _catalog() -> list[dict]:
    shared_body = (
        "A person may submit the application to the competent authority."
    )
    return [
        _row(
            100,
            source_order=0,
            kind="section",
            article_ref="Section I",
            title="Licensing",
            full_text="Section I\nLicensing",
        ),
        _row(
            110,
            source_order=1,
            kind="chapter",
            article_ref="Chapter A",
            title="Chapter A",
            full_text="Chapter A\nScope A",
        ),
        _row(
            1,
            source_order=2,
            kind="article",
            article_ref="Article 1",
            title=None,
            full_text=f"Article 1\n{shared_body}",
        ),
        _row(
            120,
            source_order=3,
            kind="chapter",
            article_ref="Chapter B",
            title="Chapter B",
            full_text="Chapter B\nScope B",
        ),
        _row(
            2,
            source_order=4,
            kind="article",
            article_ref="Article 2",
            title=None,
            full_text=f"Article 2\n{shared_body}",
        ),
        _row(
            3,
            source_order=5,
            kind="article",
            article_ref="Article 3",
            title="Persisted value is deliberately ignored",
            full_text="Article 3\nA neutral standalone provision.",
        ),
        _row(
            4,
            source_order=6,
            kind="article",
            article_ref="Article 4",
            title=None,
            full_text="Article 4\nREPEALED",
        ),
    ]


def _catalog_hash(row_id: int) -> str:
    return next(
        str(row["article_hash"])
        for row in _catalog()
        if int(row["id"]) == row_id
    )


def _source_title(article_ref: str | None, _full_text: str) -> str | None:
    if article_ref == "Article 3":
        return "Immutable title"
    if isinstance(article_ref, str) and article_ref.startswith("Chapter "):
        return article_ref
    if article_ref == "Section I":
        return "Licensing"
    return None


def _catalog_fields(
    article_ref: str | None,
    full_text: str,
) -> tuple[str, str | None, str | None]:
    if article_ref == "Section I":
        return "section", "I", _source_title(article_ref, full_text)
    if isinstance(article_ref, str) and article_ref.startswith("Chapter "):
        return "chapter", article_ref.rsplit(" ", 1)[-1], _source_title(
            article_ref,
            full_text,
        )
    return "article", (
        article_ref.rsplit(" ", 1)[-1]
        if isinstance(article_ref, str)
        else None
    ), _source_title(article_ref, full_text)


def _is_tombstone(full_text) -> bool:
    return "REPEALED" in str(full_text or "")


def _canonical(value: str) -> str | None:
    normalized = " ".join(str(value).lower().split())
    return normalized or None


def _build(target_ids) -> dict:
    return build_review_source_projection(
        _catalog(),
        target_ids,
        catalog_fields_resolver=_catalog_fields,
        article_hash_resolver=_source_hash,
        is_tombstone=_is_tombstone,
        canonicalize_scope=_canonical,
    )


def _rehash_projection(projection: dict) -> None:
    body = dict(projection)
    body.pop("projection_hash")
    projection["projection_hash"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _exact_items() -> list[dict]:
    hashes = {
        int(row["id"]): row["article_hash"]
        for row in _catalog()
    }
    return [
        {
            "id": 1,
            "article_hash": hashes[1],
            "title": "[Chapter A] Shared application rule",
            "penalty_summary": None,
            "penalty": None,
            "synonyms": [
                "[chapter a] narrow shared application situation",
            ],
        },
        {
            "id": 2,
            "article_hash": hashes[2],
            "title": "[Chapter B] Shared application rule",
            "penalty_summary": None,
            "penalty": None,
            "synonyms": [
                "[chapter b] narrow shared application situation",
            ],
        },
    ]


def test_projection_keeps_exact_group_indivisible_and_source_backed():
    projection = _build([1, 2, 3])

    assert review_source_projection_atomic_units(projection) == (
        (1, 2),
        (3,),
    )
    group = projection["exact_groups"][0]
    assert [member["article_id"] for member in group["members"]] == [1, 2]
    assert projection["articles"][0]["structural_ancestry"]["chapter"][
        "source_title"
    ] == "Chapter A"
    assert projection["articles"][2]["source"]["source_title"] == (
        "Immutable title"
    )

    with pytest.raises(
        ReviewSourceContractError,
        match="splits exact-body group",
    ):
        select_review_source_projection(projection, [1])


def test_same_projection_validator_is_strict_about_trusted_prefixes():
    projection = _build([1, 2])
    items = _exact_items()

    validate_reviewed_source_projection(
        projection,
        items,
        catalog_fields_resolver=_catalog_fields,
        article_hash_resolver=_source_hash,
        is_tombstone=_is_tombstone,
        canonicalize_scope=_canonical,
        canonicalize_suffix=_canonical,
    )

    bad = deepcopy(items)
    bad[0]["synonyms"][0] = (
        "[chapter b] narrow shared application situation"
    )
    with pytest.raises(
        ReviewSourceContractError,
        match="exact-body reviewed response failed",
    ):
        validate_reviewed_source_projection(
            projection,
            bad,
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )


def test_batch_projections_recombine_to_the_full_hash_and_full_group_proof():
    full = _build([1, 2, 3])
    exact_batch = select_review_source_projection(full, [1, 2])
    single_batch = select_review_source_projection(full, [3])

    combined = combine_review_source_projections(
        [single_batch, exact_batch],
        catalog_fields_resolver=_catalog_fields,
        article_hash_resolver=_source_hash,
        is_tombstone=_is_tombstone,
        canonicalize_scope=_canonical,
    )

    assert combined["projection_hash"] == full["projection_hash"]
    assert [article["id"] for article in combined["articles"]] == [1, 2, 3]


def test_multiple_exact_groups_have_batch_independent_projection_hash():
    catalog = _catalog()
    second_body = "A second byte-identical operative body."
    catalog.extend([
        _row(
            130,
            source_order=7,
            kind="chapter",
            article_ref="Chapter C",
            title="Chapter C",
            full_text="Chapter C\nScope C",
        ),
        _row(
            5,
            source_order=8,
            kind="article",
            article_ref="Article 5",
            title=None,
            full_text=f"Article 5\n{second_body}",
        ),
        _row(
            140,
            source_order=9,
            kind="chapter",
            article_ref="Chapter D",
            title="Chapter D",
            full_text="Chapter D\nScope D",
        ),
        _row(
            6,
            source_order=10,
            kind="article",
            article_ref="Article 6",
            title=None,
            full_text=f"Article 6\n{second_body}",
        ),
    ])
    full = build_review_source_projection(
        catalog,
        [1, 2, 3, 5, 6],
        catalog_fields_resolver=_catalog_fields,
        article_hash_resolver=_source_hash,
        is_tombstone=_is_tombstone,
        canonicalize_scope=_canonical,
    )

    combined = combine_review_source_projections(
        [
            select_review_source_projection(full, [5, 6]),
            select_review_source_projection(full, [3]),
            select_review_source_projection(full, [1, 2]),
        ],
        catalog_fields_resolver=_catalog_fields,
        article_hash_resolver=_source_hash,
        is_tombstone=_is_tombstone,
        canonicalize_scope=_canonical,
    )

    assert combined["projection_hash"] == full["projection_hash"]


def test_source_title_and_tombstone_rules_are_shared():
    source_owned = _build([3])
    changed_title = [{
        "id": 3,
        "article_hash": _catalog_hash(3),
        "title": "Invented replacement",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": ["neutral standalone provision"],
    }]
    with pytest.raises(
        ReviewSourceContractError,
        match="reviewed title is not supported",
    ):
        validate_reviewed_source_projection(
            source_owned,
            changed_title,
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )

    tombstone = _build([4])
    dirty_tombstone = [{
        "id": 4,
        "article_hash": _catalog_hash(4),
        "title": "Repealed provision",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [],
    }]
    with pytest.raises(
        ReviewSourceContractError,
        match="tombstone",
    ):
        validate_reviewed_source_projection(
            tombstone,
            dirty_tombstone,
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )


def test_rehashed_parser_title_tamper_is_rederived_from_raw_source():
    projection = _build([3])
    projection["articles"][0]["source"]["source_title"] = None
    _rehash_projection(projection)
    item = {
        "id": 3,
        "article_hash": _catalog_hash(3),
        "title": "Invented title after deleting parser ownership",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": ["neutral standalone provision"],
    }

    with pytest.raises(
        ReviewSourceContractError,
        match="derived fields differ.*articles",
    ):
        validate_reviewed_source_projection(
            projection,
            [item],
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )


def test_rehashed_raw_article_hash_is_rederived_from_source_text():
    projection = _build([3])
    raw_row = next(
        row
        for row in projection["source_catalog"]
        if int(row["id"]) == 3
    )
    raw_row["article_hash"] = "0" * 32
    _rehash_projection(projection)
    item = {
        "id": 3,
        "article_hash": _catalog_hash(3),
        "title": "Immutable title",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": ["neutral standalone provision"],
    }

    with pytest.raises(
        ReviewSourceContractError,
        match="article_hash differs from the canonical hash",
    ):
        validate_reviewed_source_projection(
            projection,
            [item],
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )


def test_rehashed_trusted_prefix_tamper_is_rederived_from_raw_structure():
    projection = _build([1, 2])
    group = projection["exact_groups"][0]
    member = group["members"][0]
    old_title_prefix = member["title_scope_prefix"]
    old_synonym_prefix = member["synonym_scope_prefix"]
    member["scope_text"] = "Forged chapter"
    member["title_scope_prefix"] = "[Forged chapter] "
    member["synonym_scope_prefix"] = "[forged chapter] "
    _rehash_projection(projection)
    items = _exact_items()
    items[0]["title"] = items[0]["title"].replace(
        old_title_prefix,
        member["title_scope_prefix"],
        1,
    )
    items[0]["synonyms"][0] = items[0]["synonyms"][0].replace(
        old_synonym_prefix,
        member["synonym_scope_prefix"],
        1,
    )

    with pytest.raises(
        ReviewSourceContractError,
        match="derived fields differ.*exact_groups",
    ):
        validate_reviewed_source_projection(
            projection,
            items,
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )


def test_rehashed_exact_topology_removal_cannot_enable_asymmetric_output():
    projection = _build([1, 2])
    projection["exact_groups"] = []
    for article in projection["articles"]:
        article["exact_group_key"] = None
    _rehash_projection(projection)
    items = _exact_items()
    items[0]["title"] = "First asymmetric application rule"
    items[1]["title"] = "Second asymmetric application rule"
    items[0]["synonyms"] = ["first narrow shared application situation"]
    items[1]["synonyms"] = ["second narrow shared application situation"]

    with pytest.raises(
        ReviewSourceContractError,
        match="derived fields differ",
    ):
        validate_reviewed_source_projection(
            projection,
            items,
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )


@pytest.mark.parametrize("field", ("tombstone", "ancestry"))
def test_rehashed_tombstone_or_ancestry_tamper_is_rederived(field):
    projection = _build([4] if field == "tombstone" else [3])
    if field == "tombstone":
        projection["articles"][0]["tombstone"] = False
    else:
        projection["articles"][0]["structural_ancestry"]["chapter"][
            "source_title"
        ] = "Forged ancestry"
    _rehash_projection(projection)
    item = {
        "id": 4 if field == "tombstone" else 3,
        "article_hash": _catalog_hash(
            4 if field == "tombstone" else 3
        ),
        "title": None if field == "tombstone" else "Immutable title",
        "penalty_summary": None,
        "penalty": None,
        "synonyms": [] if field == "tombstone" else [
            "neutral standalone provision"
        ],
    }

    with pytest.raises(
        ReviewSourceContractError,
        match="derived fields differ",
    ):
        validate_reviewed_source_projection(
            projection,
            [item],
            catalog_fields_resolver=_catalog_fields,
            article_hash_resolver=_source_hash,
            is_tombstone=_is_tombstone,
            canonicalize_scope=_canonical,
            canonicalize_suffix=_canonical,
        )
