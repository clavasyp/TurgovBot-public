# START OF FILE api/routers/web_tools.py

"""
Веб-инструменты сайта TurgovBot (/v1/web/*): генератор вопросов, далее — жалобы/иски и
уголовный писатель. Те же лимиты/аккаунт, что у бота; аутентификация — cookie-сессия
(get_current_web_user), CSRF — web_origin_guard на мутирующих эндпоинтах.

ВНИМАНИЕ: user_handlers.core (лимитер) импортируется ЛЕНИВО внутри обработчиков —
импорт на уровне модуля замыкает цикл api↔user_handlers↔law_loophole (см. web_auth.py).
"""

import logging
import os
import re
import sys
import time
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, status

# Корень проекта добавлен в sys.path в api/main.py; дублируем для надёжности импортов сервисов.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ..dependencies import get_current_web_user
from .web_auth import web_origin_guard, _ERR_RESPONSES
from ..schemas import (
    WebOption, WebInterviewOptions, WebInterviewRequest, WebInterviewResponse, WebInterviewQuestion,
    WebCriminalStartRequest, WebCriminalStartResponse, WebCriminalFactsRequest,
    WebCriminalGenerateRequest, WebCriminalGenerateResponse, WebCriminalCaseResponse, WebCriminalPart,
    WebComplaintOptions, WebComplaintRequest, WebComplaintResponse,
)
from services.interview_service import generate_interview_questions
from services.criminal_case_service import (
    create_ccw_session, get_session_data, add_facts_to_session,
    generate_case_part, get_all_parts,
)
from services.complaint_service import generate_complaint
from database.manager import db_manager
import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/web", tags=["Web Tools"])

# Строка-вопрос вида "1. ...", "2) ..." — для разбора ответа модели в список.
_NUM_RE = re.compile(r'^\s*(\d+)\s*[\.\)]\s*(.+)$')


def _parse_questions(text: str) -> List[WebInterviewQuestion]:
    """Разбирает нумерованный список модели в структуру. Если нумерации нет — пустой список
    (фронт отрисует сырой текст)."""
    out: List[WebInterviewQuestion] = []
    for line in (text or "").splitlines():
        m = _NUM_RE.match(line)
        if m:
            q = m.group(2).strip()
            if q:
                out.append(WebInterviewQuestion(number=int(m.group(1)), text=q))
    return out


# ============================ Генератор вопросов ============================

@router.get(
    "/interview/options",
    response_model=WebInterviewOptions,
    responses=_ERR_RESPONSES,
    summary="Опции генератора (роли, сложности, диапазон количества)",
)
async def interview_options(user: Dict = Depends(get_current_web_user)):
    return WebInterviewOptions(
        roles=[WebOption(key=k, label=v) for k, v in config.INTERVIEW_ROLES.items()],
        difficulties=[WebOption(key=k, label=v) for k, v in config.INTERVIEW_DIFFICULTIES.items()],
        min_questions=config.MIN_QUESTIONS,
        max_questions=config.MAX_QUESTIONS,
    )


