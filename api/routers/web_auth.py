# START OF FILE api/routers/web_auth.py

"""
Веб-вход и ядро сайта TurgovBot (/v1/web/*).

Расширение того же FastAPI-приложения, что обслуживает бота/оверлей. Переиспользует
сервисы бота: лимиты (user_handlers.core), Q&A (services.qa_service), конфиг серверов
(utils + law_sources_data), историю (message_history). Аутентификация — cookie-сессия
(httpOnly JWT) после проверки подписи Telegram Login Widget.

Эндпоинты:
- POST /v1/web/login    — проверка подписи виджета → cookie-сессия → MeResponse
- GET  /v1/web/me       — профиль + флаги + лимиты + шаблон
- POST /v1/web/logout   — стереть cookie
- POST /v1/web/ask      — ответ на юр. вопрос (лимиты как у бота)
- GET  /v1/web/servers  — каскад проект→серверы
- GET  /v1/web/history  — список истории (asked|generated) с пагинацией
- GET  /v1/web/history/{id} — деталь диалога
- PUT  /v1/web/template — сохранить шаблон вопросов
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

# Корень проекта добавлен в sys.path в api/main.py; дублируем для надёжности импортов сервисов.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ..dependencies import get_current_web_user
from ..schemas import (
    WebLoginRequest, MeResponse, WebUserProfile, WebAccountFlags, WebLimits, WebLimitItem,
    WebQuestionTemplate, WebAskRequest, WebAskResponse, WebServersResponse, WebServerOption,
    WebProjectOption, WebHistoryPage, WebHistoryItem, WebHistoryDetail, WebTemplateUpdateResponse,
    ErrorResponse,
)

from services.telegram_login import verify_telegram_login, issue_web_session
from services.web_users import upsert_web_user, compute_account_status
from services.qa_service import ask_question
# ВНИМАНИЕ: user_handlers.core импортируется ЛЕНИВО внутри ask() — импорт на уровне модуля
# замыкает цикл api → user_handlers/__init__ → test_loophole_flow → services.law_loophole →
# api.prompts → api/__init__ → main → web_auth (ImportError при загрузке через law_loophole).
from database.manager import db_manager
import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/web", tags=["Web"])

_COOKIE_NAME = "tb_web_session"

# Размер страницы истории — как в боте (user_handlers/history.py).
HISTORY_PAGE_SIZE = 8

_ERR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "Bad request"},
    401: {"model": ErrorResponse, "description": "Unauthorized"},
    403: {"model": ErrorResponse, "description": "Forbidden"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    503: {"model": ErrorResponse, "description": "Service unavailable"},
}

# Отображаемые имена лимитов ассистента (порядок = порядок вывода). Совпадает с ботом.
_ASSISTANT_LIMIT_DISPLAY = {
    "advocate_field_work": "⚖️ Адвокат (на месте)",
    "advocate_case_analysis": "📂 Адвокат (анализ дела)",
    "prosecutor_field_work": "🚨 Прокурор (на месте)",
    "prosecutor_case_analysis": "🗂️ Прокурор (анализ дела)",
    "judge_court_case": "⚖️ Судья (судебное дело)",
}

_QUESTION_TYPES = ("detailed", "brief", "free")


async def web_origin_guard(request: Request) -> None:
    """CSRF-хардненинг для МУТИРУЮЩИХ веб-эндпоинтов (login/ask/logout/template).

    Cookie стоит SameSite=lax (уже закрывает базовый CSRF); это второй барьер по Origin:
    - если задан config.WEB_ALLOWED_ORIGINS — Origin обязан быть в нём;
    - иначе (пусто) — same-origin: netloc(Origin) должен совпасть с Host (за Caddy один домен).
    Запрос без заголовка Origin (типично для не-CORS same-origin) пропускаем — его прикрывает SameSite.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    allowed = getattr(config, "WEB_ALLOWED_ORIGINS", ())
    if allowed:
        if origin not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin запрос запрещён")
        return
    try:
        origin_host = urlparse(origin).netloc
    except Exception:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Bad origin")
    host = request.headers.get("host", "")
    if origin_host and host and origin_host != host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cross-origin запрос запрещён")


