from __future__ import annotations

from copy import deepcopy

import pytest

from services.law_enrichment_context import (
    EnrichmentContextInputError,
    build_enrichment_context_digests,
    build_source_only_enrichment_projection,
)
from services.law_enrichment_exact_duplicates import (
    build_exact_duplicate_plan,
)


def _row(
    row_id: int,
    *,
    kind: str,
    source_order: int,
    document_id: int = 10,
    article_ref: str,
    title: str | None = None,
    doc_title: str = "Public Safety Act",
    code: str | None = None,
    full_text: str | None = None,
) -> dict[str, object]:
    text = full_text if full_text is not None else article_ref
    return {
        "id": row_id,
        "law_document_id": document_id,
        "source_order": source_order,
        "kind": kind,
        "code": code,
        "article_ref": article_ref,
        "title": title,
        "doc_title": doc_title,
        "full_text": text,
        "article_hash": f"source-hash:{text}",
        # These generated/current fields must never affect a context digest.
        "enrich_hash": None,
        "current_auto_synonyms": [],
        "penalty_summary": None,
        "penalty_json": None,
        "title_provenance": None,
        "enrichment_context_hash": None,
    }


def _singletons() -> list[dict[str, object]]:
    return [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="Chapter I",
            title="General rules",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Article 1",
            code="1",
            full_text="Article 1\nKeep the gate closed.",
        ),
        _row(
            3,
            kind="chapter",
            source_order=3,
            article_ref="Chapter II",
            title="Special rules",
        ),
        _row(
            4,
            kind="article",
            source_order=4,
            article_ref="Article 2",
            code="2",
            full_text="Article 2\nShow a valid permit.",
        ),
    ]


def _duplicate_group() -> list[dict[str, object]]:
    operative_body = "A person must present an identity document."
    return [
        _row(
            10,
            kind="chapter",
            source_order=1,
            article_ref="Chapter I",
            title="Port territory",
        ),
        _row(
            11,
            kind="article",
            source_order=2,
            article_ref="Article 1",
            code="1",
            full_text=f"Article 1\n{operative_body}",
        ),
        _row(
            20,
            kind="chapter",
            source_order=3,
            article_ref="Chapter II",
            title="Airport territory",
        ),
        _row(
            21,
            kind="article",
            source_order=4,
            article_ref="Article 2",
            code="2",
            full_text=f"Article 2\n{operative_body}",
        ),
    ]


def _replace_source_text(
    row: dict[str, object],
    *,
    full_text: str,
) -> None:
    row["full_text"] = full_text
    row["article_hash"] = f"source-hash:{full_text}"


def test_input_order_does_not_change_any_digest() -> None:
    rows = _duplicate_group()

    forward = build_enrichment_context_digests(rows)
    reversed_input = build_enrichment_context_digests(list(reversed(rows)))

    assert forward == reversed_input
    assert all(
        len(value) == 64
        and set(value) <= set("0123456789abcdef")
        for value in forward.values()
    )


def test_source_heading_rename_changes_article_digest() -> None:
    rows = _singletons()
    before = build_enrichment_context_digests(rows, neighbor_radius=0)
    changed = deepcopy(rows)
    article = next(row for row in changed if row["id"] == 2)
    article["article_ref"] = "Article 1A"
    _replace_source_text(
        article,
        full_text="Article 1A\nKeep the gate closed.",
    )

    after = build_enrichment_context_digests(changed, neighbor_radius=0)

    assert after[2] != before[2]
    assert after[4] == before[4]


def test_structural_rename_changes_only_descendants_in_that_scope() -> None:
    rows = _singletons()
    before = build_enrichment_context_digests(rows, neighbor_radius=0)
    changed = deepcopy(rows)
    chapter = next(row for row in changed if row["id"] == 1)
    chapter["title"] = "Renamed general rules"
    _replace_source_text(chapter, full_text="Chapter I\nRenamed general rules")

    after = build_enrichment_context_digests(changed, neighbor_radius=0)

    assert after[2] != before[2]
    assert after[4] == before[4]


