# START OF FILE user_handlers/vip_application_flow.py

"""
Анкета заявки на ВИП/безлимит за промокод (сторона пользователя).

Точка входа — кнопка главного меню «Получить ВИП за промокод» (callback
vip_promo_start), а также deep-link ?start=apply_vip (см. user_handlers/core.py).

Поток: приложить пруфы (видео+скрины) → жать «Готово» → форма из 3 полей
(сервер+проект / логин аккаунта / свободное описание) → подтверждение → заявка
постится в отдельный TG-канал (config.VIP_APPLICATIONS_CHAT_ID) альбомом пруфов +
карточкой с кнопками Одобрить/Отклонить, и пишется в vip_applications (alembic 015).

Это ОТДЕЛЬНЫЙ ConversationHandler с ЛОКАЛЬНЫМИ состояниями (как tester_application_flow).
Политика повтора (одна активная заявка; повтор после отклонения) — в services.vip_applications.

⚠️ Одобрение выдаёт users.is_unlimited (ТОЛЬКО безлимит, без оверлея) — хендлеры
accept/reject в admin_handlers.py.
"""
import html
import logging

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, InputMediaVideo,
)
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters,
)

import config
from database.manager import db_manager
from services.vip_applications import (
    is_intake_enabled,
    get_pending_application,
    create_application,
    set_channel_message,
    get_channel_chat_id,
)
# Метрики активности юзера — дженерик, переиспользуем из заявок тестировщиков.
from services.tester_applications import clip_html, gather_user_metrics, format_metrics_block

logger = logging.getLogger(__name__)

# --- Локальные состояния (не из config.py — отдельный conv, как tester_application) ---
(
    VIP_MEDIA,
    VIP_SERVER,
    VIP_LOGIN,
    VIP_DESC,
    VIP_CONFIRM,
) = range(5)

CB_START = "vip_promo_start"
CB_DONE = "vip_app_done"
CB_CONFIRM = "vip_app_confirm"
CB_RESTART = "vip_app_restart"
CB_CANCEL = "vip_app_cancel"

_DATA_KEY = "vip_app_data"      # {"media": [...], "server": .., "login": .., "desc": ..}
_PROMPT_KEY = "vip_app_prompt"  # (chat_id, message_id) карточки-промпта сбора медиа

_MAX_MEDIA = 15
_MAX_ANSWER_LEN = 1000

_HOME = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])

MEDIA_PROMPT = (
    "💎 <b>Заявка на ВИП за промокод</b>\n\n"
    "Шаг 1/4 — пруфы. Пришли <b>видео и/или скриншоты</b>, где видно, что ты ввёл промокод "
    "на сервере (можно несколько; <b>отправляй как фото/видео, не файлом</b>).\n\n"
    "Когда приложишь всё — нажми «✅ Готово»."
)
SERVER_TEXT = (
    "📝 Шаг 2/4\n\n"
    "На каком <b>сервере и проекте</b> ты вводил промокод? (напр. «Majestic RU19 Memphis»)"
)
LOGIN_TEXT = "📝 Шаг 3/4\n\nУкажи <b>логин аккаунта</b> (игровой/на сервере)."
DESC_TEXT = "📝 Шаг 4/4\n\n<b>Свободное описание</b> (любые детали для проверки). Если нечего добавить — напиши «-»."


def _done_kb(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"✅ Готово ({n})", callback_data=CB_DONE)]])


