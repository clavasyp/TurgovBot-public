# START OF FILE admin_handlers.py

import asyncio
import logging
import os
import subprocess
import sys
import secrets
import hashlib
import json
import html
import time
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, MessageOriginUser
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, CallbackQueryHandler, MessageHandler, \
    filters
from telegram.error import Forbidden, BadRequest, RetryAfter

# Локальные импорты.
import config
from database.manager import db_manager
from google_sheets_handler import log_admin_action_to_sheet
from knowledge_base_manager import update_knowledge_base_for_server, update_all_servers_knowledge_base
from config import (
    SUPER_ADMIN_ID,
    API_SERVER_PORT, API_SERVER_HOST, API_DEFAULT_RPM,
    # Состояния FSM
    CHOOSING_ACTION, MANAGE_BANS, MANAGE_VIPS, MANAGE_ADMINS, MANAGE_SERVERS,
    GET_ID_FOR_BAN, GET_ID_FOR_UNBAN, GET_ID_FOR_VIP_ADD, GET_ID_FOR_VIP_REMOVE,
    GET_ID_FOR_ADMIN_ADD, GET_ID_FOR_ADMIN_REMOVE,
    CHOOSE_SERVER_TO_TOGGLE, CHOOSE_SERVER_FOR_UPDATE,
    AWAITING_BROADCAST_MESSAGE, CONFIRMING_BROADCAST, MANAGE_LIMITS, GET_ID_FOR_LIMIT_RESET, CONFIRM_RESET_ALL_LIMITS,
    CHOOSE_SERVER_FOR_FORCE_UPDATE,
    BROADCAST_CHOOSE_AUDIENCE, BROADCAST_AWAITING_MESSAGE, BROADCAST_ATTACH_BUTTONS, BROADCAST_CONFIRM, BROADCAST_RUNNING,
    ADMIN_MANAGE_REPORTS, ADMIN_VIEWING_REPORTS_LIST, ADMIN_CONVERSATION_IN_REPORT, AWAITING_ADMIN_REPLY_IN_REPORT,
    ADMIN_CONFIRM_CLOSE_ALL_REPORTS,
    # Состояния для прецедентов
    ADMIN_MANAGE_PRECEDENTS, ADMIN_CHOOSE_SERVER_PRECEDENTS, ADMIN_VIEWING_PRECEDENTS_LIST, ADMIN_VIEWING_PRECEDENT_DETAIL,
    ADMIN_CHOOSE_SERVER_FORCE_PRECEDENTS,
    # Состояния для тестеров и поиска пользователей
    MANAGE_TESTERS, GET_ID_FOR_TESTER_ADD, GET_ID_FOR_TESTER_REMOVE,
    USER_SEARCH_INPUT, USER_SEARCH_RESULTS, USER_DETAILS_VIEW,
    ADMIN_VIEW_CASE_BY_ID,
    # Состояния для управления API
    MANAGE_API_KEYS, GET_API_KEY_NAME, GET_API_KEY_TYPE, GET_API_KEY_ORIGINS,
    GET_API_KEY_IPS, GET_API_KEY_RPM, CONFIRM_DELETE_API_KEY, VIEW_API_KEY_DETAILS, VIEW_API_KEYS_LIST,
    CONFIRM_ROTATE_API_KEY,
    # Состояния для промо-акций
    MANAGE_PROMO, GET_PROMO_END_DATE,
    # Состояния для очереди анализатора дыр
    ADMIN_LOOPHOLE_QUEUE, ADMIN_LOOPHOLE_JOB_DETAIL, ADMIN_LOOPHOLE_SET_POSITION,
    ADMIN_LOOPHOLE_RESULTS,
    ADMIN_SA_UPLOAD,
    ADMIN_SA_BUCKET,
    # Состояние для просмотра/отбора заявок в тестеры
    ADMIN_TESTER_APPS,
    # Состояние для заявок на ВИП за промокод
    ADMIN_VIP_APPS,
    # Состояния для управления HWID-привязкой лицензий оверлея
    MANAGE_HWID, GET_ID_FOR_HWID_RESET, GET_HWID_FOR_BAN, GET_HWID_FOR_UNBAN, GET_ID_FOR_HWID_VIEW,
)
from services.promo_service import is_promo_active, get_promo_info, set_promo
from user_handlers import main_menu, personal_account, start
from services.law_loophole.queue import (
    list_queued_jobs as lq_list_queued,
    list_running_jobs as lq_list_running,
    get_job as lq_get_job,
    get_queue_position as lq_position,
    cancel_job as lq_cancel,
    move_job_to_top as lq_move_top,
    set_job_position as lq_set_position,
    get_current_position_in_list as lq_current_pos,
    admin_run_out_of_band as lq_run_oob,
    is_queue_paused as lq_is_paused,
    set_queue_paused as lq_set_paused,
    is_loophole_enabled as lq_feature_enabled,
    set_loophole_enabled as lq_set_feature_enabled,
)
from services.law_loophole.analysis_cache import (
    is_caching_enabled as lq_cache_enabled,
    set_caching_enabled as lq_set_cache_enabled,
    cache_count as lq_cache_count,
    clear_cache as lq_clear_cache,
    clear_zero_cache as lq_clear_zero_cache,
    count_zero_cache as lq_count_zero_cache,
    list_analysis_results as lq_list_results,
    count_analysis_results as lq_count_results,
    get_analysis_result as lq_get_result,
)
from services.law_loophole.report import (
    format_loophole_summary as lq_format_summary,
    build_loophole_report_html_doc as lq_build_html_doc,
)
from services.tester_applications import (
    is_intake_enabled as ta_intake_enabled,
    set_intake_enabled as ta_set_intake_enabled,
    count_pending as ta_count_pending,
    count_applications as ta_count_applications,
    list_applications as ta_list_applications,
    get_application as ta_get_application,
    set_application_status as ta_set_status,
    gather_user_metrics as ta_gather_metrics,
    format_metrics_block as ta_format_metrics,
    clip_html as ta_clip_html,
)
from services.vip_applications import (
    is_intake_enabled as vip_intake_enabled,
    set_intake_enabled as vip_set_intake_enabled,
    count_pending as vip_count_pending,
    count_applications as vip_count_applications,
    list_applications as vip_list_applications,
    get_application as vip_get_application,
    set_application_status as vip_set_status,
    get_channel_chat_id as vip_get_channel,
    set_channel_chat_id as vip_set_channel,
)
# Раздел «Обращения в поддержку (сайт)» — самодостаточный модуль.
from admin_support_tickets import manage_support_tickets, support_dump
from services.support_tickets import count_open as supp_count_open


async def handle_forwarded_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Проверка, что есть и пользователь, и обычное сообщение (не канал / не отредактированное)
    if not user or not update.message:
        logging.warning("handle_forwarded_message: нет user или message, игнорирую.")
        return

    # Проверка бана
    user_record = await db_manager.fetchrow("SELECT is_banned, is_admin FROM users WHERE user_id = $1", user.id)
    if user_record and user_record['is_banned']:
        await update.message.reply_text("❌ Вы заблокированы и не можете использовать этого бота.")
        logging.warning(f"Заблокированный пользователь {user.id} попытался использовать handle_forwarded_message.")
        return

    is_admin = (user_record and user_record['is_admin']) or user.id == SUPER_ADMIN_ID

    # PTB 22: атрибут forward_from удалён. Источник пересылки теперь в forward_origin
    # (объект MessageOrigin*). ID исходного отправителя доступен ТОЛЬКО если это пользователь
    # без скрытия профиля (MessageOriginUser); для скрытого профиля/канала/чата ID недоступен.
    origin = update.message.forward_origin
    if is_admin and origin is not None:
        if isinstance(origin, MessageOriginUser):
            fwd_user = origin.sender_user
            logging.info(f"Администратор {user.id} запросил ID пользователя {fwd_user.id} через пересылку сообщения.")
            await update.message.reply_text(
                f"ID пользователя {html.escape(fwd_user.full_name)}: <code>{fwd_user.id}</code>",
                parse_mode='HTML')
        else:
            await update.message.reply_text(
                "Не удалось получить ID: отправитель скрыл профиль настройками приватности "
                "либо сообщение переслано из канала/чата.")


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id

    user_record = await db_manager.fetchrow("SELECT is_admin, is_banned FROM users WHERE user_id = $1", user_id)

    # Проверка бана
    if user_record and user_record['is_banned']:
        logging.warning(f"Заблокированный пользователь {user_id} попытался получить доступ к админ-панели.")
        if update.callback_query:
            await update.callback_query.answer("❌ Вы заблокированы.", show_alert=True)
        return ConversationHandler.END

    is_admin = (user_record and user_record['is_admin']) or user_id == SUPER_ADMIN_ID

    if not is_admin:
        logging.warning(f"Пользователь {user_id} попытался получить доступ к админ-панели без прав.")
        if update.callback_query:
            await update.callback_query.answer("Доступ запрещен.")
        return ConversationHandler.END

    context.user_data.clear()

    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'")
    reports_button_text = f"📬 Управление репортами ({open_reports_count or 0})"

    loophole_queued_count = await db_manager.fetchval(
        "SELECT COUNT(*) FROM loophole_jobs WHERE status = 'queued' AND is_out_of_band = false"
    )
    loophole_button_text = f"🕳 Очередь анализатора ({loophole_queued_count or 0})"

    tester_apps_pending = await ta_count_pending()
    tester_apps_button_text = f"📝 Заявки в тестеры ({tester_apps_pending})"

    vip_apps_pending = await vip_count_pending()
    vip_apps_button_text = f"💎 Заявки на ВИП ({vip_apps_pending})"

    supp_open = await supp_count_open()
    supp_button_text = f"🆘 Обращения ({supp_open})"

    # Проверяем статус режима тех. работ
    maintenance_mode = context.application.bot_data.get('maintenance_mode', False)
    maintenance_status = "🔴 ВКЛ" if maintenance_mode else "🟢 ВЫКЛ"

    keyboard = [
        [InlineKeyboardButton("🚫 Управление банами", callback_data="manage_bans")],
        [InlineKeyboardButton("🖥 HWID-привязка оверлея", callback_data="manage_hwid")],
        [InlineKeyboardButton("👑 Управление VIP", callback_data="manage_vips")],
        [InlineKeyboardButton("🧪 Управление тестерами", callback_data="manage_testers")],
        [InlineKeyboardButton(tester_apps_button_text, callback_data="manage_tester_apps")],
        [InlineKeyboardButton(vip_apps_button_text, callback_data="manage_vip_apps")],
        [InlineKeyboardButton(supp_button_text, callback_data="manage_support")],
        [InlineKeyboardButton("🔍 Поиск пользователей", callback_data="user_search")],
        [InlineKeyboardButton("📋 Просмотр дела по ID", callback_data="view_case_by_id")],
        [InlineKeyboardButton("🖥️ Управление серверами", callback_data="manage_servers")],
        [InlineKeyboardButton("⚖️ Просмотр прецедентов", callback_data="manage_precedents")],
        [InlineKeyboardButton("⚙️ Управление лимитами", callback_data="manage_limits")],
        [InlineKeyboardButton(reports_button_text, callback_data="manage_reports")],
        [InlineKeyboardButton(loophole_button_text, callback_data="manage_loophole_queue")],
        [InlineKeyboardButton("📊 Мониторинг API", callback_data="api_monitoring")],
        [InlineKeyboardButton("🔑 Управление REST API", callback_data="manage_api_keys")],
        [InlineKeyboardButton(f"🛠️ Тех. работы: {maintenance_status}", callback_data="toggle_maintenance")],
    ]
    if user_id == SUPER_ADMIN_ID:
        keyboard.insert(3, [InlineKeyboardButton("🧑‍⚖️ Управление админами", callback_data="manage_admins")])
        keyboard.append([InlineKeyboardButton("📢 Рассылка", callback_data="broadcast")])
        # Кнопка промо-акции
        promo_active = await is_promo_active()
        promo_status = "🟢 ВКЛ" if promo_active else "⚪ ВЫКЛ"
        keyboard.append([InlineKeyboardButton(f"🎉 Промо-акция: {promo_status}", callback_data="manage_promo")])
        keyboard.append([InlineKeyboardButton("🔑 Сменить сервис-аккаунт", callback_data="sa_change")])

    keyboard.append([InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = "🔑 Админ-панель"
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text=text, reply_markup=reply_markup)

    return CHOOSING_ACTION


async def request_user_id(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, action_text: str,
                          state_to_return: int) -> int:
    await query.answer()
    context.user_data['admin_action'] = query.data
    context.user_data['next_state'] = state_to_return
    await query.edit_message_text(f"Введите Telegram ID для {action_text}:")
    return state_to_return


async def modify_user_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        target_id = int(update.message.text)
    except (ValueError, TypeError):
        await update.message.reply_text("ID должен быть числом. Попробуйте снова.")
        return context.user_data.get('next_state')

    target_user_exists = await db_manager.fetchval("SELECT 1 FROM users WHERE user_id = $1", target_id)
    if not target_user_exists:
        await update.message.reply_text(f"Пользователь с ID {target_id} не найден в базе данных.")
        return await admin_panel(update, context)

    action_key = context.user_data.get('admin_action')
    actions_map = {
        'ban_user': ('is_banned', True, 'бан'),
        'unban_user': ('is_banned', False, 'разбан'),
        'add_vip': ('is_vip', True, 'добавление VIP'),
        'remove_vip': ('is_vip', False, 'удаление VIP'),
        'add_admin': ('is_admin', True, 'назначение админом'),
        'remove_admin': ('is_admin', False, 'снятие с админа'),
        'add_tester': ('is_tester', True, 'добавление тестера'),
        'remove_tester': ('is_tester', False, 'удаление тестера'),
    }

    params = actions_map.get(action_key)
    if not params:
        await update.message.reply_text("Произошла внутренняя ошибка. Действие не найдено.")
        logging.error(f"Не найдены параметры для действия '{action_key}' в admin_handlers.modify_user_list.")
        return await admin_panel(update, context)

    db_field, db_value, log_name = params

    if target_id == SUPER_ADMIN_ID and db_field in ('is_admin', 'is_banned'):
        await update.message.reply_text("Нельзя изменить статус или забанить Супер-Админа.")
        return await admin_panel(update, context)

    query = f"UPDATE users SET {db_field} = $1 WHERE user_id = $2;"
    await db_manager.execute(query, db_value, target_id)

    # Бан-каскад на HWID: бан юзера → его привязанное железо в hwid_bans (нельзя переактивировать
    # оверлей на том же ПК даже с нового аккаунта); разбан → снять КАСКАДНЫЕ HWID-баны (ручные не трогаем).
    hwid_note = ""
    if db_field == 'is_banned':
        try:
            from services import license as _lic
            if db_value:
                n = await _lic.ban_hwids_for_user(target_id, banned_by=update.effective_user.id)
                hwid_note = f" (HWID забанено: {n})" if n else " (привязанного HWID нет)"
            else:
                await _lic.unban_hwids_for_user(target_id)
                hwid_note = " (каскадные HWID-баны сняты)"
        except Exception:
            logging.exception("[LICENSE] ban-cascade failed user_id=%s", target_id)

    result_message = f"Статус '{log_name}' успешно {'применен к' if db_value else 'снят с'} пользователя {target_id}.{hwid_note}"
    await update.message.reply_text(result_message)

    log_action = f"{log_name.upper().replace(' ', '_')}"
    logging.info(
        f"Администратор {update.effective_user.id} выполнил действие: {log_action} для пользователя {target_id}.")
    await log_admin_action_to_sheet(update.effective_user.id, log_action, f"Target ID: {target_id}")

    return await admin_panel(update, context)


async def manage_bans_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🚫 Забанить пользователя", callback_data="ban_user")],
        [InlineKeyboardButton("🟢 Разбанить пользователя", callback_data="unban_user")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление блокировками:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_BANS


async def pre_ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "бана", GET_ID_FOR_BAN)


async def pre_unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "разбана", GET_ID_FOR_UNBAN)


# === Управление HWID-привязкой лицензий оверлея ===
async def manage_hwid_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🔓 Сбросить привязку (user_id)", callback_data="hwid_reset")],
        [InlineKeyboardButton("🔎 Показать привязку (user_id)", callback_data="hwid_view")],
        [InlineKeyboardButton("🚫 Забанить HWID", callback_data="hwid_ban")],
        [InlineKeyboardButton("🟢 Разбанить HWID", callback_data="hwid_unban")],
        [InlineKeyboardButton("🗝 Универсальный ключ (законка)", callback_data="unikey_menu")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
    ]
    await query.answer()
    await query.edit_message_text(
        "🖥 <b>Управление HWID-привязкой лицензий</b>\n"
        "Сброс привязки — позволяет юзеру переактивировать ключ на новом ПК.\n"
        "Бан HWID — блокирует железо (как каскад при бане юзера).\n"
        "Универсальный ключ — общий ключ laws-билда (поиск по законке) без привязки к железу.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
    )
    return MANAGE_HWID


async def _request_hwid_input(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE,
                              prompt: str, state: int) -> int:
    await query.answer()
    context.user_data['admin_action'] = query.data
    context.user_data['next_state'] = state
    await query.edit_message_text(prompt)
    return state


async def pre_hwid_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🔓 Сброс привязки — пришли Telegram ID пользователя:", GET_ID_FOR_HWID_RESET)


async def pre_hwid_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🔎 Просмотр привязки — пришли Telegram ID пользователя:", GET_ID_FOR_HWID_VIEW)


async def pre_hwid_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🚫 Бан HWID — пришли строку HWID (отпечаток железа):", GET_HWID_FOR_BAN)


async def pre_hwid_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _request_hwid_input(update.callback_query, context,
        "🟢 Разбан HWID — пришли строку HWID:", GET_HWID_FOR_UNBAN)


