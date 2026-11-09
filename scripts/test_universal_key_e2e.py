# START OF FILE scripts/test_universal_key_e2e.py

"""
E2E-тест универсального ключа laws-билда (миграция 019 + services/license.py + device_auth.py).

Запуск В КОНТЕЙНЕРЕ api (после деплоя и alembic upgrade head):
    docker exec turgovbot-api-1 python scripts/test_universal_key_e2e.py

Прямые вызовы сервисов (как прошлые лицензионные e2e), клиентская ed25519-подпись симулируется
через cryptography. Использует throwaway-владельца с отрицательным user_id — НЕ пересекается с
реальными юзерами; в конце подчищает за собой (residue 0).

Сценарии:
  1. issue_universal_key → формат ключа валиден, ключ universal.
  2. Личный ключ владельца НЕ гасится выпуском универсального (и наоборот).
  3. activate variant='laws' на машине A → токены, tier='laws', привязки HWID НЕТ.
  4. activate на машине B → работает, устройство A ЖИВО (нет вытеснения).
  5. Универсальный ключ в variant='full'/'qa'/None → invalid_key.
  6. refresh устройства A → ок, tier='laws' (не тир владельца!).
  7. ensure_full_access({'tier':'laws'}) → 403 (AI-эндпоинты закрыты).
  8. license_events по универсальному ключу НЕ пишутся (детект шеринга не заденет).
  9. revoke_universal_key → refresh обеих машин → key_revoked; повторная активация → invalid_key.
 10. Устройства владельца (личные) живы после отзыва универсального ключа.
"""

import asyncio
import base64
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import config  # noqa: F401  (инициализация окружения)
from database.manager import db_manager
from services import license as lic
from services import device_auth

OWNER_ID = -900_001            # throwaway-владелец (админ, выпускающий ключ)
CHAL_PREFIX = b"RPOVERLAY-CHAL-v1\n"

PASS, FAIL = "✅", "❌"
failures = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"{PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


class Machine:
    """Симуляция клиентской машины: hwid + пер-машинный ed25519."""

    def __init__(self, seed: str):
        import hashlib
        self.hwid = hashlib.sha256(f"e2e-universal-{seed}".encode()).hexdigest()
        self.priv = Ed25519PrivateKey.generate()
        self.pub_b64 = base64.b64encode(self.priv.public_key().public_bytes_raw()).decode()
        self.device_id = f"e2e-dev-{seed}"

    def signed_challenge(self):
        ch = lic.issue_challenge(self.hwid)
        msg = CHAL_PREFIX + self.hwid.encode() + b"\n" + ch["nonce"].encode()
        sig = base64.b64encode(self.priv.sign(msg)).decode()
        return ch["nonce"], sig

    async def activate(self, key: str, variant="laws"):
        nonce, sig = self.signed_challenge()
        return await lic.activate(
            key=key, hwid=self.hwid, pubkey=self.pub_b64, nonce=nonce, sig=sig,
            device_id=self.device_id, variant=variant,
        )


async def cleanup():
    await db_manager.execute(
        "DELETE FROM device_tokens WHERE user_id=$1 OR device_id LIKE 'e2e-dev-%'", OWNER_ID)
    await db_manager.execute("DELETE FROM license_keys WHERE user_id=$1", OWNER_ID)
    await db_manager.execute(
        "DELETE FROM license_events WHERE key_lookup IN "
        "(SELECT key_lookup FROM license_keys WHERE user_id=$1)", OWNER_ID)
    await db_manager.execute("DELETE FROM users WHERE user_id=$1", OWNER_ID)


