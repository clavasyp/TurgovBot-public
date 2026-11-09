# user_handlers/complaint_flow.py
"""
Модуль для управления диалогом составления жалоб в прокуратуру и исков в суд.

Содержит:
- `start_complaint_generation`: Точка входа в функцию.
- `choose_complaint_type`: Выбор типа документа (жалоба/иск).
- `choose_complaint_server`: Выбор сервера.
- `ask_forum_url`: Запрос ссылки на форум с формой.
- `ask_situation_description`: Запрос описания ситуации.
- `generate_complaint_document`: Обработка и запуск генерации.
- `process_and_send_complaint`: Асинхронная задача для вызова AI.
- `complaint_conv_handler`: ConversationHandler для этого диалога.
"""

import asyncio
import logging
import os
import re
from datetime import datetime

from google import genai
from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters

# Локальные импорты
import config
from database.manager import db_manager
from utils import get_enabled_servers, get_enabled_projects, send_project_selection_menu, rate_limited, retry_with_exponential_backoff
from prompts import get_complaint_generation_prompt, get_complaint_router_prompt
from html_to_bbcode import html_to_bbcode, save_bbcode_to_file, sanitize_telegram_html
from court_case_parser import parse_court_case_from_url, SUPPORTED_FORUMS
from .core import main_menu, start, check_and_update_usage, send_error_message, _display_main_menu, unknown_text_fallback
from .ai_retry import register_retry, retry_keyboard
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error

from config import (
    CHOOSING_COMPLAINT_TYPE, CHOOSING_COMPLAINT_SERVER, AWAITING_FORUM_URL,
    AWAITING_SITUATION_DESCRIPTION,
    COMPLAINT_TYPES, COMPLAINT_GENERATOR_MODEL_NAME, COMPLAINT_ROUTER_MODEL_NAME,
    PROJECTS_CONFIG
)


logger = logging.getLogger(__name__)


@rate_limited
async def start_complaint_generation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Начинает диалог составления жалоб/исков, запрашивая тип документа.
    """
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass  # Игнорируем ошибки "Query is too old"

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"complaint_type_{key}")]
        for key, name in COMPLAINT_TYPES.items()
    ]
    keyboard.append([InlineKeyboardButton("◀️ Отмена", callback_data="main_menu")])

    text = (
        "📝 <b>Составление юридического документа</b>\n\n"
        "Выберите тип документа, который нужно составить:"
    )
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
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    return CHOOSING_COMPLAINT_TYPE


async def choose_complaint_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Сохраняет выбранный тип документа и запрашивает проект.
    """
    query = update.callback_query
    await query.answer()

    complaint_type = query.data.split("complaint_type_", 1)[1]
    context.user_data['complaint_type'] = complaint_type
    context.user_data['complaint_type_name'] = COMPLAINT_TYPES[complaint_type]

    enabled_projects = await get_enabled_projects()

    # Если только один проект - пропускаем выбор и сразу показываем серверы
    if len(enabled_projects) == 1:
        context.user_data['complaint_project_id'] = enabled_projects[0]
        return await _show_complaint_servers(update, context, enabled_projects[0])

    # Удаляем старое сообщение
    try:
        await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта с картинкой
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="complaint_project_",
        text=f"Вы выбрали: <b>{COMPLAINT_TYPES[complaint_type]}</b>\n\n🌐 <b>Для какого проекта составляем документ?</b>",
        back_callback="start_complaint_gen"
    )
    return CHOOSING_COMPLAINT_SERVER


async def _show_complaint_servers(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: str) -> int:
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
    complaint_type = context.user_data.get('complaint_type')

    keyboard = [
        [InlineKeyboardButton(
            config.SERVERS_CONFIG[server_id]['display_name'],
            callback_data=f"complaint_server_{server_id}"
        )]
        for server_id in enabled_servers
    ]

    # Кнопка "Назад" зависит от количества проектов
    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) > 1:
        keyboard.append([InlineKeyboardButton("◀️ К выбору проекта", callback_data=f"complaint_type_{complaint_type}")])
    else:
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="start_complaint_gen")])

    text = (
        f"Вы выбрали: <b>{COMPLAINT_TYPES[complaint_type]}</b>\n"
        f"🎮 <b>{project_name}</b>\n\n"
        "Для какого сервера составляем документ?"
    )
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
    return CHOOSING_COMPLAINT_SERVER