async def _deny_reason(user_id: int) -> str | None:
    """Текст-отказ, если подать заявку нельзя; иначе None."""
    roles = await db_manager.fetchrow(
        "SELECT is_unlimited, is_admin, is_banned FROM users WHERE user_id = $1", user_id
    )
    if roles and roles['is_banned']:
        return "🚫 Доступ ограничен."
    if roles and (roles['is_unlimited'] or roles['is_admin']):
        return "💎 У тебя уже есть безлимитный статус!"
    if not await is_intake_enabled():
        return "📝 Приём заявок на ВИП сейчас закрыт. Загляни позже!"
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
    context.user_data[_DATA_KEY] = {"media": []}
    try:
        msg = await query.edit_message_text(MEDIA_PROMPT, parse_mode='HTML', reply_markup=_done_kb(0))
        context.user_data[_PROMPT_KEY] = (msg.chat_id, msg.message_id)
    except BadRequest:
        msg = await query.message.reply_text(MEDIA_PROMPT, parse_mode='HTML', reply_markup=_done_kb(0))
        context.user_data[_PROMPT_KEY] = (msg.chat_id, msg.message_id)
    return VIP_MEDIA


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    data = context.user_data.setdefault(_DATA_KEY, {"media": []})
    media = data.setdefault("media", [])
    msg = update.message
    if msg.photo:
        item = {"type": "photo", "file_id": msg.photo[-1].file_id}
    elif msg.video:
        item = {"type": "video", "file_id": msg.video.file_id}
    else:
        return VIP_MEDIA
    if len(media) >= _MAX_MEDIA:
        await msg.reply_text(f"Достаточно пруфов (максимум {_MAX_MEDIA}). Жми «✅ Готово».")
        return VIP_MEDIA
    media.append(item)
    n = len(media)
    # Обновляем счётчик на кнопке промпта (одна карточка, без спама).
    prompt = context.user_data.get(_PROMPT_KEY)
    if prompt:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=prompt[0], message_id=prompt[1], reply_markup=_done_kb(n)
            )
        except BadRequest:
            pass
    return VIP_MEDIA


async def on_media_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """В состоянии сбора медиа прислали текст — мягко напоминаем."""
    await update.message.reply_text(
        "Сейчас приложи <b>пруфы</b> (фото/видео) или нажми «✅ Готово» под карточкой выше.",
        parse_mode='HTML',
    )
    return VIP_MEDIA


