# START OF FILE services/telegram_login.py

"""
Веб-вход сайта TurgovBot через Telegram Login Widget.

Содержит:
- verify_telegram_login(data)  — проверка подписи payload'а виджета (HMAC по сырому SHA-256
                                 от токена бота) + проверка свежести auth_date.
- issue_web_session(user_id)   — выпуск подписанного HS256-JWT для cookie-сессии сайта.
- verify_web_session(token)    — декод+валидация JWT сессии (typ=='web'); None при любой ошибке.

Чистый модуль (без python-telegram-bot / FastAPI) — переиспользуется роутером api/.
"""

import hashlib
import hmac
import logging
import time

import jwt

import config

logger = logging.getLogger(__name__)

_ALG = "HS256"


def verify_telegram_login(data: dict) -> bool:
    """
    Проверяет подлинность payload'а от Telegram Login Widget (data-onauth).

    Алгоритм (по докам Telegram):
      secret = SHA256(bot_token)                       # СЫРОЙ digest, НЕ hexdigest
      data_check_string = '\\n'.join("k=v" по всем полям, кроме hash, отсортированным по ключу)
      hmac_sha256(secret, data_check_string).hexdigest() == data['hash']

    Дополнительно — защита от replay: подпись считается валидной только в пределах
    config.AUTH_DATE_MAX_AGE секунд от auth_date.

    Args:
        data: словарь полей виджета (id, first_name, ..., auth_date, hash).

    Returns:
        True — подпись валидна и свежая; False — иначе.
    """
    try:
        if not isinstance(data, dict):
            return False
        received_hash = data.get("hash")
        if not received_hash or not config.TELEGRAM_BOT_TOKEN:
            return False

        # Сырой (binary) SHA-256 от токена бота — это и есть HMAC-ключ.
        secret = hashlib.sha256(config.TELEGRAM_BOT_TOKEN.encode()).digest()

        # Все поля, кроме hash, в формате "key=value", отсортированные по ключу, через \n.
        data_check_string = "\n".join(
            f"{k}={data[k]}" for k in sorted(data) if k != "hash"
        )

        calc_hash = hmac.new(
            secret, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calc_hash, str(received_hash)):
            logger.warning("[WEB_LOGIN] HMAC подписи не совпал")
            return False

        # Свежесть: подпись не должна быть старше AUTH_DATE_MAX_AGE (анти-replay).
        try:
            auth_date = int(data.get("auth_date", 0))
        except (TypeError, ValueError):
            return False
        if auth_date <= 0:
            return False
        age = time.time() - auth_date
        if age > config.AUTH_DATE_MAX_AGE:
            logger.warning("[WEB_LOGIN] auth_date устарел: возраст %.0fs > %ss", age, config.AUTH_DATE_MAX_AGE)
            return False

        return True
    except Exception as e:
        logger.warning("[WEB_LOGIN] ошибка проверки подписи: %s", e)
        return False


def issue_web_session(user_id: int) -> str:
    """
    Выпускает подписанный JWT сессии сайта для httpOnly-cookie.

    Payload: {uid, typ:'web', iat, exp}. Ключ — config.WEB_SESSION_SECRET, TTL —
    config.WEB_SESSION_TTL_SECONDS. Отдельный typ='web' исключает использование
    device-JWT (typ иной) как веб-сессии.
    """
    # Fail-closed: без явно заданного секрета веб-сессии не выпускаем (см. config.WEB_SESSION_SECRET_OK).
    if not getattr(config, "WEB_SESSION_SECRET_OK", False):
        raise RuntimeError("WEB_SESSION_SECRET не сконфигурирован — веб-вход отключён")
    now = int(time.time())
    payload = {
        "uid": int(user_id),
        "typ": "web",
        "iat": now,
        "exp": now + config.WEB_SESSION_TTL_SECONDS,
    }
    return jwt.encode(payload, config.WEB_SESSION_SECRET, algorithm=_ALG)


def verify_web_session(token: str) -> dict | None:
    """
    Декодирует и валидирует JWT веб-сессии.

    Returns:
        claims (dict) при валидном токене с typ=='web'; None при любой ошибке/просрочке.
    """
    if not token:
        return None
    # Fail-closed: при дев-дефолтном секрете НЕ принимаем сессии (иначе их можно подделать).
    if not getattr(config, "WEB_SESSION_SECRET_OK", False):
        return None
    try:
        claims = jwt.decode(token, config.WEB_SESSION_SECRET, algorithms=[_ALG])
    except jwt.PyJWTError:
        return None
    except Exception:
        return None
    if claims.get("typ") != "web":
        return None
    return claims


# END OF FILE services/telegram_login.py
