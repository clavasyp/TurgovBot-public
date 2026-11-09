from __future__ import annotations

import pytest

from services.law_enrichment_exact_duplicates import (
    ExactDuplicateResponseError,
    ExactDuplicateScopeError,
    build_exact_duplicate_plan,
    close_targets_over_exact_duplicate_groups,
    extract_proven_operative_body,
    validate_exact_duplicate_group_response,
)


def _row(
    row_id: int,
    *,
    kind: str,
    document_id: int = 10,
    source_order: int,
    article_ref: str,
    title: str | None = None,
    doc_title: str = "Закон о территориях",
    full_text: str | None = None,
) -> dict[str, object]:
    return {
        "id": row_id,
        "kind": kind,
        "law_document_id": document_id,
        "source_order": source_order,
        "article_ref": article_ref,
        "title": title,
        "doc_title": doc_title,
        "full_text": full_text if full_text is not None else article_ref,
        "article_hash": f"hash-{row_id}",
    }


def _sf_like_rows() -> list[dict[str, object]]:
    body_a = "Лицо обязано предъявить документы.\nСанкция отсутствует."
    body_b = "Проход разрешен только по пропуску.\nНарушение запрещено."
    return [
        _row(
            100,
            kind="chapter",
            source_order=1,
            article_ref="Глава III",
            title="Закрытые территории",
        ),
        _row(
            101,
            kind="article",
            source_order=2,
            article_ref="Статья 3.1",
            full_text=f"Статья 3.1\n{body_a}",
        ),
        _row(
            102,
            kind="article",
            source_order=3,
            article_ref="Статья 3.2",
            full_text=f"Статья 3.2\n{body_b}",
        ),
        _row(
            200,
            kind="chapter",
            source_order=4,
            article_ref="Глава IV",
            title="Режимные объекты",
        ),
        _row(
            201,
            kind="article",
            source_order=5,
            article_ref="Статья 4.1",
            full_text=f"Статья 4.1\n{body_a}",
        ),
        _row(
            202,
            kind="article",
            source_order=6,
            article_ref="Статья 4.2",
            full_text=f"Статья 4.2\n{body_b}",
        ),
    ]


def _valid_group_items(group) -> list[dict[str, object]]:
    return [
        {
            "id": member.article_id,
            "title": (
                f"{member.title_scope_prefix}Предъявление документов"
            ),
            "synonyms": [
                (
                    f"{member.synonym_scope_prefix}"
                    "кто обязан предъявить документы"
                ),
                (
                    f"{member.synonym_scope_prefix}"
                    "проверка документов у лица"
                ),
            ],
            "penalty_summary": "Санкция отсутствует",
            "penalty": {"type": "none"},
        }
        for member in group.members
    ]


def test_builds_two_deterministic_chapter_scoped_groups() -> None:
    rows = _sf_like_rows()

    first = build_exact_duplicate_plan(rows)
    second = build_exact_duplicate_plan(list(reversed(rows)))

    assert first == second
    assert [group.article_ids for group in first.groups] == [
        (101, 201),
        (102, 202),
    ]
    assert {group.scope_level for group in first.groups} == {"chapter"}
    for group in first.groups:
        assert len({member.scope_prefix for member in group.members}) == 2
        assert {
            member.scope_text for member in group.members
        } == {"Закрытые территории", "Режимные объекты"}
        assert {
            member.title_scope_prefix for member in group.members
        } == {
            "[Закрытые территории] ",
            "[Режимные объекты] ",
        }
        assert {
            member.synonym_scope_prefix for member in group.members
        } == {
            "[закрытые территории] ",
            "[режимные объекты] ",
        }
        assert all(
            member.scope_prefix == member.synonym_scope_prefix
            for member in group.members
        )


def test_runtime_canonicalizer_skips_collapsed_titles_for_safe_references() -> None:
    body = "The operative wording is byte-identical."
    rows = [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="Area Alpha",
            title="Regulation!",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Article 1",
            full_text=f"Article 1\n{body}",
        ),
        _row(
            3,
            kind="chapter",
            source_order=3,
            article_ref="Area Beta",
            title="Regulation?",
        ),
        _row(
            4,
            kind="article",
            source_order=4,
            article_ref="Article 2",
            full_text=f"Article 2\n{body}",
        ),
    ]

    def runtime_key(value: str) -> str:
        return " ".join(
            value.casefold().translate(
                str.maketrans({"!": " ", "?": " ", "—": " "})
            ).split()
        )

    group = build_exact_duplicate_plan(
        rows,
        canonicalize_scope=runtime_key,
    ).groups[0]

    assert {
        member.scope_text for member in group.members
    } == {
        "Area Alpha — Regulation!",
        "Area Beta — Regulation?",
    }
    assert len(
        {
            runtime_key(member.synonym_scope_prefix[1:-2])
            for member in group.members
        }
    ) == 2


