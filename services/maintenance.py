# START OF FILE services/maintenance.py

"""
Режим тех. работ для API оверлея — ЕДИНЫЙ флаг с ботом.

Источник истины — system_cache['maintenance_mode'] (бот пишет его из админки через
utils.set_maintenance_mode, переживает рестарты). API-процесс читает тот же флаг из БД,
поэтому тумблер в админке бота гасит и бота, и оверлей одновременно.

Поведение зеркалит бота (utils.rate_limited): проходят ТОЛЬКО админы (тестеры блокируются
наравне с остальными — решение заказчика 2026-06-12), остальным — человекочитаемое сообщение.
Оверлейные эндпоинты отдают его в ОБЫЧНОМ формате ответа (200 + текст), чтобы уже выпущенные
клиенты (0.2.20/0.3.0) показали его в панели ответа без обновления.

Кэш: флаг — 15с (тумблер редкий, не дёргаем БД на каждый запрос), статус юзера — 60с.
Ошибки БД = fail-open (тех. работы не должны «включаться» из-за моргнувшей БД).
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)

MAINTENANCE_MESSAGE = (
    "⚙️ Ведутся технические работы.\n"
    "Сервис временно недоступен — попробуй позже."
)

_FLAG_TTL_SECONDS = 15.0
_USER_TTL_SECONDS = 60.0

_flag_cache: Dict[str, Any] = {"ts": 0.0, "on": False}
_user_bypass_cache: Dict[int, Tuple[float, bool]] = {}
_USER_CACHE_MAX = 4096  # кап (анти-рост памяти; больше живых юзеров одновременно не бывает)


async def _flag_enabled() -> bool:
    now = time.monotonic()
    if now - _flag_cache["ts"] < _FLAG_TTL_SECONDS:
        return _flag_cache["on"]
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", "maintenance_mode"
        )
        # тот же парсинг, что utils.get_maintenance_mode (jsonb приходит строкой)
        if isinstance(val, bool):
            on = val
        elif isinstance(val, str):
            on = val.strip().strip('"').lower() in ("true", "1")
        else:
            on = bool(val) if val is not None else False
        _flag_cache.update(ts=now, on=on)
    except Exception as e:  # БД моргнула — работаем по последнему известному значению
        logger.warning("[maintenance] не прочитал флаг: %s", e)
        _flag_cache["ts"] = now
    return _flag_cache["on"]


async def _user_bypass(user_id: int) -> bool:
    """ТОЛЬКО админы проходят тех. работы (тестеры блокируются, как в боте)."""
    if user_id == config.SUPER_ADMIN_ID:
        return True
    now = time.monotonic()
    cached = _user_bypass_cache.get(user_id)
    if cached and now - cached[0] < _USER_TTL_SECONDS:
        return cached[1]
    try:
        row = await db_manager.fetchrow(
            "SELECT is_admin FROM users WHERE user_id = $1", user_id
        )
        bypass = bool(row and row["is_admin"])
    except Exception as e:
        logger.warning("[maintenance] не прочитал статус юзера %s: %s", user_id, e)
        bypass = False
    if len(_user_bypass_cache) > _USER_CACHE_MAX:
        _user_bypass_cache.clear()
    _user_bypass_cache[user_id] = (now, bypass)
    return bypass


async def maintenance_active_for(user_id: Optional[Any]) -> bool:
    """True = юзеру надо показать «тех. работы» вместо ответа. Никогда не бросает."""
    try:
        if not await _flag_enabled():
            return False
        try:
            uid = int(user_id) if user_id is not None else 0
        except (TypeError, ValueError):
            uid = 0
        if uid and await _user_bypass(uid):
            return False
        return True
    except Exception as e:
        logger.warning("[maintenance] проверка упала (fail-open): %s", e)
        return False


# END OF FILE services/maintenance.py
