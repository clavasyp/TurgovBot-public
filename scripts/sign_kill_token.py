#!/usr/bin/env python3
"""
Офлайн-подписант кил-токена (Слой F плана усиления защиты).

Подписывает пер-устройство команду деактивации ОФЛАЙН-ключом релиза (тем же ed25519, что подписывает
манифесты апдейтов; приватного ключа на проде НЕТ). Запускается ВРУЧНУЮ на машине релиза. Захват
сервера/бота не позволяет подделать кил — приватный ключ только здесь.

Безопасность:
  * Токен привязан к ОДНОМУ target_pubkey — формата «всем» не существует.
  * Подписывается КАНОНИЧЕСКАЯ JSON-строка payload (ровно те байты проверит и сервер, и клиент).
  * Одноразовый nonce + expiry → нет реплея/переноса на другую машину.

Как получить target_pubkey и key_lookup для юзера (на сервере, суперадмин):
    SELECT hwid_pubkey, key_lookup FROM license_keys WHERE user_id=<id> AND status='active';

Пример:
    python scripts/sign_kill_token.py \
        --target-pubkey "<base64 pubkey>" --key-lookup "<sha256 hex>" --ttl 604800 \
        --key ~/.rpoverlay-release/ed25519_private.pem \
        --enqueue-url https://overlay.turgovbot.ru/v1/license/command/enqueue \
        --secret "$KILL_SWITCH_INTERNAL_SECRET" --created-by <твой_tg_id>

Без --enqueue-url печатает {payload, sig} в stdout (заливай вручную).
"""
import argparse
import base64
import json
import secrets
import sys
import time
from pathlib import Path


def canonical(token: dict) -> str:
    # ДОЛЖНО совпадать байт-в-байт с серверной/клиентской канонизацией: sorted keys, compact, UTF-8.
    return json.dumps(token, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Подписать кил-токен офлайн-ключом релиза")
    ap.add_argument("--target-pubkey", required=True, help="base64 ed25519-pub устройства (license_keys.hwid_pubkey)")
    ap.add_argument("--key-lookup", default="", help="sha256(normalize(key)) — для аудита/привязки")
    ap.add_argument("--ttl", type=int, default=7 * 24 * 3600, help="TTL токена, сек (по умолч. 7 дней)")
    ap.add_argument("--key", default=str(Path.home() / ".rpoverlay-release" / "ed25519_private.pem"),
                    help="приватный ed25519 PEM (НЕ в репозитории)")
    ap.add_argument("--enqueue-url", default="", help="если задан — POST {payload,sig} на сервер")
    ap.add_argument("--secret", default="", help="X-Internal-Secret для enqueue")
    ap.add_argument("--created-by", type=int, default=None, help="TG-id инициатора (аудит)")
    args = ap.parse_args()

    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception:
        print("Нужен пакет cryptography (pip install cryptography)", file=sys.stderr)
        return 2

    pem = Path(args.key)
    if not pem.exists():
        print(f"Нет приватного ключа: {pem}", file=sys.stderr)
        return 2
    sk = load_pem_private_key(pem.read_bytes(), password=None)
    if not isinstance(sk, Ed25519PrivateKey):
        print("Ключ не ed25519", file=sys.stderr)
        return 2

    now = int(time.time())
    token = {
        "action": "reap",
        "target_pubkey": args.target_pubkey,
        "key_lookup": args.key_lookup,
        "nonce": secrets.token_urlsafe(24),
        "issued_at": str(now),
        "expires_at": str(now + int(args.ttl)),
    }
    payload = canonical(token)
    sig = base64.b64encode(sk.sign(payload.encode("utf-8"))).decode()
    out = {"payload": payload, "sig": sig}
    if args.created_by is not None:
        out["created_by"] = args.created_by

    print(json.dumps(out, ensure_ascii=False, indent=2))

    if args.enqueue_url:
        try:
            import urllib.request
            req = urllib.request.Request(
                args.enqueue_url, data=json.dumps(out).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Internal-Secret": args.secret},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                print(f"\nENQUEUE {r.status}: {r.read().decode()}", file=sys.stderr)
        except Exception as e:
            print(f"\nENQUEUE FAILED: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
