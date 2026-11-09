"""The parser worker must fail closed on partial section discovery."""

from __future__ import annotations

import asyncio

import pytest

import parser_worker.worker as worker


class _Response:
    status = 200


class _EmptySectionPage:
    async def goto(self, *_args, **_kwargs):
        return _Response()

    async def content(self):
        return "<html>" + ("x" * 1200) + "</html>"

    async def wait_for_selector(self, *_args, **_kwargs):
        return None

    async def query_selector_all(self, _selector):
        return []

    async def close(self):
        return None


class _Anchor:
    def __init__(self, href):
        self.href = href

    async def get_attribute(self, _name):
        return self.href


class _MixedSectionPage(_EmptySectionPage):
    async def query_selector_all(self, _selector):
        return [
            _Anchor("/threads/valid.101/"),
            _Anchor("/threads/new-unrecognised-format/"),
        ]


class _Context:
    def __init__(self, page_type=_EmptySectionPage):
        self.page_type = page_type

    async def new_page(self):
        return self.page_type()


def _install_worker_limits(monkeypatch, page_type=_EmptySectionPage) -> None:
    monkeypatch.setattr(worker, "section_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(worker, "page_semaphore", asyncio.Semaphore(1))
    monkeypatch.setattr(worker, "browser_context", _Context(page_type))

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(worker.asyncio, "sleep", no_sleep)


@pytest.mark.asyncio
async def test_playwright_empty_page_is_incomplete_not_success(monkeypatch) -> None:
    _install_worker_limits(monkeypatch)

    with pytest.raises(RuntimeError, match="no topic links"):
        await worker.parse_section_urls(
            "https://forum.example/forums/laws/",
        )


@pytest.mark.asyncio
async def test_playwright_malformed_thread_anchor_discards_valid_partial_set(
    monkeypatch,
) -> None:
    _install_worker_limits(monkeypatch, _MixedSectionPage)

    with pytest.raises(RuntimeError, match="unrecognised thread URL"):
        await worker.parse_section_urls(
            "https://forum.example/forums/laws/",
        )


@pytest.mark.asyncio
async def test_flaresolverr_failure_after_first_page_discards_partial_result(
    monkeypatch,
) -> None:
    _install_worker_limits(monkeypatch)
    calls = 0
    first_page = """
        <html>
          <div class="structItem-title">
            <a href="/threads/law.101/">Law</a>
          </div>
          <a class="pageNav-jump--next" href="/forums/laws/page-2">Next</a>
        </html>
    """

    async def flaresolverr_get(_url):
        nonlocal calls
        calls += 1
        return first_page if calls == 1 else None

    monkeypatch.setattr(worker, "_flaresolverr_get", flaresolverr_get)

    with pytest.raises(RuntimeError, match="did not return discovery HTML"):
        await worker._parse_section_flaresolverr(
            "https://forum.gta5rp.com/forums/laws/",
        )

    assert calls == 2  # first page + failed page 2; partial URLs are discarded


@pytest.mark.asyncio
async def test_flaresolverr_malformed_thread_anchor_discards_valid_partial_set(
    monkeypatch,
) -> None:
    _install_worker_limits(monkeypatch)
    html = """
        <html>
          <div class="structItem-title">
            <a href="/threads/valid.101/">Valid</a>
          </div>
          <div class="structItem-title">
            <a href="/threads/new-unrecognised-format/">Malformed</a>
          </div>
        </html>
    """

    async def flaresolverr_get(_url):
        return html

    monkeypatch.setattr(worker, "_flaresolverr_get", flaresolverr_get)

    with pytest.raises(RuntimeError, match="unrecognised thread URL"):
        await worker._parse_section_flaresolverr(
            "https://forum.gta5rp.com/forums/laws/",
        )


@pytest.mark.asyncio
async def test_worker_api_attests_only_successful_complete_discovery(
    monkeypatch,
) -> None:
    async def complete(_url):
        return ["https://forum.example/threads/law.101/"]

    monkeypatch.setattr(worker, "parse_section_urls", complete)
    monkeypatch.setattr(worker, "verify_secret", lambda _secret: None)

    response = await worker.api_parse_section(
        worker.ParseSectionRequest(
            section_url="https://forum.example/forums/laws/",
            secret_key="secret",
        )
    )

    assert response.success is True
    assert response.complete is True
    assert response.urls == ["https://forum.example/threads/law.101/"]


@pytest.mark.asyncio
async def test_worker_api_never_attests_failed_discovery(monkeypatch) -> None:
    async def incomplete(_url):
        raise RuntimeError("page 2 failed")

    monkeypatch.setattr(worker, "parse_section_urls", incomplete)
    monkeypatch.setattr(worker, "verify_secret", lambda _secret: None)

    response = await worker.api_parse_section(
        worker.ParseSectionRequest(
            section_url="https://forum.example/forums/laws/",
            secret_key="secret",
        )
    )

    assert response.success is False
    assert response.complete is False
    assert response.urls == []
    assert response.error == "page 2 failed"
