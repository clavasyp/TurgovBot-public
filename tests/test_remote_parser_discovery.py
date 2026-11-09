"""Contract tests for completeness-aware remote section discovery."""

from __future__ import annotations

import httpx
import pytest

from remote_parser_client import RemoteParserClient


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _HttpClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    async def post(self, *_args, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.response


def _install_http(monkeypatch, parser, http_client):
    async def get_client():
        return http_client

    monkeypatch.setattr(parser, "_get_client", get_client)


@pytest.mark.asyncio
async def test_successful_discovery_is_explicitly_complete(monkeypatch) -> None:
    parser = RemoteParserClient("http://parser.invalid", "secret")
    _install_http(
        monkeypatch,
        parser,
        _HttpClient(
            _Response(
                200,
                {
                    "success": True,
                    "complete": True,
                    "urls": ["https://forum.example/threads/law.101/"],
                },
            )
        ),
    )

    result = await parser.discover_section_urls("https://forum.example/forums/laws/")

    assert result.complete is True
    assert result.error is None
    assert result.urls == ["https://forum.example/threads/law.101/"]


@pytest.mark.asyncio
async def test_old_worker_without_completeness_attestation_fails_closed(
    monkeypatch,
) -> None:
    parser = RemoteParserClient("http://parser.invalid", "secret")
    _install_http(
        monkeypatch,
        parser,
        _HttpClient(
            _Response(
                200,
                {
                    "success": True,
                    "urls": ["https://forum.example/threads/partial.101/"],
                },
            )
        ),
    )

    result = await parser.discover_section_urls(
        "https://forum.example/forums/laws/"
    )

    assert result.complete is False
    assert result.urls == []
    assert result.error == "worker did not attest complete discovery"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (_Response(200, {"success": False, "error": "page 2 failed"}), "page 2 failed"),
        (_Response(503, {}), "HTTP 503"),
    ],
)
async def test_worker_or_http_failure_is_not_an_empty_complete_section(
    monkeypatch,
    response,
    expected_error,
) -> None:
    parser = RemoteParserClient("http://parser.invalid", "secret")
    _install_http(monkeypatch, parser, _HttpClient(response))

    result = await parser.discover_section_urls("https://forum.example/forums/laws/")

    assert result.complete is False
    assert result.urls == []
    assert result.error == expected_error


@pytest.mark.asyncio
async def test_timeout_is_incomplete_but_legacy_list_api_remains_compatible(
    monkeypatch,
) -> None:
    parser = RemoteParserClient("http://parser.invalid", "secret")
    _install_http(
        monkeypatch,
        parser,
        _HttpClient(error=httpx.ReadTimeout("timeout")),
    )

    result = await parser.discover_section_urls("https://forum.example/forums/laws/")
    urls = await parser.parse_section_urls("https://forum.example/forums/laws/")

    assert result.complete is False
    assert result.error == "timeout"
    assert urls == []
