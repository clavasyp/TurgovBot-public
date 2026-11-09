"""Structural identity handoff: keeping article ids when a law is re-posted edited.

Byte equality only proves an unchanged re-post. Servers regularly publish an EDITED law
as a brand new thread (Memphis lost 20 of 32 laws that way): the old thread disappears,
content differs, and the strict path can prove nothing — so the document was deleted with
its articles, and with them the public article ids Overlay favourites point at, the
accumulated auto synonyms and penalty data.

These tests pin the safer behaviour and, just as importantly, the refusals.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import services.law_document_handoff as handoff_service
from services.law_document_handoff import handoff_exact_thread_replacements
from services.law_document_handoff import reconcile_missing_thread_documents

from tests.test_law_document_handoff import FakeConnection, _document, _warning_codes


def _law_text(codes, *, marker="исходная редакция"):
    """Law body with the given article numbers (skeleton the structural path reads)."""
    parts = [
        f"Преамбула. {marker}. Настоящий закон устанавливает порядок применения "
        "ответственности, гарантии участников и правила исполнения решений."
    ]
    for code in codes:
        parts.append(
            f"Статья {code}.\nНорма {code}\nч. 1 {marker}: положение статьи {code} "
            "определяет права, обязанности и последствия нарушения требований закона."
        )
    return "\n".join(parts)


CODES = ["1", "2", "3", "4", "5", "6", "7", "8"]


@pytest.mark.asyncio
async def test_edited_repost_keeps_article_identity():
    # Тот же закон, перезалитый новой темой с правками: пара статей изменена,
    # одна добавлена — скелет прежний, значит это та же норма.
    old_content = _law_text(CODES)
    new_content = _law_text(CODES + ["9"], marker="редакция от 2026 года")
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, old_content),
            _document(20, "memphis", 202, new_content),
        ],
        articles={501: 10, 502: 10},
        chunks={601: 10},
    )

    result = await handoff_exact_thread_replacements("memphis", {101}, {202}, conn=conn)

    assert result.mutated is True
    assert len(result.handoffs) == 1
    # Публичные id статей сохранены и переехали на новый документ
    assert conn.articles == {501: 20, 502: 20}
    assert conn.chunks == {601: 20}
    assert 10 not in conn.documents
    assert conn.documents[20]["derived_sync_pending"] is True


@pytest.mark.asyncio
async def test_unrelated_law_with_same_title_is_refused():
    # Заголовок совпал, но статьи совсем другие — это не та же норма.
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, _law_text(CODES)),
            _document(20, "memphis", 202, _law_text(["40", "41", "42", "43", "44", "45"])),
        ],
        articles={501: 10},
        chunks={601: 10},
    )

    result = await handoff_exact_thread_replacements("memphis", {101}, {202}, conn=conn)

    assert result.mutated is False
    assert conn.articles == {501: 10}
    assert "old_document_has_no_exact_current_match" in _warning_codes(result)


@pytest.mark.asyncio
async def test_thin_skeleton_is_never_enough_evidence():
    # Два-три совпавших номера встречаются у любых законов — доказательством не считаем.
    thin = ["1", "2", "3"]
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, _law_text(thin)),
            _document(20, "memphis", 202, _law_text(thin, marker="другая редакция")),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements("memphis", {101}, {202}, conn=conn)

    assert result.mutated is False
    assert conn.articles == {501: 10}


@pytest.mark.asyncio
async def test_duplicate_titles_are_refused_on_either_side():
    # Заголовок обязан быть уникальным якорем: два кандидата — отказ.
    content = _law_text(CODES)
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content),
            _document(20, "memphis", 202, _law_text(CODES, marker="копия A")),
            _document(30, "memphis", 303, _law_text(CODES, marker="копия B")),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "memphis", {101}, {202, 303}, conn=conn
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}


@pytest.mark.asyncio
async def test_different_titles_are_refused_even_with_same_skeleton():
    # Номера статей у кодексов повторяются, поэтому без совпадения названия не переносим.
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, _law_text(CODES), title="Уголовный кодекс"),
            _document(20, "memphis", 202, _law_text(CODES), title="Трудовой кодекс"),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements("memphis", {101}, {202}, conn=conn)

    assert result.mutated is False
    assert conn.articles == {501: 10}


@pytest.mark.asyncio
async def test_target_with_existing_dependents_is_not_merged():
    # Цель уже проиндексирована — слияние двух идентичностей намеренно не делаем.
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, _law_text(CODES)),
            _document(20, "memphis", 202, _law_text(CODES + ["9"], marker="новая")),
        ],
        articles={501: 10, 777: 20},
    )

    result = await handoff_exact_thread_replacements("memphis", {101}, {202}, conn=conn)

    assert result.mutated is False
    assert conn.articles == {501: 10, 777: 20}
    assert "current_document_has_dependents" in _warning_codes(result)


@pytest.mark.asyncio
async def test_structural_path_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(handoff_service, "_STRUCTURAL_HANDOFF_ENABLED", False)
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, _law_text(CODES)),
            _document(20, "memphis", 202, _law_text(CODES + ["9"], marker="новая")),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements("memphis", {101}, {202}, conn=conn)

    assert result.mutated is False
    assert conn.articles == {501: 10}
    assert "old_document_has_no_exact_current_match" in _warning_codes(result)


@pytest.mark.asyncio
async def test_two_missing_documents_cannot_claim_one_replacement():
    # Один и тот же заголовок у двух исчезнувших тем: цель одна, выбор небезопасен.
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, _law_text(CODES, marker="ред. A")),
            _document(11, "memphis", 102, _law_text(CODES, marker="ред. B")),
            _document(20, "memphis", 202, _law_text(CODES + ["9"], marker="новая")),
        ],
        articles={501: 10, 502: 11},
    )

    result = await handoff_exact_thread_replacements(
        "memphis", {101, 102}, {202}, conn=conn
    )

    assert result.mutated is False
    assert conn.articles == {501: 10, 502: 11}


def test_similarity_thresholds():
    strong, retention, jaccard = handoff_service._structural_similarity(
        frozenset({"1", "2", "3", "4", "5", "6", "7", "8"}),
        frozenset({"1", "2", "3", "4", "5", "6", "7", "8", "9"}),
    )
    assert strong is True and retention == 1.0 and jaccard >= 0.6

    # Половина статей исчезла — это уже другой документ
    weak, retention, _ = handoff_service._structural_similarity(
        frozenset({"1", "2", "3", "4", "5", "6", "7", "8"}),
        frozenset({"1", "2", "3", "4", "20", "21", "22", "23"}),
    )
    assert weak is False and retention < 0.7

    # Скелет короче минимума — не доказательство
    thin, _, _ = handoff_service._structural_similarity(
        frozenset({"1", "2", "3"}), frozenset({"1", "2", "3"})
    )
    assert thin is False


def test_structural_codes_normalise_hyphen_subnumbering():
    codes = handoff_service._structural_codes(_law_text(["10", "10-1", "10-2"]))
    assert {"10", "10.1", "10.2"} <= codes


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("10.6; 12.1; ... 17.2 УК штата SA.", "10.6"),
        ("100.000$.", "100.000"),
        ("70.000$", "70.000"),
        ("5.000$", "5.000"),
        ("20.000$", "20.000"),
        ("4.2. Норма", "4.2"),
    ],
)
def test_parser_approved_bare_ref_keeps_punctuation_suffix(ref, expected):
    match = handoff_service._BARE_ARTICLE_REF_RE.match(ref)
    assert match is not None
    assert match.group("code") == expected


@pytest.mark.parametrize(
    "ref",
    [
        "1.2.3",
        "1.23.4",
        "12.34.5",
        "10.60.1",
        "1.234.56",
        "10.6.1 Заголовок",
    ],
)
def test_parser_approved_bare_ref_rejects_third_numeric_level(ref):
    assert handoff_service._BARE_ARTICLE_REF_RE.match(ref) is None


def _section_primary_text(
    *,
    chapters: int,
    section_codes,
    marker: str,
):
    """Source-shaped constitution: chapters and sections, but no articles."""

    codes = list(section_codes)
    parts = [
        (
            f"Преамбула. {marker}. Конституционный акт закрепляет основы "
            "государственного устройства, права жителей, полномочия органов "
            "власти и гарантии исполнения настоящего нормативного документа."
        )
    ]
    for chapter in range(1, chapters + 1):
        parts.append(f"Глава {chapter}. Основы главы {chapter}")
        start = (chapter - 1) * len(codes) // chapters
        end = chapter * len(codes) // chapters
        for code in codes[start:end]:
            parts.append(
                f"Раздел {code}. Положение {code}\n"
                f"{marker}. Раздел {code} определяет полномочия, обязанности, "
                "правовые гарантии, порядок их осуществления и ответственность "
                "за нарушение установленных конституционных требований."
            )
    return "\n".join(parts)


def _chapter_local_section_primary_text(*, sections_by_chapter, marker: str):
    """Section-primary layout whose section numbering restarts per chapter."""

    roman_chapters = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII")
    parts = [
        (
            f"Преамбула. {marker}. Конституционный акт закрепляет основы "
            "государственного устройства, права жителей, полномочия органов "
            "власти и гарантии исполнения настоящего нормативного документа."
        )
    ]
    for chapter, section_codes in enumerate(sections_by_chapter, start=1):
        chapter_code = roman_chapters[chapter - 1]
        parts.append(f"Глава {chapter_code}. Основы главы {chapter_code}")
        for code in section_codes:
            parts.append(
                f"Раздел {code}. Положение {chapter}.{code}\n"
                f"{marker}. Раздел {chapter}.{code} определяет полномочия, "
                "обязанности, гарантии, порядок осуществления прав и "
                "ответственность за нарушение конституционных требований."
            )
    return "\n".join(parts)


def _bare_nm_law_text(chapters, *, marker: str):
    """Law whose parser-approved article refs are bare two-level N.M."""

    legacy_false_headers = {
        "10.6": "10.6; 12.1; ... 17.2 УК штата SA.",
        "100.000": "100.000$.",
        "70.000": "70.000$",
        "5.000": "5.000$",
        "20.000": "20.000$",
    }
    parts = [
        (
            f"Преамбула. {marker}. Настоящий закон регулирует лицензирование, "
            "хранение, использование, передачу, контроль и ответственность "
            "участников регулируемой деятельности."
        )
    ]
    for chapter, codes in chapters:
        parts.append(f"Глава {chapter}. Регулирование главы {chapter}")
        for code in codes:
            ref = legacy_false_headers.get(code, f"{code}. Норма {code}")
            parts.append(
                f"{ref}\n"
                f"{marker}. Положение {code} устанавливает права, обязанности, "
                "условия применения, контроль уполномоченных органов и последствия "
                "нарушения требований настоящего закона."
            )
    return "\n".join(parts)


def _source_shaped_old_weapon_chapters():
    return [
        (1, [f"1.{number}" for number in range(1, 11)]),
        # The legacy source includes false bare candidates from one clause and
        # four money lines.  Those mismatches prevent true-N.M classification
        # and leave the historical broad ``bare`` fallback.
        (
            2,
            [
                "2.1",
                "2.2",
                "2.3",
                "2.4",
                "10.6",
                "2.5",
                "100.000",
                "70.000",
                "5.000",
                "20.000",
                "2.6",
                "2.7",
            ],
        ),
        (3, ["3.1", "3.2", "3.3"]),
    ]


def _source_shaped_new_weapon_chapters():
    # Exact audited margin: 20/25 old unique codes survive; the new source has
    # 41 article rows / 40 unique codes because 4.2 appears twice.
    return [
        (1, [f"1.{number}" for number in range(1, 14)]),
        (
            2,
            [f"2.{number}" for number in range(1, 8)]
            + ["2.9", "2.10"],
        ),
        (3, [f"3.{number}" for number in range(1, 14)]),
        (
            4,
            ["4.1", "4.2", "4.2", "4.3", "4.4", "4.5"],
        ),
    ]


@pytest.mark.asyncio
async def test_exact_title_section_primary_repost_keeps_identity():
    # Source-derived shape: old constitution 7 chapters / 79 sections, new
    # edition 8 chapters / 73 sections, and neither has article markers.
    old_content = _chapter_local_section_primary_text(
        sections_by_chapter=[
            range(1, 17),
            range(1, 11),
            [1, 2, 3, 5, 6, 7, 8, 9, 10],
            range(1, 21),
            range(1, 9),
            range(1, 12),
            range(1, 6),
        ],
        marker="редакция до конституционной реформы",
    )
    new_content = _chapter_local_section_primary_text(
        sections_by_chapter=[
            range(1, 13),
            range(1, 17),
            range(1, 12),
            range(1, 12),
            range(1, 10),
            range(1, 8),
            range(1, 5),
            range(1, 4),
        ],
        marker="редакция после конституционной реформы",
    )
    old_skeleton = handoff_service._typed_structural_skeleton(old_content)
    new_skeleton = handoff_service._typed_structural_skeleton(new_content)
    assert sum(kind == "chapter" for kind, _code, _bare in old_skeleton.markers) == 7
    assert sum(kind == "section" for kind, _code, _bare in old_skeleton.markers) == 79
    assert sum(kind == "chapter" for kind, _code, _bare in new_skeleton.markers) == 8
    assert sum(kind == "section" for kind, _code, _bare in new_skeleton.markers) == 73
    strong, retention, jaccard, shared, ordered = (
        handoff_service._section_primary_similarity(
            old_skeleton,
            new_skeleton,
        )
    )
    assert strong is True and ordered is True and shared == 61
    assert retention == pytest.approx(61 / 79)
    assert jaccard == pytest.approx(61 / (79 + 73 - 61))

    conn = FakeConnection(
        documents=[
            _document(
                10,
                "san_francisco",
                101,
                old_content,
                title="Конституция штата SA",
            ),
            _document(
                20,
                "san_francisco",
                202,
                new_content,
                title="  КОНСТИТУЦИЯ   штата SA  ",
            ),
        ],
        articles={501: 10, 502: 10},
    )

    result = await handoff_exact_thread_replacements(
        "san_francisco",
        {101},
        {202},
        conn=conn,
    )

    assert result.mutated is True
    assert conn.articles == {501: 20, 502: 20}
    assert conn.documents[20]["derived_sync_pending"] is True


@pytest.mark.asyncio
async def test_quoted_weapon_law_jurisdiction_prefix_and_bare_rewrite_keep_identity():
    # Source-derived shape: 25 old bare provisions, 41 in the new true-N.M
    # layout, with 20 old codes retained in the same order.
    old_content = _bare_nm_law_text(
        _source_shaped_old_weapon_chapters(),
        marker="предыдущая редакция оружейного закона",
    )
    new_content = _bare_nm_law_text(
        _source_shaped_new_weapon_chapters(),
        marker="новая редакция оружейного закона",
    )
    old_skeleton = handoff_service._typed_structural_skeleton(old_content)
    new_skeleton = handoff_service._typed_structural_skeleton(new_content)
    assert old_skeleton.fallback_mode == "bare"
    assert old_skeleton.article_dialect_status == "not_applicable"
    assert new_skeleton.fallback_mode == "article_dialect_true_nm"
    assert new_skeleton.article_dialect_status == "resolved"
    assert new_skeleton.article_dialect_mode == "true_nm"
    assert sum(kind == "article" for kind, _code, _bare in old_skeleton.markers) == 25
    assert sum(kind == "article" for kind, _code, _bare in new_skeleton.markers) == 41
    assert all(
        is_bare
        for kind, _code, is_bare in old_skeleton.markers + new_skeleton.markers
        if kind == "article"
    )
    strong, retention, jaccard, shared, ordered = (
        handoff_service._quoted_bare_article_similarity(
            old_skeleton,
            new_skeleton,
        )
    )
    assert strong is True and ordered is True and shared == 20
    assert retention == pytest.approx(20 / 25)
    assert jaccard == pytest.approx(20 / (25 + 40 - 20))

    subject = "Об обороте оружия и государственных специальных средств"
    conn = FakeConnection(
        documents=[
            _document(
                10,
                "san_francisco",
                101,
                old_content,
                title=f'Закон "{subject}"',
            ),
            _document(
                20,
                "san_francisco",
                202,
                new_content,
                title=f'Закон штата SA "{subject}"',
            ),
        ],
        articles={501: 10, 502: 10},
        chunks={601: 10},
    )

    result = await handoff_exact_thread_replacements(
        "san_francisco",
        {101},
        {202},
        conn=conn,
    )

    assert result.mutated is True
    assert conn.articles == {501: 20, 502: 20}
    assert conn.chunks == {601: 20}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_title", "new_title"),
    [
        (
            'Закон "Общие положения"',
            'Закон штата SA "Общие положения"',
        ),
        (
            "Закон об обороте оружия и государственных специальных средств",
            "Закон штата SA об обороте оружия и государственных специальных средств",
        ),
        (
            'Закон "Об обороте оружия и государственных специальных средств"',
            'Закон штата SA "О регулировании охранной деятельности и специальных средств"',
        ),
        (
            'Положение "Об обороте оружия и государственных специальных средств"',
            'Положение штата SA "Об обороте оружия и государственных специальных средств"',
        ),
        (
            'Закон штата A "Об обороте оружия и государственных специальных средств"',
            'Закон города B "Об обороте оружия и государственных специальных средств"',
        ),
        (
            'Закон штата SA "Об обороте оружия и государственных специальных средств"',
            'Закон "Об обороте оружия и государственных специальных средств"',
        ),
        (
            'Закон "О внесении изменений в правила оборота оружия"',
            'Закон штата SA "О внесении изменений в правила оборота оружия"',
        ),
        (
            'Закон "Об обороте оружия и государственных специальных средств"',
            'Закон штата SA отмененный "Об обороте оружия и государственных специальных средств"',
        ),
    ],
)
async def test_quoted_title_proof_rejects_generic_malformed_or_changed_subjects(
    old_title,
    new_title,
):
    old_content = _bare_nm_law_text(
        _source_shaped_old_weapon_chapters(),
        marker="старая редакция",
    )
    new_content = _bare_nm_law_text(
        _source_shaped_new_weapon_chapters(),
        marker="новая редакция",
    )
    conn = FakeConnection(
        documents=[
            _document(10, "test", 101, old_content, title=old_title),
            _document(20, "test", 202, new_content, title=new_title),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "test",
        {101},
        {202},
        conn=conn,
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}


@pytest.mark.asyncio
async def test_reconcile_rolls_back_instead_of_deleting_unsafe_replacement(
    monkeypatch,
):
    import services.indexing_service as indexing_service

    monkeypatch.setattr(
        indexing_service,
        "_header_match_plan",
        lambda _text: SimpleNamespace(
            article_dialect_status="ambiguous",
            recovery_unresolved_bare_candidates=(),
        ),
    )
    conn = FakeConnection(
        documents=[
            _document(
                10,
                "test",
                101,
                _law_text(CODES),
                title="Один закон",
                gcs_uri="gs://laws/old.txt",
            ),
            _document(
                20,
                "test",
                202,
                _law_text(CODES + ["9"], marker="новая редакция"),
                title="Один закон",
                gcs_uri="gs://laws/new.txt",
            ),
        ],
        articles={501: 10},
        chunks={601: 10},
        in_transaction=False,
    )

    with pytest.raises(
        handoff_service.ExactThreadHandoffInvariantError,
        match="structural_candidate_parser_unsafe",
    ):
        async with conn.transaction():
            await reconcile_missing_thread_documents(
                conn,
                server_id="test",
                missing_thread_ids={101},
                current_thread_ids={202},
            )

    assert set(conn.documents) == {10, 20}
    assert conn.articles == {501: 10}
    assert conn.chunks == {601: 10}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["one_to_two", "two_to_one"])
async def test_reconcile_blocks_ambiguous_structural_title_multiplicity(shape):
    old_documents = [
        _document(
            10,
            "test",
            101,
            _law_text(CODES, marker="старая редакция A"),
            title="Один кодекс",
        )
    ]
    current_documents = [
        _document(
            30,
            "test",
            303,
            _law_text(CODES + ["9"], marker="новая редакция A"),
            title="Один кодекс",
        )
    ]
    if shape == "one_to_two":
        current_documents.append(
            _document(
                40,
                "test",
                404,
                _law_text(CODES + ["9"], marker="новая редакция B"),
                title="Один кодекс",
            )
        )
    else:
        old_documents.append(
            _document(
                20,
                "test",
                202,
                _law_text(CODES, marker="старая редакция B"),
                title="Один кодекс",
            )
        )

    articles = {
        501 + index: document["id"]
        for index, document in enumerate(old_documents)
    }
    conn = FakeConnection(
        documents=[*old_documents, *current_documents],
        articles=articles,
        in_transaction=False,
    )

    with pytest.raises(
        handoff_service.ExactThreadHandoffInvariantError,
        match="ambiguous_structural_match",
    ):
        async with conn.transaction():
            await reconcile_missing_thread_documents(
                conn,
                server_id="test",
                missing_thread_ids={
                    int(document["source_thread_id"])
                    for document in old_documents
                },
                current_thread_ids={
                    int(document["source_thread_id"])
                    for document in current_documents
                },
            )

    assert set(conn.documents) == {
        int(document["id"])
        for document in [*old_documents, *current_documents]
    }
    assert conn.articles == articles


@pytest.mark.asyncio
async def test_quoted_title_proof_rejects_duplicate_subject_candidates():
    old_content = _bare_nm_law_text(
        _source_shaped_old_weapon_chapters(),
        marker="старая редакция",
    )
    new_content = _bare_nm_law_text(
        _source_shaped_new_weapon_chapters(),
        marker="новая редакция",
    )
    subject = "Об обороте оружия и государственных специальных средств"
    conn = FakeConnection(
        documents=[
            _document(10, "test", 101, old_content, title=f'Закон "{subject}"'),
            _document(
                20,
                "test",
                202,
                new_content,
                title=f'Закон штата SA "{subject}"',
            ),
            _document(
                30,
                "test",
                303,
                new_content.replace("новая редакция", "конкурирующая редакция"),
                title=f'Закон области LS "{subject}"',
            ),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "test",
        {101},
        {202, 303},
        conn=conn,
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}
    assert "ambiguous_structural_match" in _warning_codes(result)


@pytest.mark.asyncio
async def test_exact_title_candidate_blocks_a_different_quoted_target():
    old_content = _bare_nm_law_text(
        _source_shaped_old_weapon_chapters(),
        marker="старая редакция",
    )
    replacement_content = _bare_nm_law_text(
        _source_shaped_new_weapon_chapters(),
        marker="настоящая новая редакция",
    )
    weak_exact_content = _bare_nm_law_text(
        [(1, ["1.1", "1.2"]), (2, ["2.1"]), (3, ["3.1"])],
        marker="другой короткий документ",
    )
    subject = "Об обороте оружия и государственных специальных средств"
    conn = FakeConnection(
        documents=[
            _document(10, "test", 101, old_content, title=f'Закон "{subject}"'),
            _document(
                20,
                "test",
                202,
                weak_exact_content,
                title=f'Закон "{subject}"',
            ),
            _document(
                30,
                "test",
                303,
                replacement_content,
                title=f'Закон штата SA "{subject}"',
            ),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "test",
        {101},
        {202, 303},
        conn=conn,
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}


@pytest.mark.asyncio
async def test_quoted_title_proof_rejects_short_bare_skeleton():
    old_content = _bare_nm_law_text(
        [(1, [f"1.{number}" for number in range(1, 7)])],
        marker="старая редакция с достаточным объёмом текста",
    )
    new_content = _bare_nm_law_text(
        [(1, [f"1.{number}" for number in range(1, 8)])],
        marker="новая редакция с достаточным объёмом текста",
    )
    subject = "Об обороте оружия и государственных специальных средств"
    conn = FakeConnection(
        documents=[
            _document(10, "test", 101, old_content, title=f'Закон "{subject}"'),
            _document(
                20,
                "test",
                202,
                new_content,
                title=f'Закон штата SA "{subject}"',
            ),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "test",
        {101},
        {202},
        conn=conn,
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}


@pytest.mark.asyncio
async def test_section_primary_proof_rejects_cross_kind_structure():
    old_content = _section_primary_text(
        chapters=4,
        section_codes=range(1, 21),
        marker="старая редакция",
    )
    new_content = _law_text(
        [str(code) for code in range(1, 21)],
        marker="новая редакция с теми же числами другого типа",
    )
    conn = FakeConnection(
        documents=[
            _document(10, "test", 101, old_content, title="Конституция"),
            _document(20, "test", 202, new_content, title="Конституция"),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "test",
        {101},
        {202},
        conn=conn,
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["reordered", "unrelated", "title_collision"])
async def test_section_primary_proof_rejects_ambiguous_or_unrelated_shapes(variant):
    old_content = _section_primary_text(
        chapters=7,
        section_codes=range(1, 80),
        marker="старая редакция",
    )
    if variant == "reordered":
        current_documents = [
            _document(
                20,
                "test",
                202,
                _section_primary_text(
                    chapters=8,
                    section_codes=reversed(range(1, 74)),
                    marker="переставленный документ",
                ),
                title="Конституция штата SA",
            )
        ]
    elif variant == "unrelated":
        current_documents = [
            _document(
                20,
                "test",
                202,
                _section_primary_text(
                    chapters=8,
                    section_codes=range(201, 274),
                    marker="другой документ",
                ),
                title="Конституция штата SA",
            )
        ]
    else:
        current_documents = [
            _document(
                20,
                "test",
                202,
                _section_primary_text(
                    chapters=8,
                    section_codes=range(1, 74),
                    marker="кандидат A",
                ),
                title="Конституция штата SA",
            ),
            _document(
                30,
                "test",
                303,
                _section_primary_text(
                    chapters=8,
                    section_codes=range(1, 74),
                    marker="кандидат B",
                ),
                title="Конституция штата SA",
            ),
        ]

    conn = FakeConnection(
        documents=[
            _document(
                10,
                "test",
                101,
                old_content,
                title="Конституция штата SA",
            ),
            *current_documents,
        ],
        articles={501: 10},
    )
    result = await handoff_exact_thread_replacements(
        "test",
        {101},
        {int(row["source_thread_id"]) for row in current_documents},
        conn=conn,
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}


def test_quoted_title_anchor_rejects_multiple_quotes_and_non_neutral_text():
    subject = "Об обороте оружия и государственных специальных средств"
    assert handoff_service._quoted_legal_title_anchor(
        f'Закон "{subject}" "О приложении к закону"'
    ) is None
    assert handoff_service._quoted_legal_title_anchor(
        f'Новый закон штата SA "{subject}"'
    ) is None
    assert handoff_service._quoted_legal_title_anchor(
        f'Закон "{subject} «приложение»"'
    ) is None


def test_section_similarity_keeps_repeated_codes_owned_by_their_chapter():
    def skeleton(chapter_sections):
        markers = []
        for chapter, sections in chapter_sections:
            markers.append(("chapter", chapter, False))
            markers.extend(
                ("section", str(code), False)
                for code in sections
            )
        return handoff_service._TypedStructuralSkeleton(markers=tuple(markers))

    old = skeleton(
        [
            ("I", range(1, 11)),
            ("II", range(11, 21)),
            ("III", range(21, 31)),
        ]
    )
    reassigned = skeleton(
        [
            ("I", range(11, 21)),
            ("II", range(21, 31)),
            ("III", range(1, 11)),
        ]
    )

    strong, _retention, _jaccard, shared, _ordered = (
        handoff_service._section_primary_similarity(old, reassigned)
    )

    assert shared == 0
    assert strong is False


def test_bare_similarity_rejects_reordered_shared_articles():
    old_markers = []
    new_markers = []
    for chapter in range(1, 4):
        chapter_marker = ("chapter", str(chapter), False)
        articles = [
            ("article", f"{chapter}.{number}", True)
            for number in range(1, 6)
        ]
        old_markers.extend([chapter_marker, *articles])
        new_markers.extend([chapter_marker, *reversed(articles)])
    old = handoff_service._TypedStructuralSkeleton(
        markers=tuple(old_markers),
        fallback_mode="bare",
        article_dialect_status="not_applicable",
    )
    new = handoff_service._TypedStructuralSkeleton(
        markers=tuple(new_markers),
        fallback_mode="article_dialect_true_nm",
        article_dialect_status="resolved",
        article_dialect_mode="true_nm",
    )

    strong, _retention, _jaccard, shared, ordered = (
        handoff_service._quoted_bare_article_similarity(old, new)
    )

    assert shared == 15
    assert ordered is False
    assert strong is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dialect_status", "unresolved"),
    [
        ("ambiguous", ()),
        ("resolved", (object(),)),
    ],
)
async def test_structural_handoff_rejects_unsafe_shared_parser_plan(
    monkeypatch,
    dialect_status,
    unresolved,
):
    import services.indexing_service as indexing_service

    monkeypatch.setattr(
        indexing_service,
        "_header_match_plan",
        lambda _text: SimpleNamespace(
            article_dialect_status=dialect_status,
            recovery_unresolved_bare_candidates=unresolved,
        ),
    )
    conn = FakeConnection(
        documents=[
            _document(10, "test", 101, _law_text(CODES), title="Один закон"),
            _document(
                20,
                "test",
                202,
                _law_text(CODES + ["9"], marker="новая редакция"),
                title="Один закон",
            ),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "test",
        {101},
        {202},
        conn=conn,
    )

    assert result.mutated is False
    assert conn.articles == {501: 10}
    assert "structural_candidate_parser_unsafe" in _warning_codes(result)
