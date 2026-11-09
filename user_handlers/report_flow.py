# START OF FILE user_handlers/report_flow.py

"""
Модуль для управления диалогом системы поддержки (репортов) со стороны пользователя.

Содержит:
- `start_report_dialog`: Точка входа в систему репортов.
- `prompt_for_report_title`: Запрашивает у пользователя описание проблемы.
- `create_new_report`: Создает новый репорт в БД.
- `view_user_reports_list`: Показывает список репортов пользователя.
- `view_user_report_conversation`: Отображает переписку по конкретному репорту.
- `prompt_for_user_reply_in_report`: Запрашивает ответ пользователя в существующем репорте.
- `handle_user_reply_in_report`: Сохраняет ответ пользователя и уведомляет администраторов.
- `report_conv_handler`: ConversationHandler для всего этого функционала.
"""

import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters

# Локальные импорты
from database.manager import db_manager
from .core import main_menu, start, _display_main_menu, unknown_text_fallback # Импортируем обработчики из core
from utils import safe_answer_callback_query # <<< ДОБАВЛЕНА ЭТА СТРОКА
from config import (
    SUPER_ADMIN_ID,
    CHOOSING_REPORT_ACTION, AWAITING_REPORT_TITLE, VIEWING_USER_REPORTS, AWAITING_USER_REPLY_IN_REPORT
)


