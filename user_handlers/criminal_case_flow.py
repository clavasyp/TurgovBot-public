# START OF FILE user_handlers/criminal_case_flow.py

"""
Модуль для системы создания уголовных дел (Criminal Case Writer).

Содержит:
- CriminalCaseManager: класс для управления сессией и 3-фазным циклом
- ConversationHandler для пошагового создания дела
- Карусельный UI для навигации между частями
- Интеграция с Google Docs для экспорта
- Автозакрытие сессий через 48 часов
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, Optional

from google.genai import types
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest
from telegram.ext import (
    ContextTypes, ConversationHandler, CallbackQueryHandler,
    MessageHandler, filters
)

# Локальные импорты
import config
from config import (
    CCW_CHOOSING_PROJECT, CCW_CHOOSING_SERVER,
    CCW_ENTER_CASE_NUMBER, CCW_COLLECT_FACTS, CCW_PHASE_A, CCW_PHASE_B,
    CCW_USER_REVIEW, CCW_NAVIGATE, CCW_EXPORT,
    CCW_MODEL_NAME, CCW_SESSION_TIMEOUT_HOURS, CCW_TEMPLATE_DOC_ID,
    CRIMINAL_CASE_PARTS_ORDER, CRIMINAL_CASE_PARTS_DISPLAY,
    PROJECTS_CONFIG, SERVERS_CONFIG
)
from database.manager import db_manager
from utils import (
    sanitize_telegram_html, safe_answer_callback_query,
    get_enabled_servers, get_enabled_projects, send_project_selection_menu
)
from prompts import (
    get_ccw_context_analyzer_prompt,
    get_ccw_draft_generator_prompt, get_ccw_self_review_prompt
)
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client
from .core import check_and_update_usage, _display_main_menu, unknown_text_fallback


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

async def safe_edit_message_text(query, text: str, **kwargs):
    """
    Безопасно редактирует сообщение, игнорируя ошибку 'Message is not modified'.
    """
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            # Игнорируем - пользователь нажал ту же кнопку
            logging.debug(f"[CCW] Message not modified (ignored): {e}")
            return None
        raise


# === КОНСТАНТЫ ===

PARTS_PER_PAGE = 6  # Все части помещаются на одной странице


# === КЛАСС МЕНЕДЖЕРА СЕССИИ ===

class CriminalCaseManager:
    """Управляет сессией создания уголовного дела."""

    def __init__(self, session_id: int):
        self.session_id = session_id
        self._session_data = None

    @classmethod
    async def create_session(cls, user_id: int, server_id: str) -> 'CriminalCaseManager':
        """Создаёт новую сессию создания дела."""
        query = """
            INSERT INTO criminal_case_sessions (user_id, server_id, collected_data, status)
            VALUES ($1, $2, '{}', 'active')
            RETURNING id
        """
        session_id = await db_manager.fetchval(query, user_id, server_id)
        logging.info(f"[CCW] Создана сессия #{session_id} для пользователя {user_id}, сервер {server_id}")
        return cls(session_id)

    @classmethod
    async def get_active_session(cls, user_id: int) -> Optional['CriminalCaseManager']:
        """Получает активную сессию пользователя."""
        query = """
            SELECT id FROM criminal_case_sessions
            WHERE user_id = $1 AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """
        session_id = await db_manager.fetchval(query, user_id)
        if session_id:
            return cls(session_id)
        return None

    async def load_session_data(self) -> Dict:
        """Загружает данные сессии из БД."""
        if self._session_data is None:
            query = """
                SELECT * FROM criminal_case_sessions WHERE id = $1
            """
            self._session_data = await db_manager.fetchrow(query, self.session_id)
        return self._session_data

    async def update_session(self, **kwargs) -> None:
        """Обновляет поля сессии."""
        set_clauses = []
        values = []
        idx = 1

        for key, value in kwargs.items():
            set_clauses.append(f"{key} = ${idx}")
            if key == 'collected_data' and isinstance(value, dict):
                value = json.dumps(value)
            values.append(value)
            idx += 1

        values.append(self.session_id)
        query = f"""
            UPDATE criminal_case_sessions
            SET {', '.join(set_clauses)}, updated_at = NOW()
            WHERE id = ${idx}
        """
        await db_manager.execute(query, *values)
        self._session_data = None  # Сбрасываем кэш

    async def get_collected_data(self) -> Dict:
        """Получает собранные данные дела."""
        session = await self.load_session_data()
        data = session.get('collected_data', {})
        if isinstance(data, str):
            return json.loads(data)
        return data or {}

    async def update_collected_data(self, new_data: Dict) -> None:
        """Обновляет собранные данные."""
        current = await self.get_collected_data()
        current.update(new_data)
        await self.update_session(collected_data=current)

    async def save_part_version(
        self,
        part_type: str,
        content: str,
        ai_review_notes: str = None,
        is_final: bool = False
    ) -> int:
        """Сохраняет версию части дела."""
        # Получаем текущую максимальную версию
        query = """
            SELECT COALESCE(MAX(version), 0) FROM criminal_case_parts
            WHERE session_id = $1 AND part_type = $2
        """
        max_version = await db_manager.fetchval(query, self.session_id, part_type)
        new_version = max_version + 1

        # Вставляем новую версию
        insert_query = """
            INSERT INTO criminal_case_parts 
            (session_id, part_type, version, content, ai_review_notes, is_final)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """
        part_id = await db_manager.fetchval(
            insert_query,
            self.session_id, part_type, new_version, content, ai_review_notes, is_final
        )

        logging.info(f"[CCW] Сохранена часть {part_type} v{new_version} для сессии #{self.session_id}")
        return part_id

    async def get_latest_part_content(self, part_type: str) -> Optional[str]:
        """Получает последнюю версию части."""
        query = """
            SELECT content FROM criminal_case_parts
            WHERE session_id = $1 AND part_type = $2
            ORDER BY version DESC LIMIT 1
        """
        return await db_manager.fetchval(query, self.session_id, part_type)

    async def get_final_parts(self) -> Dict[str, str]:
        """Получает все финальные части дела."""
        query = """
            SELECT part_type, content FROM criminal_case_parts
            WHERE session_id = $1 AND is_final = TRUE
        """
        rows = await db_manager.fetch(query, self.session_id)
        return {row['part_type']: row['content'] for row in rows}

    async def mark_part_final(self, part_type: str) -> None:
        """Помечает последнюю версию части как финальную."""
        # Сначала снимаем флаг is_final со всех версий этой части
        await db_manager.execute(
            "UPDATE criminal_case_parts SET is_final = FALSE WHERE session_id = $1 AND part_type = $2",
            self.session_id, part_type
        )
        # Ставим флаг на последнюю версию
        await db_manager.execute("""
            UPDATE criminal_case_parts SET is_final = TRUE
            WHERE id = (
                SELECT id FROM criminal_case_parts
                WHERE session_id = $1 AND part_type = $2
                ORDER BY version DESC LIMIT 1
            )
        """, self.session_id, part_type)

    async def close_session(self, status: str = 'completed') -> None:
        """Закрывает сессию."""
        await self.update_session(status=status)
        logging.info(f"[CCW] Сессия #{self.session_id} закрыта со статусом '{status}'")


# === ФУНКЦИИ КЭШИРОВАНИЯ СТРУКТУРЫ ШАБЛОНА ===

async def get_template_structure() -> Optional[Dict]:
    """Получает структуру шаблона из кэша."""
    query = "SELECT value FROM system_cache WHERE key = 'ccw_template_structure'"
    result = await db_manager.fetchval(query)

    if not result:
        return None

    try:
        # Парсим JSON если это строка
        if isinstance(result, str):
            data = json.loads(result)
        else:
            data = result

        # Строгая валидация — должен быть словарь с ключом 'sections' который является списком
        if isinstance(data, dict) and isinstance(data.get('sections'), list) and len(data['sections']) > 0:
            logging.debug(f"[CCW] Кэш валиден: {len(data['sections'])} секций")
            return data
        else:
            logging.warning(f"[CCW] Кэш структуры некорректен (неправильный формат), очищаю... Тип: {type(data)}")
            await db_manager.execute("DELETE FROM system_cache WHERE key = 'ccw_template_structure'")
            return None

    except (json.JSONDecodeError, TypeError) as e:
        logging.warning(f"[CCW] Ошибка парсинга кэша: {e}, очищаю...")
        await db_manager.execute("DELETE FROM system_cache WHERE key = 'ccw_template_structure'")
        return None


async def save_template_structure(structure: Dict) -> None:
    """Сохраняет структуру шаблона в кэш."""
    query = """
        INSERT INTO system_cache (key, value, updated_at)
        VALUES ('ccw_template_structure', $1, NOW())
        ON CONFLICT (key) DO UPDATE SET value = $1, updated_at = NOW()
    """
    await db_manager.execute(query, json.dumps(structure))
    logging.info("[CCW] Структура шаблона сохранена в кэш")


async def get_or_analyze_template_structure() -> Dict:
    """
    ВРЕМЕННО: Возвращает пустую структуру, т.к. пока не используем замену плейсхолдеров.
    """
    return {}


# === UI ФУНКЦИИ ===

def build_progress_bar(current: int, total: int) -> str:
    """Создаёт текстовый прогресс-бар."""
    filled = current
    empty = total - current
    return '■' * filled + '□' * empty


def build_carousel_keyboard(
    current_part_index: int,
    session_id: int,
    show_accept: bool = True,
    show_navigation: bool = True
) -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру карусельного UI.
    """
    keyboard = []

    # Ряд навигации
    if show_navigation:
        nav_row = []
        if current_part_index > 0:
            nav_row.append(InlineKeyboardButton("◀️ Пред.", callback_data=f"ccw_prev_{session_id}"))

        # Индикатор текущей части
        part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(
            CRIMINAL_CASE_PARTS_ORDER[current_part_index],
            f"Часть {current_part_index + 1}"
        )
        nav_row.append(InlineKeyboardButton(
            f"{current_part_index + 1}/{len(CRIMINAL_CASE_PARTS_ORDER)}",
            callback_data="ccw_noop"
        ))

        if current_part_index < len(CRIMINAL_CASE_PARTS_ORDER) - 1:
            nav_row.append(InlineKeyboardButton("След. ▶️", callback_data=f"ccw_next_{session_id}"))

        keyboard.append(nav_row)

    # Ряд действий
    if show_accept:
        keyboard.append([
            InlineKeyboardButton("✅ Принять", callback_data=f"ccw_accept_{session_id}"),
        ])
        keyboard.append([
            InlineKeyboardButton("✏️ Уточнить", callback_data=f"ccw_refine_{session_id}"),
            InlineKeyboardButton("🔄 Пересоздать", callback_data=f"ccw_regenerate_{session_id}"),
        ])

    # Кнопка отмены/завершения
    keyboard.append([
        InlineKeyboardButton("🏁 Завершить и экспортировать", callback_data=f"ccw_export_{session_id}"),
    ])
    keyboard.append([
        InlineKeyboardButton("❌ Отменить", callback_data=f"ccw_cancel_{session_id}"),
    ])

    return InlineKeyboardMarkup(keyboard)


