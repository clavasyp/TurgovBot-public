# user_handlers/court_flow.py
"""
Модуль для управления функционалом "Судебного Помощника".

Содержит:
- ConversationHandler для работы с судебными делами
- Функции парсинга тем форума с OCR изображений
- Логику ведения диалога с судьей
- Инструменты анализа дела (процессуальный аудит, контроль сроков и т.д.)
- Функционал просмотра активных дел
"""

import logging
from typing import Optional, Dict

from google import genai
from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, ConversationHandler, CallbackQueryHandler, MessageHandler, CommandHandler, filters

# Локальные импорты
from database.manager import db_manager
from utils import get_enabled_servers, sanitize_telegram_html, retry_with_exponential_backoff
from prompts import get_court_system_prompt, get_court_tool_prompt
from court_case_parser import parse_court_case_from_url, is_supported_forum_url, SUPPORTED_FORUMS
from ocr_processor import process_multiple_images
from .core import main_menu, start, send_error_message, unknown_text_fallback
from .qa_flow import _split_long_message
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error

# Константы состояний импортируются из config
from config import (
    AWAITING_COURT_CASE_URL, COURT_DIALOGUE, VIEWING_COURT_CASES,
    COURT_ASSISTANT_MODEL_NAME, COURT_OCR_MODEL_NAME
)

# --- Константы для судебного помощника ---

COURT_TOOLS = [
    InlineKeyboardButton("📋 Процессуальный аудит", callback_data="tool_court_procedural_audit"),
    InlineKeyboardButton("⏰ Контроль сроков", callback_data="tool_court_timeline_check"),
    InlineKeyboardButton("📊 Анализ доказательств", callback_data="tool_court_evidence_analysis"),
    InlineKeyboardButton("⚖️ Правовая квалификация", callback_data="tool_court_legal_qualification"),
    InlineKeyboardButton("📝 Подготовка постановления", callback_data="tool_court_draft_ruling"),
    InlineKeyboardButton("🔍 Поиск аналогий", callback_data="tool_court_search_precedents"),
]


# --- Вспомогательные функции ---

