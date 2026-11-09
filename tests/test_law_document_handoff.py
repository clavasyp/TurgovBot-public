"""Unit coverage for exact-only cross-thread document identity handoff."""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager

import pytest

import services.law_document_handoff as handoff_service
from services.law_document_handoff import (
    handoff_exact_thread_replacements,
    reconcile_missing_thread_documents,
)


class FakeConnection:
    def __init__(
        self,
        *,
        documents,
        articles=None,
        chunks=None,
        article_servers=None,
        chunk_servers=None,
        in_transaction=True,
        bulk_delete_thread_ids=None,
    ):
        self.documents = {row["id"]: dict(row) for row in documents}
        self.articles = dict(articles or {})
        self.chunks = dict(chunks or {})
        self.article_servers = dict(article_servers or {})
        self.chunk_servers = dict(chunk_servers or {})
        self._in_transaction = in_transaction
        self.bulk_delete_thread_ids = (
            None
            if bulk_delete_thread_ids is None
            else set(bulk_delete_thread_ids)
        )
        self.article_dependents = {
            9001: 501,
            9002: 502,
        }
        self.fetch_queries = []
        self.mutation_queries = []

    def is_in_transaction(self):
        return self._in_transaction

    @asynccontextmanager
    async def transaction(self):
        documents_before = copy.deepcopy(self.documents)
        articles_before = dict(self.articles)
        chunks_before = dict(self.chunks)
        was_in_transaction = self._in_transaction
        self._in_transaction = True
        try:
            yield self
        except Exception:
            self.documents = documents_before
            self.articles = articles_before
            self.chunks = chunks_before
            raise
        finally:
            self._in_transaction = was_in_transaction

    async def fetch(self, query, *args):
        self.fetch_queries.append((query, args))
        if "DELETE FROM law_documents" in query:
            server_id, thread_ids = args
            requested_ids = set(thread_ids)
            if self.bulk_delete_thread_ids is not None:
                requested_ids &= self.bulk_delete_thread_ids
            deleted = []
            for document_id, row in list(self.documents.items()):
                if (
                    row["server_id"] == server_id
                    and row["source_thread_id"] in requested_ids
                ):
                    deleted.append(
                        {
                            "id": document_id,
                            "source_thread_id": row["source_thread_id"],
                        }
                    )
                    del self.documents[document_id]
            self.mutation_queries.append((query, args))
            return sorted(deleted, key=lambda row: row["source_thread_id"])

        if "FROM law_documents" in query:
            server_id, thread_ids = args
            return sorted(
                (
                    dict(row)
                    for row in self.documents.values()
                    if row["server_id"] == server_id
                    and row["source_thread_id"] in set(thread_ids)
                ),
                key=lambda row: (row["source_thread_id"], row["id"]),
            )

        if "FROM law_articles" in query:
            document_ids = set(args[0])
            return [
                {
                    "id": row_id,
                    "law_document_id": document_id,
                    "server_id": self.article_servers.get(
                        row_id,
                        self.documents[document_id]["server_id"],
                    ),
                }
                for row_id, document_id in sorted(self.articles.items())
                if document_id in document_ids
            ]

        if "FROM law_chunks" in query:
            document_ids = set(args[0])
            return [
                {
                    "id": row_id,
                    "law_document_id": document_id,
                    "server_id": self.chunk_servers.get(
                        row_id,
                        self.documents[document_id]["server_id"],
                    ),
                }
                for row_id, document_id in sorted(self.chunks.items())
                if document_id in document_ids
            ]

        raise AssertionError(f"Unexpected fetch: {query!r} {args!r}")

    async def execute(self, query, *args):
        self.mutation_queries.append((query, args))
        if "UPDATE law_articles" in query:
            new_document_id, old_document_id = args
            updated = 0
            for row_id, document_id in list(self.articles.items()):
                if document_id == old_document_id:
                    self.articles[row_id] = new_document_id
                    updated += 1
            return f"UPDATE {updated}"

        if "UPDATE law_chunks" in query:
            new_document_id, old_document_id = args
            updated = 0
            for row_id, document_id in list(self.chunks.items()):
                if document_id == old_document_id:
                    self.chunks[row_id] = new_document_id
                    updated += 1
            return f"UPDATE {updated}"

        raise AssertionError(f"Unexpected execute: {query!r} {args!r}")

    async def fetchval(self, query, *args):
        if "SET derived_sync_pending = TRUE" in query:
            target_document_id, server_id, target_thread_id = args
            row = self.documents.get(target_document_id)
            if (
                row is None
                or row["server_id"] != server_id
                or row["source_thread_id"] != target_thread_id
            ):
                return None
            row["derived_sync_pending"] = True
            self.mutation_queries.append((query, args))
            return target_document_id

        if "DELETE FROM law_documents" not in query:
            raise AssertionError(f"Unexpected fetchval: {query!r} {args!r}")
        old_document_id, server_id, old_thread_id = args
        row = self.documents.get(old_document_id)
        if (
            row is None
            or row["server_id"] != server_id
            or row["source_thread_id"] != old_thread_id
        ):
            return None
        del self.documents[old_document_id]
        self.mutation_queries.append((query, args))
        return old_document_id


