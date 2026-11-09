# START OF FILE user_handlers/prosecution_complaint_flow.py

"""
Модуль для системы разбора жалоб в прокуратуру (Prosecution Complaint Handler).

Содержит:
- ProsecutionComplaintManager: класс для управления сессией и 4-фазным циклом
- ConversationHandler для пошаговой обработки жалобы
- Карусельный UI для навигации между этапами
- Генерация документов в административно-канцелярском стиле
- Автозакрытие сессий через 72 часа с уведомлением за 24ч
- Интеграция с Criminal Case Writer для конвертации в уголовное дело
"""

import json
import logging
import re
import io
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

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
    PCH_CHOOSING_PROJECT, PCH_CHOOSING_SERVER, PCH_AWAITING_URL,
    PCH_AWAITING_FORM, PCH_PHASE_A, PCH_USER_REVIEW, PCH_NAVIGATE,
    PCH_ADD_MATERIAL, PCH_EXPORT,
    PCH_MODEL_NAME, PCH_SESSION_TIMEOUT_HOURS, PCH_WARNING_BEFORE_HOURS,
    PCH_STAGES_ORDER, PCH_STAGES_DISPLAY,
    PCH_DOCUMENT_TYPES, PCH_STAGE_AVAILABLE_DOCS,
    PROJECTS_CONFIG, SERVERS_CONFIG
)
from database.manager import db_manager
from utils import (
    sanitize_telegram_html, safe_answer_callback_query,
    get_enabled_servers, get_enabled_projects, send_project_selection_menu
)
from prompts import (
    get_pch_intake_prompt, get_pch_planning_prompt,
    get_pch_clarification_prompt, get_pch_finalization_prompt,
    get_pch_document_prompt
)
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client
from remote_parser_client import remote_parse_topic, check_remote_parser_available
from .core import check_and_update_usage, _display_main_menu, unknown_text_fallback


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

async def send_debug_document(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    content: str,
    filename: str,
    caption: str = None
) -> None:
    """
    Отправляет текстовый документ для дебага.

    Args:
        context: Контекст бота
        chat_id: ID чата
        content: Содержимое документа
        filename: Имя файла (например, 'form_parsed.txt')
        caption: Подпись к документу
    """
    try:
        # Создаём файл в памяти
        file_bytes = io.BytesIO(content.encode('utf-8'))
        file_bytes.name = filename

        await context.bot.send_document(
            chat_id=chat_id,
            document=file_bytes,
            filename=filename,
            caption=caption or f"🔍 Debug: {filename}"
        )
        logging.info(f"[PCH] Отправлен дебаг-документ: {filename}")
    except Exception as e:
        logging.warning(f"[PCH] Не удалось отправить дебаг-документ: {e}")


async def safe_edit_message_text(query, text: str, **kwargs):
    """
    Безопасно редактирует сообщение, игнорируя ошибку 'Message is not modified'.
    """
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logging.debug(f"[PCH] Message not modified (ignored): {e}")
            return None
        raise


def generate_complaint_number() -> str:
    """Генерирует уникальный номер жалобы в формате PCH-YYYY-XXXX."""
    year = datetime.now().year
    # Получаем следующий номер из последовательности
    # Формат: PCH-2026-0001, PCH-2026-0002, ...
    return f"PCH-{year}-{{seq:04d}}"


# === КОНСТАНТЫ ===

STAGES_PER_PAGE = 4  # Все этапы помещаются на одной странице


# === КЛАСС МЕНЕДЖЕРА СЕССИИ ===

class ProsecutionComplaintManager:
    """Управляет сессией обработки жалобы в прокуратуру."""

    def __init__(self, session_id: int):
        self.session_id = session_id
        self._session_data = None

    @classmethod
    async def create_session(
        cls,
        user_id: int,
        server_id: str,
        complaint_url: str
    ) -> 'ProsecutionComplaintManager':
        """Создаёт новую сессию обработки жалобы."""
        # Генерируем номер жалобы
        year = datetime.now().year

        # Получаем следующий номер для этого года
        count_query = """
            SELECT COUNT(*) + 1 FROM prosecution_complaint_sessions
            WHERE EXTRACT(YEAR FROM created_at) = $1
        """
        seq_num = await db_manager.fetchval(count_query, year) or 1
        complaint_number = f"PCH-{year}-{seq_num:04d}"

        query = """
            INSERT INTO prosecution_complaint_sessions 
            (user_id, server_id, complaint_url, complaint_number, collected_data, status)
            VALUES ($1, $2, $3, $4, '{}', 'active')
            RETURNING id
        """
        session_id = await db_manager.fetchval(
            query, user_id, server_id, complaint_url, complaint_number
        )
        logging.info(
            f"[PCH] Создана сессия #{session_id} ({complaint_number}) "
            f"для пользователя {user_id}, сервер {server_id}"
        )
        return cls(session_id)

    @classmethod
    async def get_active_session(cls, user_id: int) -> Optional['ProsecutionComplaintManager']:
        """Получает активную сессию пользователя."""
        query = """
            SELECT id FROM prosecution_complaint_sessions
            WHERE user_id = $1 AND status = 'active'
            ORDER BY created_at DESC LIMIT 1
        """
        session_id = await db_manager.fetchval(query, user_id)
        if session_id:
            return cls(session_id)
        return None

    @classmethod
    async def get_user_sessions(
        cls,
        user_id: int,
        limit: int = 10,
        include_statuses: List[str] = None
    ) -> List[Dict]:
        """Получает список сессий пользователя."""
        if include_statuses is None:
            include_statuses = ['active', 'completed', 'converted_to_case', 'expired']

        placeholders = ', '.join(f"${i+2}" for i in range(len(include_statuses)))
        query = f"""
            SELECT id, complaint_number, complaint_url, server_id, 
                   current_stage_index, status, linked_criminal_case_id,
                   created_at, updated_at
            FROM prosecution_complaint_sessions
            WHERE user_id = $1 AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT ${len(include_statuses) + 2}
        """
        rows = await db_manager.fetch(query, user_id, *include_statuses, limit)
        return [dict(row) for row in rows]

    async def load_session_data(self) -> Dict:
        """Загружает данные сессии из БД."""
        if self._session_data is None:
            query = """
                SELECT * FROM prosecution_complaint_sessions WHERE id = $1
            """
            row = await db_manager.fetchrow(query, self.session_id)
            self._session_data = dict(row) if row else {}
        return self._session_data

    async def update_session(self, **kwargs) -> None:
        """Обновляет поля сессии."""
        set_clauses = []
        values = []
        idx = 1

        for key, value in kwargs.items():
            set_clauses.append(f"{key} = ${idx}")
            # Сериализуем dict/list в JSON
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            values.append(value)
            idx += 1

        values.append(self.session_id)
        query = f"""
            UPDATE prosecution_complaint_sessions
            SET {', '.join(set_clauses)}, updated_at = NOW()
            WHERE id = ${idx}
        """
        await db_manager.execute(query, *values)
        self._session_data = None  # Сбрасываем кэш

    async def get_collected_data(self) -> Dict:
        """Получает собранные материалы дела."""
        session = await self.load_session_data()
        data = session.get('collected_data', {})
        if isinstance(data, str):
            return json.loads(data)
        return data or {}

    async def update_collected_data(self, new_data: Dict) -> None:
        """Обновляет собранные материалы."""
        current = await self.get_collected_data()
        current.update(new_data)
        await self.update_session(collected_data=current)

    async def get_parsed_complaint(self) -> Optional[Dict]:
        """Получает распарсенную жалобу."""
        session = await self.load_session_data()
        data = session.get('parsed_complaint')
        if isinstance(data, str):
            return json.loads(data)
        return data

    async def get_ai_recommendations(self) -> Optional[Dict]:
        """Получает рекомендации AI."""
        session = await self.load_session_data()
        data = session.get('ai_recommendations')
        if isinstance(data, str):
            return json.loads(data)
        return data

    async def get_form_compliance_status(self) -> Optional[Dict]:
        """Получает результаты скрининга формы подачи."""
        session = await self.load_session_data()
        data = session.get('form_compliance_status')
        if isinstance(data, str):
            return json.loads(data)
        return data

    async def save_stage_content(
        self,
        stage_type: str,
        content: str,
        ai_review_notes: str = None,
        is_final: bool = False
    ) -> int:
        """Сохраняет версию этапа обработки."""
        # Получаем текущую максимальную версию
        query = """
            SELECT COALESCE(MAX(version), 0) FROM prosecution_complaint_stages
            WHERE session_id = $1 AND stage_type = $2
        """
        max_version = await db_manager.fetchval(query, self.session_id, stage_type)
        new_version = max_version + 1

        # Вставляем новую версию
        insert_query = """
            INSERT INTO prosecution_complaint_stages 
            (session_id, stage_type, version, content, ai_review_notes, is_final)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
        """
        stage_id = await db_manager.fetchval(
            insert_query,
            self.session_id, stage_type, new_version, content, ai_review_notes, is_final
        )

        logging.info(
            f"[PCH] Сохранён этап {stage_type} v{new_version} для сессии #{self.session_id}"
        )
        return stage_id

    async def get_latest_stage_content(self, stage_type: str) -> Optional[str]:
        """Получает последнюю версию этапа."""
        query = """
            SELECT content FROM prosecution_complaint_stages
            WHERE session_id = $1 AND stage_type = $2
            ORDER BY version DESC LIMIT 1
        """
        return await db_manager.fetchval(query, self.session_id, stage_type)

    async def get_stage_with_notes(self, stage_type: str) -> Optional[Dict]:
        """Получает последнюю версию этапа с заметками AI."""
        query = """
            SELECT content, ai_review_notes, version, is_final, created_at
            FROM prosecution_complaint_stages
            WHERE session_id = $1 AND stage_type = $2
            ORDER BY version DESC LIMIT 1
        """
        row = await db_manager.fetchrow(query, self.session_id, stage_type)
        return dict(row) if row else None

    async def get_all_stages(self) -> Dict[str, Dict]:
        """Получает все финальные этапы."""
        query = """
            SELECT DISTINCT ON (stage_type) 
                stage_type, content, ai_review_notes, version, is_final
            FROM prosecution_complaint_stages
            WHERE session_id = $1
            ORDER BY stage_type, version DESC
        """
        rows = await db_manager.fetch(query, self.session_id)
        return {row['stage_type']: dict(row) for row in rows}

    async def mark_stage_final(self, stage_type: str) -> None:
        """Помечает последнюю версию этапа как финальную."""
        # Снимаем флаг is_final со всех версий этого этапа
        await db_manager.execute(
            "UPDATE prosecution_complaint_stages SET is_final = FALSE "
            "WHERE session_id = $1 AND stage_type = $2",
            self.session_id, stage_type
        )
        # Ставим флаг на последнюю версию
        await db_manager.execute("""
            UPDATE prosecution_complaint_stages SET is_final = TRUE
            WHERE id = (
                SELECT id FROM prosecution_complaint_stages
                WHERE session_id = $1 AND stage_type = $2
                ORDER BY version DESC LIMIT 1
            )
        """, self.session_id, stage_type)

    async def add_generated_document(
        self,
        doc_type: str,
        content: str,
        metadata: Dict = None
    ) -> None:
        """Добавляет сгенерированный документ в список."""
        session = await self.load_session_data()
        docs = session.get('generated_documents', [])
        if isinstance(docs, str):
            docs = json.loads(docs)

        doc_entry = {
            'type': doc_type,
            'type_display': PCH_DOCUMENT_TYPES.get(doc_type, doc_type),
            'content': content,
            'created_at': datetime.now().isoformat(),
            'metadata': metadata or {}
        }
        docs.append(doc_entry)

        await self.update_session(generated_documents=docs)
        logging.info(f"[PCH] Добавлен документ '{doc_type}' для сессии #{self.session_id}")

    async def get_generated_documents(self) -> List[Dict]:
        """Получает список всех сгенерированных документов."""
        session = await self.load_session_data()
        docs = session.get('generated_documents', [])
        if isinstance(docs, str):
            return json.loads(docs)
        return docs or []

    async def close_session(self, status: str = 'completed') -> None:
        """Закрывает сессию."""
        await self.update_session(status=status)
        logging.info(f"[PCH] Сессия #{self.session_id} закрыта со статусом '{status}'")

    async def link_criminal_case(self, criminal_case_id: int) -> None:
        """Связывает сессию с уголовным делом."""
        await self.update_session(
            linked_criminal_case_id=criminal_case_id,
            status='converted_to_case'
        )
        logging.info(
            f"[PCH] Сессия #{self.session_id} связана с уголовным делом #{criminal_case_id}"
        )


