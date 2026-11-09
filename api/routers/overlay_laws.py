# START OF FILE api/routers/overlay_laws.py

"""
Умный поиск по законке для оверлея (без LLM в рантайме).

- POST /v1/overlay/laws/search        — гибридный поиск: карточки статей + «ещё релевантное»
- GET  /v1/overlay/laws/article/{id}  — полная статья (раскрытие карточки)

СОЗНАТЕЛЬНО без гейта тех.работ (maintenance): поиск — SQL по каталогу плюс локальная FRIDA;
Gemini/Vertex в этой функции не вызываются.
Худший случай деградации — отвал векторного плеча (degraded=true в ответе).

Доступ: бета — по тирам из config.LAW_SEARCH_TIERS (пустой список = всем).
Рейт-лимит per-user (in-memory sliding window, как у auth-эндпоинтов).
"""

import asyncio
import logging
import time
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from ..schemas import (
    ErrorResponse,
    LawSearchRequest, LawSearchResponse,
    LawArticleResponse,
)
from ..dependencies import get_device_claims
from ..security import rate_limiter

# Сервис (корень проекта добавлен в sys.path в main.py)
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from services.law_search_service import LawSearchUnavailable, search as law_search, get_article

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/overlay/laws", tags=["Overlay Laws"])

_ERR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    404: {"model": ErrorResponse, "description": "Not found"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    503: {"model": ErrorResponse, "description": "Search deadline/unavailable"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
}


def _search_quality_logging_active() -> bool:
    """Optional telemetry must never become an API import/startup dependency."""
    try:
        from services.law_search_analytics import is_law_search_logging_active

        return is_law_search_logging_active()
    except Exception:
        return False


def _record_search_quality(
    *,
    request: LawSearchRequest,
    claims: Dict,
    started_at: float,
    outcome: str,
    diagnostics: Optional[Dict],
    response: LawSearchResponse | None = None,
    error_code: str | None = None,
) -> None:
    """Build and enqueue telemetry without ever affecting the HTTP response."""
    try:
        # Lazy by design: an import/init defect in optional telemetry must not
        # prevent this router (and therefore the whole API) from loading.
        from services.law_search_analytics import (
            build_search_event,
            enqueue_search_event,
            is_law_search_logging_active,
        )

        if not is_law_search_logging_active():
            return
        if response is None:
            response_payload = None
        elif hasattr(response, "model_dump"):
            response_payload = response.model_dump()
        else:
            response_payload = response.dict()
        event = build_search_event(
            server_id=request.server_id,
            query=request.query,
            requested_limit=request.limit,
            duration_ms=(time.perf_counter() - started_at) * 1000.0,
            outcome=outcome,
            response=response_payload,
            diagnostics=diagnostics,
            error_code=error_code,
            tier=claims.get("tier"),
        )
        enqueue_search_event(event)
    except Exception:
        logger.warning(
            "[API/LAWS] quality event was not queued; search response is unaffected",
            exc_info=True,
        )


def _unavailable_response(code: str, message: str = "Поиск временно недоступен, повтори запрос") -> JSONResponse:
    """Стабильный ErrorResponse-контракт, который понимает Overlay LawsClient."""
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": message, "detail": message, "code": code},
    )


def _check_access(claims: Dict) -> None:
    """Гейт тира (бета: tester/admin + laws-билд) + рейт-лимит на устройство."""
    tiers = config.LAW_SEARCH_TIERS
    tier = (claims.get("tier") or "").lower()
    if tiers and tier not in tiers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Поиск по законке пока доступен только тестерам",
        )
    # Ключ лимита включает device_id: у универсального laws-ключа ВСЕ машины сидят под одним
    # user_id владельца — общий per-user бакет делился бы на всех тестеров разом.
    allowed, _count = rate_limiter.check_and_record(
        f"laws:{claims.get('user_id')}:{claims.get('device_id')}", config.LAW_SEARCH_RATE_PER_MIN
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много запросов поиска, подожди минуту",
        )


@router.post(
    "/search",
    response_model=LawSearchResponse,
    responses=_ERR_RESPONSES,
    summary="Гибридный поиск статей законки",
    description="«какая статья за халатность» / «12.1» / сленг («дм») → карточки статей + похожие. "
                "Код статьи + словарь синонимов + FTS(russian) + триграммы + локальная FRIDA; "
                "слияние RRF. При недоступной FRIDA честно работает в keyword-only режиме.",
)
async def search_laws(request: LawSearchRequest, claims: Dict = Depends(get_device_claims)):
    _check_access(claims)
    started_at = time.perf_counter()
    diagnostics: Optional[Dict] = (
        {} if _search_quality_logging_active() else None
    )
    try:
        res = await law_search(
            request.server_id,
            request.query,
            limit=request.limit,
            diagnostics=diagnostics,
        )
        # Record only an answer that passed the same public contract FastAPI
        # will return.  A schema defect is logged as an error, not a success.
        validated_response = LawSearchResponse(**res)
    except LawSearchUnavailable as e:
        _record_search_quality(
            request=request,
            claims=claims,
            started_at=started_at,
            outcome="unavailable",
            diagnostics=diagnostics,
            error_code=e.code,
        )
        logger.warning(
            "[API/LAWS] search временно недоступен: server=%s q=%r code=%s",
            request.server_id, request.query[:60], e.code,
        )
        return _unavailable_response(e.code)
    except asyncio.CancelledError:
        _record_search_quality(
            request=request,
            claims=claims,
            started_at=started_at,
            outcome="cancelled",
            diagnostics=diagnostics,
            error_code="request_cancelled",
        )
        raise
    except Exception as e:
        _record_search_quality(
            request=request,
            claims=claims,
            started_at=started_at,
            outcome="error",
            diagnostics=diagnostics,
            error_code=type(e).__name__,
        )
        logger.exception("[API/LAWS] search упал: server=%s q=%r", request.server_id, request.query[:60])
        raise HTTPException(status_code=500, detail="Поиск временно недоступен") from e
    _record_search_quality(
        request=request,
        claims=claims,
        started_at=started_at,
        outcome="success" if res.get("results") else "empty",
        diagnostics=diagnostics,
        response=validated_response,
    )
    logger.info(
        "[API/LAWS] search device=%s server=%s q=%r → %d результатов%s",
        claims.get("device_id"), request.server_id, request.query[:60],
        len(res["results"]), " [degraded]" if res.get("degraded") else "",
    )
    return validated_response


@router.get(
    "/article/{article_id}",
    response_model=LawArticleResponse,
    responses=_ERR_RESPONSES,
    summary="Полная статья каталога",
    description="Раскрытие карточки: полный текст статьи + структура санкции + похожие статьи.",
)
async def article(article_id: int, claims: Dict = Depends(get_device_claims)):
    _check_access(claims)
    try:
        art = await asyncio.wait_for(
            get_article(article_id), timeout=config.LAW_SEARCH_ARTICLE_TIMEOUT,
        )
    except (asyncio.TimeoutError, LawSearchUnavailable):
        logger.warning("[API/LAWS] article timeout/unavailable: id=%s", article_id)
        return _unavailable_response("article_timeout", "Статья временно недоступна, повтори запрос")
    if not art:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Статья не найдена")
    # penalty_json приходит из asyncpg строкой — Pydantic ждёт dict
    pj = art.get("penalty_json")
    if isinstance(pj, str):
        import json
        try:
            art["penalty_json"] = json.loads(pj)
        except (ValueError, TypeError):
            art["penalty_json"] = None
    art.pop("law_document_id", None)
    return LawArticleResponse(**art)

# END OF FILE api/routers/overlay_laws.py