def _document(
    document_id,
    server_id,
    thread_id,
    content,
    content_hash="same-md5",
    title="Уголовный кодекс",
    gcs_uri=None,
):
    return {
        "id": document_id,
        "server_id": server_id,
        "source_thread_id": thread_id,
        "title": title,
        "content": content,
        "gemini_file_uri": gcs_uri,
        "derived_sync_pending": False,
        # The service must intentionally ignore this legacy/MD5-compatible field.
        "content_hash": content_hash,
    }


def _warning_codes(result):
    return {warning.code for warning in result.warnings}


def _law_content(marker="одинаковый"):
    paragraphs = [
        (
            f"Раздел {number}. {marker}. Настоящий нормативный документ устанавливает "
            "права, обязанности, порядок применения ответственности, основания "
            "производства, гарантии участников и правила исполнения решений."
        )
        for number in range(1, 12)
    ]
    return "\n".join(paragraphs)


@pytest.mark.asyncio
async def test_exact_handoff_moves_both_fk_sets_and_preserves_article_identity():
    content = _law_content()
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content, content_hash="old-md5"),
            _document(20, "memphis", 202, content, content_hash="new-md5"),
        ],
        articles={501: 10, 502: 10},
        chunks={601: 10, 602: 10},
    )
    article_ids_before = set(conn.articles)
    article_dependents_before = dict(conn.article_dependents)

    result = await handoff_exact_thread_replacements(
        "memphis",
        {101},
        {202},
        conn=conn,
    )

    assert result.mutated is True
    assert len(result.handoffs) == 1
    completed = result.handoffs[0]
    assert completed.old_document_id == 10
    assert completed.target_document_id == 20
    assert completed.old_thread_id == 101
    assert completed.target_thread_id == 202
    assert completed.article_count == 2
    assert completed.chunk_count == 2
    assert len(completed.content_sha256) == 64
    assert 10 not in conn.documents
    assert conn.documents[20]["derived_sync_pending"] is True
    assert 20 in conn.documents
    assert conn.articles == {501: 20, 502: 20}
    assert conn.chunks == {601: 20, 602: 20}
    assert set(conn.articles) == article_ids_before
    assert conn.article_dependents == article_dependents_before
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_legacy_hash_equality_cannot_replace_different_full_content():
    conn = FakeConnection(
        documents=[
            _document(
                10,
                "memphis",
                101,
                _law_content("старая редакция"),
                content_hash="same-md5",
            ),
            _document(
                20,
                "memphis",
                202,
                _law_content("новая редакция"),
                content_hash="same-md5",
            ),
        ],
        articles={501: 10},
        chunks={601: 10},
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    assert _warning_codes(result) == {
        "old_document_has_no_exact_current_match",
    }
    assert conn.articles == {501: 10}
    assert conn.chunks == {601: 10}
    assert set(conn.documents) == {10, 20}
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_direct_content_equality_is_checked_even_under_digest_collision(monkeypatch):
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, _law_content("старая редакция")),
            _document(20, "memphis", 202, _law_content("новая редакция")),
        ],
        articles={501: 10},
    )
    monkeypatch.setattr(handoff_service, "_sha256_text", lambda _content: "f" * 64)

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    assert "ambiguous_exact_match" not in _warning_codes(result)
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_duplicate_exact_content_is_ambiguous_and_never_mutated():
    content = _law_content("дублированный документ")
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content),
            _document(11, "memphis", 102, content),
            _document(20, "memphis", 202, content),
        ],
        articles={501: 10, 502: 11},
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101, 102],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    assert _warning_codes(result) == {"ambiguous_exact_match"}
    warning = result.warnings[0]
    assert warning.old_thread_ids == (101, 102)
    assert warning.current_thread_ids == (202,)
    assert conn.articles == {501: 10, 502: 11}
    assert conn.mutation_queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("articles", "chunks"),
    [
        ({501: 10, 777: 20}, {}),
        ({501: 10}, {888: 20}),
    ],
)
async def test_new_document_with_any_dependents_is_not_merged(articles, chunks):
    content = _law_content()
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content),
            _document(20, "memphis", 202, content),
        ],
        articles=articles,
        chunks=chunks,
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    assert _warning_codes(result) == {"current_document_has_dependents"}
    assert set(conn.documents) == {10, 20}
    assert conn.articles == articles
    assert conn.chunks == chunks
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_cross_server_document_is_not_a_candidate_even_with_exact_content():
    content = _law_content()
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content),
            _document(20, "denver", 202, content),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    assert "current_document_not_found" in _warning_codes(result)
    assert conn.articles == {501: 10}
    assert set(conn.documents) == {10, 20}
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_independent_safe_pair_can_move_while_ambiguous_pair_stays_untouched():
    unique_content = _law_content("уникальный документ")
    duplicate_content = _law_content("дублированный документ")
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, unique_content),
            _document(20, "memphis", 201, unique_content),
            _document(11, "memphis", 102, duplicate_content),
            _document(12, "memphis", 103, duplicate_content),
            _document(21, "memphis", 202, duplicate_content),
        ],
        articles={501: 10, 502: 11, 503: 12},
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101, 102, 103],
        [201, 202],
        conn=conn,
    )

    assert result.mutated is True
    assert [
        (item.old_thread_id, item.target_thread_id) for item in result.handoffs
    ] == [(101, 201)]
    assert _warning_codes(result) == {"ambiguous_exact_match"}
    assert conn.articles == {501: 20, 502: 11, 503: 12}
    assert set(conn.documents) == {11, 12, 20, 21}