def _set_session_cookie(response: Response, token: str) -> None:
    """Ставит httpOnly+secure+samesite cookie сессии. Same-origin за Caddy → secure ок."""
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=config.WEB_SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


async def _build_me(user_id: int) -> MeResponse:
    """Собирает MeResponse: профиль + флаги + лимиты + шаблон. Используется /login и /me."""
    rec = await db_manager.fetchrow(
        """
        SELECT user_id, full_name, username,
               COALESCE(is_admin, FALSE) AS is_admin,
               COALESCE(is_vip, FALSE) AS is_vip,
               COALESCE(is_unlimited, FALSE) AS is_unlimited,
               COALESCE(is_subscriber, FALSE) AS is_subscriber,
               COALESCE(is_tester, FALSE) AS is_tester,
               COALESCE(is_banned, FALSE) AS is_banned,
               default_project_id, default_server_id, default_question_type
        FROM users WHERE user_id = $1
        """,
        user_id,
    )
    if not rec:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    tier, label = await compute_account_status(user_id)
    is_unlimited_access = bool(
        rec["is_vip"] or rec["is_admin"] or rec["is_unlimited"] or user_id == config.SUPER_ADMIN_ID
    )
    is_subscriber = bool(rec["is_subscriber"])

    profile = WebUserProfile(
        user_id=rec["user_id"],
        full_name=rec["full_name"] or "",
        username=rec["username"],
        photo_url=None,  # photo_url виджета не персистится; шапка хранит его на фронте.
        status=tier,
        status_label=label,
    )
    flags = WebAccountFlags(
        is_admin=bool(rec["is_admin"]),
        is_vip=bool(rec["is_vip"]),
        is_unlimited=bool(rec["is_unlimited"]),
        is_subscriber=is_subscriber,
        is_tester=bool(rec["is_tester"]),
        is_banned=bool(rec["is_banned"]),
    )
    limits = await _build_limits(user_id, is_unlimited_access, is_subscriber)
    template = WebQuestionTemplate(
        default_project_id=rec["default_project_id"],
        default_server_id=rec["default_server_id"],
        default_question_type=rec["default_question_type"],
    )
    return MeResponse(user=profile, flags=flags, limits=limits, template=template)


