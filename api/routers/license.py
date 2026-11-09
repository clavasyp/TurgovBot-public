# START OF FILE api/routers/license.py

"""
Лицензирование оверлея по КЛЮЧУ + HWID (заменяет device-code/deep-link).

- POST /v1/license/challenge  — выдать одноразовый nonce под HWID (анти-replay)
- POST /v1/license/activate   — активировать ключ: формат+nonce+подпись+ключ+tier+hwid-бан → токены

Refresh остаётся на /v1/auth/refresh (теперь перепроверяет hwid-бан + статус ключа).
Публичные эндпоинты лимитируются по IP. Логика — services/license.py.
"""

import logging
import os
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

# Корень проекта в sys.path (как в auth.py/overlay.py) — для импорта services.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config
from services import license as license_service

from ..security import get_client_ip, rate_limiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/license", tags=["License"])


class ChallengeRequest(BaseModel):
    hwid: str = Field(..., min_length=16, max_length=128)


class ActivateRequest(BaseModel):
    key: str = Field(..., max_length=64)
    hwid: str = Field(..., min_length=16, max_length=128)
    pubkey: str = Field(..., max_length=128)          # ed25519 pub устройства (base64, raw 32 байта)
    nonce: str = Field(..., max_length=128)
    sig: str = Field(..., max_length=256)             # ed25519 подпись (base64, 64 байта)
    device_id: Optional[str] = Field(None, max_length=128)
    components: Optional[dict] = None                 # покомпонентные хеши HWID (нечёткий матч)
    app_version: Optional[str] = Field(None, max_length=32)
    os: Optional[str] = Field(None, max_length=64)
    client_integrity: Optional[dict] = None           # харденинг (Пиллар C): анти-дебаг снимок клиента (аудит)
    variant: Optional[str] = Field(None, max_length=16)  # вариант сборки ('full'=тестер-гейт | 'qa'/None=обычный tier-гейт)


def _ip_limit(request: Request, rpm: int) -> None:
    ip = get_client_ip(request)
    # Релей (РФ-обход) схлопывает всю когорту в ОДИН ip → без множителя общий бакет душил бы
    # легит-юзеров 429-ми (verify обязателен на каждом запуске; волна блокирующего обновления
    # синхронизирует рестарты).
    if ip in config.OVERLAY_RELAY_IPS:
        rpm *= config.OVERLAY_RELAY_RPM_MULT
    # path в ключе: иначе все license-эндпоинты делили бы ОДИН счётчик, и challenge/verify/телеметрия
    # выедали бы лимит activate (rpm=10 умирает первым).
    key = abs(hash(("license", request.url.path, ip))) % (2 ** 31)
    allowed, _ = rate_limiter.check_and_record(key, rpm)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests", headers={"Retry-After": "60"})


class VerifyRequest(BaseModel):
    hwid: str = Field(..., min_length=16, max_length=128)
    pubkey: str = Field(..., max_length=128)
    nonce: str = Field(..., max_length=128)
    sig: str = Field(..., max_length=256)
    variant: Optional[str] = Field(None, max_length=16)  # вариант сборки ('full'=тестер-гейт | 'qa'/None=обычный)
    # Опционально (клиент 0.3.x+): покомпонентные хеши HWID + анти-дебаг снимок → НЕПРЕРЫВНЫЙ детект
    # дрейфа/integrity на каждом запуске (Слой C). Старые клиенты не шлют — детект просто не работает по ним.
    components: Optional[dict] = None
    client_integrity: Optional[dict] = None
    app_version: Optional[str] = Field(None, max_length=32)
    os: Optional[str] = Field(None, max_length=64)


class TelemetryRequest(BaseModel):
    """НЕаутентифицированный канал детектов клиента (Слой D, дуал-сенд main+relay). Принимает
    неактивированных клиентов (как /challenge). event_id — для идемпотентного дедупа между каналами."""
    event_id: Optional[str] = Field(None, max_length=64)       # UUID; сервер дедупит по нему
    kind: str = Field("anomaly", max_length=32)                # debugger | tamper | manifest_sig | hwid_mismatch | ...
    detail: Optional[str] = Field(None, max_length=1000)
    hwid: Optional[str] = Field(None, max_length=128)
    hwid_pubkey: Optional[str] = Field(None, max_length=128)
    integrity: Optional[dict] = None
    app_version: Optional[str] = Field(None, max_length=32)
    os: Optional[str] = Field(None, max_length=64)


# error-код сервиса → HTTP-статус
_ERR_HTTP = {
    "invalid_key_format": 400, "bad_signature": 400, "invalid_challenge": 400, "pubkey_mismatch": 400,
    "invalid_key": 401, "key_revoked": 401,
    "tier_lost": 403, "hwid_banned": 403, "rebind_limit": 403, "rebind_cooldown": 403,
    "hwid_mismatch": 403, "not_activated": 403, "tester_only": 403,
}


@router.post("/challenge", summary="Одноразовый nonce под HWID")
async def challenge(body: ChallengeRequest, request: Request):
    _ip_limit(request, 30)
    return license_service.issue_challenge(body.hwid.strip())


@router.post("/activate", summary="Активация ключа (привязка к HWID → токены)")
async def activate(body: ActivateRequest, request: Request):
    _ip_limit(request, 10)
    device_id = (body.device_id or "").strip() or body.hwid.strip()
    meta = {"app_version": body.app_version, "os": body.os, "ip": get_client_ip(request),
            "integrity": body.client_integrity}
    res = await license_service.activate(
        key=body.key, hwid=body.hwid.strip(), pubkey=body.pubkey, nonce=body.nonce, sig=body.sig,
        device_id=device_id, components=body.components, device_meta=meta, variant=body.variant,
    )
    if res.get("error"):
        raise HTTPException(status_code=_ERR_HTTP.get(res["error"], 400), detail=res["error"])
    return res


