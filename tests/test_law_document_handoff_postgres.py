"""Real PostgreSQL proofs for cross-thread stable article identity handoff.

Set ``LAW_HANDOFF_TEST_DATABASE_URL`` to a disposable PostgreSQL database.
The tests create and drop a private schema and never touch public tables.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import os
import uuid

import asyncpg
import pytest

from services.law_document_handoff import (
    ExactThreadHandoffInvariantError,
    handoff_exact_thread_replacements,
    reconcile_missing_thread_documents,
)


_DSN = os.getenv("LAW_HANDOFF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="LAW_HANDOFF_TEST_DATABASE_URL is not configured",
)


def _law_text(label: str = "Уголовный кодекс") -> str:
    paragraph = (
        "Статья устанавливает общий порядок, права, обязанности, основания, "
        "ограничения, ответственность, сроки и процедуру применения нормы закона. "
    )
    return f"{label}\n" + paragraph * 12


@asynccontextmanager
async def _database():
    conn = await asyncpg.connect(_DSN)
    schema = f"handoff_{uuid.uuid4().hex}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}"')
    await conn.execute(
        """
        CREATE TABLE law_documents (
            id INTEGER PRIMARY KEY,
            server_id TEXT NOT NULL,
            source_thread_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            gemini_file_uri TEXT,
            derived_sync_pending BOOLEAN NOT NULL DEFAULT FALSE,
            UNIQUE (server_id, source_thread_id)
        );
        CREATE TABLE law_articles (
            id INTEGER PRIMARY KEY,
            law_document_id INTEGER NOT NULL
                REFERENCES law_documents(id) ON DELETE CASCADE,
            server_id TEXT NOT NULL,
            source_order INTEGER NOT NULL,
            UNIQUE (law_document_id, source_order)
                DEFERRABLE INITIALLY DEFERRED
        );
        CREATE TABLE law_chunks (
            id INTEGER PRIMARY KEY,
            law_document_id INTEGER NOT NULL
                REFERENCES law_documents(id) ON DELETE CASCADE,
            server_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL
        );
        CREATE TABLE law_synonyms (
            id INTEGER PRIMARY KEY,
            article_id INTEGER NOT NULL
                REFERENCES law_articles(id) ON DELETE CASCADE
        );
        CREATE TABLE law_article_embeddings (
            article_id INTEGER NOT NULL
                REFERENCES law_articles(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL,
            PRIMARY KEY (article_id, space_id)
        );
        CREATE TABLE law_embedding_jobs (
            article_id INTEGER NOT NULL
                REFERENCES law_articles(id) ON DELETE CASCADE,
            space_id TEXT NOT NULL,
            PRIMARY KEY (article_id, space_id)
        );
        CREATE TABLE law_article_related (
            article_id INTEGER NOT NULL
                REFERENCES law_articles(id) ON DELETE CASCADE,
            related_id INTEGER NOT NULL
                REFERENCES law_articles(id) ON DELETE CASCADE,
            PRIMARY KEY (article_id, related_id)
        );
        """
    )
    try:
        yield conn, schema
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()


async def _seed_exact_pair(conn) -> None:
    content = _law_text()
    await conn.execute(
        """
        INSERT INTO law_documents
            (id, server_id, source_thread_id, title, content)
        VALUES
            (10, 'memphis', 101, 'Уголовный кодекс', $1),
            (20, 'memphis', 202, '  уголовный   КОДЕКС  ', $1)
        """,
        content,
    )
    await conn.execute(
        """
        INSERT INTO law_articles
            (id, law_document_id, server_id, source_order)
        VALUES
            (501, 10, 'memphis', 0),
            (502, 10, 'memphis', 1);
        INSERT INTO law_chunks
            (id, law_document_id, server_id, chunk_index)
        VALUES
            (601, 10, 'memphis', 0),
            (602, 10, 'memphis', 1);
        INSERT INTO law_synonyms (id, article_id) VALUES (701, 501);
        INSERT INTO law_article_embeddings (article_id, space_id)
            VALUES (501, 'frida-v1');
        INSERT INTO law_embedding_jobs (article_id, space_id)
            VALUES (502, 'frida-v1');
        INSERT INTO law_article_related (article_id, related_id)
            VALUES (501, 502);
        """
    )


async def _seed_genuine_deletion(conn) -> None:
    await conn.execute(
        """
        INSERT INTO law_documents
            (id, server_id, source_thread_id, title, content, gemini_file_uri)
        VALUES
            (
                30,
                'memphis',
                303,
                'Изменённый регламент',
                $1,
                'gs://laws/memphis/303.txt'
            )
        """,
        _law_text("Изменённый регламент"),
    )
    await conn.execute(
        """
        INSERT INTO law_articles
            (id, law_document_id, server_id, source_order)
        VALUES
            (503, 30, 'memphis', 0);
        INSERT INTO law_chunks
            (id, law_document_id, server_id, chunk_index)
        VALUES
            (603, 30, 'memphis', 0);
        INSERT INTO law_synonyms (id, article_id) VALUES (703, 503);
        """
    )


async def _seed_blocking_exact_pair(conn) -> None:
    content = _law_text("Процессуальный кодекс")
    await conn.execute(
        """
        INSERT INTO law_documents
            (id, server_id, source_thread_id, title, content, gemini_file_uri)
        VALUES
            (
                30,
                'memphis',
                303,
                'Процессуальный кодекс',
                $1,
                'gs://laws/memphis/303.txt'
            ),
            (
                40,
                'memphis',
                404,
                ' процессуальный  КОДЕКС ',
                $1,
                'gs://laws/memphis/404.txt'
            )
        """,
        content,
    )
    await conn.execute(
        """
        INSERT INTO law_articles
            (id, law_document_id, server_id, source_order)
        VALUES
            (503, 30, 'memphis', 0),
            (504, 40, 'memphis', 0);
        INSERT INTO law_chunks
            (id, law_document_id, server_id, chunk_index)
        VALUES
            (603, 30, 'memphis', 0);
        """
    )


@pytest.mark.asyncio
async def test_postgres_handoff_preserves_public_ids_and_all_article_dependents():
    async with _database() as (conn, _schema):
        await _seed_exact_pair(conn)

        async with conn.transaction():
            result = await handoff_exact_thread_replacements(
                "memphis",
                [101],
                [202],
                conn=conn,
            )

        assert result.mutated is True
        assert result.warnings == ()
        assert await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM law_documents WHERE id = 10)"
        ) is False
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles "
            "WHERE id IN (501, 502) AND law_document_id = 20"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_chunks WHERE law_document_id = 20"
        ) == 2
        assert await conn.fetchval(
            "SELECT derived_sync_pending FROM law_documents WHERE id = 20"
        ) is True
        assert await conn.fetchval("SELECT count(*) FROM law_synonyms") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_article_embeddings") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_embedding_jobs") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_article_related") == 1


@pytest.mark.asyncio
async def test_postgres_failure_after_article_move_rolls_back_everything():
    async with _database() as (conn, _schema):
        await _seed_exact_pair(conn)
        await conn.execute(
            """
            CREATE FUNCTION reject_chunk_move() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected chunk failure';
            END;
            $$;
            CREATE TRIGGER reject_chunk_move
            BEFORE UPDATE ON law_chunks
            FOR EACH ROW EXECUTE FUNCTION reject_chunk_move();
            """
        )

        with pytest.raises(asyncpg.RaiseError, match="injected chunk failure"):
            async with conn.transaction():
                await handoff_exact_thread_replacements(
                    "memphis",
                    [101],
                    [202],
                    conn=conn,
                )

        assert await conn.fetchval(
            "SELECT count(*) FROM law_documents WHERE id IN (10, 20)"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles WHERE law_document_id = 10"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_chunks WHERE law_document_id = 10"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles WHERE law_document_id = 20"
        ) == 0


@pytest.mark.asyncio
async def test_postgres_connection_without_transaction_is_rejected_before_sql():
    async with _database() as (conn, _schema):
        await _seed_exact_pair(conn)

        with pytest.raises(
            ExactThreadHandoffInvariantError,
            match="active database transaction",
        ):
            await handoff_exact_thread_replacements(
                "memphis",
                [101],
                [202],
                conn=conn,
            )

        assert await conn.fetchval(
            "SELECT count(*) FROM law_documents WHERE id IN (10, 20)"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles WHERE law_document_id = 10"
        ) == 2


@pytest.mark.asyncio
async def test_parent_lock_protocol_prevents_chunk_duplicates_during_handoff():
    async with _database() as (conn, schema):
        await _seed_exact_pair(conn)
        writer_conn = await asyncpg.connect(_DSN)
        await writer_conn.execute(f'SET search_path TO "{schema}"')
        writer_started = asyncio.Event()

        async def replace_chunks_like_index_document():
            async with writer_conn.transaction():
                writer_started.set()
                await writer_conn.fetchval(
                    """
                    SELECT id FROM law_documents
                    WHERE id = 20 AND server_id = 'memphis'
                    FOR UPDATE
                    """
                )
                await writer_conn.execute(
                    "DELETE FROM law_chunks WHERE law_document_id = 20"
                )
                await writer_conn.execute(
                    """
                    INSERT INTO law_chunks
                        (id, law_document_id, server_id, chunk_index)
                    VALUES (999, 20, 'memphis', 0)
                    """
                )

        transaction = conn.transaction()
        await transaction.start()
        try:
            await handoff_exact_thread_replacements(
                "memphis",
                [101],
                [202],
                conn=conn,
            )
            writer = asyncio.create_task(replace_chunks_like_index_document())
            await writer_started.wait()
            await asyncio.sleep(0.05)
            assert writer.done() is False
            await transaction.commit()
            await asyncio.wait_for(writer, timeout=2)
        except BaseException:
            if conn.is_in_transaction():
                await transaction.rollback()
            raise
        finally:
            await writer_conn.close()

        chunks = await conn.fetch(
            "SELECT id, chunk_index FROM law_chunks "
            "WHERE law_document_id = 20 ORDER BY id"
        )
        assert [(row["id"], row["chunk_index"]) for row in chunks] == [(999, 0)]


@pytest.mark.asyncio
async def test_postgres_reconcile_commits_handoff_and_genuine_deletion_atomically():
    async with _database() as (conn, _schema):
        await _seed_exact_pair(conn)
        await _seed_genuine_deletion(conn)
        await conn.execute(
            """
            UPDATE law_documents
            SET gemini_file_uri = 'gs://laws/memphis/101.txt'
            WHERE id = 10
            """
        )

        async with conn.transaction():
            result = await reconcile_missing_thread_documents(
                conn,
                server_id="memphis",
                missing_thread_ids=[303, 101],
                current_thread_ids=[202],
            )

        assert result.server_id == "memphis"
        assert result.missing_thread_ids == (101, 303)
        assert result.target_thread_ids == (202,)
        assert result.ordinary_deleted_thread_ids == (303,)
        assert result.gcs_uris == (
            "gs://laws/memphis/101.txt",
            "gs://laws/memphis/303.txt",
        )
        assert [
            (
                item.document_id,
                item.thread_id,
                item.handoff_target_thread_id,
            )
            for item in result.removed_documents
        ] == [
            (10, 101, 202),
            (30, 303, None),
        ]
        assert len(result.handoffs) == 1
        assert (
            result.handoffs[0].old_document_id,
            result.handoffs[0].target_document_id,
            result.handoffs[0].article_count,
            result.handoffs[0].chunk_count,
        ) == (10, 20, 2, 2)
        assert {
            warning.code for warning in result.handoff_result.warnings
        } == {"old_document_has_no_exact_current_match"}

        assert await conn.fetchval(
            "SELECT count(*) FROM law_documents WHERE id IN (10, 30)"
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM law_documents WHERE id = 20"
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles "
            "WHERE id IN (501, 502) AND law_document_id = 20"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_chunks "
            "WHERE id IN (601, 602) AND law_document_id = 20"
        ) == 2
        assert await conn.fetchval("SELECT count(*) FROM law_synonyms") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_article_embeddings") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_embedding_jobs") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_article_related") == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles WHERE id = 503"
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM law_chunks WHERE id = 603"
        ) == 0


@pytest.mark.asyncio
async def test_postgres_reconcile_blocker_rolls_back_every_pair_and_deletion():
    async with _database() as (conn, _schema):
        await _seed_exact_pair(conn)
        await _seed_blocking_exact_pair(conn)

        with pytest.raises(
            ExactThreadHandoffInvariantError,
            match="current_document_has_dependents",
        ):
            async with conn.transaction():
                await reconcile_missing_thread_documents(
                    conn,
                    server_id="memphis",
                    missing_thread_ids=[101, 303],
                    current_thread_ids=[202, 404],
                )

        assert await conn.fetchval(
            "SELECT count(*) FROM law_documents WHERE id IN (10, 20, 30, 40)"
        ) == 4
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles "
            "WHERE id IN (501, 502) AND law_document_id = 10"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_chunks "
            "WHERE id IN (601, 602) AND law_document_id = 10"
        ) == 2
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles "
            "WHERE id = 503 AND law_document_id = 30"
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM law_chunks "
            "WHERE id = 603 AND law_document_id = 30"
        ) == 1
        assert await conn.fetchval(
            "SELECT count(*) FROM law_articles "
            "WHERE id = 504 AND law_document_id = 40"
        ) == 1
        assert await conn.fetchval("SELECT count(*) FROM law_synonyms") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_article_embeddings") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_embedding_jobs") == 1
        assert await conn.fetchval("SELECT count(*) FROM law_article_related") == 1
