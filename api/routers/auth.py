# START OF FILE api/routers/auth.py

"""
Device-auth эндпоинты десктоп-оверлея.

- POST /v1/auth/device/start  — начать device-code login (вернёт device_code + user_code)
- POST /v1/auth/device/poll   — поллинг до подтверждения в боте (вернёт токены, когда approved)
- POST /v1/auth/refresh       — ротация пары токенов (проверяет живой доступ)
- POST /v1/auth/logout        — отзыв устройства по refresh-токену
- POST /v1/auth/ws-ticket     — (Bearer access) одноразовый тикет для WebSocket

Публичные эндпоинты лимитируются по IP. Логика — services/device_auth.py.
"""

import logging
import os
import secrets
import sys
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

# Корень проекта в sys.path (как в overlay.py) — для импорта services.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from services import device_auth

from ..dependencies import get_device_claims, ensure_full_access
from ..security import get_client_ip, rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["Auth"])


class DeviceStartRequest(BaseModel):
    device_id: Optional[str] = Field(None, max_length=128)
    app_version: Optional[str] = Field(None, max_length=32)
    os: Optional[str] = Field(None, max_length=64)


class DevicePollRequest(BaseModel):
    device_code: str = Field(..., max_length=256)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., max_length=256)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(..., max_length=256)


def _ip_limit(request: Request, rpm: int) -> None:
    """Лёгкий лимит по IP для публичных эндпоинтов (поверх существующего RateLimiter)."""
    ip = get_client_ip(request)
    # Релей (РФ-обход) схлопывает всю когорту в ОДИН ip (CF-Connecting-IP = IP релея) → лимит
    # для известных relay-IP умножается, иначе refresh каждые ~14 мин у всех релейных юзеров
    # выедал бы общий бакет. path в ключе — чтобы start/poll/refresh не делили один счётчик.
    if ip in config.OVERLAY_RELAY_IPS:
        rpm *= config.OVERLAY_RELAY_RPM_MULT
    key = abs(hash(("auth", request.url.path, ip))) % (2 ** 31)
    allowed, _ = rate_limiter.check_and_record(key, rpm)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "60"})


@router.post("/device/start", summary="Начать device-code login")
async def device_start(body: DeviceStartRequest, request: Request):
    _ip_limit(request, 30)
    device_id = (body.device_id or "").strip() or secrets.token_urlsafe(16)
    meta = {"app_version": body.app_version, "os": body.os, "ip": get_client_ip(request)}
    res = await device_auth.start_device_login(device_id, meta)
    res["device_id"] = device_id
    logger.info("[AUTH] device/start device_id=%s user_code=%s", device_id[:8], res.get("user_code"))
    return res


@router.post("/device/poll", summary="Поллинг подтверждения")
async def device_poll(body: DevicePollRequest, request: Request):
    _ip_limit(request, 120)
    res = await device_auth.poll_device_login(body.device_code)
    if res.get("status") in ("not_found", "expired", "denied"):
        raise HTTPException(status_code=400, detail=res["status"])
    return res


@router.post("/refresh", summary="Ротация токенов")
async def refresh(body: RefreshRequest, request: Request):
    _ip_limit(request, 60)
    res = await device_auth.refresh_tokens(body.refresh_token)
    if res.get("error"):
        # tier_lost/hwid_banned = доступ снят (403); прочее (невалидный/просроченный/отозванный ключ) = 401.
        code = 403 if res["error"] in ("tier_lost", "hwid_banned") else 401
        raise HTTPException(status_code=code, detail=res["error"])
    return res


@router.post("/logout", summary="Отзыв устройства")
async def logout(body: LogoutRequest):
    await device_auth.revoke_device_token(body.refresh_token)
    return {"ok": True}


@router.post("/ws-ticket", summary="Одноразовый тикет для WebSocket")
async def ws_ticket(claims: dict = Depends(get_device_claims)):
    # Голосовой WS — только полным тирам; laws (универсальный ключ) отсекаем на выдаче тикета
    # (вторичный рубеж — voice._authorize тоже отвергает laws на accept).
    ensure_full_access(claims)
    return device_auth.issue_ws_ticket(claims)


# END OF FILE api/routers/auth.py
