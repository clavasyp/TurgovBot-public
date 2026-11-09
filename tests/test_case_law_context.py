"""Подборка статей для тяжёлых флоу (помощник, жалобы) вместо целых кодексов.

Раньше эти потоки грузили модели полные документы: помощник — выбранные роутером по
заголовкам, генератор жалоб — вообще все законы сервера (31-44 документа). На делах до
432 000 символов это било по цене, латентности и точности («потеря середины»).

Каталог статей есть не у всех серверов, поэтому главное здесь — не только то, что подборка
работает, но и то, что при её отсутствии всё честно откатывается на прежний режим.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
from services import case_law_context  # noqa: E402


@pytest.fixture()
def articles_mode(monkeypatch):
    monkeypatch.setattr(config, "ASSISTANT_LAW_CONTEXT_MODE", "articles", raising=False)
    monkeypatch.setattr(config, "ASSISTANT_LAW_CONTEXT_MAX_CHARS", 60_000, raising=False)


def test_query_puts_current_request_first(articles_mode):
    query = case_law_context.build_search_query(
        "что грозит клиенту?",
        facts="Клиента задержали за перевозку оружия без лицензии на трассе.",
        recent="Обсуждали порядок обыска автомобиля.",
    )
    assert query.startswith("что грозит клиенту?")
    assert "оружия без лицензии" in query
    assert "обыска автомобиля" in query.lower()


def test_query_survives_useless_request(articles_mode):
    # «продолжай» само по себе бесполезно для поиска — тему задаёт фабула дела
    query = case_law_context.build_search_query(
        "продолжай", facts="Дело о превышении скорости и отказе от освидетельствования."
    )
    assert "освидетельствования" in query


def test_long_inputs_are_capped(articles_mode):
    query = case_law_context.build_search_query(
        "вопрос", facts="Ф" * 50_000, recent="Р" * 50_000
    )
    assert len(query) < 5_000, "запрос для поиска не должен раздуваться историей"


def test_articles_returned_when_catalog_has_data(monkeypatch, articles_mode):
    async def fake_build(server_id, query, **kwargs):
        assert "оружие" in query
        return {
            "text": "ПОДБОРКА СТАТЕЙ\nСтатья 12.8 Незаконный оборот оружия…",
            "articles": [{"key": ("uk", "12.8")}],
            "keys": {("uk", "12.8")},
            "count": 1,
            "chars": 120,
            "dropped": 0,
            "legs": {"catalog": "ok", "vector": "ok"},
        }

    monkeypatch.setattr(case_law_context, "build_qa_context", fake_build)
    ctx = asyncio.run(case_law_context.law_context_for_case("davis", "нашли оружие"))
    assert ctx and ctx["count"] == 1
    assert "12.8" in ctx["text"]


def test_empty_catalog_falls_back(monkeypatch, articles_mode):
    """Сервер без каталога → None, вызывающий уходит на целые документы."""

    async def fake_build(server_id, query, **kwargs):
        return None

    monkeypatch.setattr(case_law_context, "build_qa_context", fake_build)
    assert asyncio.run(case_law_context.law_context_for_case("new_york", "вопрос")) is None


def test_search_failure_falls_back(monkeypatch, articles_mode):
    """Падение поиска не должно оставлять модель вообще без законов."""

    async def boom(server_id, query, **kwargs):
        raise RuntimeError("pgvector недоступен")

    monkeypatch.setattr(case_law_context, "build_qa_context", boom)
    assert asyncio.run(case_law_context.law_context_for_case("davis", "вопрос")) is None


def test_documents_mode_disables_articles(monkeypatch):
    monkeypatch.setattr(config, "ASSISTANT_LAW_CONTEXT_MODE", "documents", raising=False)

    async def should_not_be_called(*a, **kw):
        raise AssertionError("в режиме documents подборка статей не должна вызываться")

    monkeypatch.setattr(case_law_context, "build_qa_context", should_not_be_called)
    assert case_law_context.articles_mode_enabled() is False
    assert asyncio.run(case_law_context.law_context_for_case("davis", "вопрос")) is None


def test_history_facts_and_recent():
    history = [
        {"sender_role": "user", "content": "Фабула: задержание на месте ДТП"},
        {"sender_role": "ai", "content": "Разбор"},
        {"sender_role": "user", "content": "А что со сроками?"},
    ]
    facts, recent = case_law_context.history_facts_and_recent(history)
    assert facts.startswith("Фабула")
    assert "сроками" in recent
    assert case_law_context.history_facts_and_recent([]) == ("", "")
