# START OF FILE services/interview_service.py

"""
Сервис генерации вопросов для собеседований на госслужбу RP-серверов.

Извлечённая бизнес-логика из user_handlers/interview_flow.py
(process_and_send_interview_questions) для использования и в Telegram-боте,
и в веб-API (/v1/web/interview). Без зависимостей от python-telegram-bot:
параметры передаются явно (никакого context.user_data), отправка/история — на стороне
вызывающего слоя (роутер/хендлер).
"""

import logging

from google.genai import types, errors as genai_errors
from google.api_core import exceptions as google_exceptions

from database.manager import db_manager
import config
from config import (
    INTERVIEW_ROLES, INTERVIEW_DIFFICULTIES, MIN_QUESTIONS, MAX_QUESTIONS,
    QUESTION_GENERATOR_MODEL_NAME, SERVERS_CONFIG,
)
from prompts import get_interview_questions_prompt
from rate_limiter import (
    execute_vertex_request, RequestPriority, get_current_client,
    mark_current_region_success, mark_current_region_error,
)
from utils import reinitialize_vertex_ai

logger = logging.getLogger(__name__)


async def generate_interview_questions(
    server_id: str,
    role_key: str,
    difficulty_key: str,
    quantity: int,
) -> str:
    """
    Генерирует список вопросов для собеседования по всей базе законов сервера.

    Args:
        server_id: ID сервера (например, 'majestic_1')
        role_key: ключ должности из INTERVIEW_ROLES ('prosecutor' | 'advocate')
        difficulty_key: ключ сложности из INTERVIEW_DIFFICULTIES ('easy' | 'medium' | 'hard')
        quantity: число вопросов (MIN_QUESTIONS..MAX_QUESTIONS)

    Returns:
        Текст с нумерованным списком вопросов (как отдаёт модель).

    Raises:
        ValueError: невалидные параметры / неизвестный сервер / пустая база знаний
        RuntimeError: ошибка аутентификации Vertex AI или перегрузка AI (429)
    """
    # --- Валидация параметров (раньше делал ConversationHandler/хендлер) ---
    if server_id not in SERVERS_CONFIG:
        raise ValueError(f"Неизвестный сервер: {server_id}")
    if role_key not in INTERVIEW_ROLES:
        raise ValueError("Неизвестная должность")
    if difficulty_key not in INTERVIEW_DIFFICULTIES:
        raise ValueError("Неизвестный уровень сложности")
    if not (MIN_QUESTIONS <= quantity <= MAX_QUESTIONS):
        raise ValueError(f"Количество вопросов должно быть от {MIN_QUESTIONS} до {MAX_QUESTIONS}")

    role_name = INTERVIEW_ROLES[role_key]
    diff_name = INTERVIEW_DIFFICULTIES[difficulty_key]
    server_name = config.server_display_name(server_id)

    logger.info(
        "[INTERVIEW_SERVICE] Генерация %s вопросов: server=%s role=%s diff=%s",
        quantity, server_id, role_key, difficulty_key,
    )

    # --- Инициализация Vertex AI ---
    if not await reinitialize_vertex_ai():
        raise RuntimeError("Ошибка аутентификации Vertex AI")

    # --- Все законы сервера (роутера нет — берём всю базу, как в боте) ---
    law_records = await db_manager.fetch(
        "SELECT gemini_file_uri FROM law_documents WHERE server_id = $1 AND gemini_file_uri IS NOT NULL",
        server_id,
    )
    files_for_query = [r['gemini_file_uri'] for r in law_records]
    if not files_for_query:
        raise ValueError(f"База знаний для сервера '{server_name}' пуста или временно недоступна")

    # --- Промпт + контент ---
    system_prompt = get_interview_questions_prompt(role_name, diff_name, quantity, server_name)
    content_parts = [types.Part.from_uri(file_uri=uri, mime_type="text/plain") for uri in files_for_query]

    async def send_interview_request():
        client = get_current_client()
        if not client:
            raise RuntimeError("genai_client не инициализирован")
        return await client.aio.models.generate_content(
            model=QUESTION_GENERATOR_MODEL_NAME,
            contents=content_parts,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )

    # Генерация вопросов — не критичная задача → NORMAL (как в боте).
    try:
        response = await execute_vertex_request(
            func=send_interview_request,
            model_name=QUESTION_GENERATOR_MODEL_NAME,
            priority=RequestPriority.NORMAL,
            max_retries=3,
        )
        await mark_current_region_success()
    except (google_exceptions.ResourceExhausted, genai_errors.ClientError, TimeoutError) as e:
        await mark_current_region_error()
        # Не-429 ClientError пробрасываем как есть (вызывающий замапит в 503 и вернёт лимит).
        if isinstance(e, genai_errors.ClientError):
            error_str = str(e)
            if '429' not in error_str and 'RESOURCE_EXHAUSTED' not in error_str:
                raise
        logger.error("[INTERVIEW_SERVICE] AI перегружен: %s", e)
        raise RuntimeError("AI-сервис временно перегружен. Попробуйте позже.")

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        logger.warning("[INTERVIEW_SERVICE] AI вернул пустой ответ (safety/MAX_TOKENS/нет текста)")
        raise RuntimeError("AI не вернул вопросы, попробуйте ещё раз")
    logger.info("[INTERVIEW_SERVICE] Сгенерировано, длина=%s", len(text))
    return text


# END OF FILE services/interview_service.py
