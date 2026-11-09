"""Загрузка вложений воркером (/fetch-image).

Cloudflare отдаёт боту 403 не только на страницы forum.gta5rp.com, но и на /attachments/*,
поэтому сканы судебных решений не доходили до OCR. Воркер качает их с cf_clearance,
полученным от FlareSolverr; для остальных хостов путь остаётся прямым.
"""
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parent.parent
for path in (str(_ROOT), str(_ROOT / "parser_worker")):
    if path not in sys.path:
        sys.path.insert(0, path)

import worker  # noqa: E402


class _Resp:
    def __init__(self, status=200, content=b"IMG", content_type="image/png"):
        self.status_code = status
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


@pytest.fixture()
def captured(monkeypatch):
    """Подменяет httpx-клиент и cf-креды; возвращает список выполненных запросов."""
    calls = {"gets": [], "creds": 0}

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, cookies=None):
            cookies = dict(cookies or {})
            calls["gets"].append({"url": url, "cookies": cookies,
                                  "ua": (headers or {}).get("User-Agent")})
            if "gta5rp" in url and cookies.get("cf_clearance") != "NEW":
                return _Resp(403, b"", "text/html")
            return _Resp()

    async def _creds(url, force_refresh=False):
        calls["creds"] += 1
        return ({"cf_clearance": "OLD"} if calls["creds"] == 1 else {"cf_clearance": "NEW"}), "FS-UA/1.0"

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(worker, "_cf_credentials", _creds)
    monkeypatch.setattr(worker, "FLARESOLVERR_URL", "http://flaresolverr:8191/v1")
    monkeypatch.setattr(worker, "CF_PROTECTED_HOSTS", ["forum.gta5rp.com"])
    return calls


def test_plain_host_downloads_directly(captured):
    content, content_type = asyncio.run(worker.fetch_binary("https://i.imgur.com/a.png"))
    assert content == b"IMG" and content_type == "image/png"
    # Никаких cookies и обращений к FlareSolverr для обычных хостов
    assert captured["gets"][0]["cookies"] == {}
    assert captured["creds"] == 0


def test_cloudflare_host_retries_with_fresh_clearance(captured):
    result = asyncio.run(worker.fetch_binary("https://forum.gta5rp.com/attachments/skan.jpg"))
    assert result is not None and result[0] == b"IMG"
    # Первый заход — с протухшим клиренсом (403), второй — с обновлённым
    assert [g["cookies"]["cf_clearance"] for g in captured["gets"]] == ["OLD", "NEW"]
    assert captured["gets"][0]["ua"] == "FS-UA/1.0"


def test_referer_defaults_to_origin(captured):
    asyncio.run(worker.fetch_binary("https://i.imgur.com/a.png"))
    assert captured["gets"], "запрос должен был уйти"
