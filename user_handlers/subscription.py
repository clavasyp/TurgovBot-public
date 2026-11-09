# START OF FILE user_handlers/subscription.py

"""
Модуль для управления диалогом о подписке на канал.

Содержит:
- `show_subscription_offer`: Показывает предложение о подписке с бонусами.
- `handle_check_subscription`: Проверяет, подписался ли пользователь, и обновляет его статус.
- `subscription_conv_handler`: ConversationHandler для этого диалога.
"""

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, CommandHandler, MessageHandler, filters

# Локальные импорты
import config
from database.manager import db_manager
from subscription_manager import check_subscription
from .core import main_menu, start, unknown_text_fallback
from utils import rate_limited
from config import (
    CHECKING_SUBSCRIPTION,
    TELEGRAM_CHANNEL_URL,
    ASSISTANT_SUBSCRIPTION_BONUSES,
)


@rate_limited
async def show_subscription_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Показывает пользователю предложение о подписке на канал для получения бонусов.
    """
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    is_already_subscriber = await db_manager.fetchval(
        "SELECT is_subscriber FROM users WHERE user_id = $1", user_id
    )

    if is_already_subscriber:
        text = "✅ Вы уже являетесь подписчиком и получаете бонусы! Ваши лимиты увеличены."
        keyboard = [
            [InlineKeyboardButton("👤 Посмотреть лимиты", callback_data="personal_account")],
            [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
        ]
    else:
        # --- ИЗМЕНЕНИЕ: Считаем суммарный бонус для ассистента ---
        total_assistant_bonus = sum(ASSISTANT_SUBSCRIPTION_BONUSES.values())

        text = (
            "💎 **Получите бонусные лимиты за подписку!**\n\n"
            "Подпишитесь на наш Telegram-канал, чтобы получить:\n"
            f"• **+{config.SUBSCRIPTION_BONUS_QUESTION}** вопросов (любой тип)\n"
            f"• **+{config.SUBSCRIPTION_BONUS_INTERVIEW_GEN}** генераций вопросов\n"
            f"• **+{total_assistant_bonus}** консультаций ассистента\n\n"
            "1. Перейдите в канал и подпишитесь.\n"
            "2. Вернитесь сюда и нажмите кнопку «Проверить подписку»."
        )
        keyboard = [
            [InlineKeyboardButton("➡️ Перейти в канал", url=TELEGRAM_CHANNEL_URL)],
            [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscription_now")],
            [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
        ]

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
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    return CHECKING_SUBSCRIPTION


async def handle_check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает нажатие кнопки "Проверить подписку".
    Вызывает `check_subscription` и обновляет статус пользователя в БД.
    """
    query = update.callback_query
    await query.answer("Проверяю подписку...")

    user_id = query.from_user.id
    is_subscribed = await check_subscription(user_id, context)

    # Обновляем статус в базе данных
    await db_manager.execute(
        "UPDATE users SET is_subscriber = $1 WHERE user_id = $2",
        is_subscribed, user_id
    )

    if is_subscribed:
        text = (
            "🎉 **Отлично, подписка подтверждена!**\n\n"
            "Вам начислены бонусные лимиты. Спасибо за поддержку!"
        )
        keyboard = [
            [InlineKeyboardButton("👤 Посмотреть лимиты", callback_data="personal_account")],
            [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return ConversationHandler.END
    else:
        text = (
            "❌ **Подписка не найдена.**\n\n"
            "Пожалуйста, убедитесь, что вы подписались на канал, и попробуйте снова."
        )
        keyboard = [
            [InlineKeyboardButton("➡️ Перейти в канал", url=TELEGRAM_CHANNEL_URL)],
            [InlineKeyboardButton("🔄 Попробовать снова", callback_data="check_subscription_now")],
            [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return CHECKING_SUBSCRIPTION


# Определяем ConversationHandler прямо в этом модуле
subscription_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(show_subscription_offer, pattern='^offer_subscription$')],
    states={
        CHECKING_SUBSCRIPTION: [
            CallbackQueryHandler(handle_check_subscription, pattern='^check_subscription_now$'),
            CallbackQueryHandler(main_menu, pattern='^main_menu$')
        ]
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(main_menu, pattern='^main_menu$'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
    ],
    per_message=False,
    allow_reentry=True
)

# END OF FILE user_handlers/subscription.py