async def modify_hwid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Текстовый ввод действий HWID (диспетчеризация по admin_action)."""
    from services import license as _lic
    action = context.user_data.get('admin_action')
    text = (update.message.text or "").strip()
    admin_id = update.effective_user.id
    try:
        if action == 'hwid_reset':
            uid = int(text)
            ok = await _lic.reset_hwid(uid)
            await update.message.reply_text(
                f"✅ Привязка ключа пользователя {uid} сброшена — может активировать на новом ПК."
                if ok else f"У пользователя {uid} нет активного ключа.")
        elif action == 'hwid_view':
            uid = int(text)
            v = await _lic.user_license_view(uid)
            if not v.get("has_key"):
                await update.message.reply_text(f"У пользователя {uid} нет активного ключа.")
            else:
                # Полный HWID в <code> — тап в Telegram копирует целиком (нужно для бана).
                hwid_line = (f"<code>{v['hwid_full']}</code>" if v.get('hwid_full')
                             else "— (не активирован)")
                await update.message.reply_text(
                    f"🔑 Ключ: <code>{v['key']}</code>\n"
                    f"Привязка HWID: {hwid_line}\n"
                    f"Ребайндов: {v['rebind_count']}/{v['rebind_max']}\n"
                    f"HWID забанен: {'да' if v['hwid_banned'] else 'нет'}",
                    parse_mode='HTML')
        elif action == 'hwid_ban':
            await _lic.ban_hwid(text, banned_by=admin_id, reason="manual via admin")
            await update.message.reply_text(f"🚫 HWID забанен:\n<code>{text}</code>", parse_mode='HTML')
        elif action == 'hwid_unban':
            await _lic.unban_hwid(text)
            await update.message.reply_text(f"🟢 HWID разбанен:\n<code>{text}</code>", parse_mode='HTML')
        else:
            await update.message.reply_text("Неизвестное действие.")
    except (ValueError, TypeError):
        await update.message.reply_text("Нужно число (Telegram ID). Попробуй снова.")
        return context.user_data.get('next_state')
    except Exception:
        logging.exception("[LICENSE] modify_hwid_input failed action=%s", action)
        await update.message.reply_text("Ошибка. Подробности в логах.")
    return await admin_panel(update, context)


# === Универсальный ключ laws-билда (поиск по законке) ===
# Один ключ раздаётся всем вместе с laws-установщиком: активация на любом числе машин,
# без HWID-привязки. Отзыв централизованно гасит все устройства (≤ ~15 мин, TTL access-токена).
async def _unikey_render(query: CallbackQuery, notice: str = "") -> int:
    from services import license as _lic
    keys = await _lic.list_universal_keys()
    lines = [notice] if notice else []
    lines.append("🗝 <b>Универсальные ключи laws-билда</b>")
    lines.append("Ключ подходит всем (без привязки к железу), даёт ТОЛЬКО поиск по законке. "
                 "Отзыв гасит все устройства в течение ~15 минут.")
    keyboard = []
    if not keys:
        lines.append("\nАктивных универсальных ключей нет.")
    for k in keys:
        created = f" · создан {k['created_at']:%d.%m.%Y}" if k.get('created_at') else ""
        lines.append(f"\n<code>{k['key']}</code>\nустройств: {k['active_devices']}{created}")
        keyboard.append([InlineKeyboardButton(
            f"⛔️ Отозвать …{k['key'][-5:]}", callback_data=f"unikey_revoke_{k['id']}")])
    keyboard.append([InlineKeyboardButton("➕ Создать ключ", callback_data="unikey_create")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_hwid")])
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return MANAGE_HWID


async def unikey_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _unikey_render(query)


async def unikey_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    from services import license as _lic
    key = await _lic.issue_universal_key(update.effective_user.id)
    return await _unikey_render(query, notice=f"✅ Создан универсальный ключ:\n<code>{key}</code>\n")


async def unikey_revoke_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key_id = int(query.data.rsplit('_', 1)[-1])
    from services import license as _lic
    k = next((x for x in await _lic.list_universal_keys() if x['id'] == key_id), None)
    if not k:
        return await _unikey_render(query, notice="Ключ уже отозван или не найден.\n")
    keyboard = [
        [InlineKeyboardButton("⛔️ Да, отозвать", callback_data=f"unikey_revoke_yes_{key_id}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="unikey_menu")],
    ]
    await query.edit_message_text(
        f"⛔️ Отозвать универсальный ключ?\n\n<code>{k['key']}</code>\n"
        f"Активных устройств: {k['active_devices']}\n\n"
        "ВСЕ laws-билды с этим ключом перестанут работать (в пределах ~15 минут), "
        "повторная активация станет невозможна. Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return MANAGE_HWID


async def unikey_revoke_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    key_id = int(query.data.rsplit('_', 1)[-1])
    from services import license as _lic
    n = await _lic.revoke_universal_key(key_id)
    notice = ("Ключ уже был отозван или не найден.\n" if n is None
              else f"⛔️ Ключ отозван, устройств погашено: {n}.\n")
    return await _unikey_render(query, notice=notice)


async def manage_vips_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("➕ Добавить VIP", callback_data="add_vip")],
        [InlineKeyboardButton("➖ Удалить VIP", callback_data="remove_vip")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление VIP пользователями:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_VIPS


async def pre_add_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "добавления VIP", GET_ID_FOR_VIP_ADD)


async def pre_remove_vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "удаления VIP", GET_ID_FOR_VIP_REMOVE)


# === УПРАВЛЕНИЕ ТЕСТЕРАМИ ===

async def manage_testers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню управления тестировщиками."""
    query = update.callback_query

    # Получаем список текущих тестеров
    testers = await db_manager.fetch(
        "SELECT user_id, full_name, username FROM users WHERE is_tester = TRUE ORDER BY full_name"
    )

    text = "🧪 <b>Управление тестировщиками</b>\n\n"
    if testers:
        text += "<b>Текущие тестеры:</b>\n"
        for t in testers[:10]:  # Показываем первых 10
            username_str = f"@{t['username']}" if t['username'] else "без username"
            text += f"• {t['full_name']} ({username_str}) — <code>{t['user_id']}</code>\n"
        if len(testers) > 10:
            text += f"\n<i>...и ещё {len(testers) - 10}</i>\n"
    else:
        text += "<i>Нет назначенных тестировщиков.</i>\n"

    text += "\n<i>Тестировщики имеют доступ к бета-функциям (например, создание уголовных дел).</i>"

    keyboard = [
        [InlineKeyboardButton("➕ Добавить тестера", callback_data="add_tester")],
        [InlineKeyboardButton("➖ Удалить тестера", callback_data="remove_tester")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_TESTERS


async def pre_add_tester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "добавления тестера", GET_ID_FOR_TESTER_ADD)


async def pre_remove_tester(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "удаления тестера", GET_ID_FOR_TESTER_REMOVE)


# === РЕЖИМ ТЕХНИЧЕСКИХ РАБОТ ===

async def toggle_maintenance_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключает режим технических работ."""
    query = update.callback_query

    current_mode = context.application.bot_data.get('maintenance_mode', False)
    new_mode = not current_mode
    context.application.bot_data['maintenance_mode'] = new_mode
    # Персистим — чтобы режим переживал рестарт bot/БД.
    try:
        from utils import set_maintenance_mode
        await set_maintenance_mode(new_mode)
    except Exception:
        logging.warning("Не удалось сохранить флаг тех. работ в system_cache", exc_info=True)

    status_text = "🔴 ВКЛЮЧЁН" if new_mode else "🟢 ВЫКЛЮЧЕН"
    logging.info(f"Администратор {query.from_user.id} {'включил' if new_mode else 'выключил'} режим тех. работ")

    await query.answer(f"Режим тех. работ {status_text}", show_alert=True)

    # Возвращаемся в админ-панель для обновления статуса кнопки
    return await admin_panel(update, context)


# === ПОИСК ПОЛЬЗОВАТЕЛЕЙ ===

async def start_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает процесс поиска пользователей."""
    query = update.callback_query
    await query.answer()

    text = (
        "🔍 <b>Поиск пользователей</b>\n\n"
        "Введите запрос для поиска:\n"
        "• Telegram ID (число)\n"
        "• Username (с @ или без)\n"
        "• Имя или часть имени\n\n"
        "<i>Будут найдены первые 20 совпадений.</i>"
    )

    keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return USER_SEARCH_INPUT


async def execute_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет поиск пользователей по запросу."""
    search_query = update.message.text.strip()

    # Сохраняем запрос для пагинации
    context.user_data['search_query'] = search_query
    context.user_data['search_page'] = 0

    # Определяем тип поиска
    results = []

    # Убираем @ если есть
    clean_query = search_query.lstrip('@')

    # Пробуем как ID
    try:
        user_id = int(clean_query)
        results = await db_manager.fetch(
            "SELECT user_id, full_name, username, is_admin, is_vip, is_unlimited, is_banned, is_tester "
            "FROM users WHERE user_id = $1",
            user_id
        )
    except ValueError:
        # Ищем по username или имени
        results = await db_manager.fetch(
            """
            SELECT user_id, full_name, username, is_admin, is_vip, is_unlimited, is_banned, is_tester
            FROM users
            WHERE username ILIKE $1 OR full_name ILIKE $1
            ORDER BY last_seen DESC
            LIMIT 20
            """,
            f"%{clean_query}%"
        )

    context.user_data['search_results'] = [dict(r) for r in results]

    return await show_user_search_results(update, context, is_message=True)


async def show_user_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE, is_message: bool = False) -> int:
    """Показывает результаты поиска с пагинацией (10 на страницу)."""
    results = context.user_data.get('search_results', [])
    page = context.user_data.get('search_page', 0)
    search_query = context.user_data.get('search_query', '')

    per_page = 10
    total_pages = (len(results) + per_page - 1) // per_page if results else 1
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_results = results[start_idx:end_idx]

    if not results:
        text = f"🔍 <b>Результаты поиска:</b> «{search_query}»\n\n<i>Ничего не найдено.</i>"
    else:
        text = f"🔍 <b>Результаты поиска:</b> «{search_query}»\n"
        text += f"Найдено: {len(results)} | Страница {page + 1}/{total_pages}\n\n"

        for user in page_results:
            # Формируем статусы
            statuses = []
            if user['is_admin']:
                statuses.append("👑 Админ")
            if user['is_vip']:
                statuses.append("💎 VIP")
            if user.get('is_unlimited'):
                statuses.append("♾ Безлимит")
            if user['is_tester']:
                statuses.append("🧪 Тестер")
            if user['is_banned']:
                statuses.append("🚫 Бан")

            status_str = " | ".join(statuses) if statuses else "👤 Обычный"
            username_str = f"@{user['username']}" if user['username'] else "—"

            text += f"<b>{user['full_name']}</b>\n"
            text += f"   ID: <code>{user['user_id']}</code> | {username_str}\n"
            text += f"   {status_str}\n\n"

    # Кнопки
    keyboard = []

    # Кнопки для выбора пользователя
    for user in page_results:
        btn_text = f"👤 {user['full_name'][:20]}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"user_details_{user['user_id']}")])

    # Пагинация
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data="search_prev_page"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data="search_next_page"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔍 Новый поиск", callback_data="user_search")])
    keyboard.append([InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_message:
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=reply_markup)
    else:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(text, parse_mode='HTML', reply_markup=reply_markup)

    return USER_SEARCH_RESULTS


async def search_change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меняет страницу результатов поиска."""
    query = update.callback_query

    if query.data == "search_prev_page":
        context.user_data['search_page'] = max(0, context.user_data.get('search_page', 0) - 1)
    else:  # search_next_page
        context.user_data['search_page'] = context.user_data.get('search_page', 0) + 1

    return await show_user_search_results(update, context)


async def show_user_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает карточку пользователя с кнопками управления."""
    query = update.callback_query
    await query.answer()

    user_id = int(query.data.split("user_details_")[1])
    context.user_data['view_user_id'] = user_id
    return await _show_user_details_by_id(update, context, user_id)


async def quick_user_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Быстрое действие над пользователем из карточки."""
    query = update.callback_query
    data = query.data

    # Парсим действие и user_id
    if data.startswith("quick_ban_"):
        user_id = int(data.split("quick_ban_")[1])
        await db_manager.execute("UPDATE users SET is_banned = TRUE WHERE user_id = $1", user_id)
        action_text = "заблокирован"
        try:
            from services import license as _lic
            await _lic.ban_hwids_for_user(user_id, banned_by=query.from_user.id)  # каскад HWID-бана
        except Exception:
            logging.exception("[LICENSE] quick ban-cascade failed user_id=%s", user_id)
    elif data.startswith("quick_unban_"):
        user_id = int(data.split("quick_unban_")[1])
        await db_manager.execute("UPDATE users SET is_banned = FALSE WHERE user_id = $1", user_id)
        action_text = "разблокирован"
        try:
            from services import license as _lic
            await _lic.unban_hwids_for_user(user_id)  # снять каскадные HWID-баны
        except Exception:
            logging.exception("[LICENSE] quick unban-cascade failed user_id=%s", user_id)
    elif data.startswith("quick_add_vip_"):
        user_id = int(data.split("quick_add_vip_")[1])
        await db_manager.execute("UPDATE users SET is_vip = TRUE WHERE user_id = $1", user_id)
        action_text = "получил VIP"
    elif data.startswith("quick_remove_vip_"):
        user_id = int(data.split("quick_remove_vip_")[1])
        await db_manager.execute("UPDATE users SET is_vip = FALSE WHERE user_id = $1", user_id)
        action_text = "лишён VIP"
    elif data.startswith("quick_add_unlim_"):
        user_id = int(data.split("quick_add_unlim_")[1])
        await db_manager.execute("UPDATE users SET is_unlimited = TRUE WHERE user_id = $1", user_id)
        action_text = "получил безлимит (промокод)"
    elif data.startswith("quick_remove_unlim_"):
        user_id = int(data.split("quick_remove_unlim_")[1])
        await db_manager.execute("UPDATE users SET is_unlimited = FALSE WHERE user_id = $1", user_id)
        action_text = "лишён безлимита (промокод)"
    elif data.startswith("quick_add_tester_"):
        user_id = int(data.split("quick_add_tester_")[1])
        await db_manager.execute("UPDATE users SET is_tester = TRUE WHERE user_id = $1", user_id)
        action_text = "стал тестером"
    elif data.startswith("quick_remove_tester_"):
        user_id = int(data.split("quick_remove_tester_")[1])
        await db_manager.execute("UPDATE users SET is_tester = FALSE WHERE user_id = $1", user_id)
        action_text = "лишён статуса тестера"
    else:
        await query.answer("Неизвестное действие", show_alert=True)
        return USER_DETAILS_VIEW

    logging.info(f"Админ {query.from_user.id} выполнил действие: пользователь {user_id} {action_text}")
    await log_admin_action_to_sheet(query.from_user.id, f"QUICK_{data.upper()}", f"Target ID: {user_id}")

    await query.answer(f"✅ Пользователь {action_text}", show_alert=True)

    # Обновляем карточку — сохраняем user_id для повторного показа
    context.user_data['view_user_id'] = user_id
    return await _show_user_details_by_id(update, context, user_id)


async def _show_user_details_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> int:
    """Внутренняя функция для показа карточки пользователя по ID."""
    query = update.callback_query

    user = await db_manager.fetchrow(
        """
        SELECT user_id, full_name, username, is_admin, is_vip, is_unlimited, is_banned, is_tester,
               first_seen, last_seen
        FROM users WHERE user_id = $1
        """,
        user_id
    )

    if not user:
        await query.edit_message_text(
            "❌ Пользователь не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]])
        )
        return CHOOSING_ACTION

    # Формируем карточку
    statuses = []
    if user['is_admin']:
        statuses.append("👑 Администратор")
    if user['is_vip']:
        statuses.append("💎 VIP")
    if user.get('is_unlimited'):
        statuses.append("♾ Безлимит (промокод)")
    if user.get('is_tester'):
        statuses.append("🧪 Тестировщик")
    if user['is_banned']:
        statuses.append("🚫 Заблокирован")
    if not statuses:
        statuses.append("👤 Обычный пользователь")

    username_str = f"@{user['username']}" if user['username'] else "не указан"
    first_seen = user['first_seen'].strftime('%d.%m.%Y %H:%M') if user['first_seen'] else "—"
    last_seen = user['last_seen'].strftime('%d.%m.%Y %H:%M') if user['last_seen'] else "—"

    text = (
        f"👤 <b>Карточка пользователя</b>\n\n"
        f"<b>Имя:</b> {user['full_name']}\n"
        f"<b>Username:</b> {username_str}\n"
        f"<b>ID:</b> <code>{user['user_id']}</code>\n\n"
        f"<b>Статусы:</b>\n" + "\n".join(f"  • {s}" for s in statuses) + "\n\n"
        f"<b>Первый визит:</b> {first_seen}\n"
        f"<b>Последний визит:</b> {last_seen}"
    )

    # Кнопки управления
    keyboard = []

    # Бан/Разбан
    if user['is_banned']:
        keyboard.append([InlineKeyboardButton("🟢 Разбанить", callback_data=f"quick_unban_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🚫 Забанить", callback_data=f"quick_ban_{user_id}")])

    # VIP
    if user['is_vip']:
        keyboard.append([InlineKeyboardButton("💎 Снять VIP", callback_data=f"quick_remove_vip_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("💎 Дать VIP", callback_data=f"quick_add_vip_{user_id}")])

    # Безлимит за промокод (is_unlimited): безлимит + оверлей, как VIP
    if user.get('is_unlimited'):
        keyboard.append([InlineKeyboardButton("♾ Снять безлимит", callback_data=f"quick_remove_unlim_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("♾ Дать безлимит", callback_data=f"quick_add_unlim_{user_id}")])

    # Тестер
    if user.get('is_tester'):
        keyboard.append([InlineKeyboardButton("🧪 Снять тестера", callback_data=f"quick_remove_tester_{user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🧪 Дать тестера", callback_data=f"quick_add_tester_{user_id}")])

    keyboard.append([InlineKeyboardButton("◀️ К результатам", callback_data="back_to_search_results")])
    keyboard.append([InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")])

    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return USER_DETAILS_VIEW



async def back_to_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает к результатам поиска."""
    return await show_user_search_results(update, context)


# === ПРОСМОТР ДЕЛА ПО ID ===

async def prompt_view_case_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает у админа ID дела для просмотра."""
    query = update.callback_query
    await query.answer()

    text = (
        "📋 <b>Просмотр дела по ID</b>\n\n"
        "Введите номер дела (ID) для просмотра:\n\n"
        "<i>Это может быть ID дела из Ролевого Помощника (assistant_cases) "
        "или ID уголовного дела (criminal_case_sessions).</i>"
    )

    keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_VIEW_CASE_BY_ID


async def view_case_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает информацию о деле по введённому ID."""
    case_id_str = update.message.text.strip()

    try:
        case_id = int(case_id_str)
    except ValueError:
        await update.message.reply_text(
            "❌ ID должен быть числом. Попробуйте снова или нажмите 'Отмена'.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]])
        )
        return ADMIN_VIEW_CASE_BY_ID

    # Пробуем найти в assistant_cases
    assistant_case = await db_manager.fetchrow("""
        SELECT ac.id, ac.user_id, ac.server_id, ac.rp_role, ac.case_stage, ac.status,
               ac.created_at, ac.last_updated_at,
               u.full_name, u.username
        FROM assistant_cases ac
        LEFT JOIN users u ON ac.user_id = u.user_id
        WHERE ac.id = $1
    """, case_id)

    # Пробуем найти в criminal_case_sessions
    criminal_case = await db_manager.fetchrow("""
        SELECT ccs.id, ccs.user_id, ccs.case_number, ccs.server_id, ccs.status,
               ccs.created_at, ccs.updated_at, ccs.google_doc_url,
               u.full_name, u.username
        FROM criminal_case_sessions ccs
        LEFT JOIN users u ON ccs.user_id = u.user_id
        WHERE ccs.id = $1
    """, case_id)

    if not assistant_case and not criminal_case:
        await update.message.reply_text(
            f"❌ Дело с ID <code>{case_id}</code> не найдено ни в Ролевом Помощнике, ни в Уголовных делах.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
        return CHOOSING_ACTION

    text = ""
    keyboard = []

    # Если нашли в assistant_cases
    if assistant_case:
        from user_handlers.assistant_flow import ASSISTANT_ROLES, ASSISTANT_STAGES

        role_name = ASSISTANT_ROLES.get(assistant_case['rp_role'], assistant_case['rp_role'])
        stage_name = ASSISTANT_STAGES.get(assistant_case['case_stage'], assistant_case['case_stage'])
        server_name = config.SERVERS_CONFIG.get(assistant_case['server_id'], {}).get('display_name', assistant_case['server_id'])

        username_str = f"@{assistant_case['username']}" if assistant_case['username'] else "—"
        created = assistant_case['created_at'].strftime('%d.%m.%Y %H:%M') if assistant_case['created_at'] else "—"
        updated = assistant_case['last_updated_at'].strftime('%d.%m.%Y %H:%M') if assistant_case['last_updated_at'] else "—"

        status_emoji = "🟢" if assistant_case['status'] == 'active' else "🔴"

        text = (
            f"📋 <b>Дело Ролевого Помощника #{case_id}</b>\n\n"
            f"<b>Пользователь:</b> {assistant_case['full_name']} ({username_str})\n"
            f"<b>User ID:</b> <code>{assistant_case['user_id']}</code>\n\n"
            f"<b>Сервер:</b> {server_name}\n"
            f"<b>Роль:</b> {role_name}\n"
            f"<b>Стадия:</b> {stage_name}\n"
            f"<b>Статус:</b> {status_emoji} {assistant_case['status']}\n\n"
            f"<b>Создано:</b> {created}\n"
            f"<b>Обновлено:</b> {updated}"
        )

        keyboard.append([InlineKeyboardButton("💬 Показать историю сообщений", callback_data=f"admin_case_history_{case_id}")])
        if assistant_case['status'] == 'active':
            keyboard.append([InlineKeyboardButton("🔴 Закрыть дело", callback_data=f"admin_close_assist_{case_id}")])

    # Если нашли в criminal_case_sessions
    if criminal_case:
        if text:
            text += "\n\n" + "=" * 30 + "\n\n"

        server_name = config.SERVERS_CONFIG.get(criminal_case['server_id'], {}).get('display_name', criminal_case['server_id'] or "—")
        username_str = f"@{criminal_case['username']}" if criminal_case['username'] else "—"
        created = criminal_case['created_at'].strftime('%d.%m.%Y %H:%M') if criminal_case['created_at'] else "—"
        updated = criminal_case['updated_at'].strftime('%d.%m.%Y %H:%M') if criminal_case['updated_at'] else "—"

        status_emoji = "🟢" if criminal_case['status'] == 'active' else "✅" if criminal_case['status'] == 'completed' else "🔴"

        text += (
            f"📋 <b>Уголовное дело #{case_id}</b>\n\n"
            f"<b>Номер дела:</b> {criminal_case['case_number'] or '—'}\n"
            f"<b>Пользователь:</b> {criminal_case['full_name']} ({username_str})\n"
            f"<b>User ID:</b> <code>{criminal_case['user_id']}</code>\n\n"
            f"<b>Сервер:</b> {server_name}\n"
            f"<b>Статус:</b> {status_emoji} {criminal_case['status']}\n\n"
            f"<b>Создано:</b> {created}\n"
            f"<b>Обновлено:</b> {updated}"
        )

        if criminal_case['google_doc_url']:
            keyboard.append([InlineKeyboardButton("📄 Открыть Google Doc", url=criminal_case['google_doc_url'])])
        if criminal_case['status'] == 'active':
            keyboard.append([InlineKeyboardButton("🔴 Закрыть дело", callback_data=f"admin_close_ccw_{case_id}")])

    keyboard.append([InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")])
    keyboard.append([InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")])

    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_ACTION


async def show_admin_case_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает историю сообщений дела для администратора."""
    query = update.callback_query
    await query.answer()

    case_id = int(query.data.split("admin_case_history_")[1])

    # Получаем историю сообщений
    messages = await db_manager.fetch("""
        SELECT sender_role, content, timestamp
        FROM assistant_messages
        WHERE case_id = $1
        ORDER BY timestamp ASC
    """, case_id)

    if not messages:
        await query.edit_message_text(
            f"📋 <b>История дела #{case_id}</b>\n\n<i>Сообщений не найдено.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад к делу", callback_data=f"view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
        return CHOOSING_ACTION

    # Формируем текст истории
    text = f"📋 <b>История дела #{case_id}</b>\n"
    text += f"<i>Всего сообщений: {len(messages)}</i>\n\n"

    for msg in messages:
        sender = "👤 Пользователь" if msg['sender_role'] == 'user' else "🤖 Ассистент"
        time_str = msg['timestamp'].strftime('%d.%m %H:%M') if msg['timestamp'] else ""

        # Ограничиваем длину сообщения
        content = msg['content']
        if len(content) > 500:
            content = content[:500] + "..."

        # Экранируем HTML
        import html
        content = html.escape(content)

        text += f"<b>{sender}</b> <i>({time_str})</i>\n{content}\n\n"

        # Ограничиваем общую длину
        if len(text) > 3500:
            text += f"\n<i>... и ещё {len(messages) - messages.index(msg) - 1} сообщений</i>"
            break

    await query.edit_message_text(
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
            [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
        ])
    )
    return CHOOSING_ACTION


async def admin_close_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Закрывает дело из админ-панели (assistant_cases или criminal_case_sessions)."""
    query = update.callback_query
    await query.answer()

    # callback_data: admin_close_assist_{id} или admin_close_ccw_{id}
    parts = query.data.split("_")  # ['admin', 'close', 'assist'|'ccw', '{id}']
    case_type = parts[2]  # 'assist' или 'ccw'
    case_id = int(parts[3])

    if case_type == "assist":
        await db_manager.execute(
            "UPDATE assistant_cases SET status = 'closed', last_updated_at = NOW() WHERE id = $1", case_id
        )
        logging.info(f"[Admin] Администратор {query.from_user.id} закрыл дело ассистента #{case_id}")
        await log_admin_action_to_sheet(query.from_user.id, "CLOSE_ASSISTANT_CASE", f"Case ID: {case_id}")
        await query.edit_message_text(
            f"✅ Дело Ролевого Помощника <b>#{case_id}</b> закрыто.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
    elif case_type == "ccw":
        await db_manager.execute(
            "UPDATE criminal_case_sessions SET status = 'closed', updated_at = NOW() WHERE id = $1", case_id
        )
        logging.info(f"[Admin] Администратор {query.from_user.id} закрыл уголовное дело #{case_id}")
        await log_admin_action_to_sheet(query.from_user.id, "CLOSE_CRIMINAL_CASE", f"Case ID: {case_id}")
        await query.edit_message_text(
            f"✅ Уголовное дело <b>#{case_id}</b> закрыто.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Искать другое дело", callback_data="view_case_by_id")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel")]
            ])
        )
    else:
        await query.edit_message_text("❌ Неизвестный тип дела.")

    return CHOOSING_ACTION


async def manage_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Доступ запрещен.", show_alert=True)
        return CHOOSING_ACTION
    keyboard = [
        [InlineKeyboardButton("➕ Добавить админа", callback_data="add_admin")],
        [InlineKeyboardButton("➖ Удалить админа", callback_data="remove_admin")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление администраторами:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_ADMINS


async def pre_add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "добавления админа", GET_ID_FOR_ADMIN_ADD)


async def pre_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(update.callback_query, context, "удаления админа", GET_ID_FOR_ADMIN_REMOVE)


async def manage_servers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("🚦 Вкл/Выкл серверы", callback_data="toggle_server_list")],
        [InlineKeyboardButton("🔄 Обновить базу знаний (по одному)", callback_data="update_kb_list")],
        [InlineKeyboardButton("💥 Принудительно обновить (по одному)", callback_data="force_update_kb_list")],
        [InlineKeyboardButton("🌐 Обновить ВСЕ серверы", callback_data="update_all_servers")],
        [InlineKeyboardButton("💥 Принудительно обновить ВСЕ", callback_data="force_update_all_servers")],
        [InlineKeyboardButton("⚖️ Синхронизировать прецеденты", callback_data="sync_precedents")],
        [InlineKeyboardButton("💥 Принудительно обновить прецеденты", callback_data="force_sync_precedents_list")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text("Управление серверами:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_SERVERS


async def choose_server_to_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с группировкой по проектам для включения/выключения."""
    query = update.callback_query
    await query.answer()

    # Получаем все серверы с информацией о проекте
    server_records = await db_manager.fetch("""
        SELECT s.server_id, s.display_name, s.is_disabled, s.project_id, p.display_name as project_name
        FROM servers s
        LEFT JOIN projects p ON s.project_id = p.project_id
        ORDER BY p.display_name, s.display_name;
    """)

    keyboard = []
    current_project = None

    for record in server_records:
        project_name = record['project_name'] or 'Без проекта'
        project_id = record['project_id']

        # Добавляем разделитель для нового проекта
        if project_id != current_project:
            current_project = project_id
            keyboard.append([InlineKeyboardButton(f"📂 {project_name}", callback_data="noop")])

        status_icon = "🅾️" if record['is_disabled'] else "✅"
        button_text = f"{status_icon} {record['display_name']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"toggle_{record['server_id']}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])
    await query.edit_message_text("Выберите сервер для изменения статуса:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_SERVER_TO_TOGGLE


async def toggle_server_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    server_id_to_toggle = query.data.split("_", 1)[1]
    new_status = await db_manager.fetchval(
        "UPDATE servers SET is_disabled = NOT is_disabled WHERE server_id = $1 RETURNING is_disabled;",
        server_id_to_toggle
    )
    status_text = 'ВЫКЛЮЧЕН' if new_status else 'ВКЛЮЧЕН'
    await query.answer(f"Статус изменен на {status_text}")
    log_action = f"SERVER_{'DISABLE' if new_status else 'ENABLE'}"
    logging.info(f"Администратор {query.from_user.id} изменил статус сервера {server_id_to_toggle} на: {status_text}.")
    await log_admin_action_to_sheet(query.from_user.id, log_action, f"Server ID: {server_id_to_toggle}")
    return await choose_server_to_toggle(update, context)


async def choose_server_for_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с группировкой по проектам для обновления БЗ."""
    query = update.callback_query
    await query.answer()

    keyboard = []
    from config import PROJECTS_CONFIG

    for project_id, project_data in PROJECTS_CONFIG.items():
        project_name = project_data.get('display_name', project_id)
        keyboard.append([InlineKeyboardButton(f"📂 {project_name}", callback_data="noop")])

        servers = project_data.get('servers', {})
        for server_id, server_data in servers.items():
            button_text = f"🔄 {server_data['display_name']}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"update_{server_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])
    await query.edit_message_text("Выберите сервер, базу знаний которого нужно обновить:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_SERVER_FOR_UPDATE


async def choose_server_for_force_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с группировкой по проектам для принудительного обновления БЗ."""
    query = update.callback_query
    await query.answer()

    keyboard = []
    from config import PROJECTS_CONFIG

    for project_id, project_data in PROJECTS_CONFIG.items():
        project_name = project_data.get('display_name', project_id)
        keyboard.append([InlineKeyboardButton(f"📂 {project_name}", callback_data="noop")])

        servers = project_data.get('servers', {})
        for server_id, server_data in servers.items():
            button_text = f"💥 {server_data['display_name']}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"force_update_{server_id}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])
    await query.edit_message_text("Выберите сервер для ПРИНУДИТЕЛЬНОГО обновления:",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_SERVER_FOR_FORCE_UPDATE


async def run_kb_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    server_id_to_update = query.data.split("_", 1)[1]
    server_name = config.SERVERS_CONFIG[server_id_to_update]['display_name']
    await query.answer(f"Запуск обновления для {server_name}...")
    await query.edit_message_text(
        f"⏳ Начинаю полное обновление базы знаний для сервера *{server_name}*... Это может занять несколько минут. Я сообщу о результате.",
        parse_mode="Markdown")
    logging.info(f"Администратор {query.from_user.id} запустил ручное обновление БЗ для сервера {server_id_to_update}.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_UPDATE_SINGLE", f"Server ID: {server_id_to_update}")
    asyncio.create_task(
        update_server_and_notify_admin(context, query.message.chat_id, query.message.message_id, server_id_to_update,
                                       force=False))
    return MANAGE_SERVERS


async def run_kb_force_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    server_id_to_update = query.data.split("_", 2)[2]  # force_update_SERVERID
    server_name = config.SERVERS_CONFIG[server_id_to_update]['display_name']
    await query.answer(f"Запуск ПРИНУДИТЕЛЬНОГО обновления для {server_name}...")
    await query.edit_message_text(
        f"⏳ Начинаю **принудительное** полное обновление базы знаний для сервера *{server_name}*... Это может занять несколько минут. Я сообщу о результате.",
        parse_mode="Markdown")
    logging.info(
        f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ ручное обновление БЗ для сервера {server_id_to_update}.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_FORCE_UPDATE_SINGLE", f"Server ID: {server_id_to_update}")
    asyncio.create_task(
        update_server_and_notify_admin(context, query.message.chat_id, query.message.message_id, server_id_to_update,
                                       force=True))
    return MANAGE_SERVERS


async def update_server_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int,
                                         server_id: str, force: bool = False):
    server_name = config.SERVERS_CONFIG[server_id]['display_name']
    success = await update_knowledge_base_for_server(server_id, force_update=force)
    final_text = (
        f"✅ {'Принудительное о' if force else 'О'}бновление базы знаний для *{server_name}* успешно завершено!" if success
        else f"❌ Ошибка при обновлении *{server_name}*. Проверьте лог-файлы для подробностей.")
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=final_text,
                                            parse_mode="Markdown")
    except Exception as e:
        logging.warning(f"Не удалось отредактировать сообщение с результатом обновления БЗ для админа {chat_id}.",
                        exc_info=e)


async def bypass_kb_protection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки 'Снять защиту и запустить повторно' из уведомления о срабатывании ЗАЩИТЫ БЗ."""
    query = update.callback_query
    user_id = query.from_user.id

    if user_id != SUPER_ADMIN_ID:
        await query.answer("Доступ запрещен.", show_alert=True)
        return

    # callback_data = "bypass_prot_{server_id}"
    server_id = query.data.split("_", 2)[2]
    server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    await query.answer(f"Защита снята. Запускаю обновление для {server_name}...")
    await query.edit_message_text(
        f"🔓 Защита снята для <b>{server_name}</b>.\n"
        f"⏳ Запускаю обновление повторно с обходом защиты...",
        parse_mode="HTML"
    )

    logging.info(f"Супер-админ {user_id} снял защиту БЗ для сервера {server_id}. Запуск повторного обновления.")
    await log_admin_action_to_sheet(user_id, "KB_BYPASS_PROTECTION", f"Server ID: {server_id}")
    asyncio.create_task(_bypass_protection_and_notify(context, user_id, server_id, server_name))


async def _bypass_protection_and_notify(context: ContextTypes.DEFAULT_TYPE, chat_id: int, server_id: str,
                                         server_name: str):
    """Фоновая задача: запускает обновление БЗ с обходом защиты и уведомляет супер-админа о результате."""
    success = await update_knowledge_base_for_server(server_id, force_update=False, bypass_protection=True)
    if success:
        msg = f"✅ Обновление <b>{server_name}</b> с обходом защиты успешно завершено!"
    else:
        msg = f"❌ Ошибка при обновлении <b>{server_name}</b> даже с обходом защиты. Проверьте логи."
    try:
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Не удалось отправить результат обхода защиты супер-админу {chat_id}", exc_info=e)


async def run_kb_update_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Запускаю процесс...")
    await query.edit_message_text(
        "⏳ Начинаю полное обновление баз знаний для **ВСЕХ** серверов... Это может занять **очень много времени**. По завершении я пришлю отчет.")
    logging.info(f"Администратор {query.from_user.id} запустил ручное обновление БЗ для ВСЕХ серверов.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_UPDATE_ALL_MANUAL")
    asyncio.create_task(update_all_and_notify_admin(context, query.message.chat_id, force=False))
    return MANAGE_SERVERS


async def run_kb_force_update_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик для кнопки принудительного обновления всех серверов."""
    query = update.callback_query
    await query.answer("Запускаю принудительный процесс...")
    await query.edit_message_text(
        "⏳ Начинаю **принудительное** полное обновление баз знаний для **ВСЕХ** серверов... Это может занять **очень много времени**. По завершении я пришлю отчет.")
    logging.info(f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ ручное обновление БЗ для ВСЕХ серверов.")
    await log_admin_action_to_sheet(query.from_user.id, "KB_FORCE_UPDATE_ALL_MANUAL")
    asyncio.create_task(update_all_and_notify_admin(context, query.message.chat_id, force=True))
    return MANAGE_SERVERS


async def update_all_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, force: bool = False):
    successful_updates, failed_servers = await update_all_servers_knowledge_base(force_update=force)
    total_servers = len(config.SERVERS_CONFIG)
    update_type = "Принудительное глобальное" if force else "Глобальное"
    final_text = (f"✅ **{update_type} обновление завершено!**\n\n"
                  f"- Успешно: {successful_updates}/{total_servers}\n")
    if failed_servers:
        final_text += f"- Ошибки на серверах: {', '.join(failed_servers)}\n"
    await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode="Markdown")
    if 'scheduler_callback' in context.application.bot_data:
        await context.application.bot_data['scheduler_callback']()


# ========== СИНХРОНИЗАЦИЯ ПРЕЦЕДЕНТОВ ==========

async def sync_precedents_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик для кнопки синхронизации прецедентов."""
    query = update.callback_query
    await query.answer("Запускаю синхронизацию прецедентов...")
    await query.edit_message_text(
        "⚖️ Начинаю синхронизацию судебных прецедентов...\n\n"
        "Будут обработаны только серверы с настроенной системой прецедентов.\n"
        "По завершении я пришлю отчет.")
    logging.info(f"Администратор {query.from_user.id} запустил синхронизацию прецедентов.")
    await log_admin_action_to_sheet(query.from_user.id, "PRECEDENTS_SYNC")
    asyncio.create_task(sync_precedents_and_notify_admin(context, query.message.chat_id))
    return MANAGE_SERVERS


async def sync_precedents_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Асинхронная задача для синхронизации прецедентов с уведомлением админа."""
    from knowledge_base_manager import sync_precedents_for_all_servers

    genai_client = context.bot_data.get('genai_client')
    successful, failed = await sync_precedents_for_all_servers(genai_client, force_update=False)

    final_text = f"⚖️ **Синхронизация прецедентов завершена!**\n\n"
    final_text += f"- Серверов обработано успешно: {successful}\n"
    if failed:
        final_text += f"- Ошибки на серверах: {', '.join(failed)}\n"
    else:
        final_text += "- Ошибок не обнаружено\n"

    await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode="Markdown")


async def choose_server_for_force_precedents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список серверов с прецедентами для принудительного обновления."""
    query = update.callback_query
    await query.answer()

    keyboard = []
    servers_with_precedents = []

    for server_id, server_config in config.SERVERS_CONFIG.items():
        if server_config.get('precedents_registry_url'):
            servers_with_precedents.append({
                'id': server_id,
                'name': server_config['display_name']
            })

    if not servers_with_precedents:
        await query.edit_message_text(
            "❌ Нет серверов с настроенной системой прецедентов.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")]])
        )
        return MANAGE_SERVERS

    for server in servers_with_precedents:
        btn_text = f"💥 {server['name']}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"force_prec_{server['id']}")])

    keyboard.append([InlineKeyboardButton("💥 ВСЕ серверы", callback_data="force_prec_all")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_servers")])

    await query.edit_message_text(
        "⚖️ **Принудительное обновление прецедентов**\n\n"
        "⚠️ Это обновит ВСЕ прецеденты, игнорируя проверку изменений изображений.\n"
        "Может занять много времени и API-запросов.\n\n"
        "Выберите сервер:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_CHOOSE_SERVER_FORCE_PRECEDENTS


async def run_force_precedents_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает принудительное обновление прецедентов для выбранного сервера."""
    query = update.callback_query

    # Определяем, один сервер или все
    callback_data = query.data
    if callback_data == "force_prec_all":
        await query.answer("Запускаю принудительное обновление для ВСЕХ серверов...")
        await query.edit_message_text(
            "⚖️💥 Начинаю **ПРИНУДИТЕЛЬНОЕ** обновление прецедентов для **ВСЕХ** серверов...\n\n"
            "⚠️ Это может занять очень много времени.\n"
            "По завершении я пришлю отчет."
        )
        logging.info(f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ обновление прецедентов для ВСЕХ серверов.")
        await log_admin_action_to_sheet(query.from_user.id, "PRECEDENTS_FORCE_SYNC_ALL")
        asyncio.create_task(force_sync_precedents_and_notify_admin(context, query.message.chat_id, server_id=None))
    else:
        server_id = callback_data.replace("force_prec_", "")
        server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

        await query.answer(f"Запускаю для {server_name}...")
        await query.edit_message_text(
            f"⚖️💥 Начинаю **ПРИНУДИТЕЛЬНОЕ** обновление прецедентов для сервера **{server_name}**...\n\n"
            f"⚠️ Это может занять несколько минут.\n"
            f"По завершении я пришлю отчет."
        )
        logging.info(f"Администратор {query.from_user.id} запустил ПРИНУДИТЕЛЬНОЕ обновление прецедентов для сервера {server_id}.")
        await log_admin_action_to_sheet(query.from_user.id, "PRECEDENTS_FORCE_SYNC_SINGLE", f"Server: {server_id}")
        asyncio.create_task(force_sync_precedents_and_notify_admin(context, query.message.chat_id, server_id=server_id))

    return MANAGE_SERVERS


async def force_sync_precedents_and_notify_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, server_id: str = None):
    """Асинхронная задача для ПРИНУДИТЕЛЬНОЙ синхронизации прецедентов."""
    from precedents_manager import sync_precedents_for_server

    genai_client = context.bot_data.get('genai_client')

    if server_id:
        # Обновляем один сервер
        try:
            updated_count = await sync_precedents_for_server(server_id, genai_client, force_update=True)
            server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)
            final_text = (
                f"⚖️💥 **Принудительное обновление прецедентов завершено!**\n\n"
                f"**Сервер:** {server_name}\n"
                f"**Обновлено прецедентов:** {updated_count}"
            )
        except Exception as e:
            logging.error(f"Ошибка принудительного обновления прецедентов для {server_id}", exc_info=e)
            final_text = f"❌ Ошибка при обновлении прецедентов для сервера {server_id}: {str(e)[:200]}"
    else:
        # Обновляем все серверы с прецедентами
        from knowledge_base_manager import sync_precedents_for_all_servers
        successful, failed = await sync_precedents_for_all_servers(genai_client, force_update=True)

        final_text = f"⚖️💥 **Принудительное обновление прецедентов завершено!**\n\n"
        final_text += f"- Серверов обработано успешно: {successful}\n"
        if failed:
            final_text += f"- Ошибки на серверах: {', '.join(failed)}\n"
        else:
            final_text += "- Ошибок не обнаружено\n"

    await context.bot.send_message(chat_id=chat_id, text=final_text, parse_mode="Markdown")


# ========== УПРАВЛЕНИЕ ПРЕЦЕДЕНТАМИ ==========

async def manage_precedents_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главное меню управления прецедентами."""
    query = update.callback_query
    await query.answer()

    # Получаем статистику по серверам с прецедентами
    servers_with_precedents = []
    for server_id, server_config in config.SERVERS_CONFIG.items():
        if server_config.get('precedents_registry_url'):
            count = await db_manager.fetchval(
                "SELECT COUNT(*) FROM precedents WHERE server_id = $1 AND is_active = TRUE",
                server_id
            ) or 0
            servers_with_precedents.append({
                'id': server_id,
                'name': server_config['display_name'],
                'count': count
            })

    if not servers_with_precedents:
        await query.edit_message_text(
            "⚖️ **Прецеденты**\n\n"
            "❌ Нет серверов с настроенной системой прецедентов.\n\n"
            "Для настройки добавьте `precedents_registry_url` в конфигурацию сервера.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]])
        )
        return CHOOSING_ACTION

    keyboard = []
    for server in servers_with_precedents:
        btn_text = f"📂 {server['name']} ({server['count']} прецедентов)"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"prec_server_{server['id']}")])

    keyboard.append([InlineKeyboardButton("🔄 Синхронизировать все", callback_data="sync_precedents")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])

    total_count = sum(s['count'] for s in servers_with_precedents)
    await query.edit_message_text(
        f"⚖️ **Просмотр прецедентов**\n\n"
        f"Всего активных прецедентов: {total_count}\n"
        f"Серверов с прецедентами: {len(servers_with_precedents)}\n\n"
        f"Выберите сервер для просмотра:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_MANAGE_PRECEDENTS


async def view_server_precedents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список прецедентов для выбранного сервера."""
    query = update.callback_query
    await query.answer()

    # Если callback_data начинается с prec_server_, то это новый выбор сервера
    # Иначе используем сохранённый server_id из context.user_data
    if query.data.startswith("prec_server_"):
        server_id = query.data.replace("prec_server_", "")
        context.user_data['precedents_server_id'] = server_id
        context.user_data['precedents_page'] = 0  # Сбрасываем страницу при выборе нового сервера
    else:
        server_id = context.user_data.get('precedents_server_id')
        if not server_id:
            # Если нет server_id, возвращаемся к списку серверов
            return await manage_precedents_menu(update, context)

    # Получаем страницу (по умолчанию 0)
    page = context.user_data.get('precedents_page', 0)
    items_per_page = 8

    # Получаем прецеденты
    precedents = await db_manager.fetch(
        """SELECT id, title, precedent_type, is_active, summary, last_updated
           FROM precedents 
           WHERE server_id = $1 
           ORDER BY is_active DESC, last_updated DESC
           LIMIT $2 OFFSET $3""",
        server_id, items_per_page + 1, page * items_per_page  # +1 для проверки наличия следующей страницы
    )

    total_count = await db_manager.fetchval(
        "SELECT COUNT(*) FROM precedents WHERE server_id = $1", server_id
    ) or 0

    server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    if not precedents:
        await query.edit_message_text(
            f"⚖️ **{server_name}**\n\n"
            f"Прецеденты не найдены.\n\n"
            f"Запустите синхронизацию для загрузки прецедентов.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Синхронизировать", callback_data="sync_precedents")],
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_precedents")]
            ])
        )
        return ADMIN_MANAGE_PRECEDENTS

    has_next_page = len(precedents) > items_per_page
    precedents = precedents[:items_per_page]  # Убираем лишний элемент

    keyboard = []
    for prec in precedents:
        # Иконка только для статуса
        status_icon = "" if prec['is_active'] else "❌ "

        # Обрезаем заголовок если слишком длинный
        title = prec['title'][:40] + "..." if len(prec['title']) > 40 else prec['title']

        btn_text = f"{status_icon}📜 {title}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"prec_view_{prec['id']}")])

    # Навигация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"prec_page_{page - 1}"))
    if has_next_page:
        nav_buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"prec_page_{page + 1}"))
    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton("◀️ К списку серверов", callback_data="manage_precedents")])

    active_count = await db_manager.fetchval(
        "SELECT COUNT(*) FROM precedents WHERE server_id = $1 AND is_active = TRUE", server_id
    ) or 0

    await query.edit_message_text(
        f"⚖️ **{server_name}**\n\n"
        f"Всего прецедентов: {total_count}\n"
        f"Активных: {active_count}\n"
        f"Страница: {page + 1}\n\n"
        f"❌ = Неактивен",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_VIEWING_PRECEDENTS_LIST


async def precedents_change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключение страницы списка прецедентов."""
    query = update.callback_query
    page = int(query.data.replace("prec_page_", ""))
    context.user_data['precedents_page'] = page
    return await view_server_precedents(update, context)


async def view_precedent_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детальную информацию о прецеденте."""
    query = update.callback_query
    await query.answer()

    precedent_id = int(query.data.replace("prec_view_", ""))
    context.user_data['current_precedent_id'] = precedent_id

    prec = await db_manager.fetchrow(
        """SELECT id, server_id, source_url, title, summary, precedent_type, 
                  is_active, last_updated, full_content, image_urls
           FROM precedents WHERE id = $1""",
        precedent_id
    )

    if not prec:
        await query.edit_message_text(
            "❌ Прецедент не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="manage_precedents")]])
        )
        return ADMIN_MANAGE_PRECEDENTS

    server_name = config.SERVERS_CONFIG.get(prec['server_id'], {}).get('display_name', prec['server_id'])
    status = "✅ Активен" if prec['is_active'] else "❌ Неактивен"

    # Форматируем дату
    last_updated = prec['last_updated'].strftime("%d.%m.%Y %H:%M") if prec['last_updated'] else "Неизвестно"

    # Количество изображений
    image_urls = prec['image_urls'] or []
    images_info = f"🖼 Изображений: {len(image_urls)}" if image_urls else "🖼 Изображений: нет"

    # Краткое описание (summary)
    summary = prec['summary'] or "Краткое описание не сгенерировано"
    if len(summary) > 500:
        summary = summary[:500] + "..."

    text = (
        f"⚖️ **{prec['title']}**\n\n"
        f"**Сервер:** {server_name}\n"
        f"**Статус:** {status}\n"
        f"**{images_info}**\n"
        f"**Обновлено:** {last_updated}\n\n"
        f"**📝 Краткое описание:**\n{summary}\n\n"
        f"🔗 [Открыть на форуме]({prec['source_url']})"
    )

    # Проверяем, есть ли полный контент
    has_full_content = bool(prec['full_content'] and len(prec['full_content']) > 0)
    content_length = len(prec['full_content']) if prec['full_content'] else 0

    keyboard = []
    if has_full_content:
        keyboard.append([InlineKeyboardButton(f"📄 Полный текст ({content_length} симв.)", callback_data=f"prec_full_{precedent_id}")])

    # Кнопка для просмотра изображений
    if image_urls:
        keyboard.append([InlineKeyboardButton(f"🖼 Показать изображения ({len(image_urls)})", callback_data=f"prec_images_{precedent_id}")])

    keyboard.append([InlineKeyboardButton("◀️ К списку", callback_data=f"prec_server_{prec['server_id']}")])
    keyboard.append([InlineKeyboardButton("◀️ К серверам", callback_data="manage_precedents")])

    await query.edit_message_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )
    return ADMIN_VIEWING_PRECEDENT_DETAIL