def test_article_move_between_chapters_changes_structural_context() -> None:
    rows = _singletons()
    before = build_enrichment_context_digests(rows)
    moved = deepcopy(rows)
    # Put Article 1 after Chapter II while leaving its source bytes intact.
    next(row for row in moved if row["id"] == 2)["source_order"] = 5

    after = build_enrichment_context_digests(moved)

    assert after[2] != before[2]


def test_document_title_change_changes_article_digest() -> None:
    rows = _singletons()
    before = build_enrichment_context_digests(rows)
    changed = deepcopy(rows)
    for row in changed:
        row["doc_title"] = "Renamed Public Safety Act"

    after = build_enrichment_context_digests(changed)

    assert set(after) == set(before)
    assert all(after[article_id] != before[article_id] for article_id in before)


@pytest.mark.parametrize(
    "change",
    ("body", "source_title", "order"),
)
def test_local_neighbor_source_change_invalidates_target_digest(
    change: str,
) -> None:
    rows = _singletons()
    before = build_enrichment_context_digests(rows)
    changed = deepcopy(rows)
    neighbor = next(row for row in changed if row["id"] == 4)
    if change == "body":
        _replace_source_text(
            neighbor,
            full_text="Article 2\nShow a valid permit and identification.",
        )
    elif change == "source_title":
        neighbor["article_ref"] = "Article 2A: Permit inspection"
        _replace_source_text(
            neighbor,
            full_text=(
                "Article 2A: Permit inspection\nShow a valid permit."
            ),
        )
    else:
        neighbor["source_order"] = 5

    after = build_enrichment_context_digests(changed)

    # Article 2 is a same-document local neighbor of unchanged Article 1.
    assert after[2] != before[2]


def test_neighbor_radius_is_part_of_every_context_contract() -> None:
    rows = _singletons()

    without_neighbors = build_enrichment_context_digests(
        rows,
        neighbor_radius=0,
    )
    default_radius = build_enrichment_context_digests(rows)

    assert set(without_neighbors) == set(default_radius)
    assert all(
        without_neighbors[article_id] != default_radius[article_id]
        for article_id in default_radius
    )


def test_group_add_and_remove_changes_existing_member_digests() -> None:
    rows = _duplicate_group()
    two_member = build_enrichment_context_digests(rows)
    expanded = deepcopy(rows)
    expanded.extend([
        _row(
            30,
            kind="chapter",
            source_order=5,
            article_ref="Chapter III",
            title="Rail territory",
        ),
        _row(
            31,
            kind="article",
            source_order=6,
            article_ref="Article 3",
            code="3",
            full_text=(
                "Article 3\n"
                "A person must present an identity document."
            ),
        ),
    ])

    three_member = build_enrichment_context_digests(expanded)
    restored = build_enrichment_context_digests(expanded[:-2])

    assert three_member[11] != two_member[11]
    assert three_member[21] != two_member[21]
    assert restored == two_member


def test_group_member_body_change_changes_unchanged_survivor_digest() -> None:
    rows = _duplicate_group()
    grouped = build_enrichment_context_digests(rows)
    changed = deepcopy(rows)
    member = next(row for row in changed if row["id"] == 21)
    _replace_source_text(
        member,
        full_text=(
            "Article 2\n"
            "A person must present an identity document and a permit."
        ),
    )

    split = build_enrichment_context_digests(changed)

    # Article 11 bytes did not change, but its exact-body topology did.
    assert split[11] != grouped[11]
    assert split[21] != grouped[21]


def test_every_article_including_excluded_tombstone_gets_digest() -> None:
    rows = _singletons()
    rows.append(
        _row(
            5,
            kind="article",
            source_order=5,
            article_ref="Article 3",
            code="3",
            full_text="Article 3\nREPEALED",
        )
    )

    digests = build_enrichment_context_digests(
        rows,
        is_substantive_article=lambda row: "REPEALED" not in str(
            row.get("full_text") or ""
        ),
    )

    assert set(digests) == {2, 4, 5}