@pytest.mark.asyncio
async def test_without_connection_uses_one_owned_transaction(monkeypatch):
    content = _law_content()
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content),
            _document(20, "memphis", 202, content),
        ]
    )

    class FakeDbManager:
        def __init__(self):
            self.entries = 0
            self.exits = 0

        @asynccontextmanager
        async def transaction(self):
            self.entries += 1
            try:
                yield conn
            finally:
                self.exits += 1

    fake_db = FakeDbManager()
    monkeypatch.setattr(handoff_service, "db_manager", fake_db)

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
    )

    assert result.mutated is True
    assert fake_db.entries == 1
    assert fake_db.exits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_content",
    [
        "",
        " \n\t ",
        "Статья 1. Слишком мало.",
        "ошибка " * 100,
    ],
)
async def test_empty_whitespace_or_short_content_never_becomes_identity_evidence(
    unsafe_content,
):
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, unsafe_content),
            _document(20, "memphis", 202, unsafe_content),
        ]
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    suffix = (
        "no_content"
        if not unsafe_content.strip()
        else "low_information_content"
    )
    assert _warning_codes(result) == {
        f"old_document_{suffix}",
        f"current_document_{suffix}",
    }
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_supplied_connection_without_active_transaction_fails_before_querying():
    content = _law_content()
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content),
            _document(20, "memphis", 202, content),
        ],
        in_transaction=False,
    )

    with pytest.raises(
        handoff_service.ExactThreadHandoffInvariantError,
        match="active database transaction",
    ):
        await handoff_exact_thread_replacements(
            "memphis",
            [101],
            [202],
            conn=conn,
        )

    assert conn.fetch_queries == []
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_normalised_titles_must_match_but_case_and_spacing_are_ignored():
    content = _law_content()
    matching_conn = FakeConnection(
        documents=[
            _document(
                10,
                "memphis",
                101,
                content,
                title="  УГОЛОВНЫЙ   КОДЕКС ",
            ),
            _document(
                20,
                "memphis",
                202,
                content,
                title="уголовный кодекс",
            ),
        ]
    )

    matching = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=matching_conn,
    )

    assert matching.mutated is True

    mismatching_conn = FakeConnection(
        documents=[
            _document(
                10,
                "memphis",
                101,
                content,
                title="Уголовный кодекс",
            ),
            _document(
                20,
                "memphis",
                202,
                content,
                title="Гражданский кодекс",
            ),
        ]
    )

    mismatching = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=mismatching_conn,
    )

    assert mismatching.mutated is False
    assert _warning_codes(mismatching) == {
        "old_document_has_no_exact_current_match"
    }
    assert mismatching_conn.mutation_queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("article_servers", "chunk_servers"),
    [
        ({501: "denver"}, {}),
        ({}, {601: "denver"}),
    ],
)
async def test_child_from_another_server_blocks_pair(article_servers, chunk_servers):
    content = _law_content()
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, content),
            _document(20, "memphis", 202, content),
        ],
        articles={501: 10},
        chunks={601: 10},
        article_servers=article_servers,
        chunk_servers=chunk_servers,
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    assert _warning_codes(result) == {"child_server_mismatch"}
    assert conn.articles == {501: 10}
    assert conn.chunks == {601: 10}
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_target_document_must_be_newer_than_missing_document():
    content = _law_content()
    conn = FakeConnection(
        documents=[
            _document(20, "memphis", 101, content),
            _document(10, "memphis", 202, content),
        ],
        articles={501: 20},
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        [202],
        conn=conn,
    )

    assert result.mutated is False
    assert _warning_codes(result) == {"target_document_not_newer"}
    assert conn.articles == {501: 20}
    assert conn.mutation_queries == []