async def view_precedent_full_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает полный текст прецедента (может быть разбит на несколько сообщений)."""
    query = update.callback_query
    await query.answer("Загружаю полный текст...")

    precedent_id = int(query.data.replace("prec_full_", ""))

    prec = await db_manager.fetchrow(
        "SELECT id, server_id, title, full_content FROM precedents WHERE id = $1",
        precedent_id
    )

    if not prec or not prec['full_content']:
        await query.edit_message_text(
            "❌ Полный текст не найден.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"prec_view_{precedent_id}")]])
        )
        return ADMIN_VIEWING_PRECEDENT_DETAIL

    full_content = prec['full_content']
    title = prec['title']

    # Разбиваем на части по 4000 символов (лимит Telegram ~4096)
    max_length = 4000
    parts = []
    while full_content:
        if len(full_content) <= max_length:
            parts.append(full_content)
            break
        # Ищем последний перенос строки в пределах лимита
        split_pos = full_content.rfind('\n', 0, max_length)
        if split_pos == -1:
            split_pos = max_length
        parts.append(full_content[:split_pos])
        full_content = full_content[split_pos:].lstrip()

    # Отправляем первую часть как редактирование текущего сообщения
    header = f"📄 **{title}**\n\n"
    first_part = header + f"```\n{parts[0]}\n```"

    # Если текст слишком длинный даже для первой части
    if len(first_part) > 4096:
        first_part = header + f"```\n{parts[0][:3800]}\n```\n\n_(продолжение в следующем сообщении)_"

    keyboard = [[InlineKeyboardButton("◀️ Назад к прецеденту", callback_data=f"prec_view_{precedent_id}")]]

    try:
        await query.edit_message_text(
            first_part,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except BadRequest:
        # Если не удалось с Markdown, пробуем без форматирования
        await query.edit_message_text(
            f"📄 {title}\n\n{parts[0][:3900]}",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    # Отправляем остальные части как новые сообщения
    chat_id = update.effective_chat.id
    for i, part in enumerate(parts[1:], start=2):
        try:
            await asyncio.sleep(0.5)  # Небольшая задержка между сообщениями
            part_text = f"_(Часть {i}/{len(parts)})_\n\n```\n{part}\n```"
            if len(part_text) > 4096:
                part_text = f"_(Часть {i}/{len(parts)})_\n\n{part[:3900]}"
            await context.bot.send_message(chat_id, part_text, parse_mode="Markdown")
        except Exception as e:
            logging.warning(f"Ошибка отправки части {i} прецедента: {e}")
            await context.bot.send_message(chat_id, f"_(Часть {i}/{len(parts)})_\n\n{part[:3900]}")

    return ADMIN_VIEWING_PRECEDENT_DETAIL


async def view_precedent_images(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список URL изображений прецедента."""
    query = update.callback_query
    await query.answer()

    precedent_id = int(query.data.replace("prec_images_", ""))

    prec = await db_manager.fetchrow(
        "SELECT id, server_id, title, image_urls FROM precedents WHERE id = $1",
        precedent_id
    )

    if not prec or not prec['image_urls']:
        await query.edit_message_text(
            "❌ Изображения не найдены.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=f"prec_view_{precedent_id}")]])
        )
        return ADMIN_VIEWING_PRECEDENT_DETAIL

    image_urls = prec['image_urls']
    title = prec['title']

    # Формируем текст со списком URL
    text = f"🖼 **Изображения прецедента**\n\n"
    text += f"📌 {title}\n\n"
    text += f"Всего изображений: {len(image_urls)}\n\n"

    for i, url in enumerate(image_urls, 1):
        # Сокращаем URL если слишком длинный
        short_url = url[:60] + "..." if len(url) > 60 else url
        text += f"{i}. `{short_url}`\n"

    keyboard = [[InlineKeyboardButton("◀️ Назад к прецеденту", callback_data=f"prec_view_{precedent_id}")]]

    try:
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True
        )
    except BadRequest:
        # Если слишком длинный текст
        await query.edit_message_text(
            f"🖼 Изображения прецедента: {title}\n\nВсего: {len(image_urls)}\n\n(Список слишком длинный для отображения)",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return ADMIN_VIEWING_PRECEDENT_DETAIL


# ========== УПРАВЛЕНИЕ КЭШЕМ ==========

async def manage_limits_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("💥 Сбросить всем пользователям", callback_data="reset_limits_all_confirm")],
        [InlineKeyboardButton("👤 Сбросить конкретному пользователю", callback_data="reset_limits_user")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")]
    ]
    await query.answer()
    await query.edit_message_text(
        text="⚙️ Управление лимитами использования.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MANAGE_LIMITS


async def pre_reset_user_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await request_user_id(
        update.callback_query, context, "сброса лимитов", GET_ID_FOR_LIMIT_RESET
    )


async def reset_user_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        target_id = int(update.message.text)
    except (ValueError, TypeError):
        await update.message.reply_text("ID должен быть числом.")
        return GET_ID_FOR_LIMIT_RESET
    target_user_exists = await db_manager.fetchval("SELECT 1 FROM users WHERE user_id = $1", target_id)
    if not target_user_exists:
        await update.message.reply_text(f"Пользователь с ID {target_id} не найден.")
        return await admin_panel(update, context)
    # Сбрасываем дневные лимиты
    query = "DELETE FROM user_usage WHERE user_id = $1 AND usage_date = CURRENT_DATE;"
    await db_manager.execute(query, target_id)
    # Сбрасываем лимиты ролевого помощника
    query_assistant = "DELETE FROM assistant_usage_log WHERE user_id = $1;"
    await db_manager.execute(query_assistant, target_id)
    await update.message.reply_text(f"✅ Все лимиты для пользователя {target_id} сброшены (включая ролевого помощника).")
    admin_id = update.effective_user.id
    logging.info(f"Администратор {admin_id} сбросил все лимиты для {target_id}.")
    await log_admin_action_to_sheet(admin_id, "LIMITS_RESET_SINGLE", f"Target ID: {target_id}")
    return await admin_panel(update, context)


async def confirm_reset_all_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    keyboard = [
        [InlineKeyboardButton("ДА, сбросить всем", callback_data="reset_limits_all_execute")],
        [InlineKeyboardButton("НЕТ, отмена", callback_data="manage_limits")]
    ]
    await query.answer()
    await query.edit_message_text(
        text="⚠️ **ВЫ УВЕРЕНЫ?**\n\nЭто действие сбросит дневные лимиты для **ВСЕХ** пользователей.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CONFIRM_RESET_ALL_LIMITS


async def execute_reset_all_limits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Выполняю сброс...")
    # Сбрасываем дневные лимиты
    db_query = "DELETE FROM user_usage WHERE usage_date = CURRENT_DATE;"
    result = await db_manager.execute(db_query)
    users_affected = int(result.split()[-1]) if result and result.split() else 0
    # Сбрасываем лимиты ролевого помощника для ВСЕХ пользователей
    db_query_assistant = "DELETE FROM assistant_usage_log;"
    result_assistant = await db_manager.execute(db_query_assistant)
    assistant_affected = int(result_assistant.split()[-1]) if result_assistant and result_assistant.split() else 0
    await query.edit_message_text(
        f"✅ Глобальный сброс лимитов завершен.\n"
        f"• Дневные лимиты: {users_affected} пользователей\n"
        f"• Ролевой помощник: {assistant_affected} записей"
    )
    admin_id = update.effective_user.id
    logging.info(f"Администратор {admin_id} сбросил все лимиты для ВСЕХ (daily: {users_affected}, assistant: {assistant_affected}).")
    await log_admin_action_to_sheet(admin_id, "LIMITS_RESET_ALL", f"Daily: {users_affected}, Assistant: {assistant_affected}")
    await admin_panel(update, context)
    return ConversationHandler.END


async def manage_reports_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'") or 0
    closed_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'closed'") or 0
    keyboard = [
        [InlineKeyboardButton(f"🔵 Активные ({open_reports_count})", callback_data="view_reports_open")],
        [InlineKeyboardButton(f"⚪️ Архив ({closed_reports_count})", callback_data="view_reports_closed")],
    ]

    # Добавляем кнопку массового закрытия, если есть активные репорты
    if open_reports_count > 0:
        keyboard.append([InlineKeyboardButton(f"🔴 Закрыть все активные ({open_reports_count})", callback_data="close_all_reports_confirm")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])

    await query.edit_message_text("📬 Управление репортами:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_MANAGE_REPORTS


async def _display_reports_list(query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, status: str, page: int = 0):
    """Отображает список репортов с пагинацией."""
    REPORTS_PER_PAGE = 10  # Максимум репортов на одной странице
    
    reports = await db_manager.fetch(
        "SELECT r.id, r.title, r.last_message_at, u.full_name FROM reports r JOIN users u ON r.user_id = u.user_id WHERE r.status = $1 ORDER BY r.last_message_at DESC;",
        status
    )
    title_text = "🔵 Активные репорты" if status == 'open' else "⚪️ Архив репортов"
    
    if not reports:
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="manage_reports")]]
        await query.edit_message_text(f"{title_text}\n\nЗдесь пока пусто.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    # Вычисляем пагинацию
    total_reports = len(reports)
    total_pages = (total_reports + REPORTS_PER_PAGE - 1) // REPORTS_PER_PAGE
    page = max(0, min(page, total_pages - 1))  # Ограничиваем страницу
    
    start_idx = page * REPORTS_PER_PAGE
    end_idx = min(start_idx + REPORTS_PER_PAGE, total_reports)
    page_reports = reports[start_idx:end_idx]
    
    keyboard = []
    for report in page_reports:
        button_text = f"#{report['id']} от {report['full_name']} - {report['title'][:30]}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"admin_open_report_{report['id']}")])
    
    # Добавляем навигацию по страницам
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Пред.", callback_data=f"reports_page_{status}_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("След. ➡️", callback_data=f"reports_page_{status}_{page+1}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="manage_reports")])
    
    # Добавляем информацию о странице
    page_info = f"\n\nСтраница {page + 1}/{total_pages} (всего: {total_reports})"
    await query.edit_message_text(f"{title_text}:{page_info}", reply_markup=InlineKeyboardMarkup(keyboard))


async def view_reports_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    status = query.data.split('_')[-1]
    context.user_data['report_list_status'] = status
    await _display_reports_list(query, context, status, page=0)
    return ADMIN_VIEWING_REPORTS_LIST


async def navigate_reports_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик навигации по страницам списка репортов."""
    query = update.callback_query
    await query.answer()
    
    # Парсим callback_data: reports_page_{status}_{page}
    parts = query.data.split('_')
    status = parts[2]  # 'open' или 'closed'
    page = int(parts[3])
    
    await _display_reports_list(query, context, status, page=page)
    return ADMIN_VIEWING_REPORTS_LIST


async def _build_report_view_for_admin(report_id: int, context: ContextTypes.DEFAULT_TYPE):
    report_info = await db_manager.fetchrow(
        "SELECT r.id, r.status, r.user_id, u.full_name, u.username FROM reports r JOIN users u ON r.user_id = u.user_id WHERE r.id = $1",
        report_id
    )
    if not report_info: return None, None
    context.user_data.update({'report_user_id': report_info['user_id'], 'current_report_id': report_id})
    messages = await db_manager.fetch(
        "SELECT sender_role, content, sender_id FROM report_messages WHERE report_id = $1 ORDER BY timestamp ASC;",
        report_id
    )
    import html as _html
    full_name_safe = _html.escape(str(report_info['full_name'] or ''))
    username_safe = _html.escape(str(report_info['username'] or 'N/A'))
    header = (f"Диалог по репорту #{report_id}\nСтатус: {report_info['status'].upper()}\n"
              f"Пользователь: {full_name_safe} (@{username_safe}, <code>{report_info['user_id']}</code>)\n" + "=" * 20)

    # --- ИСПРАВЛЕННАЯ ЛОГИКА ---
    conversation_parts = []
    for msg in messages:
        if msg['sender_role'] == 'user':
            sender = "Пользователь"
        else:
            sender = f"Админ ({msg['sender_id']})"
        content_safe = _html.escape(str(msg['content'] or ''))
        conversation_parts.append(f"<b>{sender}:</b>\n{content_safe}")
    conversation_text = "\n\n".join(conversation_parts)
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    full_text = f"{header}\n\n{conversation_text}"
    keyboard = []
    if report_info['status'] == 'open':
        keyboard.append([InlineKeyboardButton("✍️ Ответить", callback_data=f"admin_reply_report_{report_id}"),
                         InlineKeyboardButton("✅ Закрыть", callback_data=f"admin_close_report_{report_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад к списку",
                                          callback_data=f"view_reports_{context.user_data.get('report_list_status', 'open')}")])
    return full_text, InlineKeyboardMarkup(keyboard)


async def view_report_conversation_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    report_id = int(query.data.split('_')[-1])
    text, reply_markup = await _build_report_view_for_admin(report_id, context)
    if not text:
        await query.edit_message_text("Ошибка: репорт не найден.")
        return ADMIN_VIEWING_REPORTS_LIST
    await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
    return ADMIN_CONVERSATION_IN_REPORT


async def prompt_admin_for_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("◀️ Отмена",
                                      callback_data=f"admin_open_report_{context.user_data['current_report_id']}")]]
    await query.edit_message_text("Введите ваш ответ пользователю.", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_ADMIN_REPLY_IN_REPORT


async def handle_admin_reply_in_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin_id = update.effective_user.id
    message_text = update.message.text
    report_id = context.user_data['current_report_id']
    user_id = context.user_data['report_user_id']
    await db_manager.execute(
        "INSERT INTO report_messages (report_id, sender_id, sender_role, content) VALUES ($1, $2, 'admin', $3);",
        report_id, admin_id, message_text)
    await db_manager.execute("UPDATE reports SET last_message_at = NOW() WHERE id = $1;", report_id)
    logging.info(f"Администратор {admin_id} ответил в репорт #{report_id}.")
    try:
        await context.bot.send_message(chat_id=user_id,
                                       text=f"‼️ **Получен ответ от администрации по вашему репорту #{report_id}**",
                                       reply_markup=InlineKeyboardMarkup(
                                           [[InlineKeyboardButton("📂 Просмотреть", callback_data="start_report_dialog")]]),
                                       parse_mode='Markdown')
    except Exception as e:
        await context.bot.send_message(chat_id=admin_id, text=f"⚠️ Не удалось уведомить пользователя: {e}")
    await update.message.delete()
    text, reply_markup = await _build_report_view_for_admin(report_id, context)
    if text:
        await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup, parse_mode='HTML')
    return ADMIN_CONVERSATION_IN_REPORT


async def close_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Репорт закрыт.", show_alert=True)
    admin_id = query.from_user.id
    report_id = context.user_data['current_report_id']
    await db_manager.execute("UPDATE reports SET status = 'closed' WHERE id = $1;", report_id)
    logging.info(f"Администратор {admin_id} закрыл репорт #{report_id}.")
    await log_admin_action_to_sheet(admin_id, "REPORT_CLOSE", f"Report ID: {report_id}")
    try:
        await context.bot.send_message(chat_id=context.user_data['report_user_id'],
                                       text=f"✅ Ваш репорт #{report_id} был закрыт администрацией.")
    except Exception as e:
        logging.warning(f"Не удалось уведомить пользователя о закрытии репорта: {e}")
    await _display_reports_list(query, context, context.user_data.get('report_list_status', 'open'))
    return ADMIN_VIEWING_REPORTS_LIST


async def confirm_close_all_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает подтверждение на массовое закрытие всех активных репортов."""
    query = update.callback_query
    await query.answer()

    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'") or 0

    if open_reports_count == 0:
        await query.edit_message_text("Нет активных репортов для закрытия.")
        return await manage_reports_menu(update, context)

    keyboard = [
        [InlineKeyboardButton("✅ Да, закрыть все", callback_data="close_all_reports_execute")],
        [InlineKeyboardButton("❌ Отмена", callback_data="manage_reports")]
    ]

    await query.edit_message_text(
        f"⚠️ Вы уверены, что хотите закрыть все {open_reports_count} активных репортов?\n\n"
        f"Пользователи НЕ получат уведомления о закрытии.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_CONFIRM_CLOSE_ALL_REPORTS


async def execute_close_all_reports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет массовое закрытие всех активных репортов без уведомления пользователей."""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    # Получаем количество репортов для закрытия
    open_reports_count = await db_manager.fetchval("SELECT COUNT(*) FROM reports WHERE status = 'open'") or 0

    if open_reports_count == 0:
        await query.edit_message_text("Нет активных репортов для закрытия.")
        return await manage_reports_menu(update, context)

    # Закрываем все активные репорты
    await db_manager.execute("UPDATE reports SET status = 'closed' WHERE status = 'open';")

    logging.info(f"Администратор {admin_id} закрыл все активные репорты (количество: {open_reports_count}).")
    await log_admin_action_to_sheet(admin_id, "REPORTS_CLOSE_ALL", f"Closed: {open_reports_count} reports")

    await query.edit_message_text(
        f"✅ Успешно закрыто репортов: {open_reports_count}\n\n"
        f"Пользователи не были уведомлены."
    )

    # Возвращаемся в меню управления репортами
    await asyncio.sleep(2)
    return await manage_reports_menu(update, context)


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 1: Выбор аудитории для рассылки."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('broadcast', None)  # Очищаем предыдущие данные рассылки

    # Считаем количество пользователей по категориям
    total = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE")
    subscribers = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND is_subscriber = TRUE")
    non_subs = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND (is_subscriber = FALSE OR is_subscriber IS NULL)")
    vips = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND is_vip = TRUE")
    testers = await db_manager.fetchval("SELECT COUNT(*) FROM users WHERE is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE AND is_tester = TRUE")

    keyboard = [
        [InlineKeyboardButton(f"👥 Все пользователи ({total})", callback_data="bc_audience_all")],
        [InlineKeyboardButton(f"⭐ Подписчики ({subscribers})", callback_data="bc_audience_subscribers")],
        [InlineKeyboardButton(f"📭 Без подписки ({non_subs})", callback_data="bc_audience_non_subscribers")],
        [InlineKeyboardButton(f"👑 VIP ({vips})", callback_data="bc_audience_vip")],
        [InlineKeyboardButton(f"🧪 Тестеры ({testers})", callback_data="bc_audience_testers")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")]
    ]

    await query.edit_message_text(
        "📢 <b>Рассылка — Шаг 1/4: Выбор аудитории</b>\n\n"
        "Выберите, кому отправить рассылку.\n"
        "<i>Заблокировавшие бота и забаненные автоматически исключены.</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return BROADCAST_CHOOSE_AUDIENCE


async def choose_broadcast_audience(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 2: Сохраняем аудиторию, просим отправить сообщение."""
    query = update.callback_query
    await query.answer()

    audience = query.data.replace("bc_audience_", "")
    context.user_data['broadcast'] = {'audience': audience}

    # Считаем точное количество
    base_filter = "is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE"
    if audience == "subscribers":
        base_filter += " AND is_subscriber = TRUE"
    elif audience == "non_subscribers":
        base_filter += " AND (is_subscriber = FALSE OR is_subscriber IS NULL)"
    elif audience == "vip":
        base_filter += " AND is_vip = TRUE"
    elif audience == "testers":
        base_filter += " AND is_tester = TRUE"

    count = await db_manager.fetchval(f"SELECT COUNT(*) FROM users WHERE {base_filter}")
    context.user_data['broadcast']['recipient_count'] = count
    context.user_data['broadcast']['db_filter'] = base_filter

    audience_names = {
        "all": "👥 Все пользователи",
        "subscribers": "⭐ Подписчики",
        "non_subscribers": "📭 Без подписки",
        "vip": "👑 VIP",
        "testers": "🧪 Тестеры"
    }

    await query.edit_message_text(
        f"📢 <b>Рассылка — Шаг 2/4: Сообщение</b>\n\n"
        f"Аудитория: <b>{audience_names.get(audience, audience)}</b> ({count} чел.)\n\n"
        f"Отправьте мне сообщение для рассылки.\n"
        f"Поддерживается <b>любой тип</b>:\n"
        f"• 📝 Текст (с HTML-разметкой)\n"
        f"• 🖼 Фото с подписью\n"
        f"• 🎥 Видео с подписью\n"
        f"• 📄 Документ\n"
        f"• 🎞 GIF-анимация\n"
        f"• 🎤 Голосовое сообщение\n"
        f"• 🎵 Аудио\n\n"
        f"<i>Сообщение будет переслано получателям в точном виде.</i>",
        parse_mode="HTML"
    )
    return BROADCAST_AWAITING_MESSAGE


async def receive_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 3: Получаем сообщение, показываем превью и опции."""
    message = update.message
    bc = context.user_data.get('broadcast', {})

    # Сохраняем координаты сообщения для copy_message
    bc['source_chat_id'] = message.chat_id
    bc['source_message_id'] = message.message_id

    # Определяем тип сообщения для отображения
    if message.photo:
        msg_type = "🖼 Фото"
    elif message.video:
        msg_type = "🎥 Видео"
    elif message.document:
        msg_type = "📄 Документ"
    elif message.animation:
        msg_type = "🎞 GIF"
    elif message.voice:
        msg_type = "🎤 Голосовое"
    elif message.audio:
        msg_type = "🎵 Аудио"
    elif message.sticker:
        msg_type = "😀 Стикер"
    else:
        msg_type = "📝 Текст"
    bc['msg_type'] = msg_type

    # Отправляем превью (копируем сообщение обратно админу)
    try:
        await message.reply_text("👁 <b>Превью рассылки:</b>", parse_mode="HTML")
        preview = await context.bot.copy_message(
            chat_id=message.chat_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id
        )
        bc['preview_message_id'] = preview.message_id
    except Exception as e:
        logging.error(f"[BROADCAST] Ошибка превью: {e}")
        await message.reply_text("❌ Не удалось создать превью. Попробуйте другое сообщение.")
        return BROADCAST_AWAITING_MESSAGE

    bc.setdefault('inline_keyboard', None)
    bc.setdefault('pin_message', False)
    context.user_data['broadcast'] = bc

    return await _show_broadcast_options(update, context)


async def _show_broadcast_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает меню опций рассылки."""
    bc = context.user_data.get('broadcast', {})

    pin_emoji = "✅" if bc.get('pin_message') else "⬜"
    buttons_status = "✅ Добавлены" if bc.get('inline_keyboard') else "⬜ Нет"

    audience_names = {
        "all": "👥 Все", "subscribers": "⭐ Подписчики",
        "non_subscribers": "📭 Без подписки", "vip": "👑 VIP"
    }

    text = (
        f"📢 <b>Рассылка — Шаг 3/4: Настройки</b>\n\n"
        f"📨 Тип: <b>{bc.get('msg_type', 'Текст')}</b>\n"
        f"👥 Аудитория: <b>{audience_names.get(bc.get('audience', 'all'), bc.get('audience'))}</b>\n"
        f"📊 Получателей: <b>{bc.get('recipient_count', 0)}</b>\n"
        f"🔘 Инлайн-кнопки: {buttons_status}\n"
        f"📌 Закрепить: {pin_emoji}\n"
    )

    keyboard = [
        [InlineKeyboardButton(f"🔘 {'Изменить' if bc.get('inline_keyboard') else 'Добавить'} кнопки", callback_data="bc_add_buttons")],
        [InlineKeyboardButton(f"📌 {'Убрать закрепление' if bc.get('pin_message') else 'Закрепить сообщение'}", callback_data="bc_toggle_pin")],
        [InlineKeyboardButton("✅ Запустить рассылку", callback_data="bc_confirm_send")],
        [InlineKeyboardButton("🔄 Изменить сообщение", callback_data="bc_change_message")],
        [InlineKeyboardButton("❌ Отмена", callback_data="bc_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Если вызвано из callback (toggle_pin, add_buttons) — редактируем
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="HTML")
            return BROADCAST_CONFIRM
        except BadRequest:
            pass  # "Message is not modified" или другое — отправляем новое

    # Если вызвано после получения media-сообщения — отправляем новое
    chat_id = update.effective_chat.id
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")
    return BROADCAST_CONFIRM


async def handle_broadcast_buttons_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Предлагает ввести инлайн-кнопки."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🔘 <b>Добавление инлайн-кнопок</b>\n\n"
        "Отправьте кнопки в формате (одна кнопка на строку):\n\n"
        "<code>Текст кнопки 1 - https://example.com\n"
        "Текст кнопки 2 - https://t.me/channel</code>\n\n"
        "Для двух кнопок в одном ряду разделяйте символом <code>|</code>:\n\n"
        "<code>Кнопка 1 - https://url1 | Кнопка 2 - https://url2</code>\n\n"
        "Отправьте <code>удалить</code> чтобы убрать кнопки.",
        parse_mode="HTML"
    )
    return BROADCAST_ATTACH_BUTTONS


async def receive_broadcast_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Парсит и сохраняет инлайн-кнопки."""
    text = update.message.text.strip()
    bc = context.user_data.get('broadcast', {})

    if text.lower() in ('удалить', 'убрать', 'нет', 'delete', 'remove'):
        bc['inline_keyboard'] = None
        context.user_data['broadcast'] = bc
        await update.message.reply_text("✅ Кнопки удалены.")
        return await _show_broadcast_options(update, context)

    try:
        keyboard = []
        for line in text.strip().split('\n'):
            row = []
            parts = line.split('|')
            for part in parts:
                part = part.strip()
                if ' - ' not in part:
                    raise ValueError(f"Неверный формат: '{part}'. Используйте 'Текст - URL'")
                btn_text, url = part.rsplit(' - ', 1)
                btn_text = btn_text.strip()
                url = url.strip()
                if not url.startswith(('http://', 'https://', 'tg://')):
                    raise ValueError(f"Неверный URL: '{url}'")
                row.append({"text": btn_text, "url": url})
            keyboard.append(row)

        # Проверяем, что кнопки валидны, пробуя собрать InlineKeyboardMarkup
        test_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=btn['text'], url=btn['url']) for btn in row]
            for row in keyboard
        ])

        bc['inline_keyboard'] = keyboard
        context.user_data['broadcast'] = bc

        btn_count = sum(len(row) for row in keyboard)
        await update.message.reply_text(f"✅ Добавлено кнопок: {btn_count}")

    except ValueError as e:
        await update.message.reply_text(f"❌ Ошибка формата:\n{e}\n\nПопробуйте ещё раз.")
        return BROADCAST_ATTACH_BUTTONS
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}\n\nПопробуйте ещё раз.")
        return BROADCAST_ATTACH_BUTTONS

    return await _show_broadcast_options(update, context)


async def toggle_broadcast_pin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переключает опцию закрепления."""
    query = update.callback_query
    await query.answer()

    bc = context.user_data.get('broadcast', {})
    bc['pin_message'] = not bc.get('pin_message', False)
    context.user_data['broadcast'] = bc

    return await _show_broadcast_options(update, context)


async def change_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает к шагу выбора сообщения."""
    query = update.callback_query
    await query.answer()

    await query.edit_message_text(
        "🔄 Отправьте новое сообщение для рассылки (любой тип).",
        parse_mode="HTML"
    )
    return BROADCAST_AWAITING_MESSAGE


async def confirm_and_start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Шаг 4: Подтверждаем и запускаем рассылку."""
    query = update.callback_query
    await query.answer()

    bc = context.user_data.get('broadcast', {})
    admin_id = query.from_user.id

    # Получаем список получателей
    db_filter = bc.get('db_filter', "is_banned = FALSE AND COALESCE(is_blocked, FALSE) = FALSE")
    user_ids = [r['user_id'] for r in await db_manager.fetch(f"SELECT user_id FROM users WHERE {db_filter}")]

    if not user_ids:
        await query.edit_message_text("❌ Нет подходящих получателей.")
        return await admin_panel(update, context)

    # Создаём запись в БД
    broadcast_id = await db_manager.fetchval(
        """INSERT INTO broadcasts 
           (admin_user_id, source_chat_id, source_message_id, audience_filter, 
            inline_keyboard_json, pin_message, total_recipients, status, started_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 'running', NOW())
           RETURNING id""",
        admin_id,
        bc['source_chat_id'],
        bc['source_message_id'],
        bc.get('audience', 'all'),
        json.dumps(bc['inline_keyboard']) if bc.get('inline_keyboard') else None,
        bc.get('pin_message', False),
        len(user_ids)
    )

    # Формируем сообщение прогресса
    progress_msg = await query.edit_message_text(
        f"🚀 <b>Рассылка #{broadcast_id} запущена!</b>\n\n"
        f"📊 Прогресс: 0/{len(user_ids)} (0%)\n"
        f"<code>░░░░░░░░░░░░░░░░░░░░</code>\n\n"
        f"✅ Доставлено: 0\n"
        f"❌ Ошибок: 0\n"
        f"🚫 Заблокировали: 0",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⛔ Остановить рассылку", callback_data=f"bc_cancel_running_{broadcast_id}")]
        ])
    )

    # Запускаем фоновую задачу
    context.job_queue.run_once(
        _broadcast_job,
        when=0.5,
        data={
            'broadcast_id': broadcast_id,
            'user_ids': user_ids,
            'source_chat_id': bc['source_chat_id'],
            'source_message_id': bc['source_message_id'],
            'inline_keyboard': bc.get('inline_keyboard'),
            'pin_message': bc.get('pin_message', False),
            'progress_message_id': progress_msg.message_id,
            'admin_chat_id': query.message.chat_id,
        },
        chat_id=query.message.chat_id,
        name=f"broadcast_{broadcast_id}"
    )

    await log_admin_action_to_sheet(admin_id, "BROADCAST_START",
                                    f"ID: {broadcast_id}, Audience: {bc.get('audience')}, Recipients: {len(user_ids)}")

    context.user_data.pop('broadcast', None)
    return ConversationHandler.END


async def _broadcast_job(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача рассылки с rate limiting и прогрессом в реальном времени."""
    job = context.job
    data = job.data
    bot = context.bot

    broadcast_id = data['broadcast_id']
    user_ids = data['user_ids']
    source_chat_id = data['source_chat_id']
    source_message_id = data['source_message_id']
    inline_keyboard = data.get('inline_keyboard')
    pin_message = data.get('pin_message', False)
    progress_message_id = data['progress_message_id']
    admin_chat_id = data['admin_chat_id']

    # Подготавливаем reply_markup если есть кнопки
    reply_markup = None
    if inline_keyboard:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(text=btn['text'], url=btn['url']) for btn in row]
            for row in inline_keyboard
        ])

    delivered = 0
    failed = 0
    blocked = 0
    total = len(user_ids)
    last_progress_update = 0
    cancel_key = f'cancel_broadcast_{broadcast_id}'

    for i, user_id in enumerate(user_ids):
        # Проверяем отмену
        if context.bot_data.get(cancel_key, False):
            logging.info(f"[BROADCAST #{broadcast_id}] Отменена администратором на {i}/{total}")
            break

        try:
            sent_msg = await bot.copy_message(
                chat_id=user_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=reply_markup
            )
            delivered += 1

            # Закрепляем если нужно
            if pin_message:
                try:
                    await bot.pin_chat_message(
                        chat_id=user_id,
                        message_id=sent_msg.message_id,
                        disable_notification=True
                    )
                except Exception:
                    pass  # Закрепление может не работать в личных чатах старых версий

        except RetryAfter as e:
            # Telegram просит подождать
            wait_time = e.retry_after + 1
            logging.warning(f"[BROADCAST #{broadcast_id}] RetryAfter: ждём {wait_time}с")
            await asyncio.sleep(wait_time)
            # Повторяем попытку
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                    reply_markup=reply_markup
                )
                delivered += 1
            except Forbidden:
                blocked += 1
                await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", user_id)
            except Exception:
                failed += 1

        except Forbidden:
            blocked += 1
            # Помечаем пользователя как заблокировавшего бота
            await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", user_id)

        except BadRequest as e:
            failed += 1
            logging.warning(f"[BROADCAST #{broadcast_id}] BadRequest для {user_id}: {e}")

        except Exception as e:
            failed += 1
            logging.warning(f"[BROADCAST #{broadcast_id}] Ошибка для {user_id}: {e}")

        # Rate limiting: ~25 msg/sec
        if (i + 1) % 25 == 0:
            await asyncio.sleep(1.1)
        else:
            await asyncio.sleep(0.04)

        # Обновляем прогресс каждые 3 секунды
        now = time.time()
        if now - last_progress_update >= 3:
            last_progress_update = now
            await _update_broadcast_progress(
                bot, admin_chat_id, progress_message_id, broadcast_id,
                i + 1, total, delivered, failed, blocked
            )

    # Определяем финальный статус
    was_cancelled = context.bot_data.pop(cancel_key, False)
    final_status = 'cancelled' if was_cancelled else 'completed'

    # Обновляем БД
    await db_manager.execute(
        """UPDATE broadcasts 
           SET delivered = $1, failed = $2, blocked_users = $3, status = $4, completed_at = NOW()
           WHERE id = $5""",
        delivered, failed, blocked, final_status, broadcast_id
    )

    # Финальный отчёт
    status_emoji = "⛔" if was_cancelled else "✅"
    status_text = "ОТМЕНЕНА" if was_cancelled else "ЗАВЕРШЕНА"

    progress_bar = _make_progress_bar(delivered + failed + blocked, total)
    report = (
        f"{status_emoji} <b>Рассылка #{broadcast_id} {status_text}</b>\n\n"
        f"📊 Прогресс: {delivered + failed + blocked}/{total}\n"
        f"{progress_bar}\n\n"
        f"✅ Доставлено: <b>{delivered}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>\n\n"
        f"⏱ Завершено: {datetime.now().strftime('%H:%M:%S')}"
    )

    try:
        await bot.edit_message_text(
            chat_id=admin_chat_id,
            message_id=progress_message_id,
            text=report,
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"[BROADCAST #{broadcast_id}] Не удалось обновить финальный отчёт: {e}")
        try:
            await bot.send_message(chat_id=admin_chat_id, text=report, parse_mode="HTML")
        except Exception:
            pass

    logging.info(f"[BROADCAST #{broadcast_id}] {status_text}: delivered={delivered}, failed={failed}, blocked={blocked}")


async def _update_broadcast_progress(bot, chat_id, message_id, broadcast_id, current, total, delivered, failed, blocked):
    """Обновляет сообщение прогресса рассылки."""
    try:
        progress_bar = _make_progress_bar(current, total)
        pct = int(current / total * 100) if total > 0 else 0

        text = (
            f"🚀 <b>Рассылка #{broadcast_id} в процессе...</b>\n\n"
            f"📊 Прогресс: {current}/{total} ({pct}%)\n"
            f"{progress_bar}\n\n"
            f"✅ Доставлено: {delivered}\n"
            f"❌ Ошибок: {failed}\n"
            f"🚫 Заблокировали: {blocked}"
        )

        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⛔ Остановить", callback_data=f"bc_cancel_running_{broadcast_id}")]
            ])
        )
    except BadRequest:
        pass  # "Message is not modified" — прогресс не изменился
    except Exception as e:
        logging.debug(f"[BROADCAST] Ошибка обновления прогресса: {e}")


def _make_progress_bar(current: int, total: int, length: int = 20) -> str:
    """Создаёт визуальный прогресс-бар."""
    if total == 0:
        return "<code>" + "░" * length + "</code>"
    filled = int(length * current / total)
    bar = "█" * filled + "░" * (length - filled)
    return f"<code>{bar}</code>"


async def cancel_running_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Останавливает запущенную рассылку."""
    query = update.callback_query
    broadcast_id_str = query.data.replace("bc_cancel_running_", "")

    try:
        broadcast_id = int(broadcast_id_str)
    except ValueError:
        await query.answer("❌ Неверный ID рассылки", show_alert=True)
        return ConversationHandler.END

    context.bot_data[f'cancel_broadcast_{broadcast_id}'] = True
    await query.answer("⛔ Рассылка будет остановлена...", show_alert=True)
    logging.info(f"[BROADCAST #{broadcast_id}] Запрос на отмену от {query.from_user.id}")
    return ConversationHandler.END


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отмена рассылки до запуска."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop('broadcast', None)
    await query.edit_message_text("❌ Рассылка отменена.")
    return await admin_panel(update, context)


async def show_api_monitoring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает статистику использования Vertex AI API через rate limiter."""
    from rate_limiter import vertex_rate_limiter

    query = update.callback_query
    await query.answer()

    # Получаем статистику
    stats = vertex_rate_limiter.get_stats()

    # Формируем сообщение
    text = "📊 <b>Мониторинг Vertex AI API</b>\n\n"

    # Общая статистика
    text += "<b>📈 Общая статистика:</b>\n"
    text += f"├ Всего запросов: <code>{stats['total_requests']}</code>\n"
    text += f"├ Успешных: <code>{stats['successful_requests']}</code> ✅\n"
    text += f"├ Неудачных: <code>{stats['failed_requests']}</code> ❌\n"
    text += f"├ Таймауты очереди: <code>{stats['queue_timeouts']}</code> ⏱\n"
    text += f"└ ResourceExhausted: <code>{stats['resource_exhausted_errors']}</code> 🚫\n\n"

    # Success rate
    if stats['total_requests'] > 0:
        success_rate = (stats['successful_requests'] / stats['total_requests']) * 100
        text += f"<b>Success Rate:</b> {success_rate:.1f}%\n\n"

    # Текущая нагрузка по моделям
    text += "<b>⚡ Текущая нагрузка (QPM):</b>\n"
    current_qpm = stats.get('current_qpm', {})
    qpm_limits = stats.get('qpm_limits', {})

    for model_name, limit in qpm_limits.items():
        current = current_qpm.get(model_name, 0)
        percentage = (current / limit) * 100 if limit > 0 else 0

        # Выбираем эмодзи в зависимости от загрузки
        if percentage < 50:
            emoji = "🟢"
        elif percentage < 80:
            emoji = "🟡"
        else:
            emoji = "🔴"

        text += f"{emoji} <code>{model_name}</code>:\n"
        text += f"   └ {current}/{limit} QPM ({percentage:.0f}%)\n"

    # Adaptive throttling статус
    text += "\n<b>🎛 Adaptive Throttling:</b>\n"
    adaptive = stats.get('adaptive_throttling', {})
    if any(adaptive.values()):
        for model_name, is_active in adaptive.items():
            if is_active:
                text += f"⚠️ <code>{model_name}</code>: АКТИВЕН\n"
    else:
        text += "✅ Не активен (все модели работают нормально)\n"

    # Счетчики ошибок
    error_counts = stats.get('error_counts', {})
    if any(error_counts.values()):
        text += "\n<b>⚠️ Счетчики ошибок:</b>\n"
        for model_name, count in error_counts.items():
            if count > 0:
                text += f"├ <code>{model_name}</code>: {count}\n"

    # Кнопки
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="api_monitoring")],
        [InlineKeyboardButton("♻️ Сбросить статистику", callback_data="api_reset_stats")],
        [InlineKeyboardButton("🗑️ Очистить Google Drive", callback_data="api_cleanup_gdrive")],
        [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel_from_monitoring")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
    return CHOOSING_ACTION


async def reset_api_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сбрасывает статистику rate limiter."""
    from rate_limiter import vertex_rate_limiter

    query = update.callback_query
    await query.answer("Статистика сброшена ✅")

    vertex_rate_limiter.reset_stats()
    logging.info(f"Админ {query.from_user.id} сбросил статистику rate limiter")

    # Показываем обновленную статистику
    return await show_api_monitoring(update, context)


async def cleanup_gdrive_storage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Очищает хранилище Google Drive сервисного аккаунта."""
    from google_docs_exporter import _cleanup_service_account_storage

    query = update.callback_query
    await query.answer("Начинаю очистку...")

    try:
        cleaned = _cleanup_service_account_storage()
        logging.info(f"Админ {query.from_user.id} запустил очистку Google Drive: удалено {cleaned} файлов")

        # Обновляем сообщение
        await query.edit_message_text(
            text=f"🗑️ <b>Очистка Google Drive завершена</b>\n\n"
                 f"✅ Очищено/удалено: {cleaned} файлов\n\n"
                 f"<i>Корзина очищена, старые документы CCW (>7 дней) удалены.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 К мониторингу", callback_data="api_monitoring")],
                [InlineKeyboardButton("◀️ В админ-панель", callback_data="admin_panel_from_monitoring")]
            ])
        )
    except Exception as e:
        logging.error(f"Ошибка при очистке Google Drive: {e}")
        await query.edit_message_text(
            text=f"❌ <b>Ошибка при очистке</b>\n\n<code>{str(e)[:500]}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_monitoring")]
            ])
        )

    return CHOOSING_ACTION


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("Рассылка отменена.")
    return await admin_panel(update, context)


# === УПРАВЛЕНИЕ REST API ===

def _generate_api_key() -> tuple[str, str, str]:
    """Генерирует API-ключ, его хеш и префикс."""
    random_bytes = secrets.token_urlsafe(32)
    full_key = f"sk_live_{random_bytes}"
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    key_prefix = full_key[:12]
    return full_key, key_hash, key_prefix


def _is_api_server_running(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Проверяет, запущен ли API сервер (subprocess или Docker-контейнер)."""
    # Способ 1: subprocess (legacy, для обратной совместимости)
    process = context.application.bot_data.get('api_server_process')
    if process is not None and process.poll() is None:
        return True
    # Способ 2: API может быть запущен как отдельный Docker-сервис — проверяем через HTTP
    try:
        import urllib.request
        url = f"http://{API_SERVER_HOST if API_SERVER_HOST != '0.0.0.0' else '127.0.0.1'}:{API_SERVER_PORT}/health"
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _notify_super_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Отправляет уведомление супер-админу."""
    try:
        await context.bot.send_message(chat_id=SUPER_ADMIN_ID, text=message, parse_mode='HTML')
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления супер-админу: {e}")


async def _notify_api_action(context: ContextTypes.DEFAULT_TYPE, admin_id: int, action: str, details: str = ""):
    """
    Уведомляет супер-админа о действиях обычных админов в API.
    Если действие выполнил сам супер-админ, уведомление не отправляется.
    """
    if admin_id == SUPER_ADMIN_ID:
        return  # Не уведомляем супер-админа о его собственных действиях

    # Получаем информацию об админе
    admin_info = await db_manager.fetchrow(
        "SELECT user_id, username, full_name FROM users WHERE user_id = $1",
        admin_id
    )
    admin_name = "Неизвестный"
    if admin_info:
        if admin_info.get('username'):
            admin_name = f"@{admin_info['username']}"
        elif admin_info.get('full_name'):
            admin_name = admin_info['full_name']
        else:
            admin_name = f"ID: {admin_id}"

    message = (
        f"🔔 <b>Действие в API</b>\n\n"
        f"👤 <b>Админ:</b> {admin_name} (<code>{admin_id}</code>)\n"
        f"⚡ <b>Действие:</b> {action}\n"
    )
    if details:
        message += f"📝 <b>Детали:</b> {details}"

    await _notify_super_admin(context, message)


async def manage_api_keys_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Главное меню управления API."""
    query = update.callback_query
    await query.answer()

    # Проверяем статус сервера
    is_running = _is_api_server_running(context)
    server_status = "🟢 Запущен" if is_running else "🔴 Остановлен"

    # Считаем ключи
    total_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys") or 0
    active_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE is_active = true") or 0

    text = f"""🔑 <b>Управление REST API</b>

<b>Статус сервера:</b> {server_status}
<b>Порт:</b> {API_SERVER_PORT}

<b>API-ключи:</b> {active_keys} активных / {total_keys} всего
"""

    # Кнопки
    if is_running:
        server_btn = InlineKeyboardButton("⏹️ Остановить API", callback_data="api_stop_server")
    else:
        server_btn = InlineKeyboardButton("🚀 Запустить API", callback_data="api_start_server")

    keyboard = [
        [server_btn],
        [InlineKeyboardButton("📊 Статус API", callback_data="api_server_status")],
        [InlineKeyboardButton("➕ Создать ключ", callback_data="api_create_key")],
        [InlineKeyboardButton(f"📋 Список ключей ({total_keys})", callback_data="api_list_keys")],
        [InlineKeyboardButton("📈 Статистика", callback_data="api_stats")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return MANAGE_API_KEYS


async def api_start_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает API сервер."""
    query = update.callback_query
    await query.answer()

    if _is_api_server_running(context):
        await query.edit_message_text(
            "⚠️ API сервер уже запущен!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
        return MANAGE_API_KEYS

    try:
        # Получаем корневую директорию проекта
        project_root = os.path.dirname(os.path.abspath(__file__))
        run_server_path = os.path.join(project_root, "api", "run_server.py")

        # Запускаем ��ервер через subprocess
        process = subprocess.Popen(
            [sys.executable, run_server_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=project_root,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
        )

        # Даем серверу время на запуск
        import asyncio
        await asyncio.sleep(2)

        # Проверяем, не упал ли процесс сразу
        if process.poll() is not None:
            # Процесс уже завершился - читаем ошибку
            stdout, stderr = process.communicate(timeout=5)
            error_msg = stderr.decode('utf-8', errors='replace')[:1000] if stderr else "Нет данных об ошибке"
            stdout_msg = stdout.decode('utf-8', errors='replace')[:500] if stdout else ""

            logging.error(f"API сервер упал при запуске: {error_msg}")

            # Экранируем HTML-символы чтобы избежать ошибок парсинга
            import html
            error_msg_safe = html.escape(error_msg)
            stdout_msg_safe = html.escape(stdout_msg)

            await query.edit_message_text(
                f"❌ <b>API сервер упал при запуске!</b>\n\n"
                f"<b>Код возврата:</b> {process.returncode}\n\n"
                f"<b>Ошибка:</b>\n<pre>{error_msg_safe}</pre>\n\n"
                f"<b>Вывод:</b>\n<pre>{stdout_msg_safe}</pre>",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Повторить", callback_data="api_start_server")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
                ])
            )
            return MANAGE_API_KEYS

        context.application.bot_data['api_server_process'] = process

        logging.info(f"API сервер запущен, PID: {process.pid}")

        await query.edit_message_text(
            f"✅ <b>API сервер запущен!</b>\n\n"
            f"PID: <code>{process.pid}</code>\n"
            f"Адрес: <code>http://{API_SERVER_HOST}:{API_SERVER_PORT}</code>\n\n"
            f"📚 <b>Документация:</b>\n"
            f"<code>http://localhost:{API_SERVER_PORT}/verblud?api_key=ВАШ_КЛЮЧ</code>\n\n"
            f"⚠️ Для доступа к документации нужен API-ключ!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            query.from_user.id,
            "🚀 Запуск API сервера",
            f"PID: {process.pid}"
        )

    except Exception as e:
        logging.error(f"Ошибка запуска API сервера: {e}")
        await query.edit_message_text(
            f"❌ Ошибка запуска API сервера:\n<code>{str(e)[:500]}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

    return MANAGE_API_KEYS


async def api_stop_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Останавливает API сервер."""
    query = update.callback_query
    await query.answer()

    process = context.application.bot_data.get('api_server_process')

    if not process or process.poll() is not None:
        await query.edit_message_text(
            "⚠️ API сервер не запущен!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
        return MANAGE_API_KEYS

    try:
        pid = process.pid
        process.terminate()
        process.wait(timeout=5)

        context.application.bot_data['api_server_process'] = None

        logging.info(f"API сервер остановлен, PID: {pid}")

        await query.edit_message_text(
            f"✅ <b>API сервер остановлен!</b>\n\nPID: <code>{pid}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            query.from_user.id,
            "⏹️ Остановка API сервера",
            f"PID: {pid}"
        )

    except Exception as e:
        logging.error(f"Ошибка остановки API сервера: {e}")
        await query.edit_message_text(
            f"❌ Ошибка остановки API сервера:\n<code>{str(e)[:500]}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )

    return MANAGE_API_KEYS


async def api_server_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает статус API сервера."""
    query = update.callback_query
    await query.answer()

    is_running = _is_api_server_running(context)
    process = context.application.bot_data.get('api_server_process')

    if is_running and process:
        text = f"""📊 <b>Статус API сервера</b>

<b>Состояние:</b> 🟢 Запущен
<b>PID:</b> <code>{process.pid}</code>
<b>Адрес:</b> <code>http://{API_SERVER_HOST}:{API_SERVER_PORT}</code>

<b>Эндпоинты:</b>
• <code>POST /v1/ask</code> — ответ на вопрос
• <code>POST /v1/criminal-case/start</code> — создание дела
• <code>GET /health</code> — проверка здоровья
• <code>GET /verblud</code> — документация (требует API-ключ)"""
    else:
        text = """📊 <b>Статус API сервера</b>

<b>Состояние:</b> 🔴 Остановлен

Нажмите "Запустить API" для старта сервера."""

    try:
        await query.edit_message_text(
            text=text,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Обновить", callback_data="api_server_status")],
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
    except Exception as e:
        # Игнорируем ошибку "Message is not modified"
        if "Message is not modified" not in str(e):
            raise
    return MANAGE_API_KEYS


async def api_create_key_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает процесс создания ключа — выбор типа."""
    query = update.callback_query
    await query.answer()

    text = """➕ <b>Создание API-ключа</b>

Выберите тип ключа:

🌐 <b>Web</b> — для сайтов
• Требует указания доменов (CORS)
• Проверяет Origin/Referer

📱 <b>App</b> — для приложений
• Без CORS
• Блокирует браузерные запросы"""

    keyboard = [
        [InlineKeyboardButton("🌐 Для сайта (Web)", callback_data="api_key_type_web")],
        [InlineKeyboardButton("📱 Для приложения (App)", callback_data="api_key_type_app")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GET_API_KEY_TYPE


async def api_key_select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает выбор типа ключа."""
    query = update.callback_query
    await query.answer()

    key_type = "web" if "web" in query.data else "app"
    context.user_data['api_key_type'] = key_type

    await query.edit_message_text(
        f"📝 Введите название для ключа (например, 'Production Site' или 'Mobile App'):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
        ])
    )
    return GET_API_KEY_NAME


async def api_key_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает название ключа."""
    name = update.message.text.strip()[:100]
    context.user_data['api_key_name'] = name

    key_type = context.user_data.get('api_key_type', 'app')

    if key_type == 'web':
        await update.message.reply_text(
            f"🌐 Введите разрешённые домены через запятую:\n\n"
            f"Примеры:\n"
            f"• <code>https://mysite.com</code>\n"
            f"• <code>https://mysite.com, https://api.mysite.com</code>\n"
            f"• <code>*.mysite.com</code> (wildcard поддомены)",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
            ])
        )
        return GET_API_KEY_ORIGINS
    else:
        # Для app-ключей сразу спрашиваем IP whitelist
        await update.message.reply_text(
            f"🔒 Введите разрешённые IP-адреса через запятую (или нажмите 'Пропустить'):\n\n"
            f"Примеры:\n"
            f"• <code>1.2.3.4</code>\n"
            f"• <code>1.2.3.4, 5.6.7.8</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Пропустить", callback_data="api_key_skip_ips")],
                [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
            ])
        )
        return GET_API_KEY_IPS


async def api_key_receive_origins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает разрешённые домены для web-ключа."""
    origins_text = update.message.text.strip()
    origins = [o.strip() for o in origins_text.split(',') if o.strip()]
    context.user_data['api_key_origins'] = origins

    await update.message.reply_text(
        f"🔒 Введите разрешённые IP-адреса через запятую (или нажмите 'Пропустить'):",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭️ Пропустить", callback_data="api_key_skip_ips")],
            [InlineKeyboardButton("◀️ Отмена", callback_data="manage_api_keys")]
        ])
    )
    return GET_API_KEY_IPS


async def api_key_receive_ips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает IP whitelist."""
    ips_text = update.message.text.strip()
    ips = [ip.strip() for ip in ips_text.split(',') if ip.strip()]
    context.user_data['api_key_ips'] = ips

    return await _create_api_key(update, context)


async def api_key_skip_ips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пропускает ввод IP whitelist."""
    query = update.callback_query
    await query.answer()

    context.user_data['api_key_ips'] = []

    return await _create_api_key(update, context, from_callback=True)


async def _create_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback: bool = False) -> int:
    """Создаёт API-ключ в БД и показывает результат."""
    user_id = update.effective_user.id

    # Получаем данные из контекста
    key_type = context.user_data.get('api_key_type', 'app')
    name = context.user_data.get('api_key_name', 'Unnamed')
    origins = context.user_data.get('api_key_origins', [])
    ips = context.user_data.get('api_key_ips', [])

    # Генерируем ключ
    full_key, key_hash, key_prefix = _generate_api_key()

    # Сохраняем в БД
    try:
        query = """
            INSERT INTO api_keys (key_hash, key_prefix, name, owner_user_id, key_type, 
                                  allowed_origins, allowed_ips, requests_per_minute)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING id
        """
        key_id = await db_manager.fetchval(
            query, key_hash, key_prefix, name, user_id, key_type,
            origins if origins else None,
            ips if ips else None,
            API_DEFAULT_RPM
        )

        logging.info(f"Создан API-ключ #{key_id} ({key_type}) для пользователя {user_id}")

        # Формируем сообщение с ключом
        type_emoji = "🌐" if key_type == "web" else "📱"
        origins_text = ", ".join(origins) if origins else "Не указаны"
        ips_text = ", ".join(ips) if ips else "Не указаны"

        text = f"""✅ <b>API-ключ создан!</b>

🔑 <b>Ключ (СОХРАНИТЕ, показывается ОДИН раз):</b>
<code>{full_key}</code>

📋 <b>Настройки:</b>
• Тип: {type_emoji} {key_type.upper()}
• Название: {name}
• Домены: {origins_text}
• IP whitelist: {ips_text}
• Лимит: {API_DEFAULT_RPM} запросов/мин

⚠️ <b>Этот ключ больше не будет показан!</b>"""

        keyboard = [[InlineKeyboardButton("◀️ К списку ключей", callback_data="manage_api_keys")]]

        if from_callback:
            await update.callback_query.edit_message_text(
                text=text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            user_id,
            "🔑 Создание API-ключа",
            f"ID: #{key_id}, Тип: {key_type}, Название: {name}"
        )

    except Exception as e:
        logging.error(f"Ошибка создания API-ключа: {e}")
        error_text = f"❌ Ошибка создания ключа:\n<code>{str(e)[:500]}</code>"
        keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]]

        if from_callback:
            await update.callback_query.edit_message_text(
                text=error_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text(
                text=error_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    # Очищаем контекст
    for key in ['api_key_type', 'api_key_name', 'api_key_origins', 'api_key_ips']:
        context.user_data.pop(key, None)

    return MANAGE_API_KEYS


async def api_list_keys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список API-ключей с пагинацией."""
    query = update.callback_query
    await query.answer()

    page = context.user_data.get('api_keys_page', 0)
    per_page = 5

    # Получаем ключи
    keys = await db_manager.fetch("""
        SELECT id, key_prefix, name, key_type, is_active, total_requests, last_used_at
        FROM api_keys
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
    """, per_page + 1, page * per_page)

    total = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys") or 0

    if not keys:
        await query.edit_message_text(
            "📋 <b>API-ключи</b>\n\n<i>Нет созданных ключей.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Создать ключ", callback_data="api_create_key")],
                [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
            ])
        )
        return MANAGE_API_KEYS

    has_next = len(keys) > per_page
    keys = keys[:per_page]

    total_pages = (total + per_page - 1) // per_page
    text = f"📋 <b>API-ключи</b> (стр. {page + 1}/{total_pages})\n\n"

    keyboard = []
    for key in keys:
        status_emoji = "🟢" if key['is_active'] else "🔴"
        type_emoji = "🌐" if key['key_type'] == 'web' else "📱"

        last_used = "никогда"
        if key['last_used_at']:
            delta = datetime.now(key['last_used_at'].tzinfo) - key['last_used_at']
            if delta.days > 0:
                last_used = f"{delta.days}д назад"
            elif delta.seconds > 3600:
                last_used = f"{delta.seconds // 3600}ч назад"
            elif delta.seconds > 60:
                last_used = f"{delta.seconds // 60}м назад"
            else:
                last_used = "только что"

        text += f"{status_emoji} <code>{key['key_prefix']}...</code> — {key['name']} {type_emoji}\n"
        text += f"   📊 {key['total_requests']} запросов | ⏱ {last_used}\n\n"

        keyboard.append([InlineKeyboardButton(
            f"{status_emoji} {key['key_prefix']}... — {key['name'][:20]}",
            callback_data=f"api_key_details_{key['id']}"
        )])

    # Пагинация
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data="api_keys_prev_page"))
    if has_next:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data="api_keys_next_page"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("◀️ Меню API", callback_data="manage_api_keys")])

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return VIEW_API_KEYS_LIST


