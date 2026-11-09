"""Fail-closed orchestration regressions for enrichment operator CLIs."""

from contextlib import asynccontextmanager
import sys

import pytest

from scripts import build_law_catalog
from scripts import reenrich_synonyms
from services import law_document_lock
from services import law_embedding_service
from services import law_catalog_service


@pytest.mark.asyncio
async def test_reenrich_server_uses_document_then_catalog_lock_and_durable_run(
    monkeypatch,
):
    events = []
    manager = object()
    pending_claim = object()

    @asynccontextmanager
    async def document_lock(server_id, *, manager: object):
        assert server_id == "memphis"
        assert manager is manager_ref
        events.append("document_enter")
        try:
            yield
        finally:
            events.append("document_exit")

    @asynccontextmanager
    async def catalog_lock(server_id):
        assert server_id == "memphis"
        assert events == ["document_enter"]
        events.append("catalog_enter")
        try:
            yield
        finally:
            events.append("catalog_exit")

    class Catalog:
        @staticmethod
        async def begin_enrichment_run(server_id, *, invalidation):
            assert events == ["document_enter", "catalog_enter"]
            events.append(("begin", server_id, invalidation))
            return pending_claim

        @staticmethod
        async def run_verified_enrichment_for_server(
            server_id,
            *,
            only_stale,
            pending_claim: object,
        ):
            assert events[-1] == ("begin", "memphis", "all")
            events.append(
                (
                    "verified",
                    server_id,
                    only_stale,
                    pending_claim,
                )
            )
            return {"batches_failed": 0, "articles": 3}

    manager_ref = manager
    monkeypatch.setattr(
        law_document_lock,
        "law_document_server_lock",
        document_lock,
    )
    monkeypatch.setattr(
        law_embedding_service,
        "catalog_server_lock",
        catalog_lock,
    )

    result = await reenrich_synonyms.reenrich_server(
        "memphis",
        manager=manager,
        catalog_module=Catalog,
    )

    assert result == {"batches_failed": 0, "articles": 3}
    assert events == [
        "document_enter",
        "catalog_enter",
        ("begin", "memphis", "all"),
        ("verified", "memphis", True, pending_claim),
        "catalog_exit",
        "document_exit",
    ]


@pytest.mark.asyncio
async def test_reenrich_server_propagates_verified_failure(
    monkeypatch,
):
    events = []
    manager = object()
    pending_claim = object()

    @asynccontextmanager
    async def document_lock(_server_id, *, manager):
        events.append("document_enter")
        try:
            yield
        finally:
            events.append("document_exit")

    @asynccontextmanager
    async def catalog_lock(_server_id):
        events.append("catalog_enter")
        try:
            yield
        finally:
            events.append("catalog_exit")

    class Catalog:
        @staticmethod
        async def begin_enrichment_run(_server_id, *, invalidation):
            assert invalidation == "all"
            events.append("pending_persisted")
            return pending_claim

        @staticmethod
        async def run_verified_enrichment_for_server(
            _server_id,
            *,
            only_stale,
            pending_claim: object,
        ):
            assert only_stale is True
            events.append("verified_failed")
            raise RuntimeError("incomplete enrichment")

    monkeypatch.setattr(
        law_document_lock,
        "law_document_server_lock",
        document_lock,
    )
    monkeypatch.setattr(
        law_embedding_service,
        "catalog_server_lock",
        catalog_lock,
    )

    with pytest.raises(RuntimeError, match="incomplete enrichment"):
        await reenrich_synonyms.reenrich_server(
            "memphis",
            manager=manager,
            catalog_module=Catalog,
        )

    assert events == [
        "document_enter",
        "catalog_enter",
        "pending_persisted",
        "verified_failed",
        "catalog_exit",
        "document_exit",
    ]


@pytest.mark.asyncio
async def test_reenrich_main_failure_never_logs_ready(
    monkeypatch,
    caplog,
):
    import rate_limiter
    from database.manager import db_manager
    from services import sa_admin

    events = []

    async def connect():
        events.append("connect")

    async def close():
        events.append("close")

    async def fail(_server_id, *, manager):
        assert manager is db_manager
        events.append("failed")
        raise RuntimeError("verified failure")

    monkeypatch.setattr(rate_limiter, "init_client", lambda **_kwargs: None)
    monkeypatch.setattr(sa_admin, "project_id_for", lambda _path: "project")
    monkeypatch.setattr(db_manager, "connect", connect)
    monkeypatch.setattr(db_manager, "close", close)
    monkeypatch.setattr(reenrich_synonyms, "reenrich_server", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        ["reenrich_synonyms.py", "memphis"],
    )
    caplog.set_level("INFO", logger="reenrich_synonyms")

    with pytest.raises(RuntimeError, match="verified failure"):
        await reenrich_synonyms.main()

    assert events == ["connect", "failed", "close"]
    assert not any("ГОТОВО" in message for message in caplog.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["--retitle", "--reenrich"])
async def test_build_catalog_cli_rejects_no_enrich_invalidation_before_io(
    monkeypatch,
    flag,
):
    async def forbidden_connect():
        raise AssertionError("invalid flags must fail before database I/O")

    def forbidden_vertex():
        raise AssertionError("invalid flags must fail before Vertex init")

    monkeypatch.setattr(
        build_law_catalog.db_manager,
        "connect",
        forbidden_connect,
    )
    monkeypatch.setattr(
        build_law_catalog,
        "_init_vertex",
        forbidden_vertex,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build_law_catalog.py",
            "memphis",
            "--no-enrich",
            flag,
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        await build_law_catalog.main()

    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_build_catalog_failure_exits_before_ready_banner(
    monkeypatch,
    caplog,
):
    events = []

    async def connect():
        events.append("connect")

    async def close():
        events.append("close")

    async def fail(server_id, **_kwargs):
        assert server_id == "memphis"
        events.append("failed")
        raise RuntimeError("verified failure")

    monkeypatch.setattr(
        build_law_catalog.db_manager,
        "connect",
        connect,
    )
    monkeypatch.setattr(
        build_law_catalog.db_manager,
        "close",
        close,
    )
    monkeypatch.setattr(
        build_law_catalog,
        "_init_vertex",
        lambda: None,
    )
    monkeypatch.setattr(
        law_catalog_service,
        "build_catalog_for_server",
        fail,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["build_law_catalog.py", "memphis"],
    )
    caplog.set_level("INFO", logger="build_law_catalog")

    with pytest.raises(SystemExit) as exc_info:
        await build_law_catalog.main()

    assert exc_info.value.code == 1
    assert events == ["connect", "failed", "close"]
    assert not any(
        message.startswith("ГОТОВО")
        or message.startswith("DRY-RUN ГОТОВ")
        for message in caplog.messages
    )