@pytest.mark.asyncio
async def test_full_live_discovery_set_is_retry_safe_and_unmatched_targets_are_normal():
    replacement_content = _law_content("заменённый документ")
    unrelated_content = _law_content("действующий отдельный документ")
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, replacement_content),
            _document(20, "memphis", 202, replacement_content),
            _document(
                30,
                "memphis",
                303,
                unrelated_content,
                title="Процессуальный кодекс",
            ),
        ],
        articles={501: 10},
    )

    result = await handoff_exact_thread_replacements(
        "memphis",
        [101],
        # The caller may pass every currently discovered/live thread, not only
        # the target inserted in this particular attempt.
        [202, 303],
        conn=conn,
    )

    assert result.mutated is True
    assert result.current_thread_ids == (202, 303)
    assert [(item.old_thread_id, item.target_thread_id) for item in result.handoffs] == [
        (101, 202)
    ]
    assert result.warnings == ()
    assert conn.articles == {501: 20}


@pytest.mark.asyncio
async def test_reconcile_combines_safe_handoff_and_ordinary_delete_with_metadata():
    replacement_content = _law_content("перенесённый документ")
    removed_content = _law_content("действительно удалённый документ")
    unrelated_content = _law_content("другой действующий документ")
    conn = FakeConnection(
        documents=[
            _document(
                10,
                "memphis",
                101,
                replacement_content,
                title="Уголовный кодекс",
                gcs_uri="gs://laws/old-criminal.txt",
            ),
            _document(
                20,
                "memphis",
                202,
                replacement_content,
                title="Уголовный кодекс",
                gcs_uri="gs://laws/current-criminal.txt",
            ),
            _document(
                30,
                "memphis",
                303,
                removed_content,
                title="Административный кодекс",
                gcs_uri="gs://laws/removed-administrative.txt",
            ),
            _document(
                40,
                "memphis",
                404,
                unrelated_content,
                title="Процессуальный кодекс",
                gcs_uri="gs://laws/current-procedure.txt",
            ),
        ],
        articles={501: 10, 502: 10},
        chunks={601: 10},
    )

    result = await reconcile_missing_thread_documents(
        conn,
        server_id="memphis",
        missing_thread_ids=[101, 303],
        current_thread_ids=[202, 404],
    )

    assert result.server_id == "memphis"
    assert result.missing_thread_ids == (101, 303)
    assert result.ordinary_deleted_thread_ids == (303,)
    assert result.target_thread_ids == (202,)
    assert [(item.old_thread_id, item.target_thread_id) for item in result.handoffs] == [
        (101, 202)
    ]
    assert _warning_codes(result.handoff_result) == {
        "old_document_has_no_exact_current_match"
    }
    assert [
        (
            item.document_id,
            item.thread_id,
            item.title,
            item.gcs_uri,
            item.handoff_target_thread_id,
        )
        for item in result.removed_documents
    ] == [
        (
            10,
            101,
            "Уголовный кодекс",
            "gs://laws/old-criminal.txt",
            202,
        ),
        (
            30,
            303,
            "Административный кодекс",
            "gs://laws/removed-administrative.txt",
            None,
        ),
    ]
    assert result.gcs_uris == (
        "gs://laws/old-criminal.txt",
        "gs://laws/removed-administrative.txt",
    )
    assert set(conn.documents) == {20, 40}
    assert conn.articles == {501: 20, 502: 20}
    assert conn.chunks == {601: 20}


