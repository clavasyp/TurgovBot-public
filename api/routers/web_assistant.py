# START OF FILE api/routers/web_assistant.py

"""
Веб «Ролевой помощник» (/v1/web/assistant): создание дела + диалог + инструменты.
Состояние дела — в БД (assistant_cases/assistant_messages). Лимит ассистента (окно 72ч,
usage_type=f"{rp_role}_{case_stage}") списывается при СОЗДАНИИ дела (1 списание = 1 дело).

ВНИМАНИЕ: user_handlers.core импортируется ЛЕНИВО внутри обработчиков (цикл telegram↔api).
"""

import logging
import os
import sys
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ..dependencies import get_current_web_user
from .web_auth import web_origin_guard, _ERR_RESPONSES
from ..schemas import (
    WebOption, WebAssistantTool, WebAssistantOptions,
    WebAssistantCreateRequest, WebAssistantCreateResponse,
    WebAssistantPostRequest, WebAssistantReplyResponse,
    WebAssistantCaseResponse, WebAssistantMessage,
    WebAssistantCaseList, WebAssistantCaseListItem,
)
from services import assistant_service as asvc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/web/assistant", tags=["Web Assistant"])


async def _load_owned_case(case_id: int, user_id: int, require_active: bool = True) -> Dict:
    """Грузит дело и проверяет владельца (404/403). При require_active — 400 если закрыто."""
    case = await asvc.get_case(case_id)
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Дело не найдено")
    if int(case.get("user_id") or 0) != int(user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Доступ к делу запрещён")
    if require_active and case.get("status") != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Дело закрыто")
    return case


def _tools_for(rp_role: str, case_stage: str):
    return [WebAssistantTool(**t) for t in asvc.ASSISTANT_TOOLS.get(rp_role, {}).get(case_stage, [])]


@router.get("/options", response_model=WebAssistantOptions, responses=_ERR_RESPONSES,
            summary="Роли, стадии, инструменты, окно лимита")
async def options(user: Dict = Depends(get_current_web_user)):
    o = asvc.get_options()
    return WebAssistantOptions(
        roles=[WebOption(**r) for r in o["roles"]],
        stages=o["stages"],
        stages_by_role=o["stages_by_role"],
        tools={
            role: {stage: [WebAssistantTool(**t) for t in tools] for stage, tools in stages.items()}
            for role, stages in o["tools"].items()
        },
        reset_hours=o["reset_hours"],
    )


@router.get("/cases", response_model=WebAssistantCaseList, responses=_ERR_RESPONSES,
            summary="Активные дела пользователя")
async def cases(user: Dict = Depends(get_current_web_user)):
    items = await asvc.list_cases(user["user_id"])
    return WebAssistantCaseList(cases=[WebAssistantCaseListItem(**c) for c in items])


@router.post("/cases", response_model=WebAssistantCreateResponse, responses=_ERR_RESPONSES,
             summary="Создать дело и получить первый ответ ассистента")
async def create_case(request: WebAssistantCreateRequest, user: Dict = Depends(get_current_web_user),
                      _csrf: None = Depends(web_origin_guard)):
    user_id = user["user_id"]

    # Валидируем роль/стадию ДО списания лимита.
    try:
        asvc.validate_role_stage(request.rp_role, request.case_stage)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    from user_handlers.core import check_and_update_usage, refund_usage

    usage_type = asvc.usage_type_for(request.rp_role, request.case_stage)
    allowed, _ = await check_and_update_usage(user_id, usage_type)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Лимит дел этого типа исчерпан (окно {asvc.ASSISTANT_LIMIT_RESET_HOURS} ч). "
                   "У подписчиков лимит выше.",
        )

    try:
        case_id, reply = await asvc.create_case(
            user_id, request.server_id, request.rp_role, request.case_stage, request.description,
        )
    except ValueError as e:
        await refund_usage(user_id, usage_type)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        await refund_usage(user_id, usage_type)
        logger.warning("[WEB] assistant create error user_id=%s: %s", user_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно недоступен — попробуйте через минуту.",
        )

    return WebAssistantCreateResponse(case_id=case_id, reply=reply)


@router.get("/cases/{case_id}", response_model=WebAssistantCaseResponse, responses=_ERR_RESPONSES,
            summary="Состояние дела: метаданные + история + инструменты")
async def get_case(case_id: int, user: Dict = Depends(get_current_web_user)):
    case = await _load_owned_case(case_id, user["user_id"], require_active=False)
    history = await asvc.get_history(case_id)
    return WebAssistantCaseResponse(
        case_id=case_id,
        server_id=case["server_id"],
        rp_role=case["rp_role"],
        rp_role_label=asvc.ASSISTANT_ROLES.get(case["rp_role"], case["rp_role"]),
        case_stage=case["case_stage"],
        case_stage_label=asvc.ASSISTANT_STAGES.get(case["case_stage"], case["case_stage"]),
        status=case.get("status", "active"),
        messages=[WebAssistantMessage(**m) for m in history],
        tools=_tools_for(case["rp_role"], case["case_stage"]),
    )


@router.post("/cases/{case_id}/message", response_model=WebAssistantReplyResponse, responses=_ERR_RESPONSES,
             summary="Реплика пользователя или активация инструмента")
async def post_message(case_id: int, request: WebAssistantPostRequest,
                       user: Dict = Depends(get_current_web_user),
                       _csrf: None = Depends(web_origin_guard)):
    case = await _load_owned_case(case_id, user["user_id"], require_active=True)
    if not request.text and not request.tool:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Нужен текст или инструмент")
    try:
        reply = await asvc.post_message(case, text=request.text, tool=request.tool)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.warning("[WEB] assistant message error case=%s: %s", case_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно недоступен — попробуйте через минуту.",
        )
    return WebAssistantReplyResponse(reply=reply)


@router.post("/cases/{case_id}/close", responses=_ERR_RESPONSES, summary="Закрыть дело")
async def close_case(case_id: int, user: Dict = Depends(get_current_web_user),
                     _csrf: None = Depends(web_origin_guard)):
    await _load_owned_case(case_id, user["user_id"], require_active=False)
    await asvc.close_case(case_id)
    return {"ok": True}


# END OF FILE api/routers/web_assistant.py
