# START OF FILE services/criminal_case_service.py

"""
Сервис для создания уголовных дел (Criminal Case Writer).

Извлечённая бизнес-логика из user_handlers/criminal_case_flow.py
для использования как в Telegram-боте, так и в REST API.
"""

import json
import logging
from typing import Optional, Dict, Tuple, List
import re

from google.genai import errors as genai_errors
from google.api_core import exceptions as google_exceptions

from database.manager import db_manager
from config import (
    CCW_MODEL_NAME, SERVERS_CONFIG,
    CRIMINAL_CASE_PARTS_ORDER, CRIMINAL_CASE_PARTS_DISPLAY
)
from prompts import get_ccw_draft_generator_prompt, get_ccw_context_analyzer_prompt
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error
from utils import reinitialize_vertex_ai

logger = logging.getLogger(__name__)


# === УПРАВЛЕНИЕ СЕССИЯМИ ===

async def create_ccw_session(
    owner_user_id: int,
    server_id: str,
    case_number: str
) -> Tuple[int, str]:
    """
    Создаёт новую сессию уголовного дела.

    Args:
        owner_user_id: Telegram ID владельца
        server_id: ID сервера
        case_number: Номер дела (например, 'DJR-001')

    Returns:
        Tuple[int, str]: (session_id, status)

    Raises:
        ValueError: Если server_id невалидный
    """
    # Проверяем server_id
    if server_id not in SERVERS_CONFIG:
        raise ValueError(f"Неизвестный сервер: {server_id}")

    query = """
        INSERT INTO criminal_case_sessions (user_id, server_id, case_number, collected_data, status)
        VALUES ($1, $2, $3, '{}', 'active')
        RETURNING id
    """
    session_id = await db_manager.fetchval(query, owner_user_id, server_id, case_number)

    logger.info(f"[CCW_SERVICE] Создана сессия #{session_id} для пользователя {owner_user_id}, сервер {server_id}")

    return session_id, 'active'


async def get_session_data(session_id: int) -> Optional[Dict]:
    """
    Получает данные сессии из БД.

    Returns:
        Dict с данными сессии или None если не найдена
    """
    query = """
        SELECT id, user_id, case_number, server_id, current_part_index,
               collected_data, status, google_doc_id, google_doc_url,
               created_at, updated_at
        FROM criminal_case_sessions 
        WHERE id = $1
    """
    row = await db_manager.fetchrow(query, session_id)
    if row:
        result = dict(row)
        # Парсим collected_data из JSON
        if result.get('collected_data'):
            if isinstance(result['collected_data'], str):
                result['collected_data'] = json.loads(result['collected_data'])
        else:
            result['collected_data'] = {}
        return result
    return None


async def add_facts_to_session(session_id: int, facts: str) -> None:
    """
    Добавляет факты дела в сессию.

    Args:
        session_id: ID сессии
        facts: Описание обстоятельств дела
    """
    # Получаем текущие данные
    session = await get_session_data(session_id)
    if not session:
        raise ValueError(f"Сессия {session_id} не найдена")

    # Обновляем collected_data
    collected_data = session.get('collected_data', {})
    collected_data['initial_facts'] = facts

    # Сохраняем
    query = """
        UPDATE criminal_case_sessions
        SET collected_data = $2, updated_at = NOW()
        WHERE id = $1
    """
    await db_manager.execute(query, session_id, json.dumps(collected_data))

    logger.info(f"[CCW_SERVICE] Факты добавлены к сессии #{session_id}")


async def get_all_parts(session_id: int) -> List[Dict]:
    """
    Получает все части дела для сессии.

    Returns:
        Список словарей с данными частей (последние версии)
    """
    query = """
        SELECT DISTINCT ON (part_type) 
               part_type, content, version, is_final, ai_review_notes, created_at
        FROM criminal_case_parts
        WHERE session_id = $1
        ORDER BY part_type, version DESC
    """
    rows = await db_manager.fetch(query, session_id)
    return [dict(row) for row in rows]


