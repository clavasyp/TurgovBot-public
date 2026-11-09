# START OF FILE api/routers/voice.py

"""
WebSocket-эндпоинт голосового оверлея: приём PCM-аудио → потоковое распознавание (Chirp)
→ выдача транскрипта обратно клиенту.

- WS /v1/voice/stream?api_key=...&server_id=...&source=mic
  Клиент шлёт бинарные кадры PCM (LINEAR16 16kHz mono) и опц. текстовые control-сообщения
  ({"type":"stop"}). Сервер шлёт JSON-события транскрипта:
    {"type":"partial"|"final"|"error","text":...,"source":...}

Совет (RAG+Gemini) остаётся на отдельном HTTP-эндпоинте /v1/overlay/advise(/stream) —
клиент вызывает его по финальным репликам. WS отвечает только за транскрипт.
"""

import asyncio
import contextlib
import json
import logging
import os
import sys

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

# Корень проекта в sys.path (как в overlay.py) — для импорта services.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.voice_session import VoiceSession
from services import device_auth
from services.session_memory import add_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/voice", tags=["Voice"])

# Коды закрытия WS
_WS_POLICY_VIOLATION = 1008
_WS_INTERNAL_ERROR = 1011


async def _authorize(websocket: WebSocket):
    """Авторизация ДО accept по одноразовому WS-тикету (анти-replay). Возвращает {user_id, device_id, tier} или None.
    Тикет клиент получает по access-токену через POST /v1/auth/ws-ticket — долгий токен в URL не светится."""
    qp = websocket.query_params
    ticket = qp.get("ws_ticket") or qp.get("ticket")
    if not ticket:
        return None
    data = device_auth.consume_ws_ticket(ticket)
    if not data:
        return None
    # Тир laws (универсальный ключ laws-билда) — только поиск по законке; облачный голосовой
    # стрим ему недоступен (laws-билд использует локальный STT).
    if (data.get("tier") or "").lower() == "laws":
        return None
    try:
        if not await device_auth.is_device_active(data["user_id"], data["device_id"]):
            return None
    except Exception:
        logger.exception("[VOICE] ошибка проверки устройства")
        return None
    return data


@router.websocket("/stream")
async def voice_stream(websocket: WebSocket):
    auth = await _authorize(websocket)
    if not auth:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return

    await websocket.accept()
    qp = websocket.query_params
    source = qp.get("source") or "mic"
    session_id = qp.get("session_id") or ""
    session = VoiceSession(source=source)
    # ВАЖНО: различаем «клиент попросил {stop}» и «транспорт реально умер».
    # transport_gone ставим ТОЛЬКО при настоящем разрыве соединения. На graceful {stop}
    # сокет ещё жив (клиент держит его ради финала) — прерывать отправку нельзя, иначе
    # финальный транскрипт, который STT досказывает по окончании аудио, будет потерян
    # (симптом: «не расслышал» при быстром отпускании PTT — аудио ушло, а final выкинут).
    transport_gone = asyncio.Event()

    logger.info("[VOICE] WS open device=%s source=%s session=%s", auth.get("device_id"), source, session_id[:8])

    async def pump_audio():
        """Приём кадров из WS → во входную очередь сессии."""
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    transport_gone.set()   # транспорт мёртв — слать нечего и некуда
                    break
                data = msg.get("bytes")
                if data:
                    await session.push(data)
                    continue
                text = msg.get("text")
                if text:
                    try:
                        ctrl = json.loads(text)
                    except Exception:
                        ctrl = {}
                    if ctrl.get("type") == "stop":
                        # Мягкое завершение: перестаём принимать аудио, НО сокет жив —
                        # session.close() ниже доскажет final, его отправит корутина отправки.
                        break
        except WebSocketDisconnect:
            transport_gone.set()
        except Exception:
            logger.exception("[VOICE] ошибка приёма аудио")
            transport_gone.set()
        finally:
            # Закрываем входной поток: текущий сегмент доработает и выдаст финальный транскрипт.
            await session.close()

    pump = asyncio.create_task(pump_audio())
    try:
        async for ev in session.events():
            # Прерываемся ТОЛЬКО если транспорт реально умер. На {stop} НЕ прерываемся —
            # иначе теряем финал (см. transport_gone выше).
            if transport_gone.is_set():
                break
            # Финальные реплики копим в rolling-память сессии (контекст для совета).
            if session_id and ev.get("type") == "final":
                try:
                    add_turn(session_id, source, ev.get("text", ""))
                except Exception:
                    pass
            try:
                await websocket.send_text(json.dumps(ev, ensure_ascii=False))
            except Exception:
                # Клиент закрыл сокет раньше времени — дальше слать некуда.
                transport_gone.set()
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("[VOICE] ошибка отправки транскрипта")
    finally:
        await session.close()
        pump.cancel()
        with contextlib.suppress(Exception):
            await pump
        with contextlib.suppress(Exception):
            await websocket.close()
        logger.info("[VOICE] WS closed device=%s", auth.get("device_id"))


# END OF FILE api/routers/voice.py
