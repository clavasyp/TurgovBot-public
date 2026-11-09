# START OF FILE user_handlers/qa_flow.py

"""
Модуль для управления основным диалогом "Задать вопрос".

Это ядро бота, реализующее Retrieval-Augmented Generation (RAG).
Содержит:
- ConversationHandler для полного цикла: выбор сервера -> выбор типа ответа -> вопрос -> ответ.
- Логику оптимизации вопроса с помощью AI.
- Обработку голосовых сообщений и их транскрипцию.
- Функции для вызова AI-моделей (роутера и основной) для получения ответа.
- Обработку уточняющих вопросов.
"""

import asyncio
import logging
import json
import re
import emoji
import asyncpg

from google import genai
from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions
from google.cloud import storage
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters

# Локальные импорты
import config
import knowledge_base_manager
from database.manager import db_manager
from utils import get_enabled_servers, get_enabled_projects, send_project_selection_menu, rate_limited, sanitize_telegram_html, reinitialize_vertex_ai, retry_with_exponential_backoff, safe_answer_callback_query
# ВРЕМЕННО ОТКЛЮЧЕНО: from google_sheets_handler import log_to_google_sheet
from prompts import get_system_prompt, get_router_prompt, get_optimizer_prompt
# ВРЕМЕННО ОТКЛЮЧЕНО: from voice_processor import transcribe_voice
from subscription_manager import check_subscription
from .core import main_menu, start, check_and_update_usage, send_error_message, personal_account, _display_main_menu, unknown_text_fallback
from .ai_retry import register_retry, retry_keyboard
# Импорт модуля прецедентов
from precedents_manager import get_relevant_precedents
from config import (
    CHOOSING_PROJECT, CHOOSING_SERVER, CHOOSING_ANSWER_TYPE, AWAITING_QUESTION,
    AWAITING_CLARIFICATION,
    AWAITING_TRANSCRIPTION_CONFIRMATION, AWAITING_EDITED_TRANSCRIPTION,
    OPTIMIZING_QUESTION,
    AWAITING_OPTIMIZATION_CHOICE, AWAITING_OPTIMIZATION_CONFIRMATION, AWAITING_EDITED_OPTIMIZED_QUESTION,
    ROUTER_MODEL_NAME, LEGAL_CONSULTANT_MODEL_NAME, QUESTION_OPTIMIZER_MODEL_NAME,
    HISTORY_LIMIT, PROJECTS_CONFIG
)
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error


QUESTION_TYPE_LABELS = {
    'detailed': 'Подробный',
    'brief': 'Краткий',
    'free': 'Свободный',
}
VALID_QUESTION_TYPES = set(QUESTION_TYPE_LABELS)


# --- Вспомогательные функции, используемые в этом модуле ---

def _get_question_type_label(question_type: str | None) -> str:
    return QUESTION_TYPE_LABELS.get(question_type or '', 'Подробный')


async def _load_valid_question_defaults(user_id: int) -> dict | None:
    """Загружает и валидирует сохраненный шаблон вопроса пользователя."""
    try:
        record = await db_manager.fetchrow(
            """
            SELECT default_project_id, default_server_id, default_question_type
            FROM users
            WHERE user_id = $1
            """,
            user_id
        )
    except asyncpg.exceptions.UndefinedColumnError:
        logging.warning("[QA_DEFAULTS] Колонки шаблона вопросов отсутствуют. Использую старый сценарий выбора.")
        return None
    if not record:
        return None

    project_id = record.get('default_project_id')
    server_id = record.get('default_server_id')
    question_type = record.get('default_question_type')
    if not project_id or not server_id or question_type not in VALID_QUESTION_TYPES:
        return None

    enabled_projects = await get_enabled_projects()
    if project_id not in enabled_projects:
        return None

    enabled_servers = await get_enabled_servers(project_id)
    if server_id not in enabled_servers:
        return None

    return {
        'project_id': project_id,
        'server_id': server_id,
        'question_type': question_type,
    }


async def _save_question_defaults(user_id: int, project_id: str, server_id: str, question_type: str) -> None:
    """Сохраняет шаблон вопроса пользователя."""
    try:
        await db_manager.execute(
            """
            UPDATE users
            SET default_project_id = $1,
                default_server_id = $2,
                default_question_type = $3
            WHERE user_id = $4
            """,
            project_id, server_id, question_type, user_id
        )
    except asyncpg.exceptions.UndefinedColumnError as exc:
        raise RuntimeError("question_defaults_schema_missing") from exc


def _apply_question_defaults(context: ContextTypes.DEFAULT_TYPE, defaults: dict) -> None:
    context.user_data['project_id'] = defaults['project_id']
    context.user_data['server_id'] = defaults['server_id']
    context.user_data['question_type'] = defaults['question_type']
    context.user_data['skip_optimization'] = False


def _missing_question_context_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Выбрать параметры", callback_data="choose_question_manually")],
        [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")],
    ])