async def api_keys_change_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меняет страницу списка ключей."""
    query = update.callback_query
    await query.answer()

    page = context.user_data.get('api_keys_page', 0)

    if "next" in query.data:
        context.user_data['api_keys_page'] = page + 1
    else:
        context.user_data['api_keys_page'] = max(0, page - 1)

    return await api_list_keys(update, context)


async def api_key_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детали API-ключа."""
    query = update.callback_query
    await query.answer()

    # Получаем key_id либо из callback_data, либо из контекста
    if query.data and query.data.startswith('api_key_details_'):
        key_id = int(query.data.split('_')[-1])
    else:
        key_id = context.user_data.get('viewing_api_key_id')

    if not key_id:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    context.user_data['viewing_api_key_id'] = key_id

    key = await db_manager.fetchrow("""
        SELECT id, key_prefix, name, key_type, owner_user_id, allowed_origins, allowed_ips,
               is_active, requests_per_minute, total_requests, last_used_at, last_ip, created_at
        FROM api_keys WHERE id = $1
    """, key_id)

    if not key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    status = "🟢 Активен" if key['is_active'] else "🔴 Отключён"
    type_emoji = "🌐 Web" if key['key_type'] == 'web' else "📱 App"

    origins = ", ".join(key['allowed_origins']) if key['allowed_origins'] else "Не указаны"
    ips = ", ".join(key['allowed_ips']) if key['allowed_ips'] else "Не указаны"
    last_used = key['last_used_at'].strftime('%d.%m.%Y %H:%M') if key['last_used_at'] else "никогда"
    created = key['created_at'].strftime('%d.%m.%Y %H:%M') if key['created_at'] else "N/A"

    text = f"""🔑 <b>Детали API-ключа</b>

<b>Префикс:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}
<b>Тип:</b> {type_emoji}
<b>Статус:</b> {status}

<b>Владелец:</b> <code>{key['owner_user_id']}</code>
<b>Создан:</b> {created}

<b>Лимит:</b> {key['requests_per_minute']} RPM
<b>Всего запросов:</b> {key['total_requests']}
<b>Последний запрос:</b> {last_used}
<b>Последний IP:</b> <code>{key['last_ip'] or 'N/A'}</code>

<b>Домены (CORS):</b>
{origins}

<b>IP whitelist:</b>
{ips}"""

    # Кнопки управления
    keyboard = []

    if key['is_active']:
        keyboard.append([InlineKeyboardButton("🔴 Отключить", callback_data=f"api_key_disable_{key_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🟢 Включить", callback_data=f"api_key_enable_{key_id}")])

    keyboard.append([InlineKeyboardButton(f"⚡ Изменить лимит ({key['requests_per_minute']} RPM)", callback_data=f"api_key_rpm_{key_id}")])
    keyboard.append([InlineKeyboardButton("🔄 Ротировать ключ", callback_data=f"api_key_rotate_{key_id}")])
    keyboard.append([InlineKeyboardButton("🗑️ Удалить", callback_data=f"api_key_delete_{key_id}")])
    keyboard.append([InlineKeyboardButton("◀️ К списку", callback_data="api_list_keys")])

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return VIEW_API_KEY_DETAILS


