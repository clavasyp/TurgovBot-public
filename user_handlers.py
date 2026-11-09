# START OF FILE user_handlers.py

"""
Обработчики для взаимодействия с обычными пользователями.

Этот модуль содержит всю логику, связанную с пользовательскими диалогами:
-   Навигация по меню (выбор сервера, типа ответа).
-   Проверка и обновление лимитов использования.
-   Обработка вопроса пользователя (текстом и голосом), включая вызов моделей AI.
-   Формирование и отправка ответа.
-   Управление историей диалогов, включая уточняющие вопросы.
-   Система репортов.
-   Отображение личного кабинета.
-   Генерация вопросов для собеседований.
"""

import asyncio
import re
import traceback
import logging
import json
from datetime import datetime, timezone, timedelta

import vertexai
from vertexai.generative_models import GenerativeModel, Part, Content, HarmCategory, HarmBlockThreshold
from google.api_core import exceptions as google_exceptions
# ИЗМЕНЕНИЕ: Добавляем импорт для прямого взаимодействия с GCS
from google.cloud import storage
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, Message
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler
import emoji

# Локальные импорты.
import config
import knowledge_base_manager
from database.manager import db_manager
from utils import get_enabled_servers, rate_limited, sanitize_telegram_html, reinitialize_vertex_ai
from google_sheets_handler import log_to_google_sheet
from prompts import get_system_prompt, get_router_prompt, get_interview_questions_prompt, get_optimizer_prompt
from voice_processor import transcribe_voice
from subscription_manager import check_subscription
from config import (
    DAILY_DETAILED_LIMIT, DAILY_BRIEF_LIMIT, SUPER_ADMIN_ID,
    DAILY_INTERVIEW_GEN_LIMIT,
    ROUTER_MODEL_NAME, HISTORY_LIMIT,
    INTERVIEW_ROLES, INTERVIEW_DIFFICULTIES, MIN_QUESTIONS, MAX_QUESTIONS,
    QUESTION_GENERATOR_MODEL_NAME, LEGAL_CONSULTANT_MODEL_NAME,
    QUESTION_OPTIMIZER_MODEL_NAME,
    CHOOSING_SERVER, CHOOSING_ANSWER_TYPE, AWAITING_QUESTION,
    CHOOSING_INTERVIEW_SERVER, CHOOSING_INTERVIEW_ROLE, CHOOSING_INTERVIEW_DIFFICULTY,
    AWAITING_INTERVIEW_QUANTITY,
    AWAITING_CLARIFICATION,
    AWAITING_TRANSCRIPTION_CONFIRMATION, AWAITING_EDITED_TRANSCRIPTION,
    OPTIMIZING_QUESTION,
    AWAITING_OPTIMIZATION_CHOICE, AWAITING_OPTIMIZATION_CONFIRMATION, AWAITING_EDITED_OPTIMIZED_QUESTION,
    VIEWING_HISTORY, VIEWING_HISTORY_MESSAGE,
    CHECKING_SUBSCRIPTION,
    TELEGRAM_CHANNEL_URL,
    SUBSCRIPTION_BONUS_DETAILED, SUBSCRIPTION_BONUS_BRIEF, SUBSCRIPTION_BONUS_INTERVIEW_GEN,
    CHOOSING_REPORT_ACTION, AWAITING_REPORT_TITLE, VIEWING_USER_REPORTS, AWAITING_USER_REPLY_IN_REPORT
)

HISTORY_PAGE_SIZE = 8
GENERATION_HISTORY_PREFIX = "gen_req::"


async def log_user(user: Update.effective_user):
    query = """
            INSERT INTO users (user_id, full_name, username, last_seen)
            VALUES ($1, $2, $3, NOW()) ON CONFLICT (user_id) DO
            UPDATE SET full_name = EXCLUDED.full_name, username = EXCLUDED.username, last_seen = NOW();
            """
    await db_manager.execute(query, user.id, user.full_name, user.username)


async def check_and_update_usage(user_id: int, usage_type: str) -> tuple[bool, int]:
    """
    Проверяет и обновляет использование, учитывая VIP, админ-статус и подписку.
    """
    user_info = await db_manager.fetchrow(
        "SELECT is_vip, is_admin, is_subscriber FROM users WHERE user_id = $1", user_id
    )

    if user_info and (user_info['is_vip'] or user_info['is_admin'] or user_id == SUPER_ADMIN_ID):
        return True, 0

    is_subscriber = user_info['is_subscriber'] if user_info else False

    base_limits = {
        'detailed': DAILY_DETAILED_LIMIT,
        'brief': DAILY_BRIEF_LIMIT,
        'interview': DAILY_INTERVIEW_GEN_LIMIT
    }
    bonus_limits = {
        'detailed': SUBSCRIPTION_BONUS_DETAILED,
        'brief': SUBSCRIPTION_BONUS_BRIEF,
        'interview': SUBSCRIPTION_BONUS_INTERVIEW_GEN
    }
    field_map = {
        'detailed': 'detailed_reqs',
        'brief': 'brief_reqs',
        'interview': 'interview_gens'
    }

    if usage_type not in base_limits:
        return False, -1

    total_limit = base_limits[usage_type]
    if is_subscriber:
        total_limit += bonus_limits[usage_type]

    field_to_increment = field_map[usage_type]

    query = f"""
        INSERT INTO user_usage (user_id, {field_to_increment}) VALUES ($1, 1)
        ON CONFLICT (user_id, usage_date) DO UPDATE SET
            {field_to_increment} = user_usage.{field_to_increment} + 1
        RETURNING {field_to_increment};
    """
    current_count = await db_manager.fetchval(query, user_id)
    current_count = current_count or 1

    return (current_count <= total_limit), current_count


async def get_relevant_laws(user_question: str, server_id: str) -> list[str]:
    """
    >>> ИЗМЕНЕНИЕ <<<
    Удалено условие `is_active = TRUE` из запроса, так как теперь оно не нужно.
    """
    start_time = asyncio.get_running_loop().time()
    logging.info(f"[ROUTER] Этап 1: Запуск модели-фильтра ({ROUTER_MODEL_NAME}) для сервера {server_id}...")
    try:
        law_records = await db_manager.fetch(
            """
            SELECT title, gemini_file_uri
            FROM law_documents
            WHERE server_id = $1
              AND gemini_file_uri IS NOT NULL
            """,
            server_id
        )
        if not law_records:
            logging.warning(f"[ROUTER] Этап 1: В БД отсутствуют документы для сервера {server_id}.")
            return []
        law_map_by_index = {i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']} for i, r in
                            enumerate(law_records)}
        prompt_lines = [f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)]
        law_titles_str = "\n".join(prompt_lines)
        router_prompt = get_router_prompt(user_question, law_titles_str)

        router_model = GenerativeModel(ROUTER_MODEL_NAME)
        response = await router_model.generate_content_async(router_prompt)

        selected_indices = {int(line.strip()) for line in response.text.split('\n') if line.strip().isdigit()}
        relevant_uris = [law_map_by_index[index]['uri'] for index in sorted(selected_indices) if
                         index in law_map_by_index]
        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logging.info(
            f"[ROUTER] Этап 1: Успех. Модель-фильтр выбрала {len(relevant_uris)} законов за {duration:.2f} мс.")
        return relevant_uris
    except Exception as e:
        logging.error(f"[ROUTER] Этап 1: КРИТИЧЕСКАЯ ОШИБКА. Сбой в работе модели-фильтра.", exc_info=e)
        return []


