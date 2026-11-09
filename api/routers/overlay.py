# START OF FILE api/routers/overlay.py

"""
API эндпоинты голосового оверлея: короткий совет по ситуации (RAG + Gemini).

- POST /v1/overlay/advise         — совет одним ответом (JSON)
- POST /v1/overlay/advise/stream  — потоковая выдача (SSE) для низкой задержки
"""

import asyncio
import json
import logging
import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from ..schemas import (
    AdviceRequest, AdviceResponse, ErrorResponse,
    ChecklistRequest, ChecklistResponse,
    QaRequest, QaResponse,
    ServersResponse, OverlayProjectInfo, OverlayServerInfo,
)
from ..dependencies import get_device_claims, ensure_full_access

# Сервис (корень проекта добавлен в sys.path в main.py)
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.advice_service import generate_advice, stream_advice
from services.session_memory import render_history, add_turn
from services.checklists import evaluate_checklist
from services.maintenance import maintenance_active_for, MAINTENANCE_MESSAGE
from services.qa_service import ask_question
from database.manager import db_manager
from config import OVERLAY_AI_DEADLINE_SECONDS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/overlay", tags=["Overlay"])

_ERR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
    503: {"model": ErrorResponse, "description": "Service unavailable"},
}


@router.post(
    "/advise",
    response_model=AdviceResponse,
    responses=_ERR_RESPONSES,
    summary="Совет по ситуации (RAG)",
    description="Короткий совет по законам сервера со ссылками на статьи. Источник норм — "
                "семантический поиск (RAG) по своду выбранного сервера.",
)
async def advise(request: AdviceRequest, claims: Dict = Depends(get_device_claims)):
    ensure_full_access(claims)   # тир laws (универсальный ключ) — только поиск, без AI
    start = time.time()
    # Тех. работы (единый флаг с ботом): обычный 200 с текстом — старые клиенты покажут в панели.
    if await maintenance_active_for(claims.get("user_id")):
        return AdviceResponse(advice=MAINTENANCE_MESSAGE, sources=[], server_id=request.server_id,
                              chunks_used=0, processing_time_ms=(time.time() - start) * 1000)
    try:
        logger.info(
            "[API/OVERLAY] advise device=%s server=%s mode=%s deep=%s len=%d",
            claims.get("device_id"), request.server_id, request.mode, request.deep, len(request.situation),
        )
        history = request.history or render_history(request.session_id, request.history_limit)
        res = await asyncio.wait_for(
            generate_advice(
                request.server_id, request.situation,
                history=history, deep=request.deep, top_k=request.top_k, mode=request.mode,
                model_tier=request.model_tier, project_id=request.project_id,
            ),
            timeout=OVERLAY_AI_DEADLINE_SECONDS,
        )
        if request.session_id:
            add_turn(request.session_id, "вопрос", request.situation)
        return AdviceResponse(
            advice=res["advice"],
            sources=res["sources"],
            server_id=request.server_id,
            chunks_used=len(res.get("chunks") or []),
            processing_time_ms=(time.time() - start) * 1000,
        )
    except asyncio.TimeoutError:
        # Дедлайн исчерпан — быстрый 503 вместо зависания до Cloudflare 524.
        logger.warning("[API/OVERLAY] advise deadline %ss exceeded server=%s",
                       OVERLAY_AI_DEADLINE_SECONDS, request.server_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно перегружен — попробуй через минуту.",
        )
    except ValueError as e:
        logger.warning("[API/OVERLAY] validation error: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        # AI-сервис перегружен / недоступен (по образцу /qa).
        logger.warning("[API/OVERLAY] advise overloaded: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        # Исчерпание квоты Vertex (429) после ретраев — это перегруз, а не внутренняя ошибка.
        if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
            logger.warning("[API/OVERLAY] advise overloaded (429): %s", e)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="AI-сервис временно перегружен — попробуй через минуту.",
            )
        logger.error("[API/OVERLAY] advise error: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error",
        )


@router.post(
    "/advise/stream",
    responses=_ERR_RESPONSES,
    summary="Совет по ситуации (SSE-стрим)",
    description="То же, что /advise, но ответ стримится по мере генерации (Server-Sent Events). "
                "События: sources → delta(ы) → done | error.",
)
async def advise_stream(request: AdviceRequest, claims: Dict = Depends(get_device_claims)):
    ensure_full_access(claims)   # тир laws (универсальный ключ) — только поиск, без AI
    logger.info(
        "[API/OVERLAY] advise/stream device=%s server=%s mode=%s deep=%s",
        claims.get("device_id"), request.server_id, request.mode, request.deep,
    )

    # Тех. работы (единый флаг с ботом): отдаём текст обычными SSE-событиями delta→done —
    # старые клиенты покажут его в панели ответа как обычный совет.
    if await maintenance_active_for(claims.get("user_id")):
        async def maint_gen():
            yield f"data: {json.dumps({'event': 'delta', 'text': MAINTENANCE_MESSAGE}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done'}, ensure_ascii=False)}\n\n"
        return StreamingResponse(
            maint_gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    history = request.history or render_history(request.session_id, request.history_limit)
    if request.session_id:
        add_turn(request.session_id, "вопрос", request.situation)

    async def event_gen():
        agen = stream_advice(
            request.server_id, request.situation,
            history=history, deep=request.deep, top_k=request.top_k, mode=request.mode,
            model_tier=request.model_tier, project_id=request.project_id,
        )
        try:
            while True:
                try:
                    etype, data = await asyncio.wait_for(
                        agen.__anext__(), timeout=OVERLAY_AI_DEADLINE_SECONDS
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    # Нет следующего события дольше дедлайна (зависший вызов/очередь лимитера
                    # при 429) — шлём SSE-error ДО того, как Cloudflare оборвёт стрим на ~120с.
                    logger.warning("[API/OVERLAY] advise/stream deadline %ss exceeded server=%s",
                                   OVERLAY_AI_DEADLINE_SECONDS, request.server_id)
                    yield f"data: {json.dumps({'event': 'error', 'message': 'AI-сервис временно перегружен — попробуй через минуту.'}, ensure_ascii=False)}\n\n"
                    break
                payload = json.dumps({"event": etype, **data}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
        except Exception as e:
            logger.error("[API/OVERLAY] stream error: %s", e, exc_info=True)
            # Ответ уже стримится — HTTP-статус не сменить, перегруз сигналим человекочитаемым SSE-error.
            msg = ("AI-сервис временно перегружен — попробуй через минуту."
                   if ("429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)) else str(e))
            yield f"data: {json.dumps({'event': 'error', 'message': msg}, ensure_ascii=False)}\n\n"
        finally:
            aclose = getattr(agen, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/checklist",
    response_model=ChecklistResponse,
    responses=_ERR_RESPONSES,
    summary="Чек-лист процедуры под роль",
    description="Оценивает пункты процедурного чек-листа роли (адвокат/прокурор) по ситуации и нормам сервера. "
                "Статусы пунктов: ok | violation | unclear.",
)
async def checklist(request: ChecklistRequest, claims: Dict = Depends(get_device_claims)):
    ensure_full_access(claims)   # тир laws (универсальный ключ) — только поиск, без AI
    # Тех. работы: чек-лист летит параллельно advise-стриму (там уже сообщение) — тихо пустой.
    if await maintenance_active_for(claims.get("user_id")):
        return ChecklistResponse(server_id=request.server_id, mode=request.mode, items=[])
    try:
        logger.info(
            "[API/OVERLAY] checklist device=%s server=%s mode=%s",
            claims.get("device_id"), request.server_id, request.mode,
        )
        history = request.history or render_history(request.session_id, request.history_limit)
        res = await evaluate_checklist(
            request.server_id, request.situation,
            history=history, mode=request.mode, top_k=request.top_k,
        )
        return ChecklistResponse(server_id=request.server_id, mode=request.mode, items=res.get("items", []))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error("[API/OVERLAY] checklist error: %s", e, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


@router.get(
    "/servers",
    response_model=ServersResponse,
    responses=_ERR_RESPONSES,
    summary="Список серверов законки (проект→сервер) для выбора в Q&A",
    description="Проекты и их серверы (с display_name) для каскадного выбора в режиме «Вопрос-ответ». "
                "Источник — конфиг проектов; включаются только серверы с доступной базой знаний.",
)
async def servers(claims: Dict = Depends(get_device_claims)):
    # PROJECTS_DATA — иерархия проект→серверы. Включаем только серверы с РЕАЛЬНО доступной базой
    # знаний (есть law_documents с gemini_file_uri), чтобы пикер не показывал серверы, на которых
    # вопрос упадёт 400 «база пуста».
    from law_sources_data import PROJECTS_DATA

    rows = await db_manager.fetch(
        "SELECT DISTINCT server_id FROM law_documents WHERE gemini_file_uri IS NOT NULL"
    )
    available = {r["server_id"] for r in rows}
    # Сервера с RAG-сводом (law_chunks) — на них доступны роли/совет (Адвокат/Прокурор/Общий).
    # Q&A работает на всех available; роли — только на rag-подмножестве.
    rag_rows = await db_manager.fetch("SELECT DISTINCT server_id FROM law_chunks")
    rag_servers = {r["server_id"] for r in rag_rows}
    # Серверы с каталогом умного поиска (law_articles). Флаг search учитывает и тир
    # (бета: config.LAW_SEARCH_TIERS) — клиент показывает фичу только тем, кому она доступна.
    import config as _cfg
    search_servers: set = set()
    tier_ok = (not _cfg.LAW_SEARCH_TIERS) or ((claims.get("tier") or "").lower() in _cfg.LAW_SEARCH_TIERS)
    if tier_ok:
        try:
            catalog_rows = await db_manager.fetch("SELECT DISTINCT server_id FROM law_articles")
            search_servers = {r["server_id"] for r in catalog_rows}
        except Exception:
            logger.warning("[API/OVERLAY] law_articles недоступна (миграция 017 не прогнана?) — флаг search off")

    projects = []
    for project_id, pdata in PROJECTS_DATA.items():
        servers_out = []
        for server_id, sdata in (pdata.get("servers") or {}).items():
            if server_id not in available:
                continue  # нет законов в БД — не показываем
            servers_out.append(OverlayServerInfo(
                server_id=server_id,
                display_name=(sdata.get("display_name") or server_id),
                rag=(server_id in rag_servers),
                search=(server_id in search_servers),
            ))
        if servers_out:
            projects.append(OverlayProjectInfo(
                project_id=project_id,
                display_name=(pdata.get("display_name") or project_id),
                servers=servers_out,
            ))
    return ServersResponse(projects=projects)


@router.get(
    "/leadership/servers",
    response_model=ServersResponse,
    responses=_ERR_RESPONSES,
    summary="Проекты/серверы со сводом правил для режима «Обзвон»",
    description="Игровые проекты и их серверы, для которых есть проиндексированный свод правил "
                "(режим «Обзвон»). Majestic — единый свод (servers пуст, выбор сервера не нужен). "
                "GTA5RP — общие правила проекта + конкретный сервер (Davis). Клиент строит по этому "
                "списку пикер проекта/сервера в баре «Обзвон».",
)
async def leadership_servers(claims: Dict = Depends(get_device_claims)):
    ensure_full_access(claims)   # тир laws (универсальный ключ) — только поиск, без AI
    # LEADERSHIP_PROJECTS — статический маппинг игровой проект→слои свода. Показываем только то, что
    # РЕАЛЬНО проиндексировано (namespace есть в law_chunks) — иначе пикер предложил бы проект/сервер,
    # на котором «Обзвон» вернул бы пусто.
    from law_sources_data import LEADERSHIP_PROJECTS

    rag_rows = await db_manager.fetch("SELECT DISTINCT server_id FROM law_chunks")
    have = {r["server_id"] for r in rag_rows}

    projects = []
    for p in LEADERSHIP_PROJECTS:
        if p["project_layer"] not in have:
            continue  # свод проекта ещё не проиндексирован — скрываем проект целиком
        servers_out = [
            OverlayServerInfo(server_id=s["server_id"], display_name=s["display_name"], rag=True)
            for s in p.get("servers", []) if s["rules_layer"] in have
        ]
        projects.append(OverlayProjectInfo(
            project_id=p["project_id"],
            display_name=p["display_name"],
            servers=servers_out,
        ))
    return ServersResponse(projects=projects)


@router.post(
    "/qa",
    response_model=QaResponse,
    responses=_ERR_RESPONSES,
    summary="Быстрый Q&A голосом (движок как у ТГ-бота)",
    description="Короткий ответ на юридический вопрос по законке ВЫБРАННОГО сервера. Использует тот же "
                "конвейер, что и Telegram-бот (qa_service: роутер законов → полные тексты → ответ), "
                "формат ответа — краткий (brief). Контекст follow-up берётся из rolling-памяти сессии.",
)
async def qa(request: QaRequest, claims: Dict = Depends(get_device_claims)):
    ensure_full_access(claims)   # тир laws (универсальный ключ) — только поиск, без AI
    start = time.time()
    # Тех. работы (единый флаг с ботом): обычный 200 с текстом — старые клиенты покажут в панели.
    if await maintenance_active_for(claims.get("user_id")):
        return QaResponse(answer=MAINTENANCE_MESSAGE, server_id=request.server_id,
                          processing_time_ms=(time.time() - start) * 1000)
    try:
        logger.info(
            "[API/OVERLAY] qa device=%s server=%s session=%s len=%d",
            claims.get("device_id"), request.server_id,
            (request.session_id or "")[:8], len(request.question),
        )
        history = request.history or render_history(request.session_id, request.history_limit)
        answer = await asyncio.wait_for(
            ask_question(request.server_id, request.question, answer_type="brief", history=history),
            timeout=OVERLAY_AI_DEADLINE_SECONDS,
        )
        if request.session_id:
            add_turn(request.session_id, "вопрос", request.question)
            add_turn(request.session_id, "ответ", answer)
        return QaResponse(
            answer=answer,
            server_id=request.server_id,
            processing_time_ms=(time.time() - start) * 1000,
        )
    except asyncio.TimeoutError:
        # Дедлайн исчерпан (зависший вызов/очередь лимитера при 429) — отдаём быстрый 503,
        # а не висим до потолка Cloudflare ~120с (HTTP 524 / «бесконечное думаю» в оверлее).
        logger.warning("[API/OVERLAY] qa deadline %ss exceeded server=%s",
                       OVERLAY_AI_DEADLINE_SECONDS, request.server_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно перегружен — попробуй ещё раз через минуту.",
        )
    except ValueError as e:
        # Неизвестный сервер / пустая база знаний.
        logger.warning("[API/OVERLAY] qa validation error: %s", e)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        # AI-сервис перегружен (qa_service поднимает RuntimeError на 429).
        logger.warning("[API/OVERLAY] qa overloaded: %s", e)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error("[API/OVERLAY] qa error: %s", e, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# END OF FILE api/routers/overlay.py