async def _save_court_case_to_db(user_id: int, server_name: str, case_data: Dict) -> Optional[int]:
    """
    Сохраняет данные судебного дела в базу данных.

    Returns:
        ID созданного дела или None при ошибке
    """
    try:
        # Создаем запись в court_cases
        case_id = await db_manager.fetchval(
            """
            INSERT INTO court_cases 
            (user_id, server_name, thread_url, thread_id, case_title, plaintiff, defendant, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'active')
            RETURNING id
            """,
            user_id,
            server_name,
            case_data['thread_url'],
            case_data.get('thread_id'),
            case_data['thread_title'],
            case_data.get('plaintiff', 'Неизвестно'),
            case_data.get('defendant', 'Неизвестно')
        )

        logging.info(f"[CourtFlow] Создано дело #{case_id} для пользователя {user_id}")

        # Сохраняем сообщения
        for msg in case_data['messages']:
            message_id = await db_manager.fetchval(
                """
                INSERT INTO court_messages
                (court_case_id, message_order, author, author_role, message_timestamp, 
                 text_content, has_images)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                case_id,
                msg['message_order'],
                msg['author'],
                msg['author_role'],
                msg.get('message_timestamp'),
                msg['text_content'],
                msg['has_images']
            )

            # Сохраняем изображения (если есть)
            if msg.get('images'):
                for img in msg['images']:
                    await db_manager.execute(
                        """
                        INSERT INTO court_images
                        (court_message_id, image_url, bbcode_tag, ocr_text, document_type, processed_at)
                        VALUES ($1, $2, $3, $4, $5, NOW())
                        """,
                        message_id,
                        img['url'],
                        img.get('bbcode'),
                        img.get('ocr_text'),
                        img.get('document_type')
                    )

        logging.info(f"[CourtFlow] Сохранено {len(case_data['messages'])} сообщений для дела #{case_id}")
        return case_id

    except Exception as e:
        logging.error(f"[CourtFlow] Ошибка сохранения дела в БД: {e}", exc_info=True)
        return None


async def _process_images_with_ocr(messages: list, update: Update) -> None:
    """
    Обрабатывает все изображения из сообщений с помощью OCR.
    Показывает прогресс пользователю.
    """
    # Собираем все изображения
    all_images = []
    for msg in messages:
        if msg.get('images'):
            all_images.extend(msg['images'])

    if not all_images:
        return

    total = len(all_images)
    logging.info(f"[CourtFlow] Начало OCR для {total} изображений")

    # Показываем прогресс
    if update.callback_query and update.callback_query.message:
        progress_msg = await update.callback_query.message.reply_text(
            f"📸 Найдено изображений: {total}\n⏳ Начинаю распознавание текста..."
        )
    else:
        # Fallback если нет callback_query
        return

    try:
        # Обрабатываем изображения параллельно
        ocr_results = await process_multiple_images(context, all_images, COURT_OCR_MODEL_NAME)

        # Обновляем данные изображений с результатами OCR
        for idx, (img, result) in enumerate(zip(all_images, ocr_results)):
            if result:
                img['ocr_text'] = result.get('ocr_text', '')
                img['document_type'] = result.get('document_type', 'неизвестно')
                logging.info(
                    f"[CourtFlow] OCR #{idx+1}/{total}: тип={img['document_type']}, "
                    f"текст={len(img['ocr_text'])} символов"
                )

        # Обновляем сообщение о прогрессе
        successful = sum(1 for r in ocr_results if r is not None)
        await progress_msg.edit_text(
            f"✅ Распознавание завершено!\n"
            f"📊 Успешно обработано: {successful}/{total} изображений"
        )

    except Exception as e:
        logging.error(f"[CourtFlow] Ошибка при OCR: {e}", exc_info=True)
        await progress_msg.edit_text(
            f"⚠️ Произошла ошибка при распознавании изображений.\n"
            f"Продолжаю работу с текстовым содержимым дела."
        )


def _build_case_context(case_data: Dict) -> str:
    """
    Формирует текстовое представление дела для контекста AI.
    """
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
            for idx, img in enumerate(msg['images'], 1):
                if img.get('ocr_text'):
                    context_parts.append("")
                    context_parts.append(
                        f"    [ИЗОБРАЖЕНИЕ #{idx}: {img.get('document_type', 'документ')}]"
                    )
                    context_parts.append(f"    {img['ocr_text']}")

        context_parts.append("")
        context_parts.append("-" * 50)
        context_parts.append("")

    return "\n".join(context_parts)


# --- Основные обработчики ConversationHandler ---

async def start_court_assistant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Точка входа в судебного помощника.
    """
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Проверяем лимиты
    # TODO: Реализовать проверку лимитов как в assistant_flow.py

    # Показываем приветственное сообщение
    welcome_text = """
⚖️ <b>СУДЕБНЫЙ ПОМОЩНИК</b>

Я помогу вам вести судебные дела, анализировать материалы и готовить постановления.

<b>Как начать работу:</b>
1️⃣ Отправьте мне ссылку на тему с форума (судебное дело)
2️⃣ Я распарсю все сообщения и изображения
3️⃣ Задавайте вопросы или используйте инструменты анализа

<b>Пример ссылки:</b>
<code>https://forum.majestic-rp.ru/threads/название.12345/</code>
<code>https://forum.gta5rp.com/threads/название.12345/</code>
<code>https://forum.rmrp.ru/threads/название.12345/</code>
<code>https://forum.russia.online/threads/название.12345/</code>

📎 Отправьте ссылку на дело:
"""

    keyboard = [[InlineKeyboardButton("◀️ Назад в меню", callback_data="main_menu")]]

    await query.edit_message_text(
        welcome_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )

    return AWAITING_COURT_CASE_URL


async def receive_court_case_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Принимает URL темы форума от пользователя и начинает парсинг.
    """
    user_id = update.effective_user.id
    message_text = update.message.text.strip()

    # Проверяем, что это ссылка на поддерживаемый форум
    is_valid_forum = any(f'{forum}/threads/' in message_text for forum in SUPPORTED_FORUMS)
    if not is_valid_forum:
        await update.message.reply_text(
            "❌ Это не похоже на ссылку на тему форума.\n\n"
            "Ссылка должна выглядеть так:\n"
            "<code>https://forum.majestic-rp.ru/threads/название.12345/</code>\n"
            "<code>https://forum.gta5rp.com/threads/название.12345/</code>\n"
            "<code>https://forum.rmrp.ru/threads/название.12345/</code>\n"
            "<code>https://forum.russia.online/threads/название.12345/</code>\n\n"
            "Попробуйте еще раз:",
            parse_mode='HTML'
        )
        return AWAITING_COURT_CASE_URL

    # Определяем сервер (пока используем первый доступный)
    enabled_servers = await get_enabled_servers()
    if not enabled_servers:
        await update.message.reply_text(
            "❌ Нет доступных серверов. Обратитесь к администратору."
        )
        return ConversationHandler.END

    server_name = list(enabled_servers)[0] if isinstance(enabled_servers, dict) else enabled_servers[0]
    if isinstance(server_name, dict):
        server_name = server_name.get('display_name', 'Неизвестный сервер')

    # Показываем сообщение о начале парсинга
    status_msg = await update.message.reply_text(
        "⏳ <b>Начинаю обработку дела...</b>\n\n"
        "📥 Загружаю тему с форума\n"
        "🔍 Парсю сообщения\n"
        "📸 Ищу изображения\n\n"
        "<i>Это может занять 1-3 минуты...</i>",
        parse_mode='HTML'
    )

    try:
        # Парсим тему форума
        logging.info(f"[CourtFlow] Пользователь {user_id} начал парсинг: {message_text}")
        case_data = await parse_court_case_from_url(message_text)

        if not case_data:
            await status_msg.edit_text(
                "❌ <b>Ошибка парсинга</b>\n\n"
                "Не удалось загрузить тему с форума. Проверьте:\n"
                "• Правильность ссылки\n"
                "• Доступность форума\n"
                "• Публичность темы\n\n"
                "Попробуйте еще раз или обратитесь к администратору.",
                parse_mode='HTML'
            )
            return AWAITING_COURT_CASE_URL

        # Обновляем статус
        await status_msg.edit_text(
            f"✅ Тема загружена!\n\n"
            f"📋 Название: {case_data['thread_title']}\n"
            f"💬 Сообщений: {len(case_data['messages'])}\n"
            f"📸 Изображений: {sum(len(m.get('images', [])) for m in case_data['messages'])}\n\n"
            f"⏳ Распознаю текст с изображений...",
            parse_mode='HTML'
        )

        # Обрабатываем изображения с OCR
        # Создаем временный update для передачи в функцию OCR
        temp_update = Update(update.update_id, message=update.message)
        temp_update._effective_message = status_msg

        class TempQuery:
            def __init__(self, msg):
                self.message = msg

        temp_update.callback_query = TempQuery(status_msg)

        await _process_images_with_ocr(case_data['messages'], temp_update)

        # Сохраняем дело в БД
        case_id = await _save_court_case_to_db(user_id, server_name, case_data)

        if not case_id:
            await status_msg.edit_text(
                "❌ Ошибка сохранения дела в базу данных.\n"
                "Попробуйте позже или обратитесь к администратору."
            )
            return ConversationHandler.END

        # Сохраняем данные в контекст
        context.user_data['court_case_id'] = case_id
        context.user_data['court_case_data'] = case_data
        context.user_data['court_server'] = server_name

        # Показываем сводку дела
        await show_case_overview(update, context, status_msg)

        return COURT_DIALOGUE

    except Exception as e:
        logging.error(f"[CourtFlow] Ошибка при обработке дела: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ <b>Произошла ошибка</b>\n\n"
            f"Не удалось обработать дело. Попробуйте позже.\n"
            f"Если ошибка повторяется - обратитесь к администратору.",
            parse_mode='HTML'
        )
        return ConversationHandler.END


async def show_case_overview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_to_edit=None
) -> None:
    """
    Отображает краткую сводку по делу с инструментами.
    """
    case_data = context.user_data.get('court_case_data')

    if not case_data:
        return

    total_messages = len(case_data['messages'])
    total_images = sum(len(m.get('images', [])) for m in case_data['messages'])
    ocr_processed = sum(
        1 for m in case_data['messages']
        for img in m.get('images', [])
        if img.get('ocr_text')
    )

    overview_text = f"""
⚖️ <b>ДЕЛО ЗАГРУЖЕНО И ГОТОВО К АНАЛИЗУ</b>

📋 <b>Название:</b> {case_data['thread_title']}
👤 <b>Истец:</b> {case_data.get('plaintiff', 'Неизвестно')}
👤 <b>Ответчик:</b> {case_data.get('defendant', 'Неизвестно')}

📊 <b>Статистика:</b>
• Сообщений: {total_messages}
• Изображений: {total_images}
• Распознано текста: {ocr_processed}/{total_images}

💡 <b>Вы можете:</b>
• Задать вопрос по делу
• Использовать инструменты анализа (кнопки ниже)

<i>Задайте вопрос или выберите инструмент:</i>
"""

    # Формируем клавиатуру с инструментами (по 2 кнопки в ряд)
    keyboard = []
    for i in range(0, len(COURT_TOOLS), 2):
        row = COURT_TOOLS[i:i+2]
        keyboard.append(row)

    # Добавляем кнопки управления
    keyboard.append([
        InlineKeyboardButton("📂 Мои дела", callback_data="view_court_cases"),
        InlineKeyboardButton("✅ Завершить дело", callback_data="close_court_case")
    ])
    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])

    if message_to_edit:
        await message_to_edit.edit_text(
            overview_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )
    else:
        await update.callback_query.edit_message_text(
            overview_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='HTML'
        )


async def handle_court_dialogue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает вопросы пользователя по делу с помощью AI.
    """
    user_message = update.message.text
    user_id = update.effective_user.id

    case_id = context.user_data.get('court_case_id')
    case_data = context.user_data.get('court_case_data')
    server_name = context.user_data.get('court_server')

    if not all([case_id, case_data, server_name]):
        await update.message.reply_text(
            "❌ Сессия дела не найдена. Начните сначала с /start"
        )
        return ConversationHandler.END

    # Показываем индикатор набора
    await update.message.chat.send_action("typing")

    try:
        # Строим контекст дела
        case_context = _build_case_context(case_data)

        # TODO: Реализовать роутер законов и получение релевантных материалов
        # Пока используем упрощенную версию без фильтрации законов

        system_prompt = get_court_system_prompt()

        # Формируем запрос
        full_prompt = f"""
{system_prompt}

---

{case_context}

---

ВОПРОС СУДЬИ:
{user_message}
"""

        # Определяем функцию для выполнения с rate limiting
        async def send_court_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=COURT_ASSISTANT_MODEL_NAME,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4096
                )
            )

        try:
            # Используем rate limiter с КРИТИЧЕСКИМ приоритетом
            response = await execute_vertex_request(
                func=send_court_request,
                model_name=COURT_ASSISTANT_MODEL_NAME,
                priority=RequestPriority.CRITICAL,  # КРИТИЧЕСКИЙ приоритет для судебного помощника
                user_id=update.effective_user.id,
                max_retries=3
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

            logging.error(f"[CourtFlow] Не удалось выполнить запрос для пользователя {update.effective_user.id}: {e}")
            await update.message.reply_text(
                "⏳ К сожалению, AI-сервис временно перегружен.\n\n"
                "Система управления нагрузкой не смогла обработать ваш запрос.\n\n"
                "Пожалуйста, попробуйте задать свой вопрос через 2-3 минуты.\n\n"
                "Приносим извинения за неудобства."
            )
            return COURT_DIALOGUE

        if not response or not response.text:
            await update.message.reply_text(
                "❌ Не удалось получить ответ от AI. Попробуйте переформулировать вопрос."
            )
            return COURT_DIALOGUE

        ai_response = sanitize_telegram_html(response.text)

        # Сохраняем диалог в БД
        await db_manager.execute(
            """
            INSERT INTO court_dialogue_logs
            (court_case_id, user_message, assistant_response, used_tools)
            VALUES ($1, $2, $3, $4)
            """,
            case_id,
            user_message,
            ai_response,
            []
        )

        # Отправляем ответ (разбиваем на части если нужно)
        messages = _split_long_message(ai_response)
        for msg in messages:
            await update.message.reply_text(msg, parse_mode='HTML')

        return COURT_DIALOGUE

    except Exception as e:
        logging.error(f"[CourtFlow] Ошибка при обработке вопроса: {e}", exc_info=True)
        await send_error_message(update, "генерации ответа")
        return COURT_DIALOGUE


async def handle_court_tool(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает нажатие на кнопки-инструменты.
    """
    query = update.callback_query
    await query.answer()

    tool_name = query.data.replace("tool_", "")

    # Получаем специальный промпт для инструмента
    tool_instruction = get_court_tool_prompt(tool_name)

    # Показываем индикатор
    await query.message.chat.send_action("typing")

    case_data = context.user_data.get('court_case_data')
    case_id = context.user_data.get('court_case_id')

    if not case_data:
        await query.message.reply_text("❌ Данные дела не найдены.")
        return COURT_DIALOGUE

    try:
        # Строим контекст
        case_context = _build_case_context(case_data)

        system_prompt = get_court_system_prompt()

        # Формируем запрос с инструкцией инструмента
        full_prompt = f"""
{system_prompt}

{tool_instruction}

---

{case_context}
"""

        # Определяем функцию для выполнения с rate limiting
        async def send_tool_request():
            # Пробуем получить клиент из менеджера регионов (лучший регион)
            client = get_current_client()
            if not client:
                # Fallback на стандартный клиент из bot_data
                client = context.bot_data.get('genai_client')
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=COURT_ASSISTANT_MODEL_NAME,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    max_output_tokens=8192
                )
            )

        try:
            # Используем rate limiter с ВЫСОКИМ приоритетом (инструменты важны)
            response = await execute_vertex_request(
                func=send_tool_request,
                model_name=COURT_ASSISTANT_MODEL_NAME,
                priority=RequestPriority.HIGH,
                user_id=query.from_user.id,
                max_retries=3
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

            logging.error(f"[CourtFlow] Не удалось выполнить инструмент {tool_name}: {e}")
            await query.message.reply_text(
                "⏳ К сожалению, AI-сервис временно перегружен.\n\n"
                "Система управления нагрузкой не смогла обработать ваш запрос.\n\n"
                "Пожалуйста, попробуйте использовать инструмент через 2-3 минуты.\n\n"
                "Приносим извинения за неудобства."
            )
            return COURT_DIALOGUE

        if not response or not response.text:
            await query.message.reply_text(
                "❌ Не удалось выполнить инструмент. Попробуйте еще раз."
            )
            return COURT_DIALOGUE

        ai_response = sanitize_telegram_html(response.text)

        # Сохраняем в БД
        await db_manager.execute(
            """
            INSERT INTO court_dialogue_logs
            (court_case_id, user_message, assistant_response, used_tools)
            VALUES ($1, $2, $3, $4)
            """,
            case_id,
            f"[Инструмент: {tool_name}]",
            ai_response,
            [tool_name]
        )

        # Отправляем результат
        messages = _split_long_message(ai_response)
        for msg in messages:
            await query.message.reply_text(msg, parse_mode='HTML')

        return COURT_DIALOGUE

    except Exception as e:
        logging.error(f"[CourtFlow] Ошибка при выполнении инструмента {tool_name}: {e}", exc_info=True)
        await query.message.reply_text(
            "❌ Произошла ошибка при выполнении инструмента."
        )
        return COURT_DIALOGUE


async def close_court_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Завершает работу с делом.
    """
    query = update.callback_query
    await query.answer()

    case_id = context.user_data.get('court_case_id')

    if case_id:
        # Обновляем статус дела в БД
        await db_manager.execute(
            """
            UPDATE court_cases
            SET status = 'closed', closed_at = NOW()
            WHERE id = $1
            """,
            case_id
        )

        logging.info(f"[CourtFlow] Дело #{case_id} завершено пользователем")

    # Очищаем контекст
    context.user_data.pop('court_case_id', None)
    context.user_data.pop('court_case_data', None)
    context.user_data.pop('court_server', None)

    await query.edit_message_text(
        "✅ <b>Дело завершено</b>\n\n"
        "Спасибо за использование судебного помощника!\n"
        "Вы можете начать новое дело или вернуться в меню.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⚖️ Новое дело", callback_data="start_court_assistant")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
        ])
    )

    return ConversationHandler.END


