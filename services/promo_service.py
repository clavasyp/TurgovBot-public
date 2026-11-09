# START OF FILE services/promo_service.py

"""
Сервис управления промо-акцией «Безлимитные запросы».

Хранит состояние в таблице bot_settings (ключ 'free_limits_promo').
Кеширует результат на 30 секунд, чтобы не спамить в БД на каждый check_and_update_usage.
"""

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from database.manager import db_manager

PROMO_KEY = 'free_limits_promo'

# --- Кеш в памяти ---
_cache: dict = {
    'value': None,       # dict или None
    'fetched_at': 0.0,   # time.monotonic()
}
_CACHE_TTL = 30  # секунд


def _default_promo() -> dict:
    """Возвращает дефолтное (выключенное) состояние промо."""
    return {
        'enabled': False,
        'end_date': None,       # ISO-строка или None
        'enabled_by': None,     # admin user_id
        'description': None,    # текст для пользователей
    }


async def _fetch_promo_from_db() -> dict:
    """Читает промо из БД. Если записи нет — возвращает дефолт."""
    row = await db_manager.fetchval(
        "SELECT value FROM bot_settings WHERE key = $1", PROMO_KEY
    )
    if row:
        try:
            return json.loads(row)
        except (json.JSONDecodeError, TypeError):
            logging.warning(f"[PROMO] Невалидный JSON в bot_settings для ключа '{PROMO_KEY}'")
    return _default_promo()


async def _save_promo_to_db(data: dict):
    """Сохраняет промо в БД (upsert)."""
    value_json = json.dumps(data, ensure_ascii=False)
    await db_manager.execute(
        """INSERT INTO bot_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
        PROMO_KEY, value_json
    )
    # Инвалидируем кеш
    _cache['value'] = data
    _cache['fetched_at'] = time.monotonic()


async def get_promo_info() -> dict:
    """
    Возвращает полное состояние промо (без кеша — всегда из БД).
    Используется в админ-панели.
    """
    return await _fetch_promo_from_db()


async def is_promo_active() -> bool:
    """
    Проверяет, активна ли промо-акция.
    Результат кешируется на 30 секунд.
    Если дата окончания прошла — автоматически деактивирует.
    """
    now = time.monotonic()

    # Проверяем кеш
    if _cache['value'] is not None and (now - _cache['fetched_at']) < _CACHE_TTL:
        data = _cache['value']
    else:
        data = await _fetch_promo_from_db()
        _cache['value'] = data
        _cache['fetched_at'] = now

    if not data.get('enabled', False):
        return False

    # Проверяем дату окончания
    end_date_str = data.get('end_date')
    if end_date_str:
        try:
            end_date = datetime.fromisoformat(end_date_str)
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= end_date:
                # Промо истекло — автоотключаем
                logging.info(f"[PROMO] Акция автоматически завершена (end_date={end_date_str})")
                data['enabled'] = False
                await _save_promo_to_db(data)
                return False
        except (ValueError, TypeError):
            logging.warning(f"[PROMO] Невалидная дата окончания: {end_date_str}")

    return True


async def set_promo(
    enabled: bool,
    end_date: Optional[datetime] = None,
    admin_id: Optional[int] = None,
    description: Optional[str] = None
):
    """
    Включает или выключает промо-акцию.

    Args:
        enabled: True — включить, False — выключить
        end_date: Дата/время окончания (None = бессрочно)
        admin_id: Telegram ID админа, включившего акцию
        description: Текст для пользователей (напр. «Свободные выходные!»)
    """
    data = {
        'enabled': enabled,
        'end_date': end_date.isoformat() if end_date else None,
        'enabled_by': admin_id,
        'description': description,
    }
    await _save_promo_to_db(data)
    status = "ВКЛЮЧЕНА" if enabled else "ВЫКЛЮЧЕНА"
    logging.info(f"[PROMO] Акция {status} администратором {admin_id}. end_date={end_date}")


def invalidate_cache():
    """Принудительная инвалидация кеша (при изменении из другого места)."""
    _cache['value'] = None
    _cache['fetched_at'] = 0.0


# END OF FILE services/promo_service.py