async def api_key_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включает/отключает API-ключ."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])
    enable = "enable" in query.data

    # Получаем информацию о ключе для уведомления
    key_info = await db_manager.fetchrow(
        "SELECT name, owner_user_id FROM api_keys WHERE id = $1", key_id
    )

    await db_manager.execute(
        "UPDATE api_keys SET is_active = $1 WHERE id = $2",
        enable, key_id
    )

    action = "включён" if enable else "отключён"
    action_emoji = "🟢" if enable else "🔴"
    logging.info(f"API-ключ #{key_id} {action} администратором {query.from_user.id}")

    # Уведомляем супер-админа (если это не сам супер-админ)
    if key_info:
        await _notify_api_action(
            context,
            query.from_user.id,
            f"{action_emoji} Ключ {action}",
            f"ID: #{key_id}, Название: {key_info['name']}"
        )

    await query.answer(f"✅ Ключ {action}!")
    return await api_key_details(update, context)


async def api_key_rpm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает меню выбора лимита RPM."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])
    context.user_data['editing_api_key_id'] = key_id

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name, requests_per_minute FROM api_keys WHERE id = $1", key_id
    )

    if not key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    current_rpm = key['requests_per_minute']

    text = f"""⚡ <b>Изменение лимита RPM</b>

<b>Ключ:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}
<b>Текущий лимит:</b> {current_rpm} запросов/мин

Выберите новый лимит или введите своё значение:"""

    # Предустановленные значения
    presets = [5, 10, 20, 30, 50, 100]
    keyboard = []

    # Создаём кнопки по 3 в ряд
    row = []
    for rpm in presets:
        label = f"{'✓ ' if rpm == current_rpm else ''}{rpm} RPM"
        row.append(InlineKeyboardButton(label, callback_data=f"api_key_set_rpm_{key_id}_{rpm}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("✏️ Ввести вручную", callback_data=f"api_key_rpm_custom_{key_id}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"api_key_details_{key_id}")])

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return VIEW_API_KEY_DETAILS


async def api_key_set_rpm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Устанавливает выбранный лимит RPM."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split('_')
    key_id = int(parts[-2])
    new_rpm = int(parts[-1])

    key_info = await db_manager.fetchrow(
        "SELECT name, requests_per_minute FROM api_keys WHERE id = $1", key_id
    )

    if not key_info:
        await query.edit_message_text("❌ Ключ не найден.")
        return MANAGE_API_KEYS

    old_rpm = key_info['requests_per_minute']

    await db_manager.execute(
        "UPDATE api_keys SET requests_per_minute = $1 WHERE id = $2",
        new_rpm, key_id
    )

    logging.info(f"API-ключ #{key_id}: лимит изменён {old_rpm} -> {new_rpm} RPM администратором {query.from_user.id}")

    # Уведомляем супер-админа
    await _notify_api_action(
        context,
        query.from_user.id,
        "⚡ Изменение лимита RPM",
        f"ID: #{key_id}, Название: {key_info['name']}, {old_rpm} → {new_rpm} RPM"
    )

    await query.answer(f"✅ Лимит изменён на {new_rpm} RPM!")

    # Обновляем контекст для возврата к деталям ключа
    context.user_data['viewing_api_key_id'] = key_id
    return await api_key_details(update, context)


async def api_key_rpm_custom_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает ввод кастомного лимита RPM."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])
    context.user_data['editing_api_key_id'] = key_id

    await query.edit_message_text(
        "✏️ <b>Введите новый лимит RPM</b>\n\n"
        "Укажите число от 1 до 1000:\n"
        "(например: <code>25</code>)",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
        ])
    )
    return GET_API_KEY_RPM


async def api_key_rpm_custom_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает кастомный лимит RPM от пользователя."""
    key_id = context.user_data.get('editing_api_key_id')

    if not key_id:
        await update.message.reply_text("❌ Ошибка: ключ не найден. Попробуйте снова.")
        return MANAGE_API_KEYS

    try:
        new_rpm = int(update.message.text.strip())
        if new_rpm < 1 or new_rpm > 1000:
            raise ValueError("Out of range")
    except ValueError:
        await update.message.reply_text(
            "❌ Некорректное значение. Введите число от 1 до 1000:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
            ])
        )
        return GET_API_KEY_RPM

    key_info = await db_manager.fetchrow(
        "SELECT name, requests_per_minute FROM api_keys WHERE id = $1", key_id
    )

    if not key_info:
        await update.message.reply_text("❌ Ключ не найден.")
        return MANAGE_API_KEYS

    old_rpm = key_info['requests_per_minute']

    await db_manager.execute(
        "UPDATE api_keys SET requests_per_minute = $1 WHERE id = $2",
        new_rpm, key_id
    )

    logging.info(f"API-ключ #{key_id}: лимит изменён {old_rpm} -> {new_rpm} RPM администратором {update.effective_user.id}")

    # Уведомляем супер-админа
    await _notify_api_action(
        context,
        update.effective_user.id,
        "⚡ Изменение лимита RPM",
        f"ID: #{key_id}, Название: {key_info['name']}, {old_rpm} → {new_rpm} RPM"
    )

    await update.message.reply_text(
        f"✅ <b>Лимит успешно изменён!</b>\n\n"
        f"<b>Ключ:</b> {key_info['name']}\n"
        f"<b>Новый лимит:</b> {new_rpm} RPM",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К деталям ключа", callback_data=f"api_key_details_{key_id}")]
        ])
    )
    return VIEW_API_KEY_DETAILS


async def api_key_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение удаления ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name FROM api_keys WHERE id = $1", key_id
    )

    if not key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    text = f"""⚠️ <b>Подтверждение удаления</b>