@pytest.mark.asyncio
async def test_reconcile_blocker_after_safe_pair_raises_for_caller_rollback():
    replacement_content = _law_content("безопасная замена")
    conn = FakeConnection(
        documents=[
            _document(10, "memphis", 101, replacement_content),
            _document(20, "memphis", 202, replacement_content),
            _document(
                30,
                "memphis",
                303,
                "ошибка парсера",
                title="Повреждённый документ",
            ),
        ],
        articles={501: 10},
        chunks={601: 10},
        in_transaction=False,
    )

    with pytest.raises(
        handoff_service.ExactThreadHandoffInvariantError,
        match="blocking warnings.*old_document_low_information_content",
    ):
        async with conn.transaction():
            await reconcile_missing_thread_documents(
                conn,
                server_id="memphis",
                missing_thread_ids=[101, 303],
                current_thread_ids=[202],
            )

    # The exact pair was processed before the blocker was surfaced, so the
    # caller-owned transaction must be able to restore every database change.
    assert any(
        "UPDATE law_articles" in query
        for query, _args in conn.mutation_queries
    )
    assert any(
        "DELETE FROM law_documents" in query
        for query, _args in conn.mutation_queries
    )
    assert set(conn.documents) == {10, 20, 30}
    assert conn.articles == {501: 10}
    assert conn.chunks == {601: 10}


@pytest.mark.asyncio
async def test_reconcile_allows_only_no_exact_current_match_for_ordinary_delete():
    conn = FakeConnection(
        documents=[
            _document(
                30,
                "memphis",
                303,
                _law_content("удалённый без точной замены документ"),
                gcs_uri="gs://laws/ordinary-delete.txt",
            ),
        ]
    )

    result = await reconcile_missing_thread_documents(
        conn,
        server_id="memphis",
        missing_thread_ids=[303],
        current_thread_ids=[],
    )

    assert result.handoffs == ()
    assert _warning_codes(result.handoff_result) == {
        "old_document_has_no_exact_current_match"
    }
    assert result.ordinary_deleted_thread_ids == (303,)
    assert result.gcs_uris == ("gs://laws/ordinary-delete.txt",)
    assert conn.documents == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_content", "warning_code"),
    [
        ("", "current_document_no_content"),
        ("обрезанная страница ошибки", "current_document_low_information_content"),
    ],
)
async def test_reconcile_blocks_cleanup_for_broken_current_document(
    current_content,
    warning_code,
):
    conn = FakeConnection(
        documents=[
            _document(
                30,
                "memphis",
                303,
                _law_content("старый исчезнувший документ"),
            ),
            _document(
                40,
                "memphis",
                404,
                current_content,
                title="Повреждённая текущая тема",
            ),
        ]
    )

    with pytest.raises(
        handoff_service.ExactThreadHandoffInvariantError,
        match=warning_code,
    ):
        await reconcile_missing_thread_documents(
            conn,
            server_id="memphis",
            missing_thread_ids=[303],
            current_thread_ids=[404],
        )

    assert set(conn.documents) == {30, 40}


@pytest.mark.asyncio
async def test_reconcile_raises_when_ordinary_delete_count_does_not_match():
    conn = FakeConnection(
        documents=[
            _document(
                30,
                "memphis",
                303,
                _law_content("строка должна была удалиться"),
            ),
        ],
        bulk_delete_thread_ids=set(),
    )

    with pytest.raises(
        handoff_service.ExactThreadHandoffInvariantError,
        match=r"expected=\(303,\), deleted=\(\)",
    ):
        await reconcile_missing_thread_documents(
            conn,
            server_id="memphis",
            missing_thread_ids=[303],
            current_thread_ids=[],
        )

    assert set(conn.documents) == {30}