async def load_history(chat_id: int) -> list:
    query = "SELECT role, content FROM message_history WHERE user_id = $1 ORDER BY timestamp DESC LIMIT $2;"
    records = await db_manager.fetch(query, chat_id, HISTORY_LIMIT * 2)
    return [{'role': r['role'], 'parts': [r['content']]} for r in reversed(records)]


async def save_history(chat_id: int, server_id: str, role: str, content: str):
    query = "INSERT INTO message_history (user_id, server_id, role, content) VALUES ($1, $2, $3, $4);"
    await db_manager.execute(query, chat_id, server_id, role, content)


def format_history_for_prompt(history: list) -> list[Content]:
    if not history:
        return []

    gemini_history = []
    for message in history:
        role = message.get('role', 'user')
        text_content = ""
        parts_list = message.get('parts', [])

        if isinstance(parts_list, list):
            for part in parts_list:
                if isinstance(part, dict) and 'text' in part:
                    text_content += part['text']
                elif isinstance(part, str):
                    text_content += part

        text_content = re.sub('<[^<]+?>', '', text_content)

        gemini_history.append(Content(role=role, parts=[Part.from_text(text_content)]))

    return gemini_history


async def _display_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, is_new_message: bool = False):
    user = update.effective_user
    context.user_data.clear()
    text = f"Здравствуйте, {user.first_name}! Я ваш юрист-консультант. Выберите действие:"

    keyboard = [
        [InlineKeyboardButton("❓ Задать вопрос", callback_data="start_question")],
        [InlineKeyboardButton("📝 Генератор вопросов", callback_data="start_interview_gen")],
        [InlineKeyboardButton("💎 Получить бонусы", callback_data="offer_subscription")],
        [InlineKeyboardButton("👤 Личный кабинет", callback_data="personal_account")],
        [InlineKeyboardButton("🚨 Сообщить о проблеме / Задать вопрос", callback_data="start_report_dialog")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)
    query = update.callback_query
    try:
        if query and not is_new_message:
            await query.edit_message_text(text=text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logging.warning(f"Неожиданная ошибка BadRequest в _display_main_menu: {e}")


def _fix_html_tags_in_chunk(chunk: str) -> str:
    """Закрывает незакрытые HTML-теги в чанке и удаляет лишние закрывающие."""
    telegram_tags = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
                     'a', 'code', 'pre', 'tg-spoiler']
    open_stack = []
    tag_pattern = re.compile(r'<(/?)(' + '|'.join(telegram_tags) + r')(?:\s[^>]*)?>',  re.IGNORECASE)
    for m in tag_pattern.finditer(chunk):
        is_closing = m.group(1) == '/'
        tag_name = m.group(2).lower()
        if is_closing:
            if open_stack and open_stack[-1] == tag_name:
                open_stack.pop()
        else:
            open_stack.append(tag_name)
    for tag_name in reversed(open_stack):
        chunk += f'</{tag_name}>'
    return chunk


def _split_long_message(text: str, limit: int = 4096) -> list[str]:
    if not text:
        return []
    final_chunks = []
    parts = re.split(r'(<pre>.*?</pre>)', text, flags=re.DOTALL | re.IGNORECASE)
    current_chunk = ""
    for part in parts:
        if not part:
            continue
        if len(current_chunk) + len(part) > limit:
            if current_chunk:
                final_chunks.append(current_chunk)
            current_chunk = ""
        if len(part) > limit:
            is_pre_block = part.lower().startswith('<pre>') and part.lower().endswith('</pre>')
            if is_pre_block:
                content_inside = part[5:-6]
                safe_limit = limit - 20
                while content_inside:
                    sub_chunk = content_inside[:safe_limit]
                    content_inside = content_inside[safe_limit:]
                    final_chunks.append(f"<pre>{sub_chunk}</pre>")
            else:
                while len(part) > limit:
                    split_pos = part.rfind('\n', 0, limit)
                    if split_pos == -1:
                        split_pos = part.rfind('. ', 0, limit)
                    if split_pos == -1:
                        split_pos = limit
                    final_chunks.append(part[:split_pos])
                    part = part[split_pos:]
                if part:
                    current_chunk += part
            continue
        current_chunk += part
    if current_chunk:
        final_chunks.append(current_chunk)
    # Закрываем незакрытые HTML-теги в каждом чанке
    return [_fix_html_tags_in_chunk(chunk) for chunk in final_chunks if chunk.strip()]


@rate_limited
async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except BadRequest as e:
            if "Query is too old" in str(e):
                logging.warning(
                    f"Не удалось ответить на старый CallbackQuery для пользователя {query.from_user.id}. Игнорирую.")
            else:
                logging.error("Неожиданная ошибка BadRequest при вызове query.answer()", exc_info=e)
        last_answer_id = context.user_data.get('last_answer_message_id')
        if last_answer_id and query.message.message_id == last_answer_id:
            await query.edit_message_reply_markup(reply_markup=None)
            await _display_main_menu(update, context, is_new_message=True)
        else:
            await _display_main_menu(update, context)
    return ConversationHandler.END


@rate_limited
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logging.info(f"Пользователь {update.effective_user.id} ({update.effective_user.full_name}) запустил бота.")
    await log_user(update.effective_user)
    await _display_main_menu(update, context, is_new_message=True)
    return ConversationHandler.END


@rate_limited
async def show_subscription_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        text = (
            "💎 **Получите бонусные лимиты за подписку!**\n\n"
            "Подпишитесь на наш Telegram-канал, чтобы получить:\n"
            f"• **+{config.SUBSCRIPTION_BONUS_DETAILED}** подробных вопросов\n"
            f"• **+{config.SUBSCRIPTION_BONUS_BRIEF}** кратких вопросов\n"
            f"• **+{config.SUBSCRIPTION_BONUS_INTERVIEW_GEN}** генераций вопросов\n\n"
            "1. Перейдите в канал и подпишитесь.\n"
            "2. Вернитесь сюда и нажмите кнопку «Проверить подписку»."
        )
        keyboard = [
            [InlineKeyboardButton("➡️ Перейти в канал", url=TELEGRAM_CHANNEL_URL)],
            [InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscription_now")],
            [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
        ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return CHECKING_SUBSCRIPTION


async def handle_check_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer("Проверяю подписку...")

    user_id = query.from_user.id
    is_subscribed = await check_subscription(user_id, context)

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


@rate_limited
async def personal_account(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    user_record = await db_manager.fetchrow(
        "SELECT is_vip, is_admin, is_subscriber FROM users WHERE user_id = $1", user_id
    )
    if not user_record:
        await query.edit_message_text("Не удалось найти информацию о вашем профиле.")
        logging.warning(f"Не найдена запись в БД для пользователя {user_id} при запросе личного кабинета.")
        return ConversationHandler.END

    usage_record = await db_manager.fetchrow(
        "SELECT detailed_reqs, brief_reqs, interview_gens FROM user_usage WHERE user_id = $1 AND usage_date = CURRENT_DATE",
        user_id
    )

    is_vip = user_record.get('is_vip', False)
    is_admin = user_record.get('is_admin', False)
    is_subscriber = user_record.get('is_subscriber', False)

    status_text = "👤 Стандарт"
    if is_subscriber:
        status_text = "💎 Подписчик"
    if is_vip:
        status_text = "👑 VIP"
    if is_admin:
        status_text = "🧑‍⚖️ Администратор"
    if user_id == SUPER_ADMIN_ID:
        status_text = "🔑 Супер-Админ"

    if is_vip or is_admin or user_id == SUPER_ADMIN_ID:
        detailed_remains = brief_remains = interview_remains = "Безлимитно"
        time_left_str = "Вам не нужно ждать"
    else:
        used_detailed = usage_record.get('detailed_reqs', 0) if usage_record else 0
        used_brief = usage_record.get('brief_reqs', 0) if usage_record else 0
        used_interview = usage_record.get('interview_gens', 0) if usage_record else 0

        limit_detailed = DAILY_DETAILED_LIMIT + (SUBSCRIPTION_BONUS_DETAILED if is_subscriber else 0)
        limit_brief = DAILY_BRIEF_LIMIT + (SUBSCRIPTION_BONUS_BRIEF if is_subscriber else 0)
        limit_interview = DAILY_INTERVIEW_GEN_LIMIT + (SUBSCRIPTION_BONUS_INTERVIEW_GEN if is_subscriber else 0)

        detailed_remains = f"{max(0, limit_detailed - used_detailed)} / {limit_detailed}"
        brief_remains = f"{max(0, limit_brief - used_brief)} / {limit_brief}"
        interview_remains = f"{max(0, limit_interview - used_interview)} / {limit_interview}"

        now_utc = datetime.now(timezone.utc)
        next_midnight_utc = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        time_remaining = next_midnight_utc - now_utc
        hours, rem = divmod(int(time_remaining.total_seconds()), 3600)
        minutes, _ = divmod(rem, 60)
        time_left_str = f"{hours} ч. {minutes} мин."

    text = (f"<b>👤 Ваш личный кабинет</b>\n\n"
            f"<b>Ваш статус:</b> {status_text}\n\n"
            f"<b>Оставшиеся лимиты на сегодня:</b>\n"
            f"📜 Подробные вопросы: <b>{detailed_remains}</b>\n"
            f"⚡️ Краткие вопросы: <b>{brief_remains}</b>\n"
            f"📝 Генерация вопросов: <b>{interview_remains}</b>\n\n"
            f"🕒 <i>До сброса лимитов: {time_left_str}</i>")

    keyboard = [
        [InlineKeyboardButton("📖 История заданных вопросов", callback_data="history_asked_0")],
        [InlineKeyboardButton("📝 История сгенерированных вопросов", callback_data="history_generated_0")],
        [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
    ]
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return ConversationHandler.END


@rate_limited
async def ask_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    user_record = await db_manager.fetchrow("SELECT is_subscriber FROM users WHERE user_id = $1", user_id)
    if user_record and user_record['is_subscriber']:
        is_still_subscribed = await check_subscription(user_id, context)
        if not is_still_subscribed:
            await db_manager.execute("UPDATE users SET is_subscriber = FALSE WHERE user_id = $1", user_id)
            logging.info(f"User {user_id} отписался от канала. Бонусы деактивированы.")

    is_from_answer = False
    last_answer_id = context.user_data.get('last_answer_message_id')
    if last_answer_id and query.message.message_id == last_answer_id:
        await query.edit_message_reply_markup(reply_markup=None)
        is_from_answer = True

    context.user_data.clear()
    context.user_data['is_new_conversation'] = True

    is_banned = await db_manager.fetchval("SELECT is_banned FROM users WHERE user_id = $1", user_id)
    if is_banned:
        text = "Вы заблокированы и не можете использовать бота."
        if is_from_answer:
            await context.bot.send_message(chat_id=query.message.chat_id, text=text)
        else:
            await query.edit_message_text(text)
        return ConversationHandler.END

    enabled_servers = await get_enabled_servers()
    if not enabled_servers:
        text = "На данный момент нет доступных для консультации серверов."
        if is_from_answer:
            await context.bot.send_message(chat_id=query.message.chat_id, text=text)
        else:
            await query.edit_message_text(text)
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton(config.SERVERS_CONFIG[server_id]['display_name'],
                                      callback_data=f"select_server_{server_id}")] for server_id in enabled_servers]
    keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data="main_menu")])
    text = "Пожалуйста, выберите сервер:"
    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_from_answer:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup)
    else:
        await query.edit_message_text(text=text, reply_markup=reply_markup)

    return CHOOSING_SERVER


