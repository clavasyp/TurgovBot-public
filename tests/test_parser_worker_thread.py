"""Структурный разбор темы воркером (/parse-thread) и его конвертация в формат судебного дела.

Путь появился потому, что forum.gta5rp.com за Cloudflare отдаёт локальному браузеру бота 403:
разбор дела и формы жалобы переехали на транспорт воркера (FlareSolverr/троттлинг).
"""
import asyncio
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

_ROOT = Path(__file__).resolve().parent.parent
for path in (str(_ROOT), str(_ROOT / "parser_worker")):
    if path not in sys.path:
        sys.path.insert(0, path)

import worker  # noqa: E402  (parser_worker/worker.py)
import court_case_parser as ccp  # noqa: E402


THREAD_HTML = """
<h1 class="p-title-value"><span class="prefix">Важно</span>Иск №997</h1>
<div class="block-importantMessage">
  <article class="message" data-content="post-1"><div class="bbWrapper">дубль сверху</div></article>
</div>
<article class="message message--highlighted" data-content="post-2">
  <div class="bbWrapper">подсвеченный дубль</div>
</article>
<article class="message" data-content="post-100">
  <a class="username">Вавилонов</a><time datetime="2026-07-01T10:00:00+0000"></time>
  <div class="bbWrapper">Исковое заявление
    <blockquote class="bbCodeBlock--quote">цитата ответчика</blockquote>
    <div class="bbCodeSpoiler"><div class="bbCodeSpoiler-content">скрытые доказательства</div></div>
    <img src="https://forum.gta5rp.com/attachments/skan-1.jpg">
    <img data-src="/proxy.php?image=https%3A%2F%2Fi.imgur.com%2Fabc.png&amp;hash=x" src="/styles/blank.gif">
    <img src="https://forum.gta5rp.com/data/avatars/s/1/1.jpg">
    <img src="https://forum.gta5rp.com/styles/default/xenforo/smilies/emoji.png">
  </div>
</article>
<article class="message" data-content="post-101">
  <a class="username">Филатов</a><time datetime="2026-07-02T11:00:00+0000"></time>
  <div class="bbWrapper">Решение суда</div>
</article>
"""

PAGE_URL = "https://forum.gta5rp.com/threads/isk.997/"


@pytest.fixture()
def messages():
    soup = BeautifulSoup(THREAD_HTML, "html.parser")
    return worker._messages_from_soup(soup, PAGE_URL, 0)


def test_skips_duplicate_and_non_post_blocks(messages):
    # XenForo дублирует важное/выделенное сообщение сверху — в деле это лишние копии
    assert [m["order"] for m in messages] == [1, 2]
    assert [m["author"] for m in messages] == ["Вавилонов", "Филатов"]
    assert messages[0]["timestamp"].startswith("2026-07-01")


def test_keeps_quotes_and_spoilers(messages):
    # Цитата — позиция стороны по делу, спойлер часто прячет доказательства.
    # Спойлер лежит в HTML, поэтому кликать (как делал локальный браузер) не нужно.
    assert "цитата ответчика" in messages[0]["text"]
    assert "скрытые доказательства" in messages[0]["text"]


def test_extracts_attachments_and_proxied_images(messages):
    assert messages[0]["images"] == [
        "https://forum.gta5rp.com/attachments/skan-1.jpg",
        "https://i.imgur.com/abc.png",   # раскодировано из /proxy.php?image=...
    ]
    assert messages[1]["images"] == []


def test_thread_prefix_stripped_from_title():
    soup = BeautifulSoup(THREAD_HTML, "html.parser")
    title_el = soup.select_one("h1.p-title-value")
    assert worker._clean_topic_title(title_el, True) == "Иск №997"


def test_xenforo_label_badge_stripped_from_title():
    # gta5rp метит темы не префиксом, а бейджем span.label + span.label-append (пробел)
    html = ('<h1 class="p-title-value"><span class="label label--red">Важно</span>'
            '<span class="label-append"> </span>Регламент подачи обращений</h1>')
    title_el = BeautifulSoup(html, "html.parser").select_one("h1.p-title-value")
    assert worker._clean_topic_title(title_el, True) == "Регламент подачи обращений"


def test_worker_payload_converts_to_case(monkeypatch):
    async def fake_remote(url, max_pages=None):
        return {
            "title": "Иск №997",
            "thread_id": 997,
            "truncated": False,
            "messages": [
                {"order": 1, "author": "Вавилонов", "timestamp": "2026-07-01T10:00:00+0000",
                 "text": "Прошу взыскать", "images": ["https://x/s1.jpg"]},
                {"order": 2, "author": "Филатов", "timestamp": None,
                 "text": "Решение", "images": []},
            ],
        }

    import remote_parser_client
    monkeypatch.setattr(remote_parser_client, "remote_parse_thread", fake_remote)

    case = asyncio.run(ccp._case_from_worker(PAGE_URL))
    assert case["thread_id"] == "997"
    assert case["thread_title"] == "Иск №997"
    assert case["plaintiff"] == "Вавилонов"
    assert case["messages"][0]["images"] == [
        {"bbcode": "[IMG]https://x/s1.jpg[/IMG]", "url": "https://x/s1.jpg"}
    ]
    assert case["messages"][0]["has_images"] is True
    assert case["messages"][1]["has_images"] is False


def test_worker_failure_falls_back(monkeypatch):
    async def fake_remote(url, max_pages=None):
        return None

    import remote_parser_client
    monkeypatch.setattr(remote_parser_client, "remote_parse_thread", fake_remote)
    # None → parse_court_case_from_url уходит на локальный Playwright-путь
    assert asyncio.run(ccp._case_from_worker(PAGE_URL)) is None