def test_generated_outputs_do_not_change_context_digest() -> None:
    rows = _singletons()
    before = build_enrichment_context_digests(
        rows,
        competitor_ids_by_article={2: [4]},
    )
    generated_changes = deepcopy(rows)
    for row in generated_changes:
        if row["kind"] != "article":
            continue
        row["title"] = "Model-generated title"
        row["enrich_hash"] = "some previous enrichment token"
        row["current_auto_synonyms"] = ["generated phrase"]
        row["penalty_summary"] = "generated penalty"
        row["penalty_json"] = {"type": "fine"}
        row["title_provenance"] = "generated"
        row["enrichment_context_hash"] = "f" * 64

    after = build_enrichment_context_digests(
        generated_changes,
        competitor_ids_by_article={2: [4]},
    )

    assert after == before


def test_same_body_move_changes_digest_and_shared_projection_ancestry() -> None:
    rows = _singletons()
    resolver = lambda article_ref, _text: article_ref
    before_projection = build_source_only_enrichment_projection(
        rows,
        source_title_resolver=resolver,
    )
    before = build_enrichment_context_digests(
        rows,
        source_title_resolver=resolver,
    )

    moved = deepcopy(rows)
    next(row for row in moved if row["id"] == 2)["source_order"] = 5
    after_projection = build_source_only_enrichment_projection(
        moved,
        source_title_resolver=resolver,
    )
    after = build_enrichment_context_digests(
        moved,
        source_title_resolver=resolver,
    )

    assert before_projection[2]["source"]["text"] == (
        after_projection[2]["source"]["text"]
    )
    assert before_projection[2]["structural_ancestry"]["chapter"]["id"] == 1
    assert after_projection[2]["structural_ancestry"]["chapter"]["id"] == 3
    assert after[2] != before[2]


def test_project_context_family_and_policy_versions_invalidate_digest() -> None:
    rows = _singletons()
    common = {
        "source_title_resolver": lambda article_ref, _text: article_ref,
        "project_context": " GTA V setting\r\nUse LSPD. ",
        "project_family": " MAJESTIC_RP ",
        "generation_policy_version": "generation-policy-v1",
        "source_title_policy_version": "source-title-policy-v1",
        "parser_contract_version": "parser-contract-v1",
    }
    baseline = build_enrichment_context_digests(rows, **common)

    # Unicode/newline/edge-whitespace normalization is stable.
    normalized = build_enrichment_context_digests(
        rows,
        **{
            **common,
            "project_context": "GTA V setting\nUse LSPD.",
            "project_family": "majestic_rp",
        },
    )
    assert normalized == baseline

    variants = [
        {"project_context": "GTA V setting\nUse FIB."},
        {"project_family": "gta5rp"},
        {"generation_policy_version": "generation-policy-v2"},
        {"source_title_policy_version": "source-title-policy-v2"},
        {"parser_contract_version": "parser-contract-v2"},
    ]
    for change in variants:
        changed = build_enrichment_context_digests(
            rows,
            **{**common, **change},
        )
        assert set(changed) == set(baseline)
        assert all(
            changed[article_id] != baseline[article_id]
            for article_id in baseline
        )


def test_parser_derived_source_title_is_in_projection_and_digest() -> None:
    rows = _singletons()
    first_resolver = lambda article_ref, _text: f"v1:{article_ref}"
    second_resolver = lambda article_ref, _text: f"v2:{article_ref}"

    projection = build_source_only_enrichment_projection(
        rows,
        source_title_resolver=first_resolver,
    )
    first = build_enrichment_context_digests(
        rows,
        source_title_resolver=first_resolver,
        source_title_policy_version="derivation-v1",
    )
    second = build_enrichment_context_digests(
        rows,
        source_title_resolver=second_resolver,
        source_title_policy_version="derivation-v2",
    )

    assert projection[2]["source"]["source_title"] == "v1:Article 1"
    assert first[2] != second[2]


