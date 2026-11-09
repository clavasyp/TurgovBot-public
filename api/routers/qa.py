# START OF FILE api/routers/qa.py

"""
API эндпоинты для RAG-ответов на юридические вопросы.
"""

import logging
import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status

from ..schemas import AskRequest, AskResponse, ErrorResponse
from ..dependencies import get_api_key_data

# Импортируем сервис (путь добавляется в sys.path в main.py)
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.qa_service import ask_question

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["QA"])


@router.post(
    "/ask",
    response_model=AskResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Bad request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Forbidden"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": ErrorResponse, "description": "Internal server error"},
        503: {"model": ErrorResponse, "description": "Service unavailable"},
    },
    summary="Задать юридический вопрос",
    description="Получить RAG-ответ на юридический вопрос с использованием базы законов сервера."
)
async def ask_legal_question(
    request: AskRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Эндпоинт для получения ответа на юридический вопрос.

    - **server_id**: ID сервера (например, 'majestic_1')
    - **question**: Вопрос пользователя (3-4000 символов)
    - **answer_type**: Тип ответа - 'detailed' или 'brief'
    """
    start_time = time.time()

    try:
        logger.info(
            f"[API/QA] Запрос от ключа {api_key['key_prefix']}: "
            f"server={request.server_id}, type={request.answer_type}, "
            f"question_len={len(request.question)}"
        )

        # Вызываем сервис для получения ответа
        answer = await ask_question(
            server_id=request.server_id,
            question=request.question,
            answer_type=request.answer_type
        )

        processing_time = (time.time() - start_time) * 1000

        logger.info(
            f"[API/QA] Ответ для ключа {api_key['key_prefix']}: "
            f"answer_len={len(answer)}, time={processing_time:.2f}ms"
        )

        return AskResponse(
            answer=answer,
            server_id=request.server_id,
            processing_time_ms=processing_time
        )

    except ValueError as e:
        # Ошибки валидации (неверный server_id и т.п.)
        logger.warning(f"[API/QA] Ошибка валидации: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        processing_time = (time.time() - start_time) * 1000
        logger.error(f"[API/QA] Ошибка обработки запроса: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error"
        )


# END OF FILE api/routers/qa.py
