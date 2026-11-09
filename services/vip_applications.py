# START OF FILE services/vip_applications.py

"""
Заявки на ВИП/безлимит за промокод.

Юзер подаёт анкету через кнопку главного меню «Получить ВИП за промокод» (или
deep-link ?start=apply_vip) с пруфами (видео+скрины) и формой
(user_handlers/vip_application_flow.py). Заявка постится в отдельный TG-канал
(config.VIP_APPLICATIONS_CHAT_ID) с кнопками Одобрить/Отклонить; супер-/админ
смотрит пруфы и на одобрении получает выдачу users.is_unlimited (только безлимит,
без оверлея — оверлей остаётся за подпиской на канал). Хранилище — таблица
vip_applications (alembic 015).

Приём заявок включается/выключается персистентным флагом в system_cache — по тому же
шаблону, что заявки тестировщиков (services.tester_applications) и тех. работы.

Политика повтора: одна активная (pending) заявка на юзера (БД-гарантия — частичный
UNIQUE-индекс uq_vip_apps_pending); после отклонения можно подать снова.

Метрики юзера для карточки заявки берём из services.tester_applications
(gather_user_metrics / format_metrics_block — дженерик по user_id).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import asyncpg

from database.manager import db_manager

logger = logging.getLogger(__name__)

_INTAKE_KEY = "vip_intake_enabled"
_CHANNEL_KEY = "vip_channel_chat_id"  # куда слать заявки (персистентно, ставится /set_vip_channel)


async def get_channel_chat_id() -> Optional[int]:
    """Канал/группа заявок из system_cache (ставится командой /set_vip_channel). None = не задан."""
    try:
        val = await db_manager.fetchval("SELECT value FROM system_cache WHERE key = $1", _CHANNEL_KEY)
    except Exception:
        return None
    if val is None:
        return None
    try:
        return int(str(val).strip().strip('"'))
    except Exception:
        return None


async def set_channel_chat_id(chat_id: int) -> None:
    """Запомнить чат заявок (персистентно)."""
    await db_manager.execute(
        """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _CHANNEL_KEY, json.dumps(chat_id),
    )


# ---------------------------------------------------------------------------
# Тумблер приёма заявок (персистентно, system_cache)
# ---------------------------------------------------------------------------

async def is_intake_enabled() -> bool:
    """Открыт ли приём заявок на ВИП. По умолчанию ВКЛ."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", _INTAKE_KEY
        )
    except Exception:
        return True
    if val is None:
        return True
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().strip('"').lower() not in ("false", "0")
    return bool(val)


async def set_intake_enabled(enabled: bool) -> None:
    """Открыть/закрыть приём заявок (персистентно)."""
    await db_manager.execute(
        """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ($1, $2::jsonb, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """,
        _INTAKE_KEY, "true" if enabled else "false",
    )


# ---------------------------------------------------------------------------
# CRUD заявок
# ---------------------------------------------------------------------------

def _jsonb(v) -> Any:
    """JSONB-поле приходит из asyncpg строкой — распарсить в dict/list."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


def _row(row) -> Optional[Dict[str, Any]]:
    """asyncpg.Record -> dict с распарсенными answers/media (JSONB приходит строкой)."""
    if row is None:
        return None
    d = dict(row)
    d["answers"] = _jsonb(d.get("answers")) or {}
    d["media"] = _jsonb(d.get("media")) or []
    return d


async def get_pending_application(user_id: int) -> Optional[Dict[str, Any]]:
    """Активная (pending) заявка юзера, если есть — для блокировки дублей."""
    row = await db_manager.fetchrow(
        "SELECT * FROM vip_applications WHERE user_id = $1 AND status = 'pending'",
        user_id,
    )
    return _row(row)


async def create_application(
    user_id: int, answers: Dict[str, Any], media: List[Dict[str, Any]]
) -> Optional[int]:
    """Создать заявку. Возвращает id или None, если активная заявка уже есть
    (частичный UNIQUE-индекс uq_vip_apps_pending) либо при ошибке."""
    try:
        new_id = await db_manager.fetchval(
            """
            INSERT INTO vip_applications (user_id, answers, media, status)
            VALUES ($1, $2::jsonb, $3::jsonb, 'pending')
            RETURNING id
            """,
            user_id,
            json.dumps(answers, ensure_ascii=False),
            json.dumps(media, ensure_ascii=False),
        )
        return int(new_id) if new_id is not None else None
    except asyncpg.UniqueViolationError:
        return None
    except Exception:
        logger.exception("[vip_apps] не удалось создать заявку user_id=%s", user_id)
        return None


async def set_channel_message(app_id: int, chat_id: int, message_id: int) -> None:
    """Запомнить, куда запостили карточку заявки (чтобы потом отредактировать)."""
    try:
        await db_manager.execute(
            "UPDATE vip_applications SET channel_chat_id = $2, channel_message_id = $3 WHERE id = $1",
            app_id, chat_id, message_id,
        )
    except Exception:
        logger.exception("[vip_apps] не удалось сохранить channel_message для #%s", app_id)


async def count_pending() -> int:
    try:
        val = await db_manager.fetchval(
            "SELECT COUNT(*) FROM vip_applications WHERE status = 'pending'"
        )
    except Exception:
        return 0
    return int(val or 0)


async def count_applications(only_pending: bool) -> int:
    if only_pending:
        return await count_pending()
    try:
        val = await db_manager.fetchval("SELECT COUNT(*) FROM vip_applications")
    except Exception:
        return 0
    return int(val or 0)


async def list_applications(only_pending: bool, offset: int, limit: int) -> List[Dict[str, Any]]:
    """Страница заявок. pending — старые сверху (FIFO), все — свежие сверху."""
    if only_pending:
        rows = await db_manager.fetch(
            """
            SELECT * FROM vip_applications
            WHERE status = 'pending'
            ORDER BY created_at ASC
            OFFSET $1 LIMIT $2
            """,
            offset, limit,
        )
    else:
        rows = await db_manager.fetch(
            """
            SELECT * FROM vip_applications
            ORDER BY created_at DESC
            OFFSET $1 LIMIT $2
            """,
            offset, limit,
        )
    return [_row(r) for r in rows]


async def get_application(app_id: int) -> Optional[Dict[str, Any]]:
    row = await db_manager.fetchrow(
        "SELECT * FROM vip_applications WHERE id = $1", app_id
    )
    return _row(row)


async def set_application_status(app_id: int, status: str, reviewed_by: int) -> Optional[Dict[str, Any]]:
    """Перевести заявку pending -> approved/rejected. Транзит только из pending
    (защита от двойной обработки). Возвращает обновлённую строку (с user_id для
    уведомления/выдачи статуса) или None, если заявка уже обработана/не найдена."""
    row = await db_manager.fetchrow(
        """
        UPDATE vip_applications
        SET status = $2, reviewed_by = $3, reviewed_at = NOW()
        WHERE id = $1 AND status = 'pending'
        RETURNING *
        """,
        app_id, status, reviewed_by,
    )
    return _row(row)


# END OF FILE services/vip_applications.py