async def _build_limits(user_id: int, unlimited: bool, is_subscriber: bool) -> WebLimits:
    """
    Собирает лимиты по образцу личного кабинета бота (user_handlers/core.py::personal_account).

    Безлимитным (VIP/админ/безлимит) — limit=-1, remaining=-1, unlimited=True.
    Остальным — дневные счётчики из user_usage + ассистент за окно ASSISTANT_LIMIT_RESET_HOURS.
    """
    now_utc = datetime.now(timezone.utc)
    # До сброса дневных лимитов — до ближайшей UTC-полуночи.
    next_midnight = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_reset_seconds = max(0, int((next_midnight - now_utc).total_seconds()))

    if unlimited:
        daily = [
            WebLimitItem(key="questions", label="Вопросы", used=0, limit=-1, remaining=-1),
            WebLimitItem(key="interview", label="Генерация вопросов", used=0, limit=-1, remaining=-1),
            WebLimitItem(key="complaint", label="Жалобы/иски", used=0, limit=-1, remaining=-1),
        ]
        assistant = [
            WebLimitItem(key=k, label=v, used=0, limit=-1, remaining=-1)
            for k, v in _ASSISTANT_LIMIT_DISPLAY.items()
        ]
        return WebLimits(
            unlimited=True,
            daily_reset_seconds=daily_reset_seconds,
            assistant_reset_seconds=0,
            daily=daily,
            assistant=assistant,
        )

    # --- Дневные лимиты ---
    usage_daily = await db_manager.fetchrow(
        "SELECT question_reqs, interview_gens, complaint_gens FROM user_usage "
        "WHERE user_id = $1 AND usage_date = CURRENT_DATE",
        user_id,
    )
    q_used = (usage_daily["question_reqs"] if usage_daily and usage_daily["question_reqs"] else 0) or 0
    i_used = (usage_daily["interview_gens"] if usage_daily and usage_daily["interview_gens"] else 0) or 0
    c_used = (usage_daily["complaint_gens"] if usage_daily and usage_daily["complaint_gens"] else 0) or 0

    q_limit = config.DAILY_QUESTION_LIMIT + (config.SUBSCRIPTION_BONUS_QUESTION if is_subscriber else 0)
    i_limit = config.DAILY_INTERVIEW_GEN_LIMIT + (config.SUBSCRIPTION_BONUS_INTERVIEW_GEN if is_subscriber else 0)
    c_limit = config.DAILY_COMPLAINT_GEN_LIMIT + (config.SUBSCRIPTION_BONUS_COMPLAINT_GEN if is_subscriber else 0)

    daily = [
        WebLimitItem(key="questions", label="Вопросы", used=q_used, limit=q_limit,
                     remaining=max(0, q_limit - q_used)),
        WebLimitItem(key="interview", label="Генерация вопросов", used=i_used, limit=i_limit,
                     remaining=max(0, i_limit - i_used)),
        WebLimitItem(key="complaint", label="Жалобы/иски", used=c_used, limit=c_limit,
                     remaining=max(0, c_limit - c_used)),
    ]

    # --- Лимиты ассистента (скользящее окно ASSISTANT_LIMIT_RESET_HOURS) ---
    usage_rows = await db_manager.fetch(
        "SELECT usage_type, COUNT(*) AS cnt FROM assistant_usage_log "
        "WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '1 hour' * $2 GROUP BY usage_type",
        user_id, config.ASSISTANT_LIMIT_RESET_HOURS,
    )
    usage_map = {r["usage_type"]: r["cnt"] for r in usage_rows}

    assistant = []
    for usage_type, label in _ASSISTANT_LIMIT_DISPLAY.items():
        base = config.ASSISTANT_LIMITS.get(usage_type, 0)
        bonus = config.ASSISTANT_SUBSCRIPTION_BONUSES.get(usage_type, 0) if is_subscriber else 0
        a_limit = base + bonus
        a_used = usage_map.get(usage_type, 0)
        assistant.append(WebLimitItem(
            key=usage_type, label=label, used=a_used, limit=a_limit,
            remaining=max(0, a_limit - a_used),
        ))

    # До восстановления первого использования ассистента (самая старая запись в окне).
    oldest = await db_manager.fetchval(
        "SELECT MIN(timestamp) FROM assistant_usage_log "
        "WHERE user_id = $1 AND timestamp >= NOW() - INTERVAL '1 hour' * $2",
        user_id, config.ASSISTANT_LIMIT_RESET_HOURS,
    )
    if oldest:
        expire_time = oldest + timedelta(hours=config.ASSISTANT_LIMIT_RESET_HOURS)
        assistant_reset_seconds = max(0, int((expire_time - now_utc).total_seconds()))
    else:
        assistant_reset_seconds = 0

    return WebLimits(
        unlimited=False,
        daily_reset_seconds=daily_reset_seconds,
        assistant_reset_seconds=assistant_reset_seconds,
        daily=daily,
        assistant=assistant,
    )


