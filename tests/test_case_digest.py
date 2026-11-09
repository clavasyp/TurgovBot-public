"""Карта дела: сжатие старой части консультации вместо её выбрасывания.

До этого длинное дело резалось по лимиту символов — середина исчезала вместе с эпизодами,
именами, датами и уже принятыми решениями (на проде дела доходят до 432 000 символов при
135 сообщениях). Теперь старая часть уходит в сводку, которая обновляется инкрементально.

Проверяется главным образом не «красиво сжали», а инварианты, нарушение которых тихо портит
дело: фабула и текущий вопрос на месте, покрытие карты не завышается, любая неудача честно
откатывается на прежнюю обрезку.
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
from services import case_digest  # noqa: E402

LIMIT = 120_000
RESERVE = 12_000


def _msg(mid: int, role: str, size: int, fill: str = "x") -> dict:
    return {"id": mid, "sender_role": role, "content": fill * size}


def _long_case(turns: int = 20, size: int = 20_000) -> list[dict]:
    """Фабула + turns ходов: суммарно заведомо больше лимита."""
    facts = _msg(1, "user", 5_000, "Ф")
    return [facts] + [
        _msg(i + 2, "ai" if i % 2 else "user", size) for i in range(turns)
    ]


@pytest.fixture()
def digest_on(monkeypatch):
    monkeypatch.setattr(config, "ASSISTANT_HISTORY_MAX_CHARS", LIMIT, raising=False)
    monkeypatch.setattr(config, "ASSISTANT_DIGEST_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ASSISTANT_DIGEST_MAX_CHARS", RESERVE, raising=False)
    monkeypatch.setattr(config, "ASSISTANT_DIGEST_SOURCE_MAX_CHARS", 250_000, raising=False)
    monkeypatch.setattr(config, "ASSISTANT_DIGEST_MAX_PASSES", 2, raising=False)


@pytest.fixture()
def no_db(monkeypatch):
    """Карта в БД отсутствует, запись перехвачена — тесты не ходят в базу."""
    saved: list[dict] = []

    async def fake_get(case_id):
        return None

    async def fake_save(**kwargs):
        saved.append(kwargs)
        return True

    monkeypatch.setattr(case_digest.assistant_logger, "get_case_digest", fake_get)
    monkeypatch.setattr(case_digest.assistant_logger, "save_case_digest", fake_save)
    return saved


# --- Планировщик -----------------------------------------------------------------------

def test_short_case_needs_no_digest(digest_on):
    history = [_msg(1, "user", 500), _msg(2, "ai", 800)]
    plan = case_digest.plan_case_context(history, None, limit=LIMIT, reserve=RESERVE)
    assert plan.mode == "full"
    assert case_digest.assemble(plan) == history


def test_long_case_asks_for_digest(digest_on):
    history = _long_case()
    plan = case_digest.plan_case_context(history, None, limit=LIMIT, reserve=RESERVE)

    assert plan.mode == "refresh"
    assert plan.head == [history[0]], "фабула дела подаётся сырой всегда"
    assert plan.tail[-1] is history[-1], "текущий ход обязан остаться"
    assert plan.to_summarize, "старая часть дела должна уйти в карту"
    # Сжимаем и показываем ровно всё, ничего не пропадает бесследно
    assert len(plan.to_summarize) + len(plan.tail) == len(history) - 1


def test_existing_digest_is_reused_without_regeneration(digest_on):
    history = _long_case()
    stored = {"digest": "К" * 5_000, "covered_message_id": 18, "covered_count": 18}

    plan = case_digest.plan_case_context(history, stored, limit=LIMIT, reserve=RESERVE)

    assert plan.mode == "digest", "пока карта покрывает старую часть, сжимать заново незачем"
    assert all(m["id"] > 18 for m in plan.tail), "сырыми идут только сообщения новее карты"
    assert plan.tail[-1] is history[-1]


def test_stale_digest_is_refreshed_only_on_new_messages(digest_on):
    history = _long_case()
    stored = {"digest": "К" * 5_000, "covered_message_id": 5, "covered_count": 5}

    plan = case_digest.plan_case_context(history, stored, limit=LIMIT, reserve=RESERVE)

    assert plan.mode == "refresh"
    assert all(m["id"] > 5 for m in plan.to_summarize), \
        "повторно сжимать уже покрытые сообщения нельзя — это лишний вызов модели"


def test_digest_is_not_rewritten_for_a_couple_of_lines(digest_on):
    """Дело, где одна фабула больше лимита: карту нельзя пересобирать каждым ходом.

    Каждое переписывание карты — вызов модели и риск растерять детали. Пара новых реплик
    ждёт следующего обновления.
    """
    history = [_msg(1, "user", 143_265, "Ф"), _msg(2, "ai", 900), _msg(3, "user", 300)]
    stored = {"digest": "К" * 3_000, "covered_message_id": 1, "covered_count": 1}

    plan = case_digest.plan_case_context(history, stored, limit=LIMIT, reserve=RESERVE)

    assert plan.mode == "digest"
    assert plan.tail[-1] is history[-1]


def test_assembled_context_fits_the_budget(digest_on):
    history = _long_case()
    plan = case_digest.plan_case_context(history, None, limit=LIMIT, reserve=RESERVE)
    messages = case_digest.assemble(plan, digest_text="К" * RESERVE, covered_count=10)

    assert messages[0] is history[0]
    assert messages[-1] is history[-1]
    assert "КАРТА ДЕЛА" in messages[1]["content"]
    total = sum(len(m["content"]) for m in messages)
    assert total <= LIMIT, f"контекст не уложился в лимит: {total}"


def test_huge_single_message_still_goes_through(digest_on):
    # Реальный максимум одного сообщения на проде — 143 265 символов, больше всего лимита
    history = [_msg(1, "user", 143_265, "Ф"), _msg(2, "ai", 2_000), _msg(3, "user", 300)]
    plan = case_digest.plan_case_context(history, None, limit=LIMIT, reserve=RESERVE)
    messages = case_digest.assemble(plan, digest_text="К" * 100, covered_count=1)
    assert messages[0] is history[0]
    assert messages[-1] is history[-1]


def test_digest_does_not_thrash_on_a_growing_case(digest_on):
    """Карта не должна пересобираться почти каждым ходом.

    Если набивать сырой хвост под самый лимит, следующее же сообщение снова его
    переполняет: на профиле самого большого дела прода (432 000 симв / 135 ходов) это
    давало 50 пересборок вместо ~8, то есть вызов модели через ход.
    """
    history = [_msg(1, "user", 6_000, "Ф")]
    stored = None
    refreshes = 0

    for turn in range(2, 136):
        history.append(_msg(turn, "ai" if turn % 2 == 0 else "user", 3_200))
        plan = case_digest.plan_case_context(history, stored, limit=LIMIT, reserve=RESERVE)
        assert plan.mode in ("full", "digest", "refresh")
        if plan.mode == "refresh":
            refreshes += 1
            stored = {"digest": "К" * 9_000,
                      "covered_message_id": plan.to_summarize[-1]["id"],
                      "covered_count": len(plan.to_summarize)}
        # Что бы ни решил планировщик, текущий вопрос обязан уйти модели
        assert (plan.tail or plan.head)[-1] is history[-1]

    assert refreshes <= 12, f"слишком частая пересборка карты: {refreshes}"


# --- Нарезка исходника -----------------------------------------------------------------

def test_chunking_never_splits_a_message(digest_on):
    messages = [_msg(i, "user", 30_000) for i in range(1, 11)]
    chunks = case_digest.chunk_messages(messages, 100_000)

    assert sum(len(c) for c in chunks) == len(messages)
    assert [m["id"] for c in chunks for m in c] == [m["id"] for m in messages]
    for chunk in chunks:
        assert len(chunk) == 1 or sum(len(m["content"]) for m in chunk) <= 100_000


def test_oversized_message_goes_alone(digest_on):
    messages = [_msg(1, "user", 500), _msg(2, "user", 400_000), _msg(3, "user", 500)]
    chunks = case_digest.chunk_messages(messages, 250_000)
    assert [m["id"] for m in chunks[1]] == [2]


# --- Полный путь -----------------------------------------------------------------------

def test_build_uses_digest_and_saves_coverage(monkeypatch, digest_on, no_db):
    async def fake_generate(messages, **kwargs):
        return "Суть дела: задержание на трассе.", list(messages)

    monkeypatch.setattr(case_digest, "generate_digest", fake_generate)
    history = _long_case()

    messages = asyncio.run(case_digest.build_case_messages(1, history, rp_role="lawyer"))

    assert messages[0] is history[0]
    assert messages[-1] is history[-1]
    assert "задержание на трассе" in messages[1]["content"]
    assert len(no_db) == 1, "карта должна сохраниться в БД"
    assert no_db[0]["covered_message_id"] > 0


def test_partial_coverage_is_not_overstated(monkeypatch, digest_on, no_db):
    """Если модель успела сжать лишь часть — покрытие пишем по факту.

    Завышенное covered_message_id — самый опасный сценарий: несжатые сообщения навсегда
    выпадут и из карты, и из сырого хвоста, а дело будет выглядеть целым.
    """
    async def half_generate(messages, **kwargs):
        half = list(messages)[: max(1, len(messages) // 2)]
        return "Частичная карта", half

    monkeypatch.setattr(case_digest, "generate_digest", half_generate)
    history = _long_case()
    plan = case_digest.plan_case_context(history, None, limit=LIMIT, reserve=RESERVE)

    asyncio.run(case_digest.build_case_messages(2, history))

    covered_id = no_db[0]["covered_message_id"]
    assert covered_id < plan.to_summarize[-1]["id"]


def test_model_failure_falls_back_to_trimming(monkeypatch, digest_on, no_db):
    async def failing_generate(messages, **kwargs):
        return None, []

    monkeypatch.setattr(case_digest, "generate_digest", failing_generate)
    history = _long_case()

    messages = asyncio.run(case_digest.build_case_messages(3, history))

    assert messages[0] is history[0]
    assert messages[-1] is history[-1]
    assert "Служебная пометка" in messages[1]["content"], "должна сработать прежняя обрезка"
    assert not no_db, "пустую карту сохранять нельзя"


def test_old_digest_survives_failed_refresh(monkeypatch, digest_on):
    """Не смогли обновить карту — старая всё равно лучше выброшенной середины."""
    async def fake_get(case_id):
        return {"digest": "Старая карта дела", "covered_message_id": 5, "covered_count": 5}

    async def failing_generate(messages, **kwargs):
        return None, []

    monkeypatch.setattr(case_digest.assistant_logger, "get_case_digest", fake_get)
    monkeypatch.setattr(case_digest, "generate_digest", failing_generate)
    history = _long_case()

    messages = asyncio.run(case_digest.build_case_messages(4, history))

    assert any("Старая карта дела" in (m.get("content") or "") for m in messages)
    assert messages[-1] is history[-1]


def test_disabled_flag_restores_previous_behaviour(monkeypatch, digest_on):
    monkeypatch.setattr(config, "ASSISTANT_DIGEST_ENABLED", False, raising=False)

    async def should_not_be_called(case_id):
        raise AssertionError("при выключенной карте в БД ходить незачем")

    monkeypatch.setattr(case_digest.assistant_logger, "get_case_digest", should_not_be_called)
    history = _long_case()

    messages = asyncio.run(case_digest.build_case_messages(5, history))

    assert messages[0] is history[0]
    assert messages[-1] is history[-1]
    assert "Служебная пометка" in messages[1]["content"]


def test_history_without_ids_does_not_break(monkeypatch, digest_on):
    """Легаси-путь: если id сообщений недоступны, карту строить нельзя — покрытие не отследить."""
    async def should_not_be_called(case_id):
        raise AssertionError("без id сообщений карта неприменима")

    monkeypatch.setattr(case_digest.assistant_logger, "get_case_digest", should_not_be_called)
    history = [{"sender_role": "user", "content": "Ф" * 5_000}] + [
        {"sender_role": "ai", "content": "x" * 20_000} for _ in range(20)
    ]

    messages = asyncio.run(case_digest.build_case_messages(6, history))
    assert messages[0] is history[0]
    assert messages[-1] is history[-1]


def test_short_case_never_touches_db(monkeypatch, digest_on):
    async def should_not_be_called(case_id):
        raise AssertionError("короткое дело подаётся сырым, без обращения к карте")

    monkeypatch.setattr(case_digest.assistant_logger, "get_case_digest", should_not_be_called)
    history = [_msg(1, "user", 400), _msg(2, "ai", 900)]
    assert asyncio.run(case_digest.build_case_messages(7, history)) == history