def format_part_message(
    case_number: str,
    part_index: int,
    part_content: str,
    ai_review: str = None
) -> str:
    """Форматирует сообщение для отображения части дела."""
    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]
    part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)
    progress = build_progress_bar(part_index + 1, len(CRIMINAL_CASE_PARTS_ORDER))

    message = f"""══════════════════════════════════════════
📋 <b>УГОЛОВНОЕ ДЕЛО {case_number}</b>
[{progress}] Часть {part_index + 1} из {len(CRIMINAL_CASE_PARTS_ORDER)} — {part_name}
══════════════════════════════════════════

{part_content}

──────────────────────────────────────────"""

    if ai_review:
        message += f"\n🤖 <b>AI-проверка:</b> {ai_review}\n──────────────────────────────────────────"

    return message


# === ОБРАБОТЧИКИ CONVERSATION ===

async def start_criminal_case_writer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало создания уголовного дела - показывает меню с историей."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    # Проверка доступа: функция в бета-тестировании, доступна только тестерам и админам
    user_record = await db_manager.fetchrow(
        "SELECT is_admin, is_tester FROM users WHERE user_id = $1", user_id
    )
    is_admin = (user_record and user_record['is_admin']) or user_id == config.SUPER_ADMIN_ID
    is_tester = user_record and user_record.get('is_tester', False)

    if not is_admin and not is_tester:
        await safe_edit_message_text(
            query,
            "🔒 <b>Функция в бета-тестировании</b>\n\n"
            "Создание уголовных дел пока доступно только тестировщикам.\n\n"
            "Следите за обновлениями — скоро функция будет доступна всем!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Получаем историю созданных дел
    completed_cases = await db_manager.fetch("""
        SELECT id, case_number, created_at, google_doc_url, status
        FROM criminal_case_sessions
        WHERE user_id = $1 AND status IN ('completed', 'active')
        ORDER BY created_at DESC
        LIMIT 10
    """, user_id)

    # Проверяем, есть ли активная сессия
    active_session = None
    for case in completed_cases:
        if case['status'] == 'active':
            active_session = case
            break

    # Формируем сообщение
    text = "📋 <b>Уголовные дела</b>\n\n"

    if completed_cases:
        text += "<b>📜 История дел:</b>\n"
        for i, case in enumerate(completed_cases[:5], 1):
            status_emoji = "🔄" if case['status'] == 'active' else "✅"
            date_str = case['created_at'].strftime('%d.%m.%Y %H:%M') if case['created_at'] else 'N/A'
            case_num = case['case_number'] or 'Без номера'
            text += f"{status_emoji} <b>{case_num}</b> — {date_str}\n"
        text += "\n"
    else:
        text += "<i>У вас пока нет созданных дел.</i>\n\n"

    # Формируем кнопки
    keyboard = []

    # Кнопка продолжения активной сессии
    if active_session:
        keyboard.append([InlineKeyboardButton(
            f"▶️ Продолжить: {active_session['case_number'] or 'Без номера'}",
            callback_data=f"ccw_continue_{active_session['id']}"
        )])

    # Кнопка создания нового дела
    keyboard.append([InlineKeyboardButton("➕ Создать новое дело", callback_data="ccw_create_new")])

    # Кнопки для просмотра/пересоздания завершённых дел
    completed_only = [c for c in completed_cases if c['status'] == 'completed' and c['google_doc_url']]
    if completed_only:
        keyboard.append([InlineKeyboardButton("📄 История созданных дел", callback_data="ccw_history_list")])

    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CCW_NAVIGATE


async def show_ccw_history_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список завершённых дел с возможностью пересоздать документ."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    completed_cases = await db_manager.fetch("""
        SELECT id, case_number, created_at, google_doc_url
        FROM criminal_case_sessions
        WHERE user_id = $1 AND status = 'completed'
        ORDER BY created_at DESC
        LIMIT 10
    """, user_id)

    if not completed_cases:
        await safe_edit_message_text(
            query,
            "📜 <b>История дел</b>\n\n<i>Нет завершённых дел.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="start_criminal_case")]
            ])
        )
        return CCW_NAVIGATE

    text = "📜 <b>Завершённые уголовные дела</b>\n\n"
    text += "<i>Выберите дело для просмотра или пересоздания документа:</i>\n\n"

    keyboard = []
    for case in completed_cases:
        date_str = case['created_at'].strftime('%d.%m.%Y') if case['created_at'] else 'N/A'
        case_num = case['case_number'] or 'Без номера'
        btn_text = f"📋 {case_num} ({date_str})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"ccw_view_case_{case['id']}")])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="start_criminal_case")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return CCW_NAVIGATE


async def view_completed_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детали завершённого дела с кнопкой пересоздания."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("ccw_view_case_")[1])

    case_data = await db_manager.fetchrow("""
        SELECT id, case_number, created_at, google_doc_url, google_doc_id, collected_data
        FROM criminal_case_sessions
        WHERE id = $1
    """, session_id)

    if not case_data:
        await safe_edit_message_text(
            query,
            "❌ Дело не найдено.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Назад", callback_data="ccw_history_list")]
            ])
        )
        return CCW_NAVIGATE

    date_str = case_data['created_at'].strftime('%d.%m.%Y %H:%M') if case_data['created_at'] else 'N/A'
    case_num = case_data['case_number'] or 'Без номера'

    text = f"📋 <b>Уголовное дело {case_num}</b>\n\n"
    text += f"📅 Создано: {date_str}\n"

    if case_data['google_doc_url']:
        text += f"📄 <a href=\"{case_data['google_doc_url']}\">Открыть документ</a>\n"

    text += "\n<i>⚠️ Кнопка пересоздания — временная (для тестирования)</i>"

    keyboard = []

    if case_data['google_doc_url']:
        keyboard.append([InlineKeyboardButton("🔗 Открыть документ", url=case_data['google_doc_url'])])

    # Временная кнопка пересоздания документа
    keyboard.append([InlineKeyboardButton("🔄 Пересоздать документ (ТЕСТ)", callback_data=f"ccw_recreate_doc_{session_id}")])
    keyboard.append([InlineKeyboardButton("◀️ К списку дел", callback_data="ccw_history_list")])
    keyboard.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )
    return CCW_NAVIGATE


async def recreate_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пересоздаёт Google Doc для существующего дела (временная функция для тестирования)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("ccw_recreate_doc_")[1])

    await safe_edit_message_text(
        query,
        "⏳ <b>Пересоздаю документ Google Doc...</b>\n"
        "Это может занять несколько секунд.",
        parse_mode='HTML'
    )

    try:
        from google_docs_exporter import create_criminal_case_document, fill_criminal_case_document

        # ТЕСТ: Принудительно очищаем кэш шаблона для свежего анализа
        await db_manager.execute("DELETE FROM system_cache WHERE key = 'ccw_template_structure'")
        logging.info("[CCW] Кэш структуры шаблона очищен для тестирования")

        manager = CriminalCaseManager(session_id)
        session_data = await manager.load_session_data()
        final_parts = await manager.get_final_parts()
        collected_data = await manager.get_collected_data()
        case_number = session_data.get('case_number', 'DJR-XXX')

        # Получаем структуру шаблона из AI-анализа
        template_structure = await get_or_analyze_template_structure()

        # Создаём новый документ (копируем шаблон)
        doc_id, doc_url = await create_criminal_case_document(case_number)

        # Заполняем документ сгенерированным контентом
        fill_criminal_case_document(
            doc_id=doc_id,
            case_number=case_number,
            collected_data=collected_data,
            final_parts=final_parts,
            template_structure=template_structure
        )

        # Обновляем информацию о документе в БД
        await manager.update_session(
            google_doc_id=doc_id,
            google_doc_url=doc_url
        )

        await safe_edit_message_text(
            query,
            f"✅ <b>Документ пересоздан!</b>\n\n"
            f"📋 Дело: <b>{case_number}</b>\n"
            f"🔗 <a href=\"{doc_url}\">Открыть документ</a>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Открыть документ", url=doc_url)],
                [InlineKeyboardButton("◀️ К списку дел", callback_data="ccw_history_list")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
            ]),
            disable_web_page_preview=True
        )

    except Exception as e:
        logging.error(f"[CCW] Ошибка пересоздания документа: {e}")
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка при пересоздании документа</b>\n\n"
            f"<code>{str(e)[:200]}</code>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"ccw_recreate_doc_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"ccw_view_case_{session_id}")]
            ])
        )

    return CCW_NAVIGATE


async def create_new_case(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает создание нового дела (показывает выбор проекта)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    # Проверяем лимиты
    allowed, current_usage = await check_and_update_usage(user_id, 'criminal_case_gen')
    if not allowed:
        await safe_edit_message_text(
            query,
            "❌ Вы исчерпали лимит создания уголовных дел.\n"
            "Лимит обновится через некоторое время.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Показываем выбор проекта
    enabled_projects = await get_enabled_projects()

    # Если только один проект - пропускаем выбор и сразу показываем серверы
    if len(enabled_projects) == 1:
        context.user_data['ccw_project_id'] = enabled_projects[0]
        return await _show_ccw_servers(update, context, enabled_projects[0])

    # Удаляем старое сообщение
    try:
        await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта с картинкой
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="ccw_project_",
        text="📋 <b>Создание уголовного дела</b>\n\n🌐 <b>Выберите проект:</b>",
        back_callback="start_criminal_case"
    )
    return CCW_CHOOSING_PROJECT


async def handle_continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Продолжает существующую сессию создания дела."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    # Извлекаем session_id из callback_data
    session_id = int(query.data.split("ccw_continue_")[1])

    manager = CriminalCaseManager(session_id)
    session_data = await manager.load_session_data()

    if not session_data:
        await safe_edit_message_text(
            query,
            "❌ Сессия не найдена. Начните создание дела заново.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Начать заново", callback_data="start_criminal_case")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Восстанавливаем контекст
    context.user_data['ccw_session_id'] = session_id
    context.user_data['ccw_case_number'] = session_data.get('case_number', 'DJR-XXX')
    context.user_data['ccw_server_id'] = session_data.get('server_id')

    # Получаем данные о прогрессе
    collected_data = await manager.get_collected_data()

    # Определяем текущую часть - находим первую незавершённую
    final_parts = await manager.get_final_parts()
    current_part_index = 0

    for i, part_type in enumerate(CRIMINAL_CASE_PARTS_ORDER):
        if part_type not in final_parts:
            current_part_index = i
            break
        current_part_index = i + 1  # Все части готовы

    # Если все части готовы - переходим к экспорту
    if current_part_index >= len(CRIMINAL_CASE_PARTS_ORDER):
        context.user_data['ccw_current_part_index'] = len(CRIMINAL_CASE_PARTS_ORDER) - 1
        return await show_export_preview(update, context)

    context.user_data['ccw_current_part_index'] = current_part_index

    # Проверяем, есть ли уже контент для текущей части
    part_type = CRIMINAL_CASE_PARTS_ORDER[current_part_index]
    existing_content = await manager.get_latest_part_content(part_type)

    if existing_content:
        # Есть черновик - показываем его для ревью
        return await show_current_part(update, context)
    elif collected_data.get('facts'):
        # Есть факты, но нет черновика - запускаем генерацию
        await safe_edit_message_text(
            query,
            f"⏳ <b>Продолжаем создание дела {context.user_data['ccw_case_number']}...</b>\n\n"
            f"Генерирую часть: {CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)}",
            parse_mode='HTML'
        )
        return await run_phase_a(update, context)
    else:
        # Нет фактов - нужно ввести описание дела
        server_id = context.user_data.get('ccw_server_id')
        server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

        await safe_edit_message_text(
            query,
            f"📋 <b>Продолжаем создание дела {context.user_data['ccw_case_number']}</b>\n"
            f"🖥️ Сервер: <b>{server_name}</b>\n\n"
            "📝 <b>Опишите обстоятельства дела:</b>\n\n"
            "• Кто обвиняемый (ФИО, должность)?\n"
            "• В чём суть обвинения?\n"
            "• Кто потерпевший?\n"
            "• Когда и где произошло преступление?\n"
            "• Какие есть доказательства?\n\n"
            "<i>Чем подробнее вы опишете ситуацию, тем качественнее будет сгенерировано дело.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data=f"ccw_cancel_{session_id}")]
            ])
        )
        return CCW_COLLECT_FACTS


async def handle_restart_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Перезапускает сессию - удаляет старую и начинает новую."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    # Закрываем все активные сессии пользователя
    query_close = """
        UPDATE criminal_case_sessions
        SET status = 'cancelled', updated_at = NOW()
        WHERE user_id = $1 AND status = 'active'
    """
    await db_manager.execute(query_close, user_id)

    # Очищаем данные в контексте
    for key in list(context.user_data.keys()):
        if key.startswith('ccw_'):
            del context.user_data[key]

    # Показываем выбор проекта (как будто начинаем заново)
    enabled_projects = await get_enabled_projects()

    if len(enabled_projects) == 1:
        context.user_data['ccw_project_id'] = enabled_projects[0]
        return await _show_ccw_servers(update, context, enabled_projects[0])

    # Удаляем старое сообщение
    try:
        await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="ccw_project_",
        text="📋 <b>Создание уголовного дела</b>\n\n🌐 <b>Выберите проект:</b>",
        back_callback="main_menu"
    )
    return CCW_CHOOSING_PROJECT


async def _show_ccw_servers(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: str) -> int:
    """Показывает список серверов для выбранного проекта."""
    query = update.callback_query

    enabled_servers = await get_enabled_servers(project_id)
    if not enabled_servers:
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
            await safe_edit_message_text(query, "На данный момент нет доступных серверов для этого проекта.")
        return ConversationHandler.END

    project_data = PROJECTS_CONFIG.get(project_id, {})
    project_name = project_data.get('display_name', project_id)

    keyboard = [
        [InlineKeyboardButton(
            SERVERS_CONFIG[server_id]['display_name'],
            callback_data=f"ccw_server_{server_id}"
        )]
        for server_id in enabled_servers
    ]

    # Кнопка "Назад" зависит от количества проектов
    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) > 1:
        keyboard.append([InlineKeyboardButton("◀️ К выбору проекта", callback_data="start_criminal_case")])
    else:
        keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])

    text = (
        f"📋 <b>Создание уголовного дела</b>\n"
        f"🎮 <b>{project_name}</b>\n\n"
        "Выберите сервер, законы которого будут использоваться:"
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
        await safe_edit_message_text(
            query,
            text,
            reply_markup=reply_markup,
            parse_mode='HTML'
        )
    return CCW_CHOOSING_SERVER


async def select_ccw_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный проект и показывает серверы."""
    query = update.callback_query
    await query.answer()

    project_id = query.data.split("ccw_project_", 1)[1]
    context.user_data['ccw_project_id'] = project_id

    return await _show_ccw_servers(update, context, project_id)


async def select_ccw_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный сервер и переходит к вводу номера дела."""
    query = update.callback_query
    await query.answer()

    server_id = query.data.split("ccw_server_", 1)[1]
    context.user_data['ccw_server_id'] = server_id

    server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    # Запрашиваем номер дела
    await safe_edit_message_text(
        query,
        f"📋 <b>Создание уголовного дела</b>\n"
        f"🖥️ Сервер: <b>{server_name}</b>\n\n"
        "Введите номер дела в формате <code>DJR-XXX</code>\n"
        "(например: DJR-015, DJR-003)\n\n"
        "<i>Этот номер будет использован в заголовке документа.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Назад", callback_data=f"ccw_project_{context.user_data.get('ccw_project_id', '')}")]
        ])
    )

    return CCW_ENTER_CASE_NUMBER


async def handle_case_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод номера дела."""
    user_id = update.effective_user.id
    case_number = update.message.text.strip().upper()
    server_id = context.user_data.get('ccw_server_id')

    # Валидация формата
    if not re.match(r'^DJR-?\d{1,4}$', case_number):
        await update.message.reply_text(
            "❌ Неверный формат номера дела.\n"
            "Используйте формат <code>DJR-XXX</code> (например: DJR-015)",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ К выбору сервера", callback_data=f"ccw_project_{context.user_data.get('ccw_project_id', '')}")]
            ])
        )
        return CCW_ENTER_CASE_NUMBER

    # Нормализуем формат
    if '-' not in case_number:
        case_number = f"DJR-{case_number[3:]}"

    # Создаём сессию с выбранным сервером
    manager = await CriminalCaseManager.create_session(user_id, server_id)
    await manager.update_session(case_number=case_number)

    context.user_data['ccw_session_id'] = manager.session_id
    context.user_data['ccw_case_number'] = case_number

    # Планируем автозакрытие
    schedule_ccw_session_closure(context, manager.session_id, user_id)

    server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    # Запрашиваем описание дела
    await update.message.reply_text(
        f"✅ Номер дела: <b>{case_number}</b>\n"
        f"🖥️ Сервер: <b>{server_name}</b>\n\n"
        "📝 <b>Опишите обстоятельства дела:</b>\n\n"
        "• Кто обвиняемый (ФИО, должность)?\n"
        "• В чём суть обвинения?\n"
        "• Кто потерпевший?\n"
        "• Когда и где произошло преступление?\n"
        "• Какие есть доказательства?\n\n"
        "<i>Чем подробнее вы опишете ситуацию, тем качественнее будет сгенерировано дело.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"ccw_cancel_{manager.session_id}")]
        ])
    )

    return CCW_COLLECT_FACTS


