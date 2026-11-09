# START OF FILE user_handlers/core.py

"""
Модуль с базовыми обработчиками и основными функциями навигации.
"""

import logging
from datetime import datetime, timezone, timedelta

import asyncpg
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes, ConversationHandler

# Локальные импорты.
from database.manager import db_manager
from utils import rate_limited
from config import (
    SUPER_ADMIN_ID,
    DAILY_QUESTION_LIMIT, DAILY_INTERVIEW_GEN_LIMIT, DAILY_COMPLAINT_GEN_LIMIT,
    SUBSCRIPTION_BONUS_QUESTION, SUBSCRIPTION_BONUS_INTERVIEW_GEN, SUBSCRIPTION_BONUS_COMPLAINT_GEN,
    ASSISTANT_LIMITS, ASSISTANT_SUBSCRIPTION_BONUSES, ASSISTANT_LIMIT_RESET_HOURS,
    PROJECTS_CONFIG, SERVERS_CONFIG
)

ASSISTANT_LIMIT_DISPLAY_NAMES = {
    'advocate_field_work': "⚖️ Адвокат (на месте)",
    'advocate_case_analysis': "📂 Адвокат (анализ дела)",
    'prosecutor_field_work': "🚨 Прокурор (на месте)",
    'prosecutor_case_analysis': "🗂️ Прокурор (анализ дела)",
    'judge_court_case': "⚖️ Судья (судебное дело)",
}

QUESTION_TYPE_DISPLAY_NAMES = {
    'detailed': "Подробный",
    'brief': "Краткий",
    'free': "Свободный",
}


def _format_question_template(project_id: str | None, server_id: str | None, question_type: str | None) -> str:
    """Форматирует сохраненный шаблон вопросов для личного кабинета."""
    if not project_id or not server_id or question_type not in QUESTION_TYPE_DISPLAY_NAMES:
        return "<i>Не настроен. Бот будет спрашивать проект, сервер и формат ответа.</i>"

    project_name = PROJECTS_CONFIG.get(project_id, {}).get('display_name', project_id)
    server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)
    question_type_name = QUESTION_TYPE_DISPLAY_NAMES[question_type]
    return (
        f"Проект: <b>{project_name}</b>\n"
        f"Сервер: <b>{server_name}</b>\n"
        f"Формат ответа: <b>{question_type_name}</b>"
    )


async def log_user(user: Update.effective_user):
    """Сохраняет или обновляет информацию о пользователе в базе данных."""
    query = """
            INSERT INTO users (user_id, full_name, username, last_seen)
            VALUES ($1, $2, $3, NOW()) ON CONFLICT (user_id) DO
            UPDATE SET full_name = EXCLUDED.full_name, username = EXCLUDED.username, last_seen = NOW();
            """
    await db_manager.execute(query, user.id, user.full_name, user.username)


