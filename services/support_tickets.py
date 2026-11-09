# START OF FILE services/support_tickets.py

"""
Обращения в поддержку с сайта (таблица support_tickets, миграция 016).

Юзер с сайта (/v1/web/support/ticket) выбирает тему и пишет сообщение → запись здесь +
уведомление в TG-чат поддержки. Управление — в админке бота (список/просмотр/закрыть/
ответить). Сервис общий с ботом (общая БД), по образцу services/vip_applications.py.

Статусы: open / answered / closed. Несколько обращений на юзера допустимы.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from database.manager import db_manager

logger = logging.getLogger(__name__)

VALID_STATUSES = ("open", "answered", "closed")


def _row(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


async def create_ticket(user_id: int, topic: str, message: str, source: str = "web") -> Optional[int]:
    """Создать обращение. Возвращает id или None при ошибке."""
    try:
        new_id = await db_manager.fetchval(
            """
            INSERT INTO support_tickets (user_id, topic, message, source, status)
            VALUES ($1, $2, $3, $4, 'open')
            RETURNING id
            """,
            user_id, topic, message, source,
        )
        return int(new_id) if new_id is not None else None
    except Exception:
        logger.exception("[support] не удалось создать тикет user_id=%s", user_id)
        return None


async def get_ticket(ticket_id: int) -> Optional[Dict[str, Any]]:
    row = await db_manager.fetchrow("SELECT * FROM support_tickets WHERE id = $1", ticket_id)
    return _row(row)


async def list_tickets(only_open: bool, offset: int, limit: int) -> List[Dict[str, Any]]:
    """Страница обращений. open — старые сверху (FIFO), все — свежие сверху."""
    if only_open:
        rows = await db_manager.fetch(
            "SELECT * FROM support_tickets WHERE status = 'open' "
            "ORDER BY created_at ASC OFFSET $1 LIMIT $2",
            offset, limit,
        )
    else:
        rows = await db_manager.fetch(
            "SELECT * FROM support_tickets ORDER BY created_at DESC OFFSET $1 LIMIT $2",
            offset, limit,
        )
    return [_row(r) for r in rows]


async def count_open() -> int:
    try:
        val = await db_manager.fetchval("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")
    except Exception:
        return 0
    return int(val or 0)


async def count_tickets(only_open: bool) -> int:
    if only_open:
        return await count_open()
    try:
        val = await db_manager.fetchval("SELECT COUNT(*) FROM support_tickets")
    except Exception:
        return 0
    return int(val or 0)


async def set_status(ticket_id: int, status: str, reviewed_by: int) -> Optional[Dict[str, Any]]:
    """Сменить статус обращения (open→answered/closed). Возвращает обновлённую строку."""
    if status not in VALID_STATUSES:
        return None
    row = await db_manager.fetchrow(
        """
        UPDATE support_tickets
        SET status = $2, reviewed_by = $3, reviewed_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        ticket_id, status, reviewed_by,
    )
    return _row(row)


async def set_answer(ticket_id: int, answer: str, reviewed_by: int) -> Optional[Dict[str, Any]]:
    """Сохранить ответ админа + перевести в answered."""
    row = await db_manager.fetchrow(
        """
        UPDATE support_tickets
        SET answer = $2, status = 'answered', reviewed_by = $3, reviewed_at = NOW()
        WHERE id = $1
        RETURNING *
        """,
        ticket_id, answer, reviewed_by,
    )
    return _row(row)


async def set_channel_message(ticket_id: int, chat_id: int, message_id: int) -> None:
    """Запомнить координаты уведомления (чтобы потом отредактировать карточку)."""
    try:
        await db_manager.execute(
            "UPDATE support_tickets SET channel_chat_id = $2, channel_message_id = $3 WHERE id = $1",
            ticket_id, chat_id, message_id,
        )
    except Exception:
        logger.exception("[support] не удалось сохранить channel_message для #%s", ticket_id)


# END OF FILE services/support_tickets.py