async def handle_facts_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает описание обстоятельств дела."""
    user_id = update.effective_user.id
    facts_text = update.message.text.strip()

    if len(facts_text) < 50:
        await update.message.reply_text(
            "⚠️ Описание слишком короткое. Пожалуйста, предоставьте больше деталей.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data="ccw_cancel")]
            ])
        )
        return CCW_COLLECT_FACTS

    session_id = context.user_data.get('ccw_session_id')
    if not session_id:
        await update.message.reply_text("❌ Ошибка сессии. Начните заново.")
        return ConversationHandler.END

    manager = CriminalCaseManager(session_id)

    # Сохраняем факты
    await manager.update_collected_data({
        'initial_facts': facts_text,
        'input_timestamp': datetime.now().isoformat()
    })

    # Отправляем сообщение о начале генерации
    progress_msg = await update.message.reply_text(
        "⏳ <b>Анализирую материалы дела...</b>\n\n"
        "Это может занять несколько секунд.",
        parse_mode='HTML'
    )

    # Запускаем фазу A для первой части
    context.user_data['ccw_current_part_index'] = 0
    context.user_data['ccw_progress_message_id'] = progress_msg.message_id

    return await run_phase_a(update, context)


async def run_phase_a(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Фаза A: Анализ контекста и генерация уточняющих вопросов.
    """
    session_id = context.user_data.get('ccw_session_id')
    part_index = context.user_data.get('ccw_current_part_index', 0)
    case_number = context.user_data.get('ccw_case_number', 'DJR-XXX')

    manager = CriminalCaseManager(session_id)
    collected_data = await manager.get_collected_data()

    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]
    part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)

    # Получаем релевантные законы
    server_id = context.user_data.get('ccw_server_id', 'majestic_1')
    relevant_laws = await get_relevant_laws_for_ccw(server_id, collected_data.get('initial_facts', ''))

    # Формируем промпт для фазы A
    prompt = get_ccw_context_analyzer_prompt(
        part_name=part_name,
        case_number=case_number,
        collected_data=json.dumps(collected_data, ensure_ascii=False, indent=2),
        relevant_laws=relevant_laws
    )

    try:
        # Создаём функцию для запроса
        async def make_phase_a_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=CCW_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=2048
                )
            )

        response = await execute_vertex_request(
            func=make_phase_a_request,
            model_name=CCW_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        analysis_text = sanitize_telegram_html(response.text)

        # Проверяем, есть ли уточняющие вопросы
        has_questions = '❓' in analysis_text or 'вопрос' in analysis_text.lower()

        keyboard = []
        if has_questions:
            keyboard.append([
                InlineKeyboardButton("⏭️ Пропустить вопросы", callback_data=f"ccw_skip_questions_{session_id}")
            ])
        keyboard.append([
            InlineKeyboardButton("❌ Отмена", callback_data=f"ccw_cancel_{session_id}")
        ])

        # Отправляем результат анализа
        if update.callback_query:
            await safe_edit_message_text(
                update.callback_query,
                analysis_text,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Редактируем сообщение о прогрессе
            chat_id = update.effective_chat.id
            msg_id = context.user_data.get('ccw_progress_message_id')
            if msg_id:
                try:
                    await context.bot.edit_message_text(
                        analysis_text,
                        chat_id=chat_id,
                        message_id=msg_id,
                        parse_mode='HTML',
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except BadRequest as e:
                    if "Message is not modified" not in str(e):
                        raise
            else:
                await update.message.reply_text(
                    analysis_text,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

        context.user_data['ccw_phase'] = 'A'
        return CCW_PHASE_A

    except Exception as e:
        logging.error(f"[CCW] Ошибка в фазе A: {e}")
        error_text = "❌ Произошла ошибка при анализе. Попробуйте ещё раз."
        if update.callback_query:
            await safe_edit_message_text(update.callback_query, error_text)
        else:
            await update.effective_chat.send_message(error_text)
        return CCW_COLLECT_FACTS


async def handle_phase_a_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ответы на уточняющие вопросы фазы A."""
    answer_text = update.message.text.strip()
    session_id = context.user_data.get('ccw_session_id')
    part_index = context.user_data.get('ccw_current_part_index', 0)

    manager = CriminalCaseManager(session_id)

    # Сохраняем ответ
    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]
    collected_data = await manager.get_collected_data()

    if 'phase_a_answers' not in collected_data:
        collected_data['phase_a_answers'] = {}
    if part_type not in collected_data['phase_a_answers']:
        collected_data['phase_a_answers'][part_type] = []

    collected_data['phase_a_answers'][part_type].append(answer_text)
    await manager.update_collected_data(collected_data)

    # Переходим к фазе B
    await update.message.reply_text(
        "⏳ <b>Генерирую черновик...</b>",
        parse_mode='HTML'
    )

    return await run_phase_b(update, context)


async def skip_questions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик пропуска уточняющих вопросов."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    await safe_edit_message_text(
        query,
        "⏳ <b>Генерирую черновик с имеющимися данными...</b>",
        parse_mode='HTML'
    )

    return await run_phase_b(update, context)


async def run_phase_b(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Фаза B: Генерация черновика части дела.
    """
    session_id = context.user_data.get('ccw_session_id')
    part_index = context.user_data.get('ccw_current_part_index', 0)
    case_number = context.user_data.get('ccw_case_number', 'DJR-XXX')

    manager = CriminalCaseManager(session_id)
    collected_data = await manager.get_collected_data()

    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]
    part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)

    # Получаем ответы пользователя на уточняющие вопросы
    user_answers = collected_data.get('phase_a_answers', {}).get(part_type, [])
    user_answers_text = '\n'.join(user_answers) if user_answers else 'Нет дополнительных ответов'

    # Получаем релевантные законы
    server_id = context.user_data.get('ccw_server_id', 'majestic_1')
    relevant_laws = await get_relevant_laws_for_ccw(server_id, collected_data.get('initial_facts', ''))

    # Получаем референсные формулировки из шаблонов
    reference_formulations = await get_reference_formulations(part_type)

    # Формируем промпт для фазы B
    prompt = get_ccw_draft_generator_prompt(
        part_name=part_name,
        case_number=case_number,
        collected_data=json.dumps(collected_data, ensure_ascii=False, indent=2),
        user_answers=user_answers_text,
        relevant_laws=relevant_laws,
        reference_formulations=reference_formulations
    )

    try:
        # Создаём функцию для запроса
        async def make_phase_b_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=CCW_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.5,
                    max_output_tokens=4096
                )
            )

        response = await execute_vertex_request(
            func=make_phase_b_request,
            model_name=CCW_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        draft_content = response.text

        # Сохраняем черновик
        await manager.save_part_version(part_type, draft_content)

        context.user_data['ccw_current_draft'] = draft_content
        context.user_data['ccw_phase'] = 'B'

        # Переходим к фазе C (самопроверка)
        return await run_phase_c(update, context)

    except Exception as e:
        logging.error(f"[CCW] Ошибка в фазе B: {e}")
        error_text = "❌ Произошла ошибка при генерации. Попробуйте ещё раз."
        if update.callback_query:
            await safe_edit_message_text(update.callback_query, error_text)
        else:
            await update.effective_chat.send_message(error_text)
        return CCW_PHASE_A


async def run_phase_c(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Фаза C: Самопроверка AI.
    """
    session_id = context.user_data.get('ccw_session_id')
    part_index = context.user_data.get('ccw_current_part_index', 0)
    case_number = context.user_data.get('ccw_case_number', 'DJR-XXX')
    draft_content = context.user_data.get('ccw_current_draft', '')

    manager = CriminalCaseManager(session_id)
    collected_data = await manager.get_collected_data()

    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]
    part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)

    # Получаем релевантные законы
    server_id = context.user_data.get('ccw_server_id', 'majestic_1')
    relevant_laws = await get_relevant_laws_for_ccw(server_id, collected_data.get('initial_facts', ''))

    # Формируем промпт для самопроверки
    prompt = get_ccw_self_review_prompt(
        part_name=part_name,
        case_number=case_number,
        draft_content=draft_content,
        collected_data=json.dumps(collected_data, ensure_ascii=False, indent=2),
        relevant_laws=relevant_laws
    )

    try:
        # Создаём функцию для запроса
        async def make_phase_c_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=CCW_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=1024
                )
            )

        response = await execute_vertex_request(
            func=make_phase_c_request,
            model_name=CCW_MODEL_NAME,
            priority=RequestPriority.LOW,
            max_retries=2
        )

        review_result = response.text.strip()

        # Определяем статус проверки
        is_approved = '✅' in review_result and 'ПРОВЕРКА ПРОЙДЕНА' in review_result

        if is_approved:
            ai_review = "✅ Соответствует стандартам"
        else:
            ai_review = sanitize_telegram_html(review_result[:500])

        # Обновляем версию с результатами проверки
        await manager.save_part_version(part_type, draft_content, ai_review)

        # Формируем сообщение с карусельным UI
        message_text = format_part_message(
            case_number=case_number,
            part_index=part_index,
            part_content=sanitize_telegram_html(draft_content[:3000]),
            ai_review=ai_review
        )

        keyboard = build_carousel_keyboard(part_index, session_id)

        # Отправляем результат
        if update.callback_query:
            await safe_edit_message_text(
                update.callback_query,
                message_text,
                parse_mode='HTML',
                reply_markup=keyboard
            )
        else:
            await update.effective_chat.send_message(
                message_text,
                parse_mode='HTML',
                reply_markup=keyboard
            )

        context.user_data['ccw_phase'] = 'C'
        return CCW_USER_REVIEW

    except Exception as e:
        logging.error(f"[CCW] Ошибка в фазе C: {e}")
        # Показываем черновик без проверки
        message_text = format_part_message(
            case_number=case_number,
            part_index=part_index,
            part_content=sanitize_telegram_html(draft_content[:3000]),
            ai_review="⚠️ Автопроверка недоступна"
        )

        keyboard = build_carousel_keyboard(part_index, session_id)

        if update.callback_query:
            await safe_edit_message_text(
                update.callback_query,
                message_text,
                parse_mode='HTML',
                reply_markup=keyboard
            )
        else:
            await update.effective_chat.send_message(
                message_text,
                parse_mode='HTML',
                reply_markup=keyboard
            )

        return CCW_USER_REVIEW


async def handle_accept_part(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик принятия части дела."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = context.user_data.get('ccw_session_id')
    part_index = context.user_data.get('ccw_current_part_index', 0)

    manager = CriminalCaseManager(session_id)
    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]

    # Помечаем часть как финальную
    await manager.mark_part_final(part_type)

    # Переходим к следующей части
    if part_index < len(CRIMINAL_CASE_PARTS_ORDER) - 1:
        context.user_data['ccw_current_part_index'] = part_index + 1

        await safe_edit_message_text(
            query,
            f"✅ Часть <b>{CRIMINAL_CASE_PARTS_DISPLAY.get(part_type)}</b> принята!\n\n"
            f"⏳ Переходим к следующей части...",
            parse_mode='HTML'
        )

        return await run_phase_a(update, context)
    else:
        # Все части готовы - предлагаем экспорт
        return await show_export_preview(update, context)


async def handle_refine_part(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик уточнения части (добавление комментария)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    part_index = context.user_data.get('ccw_current_part_index', 0)
    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]
    part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)

    await safe_edit_message_text(
        query,
        f"✏️ <b>Уточнение части: {part_name}</b>\n\n"
        "Напишите, что нужно изменить или добавить.\n"
        "AI сгенерирует новую версию с учётом ваших пожеланий.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"ccw_show_current_{context.user_data.get('ccw_session_id')}")]
        ])
    )

    context.user_data['ccw_refining'] = True
    return CCW_PHASE_B


async def handle_refinement_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод уточнений для части."""
    refinement_text = update.message.text.strip()
    session_id = context.user_data.get('ccw_session_id')
    part_index = context.user_data.get('ccw_current_part_index', 0)

    manager = CriminalCaseManager(session_id)
    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]

    # Добавляем уточнение в ответы
    collected_data = await manager.get_collected_data()
    if 'refinements' not in collected_data:
        collected_data['refinements'] = {}
    if part_type not in collected_data['refinements']:
        collected_data['refinements'][part_type] = []

    collected_data['refinements'][part_type].append(refinement_text)
    await manager.update_collected_data(collected_data)

    context.user_data['ccw_refining'] = False

    await update.message.reply_text(
        "⏳ <b>Генерирую новую версию с учётом уточнений...</b>",
        parse_mode='HTML'
    )

    return await run_phase_b(update, context)


async def handle_regenerate_part(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик полной пересоздания части (с фазы A)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    await safe_edit_message_text(
        query,
        "🔄 <b>Пересоздаю часть с начала...</b>",
        parse_mode='HTML'
    )

    return await run_phase_a(update, context)


async def handle_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик навигации между частями."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    data = query.data
    session_id = context.user_data.get('ccw_session_id')

    if 'prev' in data:
        context.user_data['ccw_current_part_index'] = max(0, context.user_data.get('ccw_current_part_index', 0) - 1)
    elif 'next' in data:
        context.user_data['ccw_current_part_index'] = min(
            len(CRIMINAL_CASE_PARTS_ORDER) - 1,
            context.user_data.get('ccw_current_part_index', 0) + 1
        )

    # Показываем текущую часть
    return await show_current_part(update, context)


async def show_current_part(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает текущую часть дела."""
    session_id = context.user_data.get('ccw_session_id')
    part_index = context.user_data.get('ccw_current_part_index', 0)
    case_number = context.user_data.get('ccw_case_number', 'DJR-XXX')

    manager = CriminalCaseManager(session_id)
    part_type = CRIMINAL_CASE_PARTS_ORDER[part_index]

    # Получаем последнюю версию части
    content = await manager.get_latest_part_content(part_type)

    if content:
        message_text = format_part_message(
            case_number=case_number,
            part_index=part_index,
            part_content=sanitize_telegram_html(content[:3000])
        )
        keyboard = build_carousel_keyboard(part_index, session_id)
    else:
        # Часть ещё не сгенерирована
        part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)
        message_text = f"📋 <b>Часть: {part_name}</b>\n\nЭта часть ещё не сгенерирована."
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ Сгенерировать", callback_data=f"ccw_generate_{session_id}")],
            [InlineKeyboardButton("◀️ Назад", callback_data=f"ccw_prev_{session_id}")]
        ])

    query = update.callback_query
    if query:
        await safe_edit_message_text(
            query,
            message_text,
            parse_mode='HTML',
            reply_markup=keyboard
        )

    return CCW_USER_REVIEW


async def show_export_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает превью перед экспортом."""
    session_id = context.user_data.get('ccw_session_id')
    case_number = context.user_data.get('ccw_case_number', 'DJR-XXX')

    manager = CriminalCaseManager(session_id)
    final_parts = await manager.get_final_parts()

    # Считаем готовые части
    ready_count = len(final_parts)
    total_count = len(CRIMINAL_CASE_PARTS_ORDER)

    parts_status = []
    for part_type in CRIMINAL_CASE_PARTS_ORDER:
        part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)
        if part_type in final_parts:
            parts_status.append(f"✅ {part_name}")
        else:
            parts_status.append(f"⬜ {part_name}")

    message_text = f"""🏁 <b>Экспорт уголовного дела {case_number}</b>

<b>Готовность:</b> {ready_count}/{total_count} частей

<b>Статус частей:</b>
{chr(10).join(parts_status)}

{"⚠️ Не все части готовы! Продолжить экспорт?" if ready_count < total_count else "✅ Все части готовы к экспорту!"}"""

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Создать Google Doc", callback_data=f"ccw_do_export_{session_id}")],
        [InlineKeyboardButton("◀️ Вернуться к редактированию", callback_data=f"ccw_show_current_{session_id}")]
    ])

    query = update.callback_query
    if query:
        await safe_edit_message_text(query, message_text, parse_mode='HTML', reply_markup=keyboard)
    else:
        await update.effective_chat.send_message(message_text, parse_mode='HTML', reply_markup=keyboard)

    return CCW_EXPORT