# Само-сброс залипшего is_blocked. Флаг ставится в TRUE ТОЛЬКО при Forbidden в рассылке
# («в тот момент юзер держал бота в блоке») и нигде не сбрасывался обратно → залипал
# навсегда. А compute_access_tier рубит доступ к оверлею при is_blocked даже у ВИП/тестеров.
# Любой ВХОДЯЩИЙ апдейт = юзер снова доступен боту → гасим флаг. Регистрируется как
# общий мидлвар (TypeHandler в group=-1), поэтому ловит и /start, и кнопки, и текст.
# Гард `AND is_blocked = TRUE` → для подавляющего большинства это UPDATE 0 строк (дёшево).
# Ошибки глушим: мидлвар не должен ронять обработку апдейта.
async def reset_stale_block_flag(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    try:
        await db_manager.execute(
            "UPDATE users SET is_blocked = FALSE WHERE user_id = $1 AND is_blocked = TRUE",
            user.id,
        )
    except Exception:
        logging.debug("[BLOCK_RESET] не удалось сбросить is_blocked для %s", user.id, exc_info=True)


async def _handle_daily_limits(user_id: int, usage_type: str, is_subscriber: bool,
                               consume: bool = True) -> tuple[bool, int]:
    """Обрабатывает лимиты, которые сбрасываются ежедневно."""
    field_to_increment = ""
    total_limit = 0
    if usage_type in ('detailed', 'brief', 'free'):
        total_limit = DAILY_QUESTION_LIMIT + (SUBSCRIPTION_BONUS_QUESTION if is_subscriber else 0)
        field_to_increment = 'question_reqs'
    elif usage_type == 'interview':
        total_limit = DAILY_INTERVIEW_GEN_LIMIT + (SUBSCRIPTION_BONUS_INTERVIEW_GEN if is_subscriber else 0)
        field_to_increment = 'interview_gens'
    elif usage_type == 'complaint':
        total_limit = DAILY_COMPLAINT_GEN_LIMIT + (SUBSCRIPTION_BONUS_COMPLAINT_GEN if is_subscriber else 0)
        field_to_increment = 'complaint_gens'

    # peek (consume=False): только читаем текущий счётчик, без инкремента.
    if not consume:
        if not field_to_increment:
            return True, 0
        sel = f"SELECT {field_to_increment} FROM user_usage WHERE user_id = $1 AND usage_date = CURRENT_DATE"
        current = await db_manager.fetchval(sel, user_id) or 0
        return (current < total_limit), current

    query = f"""
        INSERT INTO user_usage (user_id, usage_date, {field_to_increment}) VALUES ($1, CURRENT_DATE, 1)
        ON CONFLICT (user_id, usage_date) DO UPDATE SET
            {field_to_increment} = user_usage.{field_to_increment} + 1
        RETURNING {field_to_increment};
    """
    current_count = await db_manager.fetchval(query, user_id) or 1
    return (current_count <= total_limit), current_count


async def _handle_assistant_limits(user_id: int, usage_type: str, is_subscriber: bool,
                                   consume: bool = True) -> tuple[bool, int]:
    """Обрабатывает лимиты Ассистента со скользящим окном, используя новую таблицу."""
    query_count = """
        SELECT COUNT(*) FROM assistant_usage_log
        WHERE user_id = $1 AND usage_type = $2
        AND timestamp >= NOW() - INTERVAL '1 hour' * $3
    """
    current_usage = await db_manager.fetchval(query_count, user_id, usage_type, ASSISTANT_LIMIT_RESET_HOURS) or 0

    base_limit = ASSISTANT_LIMITS.get(usage_type, 0)
    bonus = ASSISTANT_SUBSCRIPTION_BONUSES.get(usage_type, 0)
    total_limit = base_limit + (bonus if is_subscriber else 0)

    if current_usage >= total_limit:
        return False, current_usage

    # peek (consume=False): лимит ещё есть, но НЕ списываем — навигация не должна жечь лимит.
    if not consume:
        return True, current_usage

    await db_manager.execute("INSERT INTO assistant_usage_log (user_id, usage_type) VALUES ($1, $2);", user_id,
                             usage_type)
    return True, current_usage + 1


async def check_and_update_usage(user_id: int, usage_type: str, consume: bool = True) -> tuple[bool, int]:
    """Маршрутизатор: определяет, какую логику лимитов использовать.

    consume=True  — проверить И списать одно использование (поведение по умолчанию).
    consume=False — только проверить, доступно ли ещё (peek), БЕЗ списания. Нужно, чтобы
                    простая навигация по меню (показ промпта) не сжигала лимит.
    """
    user_info = await db_manager.fetchrow(
        "SELECT is_vip, is_admin, is_subscriber, is_unlimited FROM users WHERE user_id = $1",
        user_id)
    # is_unlimited — промо-безлимит за промокод (выдаётся через заявку, alembic 015).
    # Снимает ВСЕ лимиты, но НЕ открывает оверлей (в отличие от is_vip) — оверлей за подпиской.
    if user_info and (user_info['is_vip'] or user_info['is_admin'] or user_info['is_unlimited']
                      or user_id == SUPER_ADMIN_ID):
        return True, 0

    is_subscriber = user_info['is_subscriber'] if user_info else False

    if usage_type in ASSISTANT_LIMITS:
        return await _handle_assistant_limits(user_id, usage_type, is_subscriber, consume)
    else:
        return await _handle_daily_limits(user_id, usage_type, is_subscriber, consume)


async def refund_usage(user_id: int, usage_type: str) -> bool:
    """
    Откатывает лимит пользователя на 1 использование при ошибке.

    Args:
        user_id: ID пользователя
        usage_type: Тип использования ('detailed', 'brief', 'interview', 'assistant_*')

    Returns:
        True если откат успешен, False если нет
    """
    try:
        # Для типов с ежедневными лимитами
        if usage_type in ['detailed', 'brief', 'free', 'interview', 'complaint']:
            field_to_decrement = ''
            if usage_type in ('detailed', 'brief', 'free'):
                field_to_decrement = 'question_reqs'
            elif usage_type == 'interview':
                field_to_decrement = 'interview_gens'
            elif usage_type == 'complaint':
                field_to_decrement = 'complaint_gens'

            if field_to_decrement:
                query = f"""
                    UPDATE user_usage 
                    SET {field_to_decrement} = GREATEST(0, {field_to_decrement} - 1)
                    WHERE user_id = $1 AND usage_date = CURRENT_DATE
                    RETURNING {field_to_decrement};
                """
                result = await db_manager.fetchval(query, user_id)
                if result is not None:
                    logging.info(f"[REFUND] Откат лимита для user_id {user_id}, тип {usage_type}. Новое значение: {result}")
                    return True

        # Для типов с лимитами ассистента (скользящее окно)
        elif usage_type in ASSISTANT_LIMITS:
            # Удаляем последнюю запись для этого пользователя и типа
            query = """
                DELETE FROM assistant_usage_log 
                WHERE id = (
                    SELECT id FROM assistant_usage_log
                    WHERE user_id = $1 AND usage_type = $2
                    ORDER BY timestamp DESC LIMIT 1
                )
                RETURNING id;
            """
            result = await db_manager.fetchval(query, user_id, usage_type)
            if result is not None:
                logging.info(f"[REFUND] Откат лимита ассистента для user_id {user_id}, тип {usage_type}")
                return True

        logging.warning(f"[REFUND] Не удалось откатить лимит для user_id {user_id}, тип {usage_type}")
        return False

    except Exception as e:
        logging.error(f"[REFUND] Ошибка при откате лимита для user_id {user_id}, тип {usage_type}", exc_info=e)
        return False


async def send_error_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    keyboard = [[InlineKeyboardButton("📝 Создать репорт", callback_data="start_report_dialog")]]
    await context.bot.send_message(
        chat_id=chat_id,
        text="Прошу прощения, при обработке вашего запроса произошла внутренняя ошибка.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def _display_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new_message: bool = False):
    user = update.effective_user
    context.user_data.clear()
    text = f"Здравствуйте, {user.first_name}! Я ваш юрист-консультант. Выберите действие:"
    # «Анализатор законов» можно отключить из админки — тогда кнопку не показываем.
    try:
        from services.law_loophole.queue import is_loophole_enabled
        loophole_on = await is_loophole_enabled()
    except Exception:
        loophole_on = True
    keyboard = [
        [InlineKeyboardButton("❓ Задать вопрос", callback_data="start_question")],
        [InlineKeyboardButton("⚖️ Ролевой Помощник", callback_data="start_assistant")],
    ]
    if loophole_on:
        keyboard.append([InlineKeyboardButton("🕳 Анализатор законов", callback_data="start_loophole")])
    # «🎤 Голосовой оверлей» — кнопка видна ВСЕМ (мини-реклама). Сам раздел внутри решает по
    # compute_access_tier: у кого есть доступ (ВИП/админ/тестер) — скачать/ключ/гайд; у кого
    # нет — промо-экран «доступ за ВИП по промокоду». Реальный гейт — на скачивании/ключе.
    keyboard.append([InlineKeyboardButton("🎤 Голосовой оверлей", callback_data="overlay_menu")])
    keyboard += [
        [InlineKeyboardButton("📝 Составить жалобу/иск", callback_data="start_complaint_gen")],
        [InlineKeyboardButton("📋 Генератор вопросов", callback_data="start_interview_gen")],
        [InlineKeyboardButton("📈 Расширить лимиты", callback_data="offer_subscription")],
        [InlineKeyboardButton("💎 Получить ВИП за промокод", callback_data="vip_promo_start")],
        [InlineKeyboardButton("👤 Личный кабинет", callback_data="personal_account")],
        [InlineKeyboardButton("📝 Создать репорт", callback_data="start_report_dialog")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    query = update.callback_query
    try:
        if query and not is_new_message:
            # Проверяем, является ли сообщение фото (с caption вместо text)
            if query.message and query.message.photo:
                # Сообщение с фото - удаляем и отправляем новое текстовое
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
            else:
                await query.edit_message_text(text=text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e): logging.warning(f"Ошибка в _display_main_menu: {e}")
    except Forbidden:
        logging.info(f"Пользователь {update.effective_user.id if update.effective_user else 'unknown'} заблокировал бота")


async def _display_banned_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new_message: bool = False):
    """Отображает специальное меню для забаненных пользователей с доступом только к репортам."""
    user = update.effective_user
    context.user_data.clear()
    text = (
        f"⛔️ {user.first_name}, ваш доступ к боту ограничен.\n\n"
        "Вы можете связаться с администрацией через систему поддержки, "
        "чтобы узнать причину блокировки или обжаловать решение."
    )
    keyboard = [
        [InlineKeyboardButton("📝 Создать репорт", callback_data="start_report_dialog")],
        [InlineKeyboardButton("📂 Мои репорты", callback_data="report_view_mine")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    query = update.callback_query
    try:
        if query and not is_new_message:
            # Проверяем, является ли сообщение фото (с caption вместо text)
            if query.message and query.message.photo:
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
            else:
                await query.edit_message_text(text=text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e): logging.warning(f"Ошибка в _display_banned_menu: {e}")
    except Forbidden:
        logging.info(f"Пользователь {update.effective_user.id if update.effective_user else 'unknown'} заблокировал бота")


@rate_limited
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await log_user(update.effective_user)

    # Проверяем, забанен ли пользователь
    user_id = update.effective_user.id
    user_record = await db_manager.fetchrow("SELECT is_banned FROM users WHERE user_id = $1", user_id)

    if user_record and user_record['is_banned']:
        # Показываем специальное меню для забаненных
        await _display_banned_menu(update, context, is_new_message=True)
        return ConversationHandler.END

    # Диплинк на анализатор законов: https://t.me/<bot>?start=loophole
    # (используется в рассылках — кнопка ведёт сразу к запуску функции).
    if context.args and context.args[0] == 'loophole':
        try:
            from services.law_loophole.queue import is_loophole_enabled
            _lh_on = await is_loophole_enabled()
        except Exception:
            _lh_on = True
        if _lh_on:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🕳 Открыть анализатор законов", callback_data="start_loophole")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ])
            await update.message.reply_text(
                "🕳 <b>Анализатор законов</b>\n\n"
                "Поиск дыр, противоречий и абузов в правилах сервера.\n"
                "Нажмите кнопку ниже, чтобы запустить.",
                reply_markup=keyboard, parse_mode='HTML',
            )
        else:
            await update.message.reply_text(
                "🕳 Анализатор законов сейчас временно отключён.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]),
            )
        return ConversationHandler.END

    # Дип-линк на голосовой оверлей: https://t.me/<bot>?start=overlay
    # (для рассылок). ВИП+ → сразу раздел оверлея; остальным → промо-экран (как кнопка меню).
    if context.args and context.args[0] == 'overlay':
        from services.device_auth import compute_access_tier
        from user_handlers.test_loophole_flow import _build_overlay_view, _build_overlay_promo_view
        tier = await compute_access_tier(user_id)
        if tier or user_id == SUPER_ADMIN_ID:
            text, kb = await _build_overlay_view(user_id)
        else:
            text, kb = await _build_overlay_promo_view(user_id)
        await update.message.reply_text(
            text, reply_markup=kb, parse_mode='HTML', disable_web_page_preview=True,
        )
        return ConversationHandler.END

    # Дип-линк на подачу заявки в тестировщики: https://t.me/<bot>?start=apply_tester
    # (ссылку владелец кидает в канал). Показывает карточку с кнопкой «Подать заявку»;
    # сама анкета — в user_handlers/tester_application_flow.py (entry: tester_apply_start).
    if context.args and context.args[0] == 'apply_tester':
        from services.tester_applications import is_intake_enabled, get_pending_application
        roles = await db_manager.fetchrow(
            "SELECT is_tester, is_admin FROM users WHERE user_id = $1", user_id
        )
        if roles and (roles['is_tester'] or roles['is_admin']):
            await update.message.reply_text(
                "🧪 Ты уже в команде тестировщиков! Открой /test, чтобы скачать оверлей и "
                "посмотреть свою роль.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]),
            )
            return ConversationHandler.END
        if not await is_intake_enabled():
            await update.message.reply_text(
                "📝 Приём заявок в тестировщики сейчас закрыт. Загляни позже!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]),
            )
            return ConversationHandler.END
        if await get_pending_application(user_id):
            await update.message.reply_text(
                "⏳ Твоя заявка уже на рассмотрении. Как только решим — сообщим прямо здесь. "
                "Спасибо за терпение!",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]),
            )
            return ConversationHandler.END
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Подать заявку", callback_data="tester_apply_start")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ])
        await update.message.reply_text(
            "🧪 <b>Заявка в тестировщики</b>\n\n"
            "Набираем тестеров голосового оверлея для RP — это ранний доступ к приложению "
            "в обмен на твой фидбэк и поиск багов.\n\n"
            "Заполни короткую анкету (3 вопроса), и мы рассмотрим твою кандидатуру.",
            reply_markup=keyboard, parse_mode='HTML',
        )
        return ConversationHandler.END

    # Дип-линк на подачу заявки на ВИП/безлимит за промокод: ?start=apply_vip
    # (ссылку владелец кидает в канал/рассылку). Карточка с кнопкой — сама анкета в
    # user_handlers/vip_application_flow.py (entry: vip_promo_start).
    if context.args and context.args[0] == 'apply_vip':
        from services.vip_applications import is_intake_enabled as vip_intake, get_pending_application as vip_pending
        roles = await db_manager.fetchrow(
            "SELECT is_unlimited, is_admin FROM users WHERE user_id = $1", user_id
        )
        home = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]])
        if roles and (roles['is_unlimited'] or roles['is_admin']):
            await update.message.reply_text("💎 У тебя уже есть безлимитный статус!", reply_markup=home)
            return ConversationHandler.END
        if not await vip_intake():
            await update.message.reply_text("📝 Приём заявок на ВИП сейчас закрыт. Загляни позже!", reply_markup=home)
            return ConversationHandler.END
        if await vip_pending(user_id):
            await update.message.reply_text(
                "⏳ Твоя заявка уже на рассмотрении. Как решим — сообщим прямо здесь.", reply_markup=home
            )
            return ConversationHandler.END
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Подать заявку на ВИП", callback_data="vip_promo_start")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ])
        await update.message.reply_text(
            "💎 <b>ВИП за промокод</b>\n\n"
            "Ввёл промокод на сервере? Получи <b>безлимит</b> на все функции бота!\n\n"
            "Приложи пруфы (видео/скрины ввода промокода) и заполни короткую форму — "
            "мы проверим и выдадим статус.",
            reply_markup=keyboard, parse_mode='HTML',
        )
        return ConversationHandler.END

    # Дип-линк привязки десктоп-оверлея (?start=link_<код>) — подтверждает device-code login.
    if context.args and context.args[0].startswith('link_'):
        from services.device_auth import compute_access_tier, approve_device_login
        code = context.args[0][5:].strip().upper()
        tier = await compute_access_tier(user_id)
        if not tier:
            await update.message.reply_text(
                "🔒 Доступ к оверлею пока открыт только тестировщикам. Обратись к администратору."
            )
            return ConversationHandler.END
        ok = await approve_device_login(code, user_id, tier)
        if ok:
            await update.message.reply_text(
                "✅ Устройство привязано! Вернись в приложение оверлея — вход выполнится автоматически."
            )
        else:
            await update.message.reply_text(
                "⚠️ Код не найден или истёк. Сгенерируй новый в приложении и попробуй снова."
            )
        return ConversationHandler.END

    await _display_main_menu(update, context, is_new_message=True)
    return ConversationHandler.END


