# START OF FILE services/license.py

"""
Лицензирование оверлея по КЛЮЧУ + привязка к железу (HWID).

Поток (заменяет device-code/deep-link):
  1) бот выдаёт юзеру ключ RPOV-XXXXX-XXXXX-XXXXX-XXXXX (issue_key, привязан к user_id+tier);
  2) юзер вставляет ключ в оверлей; клиент считает HWID (отпечаток железа) + имеет пер-машинный
     ed25519-keypair; запрашивает nonce (issue_challenge) и подписывает «HWID принадлежит этой машине»;
  3) activate(key, hwid, pubkey, nonce, sig, ...) валидирует формат+nonce+подпись+ключ+tier+hwid-бан,
     привязывает ключ к HWID (1 ключ = 1 ПК; сам-ребайнд LICENSE_REBIND_MAX раз) и выдаёт токены
     через device_auth.issue_tokens (форма ответа = как у device-code «approved» → клиент не меняем).

Безопасность:
  * key хранится плейнтекстом (бот переотображает), лукап по key_lookup=sha256(normalize) — сырой
    ключ не попадает в WHERE/логи; 95 бит энтропии + check-char (анти-опечатка/анти-форж);
  * signed-challenge (nonce+hwid, подпись пер-машинным ключом) бьёт replay HWID с другой машины;
  * hwid+key_id вшиты в JWT-claims и перепроверяются на КАЖДОМ refresh (device_auth.refresh_tokens);
  * бан юзера в боте каскадит в hwid_bans → нельзя переактивироваться на том же ПК даже с нового аккаунта.
  * Главный рубеж: вся ценность оверлея за серверным JWT — взлом клиента бесполезен без валидного ключа.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

import config
from database.manager import db_manager
from services import device_auth
from services import license_events

logger = logging.getLogger(__name__)


def _parse_jsonb(v: Any) -> Optional[dict]:
    """asyncpg отдаёт JSONB как str (кодек не зарегистрирован) — приводим к dict для детекторов."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None

# Алфавит без двусмысленных символов (0/O, 1/I) — тот же, что у user-code device-auth.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 32 символа = 5 бит/символ
_PREFIX = config.LICENSE_KEY_PREFIX               # 'RPOV'
_BODY_LEN = 20                                    # 4 группы по 5 → 19 случайных + 1 check
_GROUP = 5
# Канонический текст challenge-подписи. ДОЛЖЕН совпадать байт-в-байт с клиентом (C++ Hwid::signChallenge).
_CHAL_PREFIX = b"RPOVERLAY-CHAL-v1\n"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(v: Optional[dict]) -> str:
    return json.dumps(v or {}, ensure_ascii=False)


def _audit_components(components: Optional[dict], device_meta: Optional[dict]) -> dict:
    """Хранимый hwid_components = покомпонентные HWID-хеши + аудит активации под зарезервированными
    ключами `_audit`/`_integrity` (префикс `_` не коллизит с HWID-компонентами machineGuid/smbiosUuid/…).
    Сюда же кладём харденинг-снимок клиента (анти-дебаг) — видно «ключ активировали под отладчиком»."""
    out = dict(components or {})
    meta = device_meta or {}
    integ = meta.get("integrity")
    out["_audit"] = {
        "app_version": meta.get("app_version"),
        "os": meta.get("os"),
        "ip": meta.get("ip"),
        "at": _now().isoformat(),
    }
    if integ:
        out["_integrity"] = integ
    return out


# === Формат ключа ===
def _checksum_char(payload: str) -> str:
    """Взвешенная контрольная сумма (ловит опечатки и перестановки). Формула ДОЛЖНА совпадать с клиентом."""
    total = sum((i + 1) * _ALPHABET.index(ch) for i, ch in enumerate(payload))
    return _ALPHABET[total % 32]


def _canon(raw: str) -> str:
    """Компактный канон: upper, только буквы/цифры (срезает дефисы/пробелы)."""
    return "".join(c for c in (raw or "").upper() if c.isalnum())


def normalize_key(raw: str) -> str:
    """Канонический компактный вид (PREFIX + 20 body, без дефисов) — основа для key_lookup."""
    return _canon(raw)


def _key_lookup(raw: str) -> str:
    return hashlib.sha256(normalize_key(raw).encode()).hexdigest()


