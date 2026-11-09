"""Regression tests for destructive law-document cleanup guards."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import knowledge_base_manager as kb
from knowledge_base_manager import (
    canonical_thread_url,
    discovered_thread_ids,
    extract_thread_id_from_url,
    filter_discovered_thread_urls,
)
from remote_parser_client import SectionDiscoveryResult


class _FakeBucket:
    pass


class _FakeStorageClient:
    def bucket(self, _name):
        return _FakeBucket()


class _FakeStorage:
    @staticmethod
    def from_service_account_json(_path):
        return _FakeStorageClient()


class _FakeDb:
    def __init__(self, records, deletion_records=None):
        self.records = []
        for raw_record in records:
            record = dict(raw_record)
            thread_id = record.get("source_thread_id")
            if thread_id is not None:
                digest = hashlib.md5(
                    str(thread_id).encode("utf-8")
                ).hexdigest()
                record.setdefault(
                    "gemini_file_uri",
                    f"gs://unused-bucket/test/{digest}.txt",
                )
            self.records.append(record)
        self.deletion_records = deletion_records or []
        self.executed = []
        self.transaction_events = []

    async def fetch(self, query, *args):
        if "source_thread_id IS NULL" in query:
            return []
        if (
            "SELECT source_thread_id" in query
            and "FROM law_documents WHERE server_id" in query
        ):
            return self.records
        if "SELECT title, gemini_file_uri" in query:
            return self.deletion_records
        raise AssertionError(f"unexpected fetch: {query} {args}")

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"

    @asynccontextmanager
    async def connection(self):
        class Connection:
            async def execute(_self, query, *_args):
                assert "pg_advisory_lock" in query

            async def fetchval(_self, query, *_args):
                assert "pg_advisory_unlock" in query
                return True

        yield Connection()

    @asynccontextmanager
    async def transaction(self):
        self.transaction_events.append("enter")
        connection = SimpleNamespace(is_in_transaction=lambda: True)
        try:
            yield connection
        except BaseException:
            self.transaction_events.append("rollback")
            raise
        else:
            self.transaction_events.append("commit")


async def _return_true():
    return True


def _configure_sync(monkeypatch, fake_db, *, sections=None) -> None:
    monkeypatch.setattr(kb, "reinitialize_vertex_ai", _return_true)
    monkeypatch.setattr(kb, "db_manager", fake_db)
    monkeypatch.setattr(kb.storage, "Client", _FakeStorage)
    monkeypatch.setattr(kb.config, "VERTEX_CREDENTIALS_FILE", "unused.json")
    monkeypatch.setattr(kb.config, "active_gcs_bucket", lambda: "unused-bucket")
    monkeypatch.setattr(kb.config, "SERVER_UPDATE_LOCKS", {"test": asyncio.Lock()})
    monkeypatch.setattr(
        kb.config,
        "SERVERS_CONFIG",
        {
            "test": {
                "forum_section_urls": sections
                or ["https://forum.example/forums/laws/"],
                "parse_only_author_posts": True,
            }
        },
    )


def test_discovered_thread_ids_extracts_every_topic_seen_in_section() -> None:
    urls = {
        "https://forum.example/threads/criminal-code.101/",
        "https://forum.example/threads/civil-code.202",
        "https://forum.example/threads/criminal-code-copy.101/",
    }

    assert discovered_thread_ids(urls) == {101, 202}


def test_topic_parse_failure_cannot_turn_discovered_document_into_deletion() -> None:
    database_thread_ids = {101, 202, 303}
    section_urls = {
        "https://forum.example/threads/first.101/",
        "https://forum.example/threads/temporarily-unreadable.202/",
        "https://forum.example/threads/third.303/",
    }
    successfully_parsed_thread_ids = {101, 303}

    discovered = discovered_thread_ids(section_urls)

    # Старый алгоритм ориентировался на успешно скачанные темы и ошибочно удалял
    # 202. Новый destructive cleanup ориентируется на discovery и не удаляет её.
    assert database_thread_ids - successfully_parsed_thread_ids == {202}
    assert database_thread_ids - discovered == set()


def test_only_topic_absent_from_section_is_a_deletion_candidate() -> None:
    database_thread_ids = {101, 202, 303}
    section_urls = {
        "https://forum.example/threads/first.101/",
        "https://forum.example/threads/third.303/",
    }

    assert database_thread_ids - discovered_thread_ids(section_urls) == {202}


def test_unrecognised_non_topic_url_never_invents_an_id() -> None:
    urls = {
        "https://forum.example/forums/laws/",
        "https://forum.example/threads/no-numeric-id/",
        "https://forum.example/resources/archive.999/",
    }

    assert discovered_thread_ids(urls) == set()


@pytest.mark.parametrize(
    "url",
    [
        "https://forum.example/threads/criminal-code.101/?order=desc",
        "https://forum.example/threads/criminal-code.101/#latest",
        "https://forum.example/threads/criminal-code.101/unread",
        "https://forum.example/threads/criminal-code.101/latest",
        "https://forum.example/threads/criminal-code.101/page-3",
        "https://forum.example/threads/criminal-code.101/post-9001",
    ],
)
def test_thread_url_variants_have_one_stable_identity(url) -> None:
    assert extract_thread_id_from_url(url) == 101
    assert canonical_thread_url(url) == (
        "https://forum.example/threads/criminal-code.101/"
    )


def test_excluded_thread_cannot_reenter_through_url_variant() -> None:
    discovered, excluded_count = filter_discovered_thread_urls(
        {
            "https://forum.example/threads/ignored.101/unread",
            "https://forum.example/threads/kept.202/?order=desc",
        },
        {"https://forum.example/threads/ignored.101/"},
    )

    assert excluded_count == 1
    assert discovered == {
        202: "https://forum.example/threads/kept.202/",
    }


@pytest.mark.asyncio
async def test_local_discovery_discards_first_page_if_later_page_fails(
    monkeypatch,
) -> None:
    class Element:
        def __init__(self, href):
            self.href = href

        async def get_attribute(self, _name):
            return self.href

    class Page:
        async def goto(self, url, **_kwargs):
            if "page-2" in url:
                raise RuntimeError("page 2 timeout")

            class Response:
                status = 200

            return Response()

        async def content(self):
            return "<html>" + ("x" * 1200) + "</html>"

        async def wait_for_selector(self, *_args, **_kwargs):
            return None

        async def query_selector_all(self, _selector):
            return [Element("/threads/law.101/")]

        async def query_selector(self, selector):
            if selector == "a.pageNav-jump--next":
                return Element("/forums/laws/page-2")
            return None

        async def close(self):
            return None

    class Context:
        async def new_page(self):
            return Page()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(kb.asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError, match="incomplete forum discovery"):
        await kb.get_law_urls_from_section(
            Context(),
            "https://forum.example/forums/laws/",
        )


@pytest.mark.asyncio
async def test_local_discovery_rejects_mixed_valid_and_malformed_thread_urls(
    monkeypatch,
) -> None:
    class Element:
        def __init__(self, href):
            self.href = href

        async def get_attribute(self, _name):
            return self.href

    class Page:
        async def goto(self, _url, **_kwargs):
            class Response:
                status = 200

            return Response()

        async def content(self):
            return "<html>" + ("x" * 1200) + "</html>"

        async def wait_for_selector(self, *_args, **_kwargs):
            return None

        async def query_selector_all(self, _selector):
            return [
                Element("/threads/valid.101/"),
                Element("/threads/new-unrecognised-format/"),
            ]

        async def query_selector(self, _selector):
            return None

        async def close(self):
            return None

    class Context:
        async def new_page(self):
            return Page()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(kb.asyncio, "sleep", no_sleep)

    with pytest.raises(RuntimeError, match="incomplete forum discovery"):
        await kb.get_law_urls_from_section(
            Context(),
            "https://forum.example/forums/laws/",
        )


def test_discovery_rejects_cross_origin_thread_anchor() -> None:
    with pytest.raises(RuntimeError, match="cross-origin"):
        kb.validated_discovery_thread_urls(
            [
                "/threads/valid.101/",
                "https://evil.example/threads/injected.202/",
            ],
            page_url="https://forum.example/forums/laws/",
            section_url="https://forum.example/forums/laws/",
        )


@pytest.mark.asyncio
async def test_remote_topic_failure_does_not_execute_document_delete(monkeypatch) -> None:
    """Проверяет wiring целого sync, а не только чистый URL-helper."""

    async def discover_section(_url):
        return SectionDiscoveryResult(
            urls=[
                "https://forum.example/threads/healthy.101/",
                "https://forum.example/threads/temporary-parser-error.202/",
            ],
            complete=True,
        )

    async def parse_topic(url, *_args, **_kwargs):
        if ".202/" in url:
            raise RuntimeError("temporary parser outage")
        return {
            "title": "Healthy law",
            "content": "unchanged",
            "thread_id": 101,
            "content_hash": "hash-101",
        }

    fake_db = _FakeDb([
        {"source_thread_id": 101, "content_hash": "hash-101"},
        {"source_thread_id": 202, "content_hash": "hash-202"},
    ])
    _configure_sync(monkeypatch, fake_db)
    monkeypatch.setattr(kb, "USE_REMOTE_PARSER", True)
    monkeypatch.setattr(kb, "check_remote_parser_available", _return_true)
    monkeypatch.setattr(kb, "remote_discover_section", discover_section)
    monkeypatch.setattr(kb, "remote_parse_topic", parse_topic)

    assert await kb.update_knowledge_base_for_server("test") is True
    assert not any("DELETE FROM law_documents" in query for query, _args in fake_db.executed)


@pytest.mark.asyncio
async def test_missing_old_thread_is_not_deleted_while_any_new_topic_is_unparsed(
    monkeypatch,
) -> None:
    async def discover_section(_url):
        return SectionDiscoveryResult(
            urls=[
                "https://forum.example/threads/healthy.101/",
                "https://forum.example/threads/possible-replacement.202/",
            ],
            complete=True,
        )

    async def parse_topic(url, *_args, **_kwargs):
        if ".202/" in url:
            return None
        return {
            "title": "Healthy law",
            "content": "unchanged",
            "thread_id": 101,
            "content_hash": "hash-101",
        }

    fake_db = _FakeDb([
        {"source_thread_id": 101, "content_hash": "hash-101"},
        {"source_thread_id": 303, "content_hash": "hash-303"},
    ])
    _configure_sync(monkeypatch, fake_db)
    monkeypatch.setattr(kb, "USE_REMOTE_PARSER", True)
    monkeypatch.setattr(kb, "check_remote_parser_available", _return_true)
    monkeypatch.setattr(kb, "remote_discover_section", discover_section)
    monkeypatch.setattr(kb, "remote_parse_topic", parse_topic)

    assert await kb.update_knowledge_base_for_server("test") is False
    assert not any(
        "DELETE FROM law_documents" in query
        for query, _args in fake_db.executed
    )


@pytest.mark.asyncio
async def test_one_incomplete_section_aborts_sync_before_cleanup(monkeypatch) -> None:
    sections = [
        "https://forum.example/forums/criminal/",
        "https://forum.example/forums/civil/",
    ]
    calls = []

    async def discover_section(url):
        calls.append(url)
        if url.endswith("/civil/"):
            return SectionDiscoveryResult([], False, "page 2 timeout")
        return SectionDiscoveryResult(
            ["https://forum.example/threads/criminal-code.101/"],
            True,
        )

    async def topic_must_not_run(*_args, **_kwargs):
        raise AssertionError("topic parsing must not start after partial discovery")

    fake_db = _FakeDb([
        {"source_thread_id": 101, "content_hash": "hash-101"},
        {"source_thread_id": 202, "content_hash": "hash-202"},
    ])
    _configure_sync(monkeypatch, fake_db, sections=sections)
    monkeypatch.setattr(kb, "USE_REMOTE_PARSER", True)
    monkeypatch.setattr(kb, "check_remote_parser_available", _return_true)
    monkeypatch.setattr(kb, "remote_discover_section", discover_section)
    monkeypatch.setattr(kb, "remote_parse_topic", topic_must_not_run)

    assert await kb.update_knowledge_base_for_server("test") is False
    assert calls == sections
    assert fake_db.executed == []


@pytest.mark.asyncio
async def test_topic_response_id_mismatch_is_not_written_or_used_for_cleanup(
    monkeypatch,
) -> None:
    async def discover_section(_url):
        return SectionDiscoveryResult(
            ["https://forum.example/threads/law.101/unread"],
            True,
        )

    async def parse_topic(*_args, **_kwargs):
        return {
            "title": "Wrong response",
            "content": "must not be stored",
            "thread_id": 202,
            "content_hash": "wrong-hash",
        }

    fake_db = _FakeDb([
        {"source_thread_id": 101, "content_hash": "hash-101"},
    ])
    _configure_sync(monkeypatch, fake_db)
    monkeypatch.setattr(kb, "USE_REMOTE_PARSER", True)
    monkeypatch.setattr(kb, "check_remote_parser_available", _return_true)
    monkeypatch.setattr(kb, "remote_discover_section", discover_section)
    monkeypatch.setattr(kb, "remote_parse_topic", parse_topic)

    assert await kb.update_knowledge_base_for_server("test") is True
    assert fake_db.executed == []


@pytest.mark.asyncio
async def test_confirmed_missing_topic_is_still_deleted(monkeypatch) -> None:
    async def discover_section(_url):
        return SectionDiscoveryResult(
            ["https://forum.example/threads/still-present.101/"],
            True,
        )

    async def parse_topic(*_args, **_kwargs):
        return {
            "title": "Still present",
            "content": "unchanged",
            "thread_id": 101,
            "content_hash": "hash-101",
        }

    @asynccontextmanager
    async def no_op_catalog_lock(_server_id):
        yield

    import services.law_embedding_service as embedding_service

    fake_db = _FakeDb(
        [
            {
                "source_thread_id": 101,
                "source_url": "https://forum.example/threads/still-present.101/",
                "title": "Still present",
                "content_hash": "hash-101",
                "derived_sync_pending": False,
            },
            {"source_thread_id": 202, "content_hash": "hash-202"},
        ],
        deletion_records=[
            {"title": "Actually removed", "gemini_file_uri": None},
        ],
    )
    _configure_sync(monkeypatch, fake_db)
    monkeypatch.setattr(kb, "USE_REMOTE_PARSER", True)
    monkeypatch.setattr(kb, "check_remote_parser_available", _return_true)
    monkeypatch.setattr(kb, "remote_discover_section", discover_section)
    monkeypatch.setattr(kb, "remote_parse_topic", parse_topic)
    monkeypatch.setattr(
        embedding_service,
        "catalog_server_lock",
        no_op_catalog_lock,
    )
    reconciliation_calls = []

    async def reconcile(
        conn,
        *,
        server_id,
        missing_thread_ids,
        current_thread_ids,
    ):
        assert conn.is_in_transaction() is True
        reconciliation_calls.append(
            (
                server_id,
                set(missing_thread_ids),
                set(current_thread_ids),
            )
        )
        removed = SimpleNamespace(
            document_id=20,
            thread_id=202,
            title="Actually removed",
            gcs_uri=None,
            handoff_target_thread_id=None,
        )
        return SimpleNamespace(
            target_thread_ids=(),
            removed_documents=(removed,),
            gcs_uris=(),
        )

    monkeypatch.setattr(
        kb,
        "reconcile_missing_thread_documents",
        reconcile,
    )

    assert await kb.update_knowledge_base_for_server("test") is True
    assert reconciliation_calls == [("test", {202}, {101})]
    assert fake_db.transaction_events == ["enter", "commit"]


@pytest.mark.asyncio
async def test_local_topic_failure_does_not_execute_document_delete(monkeypatch) -> None:
    class FakeContext:
        async def add_init_script(self, _script):
            return None

        def set_default_timeout(self, _timeout):
            return None

        def set_default_navigation_timeout(self, _timeout):
            return None

    class FakeBrowser:
        async def new_context(self, **_kwargs):
            return FakeContext()

        async def close(self):
            return None

    class FakeChromium:
        async def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightManager:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *_args):
            return None

    async def discover_local(_context, _url):
        return [
            "https://forum.example/threads/healthy.101/",
            "https://forum.example/threads/temporary-parser-error.202/latest",
        ]

    async def scrape_local(_context, url, *_args, **_kwargs):
        if ".202/" in url:
            raise RuntimeError("temporary local parser outage")
        return "Healthy law", "unchanged", 101

    fake_db = _FakeDb([
        {
            "source_thread_id": 101,
            "content_hash": kb.hashlib.md5(b"unchanged").hexdigest(),
        },
        {"source_thread_id": 202, "content_hash": "hash-202"},
    ])
    _configure_sync(monkeypatch, fake_db)
    monkeypatch.setattr(kb, "USE_REMOTE_PARSER", False)
    monkeypatch.setattr(kb, "async_playwright", lambda: FakePlaywrightManager())
    monkeypatch.setattr(kb, "get_law_urls_from_section", discover_local)
    monkeypatch.setattr(kb, "scrape_source", scrape_local)

    assert await kb.update_knowledge_base_for_server("test") is True
    assert not any(
        "DELETE FROM law_documents" in query
        for query, _args in fake_db.executed
    )