async def ask_answer_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['server_id'] = query.data.split("select_server_", 1)[1]
    logging.info(f"Сохранен server_id: {context.user_data['server_id']}")
    return await prompt_for_optimization_choice(update, context)


async def prompt_for_optimization_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    text = (
        "🔍 **Оптимизация вопроса**\n\n"
        "Эта функция помогает улучшить ваш вопрос, чтобы вы получили максимально точный ответ. "
        "AI-помощник может задать 1-2 уточняющих вопроса.\n\n"
        "**Когда это полезно?**\n"
        "Если ваш вопрос сложный, нечеткий или вы используете игровой сленг (ДМ, ДБ, ПГ и т.д.).\n\n"
        "**Когда можно пропустить?**\n"
        "Если ваш вопрос уже четко сформулирован (например, *'Какой штраф за проезд на красный свет?'*)."
    )
    keyboard = [
        [InlineKeyboardButton("🤖 Оптимизировать мой вопрос", callback_data="set_opt_True")],
        [InlineKeyboardButton("✍️ Задать вопрос напрямую", callback_data="set_opt_False")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return AWAITING_OPTIMIZATION_CHOICE


async def set_optimization_and_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.split('_')[-1]
    context.user_data['skip_optimization'] = (choice == 'False')
    keyboard = [
        [InlineKeyboardButton("📜 Подробный", callback_data="select_type_detailed"),
         InlineKeyboardButton("⚡️ Краткий", callback_data="select_type_brief")],
        [InlineKeyboardButton("◀️ Назад (выбор сервера)", callback_data="start_question")]
    ]
    await query.edit_message_text(text="Выберите тип ответа:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_ANSWER_TYPE


async def prompt_for_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['question_type'] = query.data.split('_')[-1]
    server_id = context.user_data.get('server_id')
    if not server_id:
        logging.error("Критическая ошибка: server_id не найден в prompt_for_question.")
        await query.edit_message_text("Произошла ошибка, начните диалог заново с /start.")
        return ConversationHandler.END

    server_name = config.server_display_name(server_id)
    q_type_text = "Подробный" if context.user_data['question_type'] == 'detailed' else 'Краткий'
    opt_text = "Без оптимизации" if context.user_data.get('skip_optimization') else "С оптимизацией"
    await query.edit_message_text(
        text=f"Выбран сервер: *{server_name}*.\nТип ответа: *{q_type_text}*.\nРежим: *{opt_text}*.\n\n"
             "Теперь задавайте свой вопрос в чат (текстом или голосом). Для отмены нажмите /start.",
        parse_mode="Markdown")
    return AWAITING_QUESTION


async def send_answer_with_clarify_button(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                          text: str) -> Message | None:
    logging.info(f"Получен сырой HTML от Gemini (длина: {len(text)}). Начинаю очистку и отправку.")
    if len(text) > 1500:
        logging.debug(f"СЫРОЙ HTML ОТ GEMINI (фрагмент):\n---\n{text[:1500]}...\n---")
    else:
        logging.debug(f"СЫРОЙ HTML ОТ GEMINI:\n---\n{text}\n---")
    cleaned_text = await sanitize_telegram_html(text, context)
    parts = _split_long_message(cleaned_text, limit=4096)
    sent_message = None
    keyboard = [
        [InlineKeyboardButton("💬 Уточнить", callback_data="clarify_question")],
        [InlineKeyboardButton("🔄 Начать новый диалог", callback_data="start_question")],
        [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    for i, part in enumerate(parts):
        if not part.strip():
            continue
        if i == len(parts) - 1:
            sent_message = await context.bot.send_message(chat_id=chat_id, text=part, parse_mode='HTML',
                                                          reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=chat_id, text=part, parse_mode='HTML')
        await asyncio.sleep(0.5)
    logging.info(f"Ответ пользователю {chat_id} успешно отправлен (состоял из {len(parts)} частей).")
    return sent_message


async def send_error_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    keyboard = [[InlineKeyboardButton("🚨 Сообщить о проблеме / Задать вопрос", callback_data="start_report_dialog")]]
    await context.bot.send_message(chat_id=chat_id,
                                   text="Прошу прощения, при обработке вашего запроса произошла внутренняя ошибка.",
                                   reply_markup=InlineKeyboardMarkup(keyboard))


def contains_emoji_or_bad_symbols(text):
    if emoji.emoji_count(text) > 0: return True
    return bool(re.search(r'[^\w\s\d.,:;!?()\[\]{}\'"«»\-—_=+*@#№$%^&/\\|~`]', text))


async def _run_optimization_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> int:
    processing_message = await update.effective_message.reply_text("Обрабатываю...")
    chat_id = update.effective_chat.id
    try:
        system_instruction = get_optimizer_prompt()
        optimizer_model = GenerativeModel(QUESTION_OPTIMIZER_MODEL_NAME, system_instruction=system_instruction)
        optimizer_history_raw = context.user_data.get('optimizer_chat_history', [])
        optimizer_gemini_history = []
        for msg in optimizer_history_raw:
            role = msg['role']
            content = msg['parts'][0]
            if role == 'model':
                try:
                    model_response_data = json.loads(content)
                    text_part = model_response_data.get('text', '')
                    optimizer_gemini_history.append(Content(role=role, parts=[Part.from_text(text_part)]))
                except (json.JSONDecodeError, KeyError):
                    optimizer_gemini_history.append(Content(role=role, parts=[Part.from_text(content)]))
            else:
                optimizer_gemini_history.append(Content(role=role, parts=[Part.from_text(content)]))

        chat_session = optimizer_model.start_chat(history=optimizer_gemini_history)
        logging.info(f"[OPTIMIZER] Отправка запроса в {QUESTION_OPTIMIZER_MODEL_NAME} для пользователя {chat_id}.")
        safety_settings_config = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        response = await chat_session.send_message_async(
            user_text,
            generation_config={"response_mime_type": "application/json"},
            safety_settings=safety_settings_config
        )

        try:
            clean_response_text = re.sub(r'```json\s*|\s*```', '', response.text, flags=re.DOTALL).strip()
            data = json.loads(clean_response_text)
            status = data['status']
            text = data['text']
            logging.info(f"[OPTIMIZER] Получен корректный JSON от модели: status='{status}'")
        except (json.JSONDecodeError, KeyError) as e:
            logging.error(f"[OPTIMIZER] Ошибка парсинга JSON от модели для {chat_id}. Ответ: '{response.text}'",
                          exc_info=e)
            await processing_message.edit_text(
                "Произошла ошибка при уточнении вопроса. Использую ваш первоначальный запрос.")
            original_question = context.user_data.get('original_question', user_text)
            asyncio.create_task(
                run_question_processing_task(context, chat_id, original_question,
                                             context.user_data['question_type'], context.user_data['server_id'],
                                             processing_message.message_id)
            )
            return ConversationHandler.END

        optimizer_history_raw.append({'role': 'user', 'parts': [user_text]})
        optimizer_history_raw.append({'role': 'model', 'parts': [clean_response_text]})
        context.user_data['optimizer_chat_history'] = optimizer_history_raw

        if status == 'clarification_needed':
            await processing_message.edit_text(text)
            return OPTIMIZING_QUESTION

        elif status == 'optimization_complete':
            context.user_data['optimized_question'] = text
            keyboard = [
                [InlineKeyboardButton("✅ Да, всё верно", callback_data="confirm_optimization")],
                [InlineKeyboardButton("✏️ Отредактировать", callback_data="edit_optimization")]
            ]
            await processing_message.edit_text(
                text=f"🤖 Я оптимизировал ваш вопрос:\n\n<i>«{text}»</i>\n\nПродолжаем с этой версией?",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return AWAITING_OPTIMIZATION_CONFIRMATION

    except Exception as e:
        logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА в _run_optimization_step для {chat_id}.", exc_info=e)
        await processing_message.delete()
        await send_error_message(context, chat_id)
        return ConversationHandler.END
    return ConversationHandler.END


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return AWAITING_QUESTION
    user_question = update.message.text
    if contains_emoji_or_bad_symbols(user_question):
        await update.message.reply_text("Ваш вопрос содержит недопустимые символы или эмодзи.")
        return AWAITING_QUESTION

    chat_id = update.effective_chat.id
    q_type = context.user_data.get('question_type', 'detailed')
    can_ask, _ = await check_and_update_usage(chat_id, q_type)
    if not can_ask:
        await update.message.reply_text(f"Вы достигли дневного лимита на этот тип вопросов.")
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END

    server_id = context.user_data.get('server_id')
    if not server_id:
        await update.message.reply_text("Произошла ошибка: не выбран сервер. Начните заново с /start.")
        return ConversationHandler.END

    if context.user_data.get('skip_optimization', False):
        processing_message = await update.message.reply_text("Ваш запрос принят. Готовлю консультацию...")
        asyncio.create_task(
            run_question_processing_task(context, chat_id, user_question, q_type, server_id,
                                         processing_message.message_id)
        )
        return ConversationHandler.END
    else:
        context.user_data['original_question'] = user_question
        context.user_data['optimizer_chat_history'] = []
        return await _run_optimization_step(update, context, user_question)


async def handle_user_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return OPTIMIZING_QUESTION
    user_answer = update.message.text
    return await _run_optimization_step(update, context, user_answer)


async def handle_confirm_optimization(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    optimized_question = context.user_data.get('optimized_question')

    if not optimized_question:
        await query.edit_message_text("Произошла ошибка, оптимизированный вопрос не найден. Начните заново.")
        return ConversationHandler.END

    await query.edit_message_text("✅ Принято. Готовлю консультацию...")

    asyncio.create_task(
        run_question_processing_task(context, chat_id, optimized_question,
                                     context.user_data['question_type'], context.user_data['server_id'],
                                     query.message.message_id)
    )
    return ConversationHandler.END


async def handle_edit_optimization_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    optimized_question = context.user_data.get('optimized_question')

    if optimized_question:
        text_for_user = (
            "Хорошо. Скопируйте текст ниже, исправьте его и отправьте в чат:\n\n"
            f"<pre>{optimized_question}</pre>"
        )
        await query.edit_message_text(
            text_for_user,
            parse_mode='HTML'
        )
    else:
        await query.edit_message_text(
            "Произошла ошибка, текст вопроса не найден. Пожалуйста, отправьте исправленную версию."
        )
        logging.warning(f"optimized_question не найден в context.user_data для пользователя {query.from_user.id}")

    return AWAITING_EDITED_OPTIMIZED_QUESTION


async def handle_edited_optimized_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        return AWAITING_EDITED_OPTIMIZED_QUESTION

    chat_id = update.effective_chat.id
    edited_question = update.message.text

    processing_message = await update.message.reply_text("✅ Принято. Готовлю консультацию по вашей версии вопроса...")

    asyncio.create_task(
        run_question_processing_task(context, chat_id, edited_question,
                                     context.user_data['question_type'], context.user_data['server_id'],
                                     processing_message.message_id)
    )
    return ConversationHandler.END


async def handle_voice_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.voice: return AWAITING_QUESTION
    current_state = context.user_data.get('current_state', AWAITING_QUESTION)
    status_msg = await update.message.reply_text("Получил голосовое сообщение, начинаю распознавание...")
    try:
        voice_file = await update.message.voice.get_file()
        audio_bytes = await voice_file.download_as_bytearray()
        user_question = await transcribe_voice(bytes(audio_bytes))
        if not user_question:
            await status_msg.edit_text(
                "Не удалось распознать речь. Пожалуйста, попробуйте еще раз или напишите вопрос текстом.")
            return current_state
        context.user_data['transcribed_text'] = user_question
        context.user_data['voice_origin_state'] = current_state
        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_transcription")],
            [InlineKeyboardButton("🔄 Перезаписать", callback_data="rerecord_transcription")],
            [InlineKeyboardButton("✏️ Отредактировать", callback_data="edit_transcription")]
        ]
        await status_msg.edit_text(
            f"<i>Я распознал ваш вопрос как:</i>\n\n«{user_question}»\n\nВсё верно?",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
        )
        return AWAITING_TRANSCRIPTION_CONFIRMATION
    except Exception as e:
        logging.error("Ошибка при обработке голосового сообщения.", exc_info=e)
        await status_msg.edit_text("Произошла ошибка при обработке вашего голосового сообщения. Попробуйте снова.")
        return current_state


async def handle_confirm_transcription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_question = context.user_data.get('transcribed_text')
    if not user_question:
        await query.edit_message_text("Произошла ошибка, текст не найден. Пожалуйста, начните заново.")
        return ConversationHandler.END
    mock_update = Update(update.update_id, message=query.message)
    mock_update.message.text = user_question
    await query.delete_message()
    return await handle_question(mock_update, context)


async def handle_rerecord_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    origin_state = context.user_data.get('voice_origin_state', AWAITING_QUESTION)
    await query.edit_message_text("Хорошо, я слушаю. Пожалуйста, отправьте новое голосовое сообщение.")
    return origin_state


async def handle_edit_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    await query.edit_message_text("Хорошо. Пожалуйста, отправьте исправленный текст вашего вопроса в чат.",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_EDITED_TRANSCRIPTION


async def back_to_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_question = context.user_data.get('transcribed_text')
    if not user_question:
        await query.edit_message_text("Произошла ошибка, распознанный текст не найден. Пожалуйста, начните заново.")
        return ConversationHandler.END
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_transcription")],
        [InlineKeyboardButton("🔄 Перезаписать", callback_data="rerecord_transcription")],
        [InlineKeyboardButton("✏️ Отредактировать", callback_data="edit_transcription")]
    ]
    await query.edit_message_text(
        f"<i>Я распознал ваш вопрос как:</i>\n\n«{user_question}»\n\nВсё верно?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return AWAITING_TRANSCRIPTION_CONFIRMATION


async def handle_edited_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return AWAITING_EDITED_TRANSCRIPTION
    return await handle_question(update, context)


async def run_question_processing_task(context, chat_id, user_question, q_type, server_id, processing_message_id):
    task_start_time = asyncio.get_running_loop().time()
    logging.info(f"TASK_START: Запущена задача обработки вопроса для пользователя {chat_id}.")
    try:
        await process_question_and_reply(context, chat_id, user_question, q_type, server_id)
    except Exception:
        pass
    finally:
        try:
            if processing_message_id != context.user_data.get('last_answer_message_id'):
                await context.bot.delete_message(chat_id=chat_id, message_id=processing_message_id)
        except (BadRequest, AttributeError):
            pass
        task_duration = (asyncio.get_running_loop().time() - task_start_time) * 1000
        logging.info(
            f"TASK_END: Задача обработки вопроса для пользователя {chat_id} завершена за {task_duration:.2f} мс.")


async def trigger_knowledge_base_update(context: ContextTypes.DEFAULT_TYPE, server_id: str):
    updating_servers = context.bot_data.get('updating_servers', set())
    if server_id in updating_servers: return
    try:
        updating_servers.add(server_id)
        logging.info(f"Запущено фоновое обновление БЗ для сервера {server_id} из-за ошибки доступа.")
        await knowledge_base_manager.update_knowledge_base_for_server(server_id)
    finally:
        updating_servers.discard(server_id)


async def debug_check_gcs_permissions(gcs_uri: str):
    try:
        logging.info(f"DEBUG_GCS: Проверка доступа к файлу: {gcs_uri}")
        if not gcs_uri.startswith("gs://"):
            logging.warning(f"DEBUG_GCS: Некорректный URI, пропуск проверки: {gcs_uri}")
            return
        storage_client = storage.Client.from_service_account_json(config.VERTEX_CREDENTIALS_FILE)
        bucket_name, blob_name = gcs_uri.replace("gs://", "").split("/", 1)
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        await asyncio.to_thread(blob.reload)
        logging.info(f"DEBUG_GCS: УСПЕХ. Метаданные для {gcs_uri} успешно получены.")
    except google_exceptions.NotFound:
        logging.error(f"DEBUG_GCS: ОШИБКА. Файл не найден по пути: {gcs_uri}")
    except google_exceptions.Forbidden as e:
        logging.error(
            f"DEBUG_GCS: КРИТИЧЕСКАЯ ОШИБКА ДОСТУПА. У сервисного аккаунта бота нет прав на чтение файла {gcs_uri}. Ошибка: {e}")
    except Exception as e:
        logging.error(f"DEBUG_GCS: Неизвестная ошибка при проверке доступа к {gcs_uri}.", exc_info=e)


async def process_question_and_reply(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_question: str, q_type: str,
                                     server_id: str):
    """
    >>> ИЗМЕНЕНИЕ <<<
    Удалено условие `is_active = TRUE` из запроса, так как теперь оно не нужно.
    """
    try:
        if not await reinitialize_vertex_ai():
            await context.bot.send_message(chat_id=chat_id,
                                           text="Произошла критическая ошибка аутентификации. Пожалуйста, попробуйте позже или сообщите администратору.")
            return
        is_new_conversation = context.user_data.get('is_new_conversation', False)
        chat_history = []
        if not is_new_conversation:
            chat_history = await load_history(chat_id)
            logging.info(f"Загружено {len(chat_history)} сообщений из истории для уточняющего вопроса от {chat_id}.")
        else:
            logging.info(f"Начат новый диалог для пользователя {chat_id}. История не загружается.")
        relevant_law_uris = await get_relevant_laws(user_question, server_id)

        all_law_records = await db_manager.fetch(
            """
            SELECT gemini_file_uri
            FROM law_documents
            WHERE server_id = $1
              AND gemini_file_uri IS NOT NULL
            """,
            server_id
        )

        files_for_main_query = relevant_law_uris or [r['gemini_file_uri'] for r in all_law_records]
        logging.info(f"DEBUG_MAIN: Список файлов для Gemini ({len(files_for_main_query)} шт): {files_for_main_query}")
        for uri in files_for_main_query: await debug_check_gcs_permissions(uri)
        logging.info("DEBUG_MAIN: Проверка доступа ко всем файлам завершена.")
        if not files_for_main_query:
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"База знаний для сервера '{config.SERVERS_CONFIG[server_id]['display_name']}' пуста или временно недоступна.")
            return
        system_instruction = get_system_prompt(q_type)
        main_model = GenerativeModel(LEGAL_CONSULTANT_MODEL_NAME, system_instruction=system_instruction)
        content_parts = [Part.from_uri(uri, mime_type="text/plain") for uri in files_for_main_query]
        content_parts.append(Part.from_text(user_question))
        chat_session = main_model.start_chat(history=format_history_for_prompt(chat_history))
        main_model_start_time = asyncio.get_running_loop().time()
        logging.info(
            f"[CONSULTANT] Этап 2: Отправка запроса в основную модель ({LEGAL_CONSULTANT_MODEL_NAME}) для пользователя {chat_id}. Используется файлов: {len(files_for_main_query)}.")
        response = await chat_session.send_message_async(content_parts)
        main_model_duration = (asyncio.get_running_loop().time() - main_model_start_time) * 1000
        logging.info(f"[CONSULTANT] Этап 2: Успешно получен ответ от основной модели за {main_model_duration:.2f} мс.")
        sent_message = await send_answer_with_clarify_button(context, chat_id, response.text)
        if sent_message: context.user_data['last_answer_message_id'] = sent_message.message_id
        await save_history(chat_id, server_id, 'user', user_question)
        await save_history(chat_id, server_id, 'model', response.text)
        await log_to_google_sheet(chat_id, f"[{server_id}] {user_question}", response.text, q_type)
        context.user_data['is_new_conversation'] = False
    except Exception as e:
        logging.error(f"КРИТИЧЕСКАЯ ОШИБКА в `process_question_and_reply` для пользователя {chat_id}.", exc_info=e)
        await send_error_message(context, chat_id)
        raise


async def request_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if not context.user_data.get('server_id'):
        await query.edit_message_reply_markup(reply_markup=None)
        await context.bot.send_message(chat_id=query.message.chat_id,
                                       text="Контекст этого диалога был утерян. Пожалуйста, начните новый диалог.")
        return ConversationHandler.END
    await query.edit_message_reply_markup(reply_markup=None)
    await context.bot.send_message(chat_id=query.message.chat_id,
                                   text="Пожалуйста, задайте ваш уточняющий вопрос (текстом или голосом).")
    context.user_data['current_state'] = AWAITING_CLARIFICATION
    return AWAITING_CLARIFICATION


async def handle_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text: return AWAITING_CLARIFICATION
    user_question = update.message.text
    if contains_emoji_or_bad_symbols(user_question):
        await update.message.reply_text("Ваш вопрос содержит недопустимые символы или эмодзи.")
        return AWAITING_CLARIFICATION
    chat_id = update.effective_chat.id
    q_type = context.user_data.get('question_type')
    server_id = context.user_data.get('server_id')
    if not q_type or not server_id:
        await update.message.reply_text("Произошла ошибка, контекст диалога утерян. Начните заново с /start.")
        return ConversationHandler.END
    can_ask, _ = await check_and_update_usage(chat_id, q_type)
    if not can_ask:
        await update.message.reply_text(f"Вы достигли дневного лимита на этот тип вопросов.")
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END
    processing_message = await update.message.reply_text("Принято. Обрабатываю уточнение...")
    asyncio.create_task(
        run_question_processing_task(context, chat_id, user_question, q_type, server_id, processing_message.message_id))
    return ConversationHandler.END


@rate_limited
async def start_question_generation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    enabled_servers = await get_enabled_servers()
    if not enabled_servers:
        await query.edit_message_text("На данный момент нет доступных серверов.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(config.SERVERS_CONFIG[server_id]['display_name'],
                                      callback_data=f"interview_server_{server_id}")] for server_id in enabled_servers]
    keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data="main_menu")])
    await query.edit_message_text("Для какого сервера генерируем вопросы?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_INTERVIEW_SERVER


async def ask_interview_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['interview_server_id'] = query.data.split("interview_server_", 1)[1]
    keyboard = [[InlineKeyboardButton(name, callback_data=f"interview_role_{key}")] for key, name in
                INTERVIEW_ROLES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад (выбор сервера)", callback_data="start_interview_gen")])
    await query.edit_message_text("Выберите должность:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_INTERVIEW_ROLE


async def ask_interview_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['interview_role_key'] = query.data.split("interview_role_", 1)[1]
    keyboard = [[InlineKeyboardButton(name, callback_data=f"interview_diff_{key}")] for key, name in
                INTERVIEW_DIFFICULTIES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад",
                                          callback_data=f"interview_server_{context.user_data['interview_server_id']}")])
    await query.edit_message_text("Выберите уровень сложности:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_INTERVIEW_DIFFICULTY


async def prompt_for_interview_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['interview_diff_key'] = query.data.split("interview_diff_", 1)[1]
    keyboard = [
        [InlineKeyboardButton("◀️ Назад", callback_data=f"interview_role_{context.user_data['interview_role_key']}")]]
    await query.edit_message_text(f"Введите количество вопросов (от {MIN_QUESTIONS} до {MAX_QUESTIONS}):",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_INTERVIEW_QUANTITY


async def generate_interview_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    try:
        quantity = int(message.text)
        if not (MIN_QUESTIONS <= quantity <= MAX_QUESTIONS): raise ValueError
    except (ValueError, TypeError):
        await message.reply_text(f"Введите корректное число от {MIN_QUESTIONS} до {MAX_QUESTIONS}.")
        return AWAITING_INTERVIEW_QUANTITY
    can_ask, _ = await check_and_update_usage(update.effective_chat.id, 'interview')
    if not can_ask:
        await message.reply_text(f"Вы достигли дневного лимита на генерацию вопросов.")
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END
    processing_message = await message.reply_text("✅ Запрос принят. Генерирую вопросы...")
    asyncio.create_task(process_and_send_interview_questions(context, update.effective_chat.id, quantity,
                                                             processing_message.message_id))
    return ConversationHandler.END


async def process_and_send_interview_questions(context: ContextTypes.DEFAULT_TYPE, chat_id: int, quantity: int,
                                               processing_message_id: int):
    """
    >>> ИЗМЕНЕНИЕ <<<
    Удалено условие `is_active = TRUE` из запроса, так как теперь оно не нужно.
    """
    task_start_time = asyncio.get_running_loop().time()
    logging.info(f"TASK_START: Запущена задача генерации вопросов для {chat_id}.")
    try:
        if not await reinitialize_vertex_ai():
            await context.bot.send_message(chat_id,
                                           text="Произошла критическая ошибка аутентификации. Пожалуйста, попробуйте позже или сообщите администратору.")
            return
        server_id = context.user_data['interview_server_id']
        role_key = context.user_data['interview_role_key']
        diff_key = context.user_data['interview_diff_key']
        server_name = config.server_display_name(server_id)
        role_name = INTERVIEW_ROLES.get(role_key, str(role_key))
        diff_name = INTERVIEW_DIFFICULTIES.get(diff_key, str(diff_key))

        all_law_records = await db_manager.fetch(
            """
            SELECT gemini_file_uri
            FROM law_documents
            WHERE server_id = $1
              AND gemini_file_uri IS NOT NULL
            """,
            server_id
        )

        files_for_query = [r['gemini_file_uri'] for r in all_law_records]
        if not files_for_query:
            await context.bot.send_message(chat_id,
                                           f"Ошибка: база знаний для сервера '{server_name}' пуста или временно недоступна.")
            return
        system_prompt = get_interview_questions_prompt(role_name, diff_name, quantity, server_name)
        model = GenerativeModel(QUESTION_GENERATOR_MODEL_NAME, system_instruction=system_prompt)
        content_parts = [Part.from_uri(uri, mime_type="text/plain") for uri in files_for_query]
        max_retries = 3
        retry_delay_seconds = 5
        response = None
        for attempt in range(max_retries):
            try:
                logging.info(
                    f"[GENERATOR] Попытка {attempt + 1}/{max_retries}: Отправка запроса в {QUESTION_GENERATOR_MODEL_NAME} для {chat_id}.")
                response = await model.generate_content_async(content_parts)
                logging.info(f"[GENERATOR] Попытка {attempt + 1} успешна.")
                break
            except google_exceptions.Unknown as e:
                logging.warning(f"[GENERATOR] Попытка {attempt + 1} провалилась с сетевой ошибкой: {e}.")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay_seconds)
                else:
                    raise
        if not response: raise Exception("Не удалось получить ответ от модели после всех попыток.")
        header = (f"<b>📝 Сгенерированные вопросы для собеседования</b>\n\n"
                  f"<b>Сервер:</b> {server_name}\n"
                  f"<b>Должность:</b> {role_name}\n"
                  f"<b>Сложность:</b> {diff_name}\n\n---")
        full_response_text = f"{header}\n\n{response.text}"
        sent_message = await send_answer_with_clarify_button(context, chat_id, full_response_text)
        if sent_message: context.user_data['last_answer_message_id'] = sent_message.message_id
        user_request_text = f"Запрос на генерацию вопросов: {role_name} ({diff_name}, {quantity} шт.)"
        prefixed_request = f"{GENERATION_HISTORY_PREFIX}{user_request_text}"
        await save_history(chat_id, server_id, 'user', prefixed_request)
        await save_history(chat_id, server_id, 'model', full_response_text)
    except Exception as e:
        logging.critical(
            f"КРИТИЧЕСКАЯ ОШИБКА в `process_and_send_interview_questions` для {chat_id} после всех попыток.",
            exc_info=e)
        await send_error_message(context, chat_id)
    finally:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=processing_message_id)
        except BadRequest:
            pass
        task_duration = (asyncio.get_running_loop().time() - task_start_time) * 1000
        logging.info(f"TASK_END: Задача генерации вопросов для {chat_id} завершена за {task_duration:.2f} мс.")


@rate_limited
async def start_report_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отображает главное меню системы репортов."""
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("➕ Создать новый репорт", callback_data="report_create_new")],
        [InlineKeyboardButton("📂 Мои репорты", callback_data="report_view_mine")],
        [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
    ]
    await query.edit_message_text(
        text="⚙️ **Система поддержки**\n\nЗдесь вы можете сообщить о проблеме, ошибке в работе бота или задать вопрос напрямую администрации.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return CHOOSING_REPORT_ACTION


async def prompt_for_report_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает у пользователя первое сообщение для нового репорта."""
    query = update.callback_query
    await query.answer()
    keyboard = [[InlineKeyboardButton("◀️ Отмена", callback_data="start_report_dialog")]]
    await query.edit_message_text(
        text="Пожалуйста, опишите вашу проблему или задайте вопрос одним сообщением. Это сообщение станет заголовком вашего обращения.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return AWAITING_REPORT_TITLE


async def create_new_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Создает новый репорт в БД на основе сообщения пользователя."""
    user = update.effective_user
    report_title = update.message.text

    if not report_title or len(report_title) < 10:
        await update.message.reply_text("Текст репорта слишком короткий. Пожалуйста, опишите проблему подробнее.")
        return AWAITING_REPORT_TITLE

    report_id = await db_manager.fetchval(
        "INSERT INTO reports (user_id, title) VALUES ($1, $2) RETURNING id;",
        user.id, report_title
    )

    await db_manager.execute(
        "INSERT INTO report_messages (report_id, sender_id, sender_role, content) VALUES ($1, $2, 'user', $3);",
        report_id, user.id, report_title
    )

    logging.info(f"Пользователь {user.id} создал новый репорт #{report_id}.")
    await update.message.reply_text(
        f"✅ Ваш репорт #{report_id} успешно создан. Администрация ответит вам в ближайшее время. Вы получите уведомление."
    )

    admin_records = await db_manager.fetch("SELECT user_id FROM users WHERE is_admin = TRUE OR user_id = $1",
                                           SUPER_ADMIN_ID)
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
    """Отображает список репортов пользователя. Может вызываться глобально."""
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

    # Кнопка всегда ведет в главное меню, так как это более надежная точка выхода
    keyboard.append([InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")])

    try:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except (AttributeError, BadRequest):
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
    """Отображает диалог по конкретному репорту для пользователя и кнопки действий."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        report_id = int(query.data.split('_')[-1])
    except (ValueError, IndexError):
        return VIEWING_USER_REPORTS

    context.user_data['current_report_id'] = report_id

    report_info = await db_manager.fetchrow("SELECT id, status FROM reports WHERE id = $1 AND user_id = $2", report_id,
                                            user_id)
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
    """Запрашивает у пользователя текст ответа в репорте."""
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
    """Обрабатывает новое сообщение от пользователя в открытом репорте и завершает диалог."""
    user_id = update.effective_user.id
    report_id = context.user_data.get('current_report_id')
    message_text = update.message.text

    if not report_id:
        await update.message.reply_text("Ошибка: не удалось определить текущий репорт. Начните заново.")
        return ConversationHandler.END

    await db_manager.execute(
        "INSERT INTO report_messages (report_id, sender_id, sender_role, content) VALUES ($1, $2, 'user', $3);",
        report_id, user_id, message_text
    )
    await db_manager.execute("UPDATE reports SET last_message_at = NOW() WHERE id = $1;", report_id)

    await update.message.reply_text("✅ Ваше сообщение отправлено администрации.")

    admin_records = await db_manager.fetch("SELECT user_id FROM users WHERE is_admin = TRUE OR user_id = $1",
                                           SUPER_ADMIN_ID)
    admin_ids = {r['user_id'] for r in admin_records}
    notification_text = f"💬 Новый ответ в репорте #{report_id} от пользователя {update.effective_user.full_name} (`{user_id}`)."
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=notification_text, parse_mode='Markdown')
        except Exception:
            pass

    await _display_main_menu(update, context, is_new_message=True)
    return ConversationHandler.END


@rate_limited
async def view_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        _, history_type, page_str = query.data.split('_')
        page = int(page_str)
    except (ValueError, IndexError):
        return VIEWING_HISTORY
    offset = page * HISTORY_PAGE_SIZE
    if history_type == 'generated':
        where_condition = f"content LIKE '{GENERATION_HISTORY_PREFIX}%'"
        title_text = "📝 История сгенерированных вопросов"
    else:
        where_condition = f"content NOT LIKE '{GENERATION_HISTORY_PREFIX}%'"
        title_text = "📖 История заданных вопросов"
    total_items = await db_manager.fetchval(
        f"SELECT COUNT(*) FROM message_history WHERE user_id = $1 AND role = 'user' AND {where_condition}",
        user_id) or 0
    total_pages = (total_items + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    history_records = await db_manager.fetch(
        f"SELECT id, content, timestamp FROM message_history WHERE user_id = $1 AND role = 'user' AND {where_condition} ORDER BY timestamp DESC LIMIT $2 OFFSET $3",
        user_id, HISTORY_PAGE_SIZE, offset)
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
    pagination_row = []
    if page > 0: pagination_row.append(
        InlineKeyboardButton("⬅️ Назад", callback_data=f"history_{history_type}_{page - 1}"))
    if (page + 1) < total_pages: pagination_row.append(
        InlineKeyboardButton("Вперед ➡️", callback_data=f"history_{history_type}_{page + 1}"))
    if pagination_row: keyboard.append(pagination_row)
    keyboard.append([InlineKeyboardButton("◀️ Назад в личный кабинет", callback_data="personal_account")])
    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return VIEWING_HISTORY


async def view_history_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    try:
        _, _, history_type, msg_id_str, page_str = query.data.split('_')
        msg_id = int(msg_id_str)
        page = int(page_str)
    except (ValueError, IndexError):
        return VIEWING_HISTORY
    question_record = await db_manager.fetchrow(
        "SELECT content, timestamp FROM message_history WHERE id = $1 AND user_id = $2 AND role = 'user'", msg_id,
        user_id)
    if not question_record:
        await query.edit_message_text("Запрошенный диалог не найден.")
        return VIEWING_HISTORY
    answer_record = await db_manager.fetchrow(
        "SELECT content FROM message_history WHERE user_id = $1 AND role = 'model' AND timestamp > $2 ORDER BY timestamp ASC LIMIT 1",
        user_id, question_record['timestamp'])
    question_text = question_record['content'].replace(GENERATION_HISTORY_PREFIX, '')
    answer_text = answer_record[
        'content'] if answer_record else "<i>[На этот запрос не было получено ответа или произошла ошибка]</i>"
    final_text = (f"<b><u>Ваш запрос:</u></b>\n<i>«{question_text}»</i>\n\n---\n\n<b><u>Ответ:</u></b>\n{answer_text}")
    keyboard = [
        [InlineKeyboardButton(f"◀️ Назад к списку (стр. {page + 1})", callback_data=f"history_{history_type}_{page}")]]
    limit = 4096
    if len(final_text) > limit:
        final_text = final_text[:limit - 4] + "..."
    await query.edit_message_text(text=final_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML',
                                  disable_web_page_preview=True)
    return VIEWING_HISTORY_MESSAGE

# END OF FILE user_handlers.py