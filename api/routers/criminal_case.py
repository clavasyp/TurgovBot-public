# START OF FILE api/routers/criminal_case.py

"""
API эндпоинты для Criminal Case Writer (создание уголовных дел).
"""

import logging
import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status, Path

from ..schemas import (
    CCWStartRequest, CCWStartResponse,
    CCWFactsRequest, CCWFactsResponse,
    CCWAnalyzeRequest, CCWAnalyzeResponse,
    CCWAnswerRequest, CCWAnswerResponse,
    CCWGenerateRequest, CCWGenerateResponse,
    CCWCaseResponse, CCWPartInfo,
    ErrorResponse
)
from ..dependencies import get_api_key_data

# Импортируем сервисы (путь добавляется в sys.path в main.py)
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.criminal_case_service import (
    create_ccw_session,
    add_facts_to_session,
    analyze_for_questions,
    save_phase_a_answers,
    generate_case_part,
    get_session_data,
    get_all_parts
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/criminal-case", tags=["Criminal Case Writer"])


@router.post(
    "/start",
    response_model=CCWStartResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Создать новую сессию уголовного дела",
    description="Создаёт новую сессию для составления уголовного дела."
)
async def start_criminal_case(
    request: CCWStartRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Создаёт новую сессию уголовного дела.

    - **server_id**: ID сервера
    - **case_number**: Номер дела (например, 'DJR-001')
    """
    try:
        logger.info(
            f"[API/CCW] Создание сессии от ключа {api_key['key_prefix']}: "
            f"server={request.server_id}, case={request.case_number}"
        )

        # Создаём сессию через сервис
        session_id, status_str = await create_ccw_session(
            owner_user_id=api_key['owner_user_id'],
            server_id=request.server_id,
            case_number=request.case_number
        )

        logger.info(f"[API/CCW] Создана сессия #{session_id}")

        return CCWStartResponse(
            session_id=session_id,
            status=status_str,
            case_number=request.case_number
        )

    except ValueError as e:
        logger.warning(f"[API/CCW] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"[API/CCW] Ошибка создания сессии: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post(
    "/{session_id}/facts",
    response_model=CCWFactsResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Добавить факты дела",
    description="Добавляет описание обстоятельств дела в сессию."
)
async def add_case_facts(
    request: CCWFactsRequest,
    session_id: int = Path(..., description="ID сессии"),
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Добавляет факты дела в существующую сессию.

    - **session_id**: ID сессии (из URL)
    - **facts**: Описание обстоятельств дела
    """
    try:
        logger.info(
            f"[API/CCW] Добавление фактов к сессии #{session_id} "
            f"от ключа {api_key['key_prefix']}"
        )

        # Проверяем, что сессия принадлежит владельцу ключа
        session = await get_session_data(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session['user_id'] != api_key['owner_user_id']:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

        # Добавляем факты
        await add_facts_to_session(session_id, request.facts)

        return CCWFactsResponse(
            session_id=session_id,
            status="facts_added",
            message="Факты дела успешно добавлены"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API/CCW] Ошибка добавления фактов: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post(
    "/{session_id}/analyze",
    response_model=CCWAnalyzeResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Анализ и уточняющие вопросы (Phase A)",
    description="AI анализирует контекст и возвращает уточняющие вопросы для улучшения качества генерации."
)
async def analyze_part(
    request: CCWAnalyzeRequest,
    session_id: int = Path(..., description="ID сессии"),
    api_key: dict = Depends(get_api_key_data)
):
    """
    Phase A: Анализ контекста и генерация уточняющих вопросов.

    Перед генерацией части дела рекомендуется вызвать этот эндпоинт,
    чтобы получить уточняющие вопросы от AI. Ответы на эти вопросы
    улучшат качество генерируемого документа.

    - **session_id**: ID сессии (из URL)
    - **part_type**: Тип части дела для анализа
    """
    start_time = time.time()

    try:
        logger.info(
            f"[API/CCW] Phase A для сессии #{session_id}, часть {request.part_type} "
            f"от ключа {api_key['key_prefix']}"
        )

        # Проверяем доступ
        session = await get_session_data(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session['user_id'] != api_key['owner_user_id']:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

        # Вызываем Phase A
        analysis, questions, has_questions = await analyze_for_questions(session_id, request.part_type)

        processing_time = (time.time() - start_time) * 1000

        logger.info(
            f"[API/CCW] Phase A завершён для сессии #{session_id}: "
            f"{len(questions)} вопросов, time={processing_time:.2f}ms"
        )

        return CCWAnalyzeResponse(
            session_id=session_id,
            part_type=request.part_type,
            analysis=analysis,
            questions=questions,
            has_questions=has_questions,
            processing_time_ms=processing_time
        )

    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"[API/CCW] Ошибка валидации в Phase A: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[API/CCW] Runtime ошибка в Phase A: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[API/CCW] Ошибка Phase A: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post(
    "/{session_id}/answer",
    response_model=CCWAnswerResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Отправить ответы на уточняющие вопросы",
    description="Сохраняет ответы пользователя на уточняющие вопросы для улучшения генерации."
)
async def submit_answers(
    request: CCWAnswerRequest,
    session_id: int = Path(..., description="ID сессии"),
    api_key: dict = Depends(get_api_key_data)
):
    """
    Отправляет ответы на уточняющие вопросы Phase A.

    После получения уточняющих вопросов через /analyze, отправьте ответы
    через этот эндпоинт. Затем вызовите /generate для получения
    улучшенного черновика.

    - **session_id**: ID сессии (из URL)
    - **part_type**: Тип части дела
    - **answers**: Список ответов на уточняющие вопросы
    """
    try:
        logger.info(
            f"[API/CCW] Сохранение ответов для сессии #{session_id}, часть {request.part_type} "
            f"от ключа {api_key['key_prefix']}: {len(request.answers)} ответов"
        )

        # Проверяем доступ
        session = await get_session_data(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session['user_id'] != api_key['owner_user_id']:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

        # Сохраняем ответы
        await save_phase_a_answers(session_id, request.part_type, request.answers)

        return CCWAnswerResponse(
            session_id=session_id,
            part_type=request.part_type,
            status="answers_saved",
            message=f"Сохранено {len(request.answers)} ответов. Теперь вызовите /generate для создания черновика."
        )

    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"[API/CCW] Ошибка валидации ответов: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"[API/CCW] Ошибка сохранения ответов: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.post(
    "/{session_id}/generate",
    response_model=CCWGenerateResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Сгенерировать часть дела",
    description="Генерирует указанную часть уголовного дела с помощью AI."
)
async def generate_part(
    request: CCWGenerateRequest,
    session_id: int = Path(..., description="ID сессии"),
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Генерирует часть уголовного дела.

    - **session_id**: ID сессии
    - **part_type**: Тип части (inventory, accused_card, etc.)
    """
    start_time = time.time()

    try:
        logger.info(
            f"[API/CCW] Генерация части {request.part_type} для сессии #{session_id} "
            f"от ключа {api_key['key_prefix']}"
        )

        # Проверяем доступ
        session = await get_session_data(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session['user_id'] != api_key['owner_user_id']:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

        # Генерируем часть
        content, version = await generate_case_part(session_id, request.part_type)

        processing_time = (time.time() - start_time) * 1000

        logger.info(
            f"[API/CCW] Сгенерирована часть {request.part_type} v{version} "
            f"для сессии #{session_id}, time={processing_time:.2f}ms"
        )

        return CCWGenerateResponse(
            session_id=session_id,
            part_type=request.part_type,
            content=content,
            version=version,
            processing_time_ms=processing_time
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API/CCW] Ошибка генерации части: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get(
    "/{session_id}",
    response_model=CCWCaseResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Получить данные сессии",
    description="Возвращает полную информацию о сессии уголовного дела со всеми частями."
)
async def get_criminal_case(
    session_id: int = Path(..., description="ID сессии"),
    api_key: Dict = Depends(get_api_key_data)
):
    """
    Получает полные данные сессии уголовного дела.

    - **session_id**: ID сессии
    """
    try:
        logger.info(
            f"[API/CCW] Запрос данных сессии #{session_id} "
            f"от ключа {api_key['key_prefix']}"
        )

        # Получаем данные сессии
        session = await get_session_data(session_id)
        if not session:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

        if session['user_id'] != api_key['owner_user_id']:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

        # Получаем все части
        parts_data = await get_all_parts(session_id)

        # Формируем ответ
        parts = {}
        for part in parts_data:
            parts[part['part_type']] = CCWPartInfo(
                part_type=part['part_type'],
                content=part.get('content'),
                version=part.get('version', 0),
                is_final=part.get('is_final', False)
            )

        return CCWCaseResponse(
            session_id=session_id,
            case_number=session.get('case_number', ''),
            server_id=session.get('server_id', ''),
            status=session.get('status', 'unknown'),
            created_at=session['created_at'],
            parts=parts
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API/CCW] Ошибка получения сессии: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# END OF FILE api/routers/criminal_case.py