# === ФУНКЦИИ ДЛЯ РАБОТЫ С ФОРМАМИ ПОДАЧИ ===

async def get_form_template(user_id: int, server_id: str) -> Optional[str]:
    """Получает кэшированную форму подачи жалобы для пользователя и сервера."""
    query = """
        SELECT form_content FROM prosecution_form_templates
        WHERE user_id = $1 AND server_id = $2
    """
    return await db_manager.fetchval(query, user_id, server_id)


async def save_form_template(user_id: int, server_id: str, content: str) -> None:
    """Сохраняет форму подачи жалобы (upsert)."""
    query = """
        INSERT INTO prosecution_form_templates (user_id, server_id, form_content)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, server_id) 
        DO UPDATE SET form_content = $3, updated_at = NOW()
    """
    await db_manager.execute(query, user_id, server_id, content)
    logging.info(f"[PCH] Сохранена форма подачи для user={user_id}, server={server_id}")


async def delete_form_template(user_id: int, server_id: str) -> None:
    """Удаляет кэшированную форму подачи."""
    query = """
        DELETE FROM prosecution_form_templates
        WHERE user_id = $1 AND server_id = $2
    """
    await db_manager.execute(query, user_id, server_id)


# === UI ФУНКЦИИ ===

def build_progress_bar(current: int, total: int) -> str:
    """Создаёт текстовый прогресс-бар."""
    filled = current
    empty = total - current
    return '■' * filled + '□' * empty


def build_stage_keyboard(
    current_stage_index: int,
    session_id: int,
    show_accept: bool = True,
    show_doc_button: bool = True,
    available_docs: List[str] = None
) -> InlineKeyboardMarkup:
    """
    Создаёт клавиатуру карусельного UI для этапов.
    """
    keyboard = []

    # Ряд навигации между этапами
    nav_row = []
    if current_stage_index > 0:
        nav_row.append(InlineKeyboardButton(
            "◀️ Пред.", callback_data=f"pch_prev_{session_id}"
        ))

    stage_type = PCH_STAGES_ORDER[current_stage_index]
    stage_name = PCH_STAGES_DISPLAY.get(stage_type, f"Этап {current_stage_index + 1}")
    nav_row.append(InlineKeyboardButton(
        f"{current_stage_index + 1}/{len(PCH_STAGES_ORDER)}",
        callback_data="pch_noop"
    ))

    if current_stage_index < len(PCH_STAGES_ORDER) - 1:
        nav_row.append(InlineKeyboardButton(
            "След. ▶️", callback_data=f"pch_next_{session_id}"
        ))

    keyboard.append(nav_row)

    # Ряд действий
    if show_accept:
        keyboard.append([
            InlineKeyboardButton("✅ Принять этап", callback_data=f"pch_accept_{session_id}"),
        ])
        keyboard.append([
            InlineKeyboardButton("✏️ Уточнить", callback_data=f"pch_refine_{session_id}"),
            InlineKeyboardButton("🔄 Пересоздать", callback_data=f"pch_regenerate_{session_id}"),
        ])

    # Кнопка генерации документа (если доступны документы на этом этапе)
    if show_doc_button and available_docs:
        keyboard.append([
            InlineKeyboardButton(
                "📄 Создать документ", callback_data=f"pch_gen_doc_{session_id}"
            )
        ])

    # Кнопка добавления материалов
    keyboard.append([
        InlineKeyboardButton(
            "📎 Добавить материал", callback_data=f"pch_add_material_{session_id}"
        )
    ])

    # На последнем этапе - кнопки завершения
    if current_stage_index == len(PCH_STAGES_ORDER) - 1:
        keyboard.append([
            InlineKeyboardButton(
                "🔄 Конвертировать в уголовное дело",
                callback_data=f"pch_convert_ccw_{session_id}"
            )
        ])

    # Кнопки отмены/выхода
    keyboard.append([
        InlineKeyboardButton(
            "📜 История жалоб", callback_data="pch_history"
        ),
        InlineKeyboardButton(
            "❌ Отменить", callback_data=f"pch_cancel_{session_id}"
        ),
    ])

    return InlineKeyboardMarkup(keyboard)


def format_stage_message(
    complaint_number: str,
    stage_index: int,
    stage_content: str,
    ai_review: str = None
) -> str:
    """Форматирует сообщение для отображения этапа."""
    stage_type = PCH_STAGES_ORDER[stage_index]
    stage_name = PCH_STAGES_DISPLAY.get(stage_type, stage_type)
    progress = build_progress_bar(stage_index + 1, len(PCH_STAGES_ORDER))

    message = f"""══════════════════════════════════════════
📋 <b>ЖАЛОБА {complaint_number}</b>
[{progress}] Этап {stage_index + 1} из {len(PCH_STAGES_ORDER)} — {stage_name}
══════════════════════════════════════════

{stage_content}

──────────────────────────────────────────"""

    if ai_review:
        message += f"\n🤖 <b>AI-примечание:</b> {ai_review}\n──────────────────────────────────────────"

    return message


def format_session_status(session: Dict) -> str:
    """Форматирует статус сессии для отображения в списке."""
    status = session.get('status', 'unknown')
    stage_index = session.get('current_stage_index', 0)

    status_emoji = {
        'active': '⏳',
        'completed': '✅',
        'converted_to_case': '🔄',
        'expired': '❌'
    }.get(status, '❓')

    stage_name = ''
    if status == 'active' and stage_index < len(PCH_STAGES_ORDER):
        stage_type = PCH_STAGES_ORDER[stage_index]
        stage_name = f"\n   Этап: {PCH_STAGES_DISPLAY.get(stage_type, stage_type)}"

    linked_case = ''
    if status == 'converted_to_case' and session.get('linked_criminal_case_id'):
        linked_case = f"\n   → Уголовное дело #{session['linked_criminal_case_id']}"

    return f"{status_emoji} {status.replace('_', ' ').title()}{stage_name}{linked_case}"


# === ФУНКЦИИ ПОЛУЧЕНИЯ ЗАКОНОВ (RAG) ===

async def get_relevant_laws_for_complaint(
    server_id: str,
    complaint_text: str,
    stage_name: str = 'intake'
) -> str:
    """
    Получает релевантные законы для жалобы используя RAG-систему.

    Алгоритм:
    1. Загружает все законы сервера из БД
    2. Использует AI-роутер для выбора релевантных законов
    3. Возвращает текст выбранных законов

    Args:
        server_id: ID сервера
        complaint_text: Текст жалобы для анализа
        stage_name: Название этапа (intake, planning, clarification, finalization)

    Returns:
        Форматированный текст релевантных законов
    """
    from prompts import get_pch_law_router_prompt

    try:
        # Шаг 1: Загружаем все законы сервера
        law_records = await db_manager.fetch(
            """SELECT id, title, content FROM law_documents 
               WHERE server_id = $1 AND content IS NOT NULL 
               ORDER BY title""",
            server_id
        )

        if not law_records:
            logging.warning(f"[PCH RAG] Законы для сервера {server_id} не найдены в БД")
            return "⚠️ Законы не найдены в базе данных для данного сервера."

        # Создаём маппинг номер -> закон
        law_map_by_index = {
            i + 1: {
                'id': r['id'],
                'title': r['title'],
                'content': r['content']
            }
            for i, r in enumerate(law_records)
        }

        # Формируем список названий для роутера
        law_titles_str = "\n".join([
            f"{i + 1}. {r['title']}"
            for i, r in enumerate(law_records)
        ])

        logging.info(f"[PCH RAG] Загружено {len(law_records)} законов для сервера {server_id}")

        # Шаг 2: Используем AI-роутер для выбора релевантных законов
        router_prompt = get_pch_law_router_prompt(
            complaint_text=complaint_text,
            stage_name=stage_name,
            law_titles_str=law_titles_str
        )

        try:
            async def make_router_request():
                client = get_current_client()
                if not client:
                    raise RuntimeError("genai_client не инициализирован")
                return await client.aio.models.generate_content(
                    model=config.ROUTER_MODEL_NAME,  # Используем быструю модель для роутера
                    contents=router_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.1,  # Низкая температура для точности
                        max_output_tokens=200,
                    )
                )

            response = await execute_vertex_request(
                func=make_router_request,
                model_name=config.ROUTER_MODEL_NAME,
                priority=RequestPriority.NORMAL,
                max_retries=2
            )

            # Парсим ответ роутера
            selected_indices = set()
            for line in response.text.strip().split('\n'):
                line = line.strip()
                if line.isdigit():
                    idx = int(line)
                    if idx in law_map_by_index:
                        selected_indices.add(idx)

            logging.info(f"[PCH RAG] Роутер выбрал {len(selected_indices)} законов: {sorted(selected_indices)}")

            # Если роутер не выбрал ничего, используем fallback
            if not selected_indices:
                logging.warning("[PCH RAG] Роутер не выбрал законы, используем приоритетные")
                selected_indices = await _get_priority_law_indices(law_map_by_index)

        except Exception as e:
            logging.warning(f"[PCH RAG] Ошибка роутера: {e}, используем приоритетные законы")
            selected_indices = await _get_priority_law_indices(law_map_by_index)

        # Шаг 3: Формируем текст выбранных законов
        if not selected_indices:
            # Если совсем ничего не выбрано, берём первые 5 законов
            selected_indices = set(list(law_map_by_index.keys())[:5])

        laws_text = []
        total_length = 0
        max_total_length = 20000  # Лимит на общий размер законов

        for idx in sorted(selected_indices):
            law = law_map_by_index[idx]
            title = law['title']
            content = law['content']

            # Ограничиваем размер каждого закона
            if len(content) > 6000:
                content = content[:6000] + "\n\n[... текст сокращён ...]"

            law_block = f"\n\n{'='*50}\n📜 {title}\n{'='*50}\n\n{content}"

            # Проверяем общий лимит
            if total_length + len(law_block) > max_total_length:
                laws_text.append("\n\n⚠️ [Остальные законы опущены из-за ограничения размера]")
                break

            laws_text.append(law_block)
            total_length += len(law_block)

        result = ''.join(laws_text)
        logging.info(f"[PCH RAG] Сформирован текст законов: {len(result)} символов, {len(selected_indices)} документов")

        return result

    except Exception as e:
        logging.error(f"[PCH RAG] Критическая ошибка: {e}", exc_info=True)
        return "❌ Ошибка загрузки законов из базы данных."