@router.post("/verify", summary="Перепроверка условий активации для текущей машины (каждый запуск)")
async def verify(body: VerifyRequest, request: Request):
    # Клиент дёргает это на КАЖДОМ запуске (даже после refresh-входа): подписанный nonce доказывает,
    # что текущая машина владеет пер-машинным ключом, привязанным к активному лицензионному ключу.
    _ip_limit(request, 30)
    res = await license_service.verify(
        hwid=body.hwid.strip(), pubkey=body.pubkey, nonce=body.nonce, sig=body.sig, variant=body.variant,
        components=body.components, client_integrity=body.client_integrity, ip=get_client_ip(request),
        app_version=body.app_version, os=body.os,
    )
    if res.get("error"):
        raise HTTPException(status_code=_ERR_HTTP.get(res["error"], 400), detail=res["error"])
    # Кил-свитч (Слой F): если для ЭТОГО устройства есть pending-команда — отдаём её прямо в ответе
    # verify (pubkey уже доказан подписью challenge). Клиент сам верифицирует подпись токена и сверит
    # target_pubkey == свой. Ошибки очереди не валят verify.
    try:
        from services import kill_switch
        cmd = await kill_switch.get_pending(body.pubkey)
        if cmd:
            res["command"] = cmd
    except Exception:
        logger.exception("[license] get_pending command упал (не критично)")
    return res


@router.post("/telemetry", summary="Детект клиента (анти-дебаг/tamper/подпись) — журнал + алерт")
async def telemetry(body: TelemetryRequest, request: Request):
    # НЕаутентифицированный (клиент шлёт ДО активации тоже), дуал-сенд по обоим каналам → сервер дедупит
    # по event_id. Только ПИШЕТ событие и алертит — НЕ эскалирует (forged-телеметрия не должна банить).
    _ip_limit(request, 30)
    from services import license_events
    await license_events.record_anomaly_telemetry(
        event_id=(body.event_id or None), kind=body.kind, detail_text=body.detail,
        hwid_pubkey=body.hwid_pubkey, hwid=(body.hwid.strip() if body.hwid else None),
        ip=get_client_ip(request), app_version=body.app_version, os=body.os, integrity=body.integrity,
    )
    return {"ok": True}


# === Кил-свитч (Слой F): постановка подписанной команды в очередь + ACK устройства ===
class EnqueueCommandRequest(BaseModel):
    payload: str = Field(..., max_length=4096)        # каноническая JSON-строка токена (подписанные байты)
    sig: str = Field(..., max_length=256)             # ed25519(payload) офлайн-ключом релиза, base64
    created_by: Optional[int] = None                  # TG-id инициатора (аудит)


class AckCommandRequest(BaseModel):
    command_id: str = Field(..., max_length=128)      # = nonce токена
    hwid_pubkey: str = Field(..., max_length=128)
    ack_sig: str = Field(..., max_length=256)         # подпись устройства поверх nonce (CHAL-формат)


@router.post("/command/enqueue", summary="Поставить подписанную кил-команду в очередь (офлайн-подписант)")
async def command_enqueue(body: EnqueueCommandRequest, request: Request):
    # Доступ режется секретом-заголовком (анти-спам). Подделать команду без приватного ключа всё равно
    # нельзя — сервер верифицирует подпись публичным ключом перед постановкой.
    _ip_limit(request, 20)
    secret = request.headers.get("X-Internal-Secret", "")
    if not config.KILL_SWITCH_INTERNAL_SECRET or secret != config.KILL_SWITCH_INTERNAL_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")
    from services import kill_switch
    res = await kill_switch.enqueue(payload=body.payload, sig=body.sig, created_by=body.created_by)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    return res


@router.post("/command/ack", summary="Подтверждение приёма кил-команды (подписанный устройством ACK)")
async def command_ack(body: AckCommandRequest, request: Request):
    _ip_limit(request, 30)
    from services import kill_switch
    return await kill_switch.ack(command_id=body.command_id, hwid_pubkey=body.hwid_pubkey, ack_sig=body.ack_sig)


# === HWID-гейт инсталлятора (Слой G) ===
class HwidStatusRequest(BaseModel):
    machine_guid: Optional[str] = Field(None, max_length=128)
    disk_serial: Optional[str] = Field(None, max_length=128)
    smbios_uuid: Optional[str] = Field(None, max_length=128)


@router.post("/hwid-status", summary="Гейт инсталлятора: забанено ли это железо (по пересечению компонентов)")
async def hwid_status(body: HwidStatusRequest, request: Request):
    # Инсталлятор (Inno [Code]) дёргает ДО установки. fail-open у клиента при сетевой ошибке (не кирпичим
    # легит за флаки-сетью). Целостность ответа — по TLS (overlay.turgovbot.ru за Cloudflare); Inno не
    # умеет ed25519, поэтому подпись ответа не верифицируется в инсталляторе. РЕАЛЬНЫЙ барьер — серверный
    # лок при первом запуске (verify→hwid_banned), гейт лишь делает переустановку неудобной.
    _ip_limit(request, 20)
    return await license_service.hwid_status(
        machine_guid=body.machine_guid, disk_serial=body.disk_serial, smbios_uuid=body.smbios_uuid,
    )


# END OF FILE api/routers/license.py
