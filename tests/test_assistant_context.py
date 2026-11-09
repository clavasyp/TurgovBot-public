"""Ролевой помощник: задания инструментов и ограничители контекста.

Что чинилось (проверено на production 2026-07-28):
- в словаре промптов был единственный ключ «audit», которого нет ни у одной кнопки, поэтому
  все 7 инструментов адвоката/прокурора получали пустую строку — и она затирала последний
  ход, где написано, какой инструмент нажали (1395 нажатий по 835 делам);
- история дела грузилась целиком без лимита и переотправлялась каждым ходом: дела доходят
  до 432 000 символов при 135 сообщениях.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402
from prompts import get_assistant_tool_prompt  # noqa: E402
from user_handlers.assistant_flow import (  # noqa: E402
    ASSISTANT_TOOLS,
    _trim_case_history,
)


def _button_tool_names() -> set[str]:
    """Имена инструментов ровно так, как их получает обработчик: callback_data без «tool_»."""
    names: set[str] = set()
    for stages in ASSISTANT_TOOLS.values():
        for buttons in stages.values():
            for button in buttons:
                assert button.callback_data.startswith("tool_")
                names.add(button.callback_data.split("_", 1)[1])
    return names


def test_every_button_has_a_real_task():
    names = _button_tool_names()
    assert names, "кнопки инструментов должны быть определены"
    for name in sorted(names):
        prompt = get_assistant_tool_prompt(name)
        assert prompt.strip(), f"инструмент {name} остался без задания"
        # Пустое/куцое задание — это и был баг: модель отвечала «вообще по делу»
        assert len(prompt) > 300, f"задание инструмента {name} подозрительно короткое"


def test_unknown_tool_falls_back_instead_of_empty_prompt():
    prompt = get_assistant_tool_prompt("tool_from_the_future")
    assert prompt.strip(), "неизвестный инструмент не должен давать пустой запрос"


def test_tool_prompts_demand_citations():
    for name in sorted(_button_tool_names()):
        prompt = get_assistant_tool_prompt(name)
        assert "стать" in prompt.lower(), f"{name}: нет требования ссылаться на статьи"


# --- Обрезка истории -------------------------------------------------------------------

def _msg(role: str, size: int, fill: str = "x") -> dict:
    return {"sender_role": role, "content": fill * size}


@pytest.fixture()
def limit_120k(monkeypatch):
    monkeypatch.setattr(config, "ASSISTANT_HISTORY_MAX_CHARS", 120_000, raising=False)
    return 120_000


def test_short_case_is_untouched(limit_120k):
    history = [_msg("user", 500), _msg("ai", 800)]
    assert _trim_case_history(history, 1) == history


def test_long_case_keeps_facts_and_recent_turns(limit_120k):
    facts = _msg("user", 5_000, "Ф")
    history = [facts] + [_msg("ai" if i % 2 else "user", 20_000) for i in range(20)]

    trimmed = _trim_case_history(history, 42)

    assert trimmed[0] is facts, "фабула дела должна остаться"
    assert trimmed[-1] is history[-1], "последний ход должен остаться"
    assert "Служебная пометка" in trimmed[1]["content"], "нужен маркер об опущенных ходах"
    total = sum(len(m["content"]) for m in trimmed)
    assert total <= limit_120k + 1_000, f"обрезка не уложилась в лимит: {total}"
    assert len(trimmed) < len(history)


def test_disabled_limit_restores_previous_behaviour(monkeypatch):
    monkeypatch.setattr(config, "ASSISTANT_HISTORY_MAX_CHARS", 0, raising=False)
    history = [_msg("user", 50_000) for _ in range(20)]
    assert _trim_case_history(history, 1) == history


def test_case_of_one_huge_message_does_not_crash(limit_120k):
    # Реальный максимум одного сообщения на проде — 143 265 символов, больше лимита целиком
    history = [_msg("user", 143_265, "Ф"), _msg("ai", 2_000)]
    trimmed = _trim_case_history(history, 7)
    assert trimmed[0] is history[0]
    assert trimmed[-1] is history[-1]
