# START OF FILE api/dependencies.py

"""
FastAPI Dependencies для инъекции зависимостей.

Содержит:
- Получение пула БД
- Проверка авторизации
- Общие зависимости для эндпоинтов
"""

import logging
from typing import Dict, Optional

from fastapi import Depends, Header, Request, HTTPException, status

from .security import validate_request

logger = logging.getLogger(__name__)

# Глобальная ссылка на пул БД (устанавливается при запуске)
_db_pool = None


def set_db_pool(pool):
    """Устанавливает пул БД для использования в dependencies."""
    global _db_pool
    _db_pool = pool
    logger.info("[API] Database pool установлен")


def get_db_pool():
    """Получает пул БД."""
    if _db_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available"
        )
    return _db_pool


async def get_api_key_data(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> Dict:
    """
    Dependency для проверки API-ключа и получения его данных.

    Выполняет полную проверку безопасности:
    - Валидность ключа
    - Активность ключа
    - Origin validation (для web)
    - Browser blocking (для app)
    - IP whitelist
    - Rate limiting

    Returns:
        Dict с данными API-ключа

    Raises:
        HTTPException: При любой ошибке валидации
    """
    db_pool = get_db_pool()
    return await validate_request(request, x_api_key, db_pool)


async def get_optional_api_key(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> Optional[Dict]:
    """
    Опциональная проверка API-ключа (для эндпоинтов без обязательной авторизации).

    Returns:
        Dict с данными ключа или None
    """
    if not x_api_key:
        return None

    try:
        db_pool = get_db_pool()
        return await validate_request(request, x_api_key, db_pool)
    except HTTPException:
        return None


async def get_device_claims(
    authorization: Optional[str] = Header(None)
) -> Dict:
    """
    Dependency для эндпоинтов оверлея: проверка device access-JWT (Authorization: Bearer ...).

    Stateless (горячий путь, без БД): валидность/срок токена. Отзыв устройства проверяется
    при refresh и на accept WebSocket. Возвращает claims (user_id, device_id, tier, ...).
    """
    from services.device_auth import verify_access  # корень в sys.path добавлен в api/main.py

    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    claims = verify_access(token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired device token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return claims


# Урезанные тиры: универсальный ключ laws-билда даёт ТОЛЬКО поиск по законке (/v1/overlay/laws/*
# и список серверов). AI-эндпоинты (qa/advise/checklist/voice) обязаны звать ensure_full_access —
# иначе тир laws получил бы платные функции полного оверлея.
_RESTRICTED_TIERS = {"laws"}


def ensure_full_access(claims: Dict) -> None:
    """Гейт AI-эндпоинтов оверлея от урезанных тиров (laws-билд). 403 при недостатке прав."""
    if (claims.get("tier") or "").lower() in _RESTRICTED_TIERS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="tier_not_allowed",
        )


async def get_current_web_user(request: Request) -> Dict:
    """
    Dependency для веб-эндпоинтов сайта TurgovBot: проверка cookie-сессии.

    1. Достаёт JWT из httpOnly-cookie 'tb_web_session', валидирует подпись/срок/typ.
    2. ЖИВАЯ проверка БД (в отличие от device-claims — там горячий путь без БД): запись
       пользователя должна существовать и не быть забанена/заблокирована.

    Returns:
        {'user_id': int}

    Raises:
        HTTPException 401 — нет/невалидна сессия или нет записи пользователя.
        HTTPException 403 — пользователь забанен/заблокирован.
    """
    from services.telegram_login import verify_web_session

    token = request.cookies.get("tb_web_session", "")
    claims = verify_web_session(token)
    if not claims:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        user_id = int(claims.get("uid"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    from database.manager import db_manager

    row = await db_manager.fetchrow(
        "SELECT COALESCE(is_banned, FALSE) AS is_banned, COALESCE(is_blocked, FALSE) AS is_blocked "
        "FROM users WHERE user_id = $1",
        user_id,
    )
    if not row:
        # Сессия валидна по подписи, но пользователя нет в БД — считаем не авторизованным.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    if row["is_banned"] or row["is_blocked"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return {"user_id": user_id}


# END OF FILE api/dependencies.py
