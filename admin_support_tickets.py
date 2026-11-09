# START OF FILE admin_support_tickets.py

"""
Раздел админки бота: обращения в поддержку с сайта (таблица support_tickets).

Самодостаточный модуль — минимум хуков в admin_handlers.py/main.py (Вариант А: без
нового FSM-состояния, меню остаётся в CHOOSING_ACTION; закрытие — top-level хендлер по
паттерну `supp_close_<id>`, как сделано для ВИП-кнопок vip_ok_/vip_no_).

Точки касания живого кода (для деплоя с полным деплоем бота):
- admin_handlers.py: импорт manage_support_tickets/support_dump + счётчик и кнопка в
  admin_panel + 2 строки в блоке CHOOSING_ACTION (manage_support / supp_dump);
- main.py: импорт close_support_ticket + 1 top-level CallbackQueryHandler `^supp_close_\\d+$`.
"""

import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

import config
from config import CHOOSING_ACTION, SUPER_ADMIN_ID
from database.manager import db_manager
from services import support_tickets

logger = logging.getLogger(__name__)

_DUMP_MAX = 10  # сколько открытых обращений выгружать в личку за раз


async def _is_admin(user_id: int) -> bool:
    """Гейт админа: супер-админ или users.is_admin (без зависимости от admin_handlers)."""
    if user_id == SUPER_ADMIN_ID:
        return True
    try:
        return bool(await db_manager.fetchval("SELECT is_admin FROM users WHERE user_id = $1", user_id))
    except Exception:
        return False


def _ticket_card(t: dict) -> str:
    """HTML-карточка обращения для выгрузки в личку."""
    created = ""
    try:
        created = t["created_at"].strftime("%Y-%m-%d %H:%M")
    except Exception:
        created = str(t.get("created_at", ""))
    status_ru = {"open": "🟡 открыто", "answered": "💬 отвечено", "closed": "✅ закрыто"}.get(
        t.get("status"), t.get("status", "")
    )
    return (
        f"🆘 <b>Обращение #{t['id']}</b> ({status_ru})\n"
        f"👤 <a href=\"tg://user?id={t['user_id']}\">{t['user_id']}</a>\n"
        f"🏷 {html.escape(str(t.get('topic', '')))}\n"
        f"🕒 {created}\n\n"
        f"{html.escape(str(t.get('message', '')))}"
    )


def _close_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Закрыть", callback_data=f"supp_close_{ticket_id}")]])


async def manage_support_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню раздела (счётчики + выгрузка). Остаётся в CHOOSING_ACTION (Вариант А)."""
    query = update.callback_query
    user_id = query.from_user.id
    if not await _is_admin(user_id):
        await query.answer("Нет доступа", show_alert=True)
        return CHOOSING_ACTION
    await query.answer()

    open_n = await support_tickets.count_open()
    total = await support_tickets.count_tickets(False)
    text = (
        f"🆘 <b>Обращения в поддержку (сайт)</b>\n\n"
        f"Открытых: <b>{open_n}</b> · всего: {total}\n\n"
        f"«Открытые в личку» — пришлю до {_DUMP_MAX} карточек с кнопкой «Закрыть»."
    )
    keyboard = [
        [InlineKeyboardButton("📤 Открытые в личку", callback_data="supp_dump")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return CHOOSING_ACTION


async def support_dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выгружает открытые обращения карточками в личку админа (каждое — с кнопкой «Закрыть»)."""
    query = update.callback_query
    user_id = query.from_user.id
    if not await _is_admin(user_id):
        await query.answer("Нет доступа", show_alert=True)
        return CHOOSING_ACTION
    await query.answer("Отправляю в личку…")

    tickets = await support_tickets.list_tickets(only_open=True, offset=0, limit=_DUMP_MAX)
    if not tickets:
        try:
            await context.bot.send_message(user_id, "Открытых обращений нет.")
        except Exception:
            pass
        return CHOOSING_ACTION

    for t in tickets:
        try:
            await context.bot.send_message(
                user_id, _ticket_card(t), parse_mode="HTML",
                reply_markup=_close_kb(t["id"]), disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning("[support_admin] не удалось отправить карточку #%s: %s", t.get("id"), e)
    return CHOOSING_ACTION


async def close_support_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """TOP-LEVEL: закрытие обращения по кнопке `supp_close_<id>` (из личной карточки)."""
    query = update.callback_query
    user_id = query.from_user.id
    if not await _is_admin(user_id):
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        ticket_id = int(query.data.split("supp_close_", 1)[1])
    except (ValueError, IndexError):
        await query.answer()
        return

    row = await support_tickets.set_status(ticket_id, "closed", user_id)
    await query.answer("Закрыто" if row else "Уже закрыто / не найдено")
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        base = query.message.text_html if hasattr(query.message, "text_html") else (query.message.text or "")
        await query.edit_message_text(f"{base}\n\n✅ <b>Закрыто</b>", parse_mode="HTML",
                                      disable_web_page_preview=True)
    except Exception:
        pass


# END OF FILE admin_support_tickets.py