async def handle_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Выполняет экспорт в Google Doc."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = context.user_data.get('ccw_session_id')
    case_number = context.user_data.get('ccw_case_number', 'DJR-XXX')

    await safe_edit_message_text(
        query,
        "⏳ <b>Создаю документ Google Doc...</b>\n"
        "Это может занять несколько секунд.",
        parse_mode='HTML'
    )

    try:
        from google_docs_exporter import create_criminal_case_document, fill_criminal_case_document

        manager = CriminalCaseManager(session_id)
        final_parts = await manager.get_final_parts()
        collected_data = await manager.get_collected_data()

        # Получаем структуру шаблона из AI-анализа (кэшируется)
        template_structure = await get_or_analyze_template_structure()

        # Создаём документ (копируем шаблон)
        doc_id, doc_url = await create_criminal_case_document(case_number)

        # Заполняем документ сгенерированным контентом
        fill_criminal_case_document(
            doc_id=doc_id,
            case_number=case_number,
            collected_data=collected_data,
            final_parts=final_parts,
            template_structure=template_structure
        )

        logging.info(f"[CCW] Документ {doc_id} заполнен контентом дела {case_number}")



        # Сохраняем информацию о документе
        await manager.update_session(
            google_doc_id=doc_id,
            google_doc_url=doc_url,
            status='completed'
        )

        await safe_edit_message_text(
            query,
            f"✅ <b>Документ успешно создан!</b>\n\n"
            f"📋 Уголовное дело: <b>{case_number}</b>\n"
            f"🔗 <a href=\"{doc_url}\">Открыть документ</a>\n\n"
            f"<i>Документ доступен по ссылке для всех с правами на просмотр.</i>\n\n"
            f"⚠️ <b>Важно:</b> Проверьте документ и при необходимости отредактируйте в Google Docs.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Открыть документ", url=doc_url)],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )

        return ConversationHandler.END

    except Exception as e:
        logging.error(f"[CCW] Ошибка экспорта: {e}")
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка при создании документа</b>\n\n"
            f"Попробуйте позже или обратитесь к администратору.\n"
            f"Ваши данные сохранены.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"ccw_do_export_{session_id}")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return CCW_EXPORT


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик отмены создания дела."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = context.user_data.get('ccw_session_id')

    if session_id:
        manager = CriminalCaseManager(session_id)
        await manager.close_session('cancelled')

        # Удаляем запланированное автозакрытие
        remove_ccw_scheduled_job(context, session_id)

    # Очищаем данные
    for key in list(context.user_data.keys()):
        if key.startswith('ccw_'):
            del context.user_data[key]

    # Показываем главное меню
    await _display_main_menu(update, context)

    return ConversationHandler.END


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