def test_runtime_canonicalizer_receives_emitted_storage_scope() -> None:
    rows = _sf_like_rows()
    seen: list[str] = []

    def runtime_key(value: str) -> str:
        seen.append(value)
        assert value == value.lower().replace("ё", "е")
        assert not value.startswith("[")
        assert not value.endswith("] ")
        return value

    plan = build_exact_duplicate_plan(
        rows,
        canonicalize_scope=runtime_key,
    )

    assert len(plan.groups) == 2
    assert seen
    assert {
        member.synonym_scope_prefix[1:-2]
        for group in plan.groups
        for member in group.members
    } <= set(seen)


def test_runtime_canonicalizer_fails_when_every_scope_collapses() -> None:
    body = "The operative wording is byte-identical."
    rows = [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="Scope!",
            title="Regulation!",
            doc_title="Law!",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Article 1",
            doc_title="Law!",
            full_text=f"Article 1\n{body}",
        ),
        _row(
            3,
            kind="chapter",
            source_order=3,
            article_ref="Scope?",
            title="Regulation?",
            doc_title="Law?",
        ),
        _row(
            4,
            kind="article",
            source_order=4,
            article_ref="Article 2",
            doc_title="Law?",
            full_text=f"Article 2\n{body}",
        ),
    ]

    def runtime_key(value: str) -> str:
        return " ".join(
            value.casefold().translate(
                str.maketrans(
                    {
                        "!": " ",
                        "?": " ",
                        "—": " ",
                        "/": " ",
                    }
                )
            ).split()
        )

    with pytest.raises(ExactDuplicateScopeError):
        build_exact_duplicate_plan(
            rows,
            canonicalize_scope=runtime_key,
        )


@pytest.mark.parametrize(
    "invisible",
    ["\u00ad", "\u200b", "\u2060", "\x00", "\ue000"],
)
def test_default_ignorable_scope_titles_are_rejected(
    invisible: str,
) -> None:
    body = "The operative wording is byte-identical."
    rows = [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="Area Alpha",
            title=f"Regu{invisible}lation A",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Article 1",
            full_text=f"Article 1\n{body}",
        ),
        _row(
            3,
            kind="chapter",
            source_order=3,
            article_ref="Area Beta",
            title=f"Regu{invisible}lation B",
        ),
        _row(
            4,
            kind="article",
            source_order=4,
            article_ref="Article 2",
            full_text=f"Article 2\n{body}",
        ),
    ]

    group = build_exact_duplicate_plan(rows).groups[0]

    assert {
        member.scope_text for member in group.members
    } == {"Area Alpha", "Area Beta"}
    assert all(
        invisible not in member.scope_prefix
        for member in group.members
    )


def test_entirely_invisible_structural_labels_fail_closed() -> None:
    body = "The operative wording is byte-identical."
    rows = [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="\u2060",
            title="\u200b",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Article 1",
            full_text=f"Article 1\n{body}",
        ),
        _row(
            3,
            kind="chapter",
            source_order=3,
            article_ref="\u2060",
            title="\u200b",
        ),
        _row(
            4,
            kind="article",
            source_order=4,
            article_ref="Article 2",
            full_text=f"Article 2\n{body}",
        ),
    ]

    with pytest.raises(ExactDuplicateScopeError):
        build_exact_duplicate_plan(rows)


def test_uses_document_plus_structure_for_identical_paths_in_other_docs() -> None:
    body = "Одинаковое содержательное правило."
    rows = [
        _row(
            1,
            kind="chapter",
            document_id=10,
            source_order=1,
            article_ref="Глава I",
            title="Общие правила",
            doc_title="Первый закон",
        ),
        _row(
            2,
            kind="article",
            document_id=10,
            source_order=2,
            article_ref="Статья 1",
            doc_title="Первый закон",
            full_text=f"Статья 1\n{body}",
        ),
        _row(
            3,
            kind="chapter",
            document_id=20,
            source_order=1,
            article_ref="Глава I",
            title="Общие правила",
            doc_title="Второй закон",
        ),
        _row(
            4,
            kind="article",
            document_id=20,
            source_order=2,
            article_ref="Статья 1",
            doc_title="Второй закон",
            full_text=f"Статья 1\n{body}",
        ),
    ]

    plan = build_exact_duplicate_plan(rows)

    assert len(plan.groups) == 1
    group = plan.groups[0]
    assert group.scope_level == "document_structure"
    assert group.article_ids == (2, 4)
    assert group.members[0].scope_text.startswith("Первый закон / ")
    assert group.members[1].scope_text.startswith("Второй закон / ")