async def _recover_question_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str] | None:
    question_type = context.user_data.get('question_type')
    server_id = context.user_data.get('server_id')
    if question_type in VALID_QUESTION_TYPES and server_id:
        return question_type, server_id

    user_id = update.effective_user.id if update.effective_user else None
    if user_id:
        defaults = await _load_valid_question_defaults(user_id)
        if defaults:
            _apply_question_defaults(context, defaults)
            return defaults['question_type'], defaults['server_id']

    logging.warning(
        "[QA] Отсутствует контекст вопроса: user_id=%s, chat_id=%s, user_data_keys=%s",
        user_id,
        update.effective_chat.id if update.effective_chat else None,
        sorted(context.user_data.keys()),
    )
    return None


async def _finish_missing_question_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    text = (
        "Сессия вопроса сбилась: не вижу выбранный проект, сервер или формат ответа.\n\n"
        "Выберите параметры заново или вернитесь в главное меню."
    )
    keyboard = _missing_question_context_keyboard()
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard)
    elif update.message:
        await update.message.reply_text(text, reply_markup=keyboard)
    elif update.effective_chat:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=keyboard)
    return ConversationHandler.END


def _get_project_display_name(project_id: str | None) -> str:
    return PROJECTS_CONFIG.get(project_id or '', {}).get('display_name', project_id or 'Не выбран')


def _get_server_display_name(server_id: str | None) -> str:
    return config.SERVERS_CONFIG.get(server_id or '', {}).get('display_name', server_id or 'Не выбран')


async def _show_question_prompt(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        defaults_used: bool = False,
        is_from_answer: bool = False
) -> int:
    """Показывает итоговые параметры и просит пользователя отправить вопрос."""
    query = update.callback_query
    server_id = context.user_data.get('server_id')
    project_id = context.user_data.get('project_id')
    question_type = context.user_data.get('question_type')
    opt_text = "Без оптимизации" if context.user_data.get('skip_optimization') else "С оптимизацией"

    intro = "Использую ваш сохраненный шаблон:" if defaults_used else "Параметры вопроса:"
    text = (
        f"<b>{intro}</b>\n\n"
        f"Проект: <b>{_get_project_display_name(project_id)}</b>\n"
        f"Сервер: <b>{_get_server_display_name(server_id)}</b>\n"
        f"Тип ответа: <b>{_get_question_type_label(question_type)}</b>\n"
        f"Режим: <b>{opt_text}</b>\n\n"
        "Теперь отправьте вопрос текстом. Для отмены нажмите /start."
    )

    reply_markup = None
    if defaults_used:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Выбрать вручную", callback_data="choose_question_manually")],
            [InlineKeyboardButton("⚙️ Изменить шаблон", callback_data="setup_question_defaults")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")],
        ])

    if query:
        if is_from_answer:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        elif query.message and query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                reply_markup=reply_markup,
                parse_mode='HTML'
            )
        else:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
    elif update.message:
        await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode='HTML')

    return AWAITING_QUESTION

async def load_history(chat_id: int) -> list:
    """Загружает историю диалога из БД."""
    query = "SELECT role, content FROM message_history WHERE user_id = $1 ORDER BY timestamp DESC LIMIT $2;"
    records = await db_manager.fetch(query, chat_id, HISTORY_LIMIT * 2)
    return [{'role': r['role'], 'parts': [r['content']]} for r in reversed(records)]


async def save_history(chat_id: int, server_id: str, role: str, content: str):
    """Сохраняет сообщение в историю диалога в БД."""
    query = "INSERT INTO message_history (user_id, server_id, role, content) VALUES ($1, $2, $3, $4);"
    await db_manager.execute(query, chat_id, server_id, role, content)


def format_history_for_prompt(history: list) -> list[types.Content]:
    """Форматирует историю из БД в формат, понятный для Gemini."""
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
        gemini_history.append(types.Content(role=role, parts=[types.Part(text=text_content)]))
    return gemini_history


def _fix_html_tags_in_chunk(chunk: str) -> str:
    """Закрывает незакрытые HTML-теги в чанке и удаляет лишние закрывающие."""
    telegram_tags = ['b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del',
                     'a', 'code', 'pre', 'tg-spoiler']
    open_stack = []
    tag_pattern = re.compile(r'<(/?)(' + '|'.join(telegram_tags) + r')(?:\s[^>]*)?>',  re.IGNORECASE)

    cleaned_parts = []
    last_pos = 0
    for m in tag_pattern.finditer(chunk):
        cleaned_parts.append(chunk[last_pos:m.start()])
        is_closing = m.group(1) == '/'
        tag_name = m.group(2).lower()
        if is_closing:
            if open_stack and open_stack[-1] == tag_name:
                open_stack.pop()
                cleaned_parts.append(m.group(0))
        else:
            open_stack.append(tag_name)
            cleaned_parts.append(m.group(0))
        last_pos = m.end()

    cleaned_parts.append(chunk[last_pos:])
    fixed_chunk = ''.join(cleaned_parts)

    # Закрываем все незакрытые теги в обратном порядке
    for tag_name in reversed(open_stack):
        fixed_chunk += f'</{tag_name}>'
    return fixed_chunk


