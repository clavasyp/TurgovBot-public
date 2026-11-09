# START OF FILE user_handlers/interview_flow.py

"""
Модуль для управления диалогом генерации вопросов для собеседований.

Содержит:
- `start_question_generation`: Точка входа в функцию.
- `ask_interview_role`: Запрос должности.
- `ask_interview_difficulty`: Запрос уровня сложности.
- `prompt_for_interview_quantity`: Запрос количества вопросов.
- `generate_interview_questions`: Обработка количества и запуск генерации.
- `process_and_send_interview_questions`: Асинхронная задача для вызова AI.
- `interview_conv_handler`: ConversationHandler для этого диалога.
"""
import asyncio
import logging

from google import genai
from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters

# Локальные импорты
import config
from database.manager import db_manager
from utils import get_enabled_servers, get_enabled_projects, send_project_selection_menu, rate_limited, reinitialize_vertex_ai, safe_answer_callback_query, retry_with_exponential_backoff
from prompts import get_interview_questions_prompt
from .core import main_menu, start, check_and_update_usage, send_error_message, _display_main_menu, unknown_text_fallback
from .qa_flow import send_answer_with_clarify_button, save_history  # Импортируем функцию отправки ответа
from config import (
    CHOOSING_INTERVIEW_SERVER, CHOOSING_INTERVIEW_ROLE, CHOOSING_INTERVIEW_DIFFICULTY,
    AWAITING_INTERVIEW_QUANTITY,
    INTERVIEW_ROLES, INTERVIEW_DIFFICULTIES, MIN_QUESTIONS, MAX_QUESTIONS,
    QUESTION_GENERATOR_MODEL_NAME,
    GENERATION_HISTORY_PREFIX,
    PROJECTS_CONFIG
)
from database.manager import db_manager
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error


# Нужно импортировать функцию сохранения в историю
async def save_history(chat_id: int, server_id: str, role: str, content: str):
    query = "INSERT INTO message_history (user_id, server_id, role, content) VALUES ($1, $2, $3, $4);"
    await db_manager.execute(query, chat_id, server_id, role, content)