async def _get_priority_law_indices(law_map: dict) -> set:
    """
    Возвращает индексы приоритетных законов (УК, УПК, процессуальные).
    Используется как fallback когда роутер не работает.
    """
    priority_keywords = [
        'уголов', 'процессуальн', 'УК', 'УПК', 'прокурор',
        'надзор', 'полиц', 'должност', 'административ'
    ]

    selected = set()
    for idx, law in law_map.items():
        title_lower = law['title'].lower()
        for keyword in priority_keywords:
            if keyword.lower() in title_lower:
                selected.add(idx)
                break
        if len(selected) >= 7:  # Максимум 7 приоритетных
            break

    return selected


# === ЧАСТЬ 2: ПАРСИНГ И ГЕНЕРАЦИЯ ===

# === ФУНКЦИИ ПАРСИНГА ЖАЛОБ ===

async def parse_complaint_from_url(url: str) -> Tuple[bool, Optional[Dict], str]:
    """
    Парсит жалобу с форума по URL.

    Returns:
        Tuple[success, parsed_data, error_message]
    """
    # Проверяем доступность парсера
    parser_available = await check_remote_parser_available()
    if not parser_available:
        return False, None, "Сервис парсинга временно недоступен. Попробуйте позже."

    try:
        # Парсим тему форума (все посты, не только автора).
        # content_cleanup=False: цитаты в жалобах — содержимое, не мусор.
        result = await remote_parse_topic(url, parse_only_author=False, content_cleanup=False)

        if not result:
            return False, None, "Не удалось загрузить тему с форума. Проверьте ссылку."

        parsed_data = {
            'title': result.get('title', 'Без названия'),
            'content': result.get('content', ''),
            'thread_id': result.get('thread_id'),
            'url': url,
            'parsed_at': datetime.now().isoformat()
        }

        # Извлекаем информацию о заявителе из первого поста
        content = parsed_data['content']
        if content:
            # Пробуем найти ник заявителя в тексте
            applicant_match = re.search(
                r'(?:Заявитель|От кого|ФИО|Ник)[:\s]+([A-Za-z_]+\s*[A-Za-z_]*)',
                content, re.IGNORECASE
            )
            if applicant_match:
                parsed_data['applicant'] = applicant_match.group(1).strip()

        logging.info(f"[PCH] Успешно распарсена жалоба: {parsed_data['title']}")
        return True, parsed_data, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка парсинга жалобы {url}: {e}")
        return False, None, f"Ошибка при парсинге: {str(e)[:100]}"


# === ФУНКЦИИ ГЕНЕРАЦИИ AI ===

async def run_intake_analysis(
    session_id: int,
    parsed_complaint: Dict,
    form_template: Optional[str],
    relevant_laws: str
) -> Tuple[bool, str, str]:
    """
    Фаза A: Приём и первичный анализ жалобы.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)

    # Формируем текст жалобы для анализа
    complaint_text = f"Заголовок: {parsed_complaint.get('title', 'N/A')}\n\n"
    complaint_text += parsed_complaint.get('content', 'Текст жалобы не найден')

    prompt = get_pch_intake_prompt(
        complaint_text=complaint_text,
        form_template=form_template or "Форма подачи не предоставлена",
        relevant_laws=relevant_laws
    )

    try:
        async def make_intake_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4000,
                )
            )

        response = await execute_vertex_request(
            func=make_intake_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем результаты в сессию
        await manager.update_session(
            summary=content[:2000],  # Сохраняем краткое резюме
            parsed_complaint=parsed_complaint
        )

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='intake',
            content=content,
            ai_review_notes="Первичный анализ завершён"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка intake analysis: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def run_planning_phase(session_id: int) -> Tuple[bool, str, str]:
    """
    Фаза B: Составление плана действий.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    # Получаем данные для анализа
    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()
    server_id = session.get('server_id')

    # Получаем релевантные законы через RAG-систему
    parsed_complaint = await manager.get_parsed_complaint()
    complaint_text = parsed_complaint.get('content', '') if parsed_complaint else summary
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='planning')

    prompt = get_pch_planning_prompt(
        summary=summary,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        relevant_laws=relevant_laws
    )

    try:
        async def make_planning_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4000,
                )
            )

        response = await execute_vertex_request(
            func=make_planning_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='planning',
            content=content,
            ai_review_notes="План действий сформирован"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка planning phase: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def run_clarification_phase(session_id: int) -> Tuple[bool, str, str]:
    """
    Фаза C: Генерация уточняющих вопросов.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()

    # Получаем результаты предыдущих этапов
    planning_result = await manager.get_latest_stage_content('planning') or ''

    # Определяем недостающую информацию на основе плана
    missing_info = "На основании плана действий определить недостающие данные"

    prompt = get_pch_clarification_prompt(
        summary=summary,
        planning_result=planning_result,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        missing_info=missing_info
    )

    try:
        async def make_clarification_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.4,
                    max_output_tokens=2000,
                )
            )

        response = await execute_vertex_request(
            func=make_clarification_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='clarification',
            content=content,
            ai_review_notes="Уточняющие вопросы сформулированы"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка clarification phase: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def run_finalization_phase(session_id: int) -> Tuple[bool, str, str]:
    """
    Фаза D: Завершение и принятие решения.

    Returns:
        Tuple[success, content, ai_notes]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()
    server_id = session.get('server_id')

    # Получаем все предыдущие этапы
    planning_result = await manager.get_latest_stage_content('planning') or ''
    clarifications = await manager.get_latest_stage_content('clarification') or ''

    # Получаем законы через RAG-систему
    parsed_complaint = await manager.get_parsed_complaint()
    complaint_text = parsed_complaint.get('content', '') if parsed_complaint else summary
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='finalization')

    prompt = get_pch_finalization_prompt(
        summary=summary,
        planning_result=planning_result,
        clarifications=clarifications,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        relevant_laws=relevant_laws
    )

    try:
        async def make_finalization_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=4000,
                )
            )

        response = await execute_vertex_request(
            func=make_finalization_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем этап
        await manager.save_stage_content(
            stage_type='finalization',
            content=content,
            ai_review_notes="Финальное решение подготовлено"
        )

        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка finalization phase: {e}")
        return False, "", f"Ошибка AI: {str(e)[:100]}"


async def regenerate_stage(session_id: int, stage_type: str, user_feedback: str = None) -> Tuple[bool, str, str]:
    """
    Перегенерирует этап с учётом обратной связи пользователя.

    Returns:
        Tuple[success, content, ai_notes]
    """
    # Определяем какую функцию вызвать
    stage_runners = {
        'intake': run_intake_analysis,
        'planning': run_planning_phase,
        'clarification': run_clarification_phase,
        'finalization': run_finalization_phase
    }

    if stage_type not in stage_runners:
        return False, "", f"Неизвестный тип этапа: {stage_type}"

    # Если есть фидбек, добавляем его в collected_data
    if user_feedback:
        manager = ProsecutionComplaintManager(session_id)
        await manager.update_collected_data({
            f'{stage_type}_feedback': user_feedback,
            f'{stage_type}_feedback_at': datetime.now().isoformat()
        })

    # Для intake нужны дополнительные параметры
    if stage_type == 'intake':
        manager = ProsecutionComplaintManager(session_id)
        session = await manager.load_session_data()
        parsed_complaint = await manager.get_parsed_complaint()
        server_id = session.get('server_id')
        user_id = session.get('user_id')

        form_template = await get_form_template(user_id, server_id)
        complaint_text = parsed_complaint.get('content', '') if parsed_complaint else ''
        relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text)

        return await run_intake_analysis(session_id, parsed_complaint, form_template, relevant_laws)

    # Для остальных этапов
    return await stage_runners[stage_type](session_id)


# === ФУНКЦИИ ГЕНЕРАЦИИ ДОКУМЕНТОВ ===