@router.post(
    "/interview",
    response_model=WebInterviewResponse,
    responses=_ERR_RESPONSES,
    summary="Сгенерировать вопросы для собеседования (как у бота)",
    description="Лимит/движок — как у Telegram-бота: списываем дневной лимит 'interview' → "
                "interview_service → при ошибке возвращаем лимит. Пишется в историю как генерация.",
)
async def interview(request: WebInterviewRequest, user: Dict = Depends(get_current_web_user),
                    _csrf: None = Depends(web_origin_guard)):
    user_id = user["user_id"]
    start = time.time()

    # Лимитер бота — лениво (общий аккаунт/счётчики; см. комментарий к импортам).
    from user_handlers.core import check_and_update_usage, refund_usage

    # 1. Дневной лимит генераций (как у бота).
    allowed, _ = await check_and_update_usage(user_id, 'interview')
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Дневной лимит генераций вопросов исчерпан. Лимиты сбрасываются ежедневно.",
        )

    # 2. Генерация. При любой ошибке — возвращаем списанный лимит.
    try:
        text = await generate_interview_questions(
            request.server_id, request.role, request.difficulty, request.quantity
        )
    except ValueError as e:
        await refund_usage(user_id, 'interview')
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        await refund_usage(user_id, 'interview')
        logger.warning("[WEB] interview error user_id=%s server=%s: %s", user_id, request.server_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно недоступен — попробуйте через минуту.",
        )

    # 3. История генераций (как бот: user-реплика с GENERATION_HISTORY_PREFIX + model-ответ).
    try:
        role_name = config.INTERVIEW_ROLES.get(request.role, request.role)
        diff_name = config.INTERVIEW_DIFFICULTIES.get(request.difficulty, request.difficulty)
        req_line = (
            f"{config.GENERATION_HISTORY_PREFIX}Запрос на генерацию вопросов: "
            f"{role_name} ({diff_name}, {request.quantity} шт.)"
        )
        await db_manager.execute(
            "INSERT INTO message_history (user_id, server_id, role, content) VALUES ($1, $2, 'user', $3);",
            user_id, request.server_id, req_line,
        )
        await db_manager.execute(
            "INSERT INTO message_history (user_id, server_id, role, content) VALUES ($1, $2, 'model', $3);",
            user_id, request.server_id, text,
        )
    except Exception as e:
        logger.warning("[WEB] не удалось записать историю генерации user_id=%s: %s", user_id, e)

    return WebInterviewResponse(
        text=text,
        questions=_parse_questions(text),
        server_id=request.server_id,
        role=request.role,
        difficulty=request.difficulty,
        quantity=request.quantity,
        processing_time_ms=(time.time() - start) * 1000,
    )


# ============================ Уголовный писатель ============================