def _split_long_message(text: str, limit: int = 4096) -> list[str]:
    """Разделяет длинный текст на части, безопасные для Telegram, сохраняя блоки <pre>.
    Каждая часть гарантированно имеет закрытые HTML-теги."""
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
                    if split_pos == -1: split_pos = limit
                    final_chunks.append(part[:split_pos])
                    part = part[split_pos:]
                if part:
                    current_chunk += part
            continue
        current_chunk += part
    if current_chunk:
        final_chunks.append(current_chunk)
    # Закрываем незакрытые HTML-теги в каждом чанке
    final_chunks = [_fix_html_tags_in_chunk(chunk) for chunk in final_chunks if chunk.strip()]
    return final_chunks


async def send_answer_with_clarify_button(context: ContextTypes.DEFAULT_TYPE, chat_id: int,
                                          text: str) -> Message | None:
    """Очищает HTML, разделяет на части и отправляет ответ с кнопками."""
    logging.info(f"Получен сырой HTML от Gemini (длина: {len(text)}). Начинаю очистку и отправку.")
    cleaned_text = sanitize_telegram_html(text)
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


# --- Основная логика RAG ---

async def get_relevant_laws(context: ContextTypes.DEFAULT_TYPE, user_question: str, server_id: str) -> list[dict]:
    """Вызывает AI-роутер для выбора релевантных законов."""
    start_time = asyncio.get_running_loop().time()
    logging.info(f"[ROUTER] Этап 1: Запуск модели-фильтра ({ROUTER_MODEL_NAME}) для сервера {server_id}...")
    try:
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )
        if not law_records:
            logging.warning(f"[ROUTER] В БД отсутствуют документы для сервера {server_id}.")
            return []

        law_map_by_index = {i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']} for i, r in
                            enumerate(law_records)}
        prompt_lines = [f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)]
        law_titles_str = "\n".join(prompt_lines)

        # Используем rate limiter для контроля квот с поддержкой переключения регионов
        async def make_router_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=get_router_prompt(user_question, law_titles_str)
            )

        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,  # Роутер - обычный приоритет
            max_retries=3
        )

        # Отмечаем успех в текущем регионе
        await mark_current_region_success()

        selected_indices = {int(line.strip()) for line in response.text.split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logging.info(
            f"[ROUTER] Этап 1: Успех. Модель-фильтр выбрала {len(relevant_laws)} законов за {duration:.2f} мс.")
        return relevant_laws
    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        # Отмечаем ошибку в текущем регионе для переключения
        await mark_current_region_error()

        # Проверяем, что это действительно 429 ошибка или таймаут
        if isinstance(e, genai_errors.ClientError):
            error_str = str(e)
            if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                raise  # Пробрасываем не-429 ошибки дальше

        logging.warning(f"[ROUTER] Превышен лимит запросов к Vertex AI при фильтрации законов или таймаут: {e}")
        # Возвращаем пустой список, чтобы основная функция использовала все законы
        return []
    except Exception as e:
        logging.error(f"[ROUTER] Этап 1: КРИТИЧЕСКАЯ ОШИБКА. Сбой в работе модели-фильтра.", exc_info=e)
        return []


async def process_question_and_reply(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_question: str, q_type: str,
                                     server_id: str, model_override: str | None = None):
    """Асинхронная задача: выполняет RAG и отправляет ответ.

    model_override — если задано, основной ответ генерируется этой моделью вместо
    LEGAL_CONSULTANT_MODEL_NAME (используется кнопкой «Отправить более слабой
    модели» под сообщением о перегрузе; основная модель в конфиге не меняется).
    """
    status_message = None
    answer_model_name = model_override or LEGAL_CONSULTANT_MODEL_NAME

    try:
        if not await reinitialize_vertex_ai():
            await context.bot.send_message(chat_id=chat_id, text="Произошла критическая ошибка аутентификации.")
            return

        is_new_conversation = context.user_data.get('is_new_conversation', False)
        chat_history = []
        if not is_new_conversation:
            chat_history = await load_history(chat_id)

        # 1. Получаем релевантные законы
        relevant_laws = await get_relevant_laws(context, user_question, server_id)
        files_for_main_query_uris = [item['uri'] for item in relevant_laws] if relevant_laws else \
            [r['gemini_file_uri'] for r in await db_manager.fetch(
                "SELECT gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
                server_id)]

        if not files_for_main_query_uris:
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"База знаний для сервера '{config.SERVERS_CONFIG[server_id]['display_name']}' пуста.")
            return

        # 2. НОВОЕ: Получаем релевантные прецеденты
        genai_client = context.bot_data.get('genai_client')
        relevant_precedents = []
        if genai_client:
            try:
                relevant_precedents = await get_relevant_precedents(genai_client, user_question, server_id)
                if relevant_precedents:
                    logging.info(f"[RAG] Добавлено {len(relevant_precedents)} прецедентов к контексту")
            except Exception as e:
                logging.warning(f"[RAG] Ошибка получения прецедентов: {e}")

        # 3. Формируем части контента (законы + прецеденты + вопрос)
        content_parts = [types.Part.from_uri(file_uri=uri, mime_type="text/plain") for uri in files_for_main_query_uris]

        # Добавляем прецеденты к контексту
        for precedent in relevant_precedents:
            content_parts.append(types.Part.from_uri(file_uri=precedent['uri'], mime_type="text/plain"))

        content_parts.append(types.Part(text=user_question))

        # Формируем историю + новое сообщение
        history = format_history_for_prompt(chat_history)
        current_message = types.Content(role='user', parts=content_parts)
        full_contents = history + [current_message]

        # Определяем функцию для выполнения с rate limiting
        async def send_main_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=answer_model_name,
                contents=full_contents,
                config=types.GenerateContentConfig(
                    system_instruction=get_system_prompt(q_type)
                )
            )

        # Используем rate limiter с КРИТИЧЕСКИМ приоритетом (основной ответ пользователю)
        try:
            response = await execute_vertex_request(
                func=send_main_request,
                model_name=answer_model_name,
                priority=RequestPriority.CRITICAL,  # КРИТИЧЕСКИЙ приоритет для основного ответа
                user_id=chat_id,
                max_retries=3  # Снижаем до 3 попыток, rate limiter уже управляет квотами
            )
            # Отмечаем успех в текущем регионе
            await mark_current_region_success()
        except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
            # Отмечаем ошибку в текущем регионе
            await mark_current_region_error()

            # Проверяем, что это действительно 429 ошибка
            if isinstance(e, genai_errors.ClientError):
                error_str = str(e)
                if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                    raise  # Пробрасываем не-429 ошибки дальше

            # Все попытки исчерпаны или таймаут очереди
            logging.error(f"[RATE_LIMIT] Не удалось выполнить запрос для пользователя {chat_id}: {e}")
            is_weak = (answer_model_name == config.WEAK_FALLBACK_MODEL_NAME)
            token = register_retry(
                context, 'qa',
                {'question': user_question, 'q_type': q_type, 'server_id': server_id},
                weak_allowed=not is_weak,
            )
            overload_text = (
                "⏳ К сожалению, AI-сервис временно перегружен.\n\n"
                "Система управления нагрузкой не смогла обработать ваш запрос.\n\n"
                "Нажмите «🔄 Повторить» — возможно, лимит был временным."
            )
            if not is_weak:
                overload_text += "\nИли «⚡ Отправить более слабой модели» — она обычно свободнее."
            overload_text += "\n\nПриносим извинения за неудобства."
            await context.bot.send_message(
                chat_id=chat_id,
                text=overload_text,
                reply_markup=retry_keyboard(token, weak_allowed=not is_weak),
            )
            return

        sent_message = await send_answer_with_clarify_button(context, chat_id, response.text)
        if sent_message:
            context.user_data['last_answer_message_id'] = sent_message.message_id

        await save_history(chat_id, server_id, 'user', user_question)
        await save_history(chat_id, server_id, 'model', response.text)
        # ВРЕМЕННО ОТКЛЮЧЕНО: await log_to_google_sheet(chat_id, f"[{server_id}] {user_question}", response.text, q_type)
        context.user_data['is_new_conversation'] = False

    except Exception as e:
        logging.error(f"КРИТИЧЕСКАЯ ОШИБКА в `process_question_and_reply` для пользователя {chat_id}.", exc_info=e)
        await send_error_message(context, chat_id)
        raise


