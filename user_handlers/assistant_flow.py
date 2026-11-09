# START OF FILE user_handlers/assistant_flow.py

"""
Модуль для управления всем функционалом "Ролевого Помощника".

Содержит:
- Полный ConversationHandler для создания "Дела" и ведения диалога.
- Функции для отображения меню, выбора сервера, роли, стадии.
- Логику обработки сообщений пользователя и вызовов AI-модели.
- Интеллектуальный фильтр законов для контекстного подбора материалов.
- Обработчики для интерактивных кнопок-инструментов.
- Функционал просмотра активных дел и завершения консультаций.
- Логику для автоматического закрытия дел по таймеру.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from google import genai
from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, MessageEntity
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters
from playwright.async_api import async_playwright

# Локальные импорты
import config
from services import case_digest, case_law_context
from database import assistant_logger
from database.manager import db_manager
from utils import get_enabled_servers, get_enabled_projects, send_project_selection_menu, rate_limited, sanitize_telegram_html, reinitialize_vertex_ai, retry_with_exponential_backoff, safe_answer_callback_query
from prompts import (
    get_assistant_router_prompt,
    get_assistant_system_prompt,
    get_assistant_tool_prompt
)
from .core import main_menu, start, check_and_update_usage, send_error_message, _display_main_menu, unknown_text_fallback
from .qa_flow import _split_long_message
from case_file_parser import fetch_case_content_from_url
# Импорт модуля прецедентов
from precedents_manager import get_relevant_precedents
from config import (
    CHOOSING_PROJECT, CHOOSING_ASSISTANT_ROLE, CHOOSING_ASSISTANT_STAGE, AWAITING_CASE_DESCRIPTION,
    ASSISTANT_DIALOGUE, VIEWING_ACTIVE_CASES, AWAITING_STAGE_SELECTION,
    AWAITING_ADDITIONAL_MATERIALS,
    ASSISTANT_ROUTER_MODEL_NAME, ROLE_ASSISTANT_MODEL_NAME,
    PROJECTS_CONFIG
)
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error

# --- Константы и словари для Ассистента ---

ASSISTANT_ROLES = {
    "advocate": "Адвокат",
    "prosecutor": "Прокурор",
    "judge": "Судья"
}

ASSISTANT_STAGES = {
    "field_work": "🚨 На месте происшествия / задержания",
    "case_analysis": "🗂️ Анализ материалов дела",
    "court_case": "⚖️ Судебное дело (анализ темы с форума)"
}

ASSISTANT_TOOLS = {
    "advocate": {
        "field_work": [
            InlineKeyboardButton("🛡️ Экспресс-анализ и линия защиты", callback_data="tool_adv_build_defense"),
            InlineKeyboardButton("🔍 Аудит действий офицеров", callback_data="tool_adv_procedural_audit"),
            InlineKeyboardButton("💬 Подготовка к общению с клиентом/полицией",
                                 callback_data="tool_adv_client_briefing"),
        ],
        "case_analysis": [
            InlineKeyboardButton("🛡️ Анализ дела и линия защиты", callback_data="tool_adv_build_defense"),
            InlineKeyboardButton("🔍 Процессуальный аудит", callback_data="tool_adv_procedural_audit"),
            InlineKeyboardButton("💥 Поиск прецедентов и контраргументов", callback_data="tool_adv_search_precedents"),
        ]
    },
    "prosecutor": {
        "field_work": [
            InlineKeyboardButton("⚖️ Первичная правовая квалификация", callback_data="tool_pro_qualify_crime"),
            InlineKeyboardButton("📋 Чек-лист процессуальных действий", callback_data="tool_pro_procedural_audit"),
            InlineKeyboardButton("❗ Фиксация ключевых доказательств", callback_data="tool_pro_prepare_interrogation"),
        ],
        "case_analysis": [
            InlineKeyboardButton("🖋️ Квалификация и формирование обвинения", callback_data="tool_pro_qualify_crime"),
            InlineKeyboardButton("🔍 Процессуальный аудит (сторона обвинения)",
                                 callback_data="tool_pro_procedural_audit"),
            InlineKeyboardButton("❓ Подготовка к допросу", callback_data="tool_pro_prepare_interrogation"),
        ]
    },
    "judge": {
        # Кнопки судьи формируются динамически в _get_assistant_buttons
        "court_case": []
    }
}


async def prosecutor_assistant_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик кнопки 'Помощник прокурора' из главного меню.
    Запускает специализированное меню для прокуроров.
    """
    await safe_answer_callback_query(update.callback_query)
    # Показываем Inline клавиатуру с опциями прокурора
    keyboard = [
        [InlineKeyboardButton("🚨 Работа на месте (Field Work)", callback_data="start_prosecutor_field")],
        [InlineKeyboardButton("🗂️ Анализ дела (Case Analysis)", callback_data="start_prosecutor_case")],
        [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
    ]
    await update.effective_message.edit_text(
        "⚖️ **Панель Помощника Прокурора**\n\n"
        "Выберите режим работы:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# --- ЛОГИКА АВТОЗАКРЫТИЯ ДЕЛ ---

async def auto_close_case_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Задача, которая выполняется планировщиком для закрытия дела."""
    job = context.job
    case_id = job.data['case_id']
    user_id = job.data['user_id']

    logging.info(f"[AutoClose] Сработал таймер для дела #{case_id}.")

    case_status = await db_manager.fetchval("SELECT status FROM assistant_cases WHERE id = $1", case_id)

    if case_status == 'active':
        await assistant_logger.close_case(case_id)
        logging.info(f"[AutoClose] Дело #{case_id} было автоматически закрыто.")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⚖️ Ваша консультация по делу #{case_id} была автоматически завершена по истечении времени. "
                     "Вы можете начать новую консультацию из главного меню.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]])
            )
        except Exception as e:
            logging.warning(f"[AutoClose] Не удалось уведомить пользователя {user_id} о закрытии дела #{case_id}.",
                            exc_info=e)
    else:
        logging.info(f"[AutoClose] Дело #{case_id} уже было закрыто. Уведомление не отправлено.")


def _remove_scheduled_job(context: ContextTypes.DEFAULT_TYPE, case_id: int):
    """Удаляет существующую задачу по закрытию дела."""
    job_name = f"case_close_{case_id}"
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    if not current_jobs:
        return
    for job in current_jobs:
        job.schedule_removal()
    logging.info(f"[Scheduler] Удалена задача автозакрытия для дела #{case_id}.")


def schedule_case_closure(context: ContextTypes.DEFAULT_TYPE, case_id: int, user_id: int, case_stage: str):
    """Планирует или перепланирует задачу автозакрытия дела."""
    job_name = f"case_close_{case_id}"

    _remove_scheduled_job(context, case_id)

    job_data = {'case_id': case_id, 'user_id': user_id}
    delay = None

    if case_stage == 'field_work':
        delay = timedelta(minutes=90)
    elif case_stage == 'case_analysis':
        delay = timedelta(hours=24)

    if delay:
        context.job_queue.run_once(auto_close_case_job, delay, data=job_data, name=job_name)
        logging.info(f"[Scheduler] Запланировано автозакрытие дела #{case_id} через {delay}.")


# --- ОСНОВНЫЕ ФУНКЦИИ МОДУЛЯ ---

# Кнопки — только быстрые заготовки: помощник отвечает и на обычное сообщение, но по
# статистике многие пользователи об этом не догадываются и жмут только кнопки.
ASSISTANT_ACTION_HINT = (
    "⚖️ <b>Выберите действие</b> — или просто напишите свой вопрос сообщением.\n\n"
    "<i>Кнопки — это готовые сценарии. Помощник помнит всё дело и ответит на любой "
    "вопрос своими словами: спросите про конкретный эпизод, статью или что делать дальше.</i>"
)


def _assistant_generation_limits() -> dict:
    """max_output_tokens + thinking_level для запросов помощника.

    Без потолка вывода длинный разбор обрывался на MAX_TOKENS и возвращал пустой text.
    thinking у gemini-3-flash-preview по умолчанию HIGH — на большом деле это минуты
    ожидания; уровень задаём явно (пустое значение в env = не слать, аварийный откат).
    """
    kwargs: dict = {}
    max_tokens = getattr(config, 'ASSISTANT_MAX_OUTPUT_TOKENS', 0) or 0
    if max_tokens > 0:
        kwargs['max_output_tokens'] = max_tokens

    level = (getattr(config, 'ASSISTANT_THINKING_LEVEL', '') or '').strip().lower()
    enum_val = {
        'minimal': getattr(types.ThinkingLevel, 'MINIMAL', None),
        'low': getattr(types.ThinkingLevel, 'LOW', None),
        'medium': getattr(types.ThinkingLevel, 'MEDIUM', None),
        'high': getattr(types.ThinkingLevel, 'HIGH', None),
    }.get(level) if hasattr(types, 'ThinkingLevel') else None
    if enum_val is not None:
        kwargs['thinking_config'] = types.ThinkingConfig(thinking_level=enum_val)
    return kwargs


# Обрезка истории живёт в services/case_digest вместе с картой дела: тот же путь нужен
# веб-флоу (services/assistant_service), а два экземпляра логики неминуемо разъехались бы.
_trim_case_history = case_digest.trim_history


def _get_assistant_buttons(role: str, stage: str, case_id: int) -> InlineKeyboardMarkup:
    """Собирает клавиатуру с кнопками-инструментами и кнопкой закрытия дела."""

    # Для судьи создаем кнопки динамически с case_id
    if role == "judge" and stage == "court_case":
        buttons = [
            InlineKeyboardButton("📎 Дополнить материалы дела", callback_data=f"tool_court_add_materials_{case_id}"),
            InlineKeyboardButton("❓ Задать вопрос / Запросить помощь", callback_data=f"tool_court_ask_question_{case_id}"),
            InlineKeyboardButton("🔄 Обновить дело", callback_data=f"tool_court_refresh_case_{case_id}"),
        ]
    else:
        # Для адвоката и прокурора используем статические кнопки
        buttons = ASSISTANT_TOOLS.get(role, {}).get(stage, [])

    keyboard = [[btn] for btn in buttons]
    keyboard.append([InlineKeyboardButton("❌ Завершить консультацию", callback_data=f"close_case_{case_id}")])
    return InlineKeyboardMarkup(keyboard)


# --- Интеллектуальный фильтр законов для Ассистента ---

async def get_law_parts_for_assistant(server_id: str, case_id: int, current_request: str,
                                      context: ContextTypes.DEFAULT_TYPE) -> list[types.Part]:
    """Законы для запроса помощника: сначала подборка статей, при неудаче — целые документы.

    Подборка статей (каталог + FRIDA, тот же путь, что у Q&A v2) вместо целых кодексов
    убирает из запроса десятки тысяч лишних символов и не даёт нужной норме «утонуть»
    в середине контекста. Каталог есть не у всех серверов, поэтому пустая подборка молча
    откатывается на прежний режим — законы модель получит в любом случае.
    """
    if case_law_context.articles_mode_enabled():
        try:
            history = await assistant_logger.get_case_history(case_id) or []
            facts, recent = case_law_context.history_facts_and_recent(history)
            ctx = await case_law_context.law_context_for_case(
                server_id, current_request, facts=facts, recent=recent,
            )
            if ctx:
                logging.info(
                    "[ASSISTANT_LAW] Дело #%s: подборка статей — %s статей, %s симв.",
                    case_id, ctx['count'], ctx['chars'],
                )
                return [types.Part(text=ctx['text'])]
        except Exception as e:
            logging.warning("[ASSISTANT_LAW] Дело #%s: подборка статей не удалась (%s)", case_id, e)

    return await get_relevant_laws_for_assistant(context, server_id, case_id, current_request)


async def get_relevant_laws_for_assistant(context: ContextTypes.DEFAULT_TYPE, server_id: str, case_id: int,
                                          current_request: str) -> list[types.Part]:
    """
    Выбирает наиболее релевантные законы для Ассистента, используя полный контекст дела.
    """
    start_time = asyncio.get_running_loop().time()
    logging.info(f"[ASSISTANT_ROUTER] Запуск интеллектуального фильтра для дела #{case_id}...")

    try:
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )
        if not law_records:
            logging.warning(f"[ASSISTANT_ROUTER] В БД отсутствуют документы для сервера {server_id}.")
            return []

        law_map_by_index = {i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']} for i, r in
                            enumerate(law_records)}
        law_titles_str = "\n".join([f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)])

        history_records = await assistant_logger.get_case_history(case_id)
        if not history_records:
            logging.warning(f"[ASSISTANT_ROUTER] Не удалось загрузить историю для дела #{case_id}.")
            return []

        case_details = context.user_data
        case_context = (f"Роль пользователя: {ASSISTANT_ROLES.get(case_details['rp_role'])}, "
                        f"Стадия: {ASSISTANT_STAGES.get(case_details['case_stage'])}")

        initial_description = history_records[0]['content'] if history_records else "Фабула дела не предоставлена."
        case_context += f"\nНачальное описание: {initial_description}"

        dialog_history = ""
        for msg in history_records[-6:]:
            sender = "Пользователь" if msg['sender_role'] == 'user' else "Ассистент"
            dialog_history += f"{sender}: {msg['content']}\n"

        router_prompt = get_assistant_router_prompt(
            case_context=case_context,
            dialog_history=dialog_history,
            current_request=current_request,
            law_titles_str=law_titles_str
        )

        # Используем rate limiter с поддержкой переключения регионов
        async def make_router_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ASSISTANT_ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ASSISTANT_ROUTER_MODEL_NAME,
            priority=RequestPriority.HIGH,  # Высокий приоритет для активных диалогов
            max_retries=2
        )

        # Отмечаем успех в текущем регионе
        await mark_current_region_success()

        selected_indices = {int(line.strip()) for line in response.text.split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logging.info(
            f"[ASSISTANT_ROUTER] Успех. Фильтр выбрал {len(relevant_laws)} законов для дела #{case_id} за {duration:.2f} мс. "
            f"Выбранные законы: {[law['title'] for law in relevant_laws]}"
        )
        return [types.Part.from_uri(file_uri=law['uri'], mime_type="text/plain") for law in relevant_laws]

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        # Отмечаем ошибку в текущем регионе
        await mark_current_region_error()

        # Проверяем, что это действительно 429 ошибка или таймаут
        if isinstance(e, genai_errors.ClientError):
            error_str = str(e)
            if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                raise  # Пробрасываем не-429 ошибки дальше

        logging.warning(f"[ASSISTANT_ROUTER] Ошибка при фильтрации для дела #{case_id}: {e}")
        # Возвращаем пустой список при превышении лимита
        return []
    except Exception as e:
        logging.error(f"[ASSISTANT_ROUTER] КРИТИЧЕСКАЯ ОШИБКА в работе фильтра для дела #{case_id}.", exc_info=e)
        return []


async def _process_assistant_message(update: Update, context: ContextTypes.DEFAULT_TYPE, initial_request: str):
    """
    Основная "рабочая лошадка": обрабатывает сообщение/кнопку, вызывает AI и отправляет ответ.
    """
    query = update.callback_query
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    processing_message = await context.bot.send_message(chat_id, "Ассистент думает...")

    try:
        case_id = context.user_data.get('case_id')
        rp_role = context.user_data.get('rp_role')
        case_stage = context.user_data.get('case_stage')
        server_id = context.user_data.get('server_id')

        if not all([case_id, rp_role, case_stage, server_id]):
            await processing_message.edit_text("Ошибка: контекст диалога утерян. Начните заново.")
            return

        user_content = initial_request
        tool_name = None

        if query and query.data.startswith("tool_"):
            tool_name = query.data.split('_', 1)[1]
            button_row = ASSISTANT_TOOLS.get(rp_role, {}).get(case_stage, [])
            button_text = "Активирован инструмент"
            for btn in button_row:
                if btn.callback_data == query.data:
                    button_text = btn.text
                    break
            user_content = f"Пользователь активировал инструмент: «{button_text}»"
            await query.answer(f"Запрос принят: {button_text}")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass  # Игнорируем "Message is not modified"

        if user_content:
            await assistant_logger.add_message_to_case(case_id, 'user', user_content)

        # 1. Получаем релевантные законы
        files_for_query = await get_law_parts_for_assistant(server_id, case_id, user_content, context)
        if not files_for_query:
            logging.warning(f"Интеллектуальный фильтр не выбрал ни одного закона для дела #{case_id}.")

        # 2. НОВОЕ: Получаем релевантные прецеденты
        genai_client = context.bot_data.get('genai_client')
        if genai_client:
            try:
                relevant_precedents = await get_relevant_precedents(genai_client, user_content, server_id)
                if relevant_precedents:
                    # Добавляем прецеденты к контексту
                    for precedent in relevant_precedents:
                        files_for_query.append(types.Part.from_uri(file_uri=precedent['uri'], mime_type="text/plain"))
                    logging.info(f"[ASSISTANT] Добавлено {len(relevant_precedents)} прецедентов к контексту дела #{case_id}")
            except Exception as e:
                logging.warning(f"[ASSISTANT] Ошибка получения прецедентов для дела #{case_id}: {e}")

        history = await assistant_logger.get_case_history(case_id)
        if history is None:
            await processing_message.edit_text("Ошибка при загрузке истории дела. Попробуйте снова.")
            return

        # Длинное дело подаётся модели не сырым: старая часть сжимается в карту дела
        # (эпизоды, участники, доказательства, нормы, договорённости), свежий хвост идёт
        # как есть. Без карты — прежняя обрезка по лимиту.
        history = await case_digest.build_case_messages(
            case_id, history, rp_role=rp_role, case_stage=case_stage,
        )

        gemini_history = []
        for msg in history:
            role = 'model' if msg['sender_role'] == 'ai' else 'user'
            gemini_history.append(types.Content(role=role, parts=[types.Part(text=msg['content'])]))

        system_prompt = get_assistant_system_prompt(rp_role, case_stage)

        prompt_for_ai = gemini_history[-1].parts[0].text
        if tool_name:
            # Задание инструмента ДОПОЛНЯЕТ последний ход, а не заменяет его: раньше
            # подстановка затирала строку «Пользователь активировал инструмент: «…»»,
            # и модель не знала, какой именно инструмент нажали.
            tool_prompt = get_assistant_tool_prompt(tool_name)
            prompt_for_ai = f"{prompt_for_ai}\n\n{tool_prompt}" if prompt_for_ai else tool_prompt

        final_parts = [types.Part(text=prompt_for_ai)] + files_for_query
        final_content_item = types.Content(role='user', parts=final_parts)

        # Формируем полную историю для отправки
        full_history = gemini_history[:-1] + [final_content_item]

        # Определяем функцию для выполнения с rate limiting
        async def send_assistant_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROLE_ASSISTANT_MODEL_NAME,
                contents=full_history,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    **_assistant_generation_limits(),
                )
            )

        # Используем rate limiter с КРИТИЧЕСКИМ приоритетом
        try:
            response = await execute_vertex_request(
                func=send_assistant_request,
                model_name=ROLE_ASSISTANT_MODEL_NAME,
                priority=RequestPriority.CRITICAL,  # КРИТИЧЕСКИЙ приоритет для основного диалога
                user_id=user_id,
                max_retries=3
            )
            # Отмечаем успех в текущем регионе
            await mark_current_region_success()
        except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
            # Отмечаем ошибку в текущем регионе
            await mark_current_region_error()

            # Проверяем, что это действительно 429 ошибка или таймаут
            if isinstance(e, genai_errors.ClientError):
                error_str = str(e)
                # Переполнение контекста (400) не лечится ожиданием: история дела только
                # растёт, а лимит консультации уже списан. Говорим честно и предлагаем выход,
                # вместо «внутренней ошибки», после которой дело выглядело мёртвым.
                if '400' in error_str and (
                    'token' in error_str.lower() or 'INVALID_ARGUMENT' in error_str
                ):
                    logging.error(
                        "[ASSISTANT] Дело #%s: запрос не помещается в контекст модели: %s",
                        case_id, error_str[:300],
                    )
                    await context.bot.send_message(
                        chat_id,
                        "📚 <b>Дело стало слишком объёмным</b>\n\n"
                        "Материалов накопилось больше, чем помещается в один запрос к AI.\n\n"
                        "Что можно сделать:\n"
                        "• задать вопрос короче и конкретнее — так помощник разберёт нужный фрагмент;\n"
                        "• завершить консультацию и открыть новую по оставшимся вопросам, "
                        "кратко изложив суть.\n\n"
                        "<i>Материалы дела сохранены.</i>",
                        parse_mode='HTML',
                    )
                    return
                if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                    raise  # Пробрасываем не-429 ошибки дальше

            # Все попытки исчерпаны или таймаут
            logging.error(f"[ASSISTANT] Не удалось выполнить запрос для пользователя {user_id}: {e}")
            await context.bot.send_message(
                chat_id,
                "⏳ К сожалению, AI-сервис временно перегружен.\n\n"
                "Система управления нагрузкой не смогла обработать ваш запрос.\n\n"
                "Пожалуйста, попробуйте задать свой вопрос через 2-3 минуты.\n\n"
                "Приносим извинения за неудобства."
            )
            return

        cleaned_ai_response = sanitize_telegram_html(response.text)
        if not cleaned_ai_response.strip():
            # Пустой ответ (упёрлись в лимит вывода, safety-блок или только thinking) раньше
            # уходил в БД и терялся: плашка «думаю» исчезала, сообщений ноль, ошибки ноль.
            finish = getattr(getattr(response, 'candidates', [None])[0], 'finish_reason', None)
            logging.error("[ASSISTANT] Дело #%s: пустой ответ модели (finish_reason=%s)", case_id, finish)
            await context.bot.send_message(
                chat_id,
                "🤔 <b>Не удалось сформировать ответ</b>\n\n"
                "Попробуйте переформулировать запрос или задать его короче — "
                "например, спросить по одному эпизоду дела.",
                parse_mode='HTML',
                reply_markup=_get_assistant_buttons(rp_role, case_stage, case_id),
            )
            return

        await assistant_logger.add_message_to_case(case_id, 'ai', cleaned_ai_response)
        reply_markup = _get_assistant_buttons(rp_role, case_stage, case_id)

        parts = _split_long_message(cleaned_ai_response, limit=4096)
        for i, part in enumerate(parts):
            current_markup = reply_markup if i == len(parts) - 1 else None
            await context.bot.send_message(chat_id, text=part, parse_mode='HTML', reply_markup=current_markup)

    except Exception as e:
        logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА в _process_assistant_message для пользователя {user_id}.", exc_info=e)
        await context.bot.send_message(chat_id, "Произошла внутренняя ошибка при обработке вашего запроса.")
    finally:
        await processing_message.delete()


# --- ОБРАБОТЧИКИ СОСТОЯНИЙ CONVERSATIONHANDLER ---

@rate_limited
async def start_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    keyboard = [
        [InlineKeyboardButton("📂 Начать новое дело", callback_data="assist_new_case")],
        [InlineKeyboardButton("🗂️ Мои активные дела", callback_data="assist_view_active")],
        [InlineKeyboardButton("◀️ Назад в главное меню", callback_data="main_menu")]
    ]
    text = "⚖️ **Ролевой Помощник**\n\nЭтот раздел поможет вам вести дела в роли адвоката или прокурора."
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
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    return CHOOSING_ASSISTANT_ROLE


@rate_limited
async def prompt_new_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает создание нового дела, запрашивая проект."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    enabled_projects = await get_enabled_projects()

    # Если только один проект - пропускаем выбор и сразу показываем серверы
    if len(enabled_projects) == 1:
        context.user_data['assistant_project_id'] = enabled_projects[0]
        return await _show_assistant_servers(update, context, enabled_projects[0])

    # Удаляем старое сообщение
    try:
        await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта с картинкой
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="assist_project_",
        text="🌐 <b>Выберите проект:</b>",
        back_callback="start_assistant"
    )
    return CHOOSING_PROJECT


async def _show_assistant_servers(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: str) -> int:
    """Показывает список серверов для выбранного проекта."""
    query = update.callback_query

    enabled_servers = await get_enabled_servers(project_id)
    if not enabled_servers:
        # Проверяем, является ли сообщение фото
        if query.message.photo:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="На данный момент нет доступных серверов для этого проекта."
            )
        else:
            await query.edit_message_text("На данный момент нет доступных серверов для этого проекта.")
        return ConversationHandler.END

    project_data = PROJECTS_CONFIG.get(project_id, {})
    project_name = project_data.get('display_name', project_id)

    keyboard = [[InlineKeyboardButton(config.SERVERS_CONFIG[s]['display_name'], callback_data=f"assist_server_{s}")] for
                s in enabled_servers]

    # Кнопка "Назад" зависит от количества проектов
    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) > 1:
        keyboard.append([InlineKeyboardButton("◀️ К выбору проекта", callback_data="assist_new_case")])
    else:
        keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data="start_assistant")])

    text = f"🎮 <b>{project_name}</b>\n\nВыберите сервер, на котором ведется дело:"
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Проверяем, является ли сообщение фото
    if query.message.photo:
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
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    return CHOOSING_ASSISTANT_ROLE


async def select_assistant_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный проект и показывает серверы."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    project_id = query.data.split("assist_project_", 1)[1]
    context.user_data['assistant_project_id'] = project_id

    return await _show_assistant_servers(update, context, project_id)


async def ask_assistant_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    context.user_data['server_id'] = query.data.split('assist_server_')[1]
    keyboard = [[InlineKeyboardButton(name, callback_data=f"assist_role_{key}")] for key, name in
                ASSISTANT_ROLES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад (выбор сервера)", callback_data="assist_new_case")])
    await query.edit_message_text("Выберите вашу роль:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_ASSISTANT_STAGE


async def ask_assistant_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    role = query.data.split('assist_role_')[1]
    context.user_data['rp_role'] = role

    # Для судьи доступна только одна стадия - анализ судебного дела
    if role == "judge":
        available_stages = {"court_case": ASSISTANT_STAGES["court_case"]}
    else:
        # Для адвоката и прокурора доступны обе стадии
        available_stages = {
            "field_work": ASSISTANT_STAGES["field_work"],
            "case_analysis": ASSISTANT_STAGES["case_analysis"]
        }

    keyboard = [[InlineKeyboardButton(name, callback_data=f"assist_stage_{key}")] for key, name in
                available_stages.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад (выбор роли)",
                                          callback_data=f"assist_server_{context.user_data['server_id']}")])
    await query.edit_message_text("Выберите стадию вашей работы:", reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_STAGE_SELECTION


async def prompt_for_case_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_id = query.from_user.id
    context.user_data['case_stage'] = query.data.split('assist_stage_')[1]

    usage_type = f"{context.user_data['rp_role']}_{context.user_data['case_stage']}"
    # ВАЖНО: здесь только ПРОВЕРЯЕМ лимит (peek), НЕ списываем. Списание — в момент реального
    # создания дела (create_case_and_start_dialog). Раньше списание было тут, и простая навигация
    # (клик по стадии «анализ темы с форума») жгла лимит → после исчерпания юзера бесконечно
    # выкидывало на стартовый экран.
    can_ask, _ = await check_and_update_usage(user_id, usage_type, consume=False)
    if not can_ask:
        await safe_answer_callback_query(query)
        reset_h = config.ASSISTANT_LIMIT_RESET_HOURS
        await query.edit_message_text(
            f"⏳ <b>Лимит исчерпан</b>\n\n"
            f"Вы использовали все доступные дела этого типа за последние {reset_h} ч.\n"
            f"Попробуйте позже или оформите подписку — у подписчиков лимит выше.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="start_assistant")
            ]])
        )
        return AWAITING_STAGE_SELECTION

    # Те же поля из user_data (могли прийти из загруженного дела с легаси server_id) — безопасно.
    server_name = config.server_display_name(context.user_data['server_id'])
    role_name = ASSISTANT_ROLES.get(context.user_data['rp_role'], str(context.user_data['rp_role']))
    stage_name = ASSISTANT_STAGES.get(context.user_data['case_stage'], str(context.user_data['case_stage']))

    # Разный текст для судьи и для остальных ролей
    if context.user_data['rp_role'] == "judge":
        text = (
            f"<b>Контекст:</b> {server_name} / {role_name} / {stage_name}\n\n"
            "📋 <b>Отправьте ссылку на тему с форума</b> (судебное дело)\n\n"
            "Примеры:\n<code>https://forum.majestic-rp.ru/threads/иск-к-гражданину.12345/</code>\n"
            "<code>https://forum.gta5rp.com/threads/иск-к-гражданину.12345/</code>\n"
            "<code>https://forum.rmrp.ru/threads/иск-к-гражданину.12345/</code>\n"
            "<code>https://forum.russia.online/threads/иск-к-гражданину.12345/</code>"
        )
    else:
        text = (
            f"<b>Контекст:</b> {server_name} / {role_name} / {stage_name}\n\n"
            "Опишите фабулу дела. **Отправьте текст или пришлите ссылку** на Google Doc / Docossum."
        )

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data=f"assist_role_{context.user_data['rp_role']}")]]
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_CASE_DESCRIPTION


async def create_case_and_start_dialog(update: Update, context: ContextTypes.DEFAULT_TYPE, case_description: str,
                                       initial_request: str) -> int:
    """Создает дело и запускает первый вызов AI в фоне."""
    # Лимит списывается ЗДЕСЬ — в момент реального создания дела (1 списание = 1 дело).
    # На промпте раньше было consume → навигация жгла лимит и зацикливала юзера на старте.
    user_id = update.effective_user.id
    usage_type = f"{context.user_data.get('rp_role')}_{context.user_data.get('case_stage')}"
    can_ask, _ = await check_and_update_usage(user_id, usage_type)
    if not can_ask:
        reset_h = config.ASSISTANT_LIMIT_RESET_HOURS
        await update.effective_message.reply_text(
            f"⏳ Лимит исчерпан: вы использовали все доступные дела этого типа за последние {reset_h} ч.\n"
            f"Попробуйте позже или оформите подписку — у подписчиков лимит выше.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ В Ролевой Помощник", callback_data="start_assistant")
            ]])
        )
        return AWAITING_CASE_DESCRIPTION

    case_id = await assistant_logger.create_case(
        user_id=update.effective_user.id,
        server_id=context.user_data['server_id'],
        rp_role=context.user_data['rp_role'],
        case_stage=context.user_data['case_stage']
    )
    if not case_id:
        await update.effective_message.reply_text("Произошла ошибка при создании дела в базе данных.")
        return ConversationHandler.END

    context.user_data['case_id'] = case_id
    schedule_case_closure(context, case_id, update.effective_user.id, context.user_data['case_stage'])
    await assistant_logger.add_message_to_case(case_id, 'user', case_description)

    asyncio.create_task(_process_assistant_message(update, context, initial_request=initial_request))

    return ASSISTANT_DIALOGUE


async def handle_initial_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает текстовое описание и запускает обработку в фоне."""
    if not update.message or not update.message.text:
        return AWAITING_CASE_DESCRIPTION  # отредактированное/не-текстовое сообщение — игнор
    user_text = update.message.text
    return await create_case_and_start_dialog(update, context, case_description=user_text, initial_request=user_text)


async def handle_case_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ссылку и запускает обработку в фоне."""
    if not update.message or not update.message.text:
        return AWAITING_CASE_DESCRIPTION  # отредактированное/не-текстовое сообщение — игнор
    full_user_text = update.message.text
    processing_msg = await update.message.reply_text("Получил ссылку. Запускаю анализ...")
    found_url = None
    for entity in update.message.entities:
        if entity.type == MessageEntity.URL:
            found_url = full_user_text[entity.offset: entity.offset + entity.length]
            break
    if not found_url:
        await processing_msg.edit_text("Не удалось распознать ссылку в вашем сообщении.")
        return AWAITING_CASE_DESCRIPTION

    # НОВОЕ: Обработка ссылок на форум для судьи
    from court_case_parser import parse_court_case_from_url, is_supported_forum_url
    is_forum_link = is_supported_forum_url(found_url) and '/threads/' in found_url
    if context.user_data.get('rp_role') == "judge" and is_forum_link:
        from ocr_processor import process_multiple_images
        from config import COURT_OCR_MODEL_NAME

        await processing_msg.edit_text(
            "⚖️ <b>Обработка судебного дела</b>\n\n"
            "📥 Загружаю тему с форума...",
            parse_mode='HTML'
        )

        # Парсим тему форума
        case_data = await parse_court_case_from_url(found_url)

        if not case_data:
            await processing_msg.edit_text(
                "❌ ��е удалось загрузить тему с форума.\n"
                "Проверьте правильность ссылки и попробуйте еще раз."
            )
            return AWAITING_CASE_DESCRIPTION

        # Обновляем статус
        total_messages = len(case_data['messages'])
        total_images = sum(len(m.get('images', [])) for m in case_data['messages'])

        await processing_msg.edit_text(
            f"✅ Тема загружена!\n\n"
            f"📋 {case_data['thread_title']}\n"
            f"💬 Сообщений: {total_messages}\n"
            f"📸 Изображений: {total_images}\n\n"
            f"⏳ Распознаю текст с изображений...",
            parse_mode='HTML'
        )

        # OCR изображений
        if total_images > 0:
            all_images = []
            for msg in case_data['messages']:
                if msg.get('images'):
                    all_images.extend(msg['images'])

            try:
                ocr_results = await process_multiple_images(all_images, COURT_OCR_MODEL_NAME)

                # Обновляем данные изображений
                idx = 0
                for msg in case_data['messages']:
                    for img in msg.get('images', []):
                        if idx < len(ocr_results) and ocr_results[idx]:
                            img['ocr_text'] = ocr_results[idx].get('ocr_text', '')
                            img['document_type'] = ocr_results[idx].get('document_type', 'неизвестно')
                        idx += 1

                successful_ocr = sum(1 for r in ocr_results if r is not None)
                await processing_msg.edit_text(
                    f"✅ OCR завершен: {successful_ocr}/{total_images}\n\n"
                    f"🤖 Формирую контекст для анализа...",
                    parse_mode='HTML'
                )
            except Exception as e:
                logging.error(f"[AssistantFlow] Ошибка OCR: {e}", exc_info=True)
                await processing_msg.edit_text(
                    "⚠️ Ошибка при распознавании изображений.\n"
                    "Продолжаю с текстовым содержимым.",
                    parse_mode='HTML'
                )

        # Формируем контекст дела для AI
        context_parts = [
            f"📋 НАЗВАНИЕ ДЕЛА: {case_data['thread_title']}",
            f"👤 ИСТЕЦ: {case_data.get('plaintiff', 'Неизвестно')}",
            f"👤 ОТВЕТЧИК: {case_data.get('defendant', 'Неизвестно')}",
            f"🔗 URL: {case_data['thread_url']}",
            "",
            "=" * 50,
            "МАТЕРИАЛЫ ДЕЛА (ХРОНОЛОГИЯ)",
            "=" * 50,
            ""
        ]

        for msg in case_data['messages']:
            context_parts.append(
                f"[Сообщение #{msg['message_order']} | "
                f"Автор: {msg['author']} ({msg['author_role']}) | "
                f"Дата: {msg.get('message_timestamp', 'не указана')}]"
            )
            context_parts.append("")
            context_parts.append(msg['text_content'])

            # Добавляем распознанный текст с изображений
            if msg.get('images'):
                for img_idx, img in enumerate(msg['images'], 1):
                    if img.get('ocr_text'):
                        context_parts.append("")
                        context_parts.append(
                            f"    [ИЗОБРАЖЕНИЕ #{img_idx}: {img.get('document_type', 'документ')}]"
                        )
                        context_parts.append(f"    {img['ocr_text']}")

            context_parts.append("")
            context_parts.append("-" * 50)
            context_parts.append("")

        full_case_description = "\n".join(context_parts)
        initial_request = f"Проанализируй судебное дело: {case_data['thread_title']}"

        await processing_msg.delete()
        return await create_case_and_start_dialog(update, context,
                                                  case_description=full_case_description,
                                                  initial_request=initial_request)

    # Существующая логика для Google Docs и Docossum
    url_content = None
    if "docossum.space" in found_url or "docossum.com" in found_url:
        await processing_msg.edit_text("Ссылка на Docossum. Запускаю браузер для анализа...")
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                url_content = await fetch_case_content_from_url(page, found_url)
                await browser.close()
        except Exception as e:
            logging.critical(f"Критическая ошибка при запуске Playwright для {found_url}", exc_info=e)
            await processing_msg.edit_text("Произошла ошибка при запуске модуля анализа.")
            return AWAITING_CASE_DESCRIPTION
    else:
        url_content = await fetch_case_content_from_url(None, found_url)
    if not url_content:
        await processing_msg.edit_text("Не удалось извлечь текст из документа.")
        return AWAITING_CASE_DESCRIPTION
    await processing_msg.delete()
    full_case_description = (
        f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ:\n{full_user_text}\n\n"
        f"--- МАТЕРИАЛЫ ДЕЛА ИЗ ФАЙЛА ---\n{url_content}"
    )
    initial_request = full_user_text
    return await create_case_and_start_dialog(update, context, case_description=full_case_description,
                                              initial_request=initial_request)


async def handle_assistant_dialogue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает сообщения и запускает обработку в фоне."""
    # Защита: хендлер может получить апдейт без обычного сообщения — например,
    # ОТРЕДАКТИРОВАННОЕ сообщение (update.message = None, текст в update.edited_message)
    # или не-текстовый апдейт. Игнорируем всё, что не является текстовым сообщением,
    # чтобы не падать на update.message.text (AttributeError: 'NoneType').
    if not update.message or not update.message.text:
        return ASSISTANT_DIALOGUE

    if context.user_data.get('case_stage') == 'case_analysis' and context.user_data.get('case_id'):
        schedule_case_closure(context, context.user_data['case_id'], update.effective_user.id,
                              context.user_data['case_stage'])

    asyncio.create_task(_process_assistant_message(update, context, initial_request=update.message.text))

    return ASSISTANT_DIALOGUE


async def handle_assistant_tool_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает кнопки инструментов."""
    query = update.callback_query
    tool_name = query.data  # tool_court_add_materials_123 или tool_court_ask_question_123 или другие

    logging.info(f"[AssistantTool] ====== НАЖАТА КНОПКА ИНСТРУМЕНТА ======")
    logging.info(f"[AssistantTool] Кнопка: {tool_name}")
    logging.info(f"[AssistantTool] User ID: {update.effective_user.id}")
    logging.info(f"[AssistantTool] Case ID из context: {context.user_data.get('case_id')}")
    logging.info(f"[AssistantTool] Role: {context.user_data.get('rp_role')}")
    logging.info(f"[AssistantTool] ====== НАЖАТА КНОПКА ИНСТРУМЕНТА ======")
    logging.info(f"[AssistantTool] Кнопка: {tool_name}")
    logging.info(f"[AssistantTool] User ID: {update.effective_user.id}")
    logging.info(f"[AssistantTool] Case ID из context: {context.user_data.get('case_id')}")
    logging.info(f"[AssistantTool] Role: {context.user_data.get('rp_role')}")
    logging.info(f"[AssistantTool] Stage: {context.user_data.get('case_stage')}")
    logging.info(f"[AssistantTool] Stage: {context.user_data.get('case_stage')}")

    # НОВАЯ ЛОГИКА ДЛЯ СУДЬИ (с case_id в callback_data)
    if tool_name.startswith("tool_court_add_materials_"):
        await safe_answer_callback_query(query)
        # Извлекаем case_id из callback_data
        case_id = int(tool_name.split('_')[-1])
        context.user_data['case_id'] = case_id  # Обновляем на случай, если потерялся

        # Пользователь хочет дополнить материалы дела
        await query.edit_message_text(
            "📎 <b>Дополнение материалов дела</b>\n\n"
            "Отправьте дополнительную информацию по делу:\n"
            "• Новые документы\n"
            "• Дополнительные обстоятельства\n"
            "• Уточнения и пояснения\n\n"
            "<i>Просто напишите текст сообщения</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад к делу", callback_data="back_to_case")
            ]])
        )
        from config import AWAITING_ADDITIONAL_MATERIALS
        return AWAITING_ADDITIONAL_MATERIALS

    elif tool_name.startswith("tool_court_ask_question_"):
        await safe_answer_callback_query(query)
        # Извлекаем case_id из callback_data
        case_id = int(tool_name.split('_')[-1])
        context.user_data['case_id'] = case_id  # Обновляем на случай, если потерялся

        # Пользователь хочет задать вопрос или запроси��ь помощь
        await query.edit_message_text(
            "❓ <b>Задайте вопрос или запросите помощь</b>\n\n"
            "Примеры запросов:\n"
            "• <i>Проведи процессуальный аудит дела</i>\n"
            "• <i>Какие нарушения процедуры допущены?</i>\n"
            "• <i>Помоги составить постановление</i>\n"
            "• <i>Проанализируй доказательства</i>\n"
            "• <i>Проверь соблюдение сроков</i>\n\n"
            "<i>Напишите ваш запрос...</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад к делу", callback_data="back_to_case")
            ]])
        )
        # Остаемся в том же состоянии, ждем текстового сообщения
        context.user_data['waiting_for_question'] = True
        return ASSISTANT_DIALOGUE

    elif tool_name.startswith("tool_court_refresh_case_"):
        # Извлекаем case_id из callback_data
        case_id = int(tool_name.split('_')[-1])

        # Обновляем context.user_data на случай, если потерялся
        context.user_data['case_id'] = case_id

        # ВАЖНО: Подтверждаем нажатие кнопки
        await query.answer("🔄 Начинаю обновление дела...", show_alert=False)

        # ВАЖНО: Сразу показываем пользователю что обновление началось
        await query.edit_message_text(
            "🔄 <b>Обновление дела...</b>\n\n"
            "📥 Загружаю актуальную информацию с форума...\n\n"
            "<i>Это может занять несколько секунд</i>",
            parse_mode='HTML'
        )

        # Запускаем обновление в фоне с явной передачей case_id
        asyncio.create_task(refresh_court_case(update, context, case_id))
        return ASSISTANT_DIALOGUE

    # СТАРАЯ ЛОГИКА ДЛЯ АДВОКАТА И ПРОКУРОРА
    await safe_answer_callback_query(query)

    if context.user_data.get('case_stage') == 'case_analysis' and context.user_data.get('case_id'):
        schedule_case_closure(context, context.user_data['case_id'], update.effective_user.id,
                              context.user_data['case_stage'])

    asyncio.create_task(_process_assistant_message(update, context, initial_request=""))

    return ASSISTANT_DIALOGUE


async def refresh_court_case(update: Update, context: ContextTypes.DEFAULT_TYPE, case_id: int) -> None:
    """Обновляет судебное дело - перепарсивает тему форума и добавляет новые сообщения."""
    query = update.callback_query

    logging.info(f"[RefreshCase] Начало обновления дела #{case_id}")

    if not case_id:
        logging.error("[RefreshCase] case_id не передан в функцию")
        await query.answer("❌ Ошибка: дело не найдено", show_alert=True)
        return

    # Получаем URL исходной темы из истории дела
    case_history = await assistant_logger.get_case_history(case_id)

    if not case_history or len(case_history) == 0:
        logging.error(f"[RefreshCase] История дела #{case_id} пуста")
        await query.answer("❌ Не удалось найти исходную тему", show_alert=True)
        return

    # Ищем URL в первом сообщении (где была загружена тема)
    first_message = case_history[0]['content']
    import re
    url_match = re.search(
        r'(https?://forum\.(?:majestic-rp\.ru|gta5rp\.com|rmrp\.ru|russia\.online)/threads/[^\s]+)',
        first_message,
    )

    if not url_match:
        await query.edit_message_text(
            "❌ <b>Не найдена ссылка на тему форума</b>\n\n"
            "Дело не содержит ссылку на исходную тему.",
            parse_mode='HTML'
        )
        return

    forum_url = url_match.group(1)

    # Сообщение уже показано в обработчике, просто обновляем его с подробностями
    await query.edit_message_text(
        "🔄 <b>Обновление дела...</b>\n\n"
        f"📥 Загружаю тему с форума...\n"
        f"🔗 {forum_url[:50]}...",
        parse_mode='HTML'
    )

    try:
        # Парсим тему заново
        from court_case_parser import parse_court_case_from_url
        from ocr_processor import process_multiple_images
        from config import COURT_OCR_MODEL_NAME

        case_data = await parse_court_case_from_url(forum_url)

        if not case_data:
            await query.edit_message_text(
                "❌ <b>Не удалось загрузить тему с форума</b>\n\n"
                "Попробуйте позже.",
                parse_mode='HTML'
            )
            return

        # Подсчитываем новые сообщения
        # Получаем количество сообщений из исходной загрузки
        old_messages_count = first_message.count('[Сообщение #')
        new_messages_count = len(case_data['messages'])
        new_messages_added = new_messages_count - old_messages_count

        if new_messages_added <= 0:
            await query.edit_message_text(
                "ℹ️ <b>Новых сообщений не найдено</b>\n\n"
                f"📋 {case_data['thread_title']}\n"
                f"💬 Всего сообщений: {new_messages_count}\n\n"
                "Дело уже содержит актуальную информацию.",
                parse_mode='HTML'
            )

            # Показываем кнопки обратно через несколько секунд
            await asyncio.sleep(3)
            await show_case_tools_after_update(update, context)
            return

        # Обновляем статус
        await query.edit_message_text(
            f"✅ <b>Найдено новых сообщений: {new_messages_added}</b>\n\n"
            f"📸 Изображений в новых сообщениях: {sum(len(m.get('images', [])) for m in case_data['messages'][old_messages_count:])}\n\n"
            f"⏳ Распознаю изображения...",
            parse_mode='HTML'
        )

        # OCR для новых изображений
        new_images = []
        for msg in case_data['messages'][old_messages_count:]:
            if msg.get('images'):
                new_images.extend(msg['images'])

        if new_images:
            try:
                ocr_results = await process_multiple_images(context, new_images, COURT_OCR_MODEL_NAME)

                # Обновляем данные изображений
                idx = 0
                for msg in case_data['messages'][old_messages_count:]:
                    for img in msg.get('images', []):
                        if idx < len(ocr_results) and ocr_results[idx]:
                            img['ocr_text'] = ocr_results[idx].get('ocr_text', '')
                            img['document_type'] = ocr_results[idx].get('document_type', 'неизвестно')
                        idx += 1

                successful_ocr = sum(1 for r in ocr_results if r is not None)
                await query.edit_message_text(
                    f"✅ <b>OCR завершен: {successful_ocr}/{len(new_images)}</b>\n\n"
                    f"💾 Сохраняю обновления...",
                    parse_mode='HTML'
                )
            except Exception as e:
                logging.error(f"[RefreshCase] Ошибка OCR: {e}", exc_info=True)

        # Формируем контекст новых сообщений
        update_context_parts = [
            f"🔄 ОБНОВЛЕНИЕ ДЕЛА (добавлено новых сообщений: {new_messages_added})",
            f"🔗 Ссылка: {forum_url}",
            f"📋 Тема: {case_data['thread_title']}",
            "",
            "=" * 50,
            "НОВЫЕ СООБЩЕНИЯ:",
            "=" * 50,
            ""
        ]

        # Добавляем только новые сообщения
        for msg in case_data['messages'][old_messages_count:]:
            update_context_parts.append(
                f"[НОВОЕ Сообщение #{msg['message_order']} | "
                f"Автор: {msg['author']} ({msg['author_role']}) | "
                f"Дата: {msg.get('message_timestamp', 'не указана')}]"
            )
            update_context_parts.append("")
            update_context_parts.append(msg['text_content'])

            # Добавляем распознанный текст с изображений
            if msg.get('images'):
                for img_idx, img in enumerate(msg['images'], 1):
                    if img.get('ocr_text'):
                        update_context_parts.append("")
                        update_context_parts.append(
                            f"    [ИЗОБРАЖЕНИЕ #{img_idx}: {img.get('document_type', 'документ')}]"
                        )
                        update_context_parts.append(f"    {img['ocr_text']}")

            update_context_parts.append("")
            update_context_parts.append("-" * 50)
            update_context_parts.append("")

        full_update_context = "\n".join(update_context_parts)

        # Сохраняем обновление в БД
        await assistant_logger.add_message_to_case(case_id, 'user', full_update_context)

        await query.edit_message_text(
            f"✅ <b>Дело успешно обновлено!</b>\n\n"
            f"📋 {case_data['thread_title']}\n"
            f"✨ Добавлено новых сообщений: {new_messages_added}\n"
            f"💬 Всего сообщений в деле: {new_messages_count}\n\n"
            f"Новая информация учтена и доступна для анализа.",
            parse_mode='HTML'
        )

        # Показываем кнопки обратно
        await asyncio.sleep(2)
        await show_case_tools_after_update(update, context)

    except Exception as e:
        logging.error(f"[RefreshCase] Ошибка при обновлении дела: {e}", exc_info=True)
        await query.edit_message_text(
            "❌ <b>Ошибка при обновлении дела</b>\n\n"
            "Произошла ошибка при загрузке данных с форума.\n"
            "Попробуйте позже.",
            parse_mode='HTML'
        )
        await asyncio.sleep(2)
        await show_case_tools_after_update(update, context)


async def show_case_tools_after_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает кнопки после обновления дела."""
    case_id = context.user_data.get('case_id')
    rp_role = context.user_data.get('rp_role')
    case_stage = context.user_data.get('case_stage')

    if not case_id:
        logging.error("[ShowCaseTools] case_id отсутствует в context.user_data")
        return

    # Используем универсальную функцию формирования кнопок
    reply_markup = _get_assistant_buttons(rp_role, case_stage, case_id)

    # Получаем последнее сообщение от бота
    query = update.callback_query
    await query.edit_message_text(
        ASSISTANT_ACTION_HINT,
        parse_mode='HTML',
        reply_markup=reply_markup
    )


async def close_assistant_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Закрывает дело и снимает задачу автозакрытия."""
    query = update.callback_query
    logging.info(f"[CloseCase] ====== ВЫЗВАНА ФУНКЦИЯ close_assistant_case ======")
    logging.info(f"[CloseCase] Callback data: {query.data}")
    logging.info(f"[CloseCase] User ID: {update.effective_user.id}")
    case_id = int(query.data.split('close_case_')[1])
    _remove_scheduled_job(context, case_id)
    await query.answer("Консультация завершена.", show_alert=True)
    await query.edit_message_text("✅ Консультация по этому делу завершена.")
    context.user_data.clear()
    await assistant_logger.close_case(case_id)
    return ConversationHandler.END


async def receive_additional_materials(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимает дополнительные материалы от судьи (текст или ссылка на форум)."""
    user_text = update.message.text
    case_id = context.user_data.get('case_id')

    if not case_id:
        await update.message.reply_text("❌ Ошибка: дело не найдено.")
        return ConversationHandler.END

    # Проверяем, есть ли в сообщении ссылка на форум
    forum_url = None
    user_comment = user_text  # Комментарий пользователя (если есть)

    # Ищем ссылку на поддерживаемые форумы
    from court_case_parser import SUPPORTED_FORUMS
    import re

    for forum_domain in SUPPORTED_FORUMS:
        if f'{forum_domain}/threads/' in user_text:
            # Извлекаем URL
            url_pattern = re.compile(rf'(https?://{re.escape(forum_domain)}/threads/[^\s]+)')
            match = url_pattern.search(user_text)

            if match:
                forum_url = match.group(1)
                # Убираем URL из комментария, оставляем только текст пользователя
                user_comment = user_text.replace(forum_url, '').strip()
                break


    # Если есть ссылка на форум - парсим её
    if forum_url:
        processing_msg = await update.message.reply_text(
            "🔗 <b>Обнаружена ссылка на форум!</b>\n\n"
            "📥 Загружаю дополнительные материалы...",
            parse_mode='HTML'
        )

        try:
            # Парсим тему с форума
            from court_case_parser import parse_court_case_from_url

            case_data = await parse_court_case_from_url(forum_url)

            if not case_data:
                await processing_msg.edit_text(
                    "❌ Не удалось загрузить материалы с форума.\n"
                    "Проверьте правильность ссылки."
                )
                return AWAITING_ADDITIONAL_MATERIALS

            # Формируем контекст из дополнительных материалов
            additional_context_parts = [
                f"📎 ДОПОЛНИТЕЛЬНЫЕ МАТЕРИАЛЫ (из темы форума)",
                f"🔗 Ссылка: {forum_url}",
                f"📋 Тема: {case_data['thread_title']}",
                ""
            ]

            # Добавляем комментарий пользователя, если есть
            if user_comment:
                additional_context_parts.append(f"💬 Комментарий судьи: {user_comment}")
                additional_context_parts.append("")

            additional_context_parts.append("=" * 50)
            additional_context_parts.append("СОДЕРЖИМОЕ ТЕМЫ:")
            additional_context_parts.append("=" * 50)
            additional_context_parts.append("")

            # Добавляем все сообщения из темы
            for msg in case_data['messages']:
                additional_context_parts.append(
                    f"[Сообщение #{msg['message_order']} | "
                    f"Автор: {msg['author']} | "
                    f"Дата: {msg.get('message_timestamp', 'не указана')}]"
                )
                additional_context_parts.append("")
                additional_context_parts.append(msg['text_content'])
                additional_context_parts.append("")
                additional_context_parts.append("-" * 50)
                additional_context_parts.append("")

            full_additional_context = "\n".join(additional_context_parts)

            # Сохраняем в БД
            await assistant_logger.add_message_to_case(case_id, 'user', full_additional_context)

            await processing_msg.edit_text(
                f"✅ <b>Дополнительные материалы добавлены!</b>\n\n"
                f"📋 Тема: {case_data['thread_title']}\n"
                f"💬 Сообщений: {len(case_data['messages'])}\n\n"
                f"Информация учтена и будет использоваться в анализе.",
                parse_mode='HTML'
            )

        except Exception as e:
            logging.error(f"[AssistantFlow] Ошибка при парсинге дополнительных материалов: {e}", exc_info=True)
            await processing_msg.edit_text(
                "❌ Произошла ошибка при загрузке материалов с форума.\n"
                "Попробуйте еще раз или отправьте информацию текстом."
            )
            return AWAITING_ADDITIONAL_MATERIALS

    else:
        # Обычный текст без ссылки
        await assistant_logger.add_message_to_case(case_id, 'user', user_text)

        await update.message.reply_text(
            "✅ <b>Материалы добавлены к делу!</b>\n\n"
            "Дополнительная информация учтена и будет использоваться в анализе.",
            parse_mode='HTML'
        )

    # Показываем подсказку
    await update.message.reply_text(
        "Теперь вы можете:\n"
        "• Задать вопрос по делу\n"
        "• Добавить еще материалы\n"
        "• Завершить работу с делом",
        parse_mode='HTML'
    )

    # Возвращаемся к диалогу с кнопками
    return await show_case_tools(update, context)


async def show_case_tools(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает инструменты для работы с делом."""
    case_id = context.user_data.get('case_id')
    rp_role = context.user_data.get('rp_role')
    case_stage = context.user_data.get('case_stage')

    if not case_id:
        await update.message.reply_text("❌ Ошибка: дело не найдено.")
        return ConversationHandler.END

    # Используем универсальную функцию формирования кнопок
    reply_markup = _get_assistant_buttons(rp_role, case_stage, case_id)

    await update.message.reply_text(
        ASSISTANT_ACTION_HINT,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

    return ASSISTANT_DIALOGUE


async def back_to_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возвращает к просмотру дела с кнопками."""
    query = update.callback_query
    logging.info(f"[BackToCase] ====== ВЫЗВАНА ФУНКЦИЯ back_to_case ======")
    logging.info(f"[BackToCase] Callback data: {query.data}")
    logging.info(f"[BackToCase] User ID: {update.effective_user.id}")
    await safe_answer_callback_query(query)

    # Убираем флаг ожидания вопроса
    context.user_data.pop('waiting_for_question', None)

    # Получаем данные из context
    case_id = context.user_data.get('case_id')
    rp_role = context.user_data.get('rp_role')
    case_stage = context.user_data.get('case_stage')

    # Показываем кнопки снова

    if not case_id:
        await query.edit_message_text("❌ Ошибка: дело не найдено.")
        return ConversationHandler.END

    # Используем универсальную функцию формирования кнопок
    reply_markup = _get_assistant_buttons(rp_role, case_stage, case_id)

    await query.edit_message_text(
        ASSISTANT_ACTION_HINT,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

    return ASSISTANT_DIALOGUE


async def view_active_cases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список активных дел пользователя."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_id = query.from_user.id
    active_cases = await assistant_logger.get_active_cases_for_user(user_id)
    text = "🗂️ **Ваши активные дела:**\n\n"
    keyboard = []
    if not active_cases:
        text += "У вас пока нет активных дел. Начните новое!"
    else:
        for case in active_cases:
            server_name = config.SERVERS_CONFIG.get(case['server_id'], {}).get('display_name', case['server_id'])
            role_name = ASSISTANT_ROLES.get(case['rp_role'], case['rp_role'])
            created_at_local = case['created_at'].astimezone(datetime.now().astimezone().tzinfo).strftime('%d.%m %H:%M')
            button_text = f"Дело #{case['id']} - {server_name} ({role_name}) от {created_at_local}"
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"open_assist_case_{case['id']}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад в меню Ассистента", callback_data="start_assistant")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return VIEWING_ACTIVE_CASES


async def _load_case_context_and_display(update: Update, context: ContextTypes.DEFAULT_TYPE, case_id: int) -> int:
    """Загружает всю историю дела и отображает ее пользователю."""
    query = update.callback_query
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    context.user_data.clear()
    case_details = await db_manager.fetchrow(
        "SELECT id, user_id, server_id, rp_role, case_stage, status FROM assistant_cases WHERE id = $1 AND user_id = $2",
        case_id, user_id
    )
    if not case_details or case_details['status'] == 'closed':
        await context.bot.send_message(chat_id, "Ошибка: дело не найдено или уже закрыто.")
        return CHOOSING_ASSISTANT_ROLE
    context.user_data.update(dict(case_details))
    history = await assistant_logger.get_case_history(case_id)
    if history is None:
        await context.bot.send_message(chat_id, "Ошибка при загрузке истории дела.")
        return CHOOSING_ASSISTANT_ROLE
    # server_id/role/stage берутся из СОХРАНЁННОГО дела — там может лежать легаси-значение
    # (напр. орфан 'Denver' с заглавной) → без .get это роняло хендлер KeyError.
    server_name = config.server_display_name(case_details['server_id'])
    role_name = ASSISTANT_ROLES.get(case_details['rp_role'], str(case_details['rp_role']))
    stage_name = ASSISTANT_STAGES.get(case_details['case_stage'], str(case_details['case_stage']))
    conversation_text = (
            f"<b>⚖️ Продолжение дела #{case_id}</b>\n"
            f"<b>Контекст:</b> {server_name} / {role_name} / {stage_name}\n" + "=" * 20 + "\n\n"
    )
    for msg in history:
        sender = "<b>Вы</b>" if msg['sender_role'] == 'user' else "<b>Ассистент</b>"
        # Санитизируем каждое сообщение отдельно для безопасности
        safe_content = sanitize_telegram_html(msg['content'])
        conversation_text += f"{sender}:\n{safe_content}\n\n"
    reply_markup = _get_assistant_buttons(case_details['rp_role'], case_details['case_stage'], case_id)
    # Дополнительная санитизация всего текста на случай, если что-то пропустили
    cleaned_conversation_text = sanitize_telegram_html(conversation_text)
    parts = _split_long_message(cleaned_conversation_text, limit=4096)
    for i, part in enumerate(parts):
        current_markup = reply_markup if i == len(parts) - 1 else None
        if i == 0 and query:
            await query.edit_message_text(text=part, reply_markup=current_markup, parse_mode='HTML')
        else:
            await context.bot.send_message(chat_id, text=part, parse_mode='HTML', reply_markup=current_markup)
        await asyncio.sleep(0.5)
    return ASSISTANT_DIALOGUE


async def open_assistant_case_from_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик для кнопки открытия конкретного дела из списка."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    try:
        case_id = int(query.data.split('open_assist_case_')[1])
    except (ValueError, IndexError):
        await query.edit_message_text("Ошибка при определении номера дела.")
        return VIEWING_ACTIVE_CASES
    return await _load_case_context_and_display(update, context, case_id)


# --- Определяем ConversationHandler для Ролевого Помощника ---
assistant_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(start_assistant, pattern='^start_assistant$')],
    states={
        CHOOSING_PROJECT: [
            CallbackQueryHandler(select_assistant_project, pattern=r'^assist_project_')
        ],
        CHOOSING_ASSISTANT_ROLE: [
            CallbackQueryHandler(prompt_new_case, pattern='^assist_new_case$'),
            CallbackQueryHandler(view_active_cases, pattern='^assist_view_active$'),
            CallbackQueryHandler(ask_assistant_role, pattern=r'^assist_server_')
        ],
        CHOOSING_ASSISTANT_STAGE: [
            CallbackQueryHandler(ask_assistant_stage, pattern=r'^assist_role_')
        ],
        AWAITING_STAGE_SELECTION: [
            CallbackQueryHandler(prompt_for_case_description, pattern=r'^assist_stage_'),
            CallbackQueryHandler(ask_assistant_role, pattern=r'^assist_server_')
        ],
        AWAITING_CASE_DESCRIPTION: [
            MessageHandler(filters.Entity(MessageEntity.URL) & ~filters.COMMAND, handle_case_url),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_initial_description),
            CallbackQueryHandler(ask_assistant_stage, pattern=r'^assist_role_')
        ],
        ASSISTANT_DIALOGUE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_assistant_dialogue),
            CallbackQueryHandler(handle_assistant_tool_click, pattern=r'^tool_'),
            CallbackQueryHandler(close_assistant_case, pattern=r'^close_case_'),
            CallbackQueryHandler(back_to_case, pattern=r'^back_to_case$')
        ],
        AWAITING_ADDITIONAL_MATERIALS: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_additional_materials),
            CallbackQueryHandler(back_to_case, pattern=r'^back_to_case$')
        ],
        VIEWING_ACTIVE_CASES: [
            CallbackQueryHandler(open_assistant_case_from_list, pattern=r'^open_assist_case_'),
            CallbackQueryHandler(start_assistant, pattern='^start_assistant$')
        ],
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(main_menu, pattern='^main_menu$'),
        CallbackQueryHandler(start_assistant, pattern='^start_assistant$'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
    ],
    per_message=False,
    allow_reentry=True
)

# END OF FILE user_handlers/assistant_flow.py
