# START OF FILE services/device_auth.py

"""
Device-auth для десктоп-оверлея: вход по Telegram-подписке/тестер-статусу БЕЗ секрета на клиенте.

Поток (OAuth 2.0 Device Authorization Grant, упрощённый):
  1) клиент -> start_device_login() -> device_code (секрет клиента) + user_code (короткий, показывается юзеру);
  2) юзер в Telegram-боте подтверждает (deep-link / меню /test) -> approve_device_login(user_code, user_id, tier);
  3) клиент поллит poll_device_login(device_code) -> когда approved -> выдаём access-JWT + refresh-токен,
     гасим прежнее активное устройство юзера (1 устройство на аккаунт);
  4) дальше клиент шлёт Authorization: Bearer <access>; по истечении -> refresh_tokens(refresh) (ротация).

Безопасность: refresh храним только SHA256-хэшем (как api_keys.key_hash); access — короткоживущий
stateless JWT (HS256), проверяется без БД на горячем пути; доступ привязан к ЖИВЫМ флагам users
(is_tester/is_admin) — при потере доступа refresh не продлевается, устройство отзывается.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional, Tuple

import jwt

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)

_ALG = "HS256"
# Алфавит user-code без двусмысленных символов (0/O, 1/I).
_USER_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _json(v: Optional[dict]) -> str:
    return json.dumps(v or {}, ensure_ascii=False)


def _gen_user_code(n: int = 6) -> str:
    return "".join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(n))


# === Доступ по флагам users ===
# Доступ к оверлею — ВИП-перк: админы, VIP (ручной is_vip + промо-безлимит is_unlimited),
# тестировщики. Заблокированным — отказ. ПОДПИСЧИКИ оверлей БОЛЬШЕ НЕ получают
# (решение 2026-06-22: оверлей переведён на ВИП, т.к. ВИП раздаётся за ввод промокода в игре).
# Порядок проверок = приоритет tier (влияет на лимиты/фичи на стороне сервера).
async def compute_access_tier(user_id: int) -> Optional[str]:
    """Уровень доступа к оверлею ('admin'|'vip'|'tester') или None, если доступа нет.

    is_unlimited (промо-ВИП за промокод) приравнен к is_vip → tier 'vip'."""
    try:
        row = await db_manager.fetchrow(
            "SELECT is_admin, is_vip, is_unlimited, is_tester, is_banned, is_blocked "
            "FROM users WHERE user_id = $1",
            user_id,
        )
    except Exception:
        logger.exception("[DEVICE] не удалось прочитать флаги доступа user_id=%s", user_id)
        return None
    if not row:
        return None
    if row.get("is_banned") or row.get("is_blocked"):
        return None
    if row.get("is_admin"):
        return "admin"
    if row.get("is_vip") or row.get("is_unlimited"):
        return "vip"
    if row.get("is_tester"):
        return "tester"
    return None


# === Device-code flow ===
async def start_device_login(device_id: str, client_meta: Optional[dict] = None) -> Dict[str, Any]:
    """Создаёт pending-заявку. Возвращает device_code (секрет клиента) и user_code (для юзера)."""
    device_code = secrets.token_urlsafe(32)
    # user_code уникален среди живых pending-заявок (короткий TTL → коллизии редки, но подстрахуемся).
    user_code = _gen_user_code()
    for _ in range(5):
        exists = await db_manager.fetchval(
            "SELECT 1 FROM device_auth_requests WHERE user_code = $1 AND status = 'pending' AND expires_at > NOW()",
            user_code,
        )
        if not exists:
            break
        user_code = _gen_user_code()

    expires_at = _now() + timedelta(seconds=config.DEVICE_CODE_TTL_SECONDS)
    await db_manager.execute(
        """
        INSERT INTO device_auth_requests (device_code, user_code, device_id, status, client_meta, expires_at)
        VALUES ($1, $2, $3, 'pending', $4::jsonb, $5)
        """,
        device_code, user_code, device_id, _json(client_meta), expires_at,
    )
    return {
        "device_code": device_code,
        "user_code": user_code,
        "expires_in": config.DEVICE_CODE_TTL_SECONDS,
        "interval": config.DEVICE_POLL_INTERVAL_SECONDS,
    }


async def approve_device_login(user_code: str, user_id: int, tier: str) -> bool:
    """Вызывается из БОТА после проверки доступа. Привязывает pending-заявку к юзеру. True если успешно."""
    user_code = (user_code or "").strip().upper()
    rid = await db_manager.fetchval(
        """
        UPDATE device_auth_requests
        SET status = 'approved', user_id = $2, tier = $3
        WHERE user_code = $1 AND status = 'pending' AND expires_at > NOW()
        RETURNING id
        """,
        user_code, user_id, tier,
    )
    return rid is not None


async def poll_device_login(device_code: str) -> Dict[str, Any]:
    """Поллинг клиентом. Когда approved — выдаём токены и помечаем заявку consumed."""
    row = await db_manager.fetchrow(
        "SELECT id, device_id, status, user_id, tier, expires_at FROM device_auth_requests WHERE device_code = $1",
        device_code,
    )
    if not row:
        return {"status": "not_found"}
    status = row["status"]
    if status == "consumed":
        return {"status": "consumed"}
    if status == "denied":
        return {"status": "denied"}
    if status == "pending":
        if row["expires_at"] <= _now():
            return {"status": "expired"}
        return {"status": "authorization_pending"}
    if status == "approved":
        access, refresh, ttl = await issue_tokens(row["user_id"], row["device_id"], row["tier"])
        await db_manager.execute(
            "UPDATE device_auth_requests SET status = 'consumed' WHERE id = $1", row["id"]
        )
        return {
            "status": "approved",
            "access_token": access,
            "refresh_token": refresh,
            "access_expires_in": ttl,
            "tier": row["tier"],
        }
    return {"status": status}


# === Токены ===
def _make_access(user_id: int, device_id: str, tier: str, jti: str,
                 hwid: Optional[str] = None, key_id: Optional[int] = None) -> str:
    now = _now()
    payload = {
        "sub": str(user_id),
        "user_id": user_id,
        "device_id": device_id,
        "tier": tier,
        "jti": jti,
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=config.DEVICE_ACCESS_TTL_SECONDS)).timestamp()),
    }
    if hwid:
        payload["hwid"] = hwid
    if key_id is not None:
        payload["key_id"] = key_id
    return jwt.encode(payload, config.DEVICE_JWT_SECRET, algorithm=_ALG)


async def issue_tokens(user_id: int, device_id: str, tier: str,
                       *, hwid: Optional[str] = None, key_id: Optional[int] = None,
                       universal: bool = False) -> Tuple[str, str, int]:
    """Гасит прежнее активное устройство юзера (1 устройство) и выдаёт новую пару токенов.
    hwid/key_id — привязка к железу и лицензионному ключу (ключ-флоу); legacy device-code → None.
    universal=True (универсальный laws-ключ): N машин на один ключ — прежние устройства НЕ гасим
    (иначе каждая новая машина выкидывала бы предыдущую), запись помечается universal."""
    if not universal:
        # Гасим только ОБЫЧНЫЕ сессии юзера: универсальные машины живут поверх личного устройства
        # владельца-админа и не участвуют в правиле «1 устройство на аккаунт».
        await db_manager.execute(
            "UPDATE device_tokens SET revoked = TRUE WHERE user_id = $1 AND revoked = FALSE "
            "AND universal = FALSE",
            user_id,
        )
    elif key_id is not None:
        # Универсальный ключ: НЕ вытесняем ДРУГИЕ машины, но гасим прежнюю живую сессию ЭТОЙ ЖЕ
        # машины (повторная активация тем же device_id) — иначе device_tokens растёт, а счётчик
        # active_devices в админ-панели завышается orphan-строками.
        await db_manager.execute(
            "UPDATE device_tokens SET revoked = TRUE "
            "WHERE key_id = $1 AND device_id = $2 AND revoked = FALSE",
            key_id, device_id,
        )
    refresh = secrets.token_urlsafe(48)
    jti = secrets.token_urlsafe(12)
    refresh_exp = _now() + timedelta(seconds=config.DEVICE_REFRESH_TTL_SECONDS)
    await db_manager.execute(
        """
        INSERT INTO device_tokens
            (user_id, device_id, refresh_token_hash, refresh_expires_at, access_jti, tier,
             revoked, last_seen_at, hwid, key_id, universal)
        VALUES ($1, $2, $3, $4, $5, $6, FALSE, NOW(), $7, $8, $9)
        """,
        user_id, device_id, _hash(refresh), refresh_exp, jti, tier, hwid, key_id, universal,
    )
    return _make_access(user_id, device_id, tier, jti, hwid, key_id), refresh, config.DEVICE_ACCESS_TTL_SECONDS


def verify_access(token: str) -> Optional[Dict[str, Any]]:
    """Stateless-проверка access-JWT (горячий путь, без БД). None если невалиден/просрочен."""
    if not token:
        return None
    try:
        claims = jwt.decode(token, config.DEVICE_JWT_SECRET, algorithms=[_ALG])
    except jwt.PyJWTError:
        return None
    if claims.get("typ") != "access":
        return None
    return claims


async def refresh_tokens(refresh_token: str) -> Dict[str, Any]:
    """Ротация: проверяем refresh + ЖИВОЙ доступ, выдаём новую пару. {error: ...} при отказе."""
    if not refresh_token:
        return {"error": "invalid_refresh"}
    row = await db_manager.fetchrow(
        "SELECT id, user_id, device_id, refresh_expires_at, revoked, hwid, key_id "
        "FROM device_tokens WHERE refresh_token_hash = $1",
        _hash(refresh_token),
    )
    if not row or row["revoked"]:
        return {"error": "invalid_refresh"}
    if row["refresh_expires_at"] <= _now():
        return {"error": "expired_refresh"}

    # Статус ключа проверяем ДО пересчёта тира: универсальный ключ (laws-билд) живёт СВОИМ тиром
    # из строки ключа и НЕ зависит от флагов users владельца — compute_access_tier для него дал бы
    # тир владельца-админа (эскалация) или None (tier_lost → сессии бы умерли).
    hwid = row.get("hwid")
    key_id = row.get("key_id")
    key_row = None
    if key_id is not None:
        key_row = await db_manager.fetchrow(
            "SELECT status, tier, universal FROM license_keys WHERE id = $1", key_id
        )
        if not key_row or key_row["status"] != "active":
            await db_manager.execute("UPDATE device_tokens SET revoked = TRUE WHERE id = $1", row["id"])
            return {"error": "key_revoked"}

    if key_row and key_row.get("universal"):
        tier = key_row["tier"]
    else:
        tier = await compute_access_tier(row["user_id"])
        if not tier:
            await db_manager.execute("UPDATE device_tokens SET revoked = TRUE WHERE id = $1", row["id"])
            return {"error": "tier_lost"}

    # HWID не забанен — иначе отзываем устройство (действует и на универсальные сессии).
    if hwid:
        banned = await db_manager.fetchval("SELECT 1 FROM hwid_bans WHERE hwid = $1", hwid)
        if banned:
            await db_manager.execute("UPDATE device_tokens SET revoked = TRUE WHERE id = $1", row["id"])
            return {"error": "hwid_banned"}

    new_refresh = secrets.token_urlsafe(48)
    jti = secrets.token_urlsafe(12)
    new_exp = _now() + timedelta(seconds=config.DEVICE_REFRESH_TTL_SECONDS)
    await db_manager.execute(
        """
        UPDATE device_tokens
        SET refresh_token_hash = $2, refresh_expires_at = $3, access_jti = $4, tier = $5, last_seen_at = NOW()
        WHERE id = $1
        """,
        row["id"], _hash(new_refresh), new_exp, jti, tier,
    )
    access = _make_access(row["user_id"], row["device_id"], tier, jti, hwid, key_id)
    return {
        "access_token": access,
        "refresh_token": new_refresh,
        "access_expires_in": config.DEVICE_ACCESS_TTL_SECONDS,
        "tier": tier,
    }


async def revoke_device_token(refresh_token: str) -> None:
    """Logout: отзыв по refresh-токену."""
    if refresh_token:
        await db_manager.execute(
            "UPDATE device_tokens SET revoked = TRUE WHERE refresh_token_hash = $1", _hash(refresh_token)
        )


async def is_device_active(user_id: int, device_id: str) -> bool:
    """Проверка на accept WS: устройство существует и не отозвано."""
    row = await db_manager.fetchrow(
        "SELECT 1 FROM device_tokens WHERE user_id = $1 AND device_id = $2 AND revoked = FALSE",
        user_id, device_id,
    )
    return row is not None


# === Одноразовые WS-тикеты (анти-replay для WebSocket) ===
# WS не проходит через HTTP-middleware и не должен таскать долгий токен в URL (логи).
# Клиент по access-токену получает короткий одноразовый тикет и предъявляет его при upgrade.
# Хранилище — в памяти процесса api (uvicorn в один процесс), TTL ~30с.
_WS_TICKETS: Dict[str, Dict[str, Any]] = {}


def _prune_ws_tickets() -> None:
    now = _now()
    for k in [k for k, v in _WS_TICKETS.items() if v["exp"] <= now]:
        _WS_TICKETS.pop(k, None)


def issue_ws_ticket(claims: Dict[str, Any]) -> Dict[str, Any]:
    _prune_ws_tickets()
    ticket = secrets.token_urlsafe(24)
    _WS_TICKETS[ticket] = {
        "user_id": claims.get("user_id"),
        "device_id": claims.get("device_id"),
        "tier": claims.get("tier"),
        "exp": _now() + timedelta(seconds=30),
    }
    return {"ticket": ticket, "expires_in": 30}


def consume_ws_ticket(ticket: str) -> Optional[Dict[str, Any]]:
    """Возвращает данные тикета и удаляет его (одноразовость). None если нет/просрочен."""
    _prune_ws_tickets()
    data = _WS_TICKETS.pop(ticket, None)
    if not data or data["exp"] <= _now():
        return None
    return data


# END OF FILE services/device_auth.py