async def run_question_processing_task(context, chat_id, user_question, q_type, server_id, processing_message_id,
                                       model_override: str | None = None):
    """Обертка для запуска фоновой задачи и удаления сообщения "Обрабатываю..."."""
    task_start_time = asyncio.get_running_loop().time()
    logging.info(f"TASK_START: Запущена задача обработки вопроса для пользователя {chat_id}.")
    try:
        await process_question_and_reply(context, chat_id, user_question, q_type, server_id,
                                         model_override=model_override)
    finally:
        try:
            if processing_message_id != context.user_data.get('last_answer_message_id'):
                await context.bot.delete_message(chat_id=chat_id, message_id=processing_message_id)
        except (BadRequest, AttributeError):
            pass
        task_duration = (asyncio.get_running_loop().time() - task_start_time) * 1000
        logging.info(f"TASK_END: Задача обработки вопроса для {chat_id} завершена за {task_duration:.2f} мс.")


async def replay_qa_retry(context: ContextTypes.DEFAULT_TYPE, chat_id: int, params: dict, *, weak: bool):
    """Повтор вопроса по кнопке под сообщением о перегрузе.

    weak=False — повторить тот же вопрос на основной модели.
    weak=True  — разово отправить тот же вопрос на запасную (слабую) модель.
    Лимит не списывается повторно (он списывается при первичной отправке вопроса).
    """
    question = (params or {}).get('question')
    q_type = (params or {}).get('q_type')
    server_id = (params or {}).get('server_id')
    if not question or not server_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не удалось повторить вопрос — данные устарели. Задайте вопрос заново."
        )
        return

    model_override = config.WEAK_FALLBACK_MODEL_NAME if weak else None
    processing_message = await context.bot.send_message(
        chat_id=chat_id,
        text="⏳ Отправляю запрос запасной модели..." if weak else "⏳ Повторяю запрос..."
    )
    asyncio.create_task(run_question_processing_task(
        context, chat_id, question, q_type, server_id, processing_message.message_id,
        model_override=model_override,
    ))


# --- Логика оптимизации вопроса ---

