# START OF FILE user_handlers/tester_application_flow.py

"""
Анкета заявки в тестировщики (сторона пользователя).

Точка входа — кнопка «📝 Подать заявку» (callback tester_apply_start), которую
показывает start() по deep-link ?start=apply_tester (см. user_handlers/core.py).
Владелец кидает эту ссылку в свой канал.

Анкета — 3 свободных текстовых вопроса, затем подтверждение и запись в
tester_applications. Это ОТДЕЛЬНЫЙ ConversationHandler с ЛОКАЛЬНЫМИ состояниями
(как test_loophole_flow) — не пересекается с админским admin_conv_handler.

Уведомление админу НЕ шлём: он видит счётчик новых заявок на кнопке в админ-панели.
Политика повтора (одна активная заявка; повтор после отклонения) обеспечивается
сервисом services.tester_applications.
"""
import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters,
)

from database.manager import db_manager
from services.tester_applications import (
    is_intake_enabled,
    get_pending_application,
    create_application,
    clip_html,
)

logger = logging.getLogger(__name__)

# --- Локальные состояния (не из config.py — отдельный conv, как test_loophole) ---
(
    APP_Q1,
    APP_Q2,
    APP_Q3,
    APP_CONFIRM,
) = range(4)

CB_START = "tester_apply_start"
CB_CONFIRM = "tester_apply_confirm"
CB_RESTART = "tester_apply_restart"
CB_CANCEL = "tester_apply_cancel"

_DATA_KEY = "tester_app_answers"

_HOME = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])

Q1_TEXT = (
    "📝 <b>Анкета тестировщика</b> (1/3)\n\n"
    "Расскажи о себе: твой ник, на каких RP-серверах играешь и как долго?\n\n"
    "<i>Ответь сообщением. /cancel — отмена.</i>"
)
Q2_TEXT = (
    "📝 (2/3)\n\n"
    "Почему хочешь стать тестером и что готов делать — искать баги, давать фидбэк, "
    "проверять новые функции?"
)
Q3_TEXT = (
    "📝 (3/3)\n\n"
    "Есть ли у тебя ПК на Windows? Оверлей — десктоп-приложение под Windows. "
    "Укажи версию ОС, если знаешь."
)

_QUESTION_LABELS = {
    "q1": "О себе / опыт в RP",
    "q2": "Мотивация",
    "q3": "ПК / Windows",
}

_MAX_ANSWER_LEN = 1500


async def _deny_reason(user_id: int) -> str | None:
    """Текст-отказ, если подать заявку нельзя; иначе None."""
    roles = await db_manager.fetchrow(
        "SELECT is_tester, is_admin, is_banned FROM users WHERE user_id = $1", user_id
    )
    if roles and roles['is_banned']:
        return "🚫 Доступ ограничен."
    if roles and (roles['is_tester'] or roles['is_admin']):
        return "🧪 Ты уже в команде тестировщиков! Открой /test, чтобы скачать оверлей."
    if not await is_intake_enabled():
        return "📝 Приём заявок в тестировщики сейчас закрыт. Загляни позже!"
    if await get_pending_application(user_id):
        return "⏳ Твоя заявка уже на рассмотрении. Как решим — сообщим прямо здесь."
    return None


async def start_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    deny = await _deny_reason(update.effective_user.id)
    if deny:
        try:
            await query.edit_message_text(deny, reply_markup=_HOME)
        except BadRequest:
            pass
        return ConversationHandler.END
    context.user_data[_DATA_KEY] = {}
    try:
        await query.edit_message_text(Q1_TEXT, parse_mode='HTML')
    except BadRequest:
        await query.message.reply_text(Q1_TEXT, parse_mode='HTML')
    return APP_Q1


async def on_q1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Напиши ответ текстом, пожалуйста.")
        return APP_Q1
    context.user_data.setdefault(_DATA_KEY, {})["q1"] = text[:_MAX_ANSWER_LEN]
    await update.message.reply_text(Q2_TEXT, parse_mode='HTML')
    return APP_Q2


async def on_q2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Напиши ответ текстом, пожалуйста.")
        return APP_Q2
    context.user_data.setdefault(_DATA_KEY, {})["q2"] = text[:_MAX_ANSWER_LEN]
    await update.message.reply_text(Q3_TEXT, parse_mode='HTML')
    return APP_Q3