async def generate_document(
    session_id: int,
    doc_type: str,
    additional_instructions: str = ""
) -> Tuple[bool, str, str]:
    """
    Генерирует документ указанного типа.

    Returns:
        Tuple[success, document_content, error_message]
    """
    # Разрешаем как стандартные типы, так и 'custom' для кастомных запросов
    if doc_type != 'custom' and doc_type not in PCH_DOCUMENT_TYPES:
        return False, "", f"Неизвестный тип документа: {doc_type}"

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    summary = session.get('summary', '')
    collected_data = await manager.get_collected_data()
    server_id = session.get('server_id')

    # Получаем релевантные законы через RAG-систему
    parsed_complaint = await manager.get_parsed_complaint()
    complaint_text = parsed_complaint.get('content', '') if parsed_complaint else summary
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='document')

    prompt = get_pch_document_prompt(
        document_type=doc_type,
        summary=summary,
        collected_data=json.dumps(collected_data, ensure_ascii=False),
        relevant_laws=relevant_laws,
        additional_instructions=additional_instructions
    )

    try:
        async def make_document_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=PCH_MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.2,  # Низкая температура для документов
                    max_output_tokens=3000,
                )
            )

        response = await execute_vertex_request(
            func=make_document_request,
            model_name=PCH_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        content = response.text.strip()

        # Сохраняем документ в сессию
        await manager.add_generated_document(
            doc_type=doc_type,
            content=content,
            metadata={'additional_instructions': additional_instructions}
        )

        logging.info(f"[PCH] Сгенерирован документ '{doc_type}' для сессии #{session_id}")
        return True, content, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка генерации документа: {e}")
        return False, "", f"Ошибка генерации: {str(e)[:100]}"


def get_available_documents_for_stage(stage_index: int) -> List[str]:
    """Возвращает список доступных типов документов для текущего этапа."""
    if stage_index >= len(PCH_STAGES_ORDER):
        return []

    stage_type = PCH_STAGES_ORDER[stage_index]
    return PCH_STAGE_AVAILABLE_DOCS.get(stage_type, [])


def build_document_selection_keyboard(
    session_id: int,
    available_docs: List[str]
) -> InlineKeyboardMarkup:
    """Создаёт клавиатуру для выбора типа документа."""
    keyboard = []

    for doc_type in available_docs:
        doc_name = PCH_DOCUMENT_TYPES.get(doc_type, doc_type)
        keyboard.append([
            InlineKeyboardButton(
                f"📄 {doc_name}",
                callback_data=f"pch_doc_{doc_type}_{session_id}"
            )
        ])

    # Добавляем кнопку для кастомного документа
    keyboard.append([
        InlineKeyboardButton(
            "✏️ Свой запрос (написать вручную)",
            callback_data=f"pch_custom_doc_{session_id}"
        )
    ])

    keyboard.append([
        InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")
    ])

    return InlineKeyboardMarkup(keyboard)


# === ИНТЕГРАЦИЯ С CRIMINAL CASE WRITER ===

async def convert_to_criminal_case(session_id: int, context) -> Tuple[bool, int, str]:
    """
    Конвертирует жалобу в уголовное дело через CCW.

    Returns:
        Tuple[success, criminal_case_id, error_message]
    """
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    user_id = session.get('user_id')
    server_id = session.get('server_id')
    complaint_number = session.get('complaint_number', '')

    # Собираем все материалы жалобы
    collected_data = await manager.get_collected_data()
    parsed_complaint = await manager.get_parsed_complaint()
    all_stages = await manager.get_all_stages()
    generated_docs = await manager.get_generated_documents()

    # Формируем данные для уголовного дела
    case_facts = f"""
МАТЕРИАЛЫ ИЗ ЖАЛОБЫ {complaint_number}

=== ИСХОДНАЯ ЖАЛОБА ===
{parsed_complaint.get('content', 'Не найдено') if parsed_complaint else 'Не найдено'}

=== РЕЗЮМЕ (Phase A) ===
{session.get('summary', 'Не сформировано')}

=== ПЛАН ДЕЙСТВИЙ (Phase B) ===
{all_stages.get('planning', {}).get('content', 'Не сформирован')}

=== УТОЧНЕНИЯ (Phase C) ===
{all_stages.get('clarification', {}).get('content', 'Не проводились')}

=== ФИНАЛЬНОЕ РЕШЕНИЕ (Phase D) ===
{all_stages.get('finalization', {}).get('content', 'Не принято')}

=== ДОПОЛНИТЕЛЬНЫЕ МАТЕРИАЛЫ ===
{json.dumps(collected_data, ensure_ascii=False, indent=2)}
"""

    try:
        # Импортируем CCW менеджер
        from .criminal_case_flow import CriminalCaseManager, schedule_ccw_session_closure

        # Создаём сессию CCW
        ccw_manager = await CriminalCaseManager.create_session(user_id, server_id)
        ccw_session_id = ccw_manager.session_id

        # Генерируем номер дела на основе номера жалобы
        # PCH-2026-0042 -> DJR-042
        case_number = complaint_number.replace('PCH-', 'DJR-').split('-')[-1]
        case_number = f"DJR-{case_number}"

        await ccw_manager.update_session(case_number=case_number)
        await ccw_manager.update_collected_data({
            'initial_facts': case_facts,
            'source': 'prosecution_complaint',
            'source_session_id': session_id,
            'source_complaint_number': complaint_number,
            'input_timestamp': datetime.now().isoformat()
        })

        # Связываем жалобу с уголовным делом
        await manager.link_criminal_case(ccw_session_id)

        # Планируем автозакрытие CCW сессии
        schedule_ccw_session_closure(context, ccw_session_id, user_id)

        logging.info(
            f"[PCH] Жалоба #{session_id} конвертирована в уголовное дело #{ccw_session_id}"
        )
        return True, ccw_session_id, ""

    except Exception as e:
        logging.error(f"[PCH] Ошибка конвертации в уголовное дело: {e}")
        return False, 0, f"Ошибка: {str(e)[:100]}"


# Конец Части 2


# === ЧАСТЬ 3: ОБРАБОТЧИКИ СОСТОЯНИЙ ===

# === ENTRY POINTS ===

async def start_prosecution_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начало разбора жалобы - показывает меню с историей."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    # Проверка доступа: функция доступна тестерам и админам
    user_record = await db_manager.fetchrow(
        "SELECT is_admin, is_tester FROM users WHERE user_id = $1", user_id
    )
    is_admin = (user_record and user_record['is_admin']) or user_id == config.SUPER_ADMIN_ID
    is_tester = user_record and user_record.get('is_tester', False)

    if not is_admin and not is_tester:
        await safe_edit_message_text(
            query,
            "🔒 <b>Функция в бета-тестировании</b>\n\n"
            "Разбор жалоб пока доступен только тестировщикам.\n\n"
            "Следите за обновлениями — скоро функция будет доступна всем!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Получаем историю жалоб
    sessions = await ProsecutionComplaintManager.get_user_sessions(user_id, limit=10)

    # Проверяем, есть ли активная сессия
    active_session = None
    for session in sessions:
        if session['status'] == 'active':
            active_session = session
            break

    # Формируем сообщение
    text = "📋 <b>Разбор жалоб в прокуратуру</b>\n\n"

    if sessions:
        text += "<b>📜 История жалоб:</b>\n"
        for session in sessions[:5]:
            status_info = format_session_status(session)
            date_str = session['created_at'].strftime('%d.%m.%Y %H:%M') if session.get('created_at') else 'N/A'
            complaint_num = session.get('complaint_number', 'Без номера')
            text += f"📋 <b>{complaint_num}</b> ({date_str})\n   {status_info}\n"
        text += "\n"
    else:
        text += "<i>У вас пока нет обработанных жалоб.</i>\n\n"

    # Формируем кнопки
    keyboard = []

    # Кнопка продолжения активной сессии
    if active_session:
        keyboard.append([InlineKeyboardButton(
            f"▶️ Продолжить: {active_session.get('complaint_number', 'N/A')}",
            callback_data=f"pch_continue_{active_session['id']}"
        )])

    # Кнопка создания новой жалобы
    keyboard.append([InlineKeyboardButton("➕ Новая жалоба", callback_data="pch_create_new")])

    # Кнопка истории
    if sessions:
        keyboard.append([InlineKeyboardButton("📜 Вся история", callback_data="pch_history")])

    keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return PCH_NAVIGATE


async def create_new_complaint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает создание новой жалобы (показывает выбор проекта)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id

    # Проверяем лимиты
    allowed, current_usage = await check_and_update_usage(user_id, 'complaint_processing')
    if not allowed:
        await safe_edit_message_text(
            query,
            "❌ <b>Лимит исчерпан</b>\n\n"
            "Вы использовали все доступные запросы на разбор жалоб.\n"
            "Лимит обновится через некоторое время.\n\n"
            "💎 Подпишитесь на канал для получения бонусных запросов!",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Показываем выбор проекта
    enabled_projects = await get_enabled_projects()

    # Если только один проект - пропускаем выбор
    if len(enabled_projects) == 1:
        context.user_data['pch_project_id'] = enabled_projects[0]
        return await _show_pch_servers(update, context, enabled_projects[0])

    # Удаляем старое сообщение
    try:
        await query.message.delete()
    except Exception:
        pass

    # Отправляем меню выбора проекта
    await send_project_selection_menu(
        context=context,
        chat_id=query.message.chat_id,
        callback_prefix="pch_project_",
        text="📋 <b>Разбор жалобы</b>\n\n🌐 <b>Выберите проект:</b>",
        back_callback="start_prosecution_complaint"
    )
    return PCH_CHOOSING_PROJECT


async def _show_pch_servers(update: Update, context: ContextTypes.DEFAULT_TYPE, project_id: str) -> int:
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
            await safe_edit_message_text(
                query, "На данный момент нет доступных серверов для этого проекта."
            )
        return ConversationHandler.END

    project_data = PROJECTS_CONFIG.get(project_id, {})
    project_name = project_data.get('display_name', project_id)

    keyboard = [
        [InlineKeyboardButton(
            SERVERS_CONFIG[server_id]['display_name'],
            callback_data=f"pch_server_{server_id}"
        )]
        for server_id in enabled_servers
    ]

    # Кнопка "Назад" зависит от количества проектов
    enabled_projects = await get_enabled_projects()
    if len(enabled_projects) > 1:
        keyboard.append([InlineKeyboardButton(
            "◀️ К выбору проекта", callback_data="start_prosecution_complaint"
        )])
    else:
        keyboard.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])

    text = (
        f"📋 <b>Разбор жалобы</b>\n"
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
    return PCH_CHOOSING_SERVER


async def select_pch_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный проект и показывает серверы."""
    query = update.callback_query
    await query.answer()

    project_id = query.data.split("pch_project_", 1)[1]
    context.user_data['pch_project_id'] = project_id

    return await _show_pch_servers(update, context, project_id)


async def select_pch_server(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Сохраняет выбранный сервер и переходит к вводу URL жалобы."""
    query = update.callback_query
    await query.answer()

    server_id = query.data.split("pch_server_", 1)[1]
    context.user_data['pch_server_id'] = server_id

    server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)
    user_id = update.effective_user.id

    # Проверяем, есть ли кэшированная форма подачи
    cached_form = await get_form_template(user_id, server_id)

    text = (
        f"📋 <b>Разбор жалобы</b>\n"
        f"🖥️ Сервер: <b>{server_name}</b>\n\n"
        "📎 <b>Отправьте ссылку на тему с жалобой на форуме</b>\n\n"
        "<i>Пример: https://forum.gta5rp.com/threads/12345/, https://forum.rmrp.ru/threads/12345/ "
        "или https://forum.russia.online/threads/12345/</i>"
    )

    if cached_form:
        text += "\n\n✅ <i>Форма подачи жалобы для этого сервера уже сохранена.</i>"
    else:
        text += (
            "\n\n💡 <i>После ввода ссылки я попрошу вас предоставить форму подачи "
            "жалобы для скрининга соответствия.</i>"
        )

    # Формируем клавиатуру
    keyboard = []

    # Кнопка замены формы (если форма уже есть)
    if cached_form:
        keyboard.append([InlineKeyboardButton(
            "🔄 Заменить форму подачи",
            callback_data="pch_replace_form"
        )])

    keyboard.append([InlineKeyboardButton(
        "◀️ Назад",
        callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}"
    )])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_AWAITING_URL


