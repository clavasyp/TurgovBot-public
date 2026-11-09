# START OF FILE user_handlers/tester_feedback_flow.py

"""
Канал обратной связи для ТЕСТЕРОВ (баги/идеи по оверлею).

Сделан «по образцу репортов», но только для тестеров: тестер из тестового раздела
(/test) жмёт «✍️ Сообщить баг / идею», пишет одно сообщение — и оно сохраняется как
обычный репорт (двусторонняя ветка переписки уже есть в системе репортов: тестер видит её
в «Мои репорты», админ — в панели управления репортами, там же отвечает). Дополнительно
админам летит прямое уведомление с меткой «🧪 отзыв тестера».

Почему через таблицу reports, а не отдельную: переиспользуем готовый двусторонний канал
(ответы админа → пользователю, история переписки), не плодя дублирующий функционал.
Тестерские помечаем ПРЕФИКСОМ В ЗАГОЛОВКЕ ("🧪 [тестер] …") — он сразу виден в списке
репортов у админа и НЕ требует менять схему БД (в живой таблице reports колонки category нет).
"""

import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler,
    CommandHandler, filters,
)

from database.manager import db_manager
from .core import main_menu, start, _display_main_menu
from utils import safe_answer_callback_query
from config import SUPER_ADMIN_ID

logger = logging.getLogger(__name__)

# Локальное состояние диалога (изолировано в рамках этого ConversationHandler).
TFB_AWAITING_FEEDBACK = 0

# Кнопка входа (ставится в тестовом разделе /test).
CB_FEEDBACK_START = "tester_feedback_start"

_MIN_LEN = 5
_PREVIEW_LEN = 250


async def _is_tester_or_admin(user_id: int) -> bool:
    """Доступ к каналу: только тестер / админ / супер-админ."""
    rec = await db_manager.fetchrow(
        "SELECT is_admin, is_tester FROM users WHERE user_id = $1", user_id
    )
    is_admin = (rec and rec.get("is_admin")) or user_id == SUPER_ADMIN_ID
    is_tester = bool(rec and rec.get("is_tester"))
    return bool(is_admin or is_tester)


async def start_tester_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Вход в канал обратной связи тестера (кнопка из /test). Запрашивает текст бага/идеи."""
    query = update.callback_query
    user_id = query.from_user.id

    if not await _is_tester_or_admin(user_id):
        # На всякий случай (кнопку видят только тестеры, но проверяем доступ ещё раз).
        # Отвечаем алертом ДО общего answer, иначе Telegram проигнорирует второй ответ.
        await query.answer("Этот канал доступен только тестерам.", show_alert=True)
        return ConversationHandler.END

    await safe_answer_callback_query(query)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="main_menu")]])
    text = (
        "✍️ <b>Обратная связь тестера</b>\n\n"
        "Опиши баг или идею <b>одним сообщением</b> — оно уйдёт администрации.\n"
        "Ответ придёт сюда, в этот чат; всю переписку потом найдёшь в «📂 Мои репорты»."
    )
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        # Сообщение могло быть с фото/устарело — шлём новым.
        await context.bot.send_message(
            chat_id=update.effective_chat.id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML
        )
    return TFB_AWAITING_FEEDBACK


async def receive_tester_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет отзыв тестера как репорт (category='tester') и уведомляет администрацию."""
    user = update.effective_user
    text = (update.message.text or "").strip()

    if len(text) < _MIN_LEN:
        await update.message.reply_text(
            "Слишком коротко — опиши баг/идею подробнее одним сообщением."
        )
        return TFB_AWAITING_FEEDBACK

    # Создаём репорт (двусторонняя ветка — как у обычных репортов). Метка тестера — префикс
    # в заголовке: виден в списке репортов у админа, без изменения схемы БД (колонки category нет).
    report_id = await db_manager.fetchval(
        "INSERT INTO reports (user_id, title) VALUES ($1, $2) RETURNING id;",
        user.id, f"🧪 [тестер] {text}",
    )
    await db_manager.execute(
        "INSERT INTO report_messages (report_id, sender_id, sender_role, content) "
        "VALUES ($1, $2, 'user', $3);",
        report_id, user.id, text,
    )

    logger.info("Тестер %s оставил отзыв (репорт #%s, category=tester).", user.id, report_id)
    await update.message.reply_text(
        f"✅ Спасибо! Отзыв #{report_id} отправлен администрации. "
        f"Ответ придёт сюда, а историю переписки найдёшь в «📂 Мои репорты»."
    )

    # Прямое уведомление администраторам (plain text — без parse_mode, чтобы текст
    # тестера с любыми символами не ломал разметку).
    admin_records = await db_manager.fetch(
        "SELECT user_id FROM users WHERE is_admin = TRUE OR user_id = $1", SUPER_ADMIN_ID
    )
    preview = text if len(text) <= _PREVIEW_LEN else text[:_PREVIEW_LEN] + "…"
    note = (
        f"🧪 Новый отзыв тестера #{report_id}\n"
        f"от {user.full_name} (id {user.id}):\n\n{preview}\n\n"
        f"Ответить: /admin → «Управление репортами»."
    )
    for admin_id in {r["user_id"] for r in admin_records}:
        try:
            await context.bot.send_message(chat_id=admin_id, text=note)
        except Exception as e:
            logger.warning("Не удалось уведомить админа %s об отзыве тестера.", admin_id, exc_info=e)

    await _display_main_menu(update, context, is_new_message=True)
    return ConversationHandler.END


tester_feedback_conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_tester_feedback, pattern=f"^{CB_FEEDBACK_START}$"),
    ],
    states={
        TFB_AWAITING_FEEDBACK: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tester_feedback),
        ],
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(main_menu, pattern="^main_menu$"),
    ],
    per_message=False,
    allow_reentry=True,
)

# END OF FILE user_handlers/tester_feedback_flow.py
