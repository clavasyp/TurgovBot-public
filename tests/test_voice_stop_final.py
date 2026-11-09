# START OF FILE tests/test_voice_stop_final.py
"""
E2E-симуляция WS-эндпоинта голосового оверлея (/v1/voice/stream).

Регрессия бага «не расслышал при быстром отпускании PTT»:
STT досказывает ФИНАЛЬНЫЙ транскрипт по окончании аудио (по {"type":"stop"} от клиента).
Раньше обработчик ставил единый флаг `disconnected` и на graceful-{stop}, и на реальный
разрыв, а корутина отправки прерывалась по этому флагу — финал генерировался, но
выбрасывался до отправки. Тест гоняет настоящий voice_stream() с фейковым WebSocket и
фейковым stream_transcribe (финал только по концу аудио) и проверяет, что клиент
ПОЛУЧАЕТ final после {stop}. Плюс проверка, что на реальном разрыве мы не зависаем.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Фейки транспорта/STT
# ---------------------------------------------------------------------------


class FakeWebSocket:
    """Минимальный двойник Starlette WebSocket для драйва voice_stream()."""

    def __init__(self, incoming):
        # incoming — список сообщений, которые отдаст receive() по очереди.
        self._incoming = list(incoming)
        self.sent = []          # тексты, отправленные клиенту (send_text)
        self.accepted = False
        self.closed_code = None
        self.query_params = {
            "ws_ticket": "t-ok",
            "source": "mic",
            "session_id": "sess1234",
        }

    async def accept(self):
        self.accepted = True

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        # Очередь исчерпана — эмулируем разрыв транспорта (клиент молчит/ушёл).
        return {"type": "websocket.disconnect"}

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self, code=None):
        self.closed_code = code

    # --- удобные проверки ---
    def events(self):
        return [json.loads(s) for s in self.sent]

    def finals(self):
        return [e for e in self.events() if e.get("type") == "final"]


def _fake_stt_final_only(final_text="тест вопрос"):
    """stream_transcribe, который досказывает final ТОЛЬКО по окончании аудио
    (как при быстром отпускании PTT — естественного эндпойнта в потоке не было)."""

    async def _gen(audio_aiter, language=None, phrases=None):
        consumed = 0
        async for _chunk in audio_aiter:
            consumed += 1
        # Аудио закончилось (получен sentinel из session.close()) → финал.
        yield {"type": "final", "text": final_text}

    return _gen


def _audio_then_stop(n_frames=3, stop=True):
    msgs = [
        {"type": "websocket.receive", "bytes": bytes([i % 251]) * 320}
        for i in range(n_frames)
    ]
    if stop:
        msgs.append({"type": "websocket.receive", "text": '{"type":"stop"}'})
    return msgs


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


async def _run_voice_stream(ws):
    """Импорт внутри теста — патчи device_auth уже наложены."""
    from api.routers.voice import voice_stream
    await voice_stream(ws)


@pytest.fixture
def authorized():
    """consume_ws_ticket → валидный, устройство активно."""
    with patch("services.device_auth.consume_ws_ticket",
               return_value={"user_id": 1, "device_id": "dev1", "tier": "pro"}), \
         patch("services.device_auth.is_device_active", new=AsyncMock(return_value=True)), \
         patch("api.routers.voice.add_turn", return_value=None):
        yield


async def test_final_delivered_on_stop(authorized):
    """РЕГРЕССИЯ: при {stop} финальный транскрипт обязан дойти до клиента."""
    ws = FakeWebSocket(_audio_then_stop(n_frames=3, stop=True))
    with patch("services.voice_session.stream_transcribe",
               new=_fake_stt_final_only("привет как дела")):
        await _run_voice_stream(ws)

    finals = ws.finals()
    assert finals, (
        "final не был отправлен клиенту после {stop} — "
        "именно этот баг даёт «не расслышал» при быстром отпускании PTT"
    )
    assert finals[0]["text"] == "привет как дела"
    assert finals[0]["source"] == "mic"


async def test_partial_then_final_both_delivered(authorized):
    """Partial во время речи + final по stop — оба доходят, порядок сохранён."""

    async def _gen(audio_aiter, language=None, phrases=None):
        yield {"type": "partial", "text": "прив"}
        async for _ in audio_aiter:
            pass
        yield {"type": "final", "text": "привет"}

    ws = FakeWebSocket(_audio_then_stop(n_frames=2, stop=True))
    with patch("services.voice_session.stream_transcribe", new=_gen):
        await _run_voice_stream(ws)

    evs = ws.events()
    types = [e["type"] for e in evs]
    assert "partial" in types and "final" in types
    assert types.index("partial") < types.index("final")
    assert [e for e in evs if e["type"] == "final"][0]["text"] == "привет"


async def test_real_disconnect_does_not_hang(authorized):
    """Реальный разрыв (без stop): обработчик завершается, не зависает."""
    # Аудио без {stop}; receive() затем вернёт websocket.disconnect.
    ws = FakeWebSocket(_audio_then_stop(n_frames=2, stop=False))

    async def _gen(audio_aiter, language=None, phrases=None):
        async for _ in audio_aiter:
            pass
        yield {"type": "final", "text": "поздно"}
        if False:
            yield  # pragma: no cover

    with patch("services.voice_session.stream_transcribe", new=_gen):
        await _run_voice_stream(ws)
    # Главное — дошли сюда без зависания. Финал на мёртвом транспорте не обязателен.


async def test_unauthorized_closes(authorized=None):
    """Без валидного тикета — закрытие до accept, без отправок."""
    ws = FakeWebSocket([])
    with patch("services.device_auth.consume_ws_ticket", return_value=None):
        await _run_voice_stream(ws)
    assert not ws.accepted
    assert ws.closed_code == 1008
    assert ws.sent == []


# END OF FILE tests/test_voice_stop_final.py
