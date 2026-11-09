# START OF FILE services/kill_switch.py

"""
Удалённый кил-свитч (Слой F плана усиления защиты) — серверная часть: ретрансляция подписанных
ОФЛАЙН-ключом релиза пер-устройство команд деактивации/чистки.

Инварианты безопасности (НЕ скомпрометировать):
  * Команда привязана к ОДНОМУ hwid_pubkey — формата «всем» не существует (нет broadcast).
  * Подпись делается ОФЛАЙН-ключом релиза (scripts/sign_kill_token.py), приватного ключа на проде НЕТ.
    Сервер лишь ВЕРИФИЦИРУЕТ подпись публичным ключом и кладёт в очередь → захват сервера не даёт
    подделать кил (нет приватного ключа). Клиент ПОВТОРНО верифицирует подпись вшитым публичным ключом.
  * Очередь, не «выстрелил-забыл»: команда pending и отдаётся при каждом verify()-чек-ине, пока не
    придёт подписанный устройством ACK.
  * payload — КАНОНИЧЕСКАЯ JSON-строка (ровно подписанные байты): клиент верифицирует sig над payload
    как есть, потом парсит. Никакого рассинхрона канонизации Python↔C++.

Реальное лишение доступа даёт СЕРВЕРНЫЙ лок (verify → hwid_banned), не рип. Рип — best-effort чистка
следов на чужой машине (бэкап/образ диска гарантированно не сотрёшь).
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import config
from database.manager import db_manager

logger = logging.getLogger(__name__)


def _update_pubkey() -> Optional[bytes]:
    try:
        return bytes.fromhex(config.OVERLAY_UPDATE_PUBKEY_HEX.strip())
    except Exception:
        logger.error("[kill_switch] OVERLAY_UPDATE_PUBKEY_HEX невалиден")
        return None


def _verify_token_sig(payload: str, sig_b64: str) -> bool:
    """ed25519: подпись офлайн-ключа релиза над КАНОНИЧЕСКИМ payload (ровно теми байтами)."""
    pub = _update_pubkey()
    if not pub or not payload or not sig_b64:
        return False
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(pub).verify(base64.b64decode(sig_b64), payload.encode("utf-8"))
        return True
    except Exception:
        return False


def _parse_unix(v: Any) -> Optional[int]:
    try:
        return int(str(v))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Постановка команды в очередь (вызывается из /v1/license/command/enqueue;
# payload+sig приходят от офлайн-подписанта sign_kill_token.py)
# ---------------------------------------------------------------------------
async def enqueue(payload: str, sig: str, created_by: Optional[int] = None) -> Dict[str, Any]:
    if not _verify_token_sig(payload, sig):
        return {"error": "bad_token_sig"}          # без приватного ключа подделать нельзя
    try:
        tok = json.loads(payload)
    except Exception:
        return {"error": "bad_payload"}
    if tok.get("action") != "reap":
        return {"error": "bad_action"}
    pubkey = tok.get("target_pubkey")
    nonce = tok.get("nonce")
    if not pubkey or not nonce:
        return {"error": "incomplete_token"}

    exp_unix = _parse_unix(tok.get("expires_at"))
    expires_at = datetime.fromtimestamp(exp_unix, tz=timezone.utc) if exp_unix else None
    if expires_at and expires_at <= datetime.now(timezone.utc):
        return {"error": "already_expired"}

    try:
        await db_manager.execute(
            """
            INSERT INTO license_commands
                (command_id, hwid_pubkey, key_lookup, action, payload, sig, status, created_by, expires_at)
            VALUES ($1, $2, $3, 'reap', $4, $5, 'pending', $6, $7)
            ON CONFLICT (command_id) DO NOTHING
            """,
            nonce, pubkey, tok.get("key_lookup"), payload, sig, created_by, expires_at,
        )
    except Exception as e:
        logger.exception("[kill_switch] enqueue упал: %s", e)
        return {"error": "db_error"}
    logger.warning("[kill_switch] поставлен reap для pubkey=…%s (by=%s)", (pubkey or "")[-8:], created_by)
    return {"ok": True, "command_id": nonce}


# ---------------------------------------------------------------------------
# Выдача pending-команды (вызывается из роутера verify ПОСЛЕ успешной проверки)
# ---------------------------------------------------------------------------
async def get_pending(hwid_pubkey: str) -> Optional[Dict[str, Any]]:
    if not hwid_pubkey:
        return None
    try:
        row = await db_manager.fetchrow(
            "SELECT command_id, payload, sig FROM license_commands "
            "WHERE hwid_pubkey=$1 AND status='pending' AND (expires_at IS NULL OR expires_at > NOW()) "
            "ORDER BY created_at ASC LIMIT 1",
            hwid_pubkey,
        )
    except Exception as e:
        logger.warning("[kill_switch] get_pending упал: %s", e)
        return None
    if not row:
        return None
    # Клиент верифицирует sig над payload вшитым публичным ключом + сверит target_pubkey == свой.
    return {"command_id": row["command_id"], "payload": row["payload"], "sig": row["sig"]}


# ---------------------------------------------------------------------------
# Подтверждение приёма (подписанный устройством ACK) — снимает команду с очереди
# ---------------------------------------------------------------------------
async def ack(command_id: str, hwid_pubkey: str, ack_sig: str) -> Dict[str, Any]:
    if not command_id or not hwid_pubkey:
        return {"error": "incomplete"}
    try:
        row = await db_manager.fetchrow(
            "SELECT status FROM license_commands WHERE command_id=$1 AND hwid_pubkey=$2",
            command_id, hwid_pubkey,
        )
    except Exception as e:
        logger.warning("[kill_switch] ack lookup упал: %s", e)
        return {"error": "db_error"}
    if not row:
        return {"error": "not_found"}
    if row["status"] == "acked":
        return {"ok": True}                     # идемпотентно

    # Проверяем подпись устройства поверх nonce (= command_id) тем же CHAL-форматом, что verify/activate.
    # target_pubkey == hwid_pubkey; bound_hwid берём из активного ключа этого устройства.
    ok = False
    try:
        from services.license import _CHAL_PREFIX, _verify_sig  # reuse; license не импортит kill_switch
        krow = await db_manager.fetchrow(
            "SELECT hwid FROM license_keys WHERE hwid_pubkey=$1 AND status='active'", hwid_pubkey
        )
        if krow and krow["hwid"] and ack_sig:
            msg = _CHAL_PREFIX + krow["hwid"].encode() + b"\n" + command_id.encode()
            ok = _verify_sig(hwid_pubkey, msg, ack_sig)
    except Exception as e:
        logger.warning("[kill_switch] ack verify упал: %s", e)

    if not ok:
        # Невалидный ACK ничего не делает (анти-DoS: чужой не снимет команду с очереди).
        return {"ok": False}
    try:
        await db_manager.execute(
            "UPDATE license_commands SET status='acked', acked_at=NOW(), ack_sig=$2 "
            "WHERE command_id=$1 AND status='pending'",   # AND status — без двойной записи при гонке
            command_id, ack_sig,
        )
    except Exception as e:
        logger.warning("[kill_switch] ack UPDATE упал: %s", e)
        return {"error": "db_error"}     # не 500: команда осталась pending (безопасная сторона)
    logger.warning("[kill_switch] reap ACK от pubkey=…%s (command reaped)", (hwid_pubkey or "")[-8:])
    return {"ok": True}


# END OF FILE services/kill_switch.py