async def _run_optimization_step(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> int:
    """Выполняет один шаг диалога с AI-оптимизатором."""
    processing_message = await update.effective_message.reply_text("Оптимизирую ваш вопрос...")
    chat_id = update.effective_chat.id
    try:
        optimizer_history_raw = context.user_data.get('optimizer_chat_history', [])
        optimizer_gemini_history = [types.Content(role=msg['role'], parts=[types.Part(text=msg['parts'][0])]) for msg in
                                    optimizer_history_raw]

        # Добавляем текущее сообщение пользователя
        current_msg = types.Content(role='user', parts=[types.Part(text=user_text)])
        full_history = optimizer_gemini_history + [current_msg]

        # Определяем функцию для выполнения с rate limiting
        async def send_optimizer_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=QUESTION_OPTIMIZER_MODEL_NAME,
                contents=full_history,
                config=types.GenerateContentConfig(
                    system_instruction=get_optimizer_prompt(),
                    response_mime_type="application/json"
                )
            )

        try:
            # Используем rate limiter с ОБЫЧНЫМ приоритетом (оптимизатор некритичен)
            response = await execute_vertex_request(
                func=send_optimizer_request,
                model_name=QUESTION_OPTIMIZER_MODEL_NAME,
                priority=RequestPriority.NORMAL,  # Обычный приоритет для оптимизатора
                user_id=chat_id,
                max_retries=2  # Меньше попыток для некритичной функции
            )
            # Отмечаем успех в текущем регионе
            await mark_current_region_success()
        except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
            # Отмечаем ошибку в текущем регионе
            await mark_current_region_error()

            # Проверяем, что это действительно 429 ошибка
            if isinstance(e, genai_errors.ClientError):
                error_str = str(e)
                if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                    raise  # Пробрасываем не-429 ошибки дальше

            # Все попытки исчерпаны или таймаут
            logging.warning(f"[OPTIMIZER] Не удалось оптимизировать вопрос для пользователя {chat_id}: {e}")
            await processing_message.edit_text(
                "⏳ Сервис оптимизации временно недоступен.\n\n"
                "Пропускаю оптимизацию. Вы можете задать вопрос напрямую."
            )
            # Пропускаем оптимизацию и переходим к обычному вопросу
            context.user_data['optimized_question'] = None
            return AWAITING_QUESTION

        clean_response_text = re.sub(r'```json\s*|\s*```', '', response.text, flags=re.DOTALL).strip()
        data = json.loads(clean_response_text)
        status, text = data['status'], data['text']

        optimizer_history_raw.extend(
            [{'role': 'user', 'parts': [user_text]}, {'role': 'model', 'parts': [clean_response_text]}])
        context.user_data['optimizer_chat_history'] = optimizer_history_raw

        if status == 'clarification_needed':
            await processing_message.edit_text(text)
            return OPTIMIZING_QUESTION
        elif status == 'optimization_complete':
            context.user_data['optimized_question'] = text
            keyboard = [[InlineKeyboardButton("✅ Да, всё верно", callback_data="confirm_optimization")],
                        [InlineKeyboardButton("✏️ Отредактировать", callback_data="edit_optimization")]]
            await processing_message.edit_text(
                text=f"🤖 Я оптимизировал ваш вопрос:\n\n<i>«{text}»</i>\n\nПродолжаем с этой версией?",
                reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
            )
            return AWAITING_OPTIMIZATION_CONFIRMATION
    except Exception as e:
        logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА в _run_optimization_step для {chat_id}.", exc_info=e)
        await processing_message.delete()
        await send_error_message(context, chat_id)
        return ConversationHandler.END
    return ConversationHandler.END


# --- Обработчики состояний ConversationHandler ---

@rate_limited
async def ask_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог "Задать вопрос", запрашивая проект."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    await check_subscription(query.from_user.id, context)
    force_manual = query.data == 'choose_question_manually'

    is_from_answer = False
    last_answer_id = context.user_data.get('last_answer_message_id')
    if last_answer_id and query.message.message_id == last_answer_id:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass  # Игнорируем "Message is not modified"
        is_from_answer = True

    context.user_data.clear()
    context.user_data['is_new_conversation'] = True

    if not force_manual:
        defaults = await _load_valid_question_defaults(query.from_user.id)
        if defaults:
            _apply_question_defaults(context, defaults)
            return await _show_question_prompt(update, context, defaults_used=True, is_from_answer=is_from_answer)

    enabled_projects = await get_enabled_projects()

    # Если только один проект - пропускаем выбор и сразу показываем серверы
    if len(enabled_projects) == 1:
        context.user_data['project_id'] = enabled_projects[0]
        return await _show_servers_for_project(update, context, enabled_projects[0], is_from_answer)

    # Удаляем старое сообщение
    try:
        if not is_from_answer:
            await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта с картинкой
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="select_project_",
        text="🌐 <b>Выберите проект:</b>",
        back_callback="main_menu"
    )

    return CHOOSING_PROJECT