async def handle_url_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод URL жалобы."""
    user_id = update.effective_user.id
    url = update.message.text.strip()
    server_id = context.user_data.get('pch_server_id')

    # Валидация URL
    if not re.match(r'^https?://', url):
        await update.message.reply_text(
            "❌ Неверный формат ссылки.\n"
            "Отправьте полную ссылку на тему форума, начинающуюся с http:// или https://",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "◀️ К выбору сервера",
                    callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}"
                )]
            ])
        )
        return PCH_AWAITING_URL

    context.user_data['pch_complaint_url'] = url

    # Проверяем, есть ли кэшированная форма подачи
    cached_form = await get_form_template(user_id, server_id)

    if cached_form:
        # Форма уже есть - переходим сразу к парсингу
        context.user_data['pch_form_template'] = cached_form
        return await start_parsing_complaint(update, context)
    else:
        # Запрашиваем форму подачи
        await update.message.reply_text(
            "📝 <b>Форма подачи жалобы</b>\n\n"
            "Для корректного скрининга жалобы, пожалуйста, отправьте образец формы "
            "подачи жалобы в прокуратуру, принятый на вашем сервере.\n\n"
            "<b>Варианты:</b>\n"
            "• 🔗 <b>Ссылка на форум</b> — бот автоматически спарсит форму\n"
            "• 📋 <b>Текст формы</b> — скопируйте и отправьте текст шаблона\n\n"
            "<i>Форма будет сохранена для последующих жалоб на этом сервере.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_AWAITING_FORM


async def handle_form_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод формы подачи жалобы (текст или ссылка)."""
    user_id = update.effective_user.id
    input_text = update.message.text.strip()
    server_id = context.user_data.get('pch_server_id')

    # Проверяем, является ли ввод ссылкой
    if re.match(r'^https?://', input_text):
        # Это ссылка - парсим форму с форума
        progress_msg = await update.message.reply_text(
            "⏳ <b>Загружаю форму с форума...</b>",
            parse_mode='HTML'
        )

        # Проверяем доступность парсера
        parser_available = await check_remote_parser_available()
        if not parser_available:
            await context.bot.edit_message_text(
                chat_id=progress_msg.chat.id,
                message_id=progress_msg.message_id,
                text="❌ Сервис парсинга временно недоступен. Пожалуйста, скопируйте текст формы вручную.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
                ])
            )
            return PCH_AWAITING_FORM

        try:
            # Парсим тему с форумом (content_cleanup=False: форма может лежать в цитате)
            result = await remote_parse_topic(input_text, parse_only_author=True, content_cleanup=False)

            if not result or not result.get('content'):
                await context.bot.edit_message_text(
                    chat_id=progress_msg.chat.id,
                    message_id=progress_msg.message_id,
                    text="❌ Не удалось загрузить форму с форума. Проверьте ссылку или скопируйте текст вручную.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                        [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
                    ])
                )
                return PCH_AWAITING_FORM

            form_content = result.get('content', '')
            form_title = result.get('title', 'Форма подачи')

            # Сохраняем форму в кэш
            await save_form_template(user_id, server_id, form_content)
            context.user_data['pch_form_template'] = form_content

            # === DEBUG: Отправляем файл с распарсенной формой ===
            debug_content = f"""=== ДЕБАГ: РАСПАРСЕННАЯ ФОРМА ПОДАЧИ ===
Дата парсинга: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
URL: {input_text}
Сервер: {server_id}
Заголовок: {form_title}
Длина контента: {len(form_content)} символов

{'='*50}
СОДЕРЖИМОЕ ФОРМЫ:
{'='*50}

{form_content}

{'='*50}
КОНЕЦ ФОРМЫ
{'='*50}
"""
            await send_debug_document(
                context=context,
                chat_id=update.effective_chat.id,
                content=debug_content,
                filename=f"form_parsed_{server_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
                caption=f"🔍 DEBUG: Распарсенная форма подачи\n📋 {form_title}"
            )
            # === END DEBUG ===

            # Проверяем, заменяем ли мы форму или это первичный ввод
            if context.user_data.get('pch_replacing_form'):
                context.user_data['pch_replacing_form'] = False
                server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

                await context.bot.edit_message_text(
                    chat_id=progress_msg.chat.id,
                    message_id=progress_msg.message_id,
                    text=f"✅ <b>Форма обновлена!</b>\n\n"
                         f"📋 <b>{form_title}</b>\n"
                         f"<i>Длина: {len(form_content)} символов</i>\n\n"
                         f"🖥️ Сервер: <b>{server_name}</b>\n\n"
                         "📎 <b>Теперь отправьте ссылку на тему с жалобой на форуме</b>",
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔄 Заменить форму подачи", callback_data="pch_replace_form")],
                        [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}")]
                    ])
                )
                return PCH_AWAITING_URL

            await context.bot.edit_message_text(
                chat_id=progress_msg.chat.id,
                message_id=progress_msg.message_id,
                text=f"✅ <b>Форма загружена и сохранена!</b>\n\n"
                     f"📋 <b>{form_title}</b>\n\n"
                     f"<i>Длина: {len(form_content)} символов</i>",
                parse_mode='HTML'
            )

            return await start_parsing_complaint(update, context)

        except Exception as e:
            logging.error(f"[PCH] Ошибка парсинга формы: {e}")
            await context.bot.edit_message_text(
                chat_id=progress_msg.chat.id,
                message_id=progress_msg.message_id,
                text=f"❌ Ошибка при загрузке формы: {str(e)[:100]}\n\n"
                     "Пожалуйста, скопируйте текст формы вручную.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
                ])
            )
            return PCH_AWAITING_FORM

    # Это текст формы
    form_content = input_text

    if len(form_content) < 50:
        await update.message.reply_text(
            "⚠️ Форма слишком короткая. Пожалуйста, предоставьте более полный шаблон.\n\n"
            "Если хотите продолжить без формы, нажмите «Пропустить».",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭️ Пропустить", callback_data="pch_skip_form")],
                [InlineKeyboardButton("❌ Отмена", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_AWAITING_FORM

    # Сохраняем форму в кэш
    await save_form_template(user_id, server_id, form_content)
    context.user_data['pch_form_template'] = form_content

    # Проверяем, заменяем ли мы форму или это первичный ввод
    if context.user_data.get('pch_replacing_form'):
        context.user_data['pch_replacing_form'] = False
        server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

        await update.message.reply_text(
            f"✅ <b>Форма обновлена!</b>\n\n"
            f"📋 <b>Разбор жалобы</b>\n"
            f"🖥️ Сервер: <b>{server_name}</b>\n\n"
            "📎 <b>Теперь отправьте ссылку на тему с жалобой на форуме</b>\n\n"
            "<i>Пример: https://forum.gta5rp.com/threads/12345/, https://forum.rmrp.ru/threads/12345/ "
            "или https://forum.russia.online/threads/12345/</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Заменить форму подачи", callback_data="pch_replace_form")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_project_{context.user_data.get('pch_project_id', '')}")]
            ])
        )
        return PCH_AWAITING_URL

    await update.message.reply_text("✅ Форма сохранена!")

    return await start_parsing_complaint(update, context)


async def skip_form_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Пропускает ввод формы подачи."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    context.user_data['pch_form_template'] = None
    return await start_parsing_complaint(update, context, from_callback=True)


async def replace_form_template(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Позволяет заменить сохранённую форму подачи на новую."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    server_id = context.user_data.get('pch_server_id')
    server_name = SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    await safe_edit_message_text(
        query,
        f"🔄 <b>Замена формы подачи</b>\n"
        f"🖥️ Сервер: <b>{server_name}</b>\n\n"
        "Отправьте новую форму подачи жалобы:\n\n"
        "<b>Варианты:</b>\n"
        "• 🔗 <b>Ссылка на форум</b> — бот автоматически спарсит форму\n"
        "• 📋 <b>Текст формы</b> — скопируйте и отправьте текст шаблона\n\n"
        "<i>Новая форма заменит ранее сохранённую.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_server_{server_id}")]
        ])
    )

    # Устанавливаем флаг что мы заменяем форму (чтобы после сохранения вернуться к вводу URL)
    context.user_data['pch_replacing_form'] = True
    return PCH_AWAITING_FORM


async def start_parsing_complaint(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    from_callback: bool = False
) -> int:
    """Начинает парсинг жалобы и первичный анализ."""
    user_id = update.effective_user.id
    url = context.user_data.get('pch_complaint_url')
    server_id = context.user_data.get('pch_server_id')
    form_template = context.user_data.get('pch_form_template')

    # Отправляем сообщение о начале парсинга
    if from_callback:
        query = update.callback_query
        progress_msg = await safe_edit_message_text(
            query,
            "⏳ <b>Загружаю жалобу с форума...</b>\n\n"
            "Это может занять несколько секунд.",
            parse_mode='HTML'
        )
    else:
        progress_msg = await update.message.reply_text(
            "⏳ <b>Загружаю жалобу с форума...</b>\n\n"
            "Это может занять несколько секунд.",
            parse_mode='HTML'
        )

    # Парсим жалобу
    success, parsed_complaint, error = await parse_complaint_from_url(url)

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка загрузки жалобы</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data="pch_create_new")],
                [InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_NAVIGATE

    # Создаём сессию
    manager = await ProsecutionComplaintManager.create_session(
        user_id=user_id,
        server_id=server_id,
        complaint_url=url
    )
    session_id = manager.session_id
    context.user_data['pch_session_id'] = session_id

    # Сохраняем распарсенные данные
    await manager.update_session(parsed_complaint=parsed_complaint)

    # Планируем автозакрытие
    schedule_pch_session_closure(context, session_id, user_id)

    # Обновляем сообщение
    await context.bot.edit_message_text(
        chat_id=progress_msg.chat.id,
        message_id=progress_msg.message_id,
        text="⏳ <b>Анализирую жалобу...</b>\n\n"
             f"📋 <b>{parsed_complaint.get('title', 'Без названия')}</b>\n\n"
             "Провожу первичный анализ и скрининг формы подачи...",
        parse_mode='HTML'
    )

    # Получаем релевантные законы через RAG-систему
    complaint_text = parsed_complaint.get('content', '')
    relevant_laws = await get_relevant_laws_for_complaint(server_id, complaint_text, stage_name='intake')

    # Запускаем Phase A: Intake Analysis
    success, content, error = await run_intake_analysis(
        session_id=session_id,
        parsed_complaint=parsed_complaint,
        form_template=form_template,
        relevant_laws=relevant_laws
    )

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка анализа</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"pch_retry_intake_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_NAVIGATE

    # Обновляем индекс этапа
    await manager.update_session(current_stage_index=0)
    context.user_data['pch_current_stage_index'] = 0

    # Получаем данные сессии для отображения
    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    # Форматируем и показываем результат Phase A
    available_docs = get_available_documents_for_stage(0)
    message_text = format_stage_message(
        complaint_number=complaint_number,
        stage_index=0,
        stage_content=sanitize_telegram_html(content[:3500]),
        ai_review="Первичный анализ завершён. Проверьте данные и переходите к следующему этапу."
    )

    keyboard = build_stage_keyboard(
        current_stage_index=0,
        session_id=session_id,
        show_accept=True,
        show_doc_button=bool(available_docs),
        available_docs=available_docs
    )

    await context.bot.edit_message_text(
        chat_id=progress_msg.chat.id,
        message_id=progress_msg.message_id,
        text=message_text,
        parse_mode='HTML',
        reply_markup=keyboard
    )

    return PCH_USER_REVIEW


# === ОБРАБОТЧИКИ ФАЗ ===

async def handle_accept_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Принимает текущий этап и переходит к следующему."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_accept_")[1])
    manager = ProsecutionComplaintManager(session_id)

    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Помечаем этап как финальный
    await manager.mark_stage_final(stage_type)

    # Переходим к следующему этапу
    next_stage_index = current_stage_index + 1

    if next_stage_index >= len(PCH_STAGES_ORDER):
        # Все этапы завершены - показываем финальный экран
        return await show_completion_screen(update, context, session_id)

    # Обновляем индекс
    await manager.update_session(current_stage_index=next_stage_index)
    context.user_data['pch_current_stage_index'] = next_stage_index

    # Показываем индикатор загрузки
    await safe_edit_message_text(
        query,
        f"⏳ <b>Генерирую этап {next_stage_index + 1} из {len(PCH_STAGES_ORDER)}...</b>\n\n"
        f"{PCH_STAGES_DISPLAY.get(PCH_STAGES_ORDER[next_stage_index], 'Обработка...')}",
        parse_mode='HTML'
    )

    # Запускаем соответствующую фазу
    stage_runners = {
        1: run_planning_phase,
        2: run_clarification_phase,
        3: run_finalization_phase
    }

    runner = stage_runners.get(next_stage_index)
    if runner:
        success, content, error = await runner(session_id)

        if not success:
            await safe_edit_message_text(
                query,
                f"❌ <b>Ошибка генерации этапа</b>\n\n{error}",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_accept_{session_id}")],
                    [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
                ])
            )
            return PCH_USER_REVIEW

        # Показываем результат
        return await show_current_stage(update, context, session_id=session_id)

    return PCH_USER_REVIEW