@rate_limited
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except BadRequest:
            pass
        last_answer_id = context.user_data.get('last_answer_message_id')

        # Проверяем, забанен ли пользователь
        user_id = update.effective_user.id
        user_record = await db_manager.fetchrow("SELECT is_banned FROM users WHERE user_id = $1", user_id)

        if user_record and user_record['is_banned']:
            # Показываем меню для забаненных
            if last_answer_id and query.message.message_id == last_answer_id:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass  # Игнорируем "Message is not modified"
                await _display_banned_menu(update, context, is_new_message=True)
            else:
                await _display_banned_menu(update, context)
        else:
            # Обычное меню
            if last_answer_id and query.message.message_id == last_answer_id:
                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass  # Игнорируем "Message is not modified"
                await _display_main_menu(update, context, is_new_message=True)
            else:
                await _display_main_menu(update, context)
    return ConversationHandler.END


@rate_limited
async def personal_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        user_record = await db_manager.fetchrow(
            """
            SELECT is_vip, is_admin, is_subscriber, is_unlimited,
                   default_project_id, default_server_id, default_question_type
            FROM users
            WHERE user_id = $1
            """,
            user_id
        )
    except asyncpg.exceptions.UndefinedColumnError:
        logging.warning("[PersonalAccount] Колонки шаблона вопросов отсутствуют. Показываю кабинет без шаблона.")
        user_record = await db_manager.fetchrow(
            "SELECT is_vip, is_admin, is_subscriber, is_unlimited FROM users WHERE user_id = $1",
            user_id
        )
    if not user_record:
        await query.edit_message_text("Не удалось найти информацию о вашем профиле.")
        return ConversationHandler.END

    is_vip, is_admin, is_subscriber = user_record.get('is_vip'), user_record.get('is_admin'), user_record.get(
        'is_subscriber')
    # is_unlimited — промо-безлимит за промокод (без оверлея). Снимает лимиты так же, как VIP.
    is_unlimited = user_record.get('is_unlimited')
    status_text = "👤 Стандарт"
    if is_subscriber: status_text = "💎 Подписчик"
    if is_unlimited: status_text = "♾ Безлимит"
    if is_vip: status_text = "👑 VIP"
    if is_admin: status_text = "🧑‍⚖️ Администратор"
    if user_id == SUPER_ADMIN_ID: status_text = "🔑 Супер-Админ"

    if is_vip or is_admin or is_unlimited or user_id == SUPER_ADMIN_ID:
        question_remains, interview_remains = "Безлимитно", "Безлимитно"
        assist_limits_text = "Безлимитно"
        daily_reset_time_text = "🕒 <i>Дневные лимиты для вас не ограничены.</i>"
        assistant_reset_time_text = "🕒 <i>Лимиты ассистента для вас не ограничены.</i>"
    else:
        # --- Ежедневные лимиты ---
        usage_daily = await db_manager.fetchrow(
            "SELECT * FROM user_usage WHERE user_id = $1 AND usage_date = CURRENT_DATE", user_id)
        limit_question = DAILY_QUESTION_LIMIT + (SUBSCRIPTION_BONUS_QUESTION if is_subscriber else 0)
        question_remains = f"<b>{max(0, limit_question - (usage_daily.get('question_reqs', 0) if usage_daily else 0))} / {limit_question}</b>"
        limit_interview = DAILY_INTERVIEW_GEN_LIMIT + (SUBSCRIPTION_BONUS_INTERVIEW_GEN if is_subscriber else 0)
        interview_remains = f"<b>{max(0, limit_interview - (usage_daily.get('interview_gens', 0) if usage_daily else 0))} / {limit_interview}</b>"

        now_utc = datetime.now(timezone.utc)
        next_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        remaining_daily = next_midnight - now_utc
        h_daily, rem_daily = divmod(int(remaining_daily.total_seconds()), 3600)
        m_daily, _ = divmod(rem_daily, 60)
        daily_reset_time_text = f"🕒 <i>До сброса дневных лимитов: {h_daily} ч. {m_daily} мин.</i>"

        # --- Лимиты Ассистента (72-часовое окно) ---
        usage_72h = await db_manager.fetch(
            "SELECT usage_type, COUNT(*) as count FROM assistant_usage_log WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '1 hour' * $2 GROUP BY usage_type",
            user_id, ASSISTANT_LIMIT_RESET_HOURS
        )
        usage_map = {rec['usage_type']: rec['count'] for rec in usage_72h}

        assist_lines = []
        for type, name in ASSISTANT_LIMIT_DISPLAY_NAMES.items():
            limit = ASSISTANT_LIMITS.get(type, 0) + (
                ASSISTANT_SUBSCRIPTION_BONUSES.get(type, 0) if is_subscriber else 0)
            used = usage_map.get(type, 0)
            assist_lines.append(f"{name}: <b>{max(0, limit - used)} / {limit}</b>")
        assist_limits_text = "\n".join(assist_lines)

        oldest_usage = await db_manager.fetchval(
            "SELECT MIN(timestamp) FROM assistant_usage_log WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '1 hour' * $2",
            user_id, ASSISTANT_LIMIT_RESET_HOURS
        )
        if oldest_usage:
            expire_time = oldest_usage + timedelta(hours=ASSISTANT_LIMIT_RESET_HOURS)
            remaining_72h = expire_time - datetime.now(timezone.utc)
            if remaining_72h.total_seconds() > 0:
                h_72, rem_72 = divmod(int(remaining_72h.total_seconds()), 3600)
                m_72, _ = divmod(rem_72, 60)
                assistant_reset_time_text = f"🕒 <i>До восстановления 1-го исп.: {h_72} ч. {m_72} мин.</i>"
            else:
                assistant_reset_time_text = "🕒 <i>Лимиты восстановлены.</i>"
        else:
            assistant_reset_time_text = "🕒 <i>Все лимиты ассистента доступны.</i>"

    question_template_text = _format_question_template(
        user_record.get('default_project_id'),
        user_record.get('default_server_id'),
        user_record.get('default_question_type')
    )

    text = (f"<b>👤 Ваш личный кабинет</b>\n\n"
            f"<b>Ваш статус:</b> {status_text}\n\n"
            f"<b>Оставшиеся дневные лимиты:</b>\n"
            f"❓ Вопросы: {question_remains}\n"
            f"📝 Генерация вопросов: {interview_remains}\n"
            f"{daily_reset_time_text}\n\n"
            f"<b>Консультации ассистента:</b>\n{assist_limits_text}\n"
            f"{assistant_reset_time_text}\n\n"
            f"<b>Шаблон вопросов:</b>\n{question_template_text}")

    keyboard = [
        [InlineKeyboardButton("⚙️ Настроить шаблон вопросов", callback_data="setup_question_defaults")],
        [InlineKeyboardButton("⚖️ Судебный помощник", callback_data="start_court_assistant")],
        [InlineKeyboardButton("📖 История заданных вопросов", callback_data="history_asked_0")],
        [InlineKeyboardButton("📝 История сгенерированных вопросов", callback_data="history_generated_0")],
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
            parse_mode='HTML'
        )
    else:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
    return ConversationHandler.END


async def unknown_text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Фолбэк для текстовых сообщений в состояниях, где ожидается нажатие кнопки.
    Возвращает None — состояние ConversationHandler не меняется.
    """
    await update.message.reply_text(
        "ℹ️ Сейчас я жду нажатия на кнопку.\n"
        "Используйте меню выше или нажмите /start для возврата в главное меню."
    )

# END OF FILE user_handlers/core.py