async def select_complaint_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный проект и показывает серверы."""
    query = update.callback_query
    await query.answer()

    project_id = query.data.split("complaint_project_", 1)[1]
    context.user_data['complaint_project_id'] = project_id

    return await _show_complaint_servers(update, context, project_id)


async def ask_forum_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запрашивает у пользователя ссылку на форумную тему с формой документа.
    """
    query = update.callback_query
    await query.answer()

    server_id = query.data.split("complaint_server_", 1)[1]
    context.user_data['complaint_server_id'] = server_id

    keyboard = [[InlineKeyboardButton("◀️ Назад", callback_data=f"complaint_type_{context.user_data['complaint_type']}")]]

    await query.edit_message_text(
        f"📋 <b>Шаг 1 из 2: Форма документа</b>\n\n"
        f"Отправьте ссылку на тему форума, где указана форма для заполнения "
        f"<b>{context.user_data['complaint_type_name'].lower()}</b>.\n\n"
        f"<i>Примеры:\nhttps://forum.majestic-rp.ru/threads/12345/\nhttps://forum.gta5rp.com/threads/12345/"
        f"\nhttps://forum.rmrp.ru/threads/12345/\nhttps://forum.russia.online/threads/12345/</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )
    return AWAITING_FORUM_URL


async def ask_situation_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Сохраняет ссылку на форум и запрашивает описание ситуации.
    """
    forum_url = update.message.text.strip()

    # Простая валидация URL
    if not re.match(r'https?://.+', forum_url):
        await update.message.reply_text(
            "❌ Пожалуйста, отправьте корректную ссылку на форум (начинается с http:// или https://)"
        )
        return AWAITING_FORUM_URL

    context.user_data['complaint_forum_url'] = forum_url

    await update.message.reply_text(
        f"✅ Ссылка сохранена.\n\n"
        f"📝 <b>Шаг 2 из 2: Описание ситуации</b>\n\n"
        f"Опишите вашу ситуацию своими словами. Не беспокойтесь о юридических терминах - "
        f"просто расскажите, что произошло:\n\n"
        f"• Кто участвовал?\n"
        f"• Что случилось?\n"
        f"• Когда это произошло?\n"
        f"• Какие у вас есть доказательства?\n\n"
        f"<i>Бот автоматически переведет ваше описание на юридический язык и составит документ.</i>",
        parse_mode='HTML'
    )
    return AWAITING_SITUATION_DESCRIPTION


async def generate_complaint_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Проверяет лимиты и запускает фоновую задачу генерации документа.
    """
    situation_description = update.message.text.strip()

    if len(situation_description) < 50:
        await update.message.reply_text(
            "❌ Описание слишком короткое. Пожалуйста, опишите ситуацию подробнее (минимум 50 символов)."
        )
        return AWAITING_SITUATION_DESCRIPTION

    # Проверка лимитов
    can_ask, _ = await check_and_update_usage(update.effective_chat.id, 'complaint')
    if not can_ask:
        await update.message.reply_text(
            f"❌ Вы достигли дневного лимита на составление документов.\n"
            f"Лимит обновится завтра."
        )
        await _display_main_menu(update, context, is_new_message=True)
        return ConversationHandler.END

    context.user_data['complaint_situation'] = situation_description

    processing_message = await update.message.reply_text(
        "⏳ <b>Генерация документа...</b>\n\n"
        "Это может занять 30-60 секунд:\n"
        "• Парсинг формы с форума\n"
        "• Анализ вашей ситуации\n"
        "• Подбор релевантных законов\n"
        "• Составление документа\n\n"
        "Пожалуйста, подожди��е...",
        parse_mode='HTML'
    )

    # Запускаем тяжелую задачу в фоне
    asyncio.create_task(
        process_and_send_complaint(
            context, update.effective_chat.id, processing_message.message_id
        )
    )
    return ConversationHandler.END


async def get_relevant_laws_for_complaint(
    server_id: str,
    complaint_type: str,
    situation_description: str,
    form_text: str,
    genai_client
) -> list[types.Part]:
    """
    Использует AI-роутер для выбора релевантных законов при составлении жалоб/исков.

    Args:
        server_id: ID сервера
        complaint_type: Тип документа ('complaint' или 'lawsuit')
        situation_description: Описание ситуации от пользователя
        form_text: Краткое описание формы с форума
        genai_client: Клиент Google Generative AI

    Returns:
        Список Part объектов с URI релевантных законов
    """
    start_time = asyncio.get_running_loop().time()
    doc_name = "жалобы" if complaint_type == "complaint" else "иска"
    logging.info(f"[COMPLAINT_ROUTER] Запуск фильтра законов для составления {doc_name}...")

    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )

        if not law_records:
            logging.warning(f"[COMPLAINT_ROUTER] В БД отсутствуют документы для сервера {server_id}.")
            return []

        # Создаём карту законов
        law_map_by_index = {
            i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']}
            for i, r in enumerate(law_records)
        }
        law_titles_str = "\n".join([f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)])

        # Формируем промпт для роутера
        router_prompt = get_complaint_router_prompt(
            complaint_type=complaint_type,
            situation_description=situation_description,
            form_text=form_text[:500],  # Ограничиваем длину формы для промпта
            law_titles_str=law_titles_str
        )

        # Определяем функцию для выполнения с rate limiting
        async def send_router_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на переданный клиент
                client = genai_client
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=COMPLAINT_ROUTER_MODEL_NAME,
                contents=router_prompt
            )

        try:
            # Используем rate limiter с низким приоритетом (фоновая задача)
            response = await execute_vertex_request(
                func=send_router_request,
                model_name=COMPLAINT_ROUTER_MODEL_NAME,
                priority=RequestPriority.LOW,  # Низкий приоритет для фоновой фильтрации
                max_retries=2
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

            logger.warning(f"[COMPLAINT_ROUTER] Ошибка при фильтрации законов для {doc_name}: {e}")
            # Возвращаем пустой список, чтобы использовать все законы
            return []

        # Парсим ответ (номера законов)
        selected_indices = {int(line.strip()) for line in (response.text or "").split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logging.info(
            f"[COMPLAINT_ROUTER] Успех. Фильтр выбрал {len(relevant_laws)} законов для {doc_name} за {duration:.2f} мс. "
            f"Выбранные законы: {[law['title'] for law in relevant_laws]}"
        )

        # Возвращаем Part объекты
        return [types.Part.from_uri(file_uri=law['uri'], mime_type="text/plain") for law in relevant_laws]

    except Exception as e:
        logging.error(f"[COMPLAINT_ROUTER] КРИТИЧЕСКАЯ ОШИБКА в работе фильтра для {doc_name}.", exc_info=e)
        return []


async def check_lawsuit_legality(server_id: str, situation_description: str, form_text: str, genai_client) -> str:
    """
    Проверяет законность искового заявления перед его генерацией.

    Args:
        server_id: ID сервера для загрузки законов
        situation_description: Описание ситуации от пользователя
        form_text: Текст формы с форума
        genai_client: Клиент Google Generative AI

    Returns:
        Предупреждение о проблемах с законностью или пустая строка если проверка прошла
    """
    try:
        # Загружаем все законы из БД
        law_records = await db_manager.fetch(
            "SELECT gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )

        if not law_records:
            return ""

        law_files = [types.Part.from_uri(file_uri=r['gemini_file_uri'], mime_type="text/plain") for r in law_records]

        legality_check_prompt = f"""
Ты — строгий юрист-эксперт на RP-проекте Majestic RP, который проверяет законность исковых заявлений.

Твоя задача — КРИТИЧЕСКИ проанализировать описанную ситуацию и определить, можно ли по закону подавать исковое заявление в суд.

ВАЖНО: Используй ТОЛЬКО предоставленные законы штата San Andreas. Не выдумывай нормы.

**ОПИСАНИЕ СИТУАЦИИ ОТ ПОЛЬЗОВАТЕЛЯ:**
{situation_description}

**ФОРМА ИСКОВОГО ЗАЯВЛЕНИЯ:**
{form_text}

**КРИТЕРИИ ПРОВЕРКИ (СТРОГО ПО ПОРЯДКУ):**

1. **УГОЛОВНОЕ vs ГРАЖДАНСКОЕ:**
   - Ищи в описании упоминания: убийство, смерть, нападение, избиение, кража, грабеж, разбой, мошенничество, вымогательство, похищение, угон, причинение вреда здоровью, применение оружия, угрозы убийством
   - Если найдено ХОТЯ БЫ ОДНО такое упоминание → это УГОЛОВНОЕ преступление, иск подавать НЕЛЬЗЯ
   - ОБЯЗАТЕЛЬНО укажи статью УК, по которой это преступление

2. **ГРАЖДАНСКО-ПРАВОВЫЕ СПОРЫ (разрешены):**
   - Споры о договорах, компенсациях, имуществе (без преступлений)
   - Трудовые споры (увольнение, зарплата)
   - Споры о репутации (клевета без преступных действий)

3. **ПРАВОВЫЕ ОСНОВАНИЯ:**
   - Есть ли в ГК/ГПК основания для такого иска?

**ОБЯЗАТЕЛЬНЫЙ ФОРМАТ ОТВЕТА:**

Если в описании упоминаются ЛЮБЫЕ преступления (смотри список выше):
"ПРОБЛЕМА: Описанная ситуация содержит признаки уголовного преступления - [название преступления] (УК San Andreas, ст. [номер статьи]). Такие дела НЕ рассматриваются в порядке гражданского судопроизводства. Вам необходимо подать ЖАЛОБУ В ПРОКУРАТУРУ, а не исковое заявление в суд."

Если преступлений нет, но есть другие проблемы:
"ПРОБЛЕМА: [описание проблемы со ссылкой на закон]"

Если проблем нет:
"ОК"

ВНИМАНИЕ: Будь МАКСИМАЛЬНО СТРОГИМ. Даже намек на преступление = ПРОБЛЕМА!

ТВОЙ ОТВЕТ:
"""

        content_parts = law_files + [types.Part(text=legality_check_prompt)]

        # Определяем функцию для выполнения с rate limiting
        async def send_legality_check():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на переданный клиент
                client = genai_client
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=COMPLAINT_GENERATOR_MODEL_NAME,
                contents=content_parts,
                config=types.GenerateContentConfig(
                    temperature=0.0,  # Максимальная строгость
                    top_p=0.7,
                    top_k=20,
                    max_output_tokens=800
                )
            )

        try:
            # Используем rate limiter с нормальным приоритетом
            response = await execute_vertex_request(
                func=send_legality_check,
                model_name=COMPLAINT_GENERATOR_MODEL_NAME,
                priority=RequestPriority.NORMAL,  # Обычный приоритет для проверки
                max_retries=2
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

            logger.warning(f"❌ [ПРОВЕРКА ЗАКОННОСТИ] Ошибка при проверке законности: {e}")
            return "⚠️ Не удалось автоматически проверить законность иска из-за высокой нагрузки на AI-сервис. Внимательно проверьте, относится ли ваша ситуация к гражданским спорам, а не к уголовным преступлениям."

        result = (response.text or "").strip()
        if not result:
            # Gemini вернул пустой ответ (safety-block/пустой кандидат) — не падаем на .strip(),
            # отдаём мягкое предупреждение (как при перегрузке AI).
            logger.warning("⚠️ [ПРОВЕРКА ЗАКОННОСТИ] AI вернул пустой ответ — проверку пропускаю")
            return ("⚠️ Не удалось автоматически проверить законность иска (пустой ответ AI). "
                    "Внимательно проверьте, относится ли ваша ситуация к гражданским спорам, а не к уголовным преступлениям.")
        logger.info(f"🔍 [ПРОВЕРКА ЗАКОННОСТИ] Сырой ответ AI: {result}")

        # Проверяем, есть ли проблемы
        if "ПРОБЛЕМА" in result or "проблема" in result.lower():
            # Извлекаем текст после "ПРОБЛЕМА:"
            warning_text = result
            if ":" in result:
                warning_text = result.split(":", 1)[-1].strip()

            warning_text = warning_text.replace("ПРОБЛЕМА", "").replace("проблема", "").strip()
            if warning_text.startswith(":"):
                warning_text = warning_text[1:].strip()

            logger.warning(f"⚠️ [ПРОВЕРКА ЗАКОННОСТИ] ОБНАРУЖЕНА ПРОБЛЕМА: {warning_text}")
            return warning_text if warning_text else "Обнаружены потенциальные проблемы с законностью данного иска. Рекомендуется консультация с юристом."

        # Если ответ содержит признаки проблем (уголовное дело, нельзя и т.д.)
        lower_result = result.lower()
        problem_keywords = ["нельзя", "невозможно", "уголовн", "прокуратур", "не может быть", "запрещ", "преступлени"]

        for keyword in problem_keywords:
            if keyword in lower_result:
                logger.warning(f"⚠️ [ПРОВЕРКА ЗАКОННОСТИ] Найдено ключевое слово '{keyword}': {result}")
                return result

        # Проверяем, что ответ действительно "ОК"
        if result.upper().strip() == "ОК" or result.upper().strip() == "OK":
            logger.info("✅ [ПРОВЕРКА ЗАКОННОСТИ] Проблем не обнаружено")
            return ""

        # Если ответ непонятный - перестраховываемся
        logger.warning(f"⚠️ [ПРОВЕРКА ЗАКОННОСТИ] Непонятный ответ AI: {result}. Добавляем предупреждение.")
        return result

    except Exception as e:
        logger.error(f"❌ [ПРОВЕРКА ЗАКОННОСТИ] Ошибка при проверке: {e}", exc_info=True)
        # В случае ошибки возвращаем общее предупреждение
        return "⚠️ Не удалось автоматически проверить законность иска. Внимательно проверьте, относится ли ваша ситуация к гражданским спорам, а не к уголовным преступлениям."


async def process_and_send_complaint(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    processing_message_id: int,
    model_override: str | None = None
):
    """
    Асинхронная задача, которая:
    1. Парсит форму с форума
    2. Подбирает релевантные законы
    3. Генерирует документ через AI
    4. Конвертирует в BB-код
    5. Отправляет результат пользователю

    model_override — если задано, генерация документа идёт этой моделью вместо
    COMPLAINT_GENERATOR_MODEL_NAME (кнопка «Отправить более слабой модели» под
    сообщением о перегрузе; основная модель в конфиге не меняется).
    """
    try:
        # Извлекаем данные из context.user_data
        complaint_type = context.user_data['complaint_type']
        complaint_type_name = context.user_data['complaint_type_name']
        server_id = context.user_data['complaint_server_id']
        forum_url = context.user_data['complaint_forum_url']
        situation_description = context.user_data['complaint_situation']

        # Определяем количество шагов заранее
        total_steps = 5 if complaint_type == "lawsuit" else 4

        # ШАГ 1: Парсинг формы с форума
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=processing_message_id,
            text=f"⏳ <b>Шаг 1/{total_steps}:</b> Парсинг формы с форума...",
            parse_mode='HTML'
        )

        form_text = await parse_forum_form(forum_url)

        if not form_text:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=processing_message_id,
                text="❌ <b>Ошибка:</b> Не удалось извлечь форму с указанной страницы форума.\n\n"
                     "Пожалуйста, проверьте ссылку и попробуйте снова.",
                parse_mode='HTML'
            )
            return


        # ШАГ 2: Интеллектуальная фильтрация законов
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=processing_message_id,
            text=f"⏳ <b>Шаг 2/{total_steps}:</b> Интеллектуальная фильтрация релевантных законов...",
            parse_mode='HTML'
        )

        # Используем AI-роутер для выбора релевантных законов
        genai_client = context.bot_data.get('genai_client')
        law_files = await get_relevant_laws_for_complaint(
            server_id=server_id,
            complaint_type=complaint_type,
            situation_description=situation_description,
            form_text=form_text,
            genai_client=genai_client
        )

        if not law_files:
            # Fallback: если роутер не выбрал законы, загружаем все
            logging.warning(f"[COMPLAINT] Роутер не выбрал законы, загружаем все для {server_id}")
            law_records = await db_manager.fetch(
                "SELECT gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
                server_id
            )

            if not law_records:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=processing_message_id,
                    text="❌ <b>Ошибка:</b> База знаний законов для этого сервера недоступна.",
                    parse_mode='HTML'
                )
                return

            law_files = [types.Part.from_uri(file_uri=r['gemini_file_uri'], mime_type="text/plain") for r in law_records]

        # ШАГ 3: Проверка законности иска (только для исковых заявлений)
        legality_warning = ""
        if complaint_type == "lawsuit":
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=processing_message_id,
                text="⏳ <b>Шаг 3/5:</b> Проверка законности иска...",
                parse_mode='HTML'
            )

            logger.info(f"[User {chat_id}] Запуск проверки законности иска...")

            # Предварительная проверка на уровне Python
            crime_keywords = [
                'убийство', 'убил', 'убили', 'смерть', 'умер',
                'нападение', 'напал', 'напали', 'избиение', 'избил', 'избили',
                'кража', 'украл', 'украли', 'грабеж', 'ограбил', 'разбой',
                'мошенничество', 'обманул', 'мошенник',
                'вымогательство', 'вымогал',
                'похищение', 'похитил', 'похитили',
                'угон', 'угнал',
                'вред здоровью', 'ранение', 'ранил', 'ранили',
                'оружие', 'выстрел', 'стрелял', 'застрелил',
                'угроза убийством', 'угрожал'
            ]

            situation_lower = situation_description.lower()
            found_crimes = [kw for kw in crime_keywords if kw in situation_lower]

            if found_crimes:
                logger.warning(f"⚠️ [ПРЕДПРОВЕРКА] Обнаружены ключевые слова преступлений: {found_crimes}")
                legality_warning = (
                    "⚠️ АВТОМАТИЧЕСКАЯ ПРЕДПРОВЕРКА обнаружила упоминание возможных уголовных преступлений в вашем описании. "
                    "Иски в суд подаются только по ГРАЖДАНСКИМ спорам. Если в вашей ситуации было совершено преступление "
                    "(убийство, нападение, кража и т.д.), вам необходимо подать ЖАЛОБУ В ПРОКУРАТУРУ, а не исковое заявление.\n\n"
                )

            # AI проверка для более глубокого анализа
            legality_check_result = await check_lawsuit_legality(
                server_id, situation_description, form_text, genai_client
            )

            logger.info(f"[User {chat_id}] Результат AI-проверки законности: {legality_check_result[:200]}...")

            if legality_check_result:
                # Если AI тоже нашла проблему, добавляем её к предупреждению
                if legality_warning:
                    legality_warning += f"\n\n📋 ДЕТАЛЬНЫЙ АНАЛИЗ AI:\n{legality_check_result}\n\n"
                else:
                    legality_warning = f"⚠️ <b>ВАЖНОЕ ПРЕДУПРЕЖДЕНИЕ О ЗАКОННОСТИ:</b>\n\n{legality_check_result}\n\n"

                legality_warning += "="*50 + "\n\n"
                logger.warning(f"[User {chat_id}] ОБНАРУЖЕНА ПРОБЛЕМА С ЗАКОННОСТЬЮ ИСКА")
            elif legality_warning:
                # Только клиентская проверка нашла проблему, AI не подтвердила
                legality_warning += "="*50 + "\n\n"

        # ШАГ 4: Генерация документа через AI
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=processing_message_id,
            text=f"⏳ <b>Шаг {'4' if complaint_type == 'lawsuit' else '3'}/{'5' if complaint_type == 'lawsuit' else '4'}:</b> AI составляет документ...",
            parse_mode='HTML'
        )

        # Создаем промпт
        system_prompt = get_complaint_generation_prompt(
            complaint_type=complaint_type,
            form_text=form_text,
            situation_description=situation_description
        )

        # Формируем контент: файлы законов + инструкция
        content_parts = law_files + [types.Part(text="Начинай составление документа на основе предоставленной формы и описания ситуации.")]

        gen_model_name = model_override or COMPLAINT_GENERATOR_MODEL_NAME

        # Определяем функцию для выполнения с rate limiting
        async def send_generation_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=gen_model_name,
                contents=content_parts,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.3,
                    top_p=0.95,
                    max_output_tokens=8192
                )
            )

        logger.info(f"[User {chat_id}] Запуск генерации документа...")

        try:
            # Используем rate limiter с ВЫСОКИМ приоритетом (важная задача для пользователя)
            response = await execute_vertex_request(
                func=send_generation_request,
                model_name=gen_model_name,
                priority=RequestPriority.HIGH,  # Высокий приоритет для генерации документов
                user_id=chat_id,
                max_retries=3
            )
            # Отмечаем успех в текущем регионе
            await mark_current_region_success()
            generated_html = (response.text or "").strip()
            if not generated_html:
                # Gemini вернул пустой ответ (частая причина — safety-block/пустой кандидат):
                # не падаем в AttributeError, показываем понятную ошибку с кнопкой повтора.
                await mark_current_region_error()
                logger.error(f"[COMPLAINT] Пустой ответ AI при генерации документа для {chat_id}")
                snapshot = {
                    'complaint_type': context.user_data.get('complaint_type'),
                    'complaint_type_name': context.user_data.get('complaint_type_name'),
                    'complaint_server_id': context.user_data.get('complaint_server_id'),
                    'complaint_forum_url': context.user_data.get('complaint_forum_url'),
                    'complaint_situation': context.user_data.get('complaint_situation'),
                }
                token = register_retry(context, 'complaint', {'snapshot': snapshot},
                                       weak_allowed=(gen_model_name != config.WEAK_FALLBACK_MODEL_NAME))
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=processing_message_id,
                    text=("⚠️ AI вернул пустой ответ (возможно, сработал фильтр безопасности "
                          "или перегрузка).\n\nНажмите «🔄 Повторить»."),
                    parse_mode='HTML',
                    reply_markup=retry_keyboard(token, weak_allowed=(gen_model_name != config.WEAK_FALLBACK_MODEL_NAME)),
                )
                return
            logger.info(f"[User {chat_id}] Генерация документа успешно завершена")
        except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
            # Отмечаем ошибку в текущем регионе
            await mark_current_region_error()

            # Проверяем, что это действительно 429 ошибка
            if isinstance(e, genai_errors.ClientError):
                error_str = str(e)
                if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                    raise  # Пробрасываем не-429 ошибки дальше

            # Все попытки исчерпаны или таймаут
            logger.error(f"[COMPLAINT] Не удалось сгенерировать документ для пользователя {chat_id}: {e}")
            is_weak = (gen_model_name == config.WEAK_FALLBACK_MODEL_NAME)
            # Снимок данных заявки — чтобы повтор не сломался, даже если user_data
            # перезапишут новым диалогом до нажатия кнопки.
            snapshot = {
                'complaint_type': context.user_data.get('complaint_type'),
                'complaint_type_name': context.user_data.get('complaint_type_name'),
                'complaint_server_id': context.user_data.get('complaint_server_id'),
                'complaint_forum_url': context.user_data.get('complaint_forum_url'),
                'complaint_situation': context.user_data.get('complaint_situation'),
            }
            token = register_retry(context, 'complaint', {'snapshot': snapshot},
                                   weak_allowed=not is_weak)
            overload_text = (
                "⏳ К сожалению, AI-сервис временно перегружен.\n\n"
                "Система управления нагрузкой не смогла обработать ваш запрос.\n\n"
                "Нажмите «🔄 Повторить» — возможно, лимит был временным."
            )
            if not is_weak:
                overload_text += "\nИли «⚡ Отправить более слабой модели» — она обычно свободнее."
            overload_text += "\n\nПриносим извинения за неудобства."
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=processing_message_id,
                text=overload_text,
                parse_mode='HTML',
                reply_markup=retry_keyboard(token, weak_allowed=not is_weak),
            )
            return

        # Добавляем предупреждение о законности в начало документа, если оно есть
        if legality_warning:
            generated_html = legality_warning + generated_html

        # Санитизация HTML - удаление неподдерживаемых Telegram тегов
        generated_html = sanitize_telegram_html(generated_html)

        # ШАГ 5 (или 4): Конвертация в BB-код
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=processing_message_id,
            text=f"⏳ <b>Шаг {'5' if complaint_type == 'lawsuit' else '4'}/{'5' if complaint_type == 'lawsuit' else '4'}:</b> Конвертация в формат форума...",
            parse_mode='HTML'
        )

        generated_bbcode = html_to_bbcode(generated_html)

        # Сохранение BB-кода в файл
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"complaint_{complaint_type}_{timestamp}.txt"
        file_path = save_bbcode_to_file(generated_bbcode, filename)

        # Сохранение в базу данных
        await db_manager.execute(
            """
            INSERT INTO complaints_history 
            (user_id, server_id, complaint_type, forum_url, situation_description, generated_text, generated_bb_code)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            chat_id, server_id, complaint_type, forum_url, situation_description,
            generated_html, generated_bbcode
        )

        # Удаляем сообщение о процессе
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=processing_message_id)
        except Exception:
            pass

        # Отправка результата (разбиваем на части, если текст слишком длинный)
        max_length = 4000

        # Функция для безопасной отправки с HTML
        async def safe_send_html(text: str, is_first: bool = True):
            """Пытается отправить с HTML, при ошибке - без форматирования."""
            try:
                if is_first:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ <b>Документ успешно составлен!</b>\n\n{text}",
                        parse_mode='HTML'
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode='HTML'
                    )
            except Exception as e:
                # Если ошибка парсинга HTML - отправляем без форматирования
                logger.warning(f"[User {chat_id}] Ошибка парсинга HTML: {e}. Отправка без форматирования...")

                # Удаляем все HTML теги
                import re
                clean_text = re.sub(r'<[^>]+>', '', text)

                if is_first:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ Документ успешно составлен!\n\n{clean_text}"
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=clean_text
                    )

        if len(generated_html) > max_length:
            # Разбиваем на части
            parts = [generated_html[i:i+max_length] for i in range(0, len(generated_html), max_length)]
            for i, part in enumerate(parts, 1):
                await safe_send_html(part, is_first=(i == 1))
        else:
            await safe_send_html(generated_html, is_first=True)

        # Отправка файла с BB-кодом
        with open(file_path, 'rb') as f:
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                filename=filename,
                caption="📄 <b>BB-код для публикации на форуме</b>\n\n"
                        "Скопируйте содержимое этого файла и вставьте в тему на форуме.",
                parse_mode='HTML'
            )

        # Удаляем временный файл
        try:
            os.remove(file_path)
        except Exception as e:
            logger.warning(f"Не удалось удалить временный файл {file_path}: {e}")

        # Отправка главного меню
        keyboard = [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
        await context.bot.send_message(
            chat_id=chat_id,
            text="Документ сохранен в вашу историю. Вы можете создать новый документ в любое время.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        logger.info(f"Документ {complaint_type} успешно сгенерирован для пользователя {chat_id}")


    except Exception as e:
        logger.error(f"❌ [ОШИБКА] Ошибка при генерации документа для {chat_id}: {e}", exc_info=True)

        # Уведомление суперадмину
        from utils import notify_super_admin
        error_details = (
            f"❌ Ошибка в complaint_flow\n\n"
            f"👤 Пользователь: {chat_id}\n"
            f"📋 Тип документа: {complaint_type}\n"
            f"🖥 Сервер: {server_id}\n"
            f"🔗 URL: {forum_url[:100]}\n\n"
            f"⚠️ Ошибка: {type(e).__name__}\n"
            f"📝 Сообщение: {str(e)[:500]}"
        )
        await notify_super_admin(context, e, error_details)

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=processing_message_id,
                text="❌ <b>Произошла ошибка при генерации документа.</b>\n\n"
                     "Администратор уведомлен о проблеме. Пожалуйста, попробуйте еще раз позже.",
                parse_mode='HTML'
            )
        except Exception:
            await send_error_message(context, chat_id)


async def replay_complaint_retry(context: ContextTypes.DEFAULT_TYPE, chat_id: int, params: dict, *, weak: bool):
    """Повтор генерации иска/жалобы по кнопке под сообщением о перегрузе.

    weak=False — повторить на основной модели; weak=True — разово на запасной.
    Лимит не списывается повторно (он списывается при первичной отправке описания).
    Данные заявки восстанавливаются из снимка в user_data на случай перезаписи.
    """
    snapshot = (params or {}).get('snapshot') or {}
    # Восстанавливаем поля заявки (process_and_send_complaint читает их из user_data).
    for key, value in snapshot.items():
        if value is not None:
            context.user_data[key] = value

    required = ('complaint_type', 'complaint_server_id', 'complaint_forum_url', 'complaint_situation')
    if any(not context.user_data.get(k) for k in required):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не удалось повторить — данные заявки устарели. Составьте документ заново."
        )
        return

    model_override = config.WEAK_FALLBACK_MODEL_NAME if weak else None
    processing_message = await context.bot.send_message(
        chat_id=chat_id,
        text=("⏳ <b>Повторная генерация (запасная модель)...</b>" if weak
              else "⏳ <b>Повторная генерация документа...</b>"),
        parse_mode='HTML'
    )
    asyncio.create_task(process_and_send_complaint(
        context, chat_id, processing_message.message_id, model_override=model_override
    ))


async def parse_forum_form(forum_url: str) -> str:
    """
    Парсит форумную тему и извлекает текст формы из ВСЕХ сообщений темы.

    Форма иска может находиться не только в первом сообщении, но и во втором,
    третьем и т.д. Поэтому собираем все сообщения темы.

    Args:
        forum_url: URL темы форума

    Returns:
        Объединенный текст всех сообщений или пустая строка при ошибке
    """
    try:
        # Используем существующую функцию парсинга из court_case_parser
        thread_data = await parse_court_case_from_url(forum_url)

        if not thread_data or not thread_data.get('messages'):
            logger.warning(f"Не удалось извлечь сообщения из {forum_url}")
            return ""

        # Собираем текст из ВСЕХ сообщений темы
        all_messages_text = []

        for idx, message in enumerate(thread_data['messages'], start=1):
            text_content = message.get('text_content', '')
            if text_content and len(text_content.strip()) > 0:
                # Добавляем разделитель между сообщениями для ясности
                separator = f"\n\n{'='*60}\n[Сообщение #{idx} от {message.get('author', 'Неизвестно')}]\n{'='*60}\n\n"
                all_messages_text.append(separator + text_content)

        if not all_messages_text:
            logger.warning(f"В теме {forum_url} не найдено сообщений с текстом")
            return ""

        # Объединяем все сообщения
        form_text = "\n\n".join(all_messages_text)

        logger.info(f"Форма успешно извлечена из {forum_url}, всего сообщений: {len(all_messages_text)}, общая длина: {len(form_text)} символов")
        return form_text

    except Exception as e:
        logger.error(f"Ошибка при парсинге формы с {forum_url}: {e}", exc_info=True)
        return ""


# ConversationHandler для диалога составления жалоб/исков
complaint_conv_handler = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(start_complaint_generation, pattern='^start_complaint_gen$')
    ],
    states={
        CHOOSING_COMPLAINT_TYPE: [
            CallbackQueryHandler(choose_complaint_server, pattern='^complaint_type_')
        ],
        CHOOSING_COMPLAINT_SERVER: [
            CallbackQueryHandler(select_complaint_project, pattern='^complaint_project_'),
            CallbackQueryHandler(ask_forum_url, pattern='^complaint_server_')
        ],
        AWAITING_FORUM_URL: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_situation_description)
        ],
        AWAITING_SITUATION_DESCRIPTION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, generate_complaint_document)
        ],
    },
    fallbacks=[
        CommandHandler("start", start),
        CallbackQueryHandler(main_menu, pattern='^main_menu$'),
        CallbackQueryHandler(start_complaint_generation, pattern='^start_complaint_gen$'),
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
    ],
    name="complaint_conversation",
    persistent=False
)