Вы действительно хотите удалить ключ?

<b>Префикс:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}

<b>Это действие необратимо!</b>"""

    keyboard = [
        [InlineKeyboardButton("🗑️ Да, удалить", callback_data=f"api_key_delete_exec_{key_id}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_DELETE_API_KEY


async def api_key_delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет удаление ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name, owner_user_id FROM api_keys WHERE id = $1", key_id
    )

    if key:
        await db_manager.execute("DELETE FROM api_keys WHERE id = $1", key_id)
        logging.info(f"API-ключ #{key_id} удалён администратором {query.from_user.id}")

        # Уведомляем супер-админа (если это не сам супер-админ)
        await _notify_api_action(
            context,
            query.from_user.id,
            "🗑️ Удаление API-ключа",
            f"ID: #{key_id}, Название: {key['name']}, Владелец: {key['owner_user_id']}"
        )

    await query.edit_message_text(
        "✅ Ключ успешно удалён!",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К списку ключей", callback_data="api_list_keys")]
        ])
    )
    return MANAGE_API_KEYS


async def api_key_rotate_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждение ротации ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    key = await db_manager.fetchrow(
        "SELECT key_prefix, name FROM api_keys WHERE id = $1", key_id
    )

    text = f"""🔄 <b>Ротация ключа</b>

Будет создан новый ключ с теми же настройками.
Старый ключ перестанет работать!

<b>Ключ:</b> <code>{key['key_prefix']}...</code>
<b>Название:</b> {key['name']}

Продолжить?"""

    keyboard = [
        [InlineKeyboardButton("🔄 Да, ротировать", callback_data=f"api_key_rotate_exec_{key_id}")],
        [InlineKeyboardButton("◀️ Отмена", callback_data=f"api_key_details_{key_id}")]
    ]

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CONFIRM_ROTATE_API_KEY


async def api_key_rotate_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет ротацию ключа."""
    query = update.callback_query
    await query.answer()

    key_id = int(query.data.split('_')[-1])

    # Получаем старый ключ
    old_key = await db_manager.fetchrow("""
        SELECT name, key_type, owner_user_id, allowed_origins, allowed_ips, requests_per_minute
        FROM api_keys WHERE id = $1
    """, key_id)

    if not old_key:
        await query.edit_message_text(
            "❌ Ключ не найден.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="api_list_keys")]
            ])
        )
        return MANAGE_API_KEYS

    # Генерируем новый ключ
    full_key, key_hash, key_prefix = _generate_api_key()

    # Обновляем в БД
    await db_manager.execute("""
        UPDATE api_keys 
        SET key_hash = $1, key_prefix = $2, total_requests = 0, last_used_at = NULL, last_ip = NULL
        WHERE id = $3
    """, key_hash, key_prefix, key_id)

    logging.info(f"API-ключ #{key_id} ротирован администратором {query.from_user.id}")

    text = f"""✅ <b>Ключ успешно ротирован!</b>

🔑 <b>Новый ключ (СОХРАНИТЕ):</b>
<code>{full_key}</code>

<b>Название:</b> {old_key['name']}

⚠️ <b>Старый ключ больше не работает!</b>
⚠️ <b>Этот ключ больше не будет показан!</b>"""

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ К списку ключей", callback_data="api_list_keys")]
        ])
    )

    # Уведомляем супер-админа (если это не сам супер-админ)
    await _notify_api_action(
        context,
        query.from_user.id,
        "🔄 Ротация API-ключа",
        f"ID: #{key_id}, Название: {old_key['name']}, Владелец: {old_key['owner_user_id']}"
    )

    return MANAGE_API_KEYS


async def api_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает общую статистику API."""
    query = update.callback_query
    await query.answer()

    # Собираем статистику
    total_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys") or 0
    active_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE is_active = true") or 0
    web_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE key_type = 'web'") or 0
    app_keys = await db_manager.fetchval("SELECT COUNT(*) FROM api_keys WHERE key_type = 'app'") or 0
    total_requests = await db_manager.fetchval("SELECT COALESCE(SUM(total_requests), 0) FROM api_keys") or 0

    # Топ ключей по использованию
    top_keys = await db_manager.fetch("""
        SELECT key_prefix, name, total_requests 
        FROM api_keys 
        ORDER BY total_requests DESC 
        LIMIT 5
    """)

    text = f"""📈 <b>Статистика REST API</b>

<b>Ключи:</b>
├ Всего: {total_keys}
├ Активных: {active_keys}
├ 🌐 Web: {web_keys}
└ 📱 App: {app_keys}

<b>Запросы:</b>
└ Всего выполнено: {total_requests}

<b>Топ-5 ключей по использованию:</b>
"""

    for i, key in enumerate(top_keys, 1):
        text += f"{i}. <code>{key['key_prefix']}...</code> — {key['total_requests']} запр.\n"
        text += f"   {key['name'][:30]}\n"

    if not top_keys:
        text += "<i>Нет данных</i>\n"

    await query.edit_message_text(
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="api_stats")],
            [InlineKeyboardButton("◀️ Назад", callback_data="manage_api_keys")]
        ])
    )
    return MANAGE_API_KEYS


# === ПРОМО-АКЦИИ ===

async def manage_promo_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню управления промо-акцией."""
    query = update.callback_query
    await query.answer()

    promo = await get_promo_info()
    is_active = promo.get('enabled', False)

    # Проверяем автоистечение
    if is_active:
        if not await is_promo_active():
            promo = await get_promo_info()
            is_active = False

    if is_active:
        status = "🟢 <b>АКТИВНА</b>"
        desc = promo.get('description') or '—'
        end_date_str = promo.get('end_date')
        if end_date_str:
            try:
                from datetime import datetime as dt_cls
                end_dt = dt_cls.fromisoformat(end_date_str)
                end_info = f"⏰ До: <b>{end_dt.strftime('%d.%m.%Y %H:%M')}</b> (UTC)"
            except (ValueError, TypeError):
                end_info = f"⏰ До: {end_date_str}"
        else:
            end_info = "⏰ <i>Бессрочно</i>"
        admin_id = promo.get('enabled_by', '—')

        text = (
            f"🎉 <b>Промо-акция</b>\n\n"
            f"Статус: {status}\n"
            f"📝 Описание: {desc}\n"
            f"{end_info}\n"
            f"👤 Включил: <code>{admin_id}</code>\n\n"
            f"<i>Пока акция активна, все пользователи получают безлимитные запросы.</i>"
        )
        keyboard = [
            [InlineKeyboardButton("🔴 Выключить акцию", callback_data="promo_disable")],
            [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
        ]
    else:
        text = (
            "🎉 <b>Промо-акция</b>\n\n"
            "Статус: ⚪ <b>НЕАКТИВНА</b>\n\n"
            "Включите акцию, чтобы снять лимиты для всех пользователей.\n"
            "Можно задать дату окончания — акция выключится автоматически."
        )
        keyboard = [
            [InlineKeyboardButton("🟢 Включить акцию", callback_data="promo_enable")],
            [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
        ]

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return MANAGE_PROMO


async def promo_enable_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает дату окончания промо."""
    query = update.callback_query
    await query.answer()

    text = (
        "🎉 <b>Включение промо-акции</b>\n\n"
        "Отправьте дату и время окончания в формате:\n"
        "<code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n\n"
        "Например: <code>23.02.2026 23:59</code>\n\n"
        "Или нажмите кнопку ниже для бессрочной акции."
    )
    keyboard = [
        [InlineKeyboardButton("♾ Без даты окончания", callback_data="promo_no_end_date")],
        [InlineKeyboardButton("◀️ Назад", callback_data="manage_promo")],
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return GET_PROMO_END_DATE


async def promo_receive_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает дату окончания и включает промо."""
    text_input = update.message.text.strip()
    admin_id = update.effective_user.id

    try:
        end_date = datetime.strptime(text_input, "%d.%m.%Y %H:%M")
        end_date = end_date.replace(tzinfo=None)  # Сохраняем как UTC
        from datetime import timezone as tz
        end_date = end_date.replace(tzinfo=tz.utc)
    except ValueError:
        await update.message.reply_text(
            "❌ Неверный формат даты.\n\n"
            "Используйте: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code>\n"
            "Например: <code>23.02.2026 23:59</code>",
            parse_mode="HTML"
        )
        return GET_PROMO_END_DATE

    await set_promo(
        enabled=True,
        end_date=end_date,
        admin_id=admin_id,
        description="Свободные выходные! 🎉"
    )

    await update.message.reply_text(
        f"✅ <b>Промо-акция включена!</b>\n\n"
        f"📝 Описание: Свободные выходные! 🎉\n"
        f"⏰ Окончание: <b>{end_date.strftime('%d.%m.%Y %H:%M')}</b> (UTC)\n\n"
        f"Все пользователи теперь имеют безлимитные запросы.",
        parse_mode="HTML"
    )

    await log_admin_action_to_sheet(admin_id, "PROMO_ENABLE", f"end_date={end_date.isoformat()}")
    return await admin_panel(update, context)


async def promo_enable_no_end_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включает бессрочную промо-акцию."""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    await set_promo(
        enabled=True,
        end_date=None,
        admin_id=admin_id,
        description="Свободные выходные! 🎉"
    )

    await query.edit_message_text(
        "✅ <b>Промо-акция включена!</b>\n\n"
        "📝 Описание: Свободные выходные! 🎉\n"
        "⏰ Окончание: <i>Бессрочно</i>\n\n"
        "Все пользователи теперь имеют безлимитные запросы.\n"
        "Не забудьте выключить вручную!",
        parse_mode="HTML"
    )

    await log_admin_action_to_sheet(admin_id, "PROMO_ENABLE", "end_date=бессрочно")
    await asyncio.sleep(2)
    return await admin_panel(update, context)


async def promo_disable(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выключает промо-акцию."""
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id

    await set_promo(enabled=False, admin_id=admin_id)

    await query.edit_message_text(
        "🔴 <b>Промо-акция выключена.</b>\n\n"
        "Лимиты пользователей восстановлены в обычном режиме.",
        parse_mode="HTML"
    )

    await log_admin_action_to_sheet(admin_id, "PROMO_DISABLE", "")
    await asyncio.sleep(2)
    return await admin_panel(update, context)


# ===========================================================================
# Очередь анализатора дыр в законке
# ===========================================================================

_LOOPHOLE_QUEUE_DISPLAY_CAP = 20


def _loophole_priority_label(priority: int) -> str:
    if priority >= config.LOOPHOLE_PRIORITY_ADMIN:
        return "👑"
    if priority >= config.LOOPHOLE_PRIORITY_VIP:
        return "💎"
    return "👤"


async def _loophole_user_label(user_id: int) -> str:
    rec = await db_manager.fetchrow(
        "SELECT full_name, username FROM users WHERE user_id = $1", user_id
    )
    if rec and rec.get('username'):
        return f"@{rec['username']}"
    if rec and rec.get('full_name'):
        return str(rec['full_name'])[:30]
    return f"id{user_id}"


def _wake_loophole(context: ContextTypes.DEFAULT_TYPE) -> None:
    wake = context.application.bot_data.get("loophole_wake_event")
    if wake is not None:
        wake.set()


async def _loophole_admin_guard(update: Update) -> bool:
    user_id = update.effective_user.id
    rec = await db_manager.fetchrow("SELECT is_admin FROM users WHERE user_id = $1", user_id)
    return bool((rec and rec['is_admin']) or user_id == SUPER_ADMIN_ID)


def _loophole_job_id(query) -> int:
    return int(query.data.rsplit("_", 1)[1])


async def manage_loophole_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Список очереди анализатора: выполняемые + queued (с кнопками на каждую)."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return ConversationHandler.END

    queued = await lq_list_queued()
    running = await lq_list_running(out_of_band=False)
    oob_running = await lq_list_running(out_of_band=True)
    paused = await lq_is_paused()
    cache_on = await lq_cache_enabled()
    cache_n = await lq_cache_count()
    feature_on = await lq_feature_enabled()

    status_line = "⏸ <b>НА ПАУЗЕ</b> (новые не стартуют)" if paused else "▶️ Работает"
    cache_line = f"🗄 Кэш «весь свод»: {'🟢 ВКЛ' if cache_on else '⚪ ВЫКЛ'} (записей: {cache_n})"
    feature_line = f"🔌 Функция у юзеров: {'🟢 ВКЛ' if feature_on else '🔴 ВЫКЛ — кнопка скрыта'}"
    lines = ["🕳 <b>Очередь анализатора дыр</b>", feature_line, status_line, cache_line, ""]
    if paused and not running:
        lines.append("✅ Активных анализов нет — можно безопасно перезапускать бота.")
        lines.append("")
    if running:
        lines.append("⚙️ <b>Выполняется:</b>")
        for j in running:
            label = await _loophole_user_label(j['user_id'])
            lines.append(f"  {_loophole_priority_label(j['priority'])} {label}")
        lines.append("")
    if oob_running:
        lines.append(f"⚡ <b>Параллельно (без очереди):</b> {len(oob_running)}")
        lines.append("")

    keyboard = []
    if queued:
        lines.append(f"⏳ <b>В очереди:</b> {len(queued)}")
        for idx, j in enumerate(queued[:_LOOPHOLE_QUEUE_DISPLAY_CAP], start=1):
            label = await _loophole_user_label(j['user_id'])
            keyboard.append([InlineKeyboardButton(
                f"{idx}. {_loophole_priority_label(j['priority'])} {label}",
                callback_data=f"loophole_job_{j['id']}",
            )])
        if len(queued) > _LOOPHOLE_QUEUE_DISPLAY_CAP:
            lines.append(f"… и ещё {len(queued) - _LOOPHOLE_QUEUE_DISPLAY_CAP} (показаны первые {_LOOPHOLE_QUEUE_DISPLAY_CAP})")
    else:
        lines.append("⏳ Очередь пуста.")

    keyboard.append([InlineKeyboardButton(
        "🔌 Выключить функцию (скрыть у юзеров)" if feature_on else "🔌 Включить функцию (показать юзерам)",
        callback_data="loophole_feature_toggle",
    )])
    if paused:
        keyboard.append([InlineKeyboardButton("▶️ Возобновить очередь", callback_data="loophole_queue_resume")])
    else:
        keyboard.append([InlineKeyboardButton("⏸ Пауза (для рестарта)", callback_data="loophole_queue_pause")])
    keyboard.append([
        InlineKeyboardButton(
            "🗄 Кэш: ВЫКЛ" if cache_on else "🗄 Кэш: ВКЛ",
            callback_data="loophole_cache_toggle",
        ),
        InlineKeyboardButton("🧹 Очистить кэш", callback_data="loophole_cache_clear"),
    ])
    keyboard.append([
        InlineKeyboardButton("📊 Все анализы", callback_data="show_all_analyses"),
        InlineKeyboardButton("🧹 0-дыры из кэша", callback_data="loophole_clear_zero"),
    ])
    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="manage_loophole_queue")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
    markup = InlineKeyboardMarkup(keyboard)
    text = "\n".join(lines)

    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_LOOPHOLE_QUEUE


async def loophole_queue_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    await lq_set_paused(True)
    await query.answer("⏸ Очередь на паузе. Текущие анализы доработают, новые не стартуют.")
    return await manage_loophole_queue(update, context)


async def loophole_queue_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    await lq_set_paused(False)
    _wake_loophole(context)
    await query.answer("▶️ Очередь возобновлена.")
    return await manage_loophole_queue(update, context)


async def loophole_cache_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    new_state = not await lq_cache_enabled()
    await lq_set_cache_enabled(new_state)
    await query.answer("🗄 Кэширование включено." if new_state else "🗄 Кэширование выключено.")
    return await manage_loophole_queue(update, context)


async def loophole_cache_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    n = await lq_clear_cache()
    await query.answer(f"🧹 Кэш очищен (удалено: {n}).")
    return await manage_loophole_queue(update, context)


async def loophole_clear_zero(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Удалить из КЭША записи с 0 дыр (обычно их нет — нули не кэшируются). Архив не трогает."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    n = await lq_clear_zero_cache()
    await query.answer(f"Удалено 0-дырочных из кэша: {n}." if n else "0-дырочных в кэше нет.")
    return await manage_loophole_queue(update, context)


async def loophole_feature_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ВКЛ/ВЫКЛ функцию «Анализатор законов»: при ВЫКЛ кнопка скрывается у пользователей."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE
    new_state = not await lq_feature_enabled()
    await lq_set_feature_enabled(new_state)
    await query.answer("🟢 Анализатор включён — кнопка видна юзерам." if new_state
                       else "🔴 Анализатор выключен — кнопка скрыта у юзеров.")
    return await manage_loophole_queue(update, context)


# --- Архив всех анализов (просмотр/разбор из админки) -----------------------

_RESULTS_PAGE = 10


async def show_all_analyses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Листинг архива всех анализов (с пагинацией и фильтром «только 0 дыр»).
    callback: show_all_analyses (старт) | lres_<offset>_<0|1> (навигация/фильтр)."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_QUEUE

    offset, only_zero = 0, False
    data = query.data if query else "show_all_analyses"
    if data.startswith("lres_"):
        try:
            _, off_s, z_s = data.split("_")
            offset = max(0, int(off_s))
            only_zero = (z_s == "1")
        except (ValueError, IndexError):
            offset, only_zero = 0, False

    total = await lq_count_results(only_zero=only_zero)
    rows = await lq_list_results(limit=_RESULTS_PAGE, offset=offset, only_zero=only_zero)

    title = "📊 <b>Все анализы</b>" + (" — только 0 дыр" if only_zero else "")
    lines = [title, f"Всего: <b>{total}</b>" + (f" • показаны {offset + 1}–{offset + len(rows)}" if rows else ""), ""]
    if not rows:
        lines.append("Записей нет.")

    keyboard = []
    for r in rows:
        ts = r["created_at"].strftime("%d.%m %H:%M") if r.get("created_at") else "?"
        nf = int(r.get("total_findings") or 0)
        mark = "⚠️0" if nf == 0 else f"🕳{nf}"
        oob = " ⚡" if r.get("is_out_of_band") else ""
        srv = r.get("server_id") or "?"
        atype = r.get("analysis_type") or "?"
        keyboard.append([InlineKeyboardButton(
            f"{mark} {ts} • {srv} • {atype}{oob}",
            callback_data=f"lresult_{r['id']}",
        )])

    z = "1" if only_zero else "0"
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"lres_{max(0, offset - _RESULTS_PAGE)}_{z}"))
    if offset + _RESULTS_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"lres_{offset + _RESULTS_PAGE}_{z}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(
        "Показать все" if only_zero else "⚠️ Только 0 дыр",
        callback_data=f"lres_0_{0 if only_zero else 1}",
    )])
    keyboard.append([InlineKeyboardButton("◀️ К очереди", callback_data="manage_loophole_queue")])

    markup = InlineKeyboardMarkup(keyboard)
    text = "\n".join(lines)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_LOOPHOLE_RESULTS


async def loophole_result_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Карточка одного анализа: сводка + кнопка скачать HTML-отчёт. callback: lresult_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS
    try:
        rid = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.answer("Некорректная запись.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS

    rec = await lq_get_result(rid)
    if not rec:
        await query.answer("Анализ не найден.", show_alert=True)
        return await show_all_analyses(update, context)

    report = rec.get("result") or {}
    label = await _loophole_user_label(rec["user_id"]) if rec.get("user_id") else "—"
    ts = rec["created_at"].strftime("%d.%m.%Y %H:%M") if rec.get("created_at") else "?"
    meta = (
        f"\n\n<i>#{rid} • {ts}{' ⚡oob' if rec.get('is_out_of_band') else ''}\n"
        f"сервер: {rec.get('server_id')} • тип: {rec.get('analysis_type')}\n"
        f"профиль: {rec.get('model_profile')} • режим: {rec.get('selection_mode')}\n"
        f"инициатор: {label} • дыр: {rec.get('total_findings')}</i>"
    )
    text = lq_format_summary(report) + meta

    keyboard = [
        [InlineKeyboardButton("📄 Скачать HTML-отчёт", callback_data=f"lresdoc_{rid}")],
        [InlineKeyboardButton("◀️ К списку", callback_data="show_all_analyses")],
    ]
    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    except Exception:
        await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ADMIN_LOOPHOLE_RESULTS


async def loophole_result_doc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Прислать оформленный HTML-отчёт по архивной записи. callback: lresdoc_<id>."""
    import io
    query = update.callback_query
    await query.answer("Готовлю отчёт…")
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS
    try:
        rid = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        return ADMIN_LOOPHOLE_RESULTS
    rec = await lq_get_result(rid)
    if not rec:
        await query.answer("Анализ не найден.", show_alert=True)
        return ADMIN_LOOPHOLE_RESULTS
    report = rec.get("result") or {}
    try:
        buf = io.BytesIO(lq_build_html_doc(report, title_suffix=f"архив #{rid}"))
        buf.name = f"analysis_{rid}.html"
        await context.bot.send_document(
            chat_id=query.message.chat_id, document=buf, filename=f"analysis_{rid}.html",
            caption=f"Анализ #{rid} • дыр: {rec.get('total_findings')}",
        )
    except Exception:
        logging.exception("[ADMIN] не удалось отправить HTML-отчёт анализа %s", rid)
        await query.answer("Не удалось сформировать отчёт.", show_alert=True)
    return ADMIN_LOOPHOLE_RESULTS


# --- Заявки в тестировщики (просмотр/отбор; доступно всем админам) ---

_TAPPS_PAGE = 10

_TAPP_Q_LABELS = {
    "q1": "О себе / опыт в RP",
    "q2": "Мотивация",
    "q3": "ПК / Windows",
}
_TAPP_STATUS_H = {
    "pending": "⏳ на рассмотрении",
    "approved": "✅ принята",
    "rejected": "❌ отклонена",
}
_TAPP_STATUS_MARK = {"pending": "⏳", "approved": "✅", "rejected": "❌"}


# ---------------------------------------------------------------------------
# Беседа тестеров: бот зовёт всех тестировщиков в группу админа.
# ВАЖНО: Bot API НЕ умеет добавлять участников в чат напрямую (это умеет только
# userbot/MTProto). Поэтому схема: админ создаёт группу → добавляет бота и делает его
# админом с правом приглашать → пишет в группе /invite_testers → бот делает ссылку-
# приглашение и рассылает её всем тестерам в личку (один тап — человек в беседе).
# ---------------------------------------------------------------------------

_TESTERS_GROUP_KEY = "testers_group_chat_id"


async def invite_testers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/invite_testers — выполнять В ГРУППЕ тестеров. Бот создаёт invite-ссылку и зовёт
    всех тестеров в личку. Повторный запуск зовёт только тех, кого ещё нет в беседе."""
    chat = update.effective_chat
    msg = update.effective_message
    if chat is None or chat.type not in ("group", "supergroup"):
        await msg.reply_text(
            "Эту команду нужно отправить В ГРУППЕ, куда добавлен бот.\n"
            "Создай беседу тестеров, добавь бота, сделай его администратором (с правом "
            "приглашать по ссылке) и напиши там /invite_testers."
        )
        return
    if not await _loophole_admin_guard(update):
        await msg.reply_text("🚫 Звать тестеров может только администратор бота.")
        return

    # Бот должен быть админом группы с правом приглашать → проверяем созданием ссылки.
    try:
        link = await context.bot.create_chat_invite_link(chat.id, name="Тестировщики")
        invite_url = link.invite_link
    except Exception as e:
        logging.warning("[INVITE_TESTERS] create_chat_invite_link failed: %s", e)
        await msg.reply_text(
            "Не получилось создать ссылку-приглашение.\n"
            "Сделай бота <b>администратором</b> этой группы с правом "
            "«Пригласить пользователей по ссылке» и повтори <code>/invite_testers</code>.",
            parse_mode="HTML",
        )
        return

    # Запоминаем группу (для будущих повторных приглашений).
    try:
        await db_manager.execute(
            "INSERT INTO system_cache (key, value, updated_at) VALUES ($1, $2::jsonb, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            _TESTERS_GROUP_KEY, json.dumps(chat.id),
        )
    except Exception:
        logging.warning("[INVITE_TESTERS] не удалось сохранить chat_id группы", exc_info=True)

    rows = await db_manager.fetch(
        "SELECT user_id FROM users WHERE is_tester = TRUE AND is_banned = FALSE "
        "AND COALESCE(is_blocked, FALSE) = FALSE"
    )
    tester_ids = [r["user_id"] for r in rows]
    if not tester_ids:
        await msg.reply_text("В базе нет активных тестировщиков для приглашения.")
        return

    status = await msg.reply_text(f"📨 Зову тестеров в беседу… (всего {len(tester_ids)})")
    # Тяжёлую рассылку — в фон (concurrent_updates выключен, иначе блокирует остальных).
    asyncio.create_task(
        _invite_testers_job(context.bot, chat.id, invite_url, tester_ids, status)
    )


async def _invite_testers_job(bot, group_chat_id, invite_url, tester_ids, status_msg) -> None:
    """Фон: разослать ссылку-приглашение всем тестерам в личку (кого ещё нет в беседе)."""
    invited = skipped = blocked = failed = 0
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Вступить в беседу", url=invite_url)]])
    text = (
        "👋 Тебя зовут в <b>беседу тестировщиков</b>.\n"
        "Жми кнопку ниже, чтобы вступить:"
    )
    for i, uid in enumerate(tester_ids):
        # Уже в беседе? — не спамим (актуально для повторных запусков).
        try:
            member = await bot.get_chat_member(group_chat_id, uid)
            if member.status in ("creator", "administrator", "member", "restricted"):
                skipped += 1
                await asyncio.sleep(0.03)
                continue
        except Exception:
            pass  # не участник / не найден → приглашаем

        try:
            await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
            invited += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
                invited += 1
            except Forbidden:
                blocked += 1
                await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", uid)
            except Exception:
                failed += 1
        except Forbidden:
            blocked += 1
            await db_manager.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", uid)
        except Exception as e:
            failed += 1
            logging.warning("[INVITE_TESTERS] send to %s failed: %s", uid, e)

        # Rate limiting ~25 msg/sec (как в рассылке).
        if (i + 1) % 25 == 0:
            await asyncio.sleep(1.1)
        else:
            await asyncio.sleep(0.05)

    report = (
        f"✅ Готово.\n"
        f"Приглашено: <b>{invited}</b>\n"
        f"Уже в беседе: {skipped}\n"
        f"Не дошло (заблокировали/не писали боту): {blocked}\n"
    )
    if failed:
        report += f"Ошибок: {failed}\n"
    report += (
        "\nКого «не дошло» — попроси написать боту <code>/start</code> и вступить по ссылке, "
        "либо добавь вручную из своего аккаунта."
    )
    try:
        await status_msg.edit_text(report, parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(group_chat_id, report, parse_mode="HTML")
        except Exception:
            pass


async def manage_tester_applications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню заявок в тестеры: счётчики, deep-link для канала, тумблер приёма."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return CHOOSING_ACTION

    pending = await ta_count_pending()
    total = await ta_count_applications(only_pending=False)
    intake = await ta_intake_enabled()
    intake_s = "🟢 ВКЛ" if intake else "🔴 ВЫКЛ"
    try:
        me = await context.bot.get_me()
        deep_link = f"https://t.me/{me.username}?start=apply_tester"
    except Exception:
        deep_link = "https://t.me/your_bot?start=apply_tester"

    text = (
        "📝 <b>Заявки в тестировщики</b>\n\n"
        f"На рассмотрении: <b>{pending}</b> • всего: {total}\n"
        f"Приём заявок: {intake_s}\n\n"
        "Ссылка для канала (по ней юзеры подают заявку):\n"
        f"<code>{html.escape(deep_link)}</code>\n\n"
        "👥 <b>Беседа тестеров:</b> создай группу, добавь бота и сделай его админом "
        "(с правом приглашать), затем напиши в группе <code>/invite_testers</code> — "
        "бот разошлёт всем тестерам приглашение."
    )
    keyboard = [
        [InlineKeyboardButton(f"📋 Открытые ({pending})", callback_data="tapps_0_0")],
        [InlineKeyboardButton("📜 Все заявки", callback_data="tapps_0_1")],
        [InlineKeyboardButton(f"🔁 Приём заявок: {intake_s}", callback_data="tester_intake_toggle")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_TESTER_APPS


async def show_tester_apps_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Листинг заявок с пагинацией. callback: tapps_<offset>_<0=открытые|1=все>."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS

    offset, only_pending = 0, True
    data = query.data if query else "tapps_0_0"
    if data.startswith("tapps_"):
        try:
            _, off_s, f_s = data.split("_")
            offset = max(0, int(off_s))
            only_pending = (f_s == "0")
        except (ValueError, IndexError):
            offset, only_pending = 0, True

    total = await ta_count_applications(only_pending=only_pending)
    rows = await ta_list_applications(only_pending=only_pending, offset=offset, limit=_TAPPS_PAGE)

    title = "📋 <b>Открытые заявки</b>" if only_pending else "📜 <b>Все заявки</b>"
    lines = [title, f"Всего: <b>{total}</b>" + (f" • показаны {offset + 1}–{offset + len(rows)}" if rows else ""), ""]
    if not rows:
        lines.append("Заявок нет.")

    keyboard = []
    for r in rows:
        ts = r["created_at"].strftime("%d.%m %H:%M") if r.get("created_at") else "?"
        label = await _loophole_user_label(r["user_id"])
        mark = _TAPP_STATUS_MARK.get(r.get("status"), "•")
        keyboard.append([InlineKeyboardButton(
            f"{mark} {ts} • {label}",
            callback_data=f"tapp_{r['id']}",
        )])

    f = "0" if only_pending else "1"
    # Запоминаем текущий список (фильтр+страницу), чтобы кнопка «К списку» в карточке
    # возвращала именно сюда, а не всегда в «Открытые, стр.0».
    context.user_data["tapps_last"] = f"tapps_{offset}_{f}"
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"tapps_{max(0, offset - _TAPPS_PAGE)}_{f}"))
    if offset + _TAPPS_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"tapps_{offset + _TAPPS_PAGE}_{f}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(
        "📜 Показать все" if only_pending else "📋 Только открытые",
        callback_data=f"tapps_0_{1 if only_pending else 0}",
    )])
    keyboard.append([InlineKeyboardButton("◀️ К меню заявок", callback_data="manage_tester_apps")])

    markup = InlineKeyboardMarkup(keyboard)
    text = "\n".join(lines)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_TESTER_APPS