async def handle_refine_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Запрашивает уточнения для текущего этапа."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_refine_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_name = PCH_STAGES_DISPLAY.get(PCH_STAGES_ORDER[current_stage_index], 'этапа')

    await safe_edit_message_text(
        query,
        f"✏️ <b>Уточнение {stage_name}</b>\n\n"
        "Введите ваши комментарии, уточнения или дополнительную информацию.\n\n"
        "<i>AI учтёт ваш ввод при перегенерации этапа.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    context.user_data['pch_awaiting_refinement'] = True
    return PCH_PHASE_A  # Используем состояние для ввода текста


async def handle_refinement_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод уточнений и перегенерирует этап."""
    user_feedback = update.message.text.strip()
    session_id = context.user_data.get('pch_session_id')
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    context.user_data['pch_awaiting_refinement'] = False

    # Показываем индикатор
    progress_msg = await update.message.reply_text(
        "⏳ <b>Обрабатываю уточнения...</b>",
        parse_mode='HTML'
    )

    # Перегенерируем этап
    success, content, error = await regenerate_stage(
        session_id=session_id,
        stage_type=stage_type,
        user_feedback=user_feedback
    )

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_refine_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Удаляем сообщение прогресса
    try:
        await context.bot.delete_message(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id
        )
    except Exception:
        pass

    # Показываем обновлённый этап
    return await show_current_stage_new_message(update, context, session_id=session_id)


async def handle_regenerate_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Полностью перегенерирует текущий этап."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_regenerate_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Показываем индикатор
    await safe_edit_message_text(
        query,
        f"⏳ <b>Перегенерирую {PCH_STAGES_DISPLAY.get(stage_type, 'этап')}...</b>",
        parse_mode='HTML'
    )

    # Перегенерируем
    success, content, error = await regenerate_stage(
        session_id=session_id,
        stage_type=stage_type
    )

    if not success:
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_regenerate_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Показываем результат
    return await show_current_stage(update, context, session_id=session_id)


async def show_current_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: int = None
) -> int:
    """Показывает текущий этап (редактирует существующее сообщение)."""
    query = update.callback_query

    if session_id is None:
        session_id = context.user_data.get('pch_session_id')
        if query and 'pch_show_stage_' in query.data:
            session_id = int(query.data.split("pch_show_stage_")[1])

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    current_stage_index = context.user_data.get('pch_current_stage_index', session.get('current_stage_index', 0))
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Получаем контент этапа
    stage_data = await manager.get_stage_with_notes(stage_type)
    content = stage_data.get('content', 'Контент не найден') if stage_data else 'Этап ещё не сгенерирован'
    ai_notes = stage_data.get('ai_review_notes') if stage_data else None

    # Форматируем сообщение
    message_text = format_stage_message(
        complaint_number=complaint_number,
        stage_index=current_stage_index,
        stage_content=sanitize_telegram_html(content[:3500]),
        ai_review=ai_notes
    )

    available_docs = get_available_documents_for_stage(current_stage_index)
    keyboard = build_stage_keyboard(
        current_stage_index=current_stage_index,
        session_id=session_id,
        show_accept=True,
        show_doc_button=bool(available_docs),
        available_docs=available_docs
    )

    if query:
        await safe_edit_message_text(
            query,
            message_text,
            parse_mode='HTML',
            reply_markup=keyboard
        )

    return PCH_USER_REVIEW


async def show_current_stage_new_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: int = None
) -> int:
    """Показывает текущий этап (отправляет новое сообщение)."""
    if session_id is None:
        session_id = context.user_data.get('pch_session_id')

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    current_stage_index = context.user_data.get('pch_current_stage_index', session.get('current_stage_index', 0))
    stage_type = PCH_STAGES_ORDER[current_stage_index]

    # Получаем контент этапа
    stage_data = await manager.get_stage_with_notes(stage_type)
    content = stage_data.get('content', 'Контент не найден') if stage_data else 'Этап ещё не сгенерирован'
    ai_notes = stage_data.get('ai_review_notes') if stage_data else None

    # Форматируем сообщение
    message_text = format_stage_message(
        complaint_number=complaint_number,
        stage_index=current_stage_index,
        stage_content=sanitize_telegram_html(content[:3500]),
        ai_review=ai_notes
    )

    available_docs = get_available_documents_for_stage(current_stage_index)
    keyboard = build_stage_keyboard(
        current_stage_index=current_stage_index,
        session_id=session_id,
        show_accept=True,
        show_doc_button=bool(available_docs),
        available_docs=available_docs
    )

    await update.message.reply_text(
        message_text,
        parse_mode='HTML',
        reply_markup=keyboard
    )

    return PCH_USER_REVIEW


async def _show_completion_screen_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обёртка для show_completion_screen, извлекает session_id из callback_data."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    session_id = int(query.data.split("pch_completion_")[1])
    return await show_completion_screen(update, context, session_id)


async def show_completion_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session_id: int
) -> int:
    """Показывает экран завершения обработки жалобы."""
    query = update.callback_query
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    generated_docs = await manager.get_generated_documents()

    text = (
        f"✅ <b>ОБРАБОТКА ЖАЛОБЫ ЗАВЕРШЕНА</b>\n\n"
        f"📋 Номер: <b>{complaint_number}</b>\n"
        f"📄 Сгенерировано документов: <b>{len(generated_docs)}</b>\n\n"
    )

    if generated_docs:
        text += "<b>Созданные документы:</b>\n"
        for i, doc in enumerate(generated_docs, 1):
            text += f"  {i}. {doc.get('type_display', doc.get('type'))}\n"
        text += "\n"

    text += (
        "<b>Доступные действия:</b>\n"
        "• Просмотреть сгенерированные документы\n"
        "• Конвертировать в уголовное дело\n"
        "• Завершить работу с жалобой"
    )

    keyboard = [
        [InlineKeyboardButton(
            "📄 Просмотреть документы",
            callback_data=f"pch_view_docs_{session_id}"
        )],
        [InlineKeyboardButton(
            "🔄 Конвертировать в уголовное дело",
            callback_data=f"pch_convert_ccw_{session_id}"
        )],
        [InlineKeyboardButton(
            "✅ Завершить",
            callback_data=f"pch_complete_{session_id}"
        )],
        [InlineKeyboardButton("◀️ К этапам", callback_data=f"pch_show_stage_{session_id}")]
    ]

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_EXPORT


# Конец Части 3


# === ЧАСТЬ 4: НАВИГАЦИЯ, ИСТОРИЯ, ПЛАНИРОВЩИК И CONVERSATION HANDLER ===

# === НАВИГАЦИЯ МЕЖДУ ЭТАПАМИ ===

async def handle_prev_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переходит к предыдущему этапу."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_prev_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)

    if current_stage_index > 0:
        context.user_data['pch_current_stage_index'] = current_stage_index - 1

    return await show_current_stage(update, context, session_id=session_id)


async def handle_next_stage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Переходит к следующему этапу (только просмотр, без генерации)."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_next_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)

    if current_stage_index < len(PCH_STAGES_ORDER) - 1:
        # Проверяем, есть ли контент для следующего этапа
        manager = ProsecutionComplaintManager(session_id)
        next_stage_type = PCH_STAGES_ORDER[current_stage_index + 1]
        next_content = await manager.get_latest_stage_content(next_stage_type)

        if next_content:
            context.user_data['pch_current_stage_index'] = current_stage_index + 1
        else:
            # Этап ещё не сгенерирован - показываем подсказку
            await query.answer(
                "Этот этап ещё не сгенерирован. Нажмите «Принять этап» для генерации.",
                show_alert=True
            )
            return PCH_USER_REVIEW

    return await show_current_stage(update, context, session_id=session_id)


# === ПРОДОЛЖЕНИЕ СЕССИИ ===

async def handle_continue_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Продолжает существующую сессию."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_continue_")[1])
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    if not session:
        await safe_edit_message_text(
            query,
            "❌ Сессия не найдена. Начните заново.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ Новая жалоба", callback_data="pch_create_new")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        return ConversationHandler.END

    # Восстанавливаем контекст
    context.user_data['pch_session_id'] = session_id
    context.user_data['pch_current_stage_index'] = session.get('current_stage_index', 0)
    context.user_data['pch_server_id'] = session.get('server_id')

    return await show_current_stage(update, context, session_id=session_id)


# === ИСТОРИЯ ЖАЛОБ ===

async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает полную историю жалоб пользователя."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    user_id = update.effective_user.id
    sessions = await ProsecutionComplaintManager.get_user_sessions(user_id, limit=15)

    if not sessions:
        await safe_edit_message_text(
            query,
            "📜 <b>История жалоб</b>\n\n<i>У вас пока нет обработанных жалоб.</i>",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Новая жалоба", callback_data="pch_create_new")],
                [InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")]
            ])
        )
        return PCH_NAVIGATE

    text = "📜 <b>ИСТОРИЯ ЖАЛОБ</b>\n══════════════════════════════════════════\n\n"

    for session in sessions:
        status_info = format_session_status(session)
        date_str = session['created_at'].strftime('%d.%m.%Y') if session.get('created_at') else 'N/A'
        complaint_num = session.get('complaint_number', 'Без номера')
        text += f"📋 <b>{complaint_num}</b> ({date_str})\n   {status_info}\n\n"

    text += "──────────────────────────────────────────\n"
    text += "<i>Выберите жалобу для просмотра:</i>"

    # Формируем кнопки
    keyboard = []
    for session in sessions[:5]:  # Максимум 5 кнопок
        complaint_num = session.get('complaint_number', 'N/A')
        status = session.get('status', '')
        btn_text = f"📋 {complaint_num}"
        if status == 'active':
            btn_text = f"⏳ {complaint_num}"
        keyboard.append([InlineKeyboardButton(
            btn_text,
            callback_data=f"pch_view_session_{session['id']}"
        )])

    keyboard.append([InlineKeyboardButton("➕ Новая жалоба", callback_data="pch_create_new")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="start_prosecution_complaint")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_NAVIGATE


async def view_session_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает детали конкретной сессии."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_view_session_")[1])
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    if not session:
        await safe_edit_message_text(
            query,
            "❌ Жалоба не найдена.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ К истории", callback_data="pch_history")]
            ])
        )
        return PCH_NAVIGATE

    complaint_number = session.get('complaint_number', 'PCH-XXXX')
    status = session.get('status', 'unknown')
    created_at = session.get('created_at')
    date_str = created_at.strftime('%d.%m.%Y %H:%M') if created_at else 'N/A'
    stage_index = session.get('current_stage_index', 0)

    text = f"📋 <b>ЖАЛОБА {complaint_number}</b>\n══════════════════════════════════════════\n\n"
    text += f"📅 <b>Создана:</b> {date_str}\n"
    text += f"📊 <b>Статус:</b> {format_session_status(session)}\n"

    if status == 'active':
        stage_type = PCH_STAGES_ORDER[stage_index] if stage_index < len(PCH_STAGES_ORDER) else 'N/A'
        text += f"📍 <b>Текущий этап:</b> {PCH_STAGES_DISPLAY.get(stage_type, stage_type)}\n"

    # Получаем сгенерированные документы
    generated_docs = await manager.get_generated_documents()
    if generated_docs:
        text += f"\n📄 <b>Документы ({len(generated_docs)}):</b>\n"
        for doc in generated_docs[:5]:
            text += f"  • {doc.get('type_display', doc.get('type'))}\n"

    if session.get('linked_criminal_case_id'):
        text += f"\n🔗 <b>Связано с уголовным делом:</b> #{session['linked_criminal_case_id']}"

    # Формируем кнопки
    keyboard = []

    if status == 'active':
        keyboard.append([InlineKeyboardButton(
            "▶️ Продолжить работу",
            callback_data=f"pch_continue_{session_id}"
        )])
    elif status == 'completed' or status == 'converted_to_case':
        if generated_docs:
            keyboard.append([InlineKeyboardButton(
                "📄 Просмотреть документы",
                callback_data=f"pch_view_docs_{session_id}"
            )])

    keyboard.append([InlineKeyboardButton("◀️ К истории", callback_data="pch_history")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_NAVIGATE


# === РАБОТА С ДОКУМЕНТАМИ ===

async def show_document_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает меню выбора типа документа для генерации."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_gen_doc_")[1])
    current_stage_index = context.user_data.get('pch_current_stage_index', 0)
    available_docs = get_available_documents_for_stage(current_stage_index)

    if not available_docs:
        await query.answer("На этом этапе нет доступных документов для генерации.", show_alert=True)
        return PCH_USER_REVIEW

    stage_name = PCH_STAGES_DISPLAY.get(PCH_STAGES_ORDER[current_stage_index], 'текущего этапа')
    text = (
        f"📄 <b>Генерация документа</b>\n\n"
        f"Этап: {stage_name}\n\n"
        "Выберите тип документа для генерации:"
    )

    keyboard = build_document_selection_keyboard(session_id, available_docs)

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=keyboard
    )

    return PCH_USER_REVIEW


async def handle_generate_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Генерирует выбранный документ."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    # Парсим callback_data: pch_doc_{doc_type}_{session_id}
    # doc_type может содержать _, поэтому берём всё между pch_doc_ и последним _
    data = query.data  # например: pch_doc_acceptance_notice_7
    prefix = "pch_doc_"
    suffix_part = data[len(prefix):]  # acceptance_notice_7
    last_underscore = suffix_part.rfind("_")
    doc_type = suffix_part[:last_underscore]  # acceptance_notice
    session_id = int(suffix_part[last_underscore + 1:])  # 7

    doc_name = PCH_DOCUMENT_TYPES.get(doc_type, doc_type)

    # Показываем индикатор
    await safe_edit_message_text(
        query,
        f"⏳ <b>Генерирую документ...</b>\n\n📄 {doc_name}",
        parse_mode='HTML'
    )

    # Генерируем документ
    success, content, error = await generate_document(session_id, doc_type)

    if not success:
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка генерации документа</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_doc_{doc_type}_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Показываем сгенерированный документ
    text = (
        f"✅ <b>ДОКУМЕНТ СГЕНЕРИРОВАН</b>\n"
        f"📄 <b>{doc_name}</b>\n"
        f"══════════════════════════════════════════\n\n"
        f"{sanitize_telegram_html(content[:3500])}\n\n"
        f"──────────────────────────────────────────"
    )

    keyboard = [
        [InlineKeyboardButton("📄 Создать ещё документ", callback_data=f"pch_gen_doc_{session_id}")],
        [InlineKeyboardButton("◀️ К этапу", callback_data=f"pch_show_stage_{session_id}")]
    ]

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_USER_REVIEW


async def show_custom_document_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает форму для ввода своего запроса на документ."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_custom_doc_")[1])
    context.user_data['pch_custom_doc_session_id'] = session_id

    text = (
        "✏️ <b>Свой запрос на документ</b>\n\n"
        "Опишите, какой документ вам нужен. Например:\n"
        "• <i>Повестка для свидетеля John Doe</i>\n"
        "• <i>Запрос в LSPD о служебной проверке офицера</i>\n"
        "• <i>Требование о предоставлении видеозаписей с камер</i>\n"
        "• <i>Уведомление заявителю о продлении срока рассмотрения</i>\n\n"
        "📝 <b>Напишите ваш запрос:</b>"
    )

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_gen_doc_{session_id}")]
        ])
    )

    return PCH_ADD_MATERIAL  # Используем состояние для ввода текста


async def handle_custom_document_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает текстовый запрос на кастомный документ."""
    user_request = update.message.text.strip()
    session_id = context.user_data.get('pch_custom_doc_session_id')

    if not session_id:
        await update.message.reply_text(
            "❌ Сессия не найдена. Попробуйте снова.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 В главное меню", callback_data="main_menu")]
            ])
        )
        return PCH_USER_REVIEW

    if len(user_request) < 10:
        await update.message.reply_text(
            "⚠️ Слишком короткий запрос. Опишите подробнее, какой документ вам нужен.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_gen_doc_{session_id}")]
            ])
        )
        return PCH_ADD_MATERIAL

    # Показываем индикатор генерации
    progress_msg = await update.message.reply_text(
        f"⏳ <b>Генерирую документ по вашему запросу...</b>\n\n"
        f"📝 <i>{user_request[:100]}{'...' if len(user_request) > 100 else ''}</i>",
        parse_mode='HTML'
    )

    # Генерируем документ с кастомными инструкциями
    success, content, error = await generate_document(
        session_id,
        doc_type='custom',
        additional_instructions=user_request
    )

    if not success:
        await context.bot.edit_message_text(
            chat_id=progress_msg.chat.id,
            message_id=progress_msg.message_id,
            text=f"❌ <b>Ошибка генерации документа</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Попробовать снова", callback_data=f"pch_custom_doc_{session_id}")],
                [InlineKeyboardButton("◀️ К списку документов", callback_data=f"pch_gen_doc_{session_id}")]
            ])
        )
        return PCH_USER_REVIEW

    # Показываем результат
    text = (
        f"✅ <b>ДОКУМЕНТ СГЕНЕРИРОВАН</b>\n"
        f"📝 <b>По запросу:</b> <i>{user_request[:50]}{'...' if len(user_request) > 50 else ''}</i>\n"
        f"══════════════════════════════════════════\n\n"
        f"{sanitize_telegram_html(content[:3500])}\n\n"
        f"──────────────────────────────────────────"
    )

    keyboard = [
        [InlineKeyboardButton("📄 Создать ещё документ", callback_data=f"pch_gen_doc_{session_id}")],
        [InlineKeyboardButton("◀️ К этапу", callback_data=f"pch_show_stage_{session_id}")]
    ]

    await context.bot.edit_message_text(
        chat_id=progress_msg.chat.id,
        message_id=progress_msg.message_id,
        text=text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    # Очищаем временные данные
    context.user_data.pop('pch_custom_doc_session_id', None)

    return PCH_USER_REVIEW


async def view_generated_documents(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает список сгенерированных документов."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_view_docs_")[1])
    manager = ProsecutionComplaintManager(session_id)
    generated_docs = await manager.get_generated_documents()

    if not generated_docs:
        await query.answer("Документы ещё не генерировались.", show_alert=True)
        return PCH_EXPORT

    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    text = (
        f"📄 <b>ДОКУМЕНТЫ ЖАЛОБЫ {complaint_number}</b>\n"
        f"══════════════════════════════════════════\n\n"
    )

    for i, doc in enumerate(generated_docs, 1):
        doc_name = doc.get('type_display', doc.get('type'))
        created_at = doc.get('created_at', 'N/A')
        if isinstance(created_at, str) and 'T' in created_at:
            created_at = created_at.split('T')[0]
        text += f"<b>{i}. {doc_name}</b>\n   📅 {created_at}\n\n"

    text += "──────────────────────────────────────────\n"
    text += "<i>Выберите документ для просмотра:</i>"

    # Кнопки для каждого документа
    keyboard = []
    for i, doc in enumerate(generated_docs):
        doc_name = doc.get('type_display', doc.get('type'))
        keyboard.append([InlineKeyboardButton(
            f"📄 {doc_name}",
            callback_data=f"pch_show_doc_{session_id}_{i}"
        )])

    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")])

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_EXPORT


