# START OF FILE services/web_users.py

"""
Веб-пользователи сайта TurgovBot: UPSERT профиля и вычисление статуса/тира.

Переиспользует ту же таблицу `users`, что и Telegram-бот (через database.manager.db_manager).
Логика статуса повторяет приоритеты личного кабинета бота (user_handlers/core.py),
но возвращает машинный tier из набора types.ts::Tier для бейджа на сайте.
"""

import logging

from database.manager import db_manager
from config import SUPER_ADMIN_ID

logger = logging.getLogger(__name__)


async def upsert_web_user(user_id: int, full_name: str, username: str | None) -> None:
    """
    Создаёт/обновляет запись пользователя по образцу user_handlers/core.py::log_user.

    UPSERT в users: при первом веб-входе создаёт строку, при повторном — обновляет
    full_name/username и last_seen. Флаги ролей не трогаются.
    """
    query = """
        INSERT INTO users (user_id, full_name, username, last_seen)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (user_id) DO UPDATE
            SET full_name = EXCLUDED.full_name,
                username  = EXCLUDED.username,
                last_seen = NOW();
    """
    await db_manager.execute(query, int(user_id), full_name, username)


# Машинный tier (types.ts::Tier) + человекочитаемый бейдж. Порядок — по приоритету.
_TIER_LABELS = {
    "admin": "🛡 Админ",
    "vip": "👑 VIP",
    "unlimited": "♾ Безлимит",
    "tester": "🧪 Тестер",
    "subscriber": "💎 Подписчик",
    "standard": "Стандарт",
}


async def compute_account_status(user_id: int) -> tuple[str, str]:
    """
    Вычисляет (tier, label) пользователя по флагам таблицы users.

    Приоритет (как в личном кабинете бота, сверху вниз):
        admin > vip > unlimited > tester > subscriber > standard
    Супер-админ трактуется как admin.

    Returns:
        (tier, label), где tier ∈ {standard, subscriber, vip, unlimited, tester, admin}.
    """
    rec = await db_manager.fetchrow(
        """
        SELECT is_admin, is_vip, is_unlimited, is_tester, is_subscriber
        FROM users WHERE user_id = $1
        """,
        int(user_id),
    )

    is_admin = bool(rec and rec["is_admin"]) or int(user_id) == SUPER_ADMIN_ID
    is_vip = bool(rec and rec["is_vip"])
    is_unlimited = bool(rec and rec["is_unlimited"])
    is_tester = bool(rec and rec["is_tester"])
    is_subscriber = bool(rec and rec["is_subscriber"])

    if is_admin:
        tier = "admin"
    elif is_vip:
        tier = "vip"
    elif is_unlimited:
        tier = "unlimited"
    elif is_tester:
        tier = "tester"
    elif is_subscriber:
        tier = "subscriber"
    else:
        tier = "standard"

    return tier, _TIER_LABELS[tier]


# END OF FILE services/web_users.py