async def get_relevant_laws_for_ccw(server_id: str = None, facts: str = "") -> str:
    """Получает релевантные законы для создания дела."""
    try:
        # Получаем законы из любого сервера (или из указанного если есть)
        if server_id:
            query = """
                SELECT title, content FROM law_documents
                WHERE server_id = $1 AND content IS NOT NULL
                LIMIT 10
            """
            laws = await db_manager.fetch(query, server_id)
        else:
            # Получаем законы из любого сервера (УК и ПК приоритетно)
            query = """
                SELECT title, content FROM law_documents
                WHERE content IS NOT NULL
                AND (title ILIKE '%уголов%' OR title ILIKE '%процессуальн%' OR title ILIKE '%УК%' OR title ILIKE '%ПК%')
                LIMIT 10
            """
            laws = await db_manager.fetch(query)

        if not laws:
            # Пробуем получить любые законы
            query = """
                SELECT title, content FROM law_documents
                WHERE content IS NOT NULL
                LIMIT 10
            """
            laws = await db_manager.fetch(query)

        if not laws:
            return "Законы не найдены в базе. Генерация будет выполнена на основе общих юридических знаний."

        # Форматируем законы для промпта
        laws_text = []
        for law in laws:
            title = law.get('title', 'Без названия')
            content = law.get('content', '')[:1500]  # Ограничиваем размер каждого закона
            laws_text.append(f"### {title}\n{content}")

        return '\n\n---\n\n'.join(laws_text)[:8000]  # Общий лимит
    except Exception as e:
        logging.warning(f"[CCW] Ошибка получения законов: {e}")
        return "Законы временно недоступны. Генерация будет выполнена на основе общих юридических знаний."