async def show_single_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает содержимое конкретного документа."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    # Парсим: pch_show_doc_{session_id}_{doc_index}
    parts = query.data.split("_")
    session_id = int(parts[3])
    doc_index = int(parts[4])

    manager = ProsecutionComplaintManager(session_id)
    generated_docs = await manager.get_generated_documents()

    if doc_index >= len(generated_docs):
        await query.answer("Документ не найден.", show_alert=True)
        return PCH_EXPORT

    doc = generated_docs[doc_index]
    doc_name = doc.get('type_display', doc.get('type'))
    content = doc.get('content', 'Содержимое не найдено')

    text = (
        f"📄 <b>{doc_name}</b>\n"
        f"══════════════════════════════════════════\n\n"
        f"{sanitize_telegram_html(content[:3800])}\n\n"
        f"──────────────────────────────────────────"
    )

    keyboard = [
        [InlineKeyboardButton("◀️ К списку документов", callback_data=f"pch_view_docs_{session_id}")]
    ]

    await safe_edit_message_text(
        query,
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    return PCH_EXPORT


# === ДОБАВЛЕНИЕ МАТЕРИАЛОВ ===

async def show_add_material_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает приглашение для добавления материалов."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_add_material_")[1])
    context.user_data['pch_adding_material'] = True

    await safe_edit_message_text(
        query,
        "📎 <b>Добавление материалов</b>\n\n"
        "Отправьте дополнительные материалы по делу:\n\n"
        "• Ссылки на доказательства\n"
        "• Показания свидетелей\n"
        "• Дополнительную информацию\n"
        "• Скриншоты (описанием)\n\n"
        "<i>Материалы будут добавлены к собранным данным.</i>",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    return PCH_ADD_MATERIAL


async def handle_add_material_or_custom_doc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обёртка для обработки текстового ввода в состоянии PCH_ADD_MATERIAL.
    Определяет, что делать: добавить материал или сгенерировать кастомный документ.
    """
    # Если есть pch_custom_doc_session_id — это запрос на кастомный документ
    if context.user_data.get('pch_custom_doc_session_id'):
        return await handle_custom_document_input(update, context)

    # Иначе — это добавление материала
    return await handle_material_input(update, context)


async def handle_material_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обрабатывает ввод дополнительных материалов."""
    material_text = update.message.text.strip()
    session_id = context.user_data.get('pch_session_id')

    if not session_id:
        await update.message.reply_text("❌ Ошибка сессии. Начните заново.")
        return ConversationHandler.END

    context.user_data['pch_adding_material'] = False

    manager = ProsecutionComplaintManager(session_id)

    # Добавляем материал к собранным данным
    collected = await manager.get_collected_data()
    materials = collected.get('additional_materials', [])
    materials.append({
        'content': material_text,
        'added_at': datetime.now().isoformat()
    })
    await manager.update_collected_data({'additional_materials': materials})

    await update.message.reply_text(
        "✅ <b>Материал добавлен!</b>\n\n"
        f"Всего добавлено материалов: {len(materials)}",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📎 Добавить ещё", callback_data=f"pch_add_material_{session_id}")],
            [InlineKeyboardButton("◀️ К этапу", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    return PCH_USER_REVIEW


# === КОНВЕРТАЦИЯ В УГОЛОВНОЕ ДЕЛО ===

async def handle_convert_to_ccw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Конвертирует жалобу в уголовное дело."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_convert_ccw_")[1])

    # Показываем подтверждение
    await safe_edit_message_text(
        query,
        "🔄 <b>Конвертация в уголовное дело</b>\n\n"
        "Все материалы жалобы будут переданы в систему создания уголовных дел (CCW).\n\n"
        "Продолжить?",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, конвертировать", callback_data=f"pch_confirm_ccw_{session_id}")],
            [InlineKeyboardButton("❌ Отмена", callback_data=f"pch_show_stage_{session_id}")]
        ])
    )

    return PCH_EXPORT


