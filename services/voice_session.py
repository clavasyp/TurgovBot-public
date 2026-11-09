# START OF FILE services/voice_session.py

"""
Endless-streaming обёртка над stt_service.stream_transcribe для голосового оверлея.

Одна streaming-сессия Speech v2 живёт ≤5 мин (лимит API). Чтобы распознавать непрерывно,
держим очередь входящих PCM-чанков и переоткрываем сессию каждые STT_STREAM_REOPEN_SECONDS,
НЕ теряя аудио: чанки, пришедшие в момент переоткрытия, остаются в очереди и подхватываются
следующим сегментом. Пустые сессии в тишине не открываем — ждём первый чанк сегмента блокирующе.

Использование (на стороне WS-обработчика):
    session = VoiceSession()
    # приёмник аудио из WS:
    await session.push(pcm_bytes)
    ...
    await session.close()              # по завершении/дисконнекту
    # потребитель транскрипта:
    async for ev in session.events():  # {"type": "partial"|"final"|"error", "text": ...}
        send_to_client(ev)
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Dict, List, Optional, Any

import config
from services.stt_service import stream_transcribe

logger = logging.getLogger(__name__)


class VoiceSession:
    """Непрерывное распознавание поверх коротких (≤5 мин) сессий Speech v2."""

    _SENTINEL = object()  # маркер «аудио закончилось / клиент отключился»

    def __init__(
        self,
        *,
        language: Optional[str] = None,
        phrases: Optional[List[str]] = None,
        reopen_seconds: Optional[int] = None,
        source: str = "mic",
    ):
        self.language = language or config.STT_LANGUAGE
        self.phrases = phrases
        self.reopen_seconds = reopen_seconds or config.STT_STREAM_REOPEN_SECONDS
        self.source = source
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False

    async def push(self, chunk: bytes) -> None:
        """Положить PCM-чанк (LINEAR16 16kHz mono) во входную очередь."""
        if not self._closed and chunk:
            await self._queue.put(chunk)

    async def close(self) -> None:
        """Сигнал завершения: текущий сегмент доработает и поток событий закроется."""
        if not self._closed:
            self._closed = True
            await self._queue.put(self._SENTINEL)

    async def _segment(self, first: bytes, deadline: float) -> AsyncIterator[bytes]:
        """Аудио одного сегмента: первый чанк уже на руках, дальше тянем из очереди
        до дедлайна (переоткрытие) или до сигнала close()."""
        yield first
        loop = asyncio.get_event_loop()
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return  # сегмент истёк → переоткрываем сессию
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return
            if chunk is self._SENTINEL:
                self._closed = True
                return
            yield chunk

    async def events(self) -> AsyncIterator[Dict[str, Any]]:
        """Непрерывный поток событий транскрипта (с меткой source)."""
        loop = asyncio.get_event_loop()
        while not self._closed:
            # Ждём первый чанк сегмента блокирующе — не открываем gRPC-сессию в тишине.
            first = await self._queue.get()
            if first is self._SENTINEL:
                break

            deadline = loop.time() + self.reopen_seconds
            try:
                async for ev in stream_transcribe(
                    self._segment(first, deadline),
                    language=self.language,
                    phrases=self.phrases,
                ):
                    yield {**ev, "source": self.source}
            except Exception as e:
                logger.exception("[VOICE] ошибка сегмента STT")
                yield {"type": "error", "text": str(e), "source": self.source}
                # короткая пауза, чтобы при стабильной ошибке не крутить переоткрытие впустую
                await asyncio.sleep(0.5)


# END OF FILE services/voice_session.py