def test_competitor_source_change_invalidates_target_context() -> None:
    rows = _singletons()
    competitors = {2: [4]}
    before = build_enrichment_context_digests(
        rows,
        competitor_ids_by_article=competitors,
    )
    changed = deepcopy(rows)
    competitor = next(row for row in changed if row["id"] == 4)
    _replace_source_text(
        competitor,
        full_text="Article 2\nShow a valid permit and identification.",
    )

    after = build_enrichment_context_digests(
        changed,
        competitor_ids_by_article=competitors,
    )

    assert after[2] != before[2]


def test_selector_receives_no_generated_outputs_and_order_is_normalized() -> None:
    rows = _singletons()
    for row in rows:
        if row["kind"] == "article":
            row["title"] = "Model-generated title"
            row["current_auto_synonyms"] = ["model-generated synonym"]
    seen: list[tuple[dict[str, object], ...]] = []

    def selector(targets, corpus):
        seen.append(tuple(dict(row) for row in targets))
        assert tuple(targets) == tuple(corpus)
        return {2: [{"article_id": 4}], 4: ()}

    first = build_enrichment_context_digests(
        rows,
        competitor_selector=selector,
    )
    second = build_enrichment_context_digests(
        list(reversed(rows)),
        competitor_ids_by_article={2: [4]},
    )

    assert first == second
    assert seen
    assert all(row["title"] == row["article_ref"] for row in seen[0])
    assert all(row["title"] != "Model-generated title" for row in seen[0])
    assert all("current_auto_synonyms" not in row for row in seen[0])


def test_prebuilt_plan_matches_internal_plan() -> None:
    rows = _duplicate_group()
    plan = build_exact_duplicate_plan(rows)

    internal = build_enrichment_context_digests(rows)
    supplied = build_enrichment_context_digests(
        rows,
        exact_duplicate_plan=plan,
    )

    assert supplied == internal


def test_stale_prebuilt_group_body_is_rejected() -> None:
    rows = _duplicate_group()
    plan = build_exact_duplicate_plan(rows)
    changed = deepcopy(rows)
    member = next(row for row in changed if row["id"] == 21)
    _replace_source_text(member, full_text="Article 2\nChanged operative body.")

    with pytest.raises(
        EnrichmentContextInputError,
        match="current source facts|no longer byte-equal",
    ):
        build_enrichment_context_digests(
            changed,
            exact_duplicate_plan=plan,
        )


def test_prebuilt_plan_missing_new_group_member_is_rejected() -> None:
    rows = _duplicate_group()
    stale_plan = build_exact_duplicate_plan(rows)
    changed = deepcopy(rows)
    changed.extend([
        _row(
            30,
            kind="chapter",
            source_order=5,
            article_ref="Chapter III",
            title="Rail territory",
        ),
        _row(
            31,
            kind="article",
            source_order=6,
            article_ref="Article 3",
            code="3",
            full_text=(
                "Article 3\n"
                "A person must present an identity document."
            ),
        ),
    ])

    with pytest.raises(
        EnrichmentContextInputError,
        match="group topology is incomplete",
    ):
        build_enrichment_context_digests(
            changed,
            exact_duplicate_plan=stale_plan,
        )


def test_algorithm_version_invalidates_every_digest() -> None:
    rows = _singletons()

    first = build_enrichment_context_digests(
        rows,
        algorithm_version="context-v1",
    )
    second = build_enrichment_context_digests(
        rows,
        algorithm_version="context-v2",
    )

    assert set(first) == set(second)
    assert all(first[key] != second[key] for key in first)


def test_duplicate_source_position_fails_closed() -> None:
    rows = _singletons()
    rows[3]["source_order"] = rows[2]["source_order"]

    with pytest.raises(
        EnrichmentContextInputError,
        match="duplicate catalog source position",
    ):
        build_enrichment_context_digests(rows)