async def view_court_cases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Показывает список активных дел пользователя.
    """
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id

    # Получаем активные дела из БД
    cases = await db_manager.fetch(
        """
        SELECT id, case_title, plaintiff, defendant, created_at
        FROM court_cases
        WHERE user_id = $1 AND status = 'active'
        ORDER BY created_at DESC
        LIMIT 10
        """,
        user_id
    )

    if not cases:
        await query.edit_message_text(
            "📂 <b>У вас нет активных дел</b>\n\n"
            "Начните новое дело, отправив ссылку на тему форума.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⚖️ Начать новое дело", callback_data="start_court_assistant")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Формируем список дел
    cases_text = "📂 <b>ВАШИ АКТИВНЫЕ ДЕЛА</b>\n\n"

    keyboard = []
    for case in cases:
        case_info = (
            f"📋 <b>Дело #{case['id']}</b>\n"
            f"   Название: {case['case_title'][:50]}...\n"
            f"   Истец: {case['plaintiff']}\n"
            f"   Ответчик: {case['defendant']}\n"
            f"   Создано: {case['created_at'].strftime('%d.%m.%Y %H:%M')}\n\n"
        )
        cases_text += case_info

        # Кнопка для возврата к делу (TODO: реализовать переключение между делами)

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="start_court_assistant")])

    await query.edit_message_text(
        cases_text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return VIEWING_COURT_CASES


# --- ConversationHandler ---

def get_court_assistant_conv_handler():
    """
    Возвращает ConversationHandler для судебного помощника.
    """
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_court_assistant, pattern="^start_court_assistant$")
        ],
        states={
            AWAITING_COURT_CASE_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_court_case_url),
                CallbackQueryHandler(main_menu, pattern="^main_menu$"),
            ],
            COURT_DIALOGUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_court_dialogue),
                CallbackQueryHandler(handle_court_tool, pattern="^tool_court_"),
                CallbackQueryHandler(close_court_case, pattern="^close_court_case$"),
                CallbackQueryHandler(view_court_cases, pattern="^view_court_cases$"),
                CallbackQueryHandler(main_menu, pattern="^main_menu$"),
            ],
            VIEWING_COURT_CASES: [
                CallbackQueryHandler(start_court_assistant, pattern="^start_court_assistant$"),
                CallbackQueryHandler(main_menu, pattern="^main_menu$"),
            ],
        },
        fallbacks=[
            CommandHandler("start", start),
            CallbackQueryHandler(main_menu, pattern="^main_menu$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
        ],
        name="court_assistant_conversation",
        persistent=False,
    )