def validate_format(raw: str) -> bool:
    """Локальная пре-валидация формата (префикс, длина, алфавит, check-char) без БД."""
    c = _canon(raw)
    if not c.startswith(_PREFIX):
        return False
    body = c[len(_PREFIX):]
    if len(body) != _BODY_LEN or any(ch not in _ALPHABET for ch in body):
        return False
    return _checksum_char(body[:-1]) == body[-1]


def _format_key(body20: str) -> str:
    groups = [body20[i:i + _GROUP] for i in range(0, _BODY_LEN, _GROUP)]
    return _PREFIX + "-" + "-".join(groups)


def generate_key() -> str:
    """Случайный ключ RPOV-XXXXX-XXXXX-XXXXX-XXXXX (19 случайных символов + check-char)."""
    payload = "".join(secrets.choice(_ALPHABET) for _ in range(_BODY_LEN - 1))
    return _format_key(payload + _checksum_char(payload))


# === Выдача / просмотр ключа (вызывается ботом) ===
async def _unique_key() -> tuple[str, str]:
    key = generate_key()
    lookup = _key_lookup(key)
    for _ in range(6):  # коллизия по key_lookup практически невозможна, но подстрахуемся
        if not await db_manager.fetchval("SELECT 1 FROM license_keys WHERE key_lookup=$1", lookup):
            break
        key = generate_key()
        lookup = _key_lookup(key)
    return key, lookup


async def issue_key(user_id: int, tier: str, rebind_max: Optional[int] = None) -> str:
    """Гасит прежний активный ключ юзера (1 активный/аккаунт) и выдаёт новый. Возвращает ключ (для показа).
    Универсальные ключи (laws-билд) живут отдельно и здесь НЕ трогаются."""
    rmax = config.LICENSE_REBIND_MAX if rebind_max is None else int(rebind_max)
    await db_manager.execute(
        "UPDATE license_keys SET status='revoked' WHERE user_id=$1 AND status='active' AND universal=FALSE",
        user_id,
    )
    key, lookup = await _unique_key()
    await db_manager.execute(
        """INSERT INTO license_keys (key, key_lookup, user_id, tier, status, rebind_max)
           VALUES ($1, $2, $3, $4, 'active', $5)""",
        key, lookup, user_id, tier, rmax,
    )
    logger.info("[LICENSE] issued key for user_id=%s tier=%s (…%s)", user_id, tier, key[-4:])
    return key


async def get_active_key(user_id: int) -> Optional[str]:
    return await db_manager.fetchval(
        "SELECT key FROM license_keys WHERE user_id=$1 AND status='active' AND universal=FALSE", user_id
    )


# === Challenge (анти-replay): in-process nonce-store (как device_auth._WS_TICKETS) ===
_LICENSE_NONCES: Dict[str, Dict[str, Any]] = {}


def _prune_nonces() -> None:
    now = _now()
    for k in [k for k, v in _LICENSE_NONCES.items() if v["exp"] <= now]:
        _LICENSE_NONCES.pop(k, None)


def issue_challenge(hwid: str) -> Dict[str, Any]:
    _prune_nonces()
    nonce = secrets.token_urlsafe(24)
    _LICENSE_NONCES[nonce] = {"hwid": hwid, "exp": _now() + timedelta(seconds=config.LICENSE_CHALLENGE_TTL_SECONDS)}
    return {"nonce": nonce, "expires_in": config.LICENSE_CHALLENGE_TTL_SECONDS}


def _consume_nonce(nonce: str, hwid: str) -> bool:
    """Одноразовый расход nonce; он должен соответствовать тому же hwid, на который выдавался."""
    _prune_nonces()
    data = _LICENSE_NONCES.pop(nonce, None)
    if not data or data["exp"] <= _now():
        return False
    return data.get("hwid") == hwid