async def get_reference_formulations(part_type: str) -> str:
    """Получает референсные формулировки из реальных дел."""
    # Загружаем из файлов шаблонов
    import os
    from config import BASE_DIR

    templates_dir = os.path.join(BASE_DIR, 'data', 'criminal_case_templates')
    reference_parts = []

    # Читаем DJR-015 как основной референс
    djr015_path = os.path.join(templates_dir, 'DJR==015.md')
    if os.path.exists(djr015_path):
        try:
            with open(djr015_path, 'r', encoding='utf-8') as f:
                content = f.read()
                # Извлекаем релевантную часть
                if part_type == 'indictment' and 'ОБВИНИТЕЛЬНОЕ ЗАКЛЮЧЕНИЕ' in content:
                    start = content.find('ОБВИНИТЕЛЬНОЕ ЗАКЛЮЧЕНИЕ')
                    end = content.find('Список подлежащих вызову', start)
                    if end == -1:
                        end = start + 3000
                    reference_parts.append(content[start:end])
                elif part_type == 'prosecution_demands' and 'Требования прокуратуры' in content:
                    start = content.find('Требования прокуратуры')
                    reference_parts.append(content[start:start + 1500])
        except Exception as e:
            logging.warning(f"[CCW] Ошибка чтения референса: {e}")

    return '\n---\n'.join(reference_parts) if reference_parts else "Референсы недоступны"