async def on_q3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Напиши ответ текстом, пожалуйста.")
        return APP_Q3
    context.user_data.setdefault(_DATA_KEY, {})["q3"] = text[:_MAX_ANSWER_LEN]
    return await _show_confirm(update, context)


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    answers = context.user_data.get(_DATA_KEY, {})
    summary = "\n\n".join(
        f"<b>{_QUESTION_LABELS[k]}:</b>\n{html.escape(str(answers.get(k, '—')))}"
        for k in ("q1", "q2", "q3")
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить", callback_data=CB_CONFIRM)],
        [InlineKeyboardButton("✏️ Заполнить заново", callback_data=CB_RESTART)],
        [InlineKeyboardButton("❌ Отмена", callback_data=CB_CANCEL)],
    ])
    # Ответы юзера произвольной длины + html.escape могут раздуть текст за лимит
    # Telegram (4096) — обрезаем готовую HTML-строку по безопасной границе.
    text = clip_html("📋 <b>Проверь анкету</b>\n\n" + summary + "\n\nОтправляем?")
    try:
        await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
    except BadRequest:
        # Подстраховка: уходит без разметки (видны теги, но не падаем).
        await update.message.reply_text(text, reply_markup=kb)
    return APP_CONFIRM


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    # Финальная проверка (вдруг приём закрыли / роль выдали, пока заполнял).
    deny = await _deny_reason(user_id)
    if deny:
        context.user_data.pop(_DATA_KEY, None)
        try:
            await query.edit_message_text(deny, reply_markup=_HOME)
        except BadRequest:
            pass
        return ConversationHandler.END
    answers = context.user_data.get(_DATA_KEY, {})
    app_id = await create_application(user_id, answers)
    context.user_data.pop(_DATA_KEY, None)
    if app_id is None:
        dup_msg = "⏳ Похоже, заявка от тебя уже есть и на рассмотрении."
        try:
            await query.edit_message_text(dup_msg, reply_markup=_HOME)
        except BadRequest:
            await query.message.reply_text(dup_msg, reply_markup=_HOME)
        return ConversationHandler.END
    ok_msg = ("✅ <b>Заявка отправлена!</b>\n\n"
              "Мы рассмотрим её и сообщим о решении прямо здесь, в боте. Спасибо! 🙌")
    try:
        await query.edit_message_text(ok_msg, reply_markup=_HOME, parse_mode='HTML')
    except BadRequest:
        await query.message.reply_text(ok_msg, reply_markup=_HOME, parse_mode='HTML')
    logger.info("[tester_apps] новая заявка #%s от user_id=%s", app_id, user_id)
    return ConversationHandler.END


async def on_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data[_DATA_KEY] = {}
    try:
        await query.edit_message_text(Q1_TEXT, parse_mode='HTML')
    except BadRequest:
        await query.message.reply_text(Q1_TEXT, parse_mode='HTML')
    return APP_Q1


async def on_confirm_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Юзер на шаге подтверждения прислал текст вместо кнопки — мягко напоминаем
    нажать кнопку (карточка с кнопками уже на экране). Состояние сохраняем."""
    await update.message.reply_text(
        "Нажми «✅ Отправить» или «❌ Отмена» на карточке выше (или /cancel — отменить)."
    )
    return APP_CONFIRM


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_DATA_KEY, None)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                "Отменено. Если передумаешь — открой ссылку снова.", reply_markup=_HOME
            )
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text("Отменено.")
    return ConversationHandler.END


tester_application_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(start_application, pattern=f"^{CB_START}$")],
    states={
        APP_Q1: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_q1)],
        APP_Q2: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_q2)],
        APP_Q3: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_q3)],
        APP_CONFIRM: [
            CallbackQueryHandler(on_confirm, pattern=f"^{CB_CONFIRM}$"),
            CallbackQueryHandler(on_restart, pattern=f"^{CB_RESTART}$"),
            CallbackQueryHandler(on_cancel, pattern=f"^{CB_CANCEL}$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_confirm_text),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", on_cancel),
        CommandHandler("start", on_cancel),
        CallbackQueryHandler(on_cancel, pattern=f"^{CB_CANCEL}$"),
        CallbackQueryHandler(on_cancel, pattern="^main_menu$"),
    ],
    per_message=False,
    allow_reentry=True,
    name="tester_application_conv",
)

# END OF FILE user_handlers/tester_application_flow.py