@rate_limited
async def setup_question_defaults(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запускает настройку сохраненного шаблона вопросов."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    context.user_data.clear()
    context.user_data['configuring_question_defaults'] = True

    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) == 1:
        context.user_data['project_id'] = enabled_projects[0]
        return await _show_servers_for_project(update, context, enabled_projects[0])

    try:
        if query.message and query.message.photo:
            await query.message.delete()
        else:
            await query.message.delete()
    except Exception:
        pass

    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="select_project_",
        text="⚙️ <b>Настройка шаблона вопросов</b>\n\nВыберите проект, который будет использоваться по умолчанию:",
        back_callback="personal_account"
    )

    return CHOOSING_PROJECT


async def _show_servers_for_project(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: str, is_from_answer: bool = False) -> int:
    """Вспомогательная функция для отображения списка серверов выбранного проекта."""
    query = update.callback_query
    is_defaults_setup = context.user_data.get('configuring_question_defaults', False)

    enabled_servers = await get_enabled_servers(project_id)
    project_data = PROJECTS_CONFIG.get(project_id, {})
    project_name = project_data.get('display_name', project_id)

    keyboard = []
    for server_id in enabled_servers:
        server_data = config.SERVERS_CONFIG.get(server_id, {})
        display_name = server_data.get('display_name', server_id)
        keyboard.append([InlineKeyboardButton(display_name, callback_data=f"select_server_{server_id}")])

    # Кнопка "Назад" возвращает к выбору проекта только если проектов больше одного
    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) > 1:
        back_callback = "setup_question_defaults" if is_defaults_setup else "choose_question_manually"
        keyboard.append([InlineKeyboardButton("◀️ К выбору проекта", callback_data=back_callback)])
    else:
        back_callback = "personal_account" if is_defaults_setup else "main_menu"
        keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data=back_callback)])

    action_text = "Выберите сервер для шаблона:" if is_defaults_setup else "Выберите сервер:"
    text = f"🎮 <b>{project_name}</b>\n\n{action_text}"
    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_from_answer:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode='HTML')
    else:
        # Проверяем, является ли сообщение фото (с caption) или текстовым
        if query.message.photo:
            # Сообщение с фото - удаляем и отправляем новое текстовое
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode='HTML')
    return CHOOSING_SERVER


async def ask_server_for_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный проект и показывает список серверов."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    project_id = query.data.split("select_project_", 1)[1]
    context.user_data['project_id'] = project_id

    return await _show_servers_for_project(update, context, project_id)


# Алиас для обратной совместимости (точка входа)
ask_server = ask_project


