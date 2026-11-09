"""Concurrency guards shared by RAG indexing and law-document handoff."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

import services.indexing_service as indexing


class _Connection:
    def __init__(self, parent_id):
        self.parent_id = parent_id
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self.parent_id

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "OK"


class _Db:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def transaction(self):
        yield self.conn


def _install_index_stubs(monkeypatch, conn):
    async def embed_texts(_texts, **_kwargs):
        return [[0.1, 0.2]]

    monkeypatch.setattr(indexing, "embed_texts", embed_texts)
    monkeypatch.setattr(
        indexing,
        "chunk_law_text",
        lambda _content, _title: [
            {
                "chunk_index": 0,
                "article_ref": "Статья 1",
                "content": "Длинный текст нормы",
            }
        ],
    )
    monkeypatch.setattr(indexing, "db_manager", _Db(conn))
    monkeypatch.setattr(indexing.config, "EMBEDDING_PG_TYPE", "vector")


@pytest.mark.asyncio
async def test_index_document_locks_parent_before_replacing_chunks(monkeypatch):
    conn = _Connection(parent_id=10)
    _install_index_stubs(monkeypatch, conn)

    count = await indexing.index_document(
        {
            "id": 10,
            "server_id": "memphis",
            "title": "УК",
            "content": "Статья 1\nДлинный текст нормы",
            "content_hash": "hash",
        }
    )

    assert count == 1
    assert [kind for kind, _query, _args in conn.calls] == [
        "fetchval",
        "execute",
        "execute",
    ]
    assert "FROM law_documents" in conn.calls[0][1]
    assert "FOR UPDATE" in conn.calls[0][1]
    assert conn.calls[0][2] == (10, "memphis")
    assert "DELETE FROM law_chunks" in conn.calls[1][1]
    assert "INSERT INTO law_chunks" in conn.calls[2][1]


@pytest.mark.asyncio
async def test_index_document_never_writes_chunks_after_parent_disappears(
    monkeypatch,
):
    conn = _Connection(parent_id=None)
    _install_index_stubs(monkeypatch, conn)

    count = await indexing.index_document(
        {
            "id": 10,
            "server_id": "memphis",
            "title": "УК",
            "content": "Статья 1\nДлинный текст нормы",
            "content_hash": "hash",
        }
    )

    assert count == 0
    assert len(conn.calls) == 1
    assert conn.calls[0][0] == "fetchval"
    assert "FOR UPDATE" in conn.calls[0][1]