async def main():
    await db_manager.connect()
    await cleanup()
    # throwaway-владелец: админ (как реальный выпускающий)
    await db_manager.execute(
        "INSERT INTO users (user_id, full_name, is_admin) VALUES ($1, 'e2e universal owner', TRUE) "
        "ON CONFLICT (user_id) DO UPDATE SET is_admin=TRUE",
        OWNER_ID,
    )

    try:
        # 1-2. Выпуск: личный ключ владельца + универсальный, не гасят друг друга
        personal = await lic.issue_key(OWNER_ID, "admin")
        uni = await lic.issue_universal_key(OWNER_ID)
        check("формат универсального ключа", lic.validate_format(uni))
        row = await db_manager.fetchrow(
            "SELECT tier, status, universal FROM license_keys WHERE key_lookup=$1",
            lic._key_lookup(uni))
        check("универсальный ключ: universal=TRUE, tier='laws', active",
              bool(row and row["universal"] and row["tier"] == "laws" and row["status"] == "active"))
        pstat = await db_manager.fetchval(
            "SELECT status FROM license_keys WHERE key_lookup=$1", lic._key_lookup(personal))
        check("личный ключ владельца не задет выпуском универсального", pstat == "active")
        personal2 = await lic.issue_key(OWNER_ID, "admin")  # перевыпуск личного
        ustat = await db_manager.fetchval(
            "SELECT status FROM license_keys WHERE key_lookup=$1", lic._key_lookup(uni))
        check("перевыпуск личного ключа не гасит универсальный", ustat == "active")

        # 3. Активация на машине A
        ma = Machine("A")
        ra = await ma.activate(uni)
        check("активация A (variant=laws)", "access_token" in ra, str(ra))
        check("тир из ключа = laws", ra.get("tier") == "laws", str(ra.get("tier")))
        bound = await db_manager.fetchrow(
            "SELECT hwid, hwid_pubkey FROM license_keys WHERE key_lookup=$1", lic._key_lookup(uni))
        check("HWID НЕ привязан к ключу", bound and bound["hwid"] is None and bound["hwid_pubkey"] is None)

        # 4. Активация на машине B — A не вытесняется
        mb = Machine("B")
        rb = await mb.activate(uni)
        check("активация B (вторая машина)", "access_token" in rb, str(rb))
        alive = await db_manager.fetchval(
            "SELECT COUNT(*) FROM device_tokens WHERE device_id=$1 AND revoked=FALSE", ma.device_id)
        check("устройство A живо после активации B", alive == 1, f"alive={alive}")

        # 4b. Повторная активация A — дедуп: прежняя сессия A гасится, orphan-строки не копятся
        ra2 = await ma.activate(uni)
        check("повторная активация A", "access_token" in ra2, str(ra2))
        alive_a = await db_manager.fetchval(
            "SELECT COUNT(*) FROM device_tokens WHERE device_id=$1 AND revoked=FALSE", ma.device_id)
        check("A: ровно одна живая сессия после реактивации (дедуп)", alive_a == 1, f"alive_a={alive_a}")
        key_id_now = await db_manager.fetchval(
            "SELECT id FROM license_keys WHERE key_lookup=$1", lic._key_lookup(uni))
        devcount = next(k for k in await lic.list_universal_keys() if k["id"] == key_id_now)["active_devices"]
        check("active_devices считает машины, а не активации (=2)", devcount == 2, f"active_devices={devcount}")
        ra = ra2   # дальше refresh-тест идёт по свежей сессии A

        # 5. Универсальный ключ в чужих вариантах
        for v in ("full", "qa", None):
            r = await Machine(f"X-{v}").activate(uni, variant=v)
            check(f"universal в variant={v!r} отклонён", r.get("error") == "invalid_key", str(r))

        # 6. refresh A: тир laws, не admin
        rf = await device_auth.refresh_tokens(ra["refresh_token"])
        check("refresh A ок", "access_token" in rf, str(rf))
        check("refresh A: тир остаётся laws", rf.get("tier") == "laws", str(rf.get("tier")))

        # 7. Гейт AI-эндпоинтов
        from fastapi import HTTPException
        from api.dependencies import ensure_full_access
        try:
            ensure_full_access({"tier": "laws"})
            check("ensure_full_access(laws) → 403", False, "исключение не брошено")
        except HTTPException as e:
            check("ensure_full_access(laws) → 403", e.status_code == 403)
        for t in ("admin", "vip", "tester"):
            try:
                ensure_full_access({"tier": t})
                check(f"ensure_full_access({t}) пропускает", True)
            except HTTPException:
                check(f"ensure_full_access({t}) пропускает", False)

        # 8. license_events не писались по универсальному ключу
        nev = await db_manager.fetchval(
            "SELECT COUNT(*) FROM license_events WHERE key_lookup=$1", lic._key_lookup(uni))
        check("license_events по универсальному ключу = 0", nev == 0, f"events={nev}")

        # 9. Отзыв: все устройства гаснут, реактивация невозможна
        key_id = await db_manager.fetchval(
            "SELECT id FROM license_keys WHERE key_lookup=$1", lic._key_lookup(uni))
        killed = await lic.revoke_universal_key(key_id)
        check("revoke_universal_key вернул число устройств", killed is not None and killed >= 2,
              f"killed={killed}")
        # revoke_universal_key гасит строки device_tokens напрямую (по key_id) → refresh видит
        # revoked=TRUE раньше проверки ключа и отдаёт invalid_refresh; если бы гасился только статус
        # ключа — был бы key_revoked. Оба терминальны (клиент на ЛЮБОЙ refresh-фейл → окно активации).
        _TERMINAL = {"invalid_refresh", "key_revoked"}
        rf2 = await device_auth.refresh_tokens(rf["refresh_token"])
        check("refresh A после отзыва → терминальная ошибка", rf2.get("error") in _TERMINAL, str(rf2))
        rb2 = await device_auth.refresh_tokens(rb["refresh_token"])
        check("refresh B после отзыва → терминальная ошибка", rb2.get("error") in _TERMINAL, str(rb2))
        r3 = await Machine("C").activate(uni)
        check("реактивация отозванного универсального → invalid_key",
              r3.get("error") == "invalid_key", str(r3))
        check("повторный отзыв → None", await lic.revoke_universal_key(key_id) is None)

        # 10. Личный ключ владельца жив после отзыва универсального
        p2stat = await db_manager.fetchval(
            "SELECT status FROM license_keys WHERE key_lookup=$1", lic._key_lookup(personal2))
        check("личный ключ владельца жив после отзыва универсального", p2stat == "active")

    finally:
        await cleanup()
        residue = await db_manager.fetchval(
            "SELECT (SELECT COUNT(*) FROM license_keys WHERE user_id=$1) + "
            "(SELECT COUNT(*) FROM device_tokens WHERE user_id=$1) + "
            "(SELECT COUNT(*) FROM users WHERE user_id=$1)", OWNER_ID)
        print(f"\nresidue: {residue}")
        await db_manager.close()

    print(f"\n{'=' * 40}\n{'ВСЕ ТЕСТЫ ЗЕЛЁНЫЕ' if not failures else 'ПРОВАЛЫ: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())

# END OF FILE scripts/test_universal_key_e2e.py