async def prompt_for_optimization_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отображает сообщение с выбором оптимизации."""
    query = update.callback_query
    text = (
        "🔍 **Оптимизация вопроса**\n\n"
        "Эта функция помогает улучшить ваш вопрос, чтобы вы получили максимально точный ответ. AI-помощник может задать 1-2 уточняющих вопроса.\n\n"
        "**Когда это полезно?**\n"
        "Если ваш вопрос сложный, нечеткий или вы используете игровой сленг (ДМ, ДБ, ПГ и т.д.).\n\n"
        "**Когда можно пропустить?**\n"
        "Если ваш вопрос уже четко сформулирован (например, *'Какой штраф за проезд на красный свет?'*)."
    )

    keyboard = [
        [InlineKeyboardButton("🤖 Оптимизировать мой вопрос", callback_data="set_opt_True")],
        [InlineKeyboardButton("✍️ Задать вопрос напрямую", callback_data="set_opt_False")],
        [InlineKeyboardButton("◀️ Назад", callback_data="choose_question_manually")]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return AWAITING_OPTIMIZATION_CHOICE


async def _show_answer_type_choice(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str = "Выберите тип ответа:",
        back_callback: str | None = None
) -> int:
    """Показывает выбор формата ответа."""
    query = update.callback_query
    keyboard = [[InlineKeyboardButton("📜 Подробный", callback_data="select_type_detailed"),
                 InlineKeyboardButton("⚡️ Краткий", callback_data="select_type_brief"),
                 InlineKeyboardButton("🌊 Свободный", callback_data="select_type_free")]]
    if back_callback:
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=back_callback)])

    await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_ANSWER_TYPE


async def ask_answer_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет сервер и вызывает функцию отображения выбора оптимизации."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    context.user_data['server_id'] = query.data.split("select_server_", 1)[1]
    if context.user_data.get('configuring_question_defaults'):
        project_id = context.user_data.get('project_id')
        return await _show_answer_type_choice(
            update,
            context,
            text="Выберите формат ответа для шаблона:",
            back_callback=f"select_project_{project_id}"
        )
    return await prompt_for_optimization_choice(update, context)


async def set_optimization_and_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбор оптимизации и запрашивает тип ответа."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    context.user_data['skip_optimization'] = (query.data.split('_')[-1] == 'False')
    return await _show_answer_type_choice(
        update,
        context,
        back_callback=f"select_server_{context.user_data['server_id']}"
    )


async def prompt_for_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет тип ответа и просит пользователя задать вопрос, показывая итоговую информацию."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    context.user_data['question_type'] = query.data.split('_')[-1]

    if context.user_data.get('configuring_question_defaults'):
        project_id = context.user_data.get('project_id')
        server_id = context.user_data.get('server_id')
        question_type = context.user_data.get('question_type')
        try:
            await _save_question_defaults(query.from_user.id, project_id, server_id, question_type)
        except RuntimeError as exc:
            if str(exc) != "question_defaults_schema_missing":
                raise
            await query.edit_message_text(
                "⚠️ Шаблон пока нельзя сохранить: база данных еще не обновлена.\n\n"
                "Обычный выбор проекта, сервера и формата ответа продолжает работать. "
                "После применения миграций попробуйте настроить шаблон еще раз.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔁 Выбрать вручную", callback_data="choose_question_manually")],
                    [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")],
                ])
            )
            return ConversationHandler.END

        keyboard = [
            [InlineKeyboardButton("❓ Задать вопрос по шаблону", callback_data="start_question")],
            [InlineKeyboardButton("👤 Личный кабинет", callback_data="personal_account")],
        ]
        text = (
            "✅ <b>Шаблон вопросов сохранен.</b>\n\n"
            f"Проект: <b>{_get_project_display_name(project_id)}</b>\n"
            f"Сервер: <b>{_get_server_display_name(server_id)}</b>\n"
            f"Формат ответа: <b>{_get_question_type_label(question_type)}</b>\n\n"
            "Теперь кнопка «Задать вопрос» будет сразу открывать ввод вопроса с этими параметрами."
        )
        await query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
        return ConversationHandler.END

    return await _show_question_prompt(update, context)


async def handle_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_question = update.message.text
    if emoji.emoji_count(user_question) > 0:
        await update.message.reply_text("Ваш вопрос содержит эмодзи. Пожалуйста, отправьте его без них.")
        return AWAITING_QUESTION

    question_context = await _recover_question_context(update, context)
    if not question_context:
        return await _finish_missing_question_context(update, context)
    question_type, server_id = question_context

    can_ask, _ = await check_and_update_usage(update.effective_chat.id, question_type)
    if not can_ask:
        await update.message.reply_text("Вы достигли дневного лимита вопросов.")
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END

    if context.user_data.get('skip_optimization', False):
        processing_message = await update.message.reply_text("Ваш запрос принят. Готовлю консультацию...")
        asyncio.create_task(run_question_processing_task(context, update.effective_chat.id, user_question,
                                                         question_type,
                                                         server_id, processing_message.message_id))
        return ConversationHandler.END
    else:
        context.user_data.update({'original_question': user_question, 'optimizer_chat_history': []})
        return await _run_optimization_step(update, context, user_question)


async def handle_user_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _run_optimization_step(update, context, update.message.text)


async def handle_confirm_optimization(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    question_context = await _recover_question_context(update, context)
    optimized_question = context.user_data.get('optimized_question')
    if not question_context or not optimized_question:
        return await _finish_missing_question_context(update, context)
    question_type, server_id = question_context

    await query.edit_message_text("✅ Принято. Готовлю консультацию...")
    asyncio.create_task(
        run_question_processing_task(context, query.message.chat_id, optimized_question,
                                     question_type, server_id,
                                     query.message.message_id))
    return ConversationHandler.END


async def handle_edit_optimization_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    await query.edit_message_text(
        f"Скопируйте текст ниже, исправьте его и отправьте в чат:\n\n<pre>{context.user_data['optimized_question']}</pre>",
        parse_mode='HTML'
    )
    return AWAITING_EDITED_OPTIMIZED_QUESTION


async def handle_edited_optimized_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    question_context = await _recover_question_context(update, context)
    if not question_context:
        return await _finish_missing_question_context(update, context)
    question_type, server_id = question_context

    processing_message = await update.message.reply_text("✅ Принято. Готовлю консультацию по вашей версии вопроса...")
    asyncio.create_task(run_question_processing_task(context, update.effective_chat.id, update.message.text,
                                                     question_type, server_id,
                                                     processing_message.message_id))
    return ConversationHandler.END


async def handle_voice_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """ВРЕМЕННО ОТКЛЮЧЕНО: Обработка голосовых сообщений"""
    await update.message.reply_text(
        "⚠️ Функция голосовых сообщений временно отключена.\n\n"
        "Пожалуйста, отправьте ваш вопрос текстом."
    )
    return context.user_data.get('current_state', AWAITING_QUESTION)

    # ВРЕМЕННО ОТКЛЮЧЕНО:
    # status_msg = await update.message.reply_text("Получил голосовое, распознаю...")
    # voice_file = await update.message.voice.get_file()
    # user_question = await transcribe_voice(bytes(await voice_file.download_as_bytearray()))
    #
    # if not user_question:
    #     await status_msg.edit_text("Не удалось распознать речь. Попробуйте еще раз.")
    #     return context.user_data.get('current_state', AWAITING_QUESTION)
    #
    # context.user_data['transcribed_text'] = user_question
    # context.user_data['voice_origin_state'] = context.user_data.get('current_state', AWAITING_QUESTION)
    # keyboard = [[InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_transcription")],
    #             [InlineKeyboardButton("🔄 Перезаписать", callback_data="rerecord_transcription")],
    #             [InlineKeyboardButton("✏️ Отредактировать", callback_data="edit_transcription")]]
    # await status_msg.edit_text(f"<i>Распознано:</i>\n\n«{user_question}»\n\nВерно?",
    #                            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    # return AWAITING_TRANSCRIPTION_CONFIRMATION


async def handle_confirm_transcription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    mock_update = Update(update.update_id, message=query.message)
    mock_update.message.text = context.user_data['transcribed_text']
    await query.delete_message()
    return await handle_question(mock_update, context)


async def handle_rerecord_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    await query.edit_message_text("Хорошо, я слушаю. Отправьте новое голосовое сообщение.")
    return context.user_data.get('voice_origin_state', AWAITING_QUESTION)


async def handle_edit_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data="back_to_confirm")]]
    await query.edit_message_text("Отправьте исправленный текст вашего вопроса в чат.",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_EDITED_TRANSCRIPTION


async def back_to_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_question = context.user_data.get('transcribed_text')
    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_transcription")],
        [InlineKeyboardButton("🔄 Перезаписать", callback_data="rerecord_transcription")],
        [InlineKeyboardButton("✏️ Отредактировать", callback_data="edit_transcription")]
    ]
    await query.edit_message_text(
        f"<i>Распознано:</i>\n\n«{user_question}»\n\nВерно?",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML'
    )
    return AWAITING_TRANSCRIPTION_CONFIRMATION


async def handle_edited_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await handle_question(update, context)


async def request_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass  # Игнорируем "Message is not modified"
    await context.bot.send_message(chat_id=query.message.chat_id, text="Задайте ваш уточняющий вопрос.")
    context.user_data['current_state'] = AWAITING_CLARIFICATION
    return AWAITING_CLARIFICATION


async def handle_clarification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    question_type = context.user_data.get('question_type')
    server_id = context.user_data.get('server_id')

    if not question_type or not server_id:
        await update.message.reply_text("Ошибка: отсутствуют необходимые данные. Пожалуйста, начните заново.")
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END

    can_ask, _ = await check_and_update_usage(update.effective_chat.id, question_type)
    if not can_ask:
        await update.message.reply_text("Вы достигли дневного лимита вопросов.")
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END
    processing_message = await update.message.reply_text("Обрабатываю уточнение...")
    asyncio.create_task(run_question_processing_task(context, update.effective_chat.id, update.message.text,
                                                     question_type, server_id,
                                                     processing_message.message_id))
    return ConversationHandler.END


# --- Определяем ConversationHandler для всего этого модуля ---

user_conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(ask_project, pattern='^(start_question|choose_question_manually)$'),
        CallbackQueryHandler(setup_question_defaults, pattern='^setup_question_defaults$'),
        CallbackQueryHandler(request_clarification, pattern='^clarify_question$')
    ],
    states={
        CHOOSING_PROJECT: [CallbackQueryHandler(ask_server_for_project, pattern=r'^select_project_')],
        CHOOSING_SERVER: [CallbackQueryHandler(ask_answer_type, pattern=r'^select_server_')],
        AWAITING_OPTIMIZATION_CHOICE: [
            CallbackQueryHandler(set_optimization_and_prompt, pattern=r'^set_opt_'),
            CallbackQueryHandler(ask_project, pattern='^(start_question|choose_question_manually)$')
        ],
        CHOOSING_ANSWER_TYPE: [
            CallbackQueryHandler(prompt_for_question, pattern=r'^select_type_'),
            CallbackQueryHandler(ask_answer_type, pattern=r'^select_server_'),
            CallbackQueryHandler(ask_server_for_project, pattern=r'^select_project_')
        ],
        AWAITING_QUESTION: [
            CallbackQueryHandler(ask_project, pattern='^choose_question_manually$'),
            CallbackQueryHandler(setup_question_defaults, pattern='^setup_question_defaults$'),
            CallbackQueryHandler(main_menu, pattern='^main_menu$'),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_question),
            MessageHandler(filters.VOICE, handle_voice_question)
        ],
        OPTIMIZING_QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_clarification)],
        AWAITING_OPTIMIZATION_CONFIRMATION: [
            CallbackQueryHandler(handle_confirm_optimization, pattern='^confirm_optimization$'),
            CallbackQueryHandler(handle_edit_optimization_request, pattern='^edit_optimization$')
        ],
        AWAITING_EDITED_OPTIMIZED_QUESTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edited_optimized_question)],
        AWAITING_CLARIFICATION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_clarification),
            MessageHandler(filters.VOICE, handle_voice_question)
        ],
        AWAITING_TRANSCRIPTION_CONFIRMATION: [
            CallbackQueryHandler(handle_confirm_transcription, pattern='^confirm_transcription$'),
            CallbackQueryHandler(handle_rerecord_request, pattern='^rerecord_transcription$'),
            CallbackQueryHandler(handle_edit_request, pattern='^edit_transcription$'),
        ],
        AWAITING_EDITED_TRANSCRIPTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edited_text),
            CallbackQueryHandler(back_to_confirm, pattern='^back_to_confirm$'),
        ],
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(main_menu, pattern='^main_menu$'),
        CallbackQueryHandler(personal_account, pattern='^personal_account$'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
    ],
    per_message=False, allow_reentry=True
)

# END OF FILE user_handlers/qa_flow.py