async def on_media_filehint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пруф прислали ФАЙЛОМ (document) или иным вложением — просим как фото/видео.
    Без этого хендлера такое сообщение не матчилось бы и терялось молча."""
    await update.message.reply_text(
        "📎 Похоже, ты прислал файл. Отправь скрин/видео <b>как фото или видео</b> "
        "(не как файл/документ) — тогда я их приму. Потом нажми «✅ Готово».",
        parse_mode='HTML',
    )
    return VIP_MEDIA


async def on_media_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    media = context.user_data.get(_DATA_KEY, {}).get("media", [])
    if not media:
        await query.answer("Приложи хотя бы один пруф (фото или видео).", show_alert=True)
        return VIP_MEDIA
    await query.answer()
    try:
        await query.edit_message_text(
            f"✅ Пруфы приложены: {len(media)}.", parse_mode='HTML'
        )
    except BadRequest:
        pass
    await context.bot.send_message(update.effective_chat.id, SERVER_TEXT, parse_mode='HTML')
    return VIP_SERVER


async def on_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Напиши сервер и проект текстом.")
        return VIP_SERVER
    context.user_data.setdefault(_DATA_KEY, {})["server"] = text[:_MAX_ANSWER_LEN]
    await update.message.reply_text(LOGIN_TEXT, parse_mode='HTML')
    return VIP_LOGIN


async def on_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Напиши логин аккаунта текстом.")
        return VIP_LOGIN
    context.user_data.setdefault(_DATA_KEY, {})["login"] = text[:_MAX_ANSWER_LEN]
    await update.message.reply_text(DESC_TEXT, parse_mode='HTML')
    return VIP_DESC


async def on_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("Напиши описание текстом (или «-»).")
        return VIP_DESC
    context.user_data.setdefault(_DATA_KEY, {})["desc"] = text[:_MAX_ANSWER_LEN]
    return await _show_confirm(update, context)


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    d = context.user_data.get(_DATA_KEY, {})
    summary = (
        f"<b>Пруфов приложено:</b> {len(d.get('media', []))}\n\n"
        f"<b>Сервер/проект:</b>\n{html.escape(str(d.get('server', '—')))}\n\n"
        f"<b>Логин:</b>\n{html.escape(str(d.get('login', '—')))}\n\n"
        f"<b>Описание:</b>\n{html.escape(str(d.get('desc', '—')))}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Отправить", callback_data=CB_CONFIRM)],
        [InlineKeyboardButton("✏️ Заполнить заново", callback_data=CB_RESTART)],
        [InlineKeyboardButton("❌ Отмена", callback_data=CB_CANCEL)],
    ])
    text = clip_html("📋 <b>Проверь заявку</b>\n\n" + summary + "\n\nОтправляем?")
    try:
        await update.message.reply_text(text, reply_markup=kb, parse_mode='HTML')
    except BadRequest:
        await update.message.reply_text(text, reply_markup=kb)
    return VIP_CONFIRM


# ---------------------------------------------------------------------------
# Карточка заявки для канала/админки + постинг
# ---------------------------------------------------------------------------

def approve_reject_kb(app_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"vip_ok_{app_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"vip_no_{app_id}"),
        ],
        [InlineKeyboardButton("📝 На доработку (запросить инфо)", callback_data=f"vip_info_{app_id}")],
    ])


def _media_inputs(media: list) -> list:
    out = []
    for m in media:
        fid = m.get("file_id")
        if not fid:
            continue
        if m.get("type") == "video":
            out.append(InputMediaVideo(fid))
        else:
            out.append(InputMediaPhoto(fid))
    return out


async def build_card_text(app: dict, metrics: dict) -> str:
    """HTML-карточка заявки (для канала и для админ-детали)."""
    ans = app.get("answers") or {}
    uid = app.get("user_id")
    name = metrics.get("full_name") or "—"
    uname = metrics.get("username")
    label = html.escape(str(name)) + (f" (@{html.escape(str(uname))})" if uname else "")
    head = "\n".join([
        f"💎 <b>Заявка на ВИП #{app.get('id')}</b>",
        f"👤 {label} (<code>{uid}</code>)",
        f"<b>Сервер/проект:</b> {html.escape(str(ans.get('server', '—')))}",
        f"<b>Логин:</b> {html.escape(str(ans.get('login', '—')))}",
        f"<b>Описание:</b> {html.escape(str(ans.get('desc', '—')))}",
        f"<b>Пруфов:</b> {len(app.get('media') or [])}",
        "",
        format_metrics_block(metrics),
    ])
    return clip_html(head)


async def post_application_card(context: ContextTypes.DEFAULT_TYPE, app: dict, chat_id: int):
    """Отправить заявку (альбом пруфов + карточка с кнопками Одобрить/Отклонить) в chat_id.
    Возвращает отправленное сообщение-карточку (или бросает исключение). Переиспользуется
    каналом заявок и админ-дампом в личку."""
    metrics = await gather_user_metrics(app["user_id"])
    card = await build_card_text(app, metrics)
    inputs = _media_inputs(app.get("media") or [])
    # Альбом(ы) пруфов — у media_group нельзя кнопки, поэтому карточка отдельным сообщением.
    for i in range(0, len(inputs), 10):
        chunk = inputs[i:i + 10]
        if chunk:
            await context.bot.send_media_group(chat_id=chat_id, media=chunk)
    return await context.bot.send_message(
        chat_id=chat_id, text=card, parse_mode='HTML',
        reply_markup=approve_reject_kb(app["id"]),
    )


async def post_application_to_channel(context: ContextTypes.DEFAULT_TYPE, app: dict) -> bool:
    """Запостить заявку в канал заявок (фолбэк — личка супер-админа). Сохраняет ссылку на
    карточку в БД. Возвращает True, если хоть куда-то ушло."""
    # Приоритет: чат из /set_vip_channel (system_cache) → env VIP_APPLICATIONS_CHAT_ID → личка супер-админа.
    target = await get_channel_chat_id() or config.VIP_APPLICATIONS_CHAT_ID or config.SUPER_ADMIN_ID
    if not target:
        logger.warning("[vip_apps] не задан VIP_APPLICATIONS_CHAT_ID и SUPER_ADMIN_ID — заявка #%s не запощена", app.get("id"))
        return False
    try:
        sent = await post_application_card(context, app, target)
        await set_channel_message(app["id"], sent.chat_id, sent.message_id)
        return True
    except Exception:
        logger.exception("[vip_apps] не удалось запостить заявку #%s в %s", app.get("id"), target)
        if target != config.SUPER_ADMIN_ID and config.SUPER_ADMIN_ID:
            try:
                sent = await post_application_card(context, app, config.SUPER_ADMIN_ID)
                await set_channel_message(app["id"], sent.chat_id, sent.message_id)
                return True
            except Exception:
                logger.exception("[vip_apps] фолбэк-постинг заявки #%s супер-админу тоже упал", app.get("id"))
        return False


async def on_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    deny = await _deny_reason(user_id)
    if deny:
        context.user_data.pop(_DATA_KEY, None)
        try:
            await query.edit_message_text(deny, reply_markup=_HOME)
        except BadRequest:
            pass
        return ConversationHandler.END
    d = context.user_data.get(_DATA_KEY, {})
    answers = {"server": d.get("server", ""), "login": d.get("login", ""), "desc": d.get("desc", "")}
    media = d.get("media", [])
    app_id = await create_application(user_id, answers, media)
    context.user_data.pop(_DATA_KEY, None)
    context.user_data.pop(_PROMPT_KEY, None)
    if app_id is None:
        dup_msg = "⏳ Похоже, заявка от тебя уже есть и на рассмотрении."
        try:
            await query.edit_message_text(dup_msg, reply_markup=_HOME)
        except BadRequest:
            await query.message.reply_text(dup_msg, reply_markup=_HOME)
        return ConversationHandler.END
    # Постим в канал заявок (с пруфами и кнопками).
    app = {"id": app_id, "user_id": user_id, "answers": answers, "media": media}
    await post_application_to_channel(context, app)
    ok_msg = ("✅ <b>Заявка отправлена!</b>\n\n"
              "Мы проверим пруфы и сообщим о решении прямо здесь, в боте. Спасибо! 🙌")
    try:
        await query.edit_message_text(ok_msg, reply_markup=_HOME, parse_mode='HTML')
    except BadRequest:
        await query.message.reply_text(ok_msg, reply_markup=_HOME, parse_mode='HTML')
    logger.info("[vip_apps] новая заявка #%s от user_id=%s (пруфов: %s)", app_id, user_id, len(media))
    return ConversationHandler.END


async def on_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data[_DATA_KEY] = {"media": []}
    try:
        msg = await query.edit_message_text(MEDIA_PROMPT, parse_mode='HTML', reply_markup=_done_kb(0))
        context.user_data[_PROMPT_KEY] = (msg.chat_id, msg.message_id)
    except BadRequest:
        msg = await query.message.reply_text(MEDIA_PROMPT, parse_mode='HTML', reply_markup=_done_kb(0))
        context.user_data[_PROMPT_KEY] = (msg.chat_id, msg.message_id)
    return VIP_MEDIA


async def on_confirm_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Нажми «✅ Отправить» или «❌ Отмена» на карточке выше (или /cancel — отменить)."
    )
    return VIP_CONFIRM


async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop(_DATA_KEY, None)
    context.user_data.pop(_PROMPT_KEY, None)
    if update.callback_query:
        await update.callback_query.answer()
        try:
            await update.callback_query.edit_message_text(
                "Отменено. Если передумаешь — открой раздел снова.", reply_markup=_HOME
            )
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text("Отменено.")
    return ConversationHandler.END


vip_application_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(start_application, pattern=f"^{CB_START}$")],
    states={
        VIP_MEDIA: [
            MessageHandler(filters.PHOTO | filters.VIDEO, on_media),
            CallbackQueryHandler(on_media_done, pattern=f"^{CB_DONE}$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_media_text),
            # Пруф файлом/иным вложением — иначе молча терялось бы (не PHOTO/VIDEO/TEXT).
            MessageHandler(filters.Document.ALL | filters.ANIMATION, on_media_filehint),
        ],
        VIP_SERVER: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_server)],
        VIP_LOGIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_login)],
        VIP_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, on_desc)],
        VIP_CONFIRM: [
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
    name="vip_application_conv",
)

# END OF FILE user_handlers/vip_application_flow.py
