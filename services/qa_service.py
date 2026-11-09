# START OF FILE services/qa_service.py

"""
Сервис для RAG-ответов на юридические вопросы.

Извлечённая бизнес-логика из user_handlers/qa_flow.py
для использования как в Telegram-боте, так и в REST API.
"""

import asyncio
import logging
from typing import List, Dict, Optional

from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions

from database.manager import db_manager
from config import (
    ROUTER_MODEL_NAME, LEGAL_CONSULTANT_MODEL_NAME,
    SERVERS_CONFIG
)
from prompts import get_system_prompt, get_router_prompt
from rate_limiter import execute_vertex_request, RequestPriority, get_current_client, mark_current_region_success, mark_current_region_error
from precedents_manager import get_relevant_precedents
from utils import reinitialize_vertex_ai

logger = logging.getLogger(__name__)


async def _get_relevant_laws(user_question: str, server_id: str) -> List[Dict]:
    """
    Вызывает AI-роутер для выбора релевантных законов.

    Args:
        user_question: Вопрос пользователя
        server_id: ID сервера

    Returns:
        Список словарей с title и uri релевантных законов
    """
    start_time = asyncio.get_running_loop().time()
    logger.info(f"[QA_SERVICE] Запуск модели-фильтра ({ROUTER_MODEL_NAME}) для сервера {server_id}...")

    try:
        law_records = await db_manager.fetch(
            "SELECT title, gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )
        if not law_records:
            logger.warning(f"[QA_SERVICE] В БД отсутствуют документы для сервера {server_id}.")
            return []

        law_map_by_index = {
            i + 1: {'title': r['title'], 'uri': r['gemini_file_uri']}
            for i, r in enumerate(law_records)
        }
        prompt_lines = [f"{i + 1}. {r['title']}" for i, r in enumerate(law_records)]
        law_titles_str = "\n".join(prompt_lines)

        async def make_router_request():
            client = get_current_client()
            if not client:
                raise RuntimeError("genai_client не инициализирован")
            return await client.aio.models.generate_content(
                model=ROUTER_MODEL_NAME,
                contents=get_router_prompt(user_question, law_titles_str)
            )

        response = await execute_vertex_request(
            func=make_router_request,
            model_name=ROUTER_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3
        )

        await mark_current_region_success()

        selected_indices = {int(line.strip()) for line in (response.text or '').split('\n') if line.strip().isdigit()}
        relevant_laws = [law_map_by_index[index] for index in sorted(selected_indices) if index in law_map_by_index]

        duration = (asyncio.get_running_loop().time() - start_time) * 1000
        logger.info(f"[QA_SERVICE] Модель-фильтр выбрала {len(relevant_laws)} законов за {duration:.2f} мс.")
        return relevant_laws

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()

        if isinstance(e, genai_errors.ClientError):
            error_str = str(e)
            if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                raise

        logger.warning(f"[QA_SERVICE] Превышен лимит запросов при фильтрации законов: {e}")
        return []
    except Exception as e:
        logger.error(f"[QA_SERVICE] Ошибка модели-фильтра.", exc_info=e)
        return []


async def _get_relevant_precedents_safe(user_question: str, server_id: str) -> List[Dict]:
    """
    Безопасно получает релевантные прецеденты.

    Returns:
        Список словарей с uri прецедентов или пустой список при ошибке
    """
    try:
        client = get_current_client()
        if not client:
            return []

        precedents = await get_relevant_precedents(client, user_question, server_id)
        if precedents:
            logger.info(f"[QA_SERVICE] Добавлено {len(precedents)} прецедентов к контексту")
        return precedents
    except Exception as e:
        logger.warning(f"[QA_SERVICE] Ошибка получения прецедентов: {e}")
        return []


async def ask_question(
    server_id: str,
    question: str,
    answer_type: str = "detailed",
    history: Optional[str] = None
) -> str:
    """
    Основная функция для получения RAG-ответа на юридический вопрос.

    Args:
        server_id: ID сервера (например, 'majestic_1')
        question: Вопрос пользователя
        answer_type: Тип ответа - 'detailed' или 'brief'
        history: Опциональный контекст прошлых реплик (для follow-up вопросов из оверлея).
                 Подмешивается отдельным текстовым блоком ПЕРЕД вопросом; на выбор законов
                 (роутер) не влияет — там по-прежнему идёт сам вопрос.

    Returns:
        Текст ответа AI

    Raises:
        ValueError: Если server_id невалидный или база знаний пуста
        RuntimeError: При критической ошибке AI
    """
    logger.info(f"[QA_SERVICE] Обработка вопроса для сервера {server_id}, тип={answer_type}")

    # Проверяем server_id
    if server_id not in SERVERS_CONFIG:
        raise ValueError(f"Неизвестный сервер: {server_id}")

    # Инициализируем Vertex AI
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # 1. Получаем релевантные законы
    relevant_laws = await _get_relevant_laws(question, server_id)

    # Если роутер не вернул результаты, используем все законы
    if relevant_laws:
        files_uris = [item['uri'] for item in relevant_laws]
    else:
        records = await db_manager.fetch(
            "SELECT gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
            server_id
        )
        files_uris = [r['gemini_file_uri'] for r in records]

    if not files_uris:
        server_name = SERVERS_CONFIG[server_id].get('display_name', server_id)
        raise ValueError(f"База знаний для сервера '{server_name}' пуста")

    # 2. Получаем релевантные прецеденты
    relevant_precedents = await _get_relevant_precedents_safe(question, server_id)

    # 3. Формируем контент для запроса
    content_parts = [
        types.Part.from_uri(file_uri=uri, mime_type="text/plain")
        for uri in files_uris
    ]

    # Добавляем прецеденты
    for precedent in relevant_precedents:
        content_parts.append(types.Part.from_uri(file_uri=precedent['uri'], mime_type="text/plain"))

    # Контекст прошлых реплик (follow-up из оверлея) — отдельным блоком перед вопросом.
    if history:
        content_parts.append(types.Part(
            text="Контекст этого разговора (предыдущие реплики, для уточняющих вопросов):\n" + history
        ))

    # Добавляем вопрос
    content_parts.append(types.Part(text=question))

    # Формируем запрос
    current_message = types.Content(role='user', parts=content_parts)
    full_contents = [current_message]

    # 4. Отправляем запрос к AI
    async def send_main_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=LEGAL_CONSULTANT_MODEL_NAME,
            contents=full_contents,
            config=types.GenerateContentConfig(
                system_instruction=get_system_prompt(answer_type)
            )
        )

    try:
        response = await execute_vertex_request(
            func=send_main_request,
            model_name=LEGAL_CONSULTANT_MODEL_NAME,
            priority=RequestPriority.CRITICAL,
            max_retries=3
        )
        await mark_current_region_success()

        # response.text у google.genai может быть None (safety/MAX_TOKENS/нет текстовых частей) — гард.
        answer = (getattr(response, "text", None) or "").strip()
        if not answer:
            logger.warning("[QA_SERVICE] AI вернул пустой ответ (safety/MAX_TOKENS/нет текста)")
            raise RuntimeError("AI не вернул ответ, попробуйте переформулировать вопрос")
        logger.info(f"[QA_SERVICE] Ответ получен, длина={len(answer)}")
        return answer

    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()

        if isinstance(e, genai_errors.ClientError):
            error_str = str(e)
            if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                raise

        logger.error(f"[QA_SERVICE] AI-сервис перегружен: {e}")
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")


# END OF FILE services/qa_service.py
