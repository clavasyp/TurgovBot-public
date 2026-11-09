# START OF FILE user_handlers/history.py

"""
Модуль для управления диалогом просмотра истории запросов пользователя.

Содержит:
- `view_history`: Отображает список запросов с пагинацией.
- `view_history_message`: Показывает полный текст диалога (запрос-ответ).
- `history_conv_handler`: ConversationHandler для этого диалога.
"""

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, CommandHandler, MessageHandler, filters

# Локальные импорты
from database.manager import db_manager
from .core import personal_account, start, unknown_text_fallback # Импортируем обработчик для кнопки "Назад"
from utils import rate_limited # <<< ДОБАВЛЕНА ЭТА СТРОКА
from config import (
    VIEWING_HISTORY, VIEWING_HISTORY_MESSAGE,
    GENERATION_HISTORY_PREFIX
)

# Константы для пагинации
HISTORY_PAGE_SIZE = 8


@rate_limited  # Используем декоратор из utils, который будет импортирован в main.py
async def view_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Отображает страницу истории запросов (заданных или сгенерированных).
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        _, history_type, page_str = query.data.split('_')
        page = int(page_str)
    except (ValueError, IndexError):
        # В случае ошибки просто остаемся в текущем состоянии
        return VIEWING_HISTORY

    offset = page * HISTORY_PAGE_SIZE

    # Определяем, какой тип истории просматриваем
    if history_type == 'generated':
        where_condition = f"content LIKE '{GENERATION_HISTORY_PREFIX}%'"
        title_text = "📝 История сгенерированных вопросов"
    else:  # 'asked'
        where_condition = f"content NOT LIKE '{GENERATION_HISTORY_PREFIX}%'"
        title_text = "📖 История заданных вопросов"

    # Запрос к БД для получения общего количества и записей на странице
    total_items = await db_manager.fetchval(
        f"SELECT COUNT(*) FROM message_history WHERE user_id = $1 AND role = 'user' AND {where_condition}",
        user_id
    ) or 0
    total_pages = (total_items + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE

    history_records = await db_manager.fetch(
        f"SELECT id, content, timestamp FROM message_history WHERE user_id = $1 AND role = 'user' AND {where_condition} ORDER BY timestamp DESC LIMIT $2 OFFSET $3",
        user_id, HISTORY_PAGE_SIZE, offset
    )

    if not history_records:
        text = "Здесь пока ничего нет."
        keyboard = [[InlineKeyboardButton("◀️ Назад в личный кабинет", callback_data="personal_account")]]
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        return VIEWING_HISTORY

    text = f"<b>{title_text} (Стр. {page + 1} из {total_pages})</b>"
    keyboard = []
    for record in history_records:
        clean_text = record['content'].replace(GENERATION_HISTORY_PREFIX, '').strip()
        question_text = clean_text.replace('\n', ' ')
        button_text = (question_text[:50] + '...') if len(question_text) > 50 else question_text
        callback_data = f"view_msg_{history_type}_{record['id']}_{page}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    # Кнопки пагинации
    pagination_row = []
    if page > 0:
        pagination_row.append(
            InlineKeyboardButton("⬅️ Назад", callback_data=f"history_{history_type}_{page - 1}")
        )
    if (page + 1) < total_pages:
        pagination_row.append(
            InlineKeyboardButton("Вперед ➡️", callback_data=f"history_{history_type}_{page + 1}")
        )
    if pagination_row:
        keyboard.append(pagination_row)

    keyboard.append([InlineKeyboardButton("◀️ Назад в личный кабинет", callback_data="personal_account")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return VIEWING_HISTORY


async def view_history_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Отображает полный текст выбранного из истории диалога (вопрос + ответ).
    """
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    try:
        _, _, history_type, msg_id_str, page_str = query.data.split('_')
        msg_id = int(msg_id_str)
        page = int(page_str)
    except (ValueError, IndexError):
        return VIEWING_HISTORY

    # Находим сам вопрос
    question_record = await db_manager.fetchrow(
        "SELECT content, timestamp FROM message_history WHERE id = $1 AND user_id = $2 AND role = 'user'",
        msg_id, user_id
    )
    if not question_record:
        await query.edit_message_text("Запрошенный диалог не найден.")
        return VIEWING_HISTORY

    # Находим следующий ответ модели после этого вопроса
    answer_record = await db_manager.fetchrow(
        "SELECT content FROM message_history WHERE user_id = $1 AND role = 'model' AND timestamp > $2 ORDER BY timestamp ASC LIMIT 1",
        user_id, question_record['timestamp']
    )

    question_text = question_record['content'].replace(GENERATION_HISTORY_PREFIX, '')
    answer_text = answer_record[
        'content'] if answer_record else "<i>[На этот запрос не было получено ответа или произошла ошибка]</i>"

    final_text = (
        f"<b><u>Ваш запрос:</u></b>\n<i>«{question_text}»</i>\n\n---\n\n"
        f"<b><u>Ответ:</u></b>\n{answer_text}"
    )

    keyboard = [
        [InlineKeyboardButton(f"◀️ Назад к списку (стр. {page + 1})", callback_data=f"history_{history_type}_{page}")]
    ]

    # Обрезаем сообщение, если оно слишком длинное для Telegram
    limit = 4096
    if len(final_text) > limit:
        final_text = final_text[:limit - 4] + "..."

    await query.edit_message_text(
        text=final_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML',
        disable_web_page_preview=True
    )
    return VIEWING_HISTORY_MESSAGE


# Определяем ConversationHandler для просмотра истории
history_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(view_history, pattern=r'^history_(asked|generated)_')],
    states={
        VIEWING_HISTORY: [
            CallbackQueryHandler(view_history, pattern=r'^history_(asked|generated)_'),
            CallbackQueryHandler(view_history_message, pattern=r'^view_msg_(asked|generated)_')
        ],
        VIEWING_HISTORY_MESSAGE: [
            CallbackQueryHandler(view_history, pattern=r'^history_(asked|generated)_')
        ],
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(personal_account, pattern='^personal_account$'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
    ],
    per_message=False,
    allow_reentry=True
)

# END OF FILE user_handlers/history.py