async def confirm_convert_to_ccw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Подтверждает и выполняет конвертацию."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_confirm_ccw_")[1])

    # Показываем индикатор
    await safe_edit_message_text(
        query,
        "⏳ <b>Создаю уголовное дело...</b>",
        parse_mode='HTML'
    )

    success, ccw_session_id, error = await convert_to_criminal_case(session_id, context)

    if not success:
        await safe_edit_message_text(
            query,
            f"❌ <b>Ошибка конвертации</b>\n\n{error}",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Повторить", callback_data=f"pch_convert_ccw_{session_id}")],
                [InlineKeyboardButton("◀️ Назад", callback_data=f"pch_show_stage_{session_id}")]
            ])
        )
        return PCH_EXPORT

    # Успешно создано
    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    await safe_edit_message_text(
        query,
        f"✅ <b>УГОЛОВНОЕ ДЕЛО СОЗДАНО!</b>\n\n"
        f"📋 Жалоба: <b>{complaint_number}</b>\n"
        f"📂 Уголовное дело: <b>#{ccw_session_id}</b>\n\n"
        "Материалы жалобы перенесены в уголовное дело.\n"
        "Перейдите в раздел «Уголовные дела» для продолжения работы.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📂 К уголовным делам", callback_data="start_criminal_case")],
            [InlineKeyboardButton("📜 К истории жалоб", callback_data="pch_history")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
        ])
    )

    return PCH_NAVIGATE


# === ЗАВЕРШЕНИЕ И ОТМЕНА ===

async def complete_session(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Завершает сессию обработки жалобы."""
    query = update.callback_query
    await safe_answer_callback_query(query)

    session_id = int(query.data.split("pch_complete_")[1])
    manager = ProsecutionComplaintManager(session_id)

    await manager.close_session('completed')

    # Удаляем запланированные задачи
    remove_pch_scheduled_jobs(context, session_id)

    session = await manager.load_session_data()
    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    await safe_edit_message_text(
        query,
        f"✅ <b>Жалоба {complaint_number} завершена!</b>\n\n"
        "Все материалы сохранены. Вы можете просмотреть их в истории жалоб.",
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 История жалоб", callback_data="pch_history")],
            [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
        ])
    )

    return ConversationHandler.END


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущую операцию и возвращает в главное меню."""
    query = update.callback_query
    if query:
        await safe_answer_callback_query(query)

        # Проверяем, есть ли session_id в callback_data
        if 'pch_cancel_' in query.data:
            session_id = int(query.data.split("pch_cancel_")[1])
            # Не закрываем сессию, просто выходим
            remove_pch_scheduled_jobs(context, session_id)

    # Очищаем user_data
    keys_to_remove = [k for k in context.user_data.keys() if k.startswith('pch_')]
    for key in keys_to_remove:
        del context.user_data[key]

    if query:
        await _display_main_menu(update, context)

    return ConversationHandler.END


# === ПЛАНИРОВЩИК АВТОЗАКРЫТИЯ ===

def schedule_pch_session_closure(context: ContextTypes.DEFAULT_TYPE, session_id: int, user_id: int) -> None:
    """Планирует автозакрытие сессии и уведомление."""
    # Планируем уведомление за 24 часа до закрытия
    warning_delay = timedelta(hours=PCH_SESSION_TIMEOUT_HOURS - PCH_WARNING_BEFORE_HOURS)
    context.job_queue.run_once(
        pch_expiry_warning_job,
        when=warning_delay,
        data={'session_id': session_id, 'user_id': user_id},
        name=f"pch_warning_{session_id}"
    )

    # Планируем автозакрытие
    closure_delay = timedelta(hours=PCH_SESSION_TIMEOUT_HOURS)
    context.job_queue.run_once(
        pch_auto_close_job,
        when=closure_delay,
        data={'session_id': session_id, 'user_id': user_id},
        name=f"pch_close_{session_id}"
    )

    logging.info(
        f"[PCH] Запланировано: уведомление через {warning_delay}, "
        f"закрытие через {closure_delay} для сессии #{session_id}"
    )


def remove_pch_scheduled_jobs(context: ContextTypes.DEFAULT_TYPE, session_id: int) -> None:
    """Удаляет запланированные задачи для сессии."""
    for job_name in [f"pch_warning_{session_id}", f"pch_close_{session_id}"]:
        jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in jobs:
            job.schedule_removal()


async def pch_expiry_warning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет уведомление о скором закрытии сессии."""
    job = context.job
    session_id = job.data['session_id']
    user_id = job.data['user_id']

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    # Проверяем, активна ли ещё сессия
    if not session or session.get('status') != 'active':
        return

    # Проверяем, не отправляли ли уже уведомление
    if session.get('expiry_warning_sent'):
        return

    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ <b>Внимание!</b>\n\n"
                 f"Сессия жалобы <b>{complaint_number}</b> будет автоматически закрыта "
                 f"через {PCH_WARNING_BEFORE_HOURS} часов.\n\n"
                 "Завершите работу с жалобой или она будет сохранена в текущем состоянии.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "▶️ Продолжить работу",
                    callback_data=f"pch_continue_{session_id}"
                )]
            ])
        )

        # Отмечаем, что уведомление отправлено
        await manager.update_session(expiry_warning_sent=True)
        logging.info(f"[PCH] Отправлено уведомление о закрытии сессии #{session_id}")

    except Exception as e:
        logging.warning(f"[PCH] Не удалось отправить уведомление пользователю {user_id}: {e}")


async def pch_auto_close_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Автоматически закрывает сессию по истечении времени."""
    job = context.job
    session_id = job.data['session_id']
    user_id = job.data['user_id']

    manager = ProsecutionComplaintManager(session_id)
    session = await manager.load_session_data()

    # Проверяем, активна ли ещё сессия
    if not session or session.get('status') != 'active':
        return

    complaint_number = session.get('complaint_number', 'PCH-XXXX')

    # Закрываем сессию
    await manager.close_session('expired')

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"❌ <b>Сессия закрыта</b>\n\n"
                 f"Жалоба <b>{complaint_number}</b> была автоматически закрыта "
                 f"по истечении {PCH_SESSION_TIMEOUT_HOURS} часов.\n\n"
                 "Вы можете просмотреть сохранённые материалы в истории жалоб.",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📜 История жалоб", callback_data="pch_history")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]
            ])
        )
        logging.info(f"[PCH] Сессия #{session_id} автоматически закрыта")

    except Exception as e:
        logging.warning(f"[PCH] Не удалось уведомить пользователя {user_id} о закрытии: {e}")


# === CONVERSATION HANDLER ===

def get_pch_conv_handler() -> ConversationHandler:
    """Создаёт ConversationHandler для Prosecution Complaint Handler."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
            CallbackQueryHandler(start_prosecution_complaint, pattern=r'^tool_pro_review_complaint$'),
            CallbackQueryHandler(show_history, pattern=r'^tool_pro_complaint_history$'),
            CallbackQueryHandler(handle_continue_session, pattern=r'^pch_continue_\d+$'),
        ],
        states={
            PCH_CHOOSING_PROJECT: [
                CallbackQueryHandler(select_pch_project, pattern=r'^pch_project_'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_CHOOSING_SERVER: [
                CallbackQueryHandler(select_pch_server, pattern=r'^pch_server_'),
                CallbackQueryHandler(select_pch_project, pattern=r'^pch_project_'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_AWAITING_URL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url_input),
                CallbackQueryHandler(replace_form_template, pattern=r'^pch_replace_form$'),
                CallbackQueryHandler(select_pch_project, pattern=r'^pch_project_'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_AWAITING_FORM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_form_input),
                CallbackQueryHandler(skip_form_input, pattern=r'^pch_skip_form$'),
                CallbackQueryHandler(select_pch_server, pattern=r'^pch_server_'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_PHASE_A: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_refinement_input),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_USER_REVIEW: [
                CallbackQueryHandler(handle_accept_stage, pattern=r'^pch_accept_\d+$'),
                CallbackQueryHandler(handle_refine_stage, pattern=r'^pch_refine_\d+$'),
                CallbackQueryHandler(handle_regenerate_stage, pattern=r'^pch_regenerate_\d+$'),
                CallbackQueryHandler(handle_prev_stage, pattern=r'^pch_prev_\d+$'),
                CallbackQueryHandler(handle_next_stage, pattern=r'^pch_next_\d+$'),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(show_document_selection, pattern=r'^pch_gen_doc_\d+$'),
                CallbackQueryHandler(show_custom_document_prompt, pattern=r'^pch_custom_doc_\d+$'),
                CallbackQueryHandler(handle_generate_document, pattern=r'^pch_doc_[a-z_]+_\d+$'),
                CallbackQueryHandler(show_add_material_prompt, pattern=r'^pch_add_material_\d+$'),
                CallbackQueryHandler(handle_convert_to_ccw, pattern=r'^pch_convert_ccw_\d+$'),
                CallbackQueryHandler(view_generated_documents, pattern=r'^pch_view_docs_\d+$'),
                CallbackQueryHandler(_show_completion_screen_handler, pattern=r'^pch_completion_\d+$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_ADD_MATERIAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_material_or_custom_doc),
                CallbackQueryHandler(show_document_selection, pattern=r'^pch_gen_doc_\d+$'),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_NAVIGATE: [
                CallbackQueryHandler(create_new_complaint, pattern=r'^pch_create_new$'),
                CallbackQueryHandler(show_history, pattern=r'^pch_history$'),
                CallbackQueryHandler(handle_continue_session, pattern=r'^pch_continue_\d+$'),
                CallbackQueryHandler(view_session_details, pattern=r'^pch_view_session_\d+$'),
                CallbackQueryHandler(start_prosecution_complaint, pattern=r'^start_prosecution_complaint$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
            PCH_EXPORT: [
                CallbackQueryHandler(view_generated_documents, pattern=r'^pch_view_docs_\d+$'),
                CallbackQueryHandler(show_single_document, pattern=r'^pch_show_doc_\d+_\d+$'),
                CallbackQueryHandler(handle_convert_to_ccw, pattern=r'^pch_convert_ccw_\d+$'),
                CallbackQueryHandler(confirm_convert_to_ccw, pattern=r'^pch_confirm_ccw_\d+$'),
                CallbackQueryHandler(complete_session, pattern=r'^pch_complete_\d+$'),
                CallbackQueryHandler(show_current_stage, pattern=r'^pch_show_stage_\d+$'),
                CallbackQueryHandler(show_history, pattern=r'^pch_history$'),
                CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
                CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(handle_cancel, pattern=r'^pch_cancel'),
            CallbackQueryHandler(handle_cancel, pattern=r'^main_menu$'),
            MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text_fallback),
        ],
        name="prosecution_complaint_handler",
        persistent=False,
        allow_reentry=True,
    )


# Экспортируем handler
pch_conv_handler = get_pch_conv_handler()


# END OF FILE user_handlers/prosecution_complaint_flow.py
