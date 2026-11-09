"""End-to-end wiring guards for forum-thread identity handoff.

The database identity operation itself has focused unit/PostgreSQL tests in
``test_law_document_handoff*.py``.  These tests exercise the surrounding
knowledge-base sync: persistence readiness, transaction boundaries, GCS
ordering, and downstream reindex scheduling.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import knowledge_base_manager as kb
from remote_parser_client import SectionDiscoveryResult
from services.law_document_handoff import ExactThreadHandoffInvariantError


class _TransactionConnection:
    def __init__(self, owner: "_TransactionDb") -> None:
        self.owner = owner
        self.active = False

    def is_in_transaction(self) -> bool:
        return self.active


class _AdvisoryConnection:
    def __init__(self, owner: "_TransactionDb") -> None:
        self.owner = owner

    async def execute(self, query, *args):
        assert "pg_advisory_lock" in query
        self.owner.advisory_events.append(("advisory_lock", args[0]))
        return "SELECT 1"

    async def fetchval(self, query, *args):
        assert "pg_advisory_unlock" in query
        self.owner.advisory_events.append(("advisory_unlock", args[0]))
        return True


class _TransactionDb:
    def __init__(self, records) -> None:
        self.records = records
        self.events: list[str] = []
        self.advisory_events: list[tuple[str, str]] = []
        self.fail_final_uri_update = False
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.transaction_connection = _TransactionConnection(self)
        self.advisory_connection = _AdvisoryConnection(self)

    async def fetch(self, query, *args):
        if "source_thread_id IS NULL" in query:
            return []
        if "SELECT DISTINCT gemini_file_uri" in query:
            candidates = set(args[0])
            return [
                {"gemini_file_uri": record.get("gemini_file_uri")}
                for record in self.records
                if record.get("gemini_file_uri") in candidates
            ]
        if (
            "SELECT source_thread_id" in query
            and "FROM law_documents WHERE server_id" in query
        ):
            return self.records
        raise AssertionError(f"unexpected manager fetch: {query} {args}")

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if "INSERT INTO law_documents" in query:
            thread_id = int(args[1])
            matching = [
                record
                for record in self.records
                if int(record["source_thread_id"]) == thread_id
            ]
            if matching:
                record = matching[0]
                record["source_url"] = args[2]
                record["title"] = args[3]
                record["content_hash"] = args[5]
                # Mirror the production ON CONFLICT safety invariant: changed
                # DB content invalidates the old object attestation before an
                # upload is attempted.
                record["gemini_file_uri"] = None
                record["derived_sync_pending"] = True
            else:
                self.records.append(
                    {
                        "source_thread_id": thread_id,
                        "source_url": args[2],
                        "title": args[3],
                        "content_hash": args[5],
                        "gemini_file_uri": None,
                        "derived_sync_pending": True,
                    }
                )
        return "OK"

    async def fetchval(self, query, *args):
        self.executed.append((query, args))
        if "SET gemini_file_uri = NULL" in query:
            server_id, thread_id, content_hash, title, source_url, old_uri = args
            matching = [
                record
                for record in self.records
                if int(record["source_thread_id"]) == int(thread_id)
                and record.get("content_hash") == content_hash
                and record.get("title") == title
                and record.get("source_url") == source_url
                and record.get("gemini_file_uri") == old_uri
            ]
            if not matching:
                return None
            matching[0]["gemini_file_uri"] = None
            matching[0]["derived_sync_pending"] = True
            return int(thread_id)
        if (
            "UPDATE law_documents" in query
            and "SET gemini_file_uri = $1" in query
            and "RETURNING id" in query
        ):
            if self.fail_final_uri_update:
                return None
            uri, _server_id, thread_id, content_hash, title, source_url = args
            matching = [
                record
                for record in self.records
                if int(record["source_thread_id"]) == int(thread_id)
                and record.get("content_hash") == content_hash
                and record.get("title") == title
                and record.get("source_url") == source_url
                and record.get("gemini_file_uri") is None
            ]
            if not matching:
                return None
            matching[0]["gemini_file_uri"] = uri
            return int(thread_id)
        raise AssertionError(f"unexpected manager fetchval: {query} {args}")

    @asynccontextmanager
    async def connection(self):
        yield self.advisory_connection

    @asynccontextmanager
    async def transaction(self):
        self.events.append("tx_enter")
        self.transaction_connection.active = True
        try:
            yield self.transaction_connection
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")
        finally:
            self.transaction_connection.active = False


class _UploadBlob:
    def __init__(self, bucket: "_UploadBucket", name: str) -> None:
        self.bucket = bucket
        self.name = name

    def upload_from_string(self, content, *, content_type):
        self.bucket.upload_attempts.append(
            (self.name, content, content_type)
        )
        if self.bucket.fail_upload:
            raise RuntimeError("simulated GCS upload failure")


class _UploadBucket:
    def __init__(self, *, fail_upload: bool = False) -> None:
        self.fail_upload = fail_upload
        self.upload_attempts: list[tuple[str, str, str]] = []

    def blob(self, name: str):
        return _UploadBlob(self, name)


def _storage_client_class(bucket: _UploadBucket):
    class StorageClient:
        @staticmethod
        def from_service_account_json(_path):
            class Client:
                @staticmethod
                def bucket(_name):
                    return bucket

            return Client()

    return StorageClient


async def _return_true():
    return True


async def _no_sleep(_seconds):
    return None


def _expected_gcs_uri(thread_id: int) -> str:
    digest = hashlib.md5(str(thread_id).encode("utf-8")).hexdigest()
    return f"gs://unused-bucket/test/{digest}.txt"


def _record(
    thread_id: int,
    content_hash: str,
    *,
    gcs_uri=...,
    title: str | None = None,
    source_url: str | None = None,
    derived_sync_pending: bool = False,
):
    if gcs_uri is ...:
        gcs_uri = _expected_gcs_uri(thread_id)
    return {
        "source_thread_id": thread_id,
        "source_url": source_url
        or f"https://forum.example/threads/law.{thread_id}/",
        "title": title or f"Law {thread_id}",
        "content_hash": content_hash,
        "gemini_file_uri": gcs_uri,
        "derived_sync_pending": derived_sync_pending,
    }


def _reconciliation_result(
    *,
    old_thread_id: int = 202,
    old_gcs_uri: str | None = "gs://unused-bucket/test/old-202.txt",
    target_thread_id: int | None = None,
):
    handoffs = ()
    if target_thread_id is not None:
        handoffs = (
            SimpleNamespace(
                old_document_id=20,
                target_document_id=30,
                old_thread_id=old_thread_id,
                target_thread_id=target_thread_id,
                article_count=4,
                chunk_count=2,
            ),
        )
    removed_documents = (
        SimpleNamespace(
            document_id=20,
            thread_id=old_thread_id,
            title="Old forum thread",
            gcs_uri=old_gcs_uri,
            handoff_target_thread_id=target_thread_id,
        ),
    )
    return SimpleNamespace(
        handoff_result=SimpleNamespace(handoffs=handoffs, warnings=()),
        removed_documents=removed_documents,
        handoffs=handoffs,
        target_thread_ids=(
            (target_thread_id,) if target_thread_id is not None else ()
        ),
        gcs_uris=((old_gcs_uri,) if old_gcs_uri else ()),
    )


def _configure_remote_sync(
    monkeypatch,
    fake_db: _TransactionDb,
    *,
    bucket: _UploadBucket,
    discovery_thread_ids: tuple[int, ...],
    topic_payloads: dict[int, dict[str, object]],
) -> None:
    async def discover_section(_url):
        return SectionDiscoveryResult(
            urls=[
                f"https://forum.example/threads/law.{thread_id}/"
                for thread_id in discovery_thread_ids
            ],
            complete=True,
        )

    async def parse_topic(url, *_args, **_kwargs):
        thread_id = kb.extract_thread_id_from_url(url)
        assert thread_id is not None
        return topic_payloads[thread_id]

    @asynccontextmanager
    async def no_op_catalog_lock(_server_id):
        yield

    async def no_op_reindex(
        _server_id,
        _thread_ids,
        *,
        document_lock_already_held=False,
    ):
        assert document_lock_already_held is True
        return {"chunks": 0}

    import services.indexing_service as indexing_service
    import services.law_embedding_service as embedding_service

    monkeypatch.setattr(kb, "reinitialize_vertex_ai", _return_true)
    monkeypatch.setattr(kb, "db_manager", fake_db)
    monkeypatch.setattr(kb.storage, "Client", _storage_client_class(bucket))
    monkeypatch.setattr(kb.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(kb.config, "VERTEX_CREDENTIALS_FILE", "unused.json")
    monkeypatch.setattr(kb.config, "active_gcs_bucket", lambda: "unused-bucket")
    monkeypatch.setattr(kb.config, "SERVER_UPDATE_LOCKS", {"test": asyncio.Lock()})
    monkeypatch.setattr(
        kb.config,
        "SERVERS_CONFIG",
        {
            "test": {
                "forum_section_urls": ["https://forum.example/forums/laws/"],
                "parse_only_author_posts": True,
            }
        },
    )
    monkeypatch.setattr(kb, "USE_REMOTE_PARSER", True)
    monkeypatch.setattr(kb, "check_remote_parser_available", _return_true)
    monkeypatch.setattr(kb, "remote_discover_section", discover_section)
    monkeypatch.setattr(kb, "remote_parse_topic", parse_topic)
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        no_op_catalog_lock,
    )
    monkeypatch.setattr(
        indexing_service,
        "reindex_changed_documents",
        no_op_reindex,
    )


def _unchanged_topic(thread_id: int, content_hash: str) -> dict[str, object]:
    return {
        "title": f"Law {thread_id}",
        "content": f"unchanged content for law {thread_id}",
        "thread_id": thread_id,
        "content_hash": content_hash,
    }


@pytest.mark.asyncio
async def test_handoff_commits_before_old_gcs_delete_and_reindexes_target(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202"),
            _record(303, "hash-303"),
        ]
    )
    bucket = _UploadBucket()
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(101, 303),
        topic_payloads={
            101: _unchanged_topic(101, "hash-101"),
            303: _unchanged_topic(303, "hash-303"),
        },
    )
    captured: dict[str, object] = {}

    async def reconcile(
        conn,
        *,
        server_id,
        missing_thread_ids,
        current_thread_ids,
    ):
        assert conn.is_in_transaction() is True
        assert server_id == "test"
        assert set(missing_thread_ids) == {202}
        assert set(current_thread_ids) == {101, 303}
        db.events.append("reconcile")
        return _reconciliation_result(target_thread_id=303)

    async def delete_gcs(uris, _bucket):
        assert db.events[-1] == "commit"
        db.events.append("gcs_delete")
        captured["gcs_uris"] = tuple(uris)

    async def reindex(
        server_id,
        thread_ids,
        *,
        document_lock_already_held=False,
    ):
        assert document_lock_already_held is True
        db.events.append("reindex")
        captured["reindex"] = (server_id, set(thread_ids))
        return {"chunks": 2}

    import services.indexing_service as indexing_service

    monkeypatch.setattr(kb, "reconcile_missing_thread_documents", reconcile)
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)
    monkeypatch.setattr(
        indexing_service,
        "reindex_changed_documents",
        reindex,
    )

    assert await kb.update_knowledge_base_for_server("test") is True
    assert db.events[:4] == [
        "tx_enter",
        "reconcile",
        "commit",
        "gcs_delete",
    ]
    assert captured["gcs_uris"] == (
        "gs://unused-bucket/test/old-202.txt",
    )
    assert captured["reindex"] == ("test", {303})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ExactThreadHandoffInvariantError("blocking handoff warning"),
        RuntimeError("simulated database failure"),
    ],
    ids=["invariant-warning", "database-error"],
)
async def test_reconciliation_failure_rolls_back_and_never_deletes_gcs(
    monkeypatch,
    failure,
) -> None:
    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202"),
        ]
    )
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=_UploadBucket(),
        discovery_thread_ids=(101,),
        topic_payloads={101: _unchanged_topic(101, "hash-101")},
    )
    gcs_calls = []

    async def reconcile(*_args, **_kwargs):
        db.events.append("reconcile")
        raise failure

    async def delete_gcs(*args):
        gcs_calls.append(args)

    monkeypatch.setattr(kb, "reconcile_missing_thread_documents", reconcile)
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)

    assert await kb.update_knowledge_base_for_server("test") is False
    assert db.events == ["tx_enter", "reconcile", "rollback"]
    assert gcs_calls == []


@pytest.mark.asyncio
async def test_genuine_missing_thread_is_still_removed_before_gcs_cleanup(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202"),
        ]
    )
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=_UploadBucket(),
        discovery_thread_ids=(101,),
        topic_payloads={101: _unchanged_topic(101, "hash-101")},
    )

    async def reconcile(*_args, **_kwargs):
        db.events.append("ordinary_delete")
        return _reconciliation_result(target_thread_id=None)

    async def delete_gcs(uris, _bucket):
        assert db.events[-1] == "commit"
        assert tuple(uris) == ("gs://unused-bucket/test/old-202.txt",)
        db.events.append("gcs_delete")

    monkeypatch.setattr(kb, "reconcile_missing_thread_documents", reconcile)
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)

    assert await kb.update_knowledge_base_for_server("test") is True
    assert db.events == [
        "tx_enter",
        "ordinary_delete",
        "commit",
        "gcs_delete",
    ]


@pytest.mark.asyncio
async def test_cleanup_never_deletes_gcs_uri_still_referenced_by_live_document(
    monkeypatch,
) -> None:
    shared_uri = "gs://unused-bucket/test/shared-live-object.txt"
    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202", gcs_uri=shared_uri),
            _record(303, "hash-303", gcs_uri=shared_uri),
        ]
    )
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=_UploadBucket(),
        discovery_thread_ids=(101, 303),
        topic_payloads={
            101: _unchanged_topic(101, "hash-101"),
            303: _unchanged_topic(303, "hash-303"),
        },
    )
    gcs_deletes = []

    async def reconcile(*_args, **_kwargs):
        db.events.append("ordinary_delete")
        return _reconciliation_result(
            old_gcs_uri=shared_uri,
            target_thread_id=None,
        )

    async def delete_gcs(*args):
        gcs_deletes.append(args)

    monkeypatch.setattr(kb, "reconcile_missing_thread_documents", reconcile)
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)

    assert await kb.update_knowledge_base_for_server("test") is True
    assert db.events == ["tx_enter", "ordinary_delete", "commit"]
    assert gcs_deletes == []


@pytest.mark.asyncio
async def test_new_thread_db_save_failure_blocks_destructive_cleanup(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202"),
        ]
    )
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=_UploadBucket(),
        discovery_thread_ids=(101, 303),
        topic_payloads={
            101: _unchanged_topic(101, "hash-101"),
            303: _unchanged_topic(303, "hash-303"),
        },
    )
    gcs_deletes = []

    async def fail_save(*_args, **_kwargs):
        raise RuntimeError("simulated document save failure")

    async def reconcile_must_not_run(*_args, **_kwargs):
        raise AssertionError("cleanup must not start after a save failure")

    async def delete_gcs(*args):
        gcs_deletes.append(args)

    monkeypatch.setattr(kb, "save_law_document_to_db", fail_save)
    monkeypatch.setattr(
        kb,
        "reconcile_missing_thread_documents",
        reconcile_must_not_run,
    )
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)

    assert await kb.update_knowledge_base_for_server("test") is False
    assert db.events == []
    assert gcs_deletes == []


@pytest.mark.asyncio
async def test_new_thread_gcs_upload_failure_blocks_destructive_cleanup(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202"),
        ]
    )
    bucket = _UploadBucket(fail_upload=True)
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(101, 303),
        topic_payloads={
            101: _unchanged_topic(101, "hash-101"),
            303: _unchanged_topic(303, "hash-303"),
        },
    )
    saved = []
    gcs_deletes = []

    async def save(*args, **_kwargs):
        saved.append((args, _kwargs))

    async def reconcile_must_not_run(*_args, **_kwargs):
        raise AssertionError("cleanup must not start after an upload failure")

    async def delete_gcs(*args):
        gcs_deletes.append(args)

    monkeypatch.setattr(kb, "save_law_document_to_db", save)
    monkeypatch.setattr(
        kb,
        "reconcile_missing_thread_documents",
        reconcile_must_not_run,
    )
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)

    assert await kb.update_knowledge_base_for_server("test") is False
    assert saved
    assert bucket.upload_attempts
    assert db.events == []
    assert gcs_deletes == []


@pytest.mark.asyncio
async def test_retry_repairs_missing_target_gcs_uri_before_handoff(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202"),
            _record(303, "hash-303", gcs_uri=None),
        ]
    )
    bucket = _UploadBucket()
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(101, 303),
        topic_payloads={
            101: _unchanged_topic(101, "hash-101"),
            303: _unchanged_topic(303, "hash-303"),
        },
    )
    reconcile_calls = []

    async def save(*_args, **_kwargs):
        return None

    async def reconcile(
        _conn,
        *,
        server_id,
        missing_thread_ids,
        current_thread_ids,
    ):
        reconcile_calls.append(
            (
                server_id,
                set(missing_thread_ids),
                set(current_thread_ids),
            )
        )
        return _reconciliation_result(target_thread_id=303)

    async def delete_gcs(_uris, _bucket):
        return None

    monkeypatch.setattr(kb, "save_law_document_to_db", save)
    monkeypatch.setattr(kb, "reconcile_missing_thread_documents", reconcile)
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)

    assert await kb.update_knowledge_base_for_server("test") is True
    expected_blob = _expected_gcs_uri(303).removeprefix(
        "gs://unused-bucket/"
    )
    assert [attempt[0] for attempt in bucket.upload_attempts] == [expected_blob]
    assert any(
        "UPDATE law_documents" in query
        and "SET gemini_file_uri" in query
        and args[:3] == (_expected_gcs_uri(303), "test", 303)
        for query, args in db.executed
    )
    assert reconcile_calls == [("test", {202}, {101, 303})]


@pytest.mark.asyncio
async def test_changed_existing_thread_invalidates_stale_gcs_before_retry(
    monkeypatch,
) -> None:
    """A failed overwrite must not let an old canonical URI attest new text."""

    db = _TransactionDb(
        [
            _record(101, "hash-101"),
            _record(202, "hash-202"),
            # This URI is canonical, but currently contains the old version.
            _record(303, "hash-303-old"),
        ]
    )
    bucket = _UploadBucket(fail_upload=True)
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(101, 303),
        topic_payloads={
            101: _unchanged_topic(101, "hash-101"),
            303: _unchanged_topic(303, "hash-303-new"),
        },
    )
    reconciliation_calls = []

    async def reconcile(
        _conn,
        *,
        server_id,
        missing_thread_ids,
        current_thread_ids,
    ):
        target_record = next(
            record
            for record in db.records
            if record["source_thread_id"] == 303
        )
        assert target_record["content_hash"] == "hash-303-new"
        assert target_record["gemini_file_uri"] == _expected_gcs_uri(303)
        reconciliation_calls.append(
            (
                server_id,
                set(missing_thread_ids),
                set(current_thread_ids),
            )
        )
        return _reconciliation_result(target_thread_id=303)

    async def delete_gcs(_uris, _bucket):
        return None

    monkeypatch.setattr(kb, "reconcile_missing_thread_documents", reconcile)
    monkeypatch.setattr(kb, "delete_files_from_gcs", delete_gcs)

    # First pass commits the new DB text/hash, invalidates its old GCS
    # attestation, then fails the upload.  Cleanup must not run.
    assert await kb.update_knowledge_base_for_server("test") is False
    target_after_failure = next(
        record
        for record in db.records
        if record["source_thread_id"] == 303
    )
    assert target_after_failure["content_hash"] == "hash-303-new"
    assert target_after_failure["gemini_file_uri"] is None
    save_queries = [
        query
        for query, _args in db.executed
        if "INSERT INTO law_documents" in query
    ]
    assert len(save_queries) == 1
    assert "gemini_file_uri = NULL" in save_queries[0]
    assert reconciliation_calls == []
    assert db.events == []

    # On retry the hash already matches, but NULL URI forces another upload.
    # Only the successful upload+URI update makes the target handoff-ready.
    bucket.fail_upload = False
    assert await kb.update_knowledge_base_for_server("test") is True
    expected_blob = _expected_gcs_uri(303).removeprefix(
        "gs://unused-bucket/"
    )
    assert [attempt[0] for attempt in bucket.upload_attempts] == [
        expected_blob,
        expected_blob,
    ]
    assert len(
        [
            query
            for query, _args in db.executed
            if "INSERT INTO law_documents" in query
        ]
    ) == 1
    assert reconciliation_calls == [("test", {202}, {101, 303})]
    assert db.events == ["tx_enter", "commit"]


@pytest.mark.asyncio
async def test_successful_uri_repair_reindexes_without_missing_documents(
    monkeypatch,
) -> None:
    """A retry must repair both GCS and derived indexes without a handoff."""

    db = _TransactionDb(
        [
            # The canonical object still contains the old version.
            _record(303, "hash-303-old"),
        ]
    )
    bucket = _UploadBucket(fail_upload=True)
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(303,),
        topic_payloads={
            303: _unchanged_topic(303, "hash-303-new"),
        },
    )
    reindex_calls = []

    async def reindex(
        server_id,
        thread_ids,
        *,
        document_lock_already_held=False,
    ):
        assert document_lock_already_held is True
        reindex_calls.append((server_id, set(thread_ids)))
        return {"chunks": 3}

    import services.indexing_service as indexing_service

    monkeypatch.setattr(
        indexing_service,
        "reindex_changed_documents",
        reindex,
    )

    # The DB content/hash update succeeds and invalidates the URI, but the
    # overwrite fails.  There are no missing documents, so no destructive
    # cleanup is involved and no derived index may be marked refreshed yet.
    assert await kb.update_knowledge_base_for_server("test") is True
    target_after_failure = next(
        record
        for record in db.records
        if record["source_thread_id"] == 303
    )
    assert target_after_failure["content_hash"] == "hash-303-new"
    assert target_after_failure["gemini_file_uri"] is None
    assert reindex_calls == []

    # The next sync sees matching content/hash but a missing URI.  A successful
    # upload+URI write is a recovery of the earlier content change and must
    # schedule the document for law_chunks/catalog reindexing.
    bucket.fail_upload = False
    assert await kb.update_knowledge_base_for_server("test") is True
    assert target_after_failure["gemini_file_uri"] == _expected_gcs_uri(303)
    assert reindex_calls == [("test", {303})]
    expected_blob = _expected_gcs_uri(303).removeprefix(
        "gs://unused-bucket/"
    )
    assert [attempt[0] for attempt in bucket.upload_attempts] == [
        expected_blob,
        expected_blob,
    ]


@pytest.mark.asyncio
async def test_wrong_non_null_uri_is_cleared_before_failed_upload_and_retried(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(
                303,
                "hash-303",
                gcs_uri="gs://unused-bucket/test/wrong-object.txt",
            ),
        ]
    )
    bucket = _UploadBucket(fail_upload=True)
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(303,),
        topic_payloads={303: _unchanged_topic(303, "hash-303")},
    )
    reindex_calls = []

    async def reindex(
        server_id,
        thread_ids,
        *,
        document_lock_already_held=False,
    ):
        assert document_lock_already_held is True
        reindex_calls.append((server_id, set(thread_ids)))
        return {"chunks": 1}

    import services.indexing_service as indexing_service

    monkeypatch.setattr(
        indexing_service,
        "reindex_changed_documents",
        reindex,
    )

    assert await kb.update_knowledge_base_for_server("test") is True
    record = db.records[0]
    assert record["gemini_file_uri"] is None
    assert record["derived_sync_pending"] is True
    assert reindex_calls == []

    bucket.fail_upload = False
    assert await kb.update_knowledge_base_for_server("test") is True
    assert record["gemini_file_uri"] == _expected_gcs_uri(303)
    assert record["derived_sync_pending"] is True
    assert reindex_calls == [("test", {303})]


@pytest.mark.asyncio
async def test_failed_final_uri_cas_leaves_null_and_is_retryable(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(
                303,
                "hash-303",
                gcs_uri="gs://unused-bucket/test/wrong-object.txt",
            ),
        ]
    )
    db.fail_final_uri_update = True
    bucket = _UploadBucket()
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(303,),
        topic_payloads={303: _unchanged_topic(303, "hash-303")},
    )
    reindex_calls = []

    async def reindex(
        server_id,
        thread_ids,
        *,
        document_lock_already_held=False,
    ):
        assert document_lock_already_held is True
        reindex_calls.append((server_id, set(thread_ids)))
        return {"chunks": 1}

    import services.indexing_service as indexing_service

    monkeypatch.setattr(
        indexing_service,
        "reindex_changed_documents",
        reindex,
    )

    assert await kb.update_knowledge_base_for_server("test") is True
    record = db.records[0]
    assert record["gemini_file_uri"] is None
    assert record["derived_sync_pending"] is True
    assert reindex_calls == []

    db.fail_final_uri_update = False
    assert await kb.update_knowledge_base_for_server("test") is True
    assert record["gemini_file_uri"] == _expected_gcs_uri(303)
    assert reindex_calls == [("test", {303})]


@pytest.mark.asyncio
async def test_title_only_change_updates_db_gcs_and_marks_derived_sync_pending(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(303, "hash-303", title="Old title"),
        ]
    )
    bucket = _UploadBucket()
    payload = _unchanged_topic(303, "hash-303")
    payload["title"] = "New title"
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(303,),
        topic_payloads={303: payload},
    )
    reindex_calls = []

    async def reindex(
        server_id,
        thread_ids,
        *,
        document_lock_already_held=False,
    ):
        assert document_lock_already_held is True
        reindex_calls.append((server_id, set(thread_ids)))
        return {"chunks": 1}

    import services.indexing_service as indexing_service

    monkeypatch.setattr(
        indexing_service,
        "reindex_changed_documents",
        reindex,
    )

    assert await kb.update_knowledge_base_for_server("test") is True
    record = db.records[0]
    assert record["title"] == "New title"
    assert record["content_hash"] == "hash-303"
    assert record["gemini_file_uri"] == _expected_gcs_uri(303)
    assert record["derived_sync_pending"] is True
    assert len(bucket.upload_attempts) == 1
    assert "New title" in bucket.upload_attempts[0][1]
    assert reindex_calls == [("test", {303})]


@pytest.mark.asyncio
async def test_pending_unchanged_ready_document_is_scheduled_without_reupload(
    monkeypatch,
) -> None:
    db = _TransactionDb(
        [
            _record(
                303,
                "hash-303",
                derived_sync_pending=True,
            ),
        ]
    )
    bucket = _UploadBucket()
    _configure_remote_sync(
        monkeypatch,
        db,
        bucket=bucket,
        discovery_thread_ids=(303,),
        topic_payloads={303: _unchanged_topic(303, "hash-303")},
    )
    reindex_calls = []

    async def reindex(
        server_id,
        thread_ids,
        *,
        document_lock_already_held=False,
    ):
        assert document_lock_already_held is True
        reindex_calls.append((server_id, set(thread_ids)))
        return {"chunks": 1}

    import services.indexing_service as indexing_service

    monkeypatch.setattr(
        indexing_service,
        "reindex_changed_documents",
        reindex,
    )

    assert await kb.update_knowledge_base_for_server("test") is True
    assert bucket.upload_attempts == []
    assert reindex_calls == [("test", {303})]
    # Knowledge persistence only schedules the retry; the successful indexing
    # transaction owns clearing this durable marker.
    assert db.records[0]["derived_sync_pending"] is True


@pytest.mark.asyncio
async def test_postgres_session_lock_serializes_distinct_local_sync_locks(
    monkeypatch,
) -> None:
    class AdvisoryDb:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.events = []

        @asynccontextmanager
        async def connection(self):
            owner = self

            class Connection:
                async def execute(self, query, *args):
                    assert "pg_advisory_lock" in query
                    owner.events.append(("waiting", args[0]))
                    await owner.lock.acquire()
                    owner.events.append(("acquired", args[0]))

                async def fetchval(self, query, *args):
                    assert "pg_advisory_unlock" in query
                    owner.events.append(("released", args[0]))
                    owner.lock.release()
                    return True

            yield Connection()

    fake_db = AdvisoryDb()
    monkeypatch.setattr(kb, "db_manager", fake_db)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def run(label, local_lock):
        async with kb._law_document_server_sync_lock("test", local_lock):
            fake_db.events.append(("body", label))
            if label == "first":
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()

    first = asyncio.create_task(run("first", asyncio.Lock()))
    await first_entered.wait()
    second = asyncio.create_task(run("second", asyncio.Lock()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert second_entered.is_set() is False

    release_first.set()
    await asyncio.gather(first, second)
    assert [event for event in fake_db.events if event[0] == "body"] == [
        ("body", "first"),
        ("body", "second"),
    ]


@pytest.mark.asyncio
async def test_local_sync_waiter_does_not_pin_an_extra_database_connection(
    monkeypatch,
) -> None:
    class CountingDb:
        def __init__(self):
            self.connection_entries = 0

        @asynccontextmanager
        async def connection(self):
            self.connection_entries += 1

            class Connection:
                async def execute(self, query, *_args):
                    assert "pg_advisory_lock" in query

                async def fetchval(self, query, *_args):
                    assert "pg_advisory_unlock" in query
                    return True

            yield Connection()

    fake_db = CountingDb()
    monkeypatch.setattr(kb, "db_manager", fake_db)
    shared_local_lock = asyncio.Lock()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_sync():
        async with kb._law_document_server_sync_lock(
            "test",
            shared_local_lock,
        ):
            first_entered.set()
            await release_first.wait()

    async def second_sync():
        async with kb._law_document_server_sync_lock(
            "test",
            shared_local_lock,
        ):
            second_entered.set()

    first = asyncio.create_task(first_sync())
    await first_entered.wait()
    second = asyncio.create_task(second_sync())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert second_entered.is_set() is False
    assert fake_db.connection_entries == 1

    release_first.set()
    await asyncio.gather(first, second)
    assert fake_db.connection_entries == 2


@pytest.mark.asyncio
async def test_cross_server_sync_limit_leaves_pool_capacity_for_body_queries(
    monkeypatch,
) -> None:
    """Many manual server syncs must not pin the entire shared DB pool."""

    class BoundedDb:
        def __init__(self):
            # Deliberately tiny: four advisory holders plus one body query.
            self.pool = asyncio.Semaphore(5)
            self.advisory_holders = 0
            self.max_advisory_holders = 0
            self.completed = []

        @asynccontextmanager
        async def connection(self):
            async with self.pool:
                self.advisory_holders += 1
                self.max_advisory_holders = max(
                    self.max_advisory_holders,
                    self.advisory_holders,
                )

                class Connection:
                    async def execute(_self, query, *_args):
                        assert "pg_advisory_lock" in query

                    async def fetchval(_self, query, *_args):
                        assert "pg_advisory_unlock" in query
                        return True

                try:
                    yield Connection()
                finally:
                    self.advisory_holders -= 1

        @asynccontextmanager
        async def body_query(self):
            async with self.pool:
                await asyncio.sleep(0)
                yield

    fake_db = BoundedDb()
    monkeypatch.setattr(kb, "db_manager", fake_db)
    monkeypatch.setattr(
        kb,
        "_LAW_DOCUMENT_SYNC_CONNECTION_SLOTS",
        asyncio.Semaphore(4),
    )

    async def run(server_number: int):
        server_id = f"server-{server_number}"
        async with kb._law_document_server_sync_lock(
            server_id,
            asyncio.Lock(),
        ):
            async with fake_db.body_query():
                fake_db.completed.append(server_id)

    await asyncio.wait_for(
        asyncio.gather(*(run(number) for number in range(10))),
        timeout=2,
    )

    assert len(fake_db.completed) == 10
    assert fake_db.max_advisory_holders <= 4


@pytest.mark.asyncio
async def test_cancelled_storage_call_finishes_before_server_lock_is_released(
    monkeypatch,
) -> None:
    """A cancelled to_thread upload must not overwrite a later sync."""

    class AdvisoryDb:
        def __init__(self):
            self.lock = asyncio.Lock()

        @asynccontextmanager
        async def connection(self):
            owner = self

            class Connection:
                async def execute(self, query, *_args):
                    assert "pg_advisory_lock" in query
                    await owner.lock.acquire()

                async def fetchval(self, query, *_args):
                    assert "pg_advisory_unlock" in query
                    owner.lock.release()
                    return True

            yield Connection()

    fake_db = AdvisoryDb()
    monkeypatch.setattr(kb, "db_manager", fake_db)
    monkeypatch.setattr(
        kb,
        "_LAW_DOCUMENT_SYNC_CONNECTION_SLOTS",
        asyncio.Semaphore(4),
    )

    upload_started = threading.Event()
    finish_upload = threading.Event()
    second_sync_entered = asyncio.Event()
    order = []

    def blocking_upload():
        upload_started.set()
        assert finish_upload.wait(timeout=2)
        order.append("old-upload-finished")

    async def first_sync():
        async with kb._law_document_server_sync_lock(
            "test",
            asyncio.Lock(),
        ):
            await kb._run_storage_call_cancellation_safe(blocking_upload)

    async def second_sync():
        async with kb._law_document_server_sync_lock(
            "test",
            asyncio.Lock(),
        ):
            order.append("second-sync-entered")
            second_sync_entered.set()

    first = asyncio.create_task(first_sync())
    for _ in range(100):
        if upload_started.is_set():
            break
        await asyncio.sleep(0.001)
    assert upload_started.is_set()

    first.cancel()
    second = asyncio.create_task(second_sync())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert second_sync_entered.is_set() is False

    finish_upload.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, timeout=1)

    assert order == [
        "old-upload-finished",
        "second-sync-entered",
    ]


@pytest.mark.asyncio
async def test_explicit_advisory_unlock_failure_is_controlled(
    monkeypatch,
    caplog,
) -> None:
    class UnlockFailureDb:
        @asynccontextmanager
        async def connection(self):
            class Connection:
                async def execute(self, query, *_args):
                    assert "pg_advisory_lock" in query

                async def fetchval(self, query, *_args):
                    assert "pg_advisory_unlock" in query
                    raise RuntimeError("simulated connection loss")

            yield Connection()

    monkeypatch.setattr(kb, "db_manager", UnlockFailureDb())
    monkeypatch.setattr(
        kb,
        "_LAW_DOCUMENT_SYNC_CONNECTION_SLOTS",
        asyncio.Semaphore(4),
    )
    body_completed = False

    with caplog.at_level("ERROR"):
        async with kb._law_document_server_sync_lock(
            "test",
            asyncio.Lock(),
        ):
            body_completed = True

    assert body_completed is True
    assert "Не удалось явно освободить advisory lock" in caplog.text