def _verify_sig(pubkey_b64: str, message: bytes, sig_b64: str) -> bool:
    """ed25519: подпись доказывает, что HWID посчитан на ЭТОЙ машине (приват-ключ derive из железа)."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        pub.verify(base64.b64decode(sig_b64), message)
        return True
    except Exception:
        return False


async def _has_tester_access(user_id: int) -> bool:
    """Гейт сборки 'full': доступ только тестерам/админам. Читает живые флаги users
    (паттерн compute_access_tier). Нет строки / ошибка чтения → False (фейл-клоуз)."""
    try:
        row = await db_manager.fetchrow(
            "SELECT is_tester, is_admin FROM users WHERE user_id = $1", user_id
        )
    except Exception:
        logger.exception("[LICENSE] не удалось прочитать tester-флаги user_id=%s", user_id)
        return False
    if not row:
        return False
    return bool(row.get("is_tester") or row.get("is_admin"))


# === Активация ===
async def activate(
    key: str, hwid: str, pubkey: str, nonce: str, sig: str,
    device_id: str, components: Optional[dict] = None, device_meta: Optional[dict] = None,
    variant: Optional[str] = None,
) -> Dict[str, Any]:
    """Полный пайплайн активации. Возвращает токены {access_token,...} или {error: <код>}."""
    if not validate_format(key):
        return {"error": "invalid_key_format"}
    if not _consume_nonce(nonce or "", hwid or ""):
        return {"error": "invalid_challenge"}
    msg = _CHAL_PREFIX + (hwid or "").encode() + b"\n" + (nonce or "").encode()
    if not (pubkey and sig and _verify_sig(pubkey, msg, sig)):
        return {"error": "bad_signature"}

    row = await db_manager.fetchrow(
        "SELECT id, user_id, tier, status, hwid, hwid_pubkey, hwid_components, rebind_count, rebind_max, "
        "last_rebind_at, universal "
        "FROM license_keys WHERE key_lookup = $1",
        _key_lookup(key),
    )
    if not row or row["status"] != "active":
        return {"error": "invalid_key"}
    user_id = row["user_id"]
    key_id = row["id"]
    prior_components = _parse_jsonb(row["hwid_components"])   # для детектора дрейфа (Слой C)

    if row.get("universal"):
        # === Универсальный ключ (laws-билд): один ключ на все машины ===
        # Работает ТОЛЬКО в сборке laws (в full/qa им не активироваться — иначе это бесплатный
        # полный оверлей). БЕЗ привязки HWID/лимита активаций: N машин на один ключ — норма.
        # Тир фиксированный из строки ключа ('laws'), НЕ зависит от флагов users владельца.
        if (variant or "") != "laws":
            return {"error": "invalid_key"}
        if await db_manager.fetchval("SELECT 1 FROM hwid_bans WHERE hwid=$1", hwid):
            return {"error": "hwid_banned"}
        access, refresh, ttl = await device_auth.issue_tokens(
            user_id, device_id, row["tier"], hwid=hwid, key_id=key_id, universal=True
        )
        logger.info("[LICENSE] universal activate key_id=%s hwid=…%s", key_id, (hwid or "")[-6:])
        # БЕЗ license_events.process_signed_event: детект шеринга (velocity/multi-IP/дрейф) по
        # определению считает «1 ключ на N машин» аномалией — универсальный ключ он бы забанил/зафлудил.
        return {"access_token": access, "refresh_token": refresh, "access_expires_in": ttl,
                "tier": row["tier"]}

    tier = await device_auth.compute_access_tier(user_id)
    if not tier:
        return {"error": "tier_lost"}                 # бан/блок/потеря доступа
    if await db_manager.fetchval("SELECT 1 FROM hwid_bans WHERE hwid=$1", hwid):
        return {"error": "hwid_banned"}
    if variant == "full" and not await _has_tester_access(user_id):
        return {"error": "tester_only"}               # сборка 'full' — только тестеры/админы

    stored = _audit_components(components, device_meta)   # HWID-компоненты + аудит/харденинг-снимок
    bound_hwid = row["hwid"]
    if bound_hwid is None:
        # первая привязка
        await db_manager.execute(
            "UPDATE license_keys SET hwid=$2, hwid_pubkey=$3, hwid_components=$4::jsonb, bound_at=NOW() WHERE id=$1",
            key_id, hwid, pubkey, _json(stored),
        )
    elif bound_hwid == hwid:
        # реактивация на том же ПК — без списания ребайнда
        if row["hwid_pubkey"] and row["hwid_pubkey"] != pubkey:
            return {"error": "pubkey_mismatch"}
        await db_manager.execute(
            "UPDATE license_keys SET hwid_pubkey=$2, hwid_components=$3::jsonb WHERE id=$1",
            key_id, pubkey, _json(stored),
        )
    else:
        # Другой ПК → НЕ ребайндим АВТОМАТИЧЕСКИ (требование заказчика). Возвращаем ошибку и
        # отправляем юзера в бота: там кнопка «🔄 Сменить ПК» (self_reset_hwid, бюджет rebind_max)
        # сбросит привязку → после этого ключ активируется на новом ПК как первичная привязка.
        return {"error": "hwid_mismatch"}

    access, refresh, ttl = await device_auth.issue_tokens(
        user_id, device_id, tier, hwid=hwid, key_id=key_id
    )
    logger.info("[LICENSE] activated key_id=%s user_id=%s hwid=…%s", key_id, user_id, (hwid or "")[-6:])

    # Серверный детект аномалий (Слой C): pubkey доказан подписью challenge → детекторы могут эскалировать.
    # Не влияет на выдачу токенов (полностью изолирован — log-and-swallow внутри).
    meta = device_meta or {}
    await license_events.process_signed_event(
        "activate", key_lookup=_key_lookup(key), user_id=user_id, hwid=hwid, hwid_pubkey=pubkey,
        ip=meta.get("ip"), components=components, integrity=meta.get("integrity"),
        stored_components=prior_components, bound_hwid=bound_hwid,
        app_version=meta.get("app_version"), os=meta.get("os"),
    )
    return {"access_token": access, "refresh_token": refresh, "access_expires_in": ttl, "tier": tier}


# === Управление (бот: админ / юзер) ===
async def revoke_key(user_id: int) -> None:
    """Отозвать активный ЛИЧНЫЙ ключ юзера + погасить его устройства (refresh даст key_revoked).
    Универсальный ключ (числится за админом-владельцем) отзывается только revoke_universal_key."""
    await db_manager.execute(
        "UPDATE license_keys SET status='revoked' WHERE user_id=$1 AND status='active' AND universal=FALSE",
        user_id,
    )
    await db_manager.execute(
        "UPDATE device_tokens SET revoked=TRUE WHERE user_id=$1 AND revoked=FALSE AND universal=FALSE",
        user_id,
    )


async def reset_hwid(user_id: int) -> bool:
    """Админ-сброс привязки: ключ можно активировать на новом ПК заново (счётчик ребайндов обнуляется).
    Гасит текущие устройства юзера. True если был активный ключ."""
    rid = await db_manager.fetchval(
        "UPDATE license_keys SET hwid=NULL, hwid_pubkey=NULL, hwid_components=NULL, "
        "rebind_count=0, last_rebind_at=NULL, bound_at=NULL "
        "WHERE user_id=$1 AND status='active' AND universal=FALSE RETURNING id",
        user_id,
    )
    await db_manager.execute(
        "UPDATE device_tokens SET revoked=TRUE WHERE user_id=$1 AND revoked=FALSE AND universal=FALSE",
        user_id,
    )
    return rid is not None


async def self_reset_hwid(user_id: int) -> Dict[str, Any]:
    """Самосброс привязки ключа к ПК (кнопка «🔄 Сменить ПК» в боте). Бюджет = rebind_max попыток.
    Чистит hwid-привязку (→ ключ активируется на НОВОМ ПК как первичная привязка), тратит одну
    попытку (rebind_count++), гасит текущие устройства. Бюджет исчерпан → {'error':'rebind_limit'}
    (дальше только админ-сброс reset_hwid). Заменяет прежний АВТО-ребайнд при активации."""
    row = await db_manager.fetchrow(
        "SELECT id, hwid, rebind_count, rebind_max FROM license_keys "
        "WHERE user_id=$1 AND status='active' AND universal=FALSE",
        user_id,
    )
    if not row:
        return {"error": "no_key"}
    remaining_before = max(0, row["rebind_max"] - row["rebind_count"])
    if row["hwid"] is None:
        return {"ok": True, "already_free": True, "remaining": remaining_before}
    if row["rebind_count"] >= row["rebind_max"]:
        return {"error": "rebind_limit"}
    await db_manager.execute(
        "UPDATE license_keys SET hwid=NULL, hwid_pubkey=NULL, hwid_components=NULL, "
        "rebind_count=rebind_count+1, last_rebind_at=NOW(), bound_at=NULL WHERE id=$1",
        row["id"],
    )
    await db_manager.execute(
        "UPDATE device_tokens SET revoked=TRUE WHERE user_id=$1 AND revoked=FALSE AND universal=FALSE",
        user_id,
    )
    remaining = max(0, row["rebind_max"] - (row["rebind_count"] + 1))
    logger.info("[LICENSE] self-reset hwid user_id=%s (remaining=%s)", user_id, remaining)
    return {"ok": True, "remaining": remaining}


# === Универсальные ключи (laws-билд: один ключ на всех, без HWID, отзывной) ===
async def issue_universal_key(owner_user_id: int, tier: str = "laws") -> str:
    """Выдать универсальный ключ (раздаётся вместе с laws-установщиком). owner_user_id — админ,
    выпустивший ключ (аудит; его личный ключ/устройства НЕ затрагиваются). Активных универсальных
    ключей может быть несколько (индекс уникальности на них не действует)."""
    key, lookup = await _unique_key()
    await db_manager.execute(
        """INSERT INTO license_keys (key, key_lookup, user_id, tier, status, rebind_max, universal)
           VALUES ($1, $2, $3, $4, 'active', 0, TRUE)""",
        key, lookup, owner_user_id, tier,
    )
    logger.info("[LICENSE] issued UNIVERSAL key tier=%s by user_id=%s (…%s)", tier, owner_user_id, key[-4:])
    return key


async def list_universal_keys(include_revoked: bool = False) -> list[dict]:
    """Все универсальные ключи + число живых устройств на каждом (для админ-панели)."""
    rows = await db_manager.fetch(
        "SELECT lk.id, lk.key, lk.tier, lk.status, lk.created_at, lk.user_id, "
        "  (SELECT COUNT(*) FROM device_tokens dt WHERE dt.key_id = lk.id AND dt.revoked = FALSE) "
        "    AS active_devices "
        "FROM license_keys lk WHERE lk.universal = TRUE "
        + ("" if include_revoked else "AND lk.status = 'active' ")
        + "ORDER BY lk.id DESC"
    )
    return [dict(r) for r in rows]


async def revoke_universal_key(key_id: int) -> Optional[int]:
    """Отозвать универсальный ключ: все laws-билды с ним перестают работать (refresh → key_revoked
    в пределах TTL access-токена ~15 мин; повторная активация → invalid_key). Гасим устройства по
    key_id (НЕ по user_id — не задеваем личные устройства владельца). Возвращает число погашенных
    устройств или None, если активного ключа с таким id нет."""
    rid = await db_manager.fetchval(
        "UPDATE license_keys SET status='revoked' WHERE id=$1 AND universal=TRUE AND status='active' "
        "RETURNING id",
        key_id,
    )
    if rid is None:
        return None
    res = await db_manager.execute(
        "UPDATE device_tokens SET revoked=TRUE WHERE key_id=$1 AND revoked=FALSE", key_id
    )
    try:
        n = int(str(res).split()[-1])   # asyncpg: 'UPDATE 7'
    except (ValueError, IndexError):
        n = 0
    logger.info("[LICENSE] UNIVERSAL key_id=%s revoked (%s devices killed)", key_id, n)
    return n


async def verify(hwid: str, pubkey: str, nonce: str, sig: str,
                 variant: Optional[str] = None,
                 components: Optional[dict] = None, client_integrity: Optional[dict] = None,
                 ip: Optional[str] = None, app_version: Optional[str] = None,
                 os: Optional[str] = None) -> Dict[str, Any]:
    """Перепроверка ВСЕХ условий активации для ТЕКУЩЕЙ машины — вызывается клиентом на КАЖДОМ запуске
    (даже после успешного refresh-входа). Доказательство «это та же машина» = подпись свежего nonce
    пер-машинным ключом, сверяемая с pubkey, ПРИВЯЗАННЫМ к активному ключу.
    pubkey выводится из СТАБИЛЬНЫХ компонентов (SMBIOS+MachineGuid+disk+cpu) → смена MAC проверку НЕ
    ломает, а замена ядра железа / копирование токенов на другой ПК — ломает (новый/чужой pubkey).
    components/client_integrity (опц., с 0.3.x клиента) → НЕПРЕРЫВНЫЙ детект дрейфа/integrity (Слой C)."""
    if not _consume_nonce(nonce or "", hwid or ""):
        return {"error": "invalid_challenge"}
    msg = _CHAL_PREFIX + (hwid or "").encode() + b"\n" + (nonce or "").encode()
    if not (pubkey and sig and _verify_sig(pubkey, msg, sig)):
        return {"error": "bad_signature"}
    # На одной машине (pubkey из железа) могут быть активные ключи НЕСКОЛЬКИХ аккаунтов
    # (реальный кейс: старый акк-подписчик + новый акк-тестер одного человека). fetchrow брал
    # произвольный → verify считал доступ не тому юзеру и валил легитимного (tier_lost-петля).
    # Берём первого кандидата с валидным tier'ом.
    rows = await db_manager.fetch(
        "SELECT id, user_id, hwid, key_lookup, hwid_components FROM license_keys "
        "WHERE hwid_pubkey=$1 AND status='active' ORDER BY id DESC",
        pubkey,
    )
    if not rows:
        return {"error": "not_activated"}      # на этой машине нет активной привязки ключа
    row, tier = None, None
    for cand in rows:
        t = await device_auth.compute_access_tier(cand["user_id"])
        if t:
            row, tier = cand, t
            break
    if not row:
        return {"error": "tier_lost"}
    bound_hwid = row["hwid"]
    if bound_hwid and await db_manager.fetchval("SELECT 1 FROM hwid_bans WHERE hwid=$1", bound_hwid):
        return {"error": "hwid_banned"}
    if variant == "full" and not await _has_tester_access(row["user_id"]):
        return {"error": "tester_only"}               # сборка 'full' — только тестеры/админы

    # Серверный детект аномалий (Слой C) — НЕПРЕРЫВНО на каждом запуске. Pubkey доказан подписью.
    # Не блокирует verify (флаг + лог + градуированная реакция); авто-бан (если включён) сработает на
    # СЛЕДУЮЩЕМ запуске через hwid_banned. Изолирован (log-and-swallow), на ответ не влияет.
    await license_events.process_signed_event(
        "verify", key_lookup=row["key_lookup"], user_id=row["user_id"], hwid=hwid, hwid_pubkey=pubkey,
        ip=ip, components=components, integrity=client_integrity,
        stored_components=_parse_jsonb(row["hwid_components"]), bound_hwid=bound_hwid,
        app_version=app_version, os=os,
    )
    return {"ok": True, "tier": tier}


async def _user_hwids(user_id: int) -> list[str]:
    rows = await db_manager.fetch(
        "SELECT DISTINCT hwid FROM license_keys WHERE user_id=$1 AND hwid IS NOT NULL", user_id
    )
    return [r["hwid"] for r in rows]


async def ban_hwids_for_user(user_id: int, banned_by: Optional[int] = None) -> int:
    """Каскад от бана юзера: внести его привязанный(е) HWID в hwid_bans + погасить устройства.
    Возвращает число забаненных HWID."""
    hwids = await _user_hwids(user_id)
    n = 0
    for h in hwids:
        await db_manager.execute(
            "INSERT INTO hwid_bans (hwid, reason, banned_by, source) VALUES ($1, $2, $3, 'user_ban_cascade') "
            "ON CONFLICT (hwid) DO NOTHING",
            h, f"user_ban user_id={user_id}", banned_by,
        )
        n += 1
    # universal=FALSE: бан владельца-админа НЕ должен гасить универсальные laws-сессии
    # (они отзываются только revoke_universal_key). Личные устройства юзера — гасим.
    await db_manager.execute(
        "UPDATE device_tokens SET revoked=TRUE WHERE user_id=$1 AND revoked=FALSE AND universal=FALSE",
        user_id,
    )
    if n:
        logger.info("[LICENSE] ban cascade: user_id=%s → %d hwid(s) banned", user_id, n)
    return n


async def unban_hwids_for_user(user_id: int) -> int:
    """Каскад от разбана: снять ТОЛЬКО каскадные баны HWID этого юзера (ручные баны не трогаем)."""
    hwids = await _user_hwids(user_id)
    n = 0
    for h in hwids:
        res = await db_manager.execute(
            "DELETE FROM hwid_bans WHERE hwid=$1 AND source='user_ban_cascade'", h
        )
        n += 1
    return n


async def ban_hwid(hwid: str, banned_by: Optional[int] = None, reason: Optional[str] = None) -> None:
    await db_manager.execute(
        "INSERT INTO hwid_bans (hwid, reason, banned_by, source) VALUES ($1, $2, $3, 'manual') "
        "ON CONFLICT (hwid) DO UPDATE SET reason=EXCLUDED.reason, banned_by=EXCLUDED.banned_by",
        hwid, reason, banned_by,
    )
    # погасить любые устройства на этом HWID
    await db_manager.execute("UPDATE device_tokens SET revoked=TRUE WHERE hwid=$1 AND revoked=FALSE", hwid)


async def unban_hwid(hwid: str) -> None:
    await db_manager.execute("DELETE FROM hwid_bans WHERE hwid=$1", hwid)


# === HWID-гейт инсталлятора (Слой G): сверка лёгкого HWID из Pascal по ПЕРЕСЕЧЕНИЮ компонентов ===
# Инсталлятор (Inno) считает HWID иначе, чем Hwid.cpp (другой путь) → строгое равенство не сработает.
# Поэтому сверяем по покомпонентным хешам: реплицируем норм+хеш клиента и ищем пересечение с
# компонентами забаненных устройств. Любой сильный компонент в бане → banned.
_HWID_JUNK = {
    "TO BE FILLED BY O.E.M.", "DEFAULT STRING", "NONE", "SYSTEM SERIAL NUMBER", "SERIAL NUMBER",
    "NOT SPECIFIED", "BASE BOARD SERIAL NUMBER", "DEFAULT", "0",
    "00000000-0000-0000-0000-000000000000", "FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF",
}
_COMP_PREFIX = b"RPOVERLAY-COMP-v1\n"   # ДОЛЖЕН совпадать с src/Hwid.cpp kCompPrefix


def _hwid_norm(raw: Optional[str]) -> str:
    """Реплика src/Hwid.cpp::norm(): trim+upper, junk/all-F/all-0 → пусто, возвращает trim+upper
    (С пробелами/дефисами — как клиент). Должна совпадать байт-в-байт для матча хешей."""
    s = (raw or "").strip().upper()
    if not s or s in _HWID_JUNK:
        return ""
    flat = s.replace(" ", "").replace("-", "")
    if not flat or all(c == "F" for c in flat) or all(c == "0" for c in flat):
        return ""
    return s


def _comp_hash(name: str, raw: Optional[str]) -> Optional[str]:
    v = _hwid_norm(raw)
    if not v:
        return None
    return hashlib.sha256(_COMP_PREFIX + f"{name}={v}".encode("utf-8")).hexdigest()


async def hwid_status(machine_guid: Optional[str], disk_serial: Optional[str],
                      smbios_uuid: Optional[str]) -> Dict[str, Any]:
    """Гейт инсталлятора: True, если любой сильный компонент пересекается с компонентами забаненного
    устройства. Fail-soft внутри (ошибка БД → banned=False, не блокируем легит за флаки)."""
    incoming = {h for h in (
        _comp_hash("machineGuid", machine_guid),
        _comp_hash("diskSerial", disk_serial),
        _comp_hash("smbiosUuid", smbios_uuid),
    ) if h}
    if not incoming:
        return {"banned": False}
    try:
        rows = await db_manager.fetch(
            "SELECT lk.hwid_components FROM license_keys lk "
            "JOIN hwid_bans hb ON hb.hwid = lk.hwid "
            "WHERE lk.hwid_components IS NOT NULL"
        )
    except Exception:
        logger.exception("[LICENSE] hwid_status: ошибка выборки забаненных компонентов")
        return {"banned": False}
    banned_hashes: set[str] = set()
    for r in rows:
        comps = r["hwid_components"]
        if isinstance(comps, str):
            try:
                comps = json.loads(comps)
            except Exception:
                continue
        if isinstance(comps, dict):
            for k, v in comps.items():
                if not str(k).startswith("_") and isinstance(v, str):
                    banned_hashes.add(v)
    return {"banned": bool(incoming & banned_hashes)}


async def user_license_view(user_id: int) -> Dict[str, Any]:
    """Для админ-показа: активный ключ + привязка + ребайнды + бан-статус HWID (маскированно)."""
    row = await db_manager.fetchrow(
        "SELECT key, status, hwid, rebind_count, rebind_max, last_rebind_at, bound_at, created_at "
        "FROM license_keys WHERE user_id=$1 AND status='active' AND universal=FALSE",
        user_id,
    )
    if not row:
        return {"has_key": False}
    hwid = row["hwid"]
    banned = bool(await db_manager.fetchval("SELECT 1 FROM hwid_bans WHERE hwid=$1", hwid)) if hwid else False
    return {
        "has_key": True,
        "key": row["key"],
        "hwid_masked": (hwid[:8] + "…" + hwid[-6:]) if hwid else None,
        "hwid_full": hwid,   # полный HWID — для копирования и бана из админки
        "bound": hwid is not None,
        "rebind_count": row["rebind_count"],
        "rebind_max": row["rebind_max"],
        "last_rebind_at": row["last_rebind_at"],
        "bound_at": row["bound_at"],
        "hwid_banned": banned,
    }


# END OF FILE services/license.py