def test_crlf_header_is_allowed_but_body_remains_exact_bytes() -> None:
    row = _row(
        1,
        kind="article",
        source_order=1,
        article_ref="  Статья 1  ",
        full_text="Статья 1\r\nПервая строка\r\nВторая строка",
    )

    assert extract_proven_operative_body(row) == (
        "Первая строка\r\nВторая строка".encode("utf-8")
    )


def test_header_mismatch_excludes_article_instead_of_guessing() -> None:
    rows = [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="Глава I",
            title="Первая область",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Статья 1",
            full_text="Статья 1. Название\nОдинаковое правило",
        ),
        _row(
            3,
            kind="chapter",
            source_order=3,
            article_ref="Глава II",
            title="Вторая область",
        ),
        _row(
            4,
            kind="article",
            source_order=4,
            article_ref="Статья 2",
            full_text="Статья 2\nОдинаковое правило",
        ),
    ]

    plan = build_exact_duplicate_plan(rows)

    assert plan.groups == ()
    assert plan.excluded_article_ids == (2,)


def test_caller_hook_excludes_identical_repealed_articles() -> None:
    rows = [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="Глава I",
            title="Первая область",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Статья 1",
            full_text="Статья 1\nСтатья утратила силу.",
        ),
        _row(
            3,
            kind="chapter",
            source_order=3,
            article_ref="Глава II",
            title="Вторая область",
        ),
        _row(
            4,
            kind="article",
            source_order=4,
            article_ref="Статья 2",
            full_text="Статья 2\nСтатья утратила силу.",
        ),
    ]

    plan = build_exact_duplicate_plan(
        rows,
        is_substantive_article=lambda row: (
            "утратила силу" not in str(row["full_text"])
        ),
    )

    assert plan.groups == ()
    assert plan.excluded_article_ids == (2, 4)


def test_same_source_path_fails_closed() -> None:
    rows = [
        _row(
            1,
            kind="chapter",
            source_order=1,
            article_ref="Глава I",
            title="Одна область",
        ),
        _row(
            2,
            kind="article",
            source_order=2,
            article_ref="Статья 1",
            full_text="Статья 1\nОдинаковое правило",
        ),
        _row(
            3,
            kind="article",
            source_order=3,
            article_ref="Статья 2",
            full_text="Статья 2\nОдинаковое правило",
        ),
    ]

    with pytest.raises(ExactDuplicateScopeError, match="article_ids"):
        build_exact_duplicate_plan(rows)


def test_section_resets_preceding_chapter() -> None:
    body = "Одинаковое правило для разных структур."
    rows = [
        _row(
            1,
            kind="section",
            source_order=1,
            article_ref="Раздел I",
            title="Первая территория",
        ),
        _row(
            2,
            kind="chapter",
            source_order=2,
            article_ref="Глава I",
            title="Порядок",
        ),
        _row(
            3,
            kind="article",
            source_order=3,
            article_ref="Статья 1",
            full_text=f"Статья 1\n{body}",
        ),
        _row(
            4,
            kind="section",
            source_order=4,
            article_ref="Раздел II",
            title="Вторая территория",
        ),
        _row(
            5,
            kind="article",
            source_order=5,
            article_ref="Статья 2",
            full_text=f"Статья 2\n{body}",
        ),
    ]

    group = build_exact_duplicate_plan(rows).groups[0]

    assert group.scope_level == "structure"
    assert group.members[0].path.chapter is not None
    assert group.members[1].path.chapter is None
    assert {
        member.scope_text for member in group.members
    } == {"Первая территория / Порядок", "Вторая территория"}


def test_target_closure_adds_every_group_member() -> None:
    plan = build_exact_duplicate_plan(_sf_like_rows())

    closed = close_targets_over_exact_duplicate_groups(
        {101, 999},
        plan,
    )

    assert closed == frozenset({101, 201, 999})


def test_group_response_accepts_exact_mirrored_suffixes() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    items.append({"id": 999, "title": "unrelated"})

    proof = validate_exact_duplicate_group_response(
        group,
        items,
        canonicalize_suffix=lambda value: value.replace("лица", "лицо"),
    )

    assert proof.article_ids == group.article_ids
    assert proof.normalized_base_title == "предъявление документов"
    assert proof.normalized_synonym_suffixes == (
        "кто обязан предъявить документы",
        "проверка документов у лица",
    )


def test_group_response_rejects_missing_member() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)[:-1]

    with pytest.raises(
        ExactDuplicateResponseError,
        match="incomplete",
    ):
        validate_exact_duplicate_group_response(group, items)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda item: item.update(title=item["title"] + " иначе"),
            "base titles",
        ),
        (
            lambda item: item["synonyms"].append(
                item["synonyms"][0].split("] ", 1)[0]
                + "] дополнительный запрос"
            ),
            "synonym suffixes",
        ),
        (
            lambda item: item.update(penalty_summary="Другая санкция"),
            "penalty_summary",
        ),
        (
            lambda item: item.update(penalty={"type": "fine"}),
            "penalty",
        ),
    ],
)
def test_group_response_rejects_asymmetric_content(
    mutate,
    message: str,
) -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    mutate(items[1])

    with pytest.raises(ExactDuplicateResponseError, match=message):
        validate_exact_duplicate_group_response(group, items)