async def start_report_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Точка входа в систему репортов. Предлагает создать новый или посмотреть существующие.
    Доступна даже для забаненных пользователей.
    """
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("➕ Создать новый репорт", callback_data="report_create_new")],
        [InlineKeyboardButton("📂 Мои репорты", callback_data="report_view_mine")],
        [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
    ]
    text = "⚙️ **Система поддержки**\n\nЗдесь вы можете сообщить о проблеме, ошибке в работе бота или задать вопрос напрямую администрации."
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Проверяем, является ли сообщение фото (с caption вместо text)
    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    else:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    return CHOOSING_REPORT_ACTION


async def prompt_for_report_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запрашивает у пользователя текст для нового репорта.
    """
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="start_report_dialog")]]
    await query.edit_message_text(
        text="Пожалуйста, опишите вашу проблему или задайте вопрос одним сообщением. Это сообщение станет заголовком вашего обращения.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAITING_REPORT_TITLE


async def create_new_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Создает новый репорт на основе сообщения пользователя и уведомляет администраторов.
    """
    user = update.effective_user
    report_title = update.message.text

    if not report_title or len(report_title) < 10:
        await update.message.reply_text("Текст репорта слишком короткий. Пожалуйста, опишите проблему подробнее.")
        return AWAITING_REPORT_TITLE

    # Создаем запись о репорте
    report_id = await db_manager.fetchval(
        "INSERT INTO reports (user_id, title) VALUES ($1, $2) RETURNING id;",
        user.id, report_title
    )

    # Добавляем первое сообщение в историю репорта
    await db_manager.execute(
        "INSERT INTO report_messages (report_id, sender_id, sender_role, content) VALUES ($1, $2, 'user', $3);",
        report_id, user.id, report_title
    )

    logging.info(f"Пользователь {user.id} создал новый репорт #{report_id}.")
    await update.message.reply_text(
        f"✅ Ваш репорт #{report_id} успешно создан. Администрация ответит вам в ближайшее время. Вы получите уведомление."
    )

    # Уведомляем всех администраторов о новом репорте
    admin_records = await db_manager.fetch("SELECT user_id FROM users WHERE is_admin = TRUE OR user_id = $1", SUPER_ADMIN_ID)
    admin_ids = {r['user_id'] for r in admin_records}
    notification_text = f"📢 Новый репорт #{report_id} от пользователя {user.full_name} (`{user.id}`)."
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=notification_text, parse_mode='Markdown')
        except Exception as e:
            logging.warning(f"Не удалось уведомить админа {admin_id} о новом репорте.", exc_info=e)

    await _display_main_menu(update, context, is_new_message=True)
    return ConversationHandler.END


async def view_user_reports_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Отображает список всех репортов пользователя.
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    reports = await db_manager.fetch(
        "SELECT id, title, status, last_message_at FROM reports WHERE user_id = $1 ORDER BY last_message_at DESC;",
        user_id
    )

    text = "📂 **Ваши репорты:**\n\n"
    if not reports:
        text += "У вас пока нет созданных репортов."

    keyboard = []
    for report in reports:
        status_icon = "🔵" if report['status'] == 'open' else "⚪️"
        button_text = f"{status_icon} #{report['id']}: {report['title'][:40]}..."
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"user_open_report_{report['id']}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="start_report_dialog")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except (AttributeError, BadRequest):
        # Если сообщение было удалено (например, после ответа), отправляем новое
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    return VIEWING_USER_REPORTS


async def view_user_report_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Отображает полную переписку по выбранному репорту.
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        report_id = int(query.data.split('_')[-1])
    except (ValueError, IndexError):
        return VIEWING_USER_REPORTS

    context.user_data['current_report_id'] = report_id

    report_info = await db_manager.fetchrow(
        "SELECT id, status FROM reports WHERE id = $1 AND user_id = $2", report_id, user_id
    )
    if not report_info:
        await query.edit_message_text("Ошибка: репорт не найден или у вас нет к нему доступа.")
        return VIEWING_USER_REPORTS

    messages = await db_manager.fetch(
        "SELECT sender_role, content FROM report_messages WHERE report_id = $1 ORDER BY timestamp ASC;",
        report_id
    )

    conversation_text = f"Диалог по репорту #{report_id} (Статус: {report_info['status'].upper()})\n" + "=" * 20 + "\n\n"
    for msg in messages:
        sender = "Вы" if msg['sender_role'] == 'user' else "Администратор"
        conversation_text += f"<b>{sender}:</b>\n{msg['content']}\n\n"

    keyboard = []
    if report_info['status'] == 'open':
        keyboard.append([InlineKeyboardButton("✍️ Ответить", callback_data=f"user_reply_report_{report_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад к списку репортов", callback_data="report_view_mine")])

    await query.edit_message_text(
        text=conversation_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return VIEWING_USER_REPORTS


async def prompt_for_user_reply_in_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запрашивает у пользователя текст ответа в существующем репорте.
    """
    query = update.callback_query
    await query.answer()

    report_id = context.user_data.get('current_report_id')

    keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data=f"user_open_report_{report_id}")]]

    await query.edit_message_text(
        "Пожалуйста, введите ваш ответ и отправьте его в чат.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAITING_USER_REPLY_IN_REPORT


async def handle_user_reply_in_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Сохраняет ответ пользователя в репорте и уведомляет администраторов.
    """
    user = update.effective_user
    report_id = context.user_data.get('current_report_id')
    message_text = update.message.text

    if not report_id:
        await update.message.reply_text("Ошибка: не удалось определить текущий репорт. Начните заново.")
        return ConversationHandler.END

    # Сохраняем сообщение и обновляем время
    await db_manager.execute(
        "INSERT INTO report_messages (report_id, sender_id, sender_role, content) VALUES ($1, $2, 'user', $3);",
        report_id, user.id, message_text
    )
    await db_manager.execute("UPDATE reports SET last_message_at = NOW() WHERE id = $1;", report_id)

    await update.message.reply_text("✅ Ваше сообщение отправлено администрации.")

    # Уведомляем админов
    admin_records = await db_manager.fetch("SELECT user_id FROM users WHERE is_admin = TRUE OR user_id = $1", SUPER_ADMIN_ID)
    admin_ids = {r['user_id'] for r in admin_records}
    notification_text = f"💬 Новый ответ в репорте #{report_id} от пользователя {user.full_name} (`{user.id}`)."
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=notification_text, parse_mode='Markdown')
        except Exception:
            pass

    await _display_main_menu(update, context, is_new_message=True)
    return ConversationHandler.END


# Определяем ConversationHandler для системы репортов
report_conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_report_dialog, pattern='^start_report_dialog$'),
        CallbackQueryHandler(view_user_reports_list, pattern='^report_view_mine$'),  # Прямой доступ к репортам
    ],
    states={
        CHOOSING_REPORT_ACTION: [
            CallbackQueryHandler(prompt_for_report_title, pattern='^report_create_new$'),
            CallbackQueryHandler(view_user_reports_list, pattern='^report_view_mine$'),
        ],
        AWAITING_REPORT_TITLE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, create_new_report),
        ],
        VIEWING_USER_REPORTS: [
            CallbackQueryHandler(view_user_report_conversation, pattern=r'^user_open_report_'),
            CallbackQueryHandler(prompt_for_user_reply_in_report, pattern=r'^user_reply_report_'),
            CallbackQueryHandler(view_user_reports_list, pattern='^report_view_mine$'), # Возврат к списку
        ],
        AWAITING_USER_REPLY_IN_REPORT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_reply_in_report),
            CallbackQueryHandler(view_user_report_conversation, pattern=r'^user_open_report_'), # Отмена
        ],
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(main_menu, pattern='^main_menu$'),
        CallbackQueryHandler(start_report_dialog, pattern='^start_report_dialog$'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
    ],
    per_message=False,
    allow_reentry=True
)

# END OF FILE user_handlers/report_flow.py