async def _render_app_detail(query, context: ContextTypes.DEFAULT_TYPE, app_id: int) -> int:
    """Карточка одной заявки: ответы анкеты + метрики активности юзера."""
    app = await ta_get_application(app_id)
    if not app:
        await query.answer("Заявка не найдена.", show_alert=True)
        return ADMIN_TESTER_APPS

    user_id = app["user_id"]
    label = html.escape(await _loophole_user_label(user_id))
    answers = app.get("answers") or {}
    st = app.get("status")
    st_h = _TAPP_STATUS_H.get(st, st or "?")
    ts = app["created_at"].strftime("%d.%m.%Y %H:%M") if app.get("created_at") else "?"

    answers_block = "\n\n".join(
        f"<b>{_TAPP_Q_LABELS.get(k, k)}:</b>\n{html.escape(str(answers.get(k) or '—'))}"
        for k in ("q1", "q2", "q3")
    )
    metrics = await ta_gather_metrics(user_id)
    metrics_block = ta_format_metrics(metrics)

    text = (
        f"📝 <b>Заявка #{app_id}</b> — {st_h}\n"
        f"👤 {label} (<code>{user_id}</code>)\n"
        f"🕒 подана: {ts}\n\n"
        f"{answers_block}\n\n"
        f"{metrics_block}"
    )
    # Ответы юзера произвольной длины + html.escape могут раздуть карточку за лимит
    # Telegram (4096). Обрезаем по безопасной границе, иначе карточку нельзя было бы
    # ни открыть, ни принять/отклонить — заявка зависала бы в pending.
    text = ta_clip_html(text)

    keyboard = []
    if st == "pending":
        keyboard.append([
            InlineKeyboardButton("✅ Принять", callback_data=f"tapp_ok_{app_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"tapp_no_{app_id}"),
        ])
    back_cb = context.user_data.get("tapps_last", "tapps_0_0")
    keyboard.append([InlineKeyboardButton("◀️ К списку", callback_data=back_cb)])
    markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        try:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            # Последний рубеж: без разметки, чтобы кнопки Принять/Отклонить всё равно дошли.
            await query.message.reply_text(text, reply_markup=markup)
    return ADMIN_TESTER_APPS


async def show_tester_app_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Открыть карточку заявки. callback: tapp_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    try:
        app_id = int(query.data.split("_")[1])
    except (ValueError, IndexError):
        await query.answer("Некорректная заявка.", show_alert=True)
        return ADMIN_TESTER_APPS
    return await _render_app_detail(query, context, app_id)


async def accept_tester_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принять заявку: выдать роль тестера + уведомить. callback: tapp_ok_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return ADMIN_TESTER_APPS

    row = await ta_set_status(app_id, "approved", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        return await _render_app_detail(query, context, app_id)

    user_id = row["user_id"]
    # Выдача роли — авторитетный шаг. Если она НЕ применилась (транзиентный сбой БД),
    # откатываем заявку обратно в pending и НЕ уведомляем юзера об одобрении — иначе
    # будет рассинхрон: статус approved при is_tester=false (юзер без доступа, заявку
    # уже не видно в «Открытые»).
    granted = False
    try:
        res = await db_manager.execute("UPDATE users SET is_tester = true WHERE user_id = $1", user_id)
        granted = isinstance(res, str) and res != "UPDATE 0"
    except Exception:
        logging.exception("[tester_apps] не удалось выдать роль тестера user_id=%s", user_id)

    if not granted:
        try:
            await db_manager.execute(
                "UPDATE tester_applications SET status='pending', reviewed_by=NULL, reviewed_at=NULL "
                "WHERE id=$1",
                app_id,
            )
        except Exception:
            logging.exception("[tester_apps] не удалось откатить заявку #%s в pending", app_id)
        await query.answer("Не удалось выдать роль — заявка возвращена в очередь, повтори.", show_alert=True)
        return await _render_app_detail(query, context, app_id)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("✅ <b>Твоя заявка в тестировщики одобрена!</b>\n\n"
                  "Открой /test, чтобы скачать оверлей и посмотреть инструкцию по входу. "
                  "Добро пожаловать в команду! 🎉"),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[tester_apps] не удалось уведомить user_id=%s об одобрении", user_id)

    return await _render_app_detail(query, context, app_id)


async def reject_tester_application(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отклонить заявку + уведомить (повторная подача разрешена). callback: tapp_no_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return ADMIN_TESTER_APPS

    row = await ta_set_status(app_id, "rejected", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        return await _render_app_detail(query, context, app_id)

    user_id = row["user_id"]
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("❌ <b>Твоя заявка в тестировщики отклонена.</b>\n\n"
                  "Спасибо за интерес! Ты можешь подать заявку снова позже."),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[tester_apps] не удалось уведомить user_id=%s об отклонении", user_id)

    return await _render_app_detail(query, context, app_id)


async def toggle_tester_intake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включить/выключить приём заявок. callback: tester_intake_toggle."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_TESTER_APPS
    cur = await ta_intake_enabled()
    await ta_set_intake_enabled(not cur)
    # manage_tester_applications сам ответит на callback и перерисует меню.
    return await manage_tester_applications(update, context)


# --- Заявки на ВИП за промокод (тумблер приёма + выгрузка открытых) ---
# Основной разбор — в канале заявок (config.VIP_APPLICATIONS_CHAT_ID), где у каждой
# заявки кнопки Одобрить/Отклонить (top-level хендлеры vip_ok_/vip_no_, работают и из
# канала). Этот раздел даёт тумблер приёма, ссылку и выгрузку открытых заявок в личку
# (фолбэк, если канал не настроен / карточку пропустили).

_VIP_DUMP_MAX = 10


async def manage_vip_applications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Меню заявок на ВИП: счётчики, deep-link, тумблер приёма, выгрузка открытых."""
    query = update.callback_query
    if query:
        await query.answer()
    if not await _loophole_admin_guard(update):
        if query:
            await query.answer("Доступ запрещён.", show_alert=True)
        return CHOOSING_ACTION

    pending = await vip_count_pending()
    total = await vip_count_applications(only_pending=False)
    intake = await vip_intake_enabled()
    intake_s = "🟢 ВКЛ" if intake else "🔴 ВЫКЛ"
    chan = await vip_get_channel() or config.VIP_APPLICATIONS_CHAT_ID
    if chan:
        chan_s = f"<code>{chan}</code> ✅"
    else:
        chan_s = "❗ не задан — заявки идут в личку супер-админа"
    try:
        me = await context.bot.get_me()
        deep_link = f"https://t.me/{me.username}?start=apply_vip"
    except Exception:
        deep_link = "https://t.me/your_bot?start=apply_vip"

    text = (
        "💎 <b>Заявки на ВИП за промокод</b>\n\n"
        f"На рассмотрении: <b>{pending}</b> • всего: {total}\n"
        f"Приём заявок: {intake_s}\n"
        f"Чат заявок: {chan_s}\n\n"
        "📍 <b>Куда летят заявки:</b> создай группу, добавь туда бота и сделай его "
        "администратором, затем напиши В ТОЙ ГРУППЕ команду <code>/set_vip_channel</code> — "
        "бот запомнит её и будет слать заявки туда (с кнопками Одобрить/Отклонить).\n\n"
        "Ссылка для рассылки (по ней юзеры подают заявку):\n"
        f"<code>{html.escape(deep_link)}</code>"
    )
    keyboard = [
        [InlineKeyboardButton(f"📤 Открытые в личку ({pending})", callback_data="vapps_dump")],
        [InlineKeyboardButton(f"🔁 Приём заявок: {intake_s}", callback_data="vip_intake_toggle")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    if query:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_VIP_APPS


async def vip_apps_dump(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выгрузить открытые заявки (пруфы + карточка с кнопками) в чат админа."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_VIP_APPS
    apps = await vip_list_applications(only_pending=True, offset=0, limit=_VIP_DUMP_MAX)
    if not apps:
        await query.answer("Открытых заявок нет.", show_alert=True)
        return ADMIN_VIP_APPS
    from user_handlers.vip_application_flow import post_application_card
    chat_id = update.effective_chat.id
    sent = 0
    for app in apps:
        try:
            await post_application_card(context, app, chat_id)
            sent += 1
        except Exception:
            logging.exception("[vip_apps] не удалось выгрузить заявку #%s в личку", app.get("id"))
    total_pending = await vip_count_pending()
    tail = f"\n\n(показано {sent} из {total_pending} открытых; листай вверх)" if total_pending > sent else ""
    await context.bot.send_message(
        chat_id, f"📤 Открытые заявки на ВИП выгружены.{tail}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К меню заявок", callback_data="manage_vip_apps")]]),
    )
    return ADMIN_VIP_APPS


async def toggle_vip_intake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Включить/выключить приём заявок на ВИП. callback: vip_intake_toggle."""
    query = update.callback_query
    if not await _loophole_admin_guard(update):
        await query.answer("Доступ запрещён.", show_alert=True)
        return ADMIN_VIP_APPS
    cur = await vip_intake_enabled()
    await vip_set_intake_enabled(not cur)
    return await manage_vip_applications(update, context)


async def set_vip_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_vip_channel — выполнять В ГРУППЕ/КАНАЛЕ, куда добавлен бот. Запоминает этот чат как
    приёмник заявок на ВИП (туда летят пруфы + карточка с кнопками Одобрить/Отклонить)."""
    chat = update.effective_chat
    msg = update.effective_message
    if not await _loophole_admin_guard(update):
        if msg:
            await msg.reply_text("🚫 Назначить чат заявок может только администратор бота.")
        return
    if chat is None or chat.type not in ("group", "supergroup", "channel"):
        if msg:
            await msg.reply_text(
                "Эту команду нужно отправить В ГРУППЕ или КАНАЛЕ, куда добавлен бот.\n"
                "Создай группу для заявок на ВИП, добавь туда бота (сделай админом), затем "
                "напиши там <code>/set_vip_channel</code>.",
                parse_mode="HTML",
            )
        return
    await vip_set_channel(chat.id)
    title = chat.title or str(chat.id)
    logging.info("[vip_apps] чат заявок установлен: %s (%s) админом %s", chat.id, title, update.effective_user.id if update.effective_user else "?")
    if msg:
        await msg.reply_text(
            f"✅ Готово! Заявки на ВИП теперь будут падать сюда: <b>{html.escape(title)}</b> "
            f"(id <code>{chat.id}</code>).\nКаждая заявка придёт с пруфами и кнопками "
            "«Одобрить»/«Отклонить».",
            parse_mode="HTML",
        )


def _admin_label(user) -> str:
    if user is None:
        return "админом"
    if user.username:
        return f"@{html.escape(user.username)}"
    return html.escape(user.full_name or str(user.id))


async def _vip_mark_card(query, suffix_html: str) -> None:
    """Дописать в карточку статус и убрать кнопки (работает в канале и в личке)."""
    base = query.message.text_html if getattr(query.message, "text_html", None) else (query.message.text or "💎 Заявка на ВИП")
    new_text = base + suffix_html
    try:
        await query.edit_message_text(new_text, parse_mode="HTML")
    except Exception:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


async def accept_vip_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Одобрить заявку на ВИП: выдать users.is_unlimited (безлимит + оверлей) + уведомить.
    TOP-LEVEL хендлер — жмётся из канала заявок. callback: vip_ok_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Одобрять может только администратор.", show_alert=True)
        return
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return

    row = await vip_set_status(app_id, "approved", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        await _vip_mark_card(query, "")
        return
    user_id = row["user_id"]
    # Выдача безлимита — авторитетный шаг. При сбое откатываем заявку в pending.
    granted = False
    try:
        res = await db_manager.execute("UPDATE users SET is_unlimited = true WHERE user_id = $1", user_id)
        granted = isinstance(res, str) and res != "UPDATE 0"
    except Exception:
        logging.exception("[vip_apps] не удалось выдать is_unlimited user_id=%s", user_id)
    if not granted:
        try:
            await db_manager.execute(
                "UPDATE vip_applications SET status='pending', reviewed_by=NULL, reviewed_at=NULL WHERE id=$1",
                app_id,
            )
        except Exception:
            logging.exception("[vip_apps] не удалось откатить заявку #%s в pending", app_id)
        await query.answer("Не удалось выдать статус — заявка возвращена в очередь, повтори.", show_alert=True)
        return

    await _vip_mark_card(query, f"\n\n✅ <b>Одобрено</b> {_admin_label(update.effective_user)}")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("💎 <b>Заявка на ВИП одобрена!</b>\n\n"
                  "Тебе выдан <b>безлимит</b> на все функции бота. Спасибо, что с нами! 🎉"),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[vip_apps] не удалось уведомить user_id=%s об одобрении", user_id)


async def reject_vip_application(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отклонить заявку на ВИП + уведомить (повторная подача разрешена).
    TOP-LEVEL хендлер. callback: vip_no_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Отклонять может только администратор.", show_alert=True)
        return
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return
    row = await vip_set_status(app_id, "rejected", update.effective_user.id)
    if not row:
        await query.answer("Заявка уже обработана.", show_alert=True)
        await _vip_mark_card(query, "")
        return
    user_id = row["user_id"]
    await _vip_mark_card(query, f"\n\n❌ <b>Отклонено</b> {_admin_label(update.effective_user)}")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("❌ <b>Твоя заявка на ВИП отклонена.</b>\n\n"
                  "Если считаешь это ошибкой или появились новые пруфы — можешь подать заявку снова."),
            parse_mode="HTML",
        )
    except Exception:
        logging.warning("[vip_apps] не удалось уведомить user_id=%s об отклонении", user_id)


# --- ВИП-заявка: отправить на доработку (запросить доп. инфо) ---
# Админ жмёт «📝 На доработку» в канале → бот просит ответить на сообщение текстом (что
# доработать) → текст уходит заявителю, заявка → 'needs_info', юзер может подать заново.
_VIP_REV_SENTINEL = "✏️ Доработка заявки на ВИП №"


class _VipRevReplyFilter(filters.MessageFilter):
    """Матчит ТОЛЬКО ответ на сообщение-запрос доработки (по сентинелу) — чтобы глобальный
    хендлер не ловил посторонние реплаи."""
    def filter(self, message) -> bool:
        r = getattr(message, "reply_to_message", None)
        return bool(r and (r.text or "").startswith(_VIP_REV_SENTINEL))


vip_rev_reply_filter = _VipRevReplyFilter()


def _parse_vip_rev_id(text):
    try:
        tail = text.split("№", 1)[1]
        digits = ""
        for ch in tail:
            if ch.isdigit():
                digits += ch
            else:
                break
        return int(digits) if digits else None
    except Exception:
        return None


async def request_vip_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запросить доработку заявки. TOP-LEVEL (из канала). callback: vip_info_<id>."""
    query = update.callback_query
    await query.answer()
    if not await _loophole_admin_guard(update):
        await query.answer("Запросить доработку может только администратор.", show_alert=True)
        return
    try:
        app_id = int(query.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        return
    app = await vip_get_application(app_id)
    if not app or app.get("status") != "pending":
        await query.answer("Заявка уже обработана.", show_alert=True)
        return
    # Просим админа ответить на это сообщение текстом — что доработать.
    await query.message.reply_text(
        f"{_VIP_REV_SENTINEL}{app_id}\n\n"
        "Ответь на ЭТО сообщение текстом — что нужно доработать (каких пруфов не хватает и т.п.). "
        "Текст уйдёт заявителю, а заявка вернётся ему на доработку.",
        parse_mode="HTML",
    )


async def handle_vip_revision_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ ответил на запрос доработки текстом → шлём заявителю, заявка → needs_info."""
    msg = update.effective_message
    if not await _loophole_admin_guard(update):
        return
    app_id = _parse_vip_rev_id(msg.reply_to_message.text or "")
    if not app_id:
        return
    comment = (msg.text or "").strip()
    if not comment:
        await msg.reply_text("Напиши текстом, что доработать.")
        return
    row = await vip_set_status(app_id, "needs_info", update.effective_user.id)
    if not row:
        await msg.reply_text("Эта заявка уже обработана (одобрена/отклонена/отправлена).")
        return
    user_id = row["user_id"]
    # Уведомляем заявителя.
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=("📝 <b>Твою заявку на ВИП вернули на доработку.</b>\n\n"
                  f"<b>Что нужно:</b> {html.escape(comment)}\n\n"
                  "Подай заявку заново с учётом замечаний — кнопка «💎 Получить ВИП за промокод» "
                  "в главном меню."),
            parse_mode="HTML",
        )
        notified = True
    except Exception:
        notified = False
        logging.warning("[vip_apps] не удалось уведомить user_id=%s о доработке", user_id)
    # Снимаем кнопки с исходной карточки (чтобы её не обработали повторно).
    app = await vip_get_application(app_id)
    if app and app.get("channel_chat_id") and app.get("channel_message_id"):
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=app["channel_chat_id"], message_id=app["channel_message_id"], reply_markup=None
            )
        except Exception:
            pass
    await msg.reply_text(
        f"📝 Заявка #{app_id} отправлена заявителю на доработку"
        + ("." if notified else " (но не удалось доставить ему сообщение — возможно, он не запускал бота).")
    )


# --- Горячая смена сервис-аккаунта Vertex/Gemini (только суперадмин) ---

async def sa_change_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Суперадмин: запросить .json сервис-аккаунта."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Только для супер-админа.", show_alert=True)
        return CHOOSING_ACTION
    text = (
        "🔑 <b>Смена сервис-аккаунта Vertex/Gemini</b>\n\n"
        "Пришли сюда <b>.json-файл</b> сервис-аккаунта (можно из нового GCP-проекта — ради свежей "
        "квоты Gemini). Я проверю его, заменю активный ключ и СРАЗУ переинициализирую и бота, и оверлей (API).\n\n"
        "⚠️ Если проект новый — доступа к GCS-бакету старого проекта (botlaw2) у него не будет "
        "(это про квоту Gemini, не про хранилище законов). Старый ключ сохраню в .bak."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]])
    await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    return ADMIN_SA_UPLOAD


async def sa_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принять .json, заменить активный SA и переинициализировать бота + оверлей (API)."""
    if update.effective_user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("Только для супер-админа.")
        return ConversationHandler.END

    doc = update.message.document
    if not doc:
        await update.message.reply_text("Пришли именно .json-файл сервис-аккаунта.")
        return ADMIN_SA_UPLOAD
    if doc.file_size and doc.file_size > 100_000:
        await update.message.reply_text("Слишком большой файл для сервис-аккаунта (>100 КБ). Это точно SA-ключ?")
        return ADMIN_SA_UPLOAD

    try:
        tg_file = await doc.get_file()
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Не удалось скачать файл: {e}")
        return ADMIN_SA_UPLOAD

    from services.sa_admin import swap_service_account, reload_local_vertex, trigger_api_reload
    ok, msg, info = swap_service_account(data)
    # Гигиена: убрать сообщение с приватным ключом из чата (best-effort).
    try:
        await update.message.delete()
    except Exception:
        pass
    if not ok:
        await update.effective_chat.send_message(f"❌ {msg}")
        return ADMIN_SA_UPLOAD

    ok_bot, bot_msg = reload_local_vertex()           # этот процесс (бот)
    ok_api, api_msg = await trigger_api_reload()       # процесс API (оверлей)

    proj = (info or {}).get("project_id")
    email = (info or {}).get("client_email")
    lines = [
        "✅ <b>Сервис-аккаунт заменён.</b>",
        f"Проект: <code>{proj}</code>",
        f"SA: <code>{email}</code>",
        f"Бот: {'🟢 переинициализирован (' + bot_msg + ')' if ok_bot else '🔴 ' + bot_msg}",
        f"Оверлей (API): {'🟢 ' + api_msg if ok_api else '🔴 ' + api_msg}",
    ]
    if not ok_api:
        lines.append("<i>API подхватит ключ при следующем рестарте; проверь логи API.</i>")
    lines.append("\nЕсли проект новый — перенеси законку в новый бакет (старый недоступен):")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Перенести законку в новый бакет", callback_data="sa_bucket")],
        [InlineKeyboardButton("◀️ В админку", callback_data="admin_panel")],
    ])
    await update.effective_chat.send_message("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    return CHOOSING_ACTION


# --- Перенос законки в новый бакет (после смены проекта) ---

async def sa_bucket_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Только для супер-админа.", show_alert=True)
        return CHOOSING_ACTION
    await query.edit_message_text(
        "📦 <b>Перенос законки в новый бакет</b>\n\n"
        "Пришли <b>имя нового бакета</b> (он уже должен быть создан в новом проекте, и текущий "
        "сервис-аккаунт должен иметь к нему доступ на запись).\n"
        "Бот перезальёт туда все законы и прецеденты <b>из БД</b> и обновит ссылки.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]),
        parse_mode="HTML",
    )
    return ADMIN_SA_BUCKET


async def sa_bucket_set(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != SUPER_ADMIN_ID:
        await update.message.reply_text("Только для супер-админа.")
        return ConversationHandler.END
    raw = (update.message.text or "").strip()
    name = raw.replace("gs://", "").strip("/").split("/")[0].strip()
    if not name:
        await update.message.reply_text("Пришли имя бакета одной строкой (напр. botlaw3).")
        return ADMIN_SA_BUCKET
    from services.sa_admin import set_active_bucket, trigger_api_reload
    await set_active_bucket(name)
    ok_api, api_msg = await trigger_api_reload()   # пробросить новый бакет в API
    text = (
        f"Активный бакет: <code>{name}</code>\n"
        f"Оверлей (API): {'🟢 ' + api_msg if ok_api else '🔴 ' + api_msg}\n\n"
        "Запустить перенос законки из БД в этот бакет?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Только включённые сервера", callback_data="sa_mig_enabled")],
        [InlineKeyboardButton("▶️ Все (вкл. отключённые)", callback_data="sa_mig_all")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")],
    ])
    await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")
    return ADMIN_SA_BUCKET


async def _run_sa_migration(application, chat_id: int, enabled_only: bool) -> None:
    from services.sa_admin import migrate_corpus_to_active_bucket
    import config as _cfg
    bot = application.bot
    try:
        status = await bot.send_message(
            chat_id=chat_id,
            text=f"📦 Переношу законку в <code>{_cfg.active_gcs_bucket()}</code>… старт",
            parse_mode="HTML",
        )
    except Exception:
        status = None

    async def progress(stats, i, total):
        if not status:
            return
        pct = int(i * 100 / max(total, 1))
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=status.message_id,
                text=f"📦 Перенос: {i}/{total} ({pct}%) • залито законов: {stats['laws_done']}",
                parse_mode="HTML",
            )
        except Exception:
            pass

    try:
        stats = await migrate_corpus_to_active_bucket(enabled_only=enabled_only, progress_cb=progress)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>Перенос завершён</b> → <code>{stats['bucket']}</code>\n"
                f"Законов залито: <b>{stats['laws_done']}</b> (пропущено отключённых: {stats['laws_skipped']})\n"
                f"Прецедентов: <b>{stats['precedents_done']}</b>\n"
                f"Ошибок: <b>{stats['errors']}</b>\n\n"
                "Ссылки в БД обновлены — Q&A/анализ работают по новому бакету."
            ),
            parse_mode="HTML",
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("[ADMIN] миграция законки в бакет упала")
        await bot.send_message(chat_id=chat_id, text=f"❌ Перенос упал: {e}")


async def sa_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != SUPER_ADMIN_ID:
        await query.answer("Только для супер-админа.", show_alert=True)
        return ADMIN_SA_BUCKET
    enabled_only = (query.data == "sa_mig_enabled")
    chat_id = query.message.chat_id
    context.application.create_task(_run_sa_migration(context.application, chat_id, enabled_only))
    await query.edit_message_text(
        "📦 Запустил перенос в фоне — пришлю прогресс и итог сюда.\n"
        "<i>Пока идёт перенос, часть ответов может временно не находить законы.</i>",
        parse_mode="HTML",
    )
    return CHOOSING_ACTION


async def _render_job_detail(query, job_id: int) -> int:
    job = await lq_get_job(job_id)
    if not job:
        await query.answer("Задача не найдена.", show_alert=True)
        try:
            await query.edit_message_text(
                "Задача не найдена.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ К очереди", callback_data="manage_loophole_queue")]]),
            )
        except Exception:
            pass
        return ADMIN_LOOPHOLE_QUEUE

    label = await _loophole_user_label(job['user_id'])
    pos = await lq_position(job_id) if job['status'] == 'queued' else 0
    p = job.get('params') or {}
    lines = [
        f"🕳 <b>Задача #{job_id}</b>",
        f"Пользователь: {label} (<code>{job['user_id']}</code>)",
        f"Приоритет: {_loophole_priority_label(job['priority'])} ({job['priority']})",
        f"Статус: <b>{job['status']}</b>" + (f", позиция {pos}" if pos else ""),
        f"Сервер: {p.get('server_id', '—')}",
        f"Тип: {p.get('analysis_type', '—')} | источник: {p.get('mode', '—')} | профиль: {p.get('profile', '—')}",
    ]
    keyboard = []
    if job['status'] == 'queued':
        keyboard.append([InlineKeyboardButton("⬆️ В начало", callback_data=f"loophole_jtop_{job_id}")])
        keyboard.append([
            InlineKeyboardButton("🔼 Выше", callback_data=f"loophole_jup_{job_id}"),
            InlineKeyboardButton("🔽 Ниже", callback_data=f"loophole_jdown_{job_id}"),
        ])
        keyboard.append([InlineKeyboardButton("🔢 Задать позицию", callback_data=f"loophole_jsetpos_{job_id}")])
        keyboard.append([InlineKeyboardButton("⚡ Запустить без очереди", callback_data=f"loophole_joob_{job_id}")])
        keyboard.append([InlineKeyboardButton("🚫 Отменить", callback_data=f"loophole_jcancel_{job_id}")])
    elif job['status'] == 'running':
        lines.append("\n⚙️ Задача выполняется — изменить/отменить нельзя (без прерывания).")
    keyboard.append([InlineKeyboardButton("◀️ К очереди", callback_data="manage_loophole_queue")])

    try:
        await query.edit_message_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
        )
    except Exception:
        pass
    return ADMIN_LOOPHOLE_JOB_DETAIL