@rate_limited
async def start_question_generation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Начинает диалог генерации вопросов, запрашивая проект.
    """
    query = update.callback_query
    await safe_answer_callback_query(query)

    enabled_projects = await get_enabled_projects()

    # Если только один проект - пропускаем выбор и сразу показываем серверы
    if len(enabled_projects) == 1:
        context.user_data['interview_project_id'] = enabled_projects[0]
        return await _show_interview_servers(update, context, enabled_projects[0])

    # Удаляем старое сообщение
    try:
        await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта с картинкой
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="interview_project_",
        text="🌐 <b>Для какого проекта генерируем вопросы?</b>",
        back_callback="main_menu"
    )
    return CHOOSING_INTERVIEW_SERVER  # Переиспользуем состояние (оно станет CHOOSING_PROJECT)


async def _show_interview_servers(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: str) -> int:
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

    keyboard = [[InlineKeyboardButton(config.SERVERS_CONFIG[server_id]['display_name'],
                                      callback_data=f"interview_server_{server_id}")] for server_id in enabled_servers]

    # Кнопка "Назад" зависит от количества проектов
    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) > 1:
        keyboard.append([InlineKeyboardButton("◀️ К выбору проекта", callback_data="start_interview_gen")])
    else:
        keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data="main_menu")])

    text = f"🎮 <b>{project_name}</b>\n\nДля какого сервера генерируем вопросы?"
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
    return CHOOSING_INTERVIEW_SERVER


async def select_interview_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный проект и показывает серверы."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    project_id = query.data.split("interview_project_", 1)[1]
    context.user_data['interview_project_id'] = project_id

    return await _show_interview_servers(update, context, project_id)


async def ask_interview_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запрашивает у пользователя должность для генерации вопросов.
    """
    query = update.callback_query
    await safe_answer_callback_query(query)
    context.user_data['interview_server_id'] = query.data.split("interview_server_", 1)[1]

    keyboard = [[InlineKeyboardButton(name, callback_data=f"interview_role_{key}")] for key, name in
                INTERVIEW_ROLES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад (выбор сервера)", callback_data="start_interview_gen")])
    await query.edit_message_text("Выберите должность:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_INTERVIEW_ROLE


async def ask_interview_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запрашивает уровень сложности вопросов.
    """
    query = update.callback_query
    await safe_answer_callback_query(query)
    context.user_data['interview_role_key'] = query.data.split("interview_role_", 1)[1]

    keyboard = [[InlineKeyboardButton(name, callback_data=f"interview_diff_{key}")] for key, name in
                INTERVIEW_DIFFICULTIES.items()]
    keyboard.append([InlineKeyboardButton("◀️ Назад (выбор должности)",
                                          callback_data=f"interview_server_{context.user_data['interview_server_id']}")])
    await query.edit_message_text("Выберите уровень сложности:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSING_INTERVIEW_DIFFICULTY


async def prompt_for_interview_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запрашивает количество вопросов для генерации.
    """
    query = update.callback_query
    await query.answer()
    context.user_data['interview_diff_key'] = query.data.split("interview_diff_", 1)[1]

    keyboard = [[InlineKeyboardButton("◀️ Назад (выбор сложности)",
                                      callback_data=f"interview_role_{context.user_data['interview_role_key']}")]]
    await query.edit_message_text(f"Введите количество вопросов (от {MIN_QUESTIONS} до {MAX_QUESTIONS}):",
                                  reply_markup=InlineKeyboardMarkup(keyboard))
    return AWAITING_INTERVIEW_QUANTITY


async def generate_interview_questions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Проверяет введённое количество, лимиты и запускает фоновую задачу генерации.
    """
    message = update.message
    try:
        quantity = int(message.text)
        if not (MIN_QUESTIONS <= quantity <= MAX_QUESTIONS):
            raise ValueError
    except (ValueError, TypeError):
        await message.reply_text(f"Введите корректное число от {MIN_QUESTIONS} до {MAX_QUESTIONS}.")
        return AWAITING_INTERVIEW_QUANTITY

    # Проверка лимитов
    can_ask, _ = await check_and_update_usage(update.effective_chat.id, 'interview')
    if not can_ask:
        await message.reply_text(f"Вы достигли дневного лимита на генерацию вопросов.")
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END

    processing_message = await message.reply_text("✅ Запрос принят. Генерирую вопросы...")

    # Запускаем тяжелую задачу в фоне, чтобы не блокировать бота
    asyncio.create_task(
        process_and_send_interview_questions(
            context, update.effective_chat.id, quantity, processing_message.message_id
        )
    )
    return ConversationHandler.END


async def process_and_send_interview_questions(context: ContextTypes.DEFAULT_TYPE, chat_id: int, quantity: int,
                                               processing_message_id: int):
    """
    Асинхронная задача, которая выполняет запрос к AI и отправляет результат.
    """
    task_start_time = asyncio.get_running_loop().time()
    logging.info(f"TASK_START: Запущена задача генерации вопросов для {chat_id}.")
    try:
        if not await reinitialize_vertex_ai():
            await context.bot.send_message(chat_id,
                                           text="Произошла критическая ошибка аутентификации. Пожалуйста, попробуйте позже или сообщите администратору.")
            return

        # Собираем все данные из user_data
        server_id = context.user_data['interview_server_id']
        role_key = context.user_data['interview_role_key']
        diff_key = context.user_data['interview_diff_key']
        server_name = config.server_display_name(server_id)
        role_name = INTERVIEW_ROLES.get(role_key, str(role_key))
        diff_name = INTERVIEW_DIFFICULTIES.get(diff_key, str(diff_key))

        # Получаем все законы для выбранного сервера
        all_law_records = await db_manager.fetch(
            "SELECT gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )

        files_for_query = [r['gemini_file_uri'] for r in all_law_records]
        if not files_for_query:
            await context.bot.send_message(chat_id,
                                           f"Ошибка: база знаний для сервера '{server_name}' пуста или временно недоступна.")
            return

        # Формируем промпт и вызываем модель
        system_prompt = get_interview_questions_prompt(role_name, diff_name, quantity, server_name)
        content_parts = [types.Part.from_uri(file_uri=uri, mime_type="text/plain") for uri in files_for_query]

        # Определяем функцию для выполнения с rate limiting
        async def send_interview_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=QUESTION_GENERATOR_MODEL_NAME,
                contents=content_parts,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt
                )
            )

        logging.info(f"[GENERATOR] Отправка запроса в {QUESTION_GENERATOR_MODEL_NAME} для {chat_id}")

        try:
            # Используем rate limiter с НОРМАЛЬНЫМ приоритетом (генерация вопросов - не критичная задача)
            response = await execute_vertex_request(
                func=send_interview_request,
                model_name=QUESTION_GENERATOR_MODEL_NAME,
                priority=RequestPriority.NORMAL,
                user_id=chat_id,
                max_retries=3
            )
            # Отмечаем успех в текущем регионе
            await mark_current_region_success()
            logging.info(f"[GENERATOR] Запрос успешно выполнен для {chat_id}")
        except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
            # Отмечаем ошибку в текущем регионе
            await mark_current_region_error()

            # Проверяем, что это действительно 429 ошибка
            if isinstance(e, genai_errors.ClientError):
                error_str = str(e)
                if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                    raise  # Пробрасываем не-429 ошибки дальше

            logging.error(f"[GENERATOR] Не удалось выполнить запрос для пользователя {chat_id}: {e}")
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ К сожалению, AI-сервис временно перегружен.\n\n"
                     "Система управления нагрузкой не смогла обработать ваш запрос.\н\n"
                     "Пожалуйста, попробуйте сгенерировать вопросы через 2-3 минуты.\n\n"
                     "Приносим извинения за неудобства."
            )
            return

        # Формируем и отправляем ответ
        header = (
            f"<b>📝 Сгенерированные вопросы для собеседования</b>\n\n"
            f"<b>Сервер:</b> {server_name}\n"
            f"<b>Должность:</b> {role_name}\n"
            f"<b>Сложность:</b> {diff_name}\n\n---"
        )
        full_response_text = f"{header}\n\n{response.text}"

        # Используем общую функцию для отправки ответа с кнопками
        sent_message = await send_answer_with_clarify_button(context, chat_id, full_response_text)
        if sent_message:
            context.user_data['last_answer_message_id'] = sent_message.message_id

        # Сохраняем в историю
        user_request_text = f"Запрос на генерацию вопросов: {role_name} ({diff_name}, {quantity} шт.)"
        prefixed_request = f"{GENERATION_HISTORY_PREFIX}{user_request_text}"
        await save_history(chat_id, server_id, 'user', prefixed_request)
        await save_history(chat_id, server_id, 'model', full_response_text)

    except Exception as e:
        logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА в `process_and_send_interview_questions` для {chat_id}.", exc_info=e)
        await send_error_message(context, chat_id)
    finally:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=processing_message_id)
        except BadRequest:
            pass  # Сообщение могло быть уже удалено
        task_duration = (asyncio.get_running_loop().time() - task_start_time) * 1000
        logging.info(f"TASK_END: Задача генерации вопросов для {chat_id} завершена за {task_duration:.2f} мс.")


# Определяем ConversationHandler для генератора вопросов
interview_conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(start_question_generation, pattern='^start_interview_gen$')],
    states={
        CHOOSING_INTERVIEW_SERVER: [
            CallbackQueryHandler(select_interview_project, pattern=r'^interview_project_'),
            CallbackQueryHandler(ask_interview_role, pattern=r'^interview_server_')
        ],
        CHOOSING_INTERVIEW_ROLE: [CallbackQueryHandler(ask_interview_difficulty, pattern=r'^interview_role_')],
        CHOOSING_INTERVIEW_DIFFICULTY: [
            CallbackQueryHandler(prompt_for_interview_quantity, pattern=r'^interview_diff_')],
        AWAITING_INTERVIEW_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, generate_interview_questions)],
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(main_menu, pattern='^main_menu$'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
    ],
    per_message=False,
    allow_reentry=True
)

# END OF FILE user_handlers/interview_flow.py