# === АВТОЗАКРЫТИЕ СЕССИЙ ===

def schedule_ccw_session_closure(context: ContextTypes.DEFAULT_TYPE, session_id: int, user_id: int) -> None:
    """Планирует автозакрытие сессии."""
    job_name = f"ccw_close_{session_id}"

    # Удаляем существующую задачу если есть
    remove_ccw_scheduled_job(context, session_id)

    # Планируем новую
    context.job_queue.run_once(
        ccw_auto_close_job,
        when=timedelta(hours=CCW_SESSION_TIMEOUT_HOURS),
        data={'session_id': session_id, 'user_id': user_id},
        name=job_name
    )
    logging.info(f"[CCW] Запланировано автозакрытие сессии #{session_id} через {CCW_SESSION_TIMEOUT_HOURS}ч")


def remove_ccw_scheduled_job(context: ContextTypes.DEFAULT_TYPE, session_id: int) -> None:
    """Удаляет запланированную задачу автозакрытия."""
    job_name = f"ccw_close_{session_id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in jobs:
        job.schedule_removal()


async def ccw_auto_close_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Задача автозакрытия сессии."""
    job = context.job
    session_id = job.data['session_id']
    user_id = job.data['user_id']

    logging.info(f"[CCW] Автозакрытие сессии #{session_id}")

    manager = CriminalCaseManager(session_id)
    session = await manager.load_session_data()

    if session and session.get('status') == 'active':
        await manager.close_session('expired')

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"⏰ Сессия создания уголовного дела #{session_id} была автоматически закрыта "
                     f"по истечении {CCW_SESSION_TIMEOUT_HOURS} часов.\n\n"
                     "Вы можете начать новую сессию из меню.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
                ])
            )
        except Exception as e:
            logging.warning(f"[CCW] Не удалось уведомить пользователя {user_id}: {e}")


# === CONVERSATION HANDLER ===

def get_criminal_case_conv_handler() -> ConversationHandler:
    """Создаёт ConversationHandler для Criminal Case Writer."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_criminal_case_writer, pattern=r'^start_criminal_case$'),
            CallbackQueryHandler(start_criminal_case_writer, pattern=r'^tool_pro_create_criminal_case$'),
            # Обработчики для продолжения/перезапуска сессии (вызываются из CCW_NAVIGATE)
            CallbackQueryHandler(handle_continue_session, pattern=r'^ccw_continue_\d+$'),
            CallbackQueryHandler(handle_restart_session, pattern=r'^ccw_restart$'),
        ],
        states={
            CCW_CHOOSING_PROJECT: [
                CallbackQueryHandler(select_ccw_project, pattern=r'^ccw_project_'),
                CallbackQueryHandler(start_criminal_case_writer, pattern=r'^start_criminal_case$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_CHOOSING_SERVER: [
                CallbackQueryHandler(select_ccw_server, pattern=r'^ccw_server_'),
                CallbackQueryHandler(start_criminal_case_writer, pattern=r'^start_criminal_case$'),
                CallbackQueryHandler(select_ccw_project, pattern=r'^ccw_project_'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_ENTER_CASE_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_case_number_input),
                CallbackQueryHandler(select_ccw_project, pattern=r'^ccw_project_'),
                CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_COLLECT_FACTS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_facts_input),
                CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_PHASE_A: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phase_a_answer),
                CallbackQueryHandler(skip_questions_callback, pattern=r'^ccw_skip_questions_'),
                CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_PHASE_B: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_refinement_input),
                CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_USER_REVIEW: [
                CallbackQueryHandler(handle_accept_part, pattern=r'^ccw_accept_'),
                CallbackQueryHandler(handle_refine_part, pattern=r'^ccw_refine_'),
                CallbackQueryHandler(handle_regenerate_part, pattern=r'^ccw_regenerate_'),
                CallbackQueryHandler(handle_navigation, pattern=r'^ccw_(prev|next)_'),
                CallbackQueryHandler(show_current_part, pattern=r'^ccw_show_current_'),
                CallbackQueryHandler(show_export_preview, pattern=r'^ccw_export_'),
                CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_NAVIGATE: [
                CallbackQueryHandler(handle_continue_session, pattern=r'^ccw_continue_\d+$'),
                CallbackQueryHandler(handle_restart_session, pattern=r'^ccw_restart$'),
                CallbackQueryHandler(create_new_case, pattern=r'^ccw_create_new$'),
                CallbackQueryHandler(show_ccw_history_list, pattern=r'^ccw_history_list$'),
                CallbackQueryHandler(view_completed_case, pattern=r'^ccw_view_case_\d+$'),
                CallbackQueryHandler(recreate_document, pattern=r'^ccw_recreate_doc_\d+$'),
                CallbackQueryHandler(start_criminal_case_writer, pattern=r'^start_criminal_case$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            CCW_EXPORT: [
                CallbackQueryHandler(handle_export, pattern=r'^ccw_do_export_'),
                CallbackQueryHandler(show_current_part, pattern=r'^ccw_show_current_'),
                CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(handle_cancel, pattern=r'^ccw_cancel'),
            CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
        ],
        name="criminal_case_writer",
        persistent=False,
        allow_reentry=True,
    )


# Экспортируем handler
criminal_case_conv_handler = get_criminal_case_conv_handler()


# END OF FILE user_handlers/criminal_case_flow.py