async def _load_owned_ccw_session(session_id: int, user_id: int) -> Dict:
    """Грузит сессию уголовного дела и проверяет владельца. 404 если нет, 403 если чужая."""
    session = await get_session_data(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")
    if int(session.get("user_id") or 0) != int(user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ к делу запрещён")
    return session


def _ccw_part(part_type: str, content: str, version: int) -> WebCriminalPart:
    return WebCriminalPart(
        part_type=part_type,
        display=config.CRIMINAL_CASE_PARTS_DISPLAY.get(part_type, part_type),
        content=content or "",
        version=int(version or 1),
    )


@router.post(
    "/criminal/start",
    response_model=WebCriminalStartResponse,
    responses=_ERR_RESPONSES,
    summary="Открыть сессию уголовного дела",
)
async def criminal_start(request: WebCriminalStartRequest, user: Dict = Depends(get_current_web_user),
                         _csrf: None = Depends(web_origin_guard)):
    user_id = user["user_id"]
    try:
        session_id, _ = await create_ccw_session(user_id, request.server_id, request.case_number.strip())
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return WebCriminalStartResponse(
        session_id=session_id,
        case_number=request.case_number.strip(),
        server_id=request.server_id,
        parts_order=list(config.CRIMINAL_CASE_PARTS_ORDER),
        parts_display=dict(config.CRIMINAL_CASE_PARTS_DISPLAY),
    )


@router.post(
    "/criminal/{session_id}/facts",
    responses=_ERR_RESPONSES,
    summary="Сохранить фабулу дела",
)
async def criminal_facts(session_id: int, request: WebCriminalFactsRequest,
                         user: Dict = Depends(get_current_web_user),
                         _csrf: None = Depends(web_origin_guard)):
    await _load_owned_ccw_session(session_id, user["user_id"])
    await add_facts_to_session(session_id, request.facts.strip())
    return {"ok": True}


@router.post(
    "/criminal/{session_id}/generate",
    response_model=WebCriminalGenerateResponse,
    responses=_ERR_RESPONSES,
    summary="Сгенерировать часть дела (facts|charges|evidence|demands)",
)
async def criminal_generate(session_id: int, request: WebCriminalGenerateRequest,
                            user: Dict = Depends(get_current_web_user),
                            _csrf: None = Depends(web_origin_guard)):
    await _load_owned_ccw_session(session_id, user["user_id"])
    if request.part_type not in config.CRIMINAL_CASE_PARTS_ORDER:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неизвестная часть дела")
    try:
        content, version = await generate_case_part(session_id, request.part_type)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.warning("[WEB] criminal generate error session=%s part=%s: %s",
                       session_id, request.part_type, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно недоступен — попробуйте через минуту.",
        )
    return WebCriminalGenerateResponse(part=_ccw_part(request.part_type, content, version))


@router.get(
    "/criminal/{session_id}",
    response_model=WebCriminalCaseResponse,
    responses=_ERR_RESPONSES,
    summary="Состояние сессии дела + все сгенерированные части",
)
async def criminal_get(session_id: int, user: Dict = Depends(get_current_web_user)):
    session = await _load_owned_ccw_session(session_id, user["user_id"])
    rows = await get_all_parts(session_id)
    # Упорядочиваем части по CRIMINAL_CASE_PARTS_ORDER.
    by_type = {r["part_type"]: r for r in rows}
    parts: List[WebCriminalPart] = []
    for pt in config.CRIMINAL_CASE_PARTS_ORDER:
        r = by_type.get(pt)
        if r:
            parts.append(_ccw_part(pt, r.get("content", ""), r.get("version", 1)))
    server_id = session.get("server_id", "")
    return WebCriminalCaseResponse(
        session_id=session_id,
        case_number=session.get("case_number", ""),
        server_id=server_id,
        server_name=config.server_display_name(server_id),
        status=session.get("status", "active"),
        parts=parts,
    )


# ============================ Жалобы и иски ============================

@router.get(
    "/complaint/options",
    response_model=WebComplaintOptions,
    responses=_ERR_RESPONSES,
    summary="Типы документов (жалоба/иск)",
)
async def complaint_options(user: Dict = Depends(get_current_web_user)):
    return WebComplaintOptions(
        types=[WebOption(key=k, label=v) for k, v in config.COMPLAINT_TYPES.items()],
    )


@router.post(
    "/complaint",
    response_model=WebComplaintResponse,
    responses=_ERR_RESPONSES,
    summary="Составить жалобу/иск (как у бота)",
    description="Лимит/движок — как у бота: списываем дневной лимит 'complaint' → complaint_service "
                "(парсинг формы с форума → подбор законов → генерация → BB-код) → при ошибке возврат лимита. "
                "Сохраняется в complaints_history.",
)
async def complaint(request: WebComplaintRequest, user: Dict = Depends(get_current_web_user),
                    _csrf: None = Depends(web_origin_guard)):
    user_id = user["user_id"]
    start = time.time()

    from user_handlers.core import check_and_update_usage, refund_usage

    # 1. Дневной лимит (как у бота).
    allowed, _ = await check_and_update_usage(user_id, 'complaint')
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Дневной лимит на составление документов исчерпан. Лимиты сбрасываются ежедневно.",
        )

    # 2. Генерация. При ошибке — возвращаем лимит.
    try:
        result = await generate_complaint(
            request.server_id, request.complaint_type, request.forum_url.strip(),
            request.situation_description,
        )
    except ValueError as e:
        await refund_usage(user_id, 'complaint')
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        await refund_usage(user_id, 'complaint')
        logger.warning("[WEB] complaint error user_id=%s server=%s: %s", user_id, request.server_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно недоступен — попробуйте через минуту.",
        )

    # 3. История (как бот: complaints_history).
    try:
        await db_manager.execute(
            """
            INSERT INTO complaints_history
            (user_id, server_id, complaint_type, forum_url, situation_description, generated_text, generated_bb_code)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            user_id, request.server_id, result["complaint_type"], request.forum_url.strip(),
            request.situation_description, result["generated_html"], result["generated_bbcode"],
        )
    except Exception as e:
        logger.warning("[WEB] не удалось записать complaints_history user_id=%s: %s", user_id, e)

    return WebComplaintResponse(
        complaint_type=result["complaint_type"],
        complaint_type_name=result["complaint_type_name"],
        generated_html=result["generated_html"],
        generated_bbcode=result["generated_bbcode"],
        legality_warning=result.get("legality_warning"),
        server_id=request.server_id,
        processing_time_ms=(time.time() - start) * 1000,
    )


# END OF FILE api/routers/web_tools.py
