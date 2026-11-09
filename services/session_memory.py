# START OF FILE services/session_memory.py

"""
Rolling-память диалога для оверлея (решает «контекст теряется между вопросами»).

Память живёт на СЕРВЕРЕ, ключ — session_id (клиент держит один на сессию). Её наполняет
голосовой WS (финальные реплики транскрипта с меткой источника «ты»/«собеседник»), а эндпоинт
совета подтягивает свод последних реплик как `history`. Хранилище — в памяти процесса api
(uvicorn в один процесс); сессии протухают по TTL.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

# Подписи источников для модели.
SOURCE_LABELS = {"mic": "ты", "other": "собеседник", "loopback": "собеседник"}

# Жёсткий максимум «последних сообщений» в контексте (клиент может попросить МЕНЬШЕ через
# history_limit, но не больше). Реплики сверх этого не хранятся вовсе.
_MAX_TURNS = 10
_MAX_CHARS = 4000
_SESSION_TTL = 3600  # 1 час бездействия


class SessionMemory:
    def __init__(self, max_turns: int = _MAX_TURNS, max_chars: int = _MAX_CHARS):
        self.turns: Deque[Tuple[str, str]] = deque(maxlen=max_turns)
        self.max_chars = max_chars
        self.last_access = time.time()

    def add(self, label: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        # Дедуп подряд идущих идентичных реплик (голос-финал, затем тот же текст как «вопрос»).
        if self.turns and self.turns[-1][1] == text:
            return
        self.turns.append((label, text))
        self.last_access = time.time()

    def render(self, limit: Optional[int] = None) -> str:
        """Свод последних реплик (most-recent-first cap по символам), в хронологическом порядке.

        limit — сколько ПОСЛЕДНИХ сообщений учитывать (клиентская настройка). None → все хранимые
        (до _MAX_TURNS). Значение клампится в [0, _MAX_TURNS]; 0 → пустой контекст.
        """
        self.last_access = time.time()
        turns = list(self.turns)
        if limit is not None:
            limit = max(0, min(int(limit), self.turns.maxlen or _MAX_TURNS))
            turns = turns[-limit:] if limit > 0 else []
        lines = [f"[{label}]: {text}" for label, text in turns]
        # Обрезаем по объёму, оставляя САМЫЕ СВЕЖИЕ.
        out, total = [], 0
        for line in reversed(lines):
            if total + len(line) + 1 > self.max_chars and out:
                break
            out.append(line)
            total += len(line) + 1
        return "\n".join(reversed(out))


# === Реестр сессий (в памяти процесса) ===
_SESSIONS: Dict[str, SessionMemory] = {}


def _prune() -> None:
    now = time.time()
    for sid in [s for s, m in _SESSIONS.items() if now - m.last_access > _SESSION_TTL]:
        _SESSIONS.pop(sid, None)


def get_session(session_id: str) -> Optional[SessionMemory]:
    if not session_id:
        return None
    _prune()
    mem = _SESSIONS.get(session_id)
    if mem is None:
        mem = SessionMemory()
        _SESSIONS[session_id] = mem
    return mem


def render_history(session_id: Optional[str], limit: Optional[int] = None) -> Optional[str]:
    if not session_id:
        return None
    mem = _SESSIONS.get(session_id)
    return mem.render(limit) if mem else None


def add_turn(session_id: Optional[str], source: str, text: str) -> None:
    if not session_id:
        return
    label = SOURCE_LABELS.get(source, source or "реплика")
    get_session(session_id).add(label, text)


# END OF FILE services/session_memory.py