@router.post(
    "/login",
    response_model=MeResponse,
    responses=_ERR_RESPONSES,
    summary="Вход через Telegram Login Widget",
    description="Проверяет подпись payload'а виджета (HMAC по сырому SHA-256 от токена бота) и "
                "свежесть auth_date, апсертит пользователя, ставит httpOnly cookie-сессию и "
                "возвращает профиль (как /me).",
)
async def login(request: Request, response: Response, _csrf: None = Depends(web_origin_guard)):
    # Веб-вход выключен, если секрет сессии не сконфигурирован (см. config.WEB_SESSION_SECRET_OK).
    if not getattr(config, "WEB_SESSION_SECRET_OK", False):
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Веб-вход временно недоступен (сервер не сконфигурирован)")

    # HMAC считаем по СЫРОМУ payload'у (все подписанные поля виджета), а не по фикс-схеме —
    # иначе при добавлении Telegram новых полей подпись перестанет сходиться.
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad payload")
    if not isinstance(raw, dict) or not raw.get("hash") or raw.get("id") is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad payload")

    # Telegram считает hash по СТРОКОВЫМ значениям всех полей, кроме hash.
    data = {k: str(v) for k, v in raw.items() if v is not None}

    if not verify_telegram_login(data):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Telegram login signature")

    try:
        user_id = int(raw["id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Bad payload")

    first_name = str(raw.get("first_name") or "")
    last_name = str(raw.get("last_name") or "")
    username = raw.get("username")
    username = str(username) if username is not None else None
    full_name = (first_name + (f" {last_name}" if last_name else "")).strip() or "Игрок"

    await upsert_web_user(user_id, full_name, username)

    token = issue_web_session(user_id)
    _set_session_cookie(response, token)

    logger.info("[WEB] login user_id=%s", user_id)
    return await _build_me(user_id)


@router.get(
    "/me",
    response_model=MeResponse,
    responses=_ERR_RESPONSES,
    summary="Текущий профиль + лимиты + шаблон",
)
async def me(user: Dict = Depends(get_current_web_user)):
    return await _build_me(user["user_id"])


@router.post(
    "/logout",
    responses=_ERR_RESPONSES,
    summary="Выход (стереть cookie-сессию)",
)
async def logout(response: Response, _csrf: None = Depends(web_origin_guard)):
    # Стираем cookie теми же атрибутами, что и при установке (иначе браузер не удалит).
    response.set_cookie(
        key=_COOKIE_NAME, value="", max_age=0,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return {"ok": True}


@router.post(
    "/ask",
    response_model=WebAskResponse,
    responses=_ERR_RESPONSES,
    summary="Ответ на юридический вопрос (как у бота)",
    description="Лимиты и движок — как у Telegram-бота: списываем лимит → qa_service.ask_question → "
                "при ошибке возвращаем лимит. Диалог пишется в message_history (user+model).",
)
async def ask(request: WebAskRequest, user: Dict = Depends(get_current_web_user),
              _csrf: None = Depends(web_origin_guard)):
    user_id = user["user_id"]
    start = time.time()
    answer_type = request.answer_type  # 'detailed' | 'brief'

    # Лимитер бота — лениво (та же функция/счётчики, что и у бота → общий аккаунт; см.
    # комментарий к импортам выше про циклический импорт).
    from user_handlers.core import check_and_update_usage, refund_usage

    # 1. Лимит (порядок как у бота): сначала проверить-и-списать.
    allowed, _ = await check_and_update_usage(user_id, answer_type)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Дневной лимит вопросов исчерпан. Лимиты сбрасываются ежедневно.",
        )

    # 2. Генерация ответа. При ЛЮБОЙ ошибке — возвращаем списанный лимит и отдаём 503.
    try:
        answer = await ask_question(request.server_id, request.question, answer_type=answer_type)
    except ValueError as e:
        # Неизвестный сервер / пустая база знаний — это не вина AI, лимит возвращаем.
        await refund_usage(user_id, answer_type)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        await refund_usage(user_id, answer_type)
        logger.warning("[WEB] ask error user_id=%s server=%s: %s", user_id, request.server_id, e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI-сервис временно недоступен — попробуйте через минуту.",
        )

    # 3. Пишем диалог в историю (как qa_flow: user-реплика + model-ответ, с server_id).
    try:
        await db_manager.execute(
            "INSERT INTO message_history (user_id, server_id, role, content) VALUES ($1, $2, 'user', $3);",
            user_id, request.server_id, request.question,
        )
        await db_manager.execute(
            "INSERT INTO message_history (user_id, server_id, role, content) VALUES ($1, $2, 'model', $3);",
            user_id, request.server_id, answer,
        )
    except Exception as e:
        # История — не критична для ответа пользователю; логируем и отдаём ответ.
        logger.warning("[WEB] не удалось записать историю user_id=%s: %s", user_id, e)

    return WebAskResponse(
        answer=answer,
        citations=None,
        source_url=None,
        source_label=None,
        server_id=request.server_id,
        processing_time_ms=(time.time() - start) * 1000,
    )


@router.get(
    "/servers",
    response_model=WebServersResponse,
    responses=_ERR_RESPONSES,
    summary="Каскад проект→серверы (для выбора в Q&A)",
    description="Включённые проекты и серверы (учитывая is_disabled в БД), только те, у кого есть "
                "база знаний (law_documents). Флаг rag=True — есть свод (law_chunks).",
)
async def servers(user: Dict = Depends(get_current_web_user)):
    from utils import get_enabled_projects, get_enabled_servers
    from config import PROJECTS_CONFIG, SERVERS_CONFIG

    # Серверы с реально доступной базой знаний (есть law_documents с gemini_file_uri).
    avail_rows = await db_manager.fetch(
        "SELECT DISTINCT server_id FROM law_documents WHERE gemini_file_uri IS NOT NULL"
    )
    available = {r["server_id"] for r in avail_rows}
    # Серверы с RAG-сводом (law_chunks) — флаг rag (как в api/routers/overlay.py).
    rag_rows = await db_manager.fetch("SELECT DISTINCT server_id FROM law_chunks")
    rag_servers = {r["server_id"] for r in rag_rows}

    enabled_projects = await get_enabled_projects()

    projects_out = []
    for project_id in enabled_projects:
        pdata = PROJECTS_CONFIG.get(project_id, {})
        project_name = pdata.get("display_name", project_id)
        servers_out = []
        for server_id in await get_enabled_servers(project_id):
            if server_id not in available:
                continue  # нет законов в БД — не показываем
            sdata = SERVERS_CONFIG.get(server_id, {})
            servers_out.append(WebServerOption(
                server_id=server_id,
                display_name=sdata.get("display_name", server_id),
                rag=(server_id in rag_servers),
            ))
        if servers_out:
            projects_out.append(WebProjectOption(
                project_id=project_id,
                display_name=project_name,
                servers=servers_out,
            ))

    return WebServersResponse(projects=projects_out)


@router.get(
    "/history",
    response_model=WebHistoryPage,
    responses=_ERR_RESPONSES,
    summary="История запросов (asked|generated) с пагинацией",
)
async def history(
    user: Dict = Depends(get_current_web_user),
    type: str = Query("asked", description="asked | generated"),
    page: int = Query(0, ge=0, description="Номер страницы (с 0)"),
):
    user_id = user["user_id"]
    history_type = "generated" if type == "generated" else "asked"
    prefix = config.GENERATION_HISTORY_PREFIX

    # ОТ ИНЪЕКЦИИ: префикс — серверная константа, не пользовательский ввод. like-паттерн
    # передаём ПАРАМЕТРОМ ($2), не f-string-значением.
    if history_type == "generated":
        like_op = "LIKE"
    else:
        like_op = "NOT LIKE"
    like_pattern = f"{prefix}%"

    total = await db_manager.fetchval(
        f"SELECT COUNT(*) FROM message_history "
        f"WHERE user_id = $1 AND role = 'user' AND content {like_op} $2",
        user_id, like_pattern,
    ) or 0
    total_pages = (total + HISTORY_PAGE_SIZE - 1) // HISTORY_PAGE_SIZE
    offset = page * HISTORY_PAGE_SIZE

    rows = await db_manager.fetch(
        f"SELECT id, content, timestamp FROM message_history "
        f"WHERE user_id = $1 AND role = 'user' AND content {like_op} $2 "
        f"ORDER BY timestamp DESC LIMIT $3 OFFSET $4",
        user_id, like_pattern, HISTORY_PAGE_SIZE, offset,
    )

    items = []
    for r in rows:
        question = (r["content"] or "").replace(prefix, "").strip()
        items.append(WebHistoryItem(
            id=r["id"],
            question=question,
            timestamp=r["timestamp"].isoformat() if r["timestamp"] else "",
        ))

    return WebHistoryPage(total=total, page=page, total_pages=total_pages, items=items)


@router.get(
    "/history/{item_id}",
    response_model=WebHistoryDetail,
    responses=_ERR_RESPONSES,
    summary="Деталь диалога (вопрос + ответ)",
)
async def history_item(item_id: int, user: Dict = Depends(get_current_web_user)):
    user_id = user["user_id"]
    prefix = config.GENERATION_HISTORY_PREFIX

    question_rec = await db_manager.fetchrow(
        "SELECT content, timestamp FROM message_history "
        "WHERE id = $1 AND user_id = $2 AND role = 'user'",
        item_id, user_id,
    )
    if not question_rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="История не найдена")

    # Ответ — следующая model-реплика после вопроса (как в user_handlers/history.py).
    answer_rec = await db_manager.fetchrow(
        "SELECT content FROM message_history "
        "WHERE user_id = $1 AND role = 'model' AND timestamp > $2 ORDER BY timestamp ASC LIMIT 1",
        user_id, question_rec["timestamp"],
    )

    question = (question_rec["content"] or "").replace(prefix, "")
    answer = answer_rec["content"] if answer_rec else ""

    return WebHistoryDetail(
        question=question,
        answer=answer,
        timestamp=question_rec["timestamp"].isoformat() if question_rec["timestamp"] else "",
    )