async def save_part_version(
    session_id: int,
    part_type: str,
    content: str,
    ai_review_notes: str = None,
    is_final: bool = False
) -> Tuple[int, int]:
    """
    Сохраняет версию части дела.

    Returns:
        Tuple[int, int]: (part_id, version)
    """
    # Получаем текущую максимальную версию
    query = """
        SELECT COALESCE(MAX(version), 0) FROM criminal_case_parts
        WHERE session_id = $1 AND part_type = $2
    """
    max_version = await db_manager.fetchval(query, session_id, part_type)
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
        session_id, part_type, new_version, content, ai_review_notes, is_final
    )

    logger.info(f"[CCW_SERVICE] Сохранена часть {part_type} v{new_version} для сессии #{session_id}")
    return part_id, new_version


# === ГЕНЕРАЦИЯ ЧАСТЕЙ ДЕЛА ===

async def _get_relevant_laws_for_ccw(server_id: str, facts: str) -> str:
    """
    Получает релевантные законы для генерации дела.

    Returns:
        Строка с текстами релевантных законов
    """
    try:
        records = await db_manager.fetch(
            "SELECT title, content FROM law_documents WHERE server_id = $1 AND content IS NOT NULL LIMIT 20",
            server_id
        )
        if not records:
            return "Законы не найдены в базе."

        laws_text = "\n\n".join([
            f"=== {r['title']} ===\n{r['content'][:3000]}"
            for r in records
        ])
        return laws_text
    except Exception as e:
        logger.warning(f"[CCW_SERVICE] Ошибка получения законов: {e}")
        return "Ошибка загрузки законов."