async def loophole_job_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    return await _render_job_detail(query, _loophole_job_id(query))


async def loophole_job_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    await lq_move_top(job_id)
    _wake_loophole(context)
    await query.answer("Поднято в начало очереди.")
    return await _render_job_detail(query, job_id)


async def loophole_job_up(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    cur = await lq_current_pos(job_id)
    if cur > 1:
        await lq_set_position(job_id, cur - 1)
        _wake_loophole(context)
        await query.answer("Перемещено выше.")
    else:
        await query.answer("Уже в начале очереди.")
    return await _render_job_detail(query, job_id)


async def loophole_job_down(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    cur = await lq_current_pos(job_id)
    if cur >= 1:
        await lq_set_position(job_id, cur + 1)
        _wake_loophole(context)
        await query.answer("Перемещено ниже.")
    else:
        await query.answer("Задачи нет в очереди.")
    return await _render_job_detail(query, job_id)


async def loophole_job_setpos_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    job_id = _loophole_job_id(query)
    context.user_data['loophole_setpos_job'] = job_id
    await query.edit_message_text(
        f"🔢 Введите новую позицию для задачи #{job_id} (целое число от 1).",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Отмена", callback_data="manage_loophole_queue")]]),
    )
    return ADMIN_LOOPHOLE_SET_POSITION


async def loophole_job_set_position_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    job_id = context.user_data.get('loophole_setpos_job')
    txt = (update.message.text or "").strip()
    if not job_id or not txt.isdigit() or int(txt) < 1:
        await update.message.reply_text("Нужно положительное число. Попробуйте снова.")
        return ADMIN_LOOPHOLE_SET_POSITION
    await lq_set_position(int(job_id), int(txt))
    _wake_loophole(context)
    context.user_data.pop('loophole_setpos_job', None)
    await update.message.reply_text(f"✅ Позиция задачи #{job_id} обновлена.")
    return await manage_loophole_queue(update, context)


async def loophole_job_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    cancelled = await lq_cancel(job_id)
    if cancelled:
        await query.answer("Задача отменена.")
        try:
            await context.bot.send_message(
                chat_id=cancelled['chat_id'],
                text="⚠ Ваш запрос на анализ был отменён администратором.",
            )
        except Exception:
            pass
        return await manage_loophole_queue(update, context)
    await query.answer("Нельзя отменить (уже выполняется или завершена).", show_alert=True)
    return await _render_job_detail(query, job_id)


async def loophole_job_oob(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    job_id = _loophole_job_id(query)
    job = await lq_get_job(job_id)
    if not job or job['status'] != 'queued':
        await query.answer("Задачу нельзя запустить (не в очереди).", show_alert=True)
        return await _render_job_detail(query, job_id)
    # Отменяем исходную queued-задачу и запускаем её копию параллельно, без очереди.
    await lq_cancel(job_id)
    await lq_run_oob(context.application, job['params'], job['user_id'], job['chat_id'])
    _wake_loophole(context)
    await query.answer("Запущено параллельно, без очереди.")
    try:
        await context.bot.send_message(
            chat_id=job['chat_id'],
            text="⚡ Ваш анализ запущен администратором вне очереди — отчёт придёт в этот чат.",
        )
    except Exception:
        pass
    return await manage_loophole_queue(update, context)


admin_conv_handler = ConversationHandler(
    entry_points=[CommandHandler('admin', admin_panel)],
    states={
        CHOOSING_ACTION: [
            CallbackQueryHandler(manage_bans_menu, pattern='^manage_bans$'),
            CallbackQueryHandler(manage_hwid_menu, pattern='^manage_hwid$'),
            CallbackQueryHandler(manage_vips_menu, pattern='^manage_vips$'),
            CallbackQueryHandler(manage_testers_menu, pattern='^manage_testers$'),
            CallbackQueryHandler(manage_tester_applications, pattern='^manage_tester_apps$'),
            CallbackQueryHandler(manage_vip_applications, pattern='^manage_vip_apps$'),
            CallbackQueryHandler(manage_support_tickets, pattern='^manage_support$'),
            CallbackQueryHandler(support_dump, pattern='^supp_dump$'),
            CallbackQueryHandler(manage_admins_menu, pattern='^manage_admins$'),
            CallbackQueryHandler(manage_servers_menu, pattern='^manage_servers$'),
            CallbackQueryHandler(manage_precedents_menu, pattern='^manage_precedents$'),
            CallbackQueryHandler(manage_limits_menu, pattern='^manage_limits$'),
            CallbackQueryHandler(start_broadcast, pattern='^broadcast$'),
            CallbackQueryHandler(main_menu, pattern='^main_menu$'),
            CallbackQueryHandler(manage_reports_menu, pattern='^manage_reports$'),
            CallbackQueryHandler(manage_loophole_queue, pattern='^manage_loophole_queue$'),
            CallbackQueryHandler(show_api_monitoring, pattern='^api_monitoring$'),
            CallbackQueryHandler(reset_api_stats, pattern='^api_reset_stats$'),
            CallbackQueryHandler(cleanup_gdrive_storage, pattern='^api_cleanup_gdrive$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel_from_monitoring$'),
            CallbackQueryHandler(toggle_maintenance_mode, pattern='^toggle_maintenance$'),
            CallbackQueryHandler(start_user_search, pattern='^user_search$'),
            CallbackQueryHandler(prompt_view_case_by_id, pattern='^view_case_by_id$'),
            CallbackQueryHandler(show_admin_case_history, pattern=r'^admin_case_history_\d+$'),
            CallbackQueryHandler(admin_close_case, pattern=r'^admin_close_(assist|ccw)_\d+$'),
            # REST API управление
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
            # Промо-акции
            CallbackQueryHandler(manage_promo_menu, pattern='^manage_promo$'),
            # Смена сервис-аккаунта (суперадмин)
            CallbackQueryHandler(sa_change_prompt, pattern='^sa_change$'),
            CallbackQueryHandler(sa_bucket_prompt, pattern='^sa_bucket$'),
        ],
        ADMIN_SA_UPLOAD: [
            MessageHandler(filters.Document.ALL, sa_receive_file),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        ADMIN_SA_BUCKET: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, sa_bucket_set),
            CallbackQueryHandler(sa_migrate, pattern='^sa_mig_(enabled|all)$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        MANAGE_BANS: [
            CallbackQueryHandler(pre_ban_user, pattern='^ban_user$'),
            CallbackQueryHandler(pre_unban_user, pattern='^unban_user$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        MANAGE_VIPS: [
            CallbackQueryHandler(pre_add_vip, pattern='^add_vip$'),
            CallbackQueryHandler(pre_remove_vip, pattern='^remove_vip$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        MANAGE_TESTERS: [
            CallbackQueryHandler(pre_add_tester, pattern='^add_tester$'),
            CallbackQueryHandler(pre_remove_tester, pattern='^remove_tester$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        MANAGE_ADMINS: [
            CallbackQueryHandler(pre_add_admin, pattern='^add_admin$'),
            CallbackQueryHandler(pre_remove_admin, pattern='^remove_admin$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        MANAGE_SERVERS: [
            CallbackQueryHandler(choose_server_to_toggle, pattern='^toggle_server_list$'),
            CallbackQueryHandler(choose_server_for_update, pattern='^update_kb_list$'),
            CallbackQueryHandler(choose_server_for_force_update, pattern='^force_update_kb_list$'),
            CallbackQueryHandler(run_kb_update_all, pattern='^update_all_servers$'),
            CallbackQueryHandler(run_kb_force_update_all, pattern='^force_update_all_servers$'),
            CallbackQueryHandler(sync_precedents_handler, pattern='^sync_precedents$'),
            CallbackQueryHandler(choose_server_for_force_precedents, pattern='^force_sync_precedents_list$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        GET_ID_FOR_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        GET_ID_FOR_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        MANAGE_HWID: [
            CallbackQueryHandler(pre_hwid_reset, pattern='^hwid_reset$'),
            CallbackQueryHandler(pre_hwid_view, pattern='^hwid_view$'),
            CallbackQueryHandler(pre_hwid_ban, pattern='^hwid_ban$'),
            CallbackQueryHandler(pre_hwid_unban, pattern='^hwid_unban$'),
            # Универсальный ключ laws-билда (yes-паттерн ПЕРЕД общим revoke)
            CallbackQueryHandler(unikey_menu, pattern='^unikey_menu$'),
            CallbackQueryHandler(unikey_create, pattern='^unikey_create$'),
            CallbackQueryHandler(unikey_revoke_confirm, pattern=r'^unikey_revoke_yes_\d+$'),
            CallbackQueryHandler(unikey_revoke_prompt, pattern=r'^unikey_revoke_\d+$'),
            CallbackQueryHandler(manage_hwid_menu, pattern='^manage_hwid$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        GET_ID_FOR_HWID_RESET: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_hwid_input)],
        GET_ID_FOR_HWID_VIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_hwid_input)],
        GET_HWID_FOR_BAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_hwid_input)],
        GET_HWID_FOR_UNBAN: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_hwid_input)],
        GET_ID_FOR_VIP_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        GET_ID_FOR_VIP_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        GET_ID_FOR_ADMIN_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        GET_ID_FOR_ADMIN_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        GET_ID_FOR_TESTER_ADD: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        GET_ID_FOR_TESTER_REMOVE: [MessageHandler(filters.TEXT & ~filters.COMMAND, modify_user_list)],
        CHOOSE_SERVER_TO_TOGGLE: [
            CallbackQueryHandler(toggle_server_status, pattern=r'^toggle_'),
            CallbackQueryHandler(manage_servers_menu, pattern='^manage_servers$'),
        ],
        CHOOSE_SERVER_FOR_UPDATE: [
            CallbackQueryHandler(run_kb_update, pattern=r'^update_'),
            CallbackQueryHandler(manage_servers_menu, pattern='^manage_servers$'),
        ],
        CHOOSE_SERVER_FOR_FORCE_UPDATE: [
            CallbackQueryHandler(run_kb_force_update, pattern=r'^force_update_'),
            CallbackQueryHandler(manage_servers_menu, pattern='^manage_servers$'),
        ],
        BROADCAST_CHOOSE_AUDIENCE: [
            CallbackQueryHandler(choose_broadcast_audience, pattern=r'^bc_audience_'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        BROADCAST_AWAITING_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, receive_broadcast_message)],
        BROADCAST_ATTACH_BUTTONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast_buttons)],
        BROADCAST_CONFIRM: [
            CallbackQueryHandler(confirm_and_start_broadcast, pattern='^bc_confirm_send$'),
            CallbackQueryHandler(handle_broadcast_buttons_prompt, pattern='^bc_add_buttons$'),
            CallbackQueryHandler(toggle_broadcast_pin, pattern='^bc_toggle_pin$'),
            CallbackQueryHandler(change_broadcast_message, pattern='^bc_change_message$'),
            CallbackQueryHandler(cancel_broadcast, pattern='^bc_cancel$'),
        ],
        MANAGE_LIMITS: [
            CallbackQueryHandler(pre_reset_user_limits, pattern='^reset_limits_user$'),
            CallbackQueryHandler(confirm_reset_all_limits, pattern='^reset_limits_all_confirm$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        GET_ID_FOR_LIMIT_RESET: [MessageHandler(filters.TEXT & ~filters.COMMAND, reset_user_limits)],
        CONFIRM_RESET_ALL_LIMITS: [
            CallbackQueryHandler(execute_reset_all_limits, pattern='^reset_limits_all_execute$'),
            CallbackQueryHandler(manage_limits_menu, pattern='^manage_limits$'),
        ],
        ADMIN_MANAGE_REPORTS: [
            CallbackQueryHandler(view_reports_list, pattern=r'^view_reports_(open|closed)$'),
            CallbackQueryHandler(confirm_close_all_reports, pattern='^close_all_reports_confirm$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        ADMIN_VIEWING_REPORTS_LIST: [
            CallbackQueryHandler(navigate_reports_page, pattern=r'^reports_page_'),
            CallbackQueryHandler(view_report_conversation_admin, pattern=r'^admin_open_report_'),
            CallbackQueryHandler(manage_reports_menu, pattern='^manage_reports$'),
        ],
        ADMIN_CONFIRM_CLOSE_ALL_REPORTS: [
            CallbackQueryHandler(execute_close_all_reports, pattern='^close_all_reports_execute$'),
            CallbackQueryHandler(manage_reports_menu, pattern='^manage_reports$'),
        ],
        # Состояния для управления прецедентами
        ADMIN_MANAGE_PRECEDENTS: [
            CallbackQueryHandler(view_server_precedents, pattern=r'^prec_server_'),
            CallbackQueryHandler(sync_precedents_handler, pattern='^sync_precedents$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        ADMIN_VIEWING_PRECEDENTS_LIST: [
            CallbackQueryHandler(view_precedent_detail, pattern=r'^prec_view_\d+$'),
            CallbackQueryHandler(precedents_change_page, pattern=r'^prec_page_\d+$'),
            CallbackQueryHandler(manage_precedents_menu, pattern='^manage_precedents$'),
            CallbackQueryHandler(view_server_precedents, pattern=r'^prec_server_'),
        ],
        ADMIN_VIEWING_PRECEDENT_DETAIL: [
            CallbackQueryHandler(view_precedent_full_content, pattern=r'^prec_full_\d+$'),
            CallbackQueryHandler(view_precedent_images, pattern=r'^prec_images_\d+$'),
            CallbackQueryHandler(view_precedent_detail, pattern=r'^prec_view_\d+$'),
            CallbackQueryHandler(view_server_precedents, pattern=r'^prec_server_'),
            CallbackQueryHandler(manage_precedents_menu, pattern='^manage_precedents$'),
        ],
        # Состояние для принудительного обновления прецедентов
        ADMIN_CHOOSE_SERVER_FORCE_PRECEDENTS: [
            CallbackQueryHandler(run_force_precedents_update, pattern=r'^force_prec_'),
            CallbackQueryHandler(manage_servers_menu, pattern='^manage_servers$'),
        ],
        ADMIN_CONVERSATION_IN_REPORT: [
            CallbackQueryHandler(prompt_admin_for_reply, pattern=r'^admin_reply_report_'),
            CallbackQueryHandler(close_report, pattern=r'^admin_close_report_'),
            CallbackQueryHandler(view_reports_list, pattern=r'^view_reports_(open|closed)$'),
        ],
        AWAITING_ADMIN_REPLY_IN_REPORT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_reply_in_report),
            CallbackQueryHandler(view_report_conversation_admin, pattern=r'^admin_open_report_'),
        ],
        # Состояния для поиска пользователей
        USER_SEARCH_INPUT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, execute_user_search),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        USER_SEARCH_RESULTS: [
            CallbackQueryHandler(show_user_details, pattern=r'^user_details_\d+$'),
            CallbackQueryHandler(search_change_page, pattern=r'^search_(prev|next)_page$'),
            CallbackQueryHandler(start_user_search, pattern='^user_search$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        USER_DETAILS_VIEW: [
            CallbackQueryHandler(quick_user_action, pattern=r'^quick_(ban|unban|add_vip|remove_vip|add_unlim|remove_unlim|add_tester|remove_tester)_\d+$'),
            CallbackQueryHandler(back_to_search_results, pattern='^back_to_search_results$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        # Состояние для просмотра дела по ID
        ADMIN_VIEW_CASE_BY_ID: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, view_case_by_id),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        # === Состояния для управления REST API ===
        MANAGE_API_KEYS: [
            CallbackQueryHandler(api_start_server, pattern='^api_start_server$'),
            CallbackQueryHandler(api_stop_server, pattern='^api_stop_server$'),
            CallbackQueryHandler(api_server_status, pattern='^api_server_status$'),
            CallbackQueryHandler(api_create_key_start, pattern='^api_create_key$'),
            CallbackQueryHandler(api_list_keys, pattern='^api_list_keys$'),
            CallbackQueryHandler(api_stats, pattern='^api_stats$'),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        GET_API_KEY_TYPE: [
            CallbackQueryHandler(api_key_select_type, pattern='^api_key_type_(web|app)$'),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
        ],
        GET_API_KEY_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, api_key_receive_name),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
        ],
        GET_API_KEY_ORIGINS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, api_key_receive_origins),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
        ],
        GET_API_KEY_IPS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, api_key_receive_ips),
            CallbackQueryHandler(api_key_skip_ips, pattern='^api_key_skip_ips$'),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
        ],
        VIEW_API_KEYS_LIST: [
            CallbackQueryHandler(api_key_details, pattern=r'^api_key_details_\d+$'),
            CallbackQueryHandler(api_keys_change_page, pattern=r'^api_keys_(prev|next)_page$'),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
        ],
        VIEW_API_KEY_DETAILS: [
            CallbackQueryHandler(api_key_toggle, pattern=r'^api_key_(enable|disable)_\d+$'),
            CallbackQueryHandler(api_key_rpm_menu, pattern=r'^api_key_rpm_\d+$'),
            CallbackQueryHandler(api_key_set_rpm, pattern=r'^api_key_set_rpm_\d+_\d+$'),
            CallbackQueryHandler(api_key_rpm_custom_prompt, pattern=r'^api_key_rpm_custom_\d+$'),
            CallbackQueryHandler(api_key_rotate_confirm, pattern=r'^api_key_rotate_\d+$'),
            CallbackQueryHandler(api_key_delete_confirm, pattern=r'^api_key_delete_\d+$'),
            CallbackQueryHandler(api_key_details, pattern=r'^api_key_details_\d+$'),
            CallbackQueryHandler(api_list_keys, pattern='^api_list_keys$'),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
        ],
        GET_API_KEY_RPM: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, api_key_rpm_custom_receive),
            CallbackQueryHandler(api_key_details, pattern=r'^api_key_details_\d+$'),
            CallbackQueryHandler(manage_api_keys_menu, pattern='^manage_api_keys$'),
        ],
        CONFIRM_DELETE_API_KEY: [
            CallbackQueryHandler(api_key_delete_execute, pattern=r'^api_key_delete_exec_\d+$'),
            CallbackQueryHandler(api_key_details, pattern=r'^api_key_details_\d+$'),
        ],
        CONFIRM_ROTATE_API_KEY: [
            CallbackQueryHandler(api_key_rotate_execute, pattern=r'^api_key_rotate_exec_\d+$'),
            CallbackQueryHandler(api_key_details, pattern=r'^api_key_details_\d+$'),
        ],
        MANAGE_PROMO: [
            CallbackQueryHandler(promo_enable_prompt, pattern='^promo_enable$'),
            CallbackQueryHandler(promo_disable, pattern='^promo_disable$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        GET_PROMO_END_DATE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, promo_receive_end_date),
            CallbackQueryHandler(promo_enable_no_end_date, pattern='^promo_no_end_date$'),
            CallbackQueryHandler(manage_promo_menu, pattern='^manage_promo$'),
        ],
        # Состояния очереди анализатора дыр
        ADMIN_LOOPHOLE_QUEUE: [
            CallbackQueryHandler(loophole_job_detail, pattern=r'^loophole_job_\d+$'),
            CallbackQueryHandler(loophole_queue_pause, pattern='^loophole_queue_pause$'),
            CallbackQueryHandler(loophole_queue_resume, pattern='^loophole_queue_resume$'),
            CallbackQueryHandler(loophole_cache_toggle, pattern='^loophole_cache_toggle$'),
            CallbackQueryHandler(loophole_cache_clear, pattern='^loophole_cache_clear$'),
            CallbackQueryHandler(loophole_clear_zero, pattern='^loophole_clear_zero$'),
            CallbackQueryHandler(loophole_feature_toggle, pattern='^loophole_feature_toggle$'),
            CallbackQueryHandler(show_all_analyses, pattern='^show_all_analyses$'),
            CallbackQueryHandler(manage_loophole_queue, pattern='^manage_loophole_queue$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        ADMIN_LOOPHOLE_RESULTS: [
            CallbackQueryHandler(loophole_result_doc, pattern=r'^lresdoc_\d+$'),
            CallbackQueryHandler(loophole_result_open, pattern=r'^lresult_\d+$'),
            CallbackQueryHandler(show_all_analyses, pattern=r'^lres_\d+_[01]$'),
            CallbackQueryHandler(show_all_analyses, pattern='^show_all_analyses$'),
            CallbackQueryHandler(manage_loophole_queue, pattern='^manage_loophole_queue$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        ADMIN_LOOPHOLE_JOB_DETAIL: [
            CallbackQueryHandler(loophole_job_top, pattern=r'^loophole_jtop_\d+$'),
            CallbackQueryHandler(loophole_job_up, pattern=r'^loophole_jup_\d+$'),
            CallbackQueryHandler(loophole_job_down, pattern=r'^loophole_jdown_\d+$'),
            CallbackQueryHandler(loophole_job_setpos_prompt, pattern=r'^loophole_jsetpos_\d+$'),
            CallbackQueryHandler(loophole_job_cancel, pattern=r'^loophole_jcancel_\d+$'),
            CallbackQueryHandler(loophole_job_oob, pattern=r'^loophole_joob_\d+$'),
            CallbackQueryHandler(loophole_job_detail, pattern=r'^loophole_job_\d+$'),
            CallbackQueryHandler(manage_loophole_queue, pattern='^manage_loophole_queue$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        ADMIN_LOOPHOLE_SET_POSITION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, loophole_job_set_position_receive),
            CallbackQueryHandler(manage_loophole_queue, pattern='^manage_loophole_queue$'),
        ],
        # Заявки в тестировщики: список / карточка / принять-отклонить / тумблер приёма
        ADMIN_TESTER_APPS: [
            CallbackQueryHandler(accept_tester_application, pattern=r'^tapp_ok_\d+$'),
            CallbackQueryHandler(reject_tester_application, pattern=r'^tapp_no_\d+$'),
            CallbackQueryHandler(show_tester_app_detail, pattern=r'^tapp_\d+$'),
            CallbackQueryHandler(show_tester_apps_list, pattern=r'^tapps_\d+_[01]$'),
            CallbackQueryHandler(toggle_tester_intake, pattern='^tester_intake_toggle$'),
            CallbackQueryHandler(manage_tester_applications, pattern='^manage_tester_apps$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
        # Заявки на ВИП за промокод: тумблер приёма / выгрузка открытых в личку.
        # Одобрение/отклонение (vip_ok_/vip_no_) — top-level (работают и из канала, см. main.py).
        ADMIN_VIP_APPS: [
            CallbackQueryHandler(vip_apps_dump, pattern='^vapps_dump$'),
            CallbackQueryHandler(toggle_vip_intake, pattern='^vip_intake_toggle$'),
            CallbackQueryHandler(manage_vip_applications, pattern='^manage_vip_apps$'),
            CallbackQueryHandler(admin_panel, pattern='^admin_panel$'),
        ],
    },
    fallbacks=[
        CommandHandler('start', start),
        CommandHandler('admin', admin_panel),
        CallbackQueryHandler(cancel_running_broadcast, pattern=r'^bc_cancel_running_\d+$'),
        CallbackQueryHandler(main_menu, pattern='^main_menu$'),
        CallbackQueryHandler(personal_account, pattern='^personal_account$'),
    ],
    per_message=False, allow_reentry=True
)

# END OF FILE admin_handlers.py