@router.put(
    "/template",
    response_model=WebTemplateUpdateResponse,
    responses=_ERR_RESPONSES,
    summary="Сохранить шаблон вопросов по умолчанию",
    description="Валидирует project_id/server_id против включённых сущностей и question_type против "
                "detailed|brief|free. Пустые/None значения сбрасывают шаблон.",
)
async def update_template(template: WebQuestionTemplate, user: Dict = Depends(get_current_web_user),
                          _csrf: None = Depends(web_origin_guard)):
    from utils import get_enabled_projects, get_enabled_servers

    user_id = user["user_id"]

    project_id = template.default_project_id or None
    server_id = template.default_server_id or None
    question_type = template.default_question_type or None

    # Валидация (только если значение задано). Невалидное — 400.
    if project_id is not None:
        if project_id not in await get_enabled_projects():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неизвестный проект")
    if server_id is not None:
        # Сервер должен принадлежать включённым серверам (его проекта, если задан, иначе любым).
        enabled = await get_enabled_servers(project_id) if project_id else await get_enabled_servers()
        if server_id not in enabled:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неизвестный сервер")
    if question_type is not None and question_type not in _QUESTION_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неизвестный формат ответа")

    await db_manager.execute(
        "UPDATE users SET default_project_id = $2, default_server_id = $3, default_question_type = $4 "
        "WHERE user_id = $1",
        user_id, project_id, server_id, question_type,
    )

    return WebTemplateUpdateResponse(
        ok=True,
        template=WebQuestionTemplate(
            default_project_id=project_id,
            default_server_id=server_id,
            default_question_type=question_type,
        ),
    )


# END OF FILE api/routers/web_auth.py