def test_group_response_requires_exact_prefix_on_every_term() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    items[1]["synonyms"][0] = "запрос без области"

    with pytest.raises(ExactDuplicateResponseError, match="exact scope prefix"):
        validate_exact_duplicate_group_response(group, items)


def test_group_response_uses_source_case_only_for_generated_title() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    member = group.members[0]
    assert member.title_scope_prefix != member.synonym_scope_prefix

    items[0]["title"] = (
        f"{member.synonym_scope_prefix}Предъявление документов"
    )
    with pytest.raises(
        ExactDuplicateResponseError,
        match="exact scope prefix",
    ):
        validate_exact_duplicate_group_response(group, items)


def test_group_response_uses_storage_prefix_only_for_synonyms() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    member = group.members[0]
    items[0]["synonyms"][0] = (
        f"{member.title_scope_prefix}кто обязан предъявить документы"
    )

    with pytest.raises(
        ExactDuplicateResponseError,
        match="exact scope prefix",
    ):
        validate_exact_duplicate_group_response(group, items)


def test_group_response_preserves_distinct_source_owned_titles() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    source_titles = {
        group.members[0].article_id: "Предъявление документов на территории",
        group.members[1].article_id: "Проверка документов на объекте",
    }
    for item in items:
        item["title"] = source_titles[item["id"]]

    proof = validate_exact_duplicate_group_response(
        group,
        items,
        source_titles=source_titles,
    )

    assert proof.normalized_base_title is None


def test_group_response_rejects_changed_source_owned_title() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    source_titles = {
        member.article_id: f"Исходный заголовок {index}"
        for index, member in enumerate(group.members, start=1)
    }
    for item in items:
        item["title"] = source_titles[item["id"]]
    items[1]["title"] = str(items[1]["title"]).lower()

    with pytest.raises(ExactDuplicateResponseError, match="changed its source"):
        validate_exact_duplicate_group_response(
            group,
            items,
            source_titles=source_titles,
        )


def test_group_response_rejects_incomplete_source_title_mapping() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)

    with pytest.raises(
        ExactDuplicateResponseError,
        match="source_titles coverage is not exact",
    ):
        validate_exact_duplicate_group_response(
            group,
            items,
            source_titles={
                group.members[0].article_id: "Исходный заголовок",
            },
        )


def test_group_response_supports_mixed_source_and_generated_titles() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    source_member, generated_member = group.members
    source_title = "Предъявление документов"
    items[0]["title"] = source_title

    proof = validate_exact_duplicate_group_response(
        group,
        items,
        source_titles={
            source_member.article_id: source_title,
            generated_member.article_id: None,
        },
    )

    assert proof.normalized_base_title == "предъявление документов"


def test_group_response_rejects_generated_title_without_source_anchor() -> None:
    group = build_exact_duplicate_plan(_sf_like_rows()).groups[0]
    items = _valid_group_items(group)
    source_member, generated_member = group.members
    source_title = "Сохранённый заголовок источника"
    items[0]["title"] = source_title

    with pytest.raises(
        ExactDuplicateResponseError,
        match="does not match.*source-title anchor",
    ):
        validate_exact_duplicate_group_response(
            group,
            items,
            source_titles={
                source_member.article_id: source_title,
                generated_member.article_id: None,
            },
        )


def test_group_response_rejects_conflicting_source_anchors_with_generated() -> None:
    body = "Лицо обязано предъявить документы.\nСанкция отсутствует."
    rows = _sf_like_rows() + [
        _row(
            300,
            kind="chapter",
            source_order=7,
            article_ref="Глава V",
            title="Особо охраняемые территории",
        ),
        _row(
            301,
            kind="article",
            source_order=8,
            article_ref="Статья 5.1",
            full_text=f"Статья 5.1\n{body}",
        ),
    ]
    group = build_exact_duplicate_plan(rows).groups[0]
    assert group.article_ids == (101, 201, 301)
    items = _valid_group_items(group)
    first_source = "Предъявление документов"
    second_source = "Проверка документов"
    items[0]["title"] = first_source
    items[1]["title"] = second_source

    with pytest.raises(
        ExactDuplicateResponseError,
        match="conflicting source-title anchors",
    ):
        validate_exact_duplicate_group_response(
            group,
            items,
            source_titles={
                group.members[0].article_id: first_source,
                group.members[1].article_id: second_source,
                group.members[2].article_id: None,
            },
        )
