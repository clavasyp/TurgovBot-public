# START OF FILE api/routers/web_account.py

"""
Веб «Доступ и бонусы» + «Поддержка» (/v1/web/access, /v1/web/support).

Доступ/ВИП: реальный флоу как у бота — пользователь грузит пруфы (в GCS) + анкету →
карточка с кнопками одобрения постится в TG-канал ВИП-заявок → живой бот на одобрении
выдаёт users.is_unlimited. Бонусы — подписка на канал (ссылка).

Поддержка: тикет с темой/сообщением уходит в чат поддержки (Bot API).
"""

import logging
import os
import sys
from typing import Dict, List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ..dependencies import get_current_web_user
from .web_auth import web_origin_guard, _ERR_RESPONSES
from ..schemas import (
    WebOption, WebAccessStatusResponse, WebVipApplicationResponse,
    WebSupportOptions, WebSupportTicketRequest, WebSupportTicketResponse,
)
from services import vip_applications, web_notify, support_tickets
from database.manager import db_manager
import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/web", tags=["Web Account"])

# Темы обращений в поддержку (зеркало дизайна).
_SUPPORT_TOPICS = [
    {"key": "limits", "label": "📊 Вопрос по лимитам"},
    {"key": "bonuses", "label": "💎 Бонусы не подтянулись"},
    {"key": "bug", "label": "🐞 Нашёл ошибку"},
    {"key": "idea", "label": "💡 Предложить идею"},
]
_SUPPORT_TOPIC_LABELS = {t["key"]: t["label"] for t in _SUPPORT_TOPICS}


# ============================ Доступ и бонусы / ВИП ============================

@router.get("/access/status", response_model=WebAccessStatusResponse, responses=_ERR_RESPONSES,
            summary="Статус доступа + приём ВИП-заявок + статус своей заявки")
async def access_status(user: Dict = Depends(get_current_web_user)):
    user_id = user["user_id"]
    rec = await db_manager.fetchrow(
        "SELECT COALESCE(is_unlimited,FALSE) AS is_unlimited, COALESCE(is_vip,FALSE) AS is_vip, "
        "COALESCE(is_subscriber,FALSE) AS is_subscriber FROM users WHERE user_id = $1",
        user_id,
    )
    pending = await vip_applications.get_pending_application(user_id)
    intake = await vip_applications.is_intake_enabled()
    return WebAccessStatusResponse(
        is_unlimited=bool(rec and rec["is_unlimited"]),
        is_vip=bool(rec and rec["is_vip"]),
        is_subscriber=bool(rec and rec["is_subscriber"]),
        intake_enabled=intake,
        application_status=(pending["status"] if pending else None),
        subscribe_url=config.TELEGRAM_CHANNEL_URL,
        max_files=web_notify.MAX_PROOF_FILES,
    )


@router.post("/access/vip-application", response_model=WebVipApplicationResponse, responses=_ERR_RESPONSES,
             summary="Подать заявку на ВИП (пруфы + анкета)")
async def vip_application(
    server: str = Form(..., max_length=120),
    login: str = Form(..., max_length=120),
    description: str = Form(..., max_length=3000),
    files: List[UploadFile] = File(default=[]),
    user: Dict = Depends(get_current_web_user),
    _csrf: None = Depends(web_origin_guard),
):
    user_id = user["user_id"]

    if not await vip_applications.is_intake_enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Приём заявок на ВИП временно закрыт")

    if await vip_applications.get_pending_application(user_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="У вас уже есть заявка на рассмотрении")

    if len(description.strip()) < 10:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Опишите ситуацию подробнее")

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Приложите хотя бы один пруф (фото/видео)")
    if len(files) > web_notify.MAX_PROOF_FILES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Слишком много файлов (максимум {web_notify.MAX_PROOF_FILES})")

    # Читаем + валидируем + грузим пруфы в GCS.
    proof_urls: List[str] = []
    media: List[Dict] = []
    for f in files:
        content = await f.read()
        if not content:
            continue
        if len(content) > web_notify.MAX_PROOF_BYTES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Файл «{f.filename}» больше {web_notify.MAX_PROOF_BYTES // (1024*1024)} МБ")
        ctype = (f.content_type or "").lower()
        if not ctype.startswith(web_notify.ALLOWED_PROOF_PREFIXES):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Разрешены только изображения и видео")
        try:
            url = await web_notify.upload_proof(user_id, f.filename or "proof", content, ctype)
        except Exception as e:
            logger.warning("[WEB] vip proof upload failed user_id=%s: %s", user_id, e)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="Не удалось загрузить пруфы, попробуйте позже")
        proof_urls.append(url)
        media.append({"type": "video" if ctype.startswith("video/") else "photo", "url": url})

    if not proof_urls:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Пустые файлы пруфов")

    answers = {
        "server": server.strip(), "login": login.strip(),
        "description": description.strip(), "source": "web",
    }
    app_id = await vip_applications.create_application(user_id, answers, media)
    if not app_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="У вас уже есть заявка на рассмотрении")

    # Профиль для карточки.
    prof = await db_manager.fetchrow(
        "SELECT full_name, username FROM users WHERE user_id = $1", user_id
    )
    full_name = (prof["full_name"] if prof else None) or "Игрок"
    username = prof["username"] if prof else None

    posted = await web_notify.post_vip_card(app_id, user_id, full_name, username, answers, proof_urls)
    if posted:
        await vip_applications.set_channel_message(app_id, posted[0], posted[1])
    else:
        logger.warning("[WEB] vip card not posted app_id=%s (заявка создана, карточка не ушла)", app_id)

    return WebVipApplicationResponse(ok=True, application_id=app_id, status="pending")


# ============================ Поддержка ============================

@router.get("/support/options", response_model=WebSupportOptions, responses=_ERR_RESPONSES,
            summary="Темы обращений + контакт")
async def support_options(user: Dict = Depends(get_current_web_user)):
    return WebSupportOptions(
        topics=[WebOption(**t) for t in _SUPPORT_TOPICS],
        contact_url=config.TELEGRAM_CHANNEL_URL,
    )


@router.post("/support/ticket", response_model=WebSupportTicketResponse, responses=_ERR_RESPONSES,
             summary="Отправить обращение в поддержку")
async def support_ticket(request: WebSupportTicketRequest, user: Dict = Depends(get_current_web_user),
                         _csrf: None = Depends(web_origin_guard)):
    user_id = user["user_id"]
    topic_label = _SUPPORT_TOPIC_LABELS.get(request.topic, request.topic)

    prof = await db_manager.fetchrow(
        "SELECT full_name, username FROM users WHERE user_id = $1", user_id
    )
    full_name = (prof["full_name"] if prof else None) or "Игрок"
    username = prof["username"] if prof else None
    message = request.message.strip()

    # 1. Персист в БД (управляется из админки бота). Тикет не теряется, даже если TG-уведомление не уйдёт.
    ticket_id = await support_tickets.create_ticket(user_id, topic_label, message)

    # 2. Уведомление в чат поддержки (best-effort) + запоминаем координаты для будущей правки карточки.
    posted = await web_notify.post_support_ticket(
        user_id, full_name, username, topic_label, message, ticket_id=ticket_id
    )
    if posted and ticket_id:
        await support_tickets.set_channel_message(ticket_id, posted[0], posted[1])

    # Провал только если НИ персист, НИ уведомление не сработали.
    if not ticket_id and not posted:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Не удалось отправить обращение, попробуйте позже")
    return WebSupportTicketResponse(ok=True)


# END OF FILE api/routers/web_account.py