async def analyze_for_questions(
    session_id: int,
    part_type: str
) -> Tuple[str, List[str], bool]:
    """
    Phase A: Анализирует контекст и генерирует уточняющие вопросы.

    Args:
        session_id: ID сессии
        part_type: Тип части (inventory, accused_card, etc.)

    Returns:
        Tuple[str, List[str], bool]: (analysis_text, questions_list, has_questions)

    Raises:
        ValueError: Если сессия не найдена или part_type невалидный
        RuntimeError: При ошибке AI
    """
    from google.genai import types

    # Проверяем part_type
    if part_type not in CRIMINAL_CASE_PARTS_ORDER:
        raise ValueError(f"Неизвестный тип части: {part_type}")

    # Получаем данные сессии
    session = await get_session_data(session_id)
    if not session:
        raise ValueError(f"Сессия {session_id} не найдена")

    collected_data = session.get('collected_data', {})
    facts = collected_data.get('initial_facts', '')

    if not facts:
        raise ValueError("Не указаны факты дела. Сначала добавьте факты.")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)
    case_number = session.get('case_number', 'DJR-XXX')
    server_id = session.get('server_id', '')

    # Получаем релевантные законы
    relevant_laws = await _get_relevant_laws_for_ccw(server_id, facts)

    # Формируем промпт для Phase A
    prompt = get_ccw_context_analyzer_prompt(
        part_name=part_name,
        case_number=case_number,
        collected_data=json.dumps(collected_data, ensure_ascii=False, indent=2),
        relevant_laws=relevant_laws
    )

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

    try:
        response = await execute_vertex_request(
            func=make_phase_a_request,
            model_name=CCW_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )
        await mark_current_region_success()

        analysis_text = response.text

        # Извлекаем вопросы из ответа
        questions = []
        has_questions = '❓' in analysis_text or 'вопрос' in analysis_text.lower()

        # Пытаемся извлечь конкретные вопросы (строки с ?)
        question_lines = re.findall(r'[^\n]*\?[^\n]*', analysis_text)
        for q in question_lines:
            q = q.strip()
            if len(q) > 10 and q.endswith('?'):
                questions.append(q)

        logger.info(f"[CCW_SERVICE] Phase A для сессии #{session_id}, часть {part_type}: {len(questions)} вопросов")

        return analysis_text, questions, has_questions

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        logger.error(f"[CCW_SERVICE] AI-сервис перегружен в Phase A: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


async def save_phase_a_answers(
    session_id: int,
    part_type: str,
    answers: List[str]
) -> None:
    """
    Сохраняет ответы на уточняющие вопросы Phase A.

    Args:
        session_id: ID сессии
        part_type: Тип части
        answers: Список ответов на вопросы
    """
    # Получаем текущие данные
    session = await get_session_data(session_id)
    if not session:
        raise ValueError(f"Сессия {session_id} не найдена")

    # Обновляем collected_data
    collected_data = session.get('collected_data', {})

    if 'phase_a_answers' not in collected_data:
        collected_data['phase_a_answers'] = {}

    # До��авляем/заменяем ответы для данной час��и
    collected_data['phase_a_answers'][part_type] = answers

    # Сохраняем
    query = """
        UPDATE criminal_case_sessions
        SET collected_data = $2, updated_at = NOW()
        WHERE id = $1
    """
    await db_manager.execute(query, session_id, json.dumps(collected_data))

    logger.info(f"[CCW_SERVICE] Сохранены ответы Phase A для сессии #{session_id}, часть {part_type}: {len(answers)} ответов")


async def generate_case_part(
    session_id: int,
    part_type: str
) -> Tuple[str, int]:
    """
    Генерирует часть уголовного дела с помощью AI.

    Args:
        session_id: ID сессии
        part_type: Тип части (inventory, accused_card, etc.)

    Returns:
        Tuple[str, int]: (content, version)

    Raises:
        ValueError: Если сессия не найдена или part_type невалидный
        RuntimeError: При ошибке AI
    """
    # Проверяем part_type
    if part_type not in CRIMINAL_CASE_PARTS_ORDER:
        raise ValueError(f"Неизвестный тип части: {part_type}")

    # Получаем данные сессии
    session = await get_session_data(session_id)
    if not session:
        raise ValueError(f"Сессия {session_id} не найдена")

    collected_data = session.get('collected_data', {})
    facts = collected_data.get('initial_facts', '')

    if not facts:
        raise ValueError("Не указаны факты дела. Сначала добавьте факты через /facts")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    part_name = CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type)
    case_number = session.get('case_number', 'DJR-XXX')
    server_id = session.get('server_id', '')

    # Получаем релевантные законы
    relevant_laws = await _get_relevant_laws_for_ccw(server_id, facts)

    # Получаем ответы на уточняющие вопросы (если есть)
    user_answers = collected_data.get('phase_a_answers', {}).get(part_type, [])
    user_answers_text = '\n'.join(user_answers) if user_answers else 'Нет дополнительных ответов'

    # Формируем промпт для генерации
    prompt = get_ccw_draft_generator_prompt(
        part_name=part_name,
        case_number=case_number,
        collected_data=json.dumps(collected_data, ensure_ascii=False, indent=2),
        user_answers=user_answers_text,
        relevant_laws=relevant_laws,
        reference_formulations=""  # Пока без референсов
    )

    # Генерируем черновик
    async def send_generation_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=CCW_MODEL_NAME,
            contents=prompt
        )

    try:
        response = await execute_vertex_request(
            func=send_generation_request,
            model_name=CCW_MODEL_NAME,
            priority=RequestPriority.CRITICAL,
            max_retries=3
        )
        await mark_current_region_success()

        content = response.text

        # Сохраняем версию
        part_id, version = await save_part_version(
            session_id=session_id,
            part_type=part_type,
            content=content,
            is_final=False
        )

        logger.info(f"[CCW_SERVICE] Сгенерирована часть {part_type} v{version} для сессии #{session_id}")

        return content, version

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()

        if isinstance(e, genai_errors.ClientError):
            error_str = str(e)
            if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                raise

        logger.error(f"[CCW_SERVICE] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


# END OF FILE services/criminal_case_service.py
