# START OF FILE api/middleware.py

"""
Middleware для FastAPI.

Содержит:
- Динамический CORS (только для web-ключей)
- Security headers
- Request logging
- Защита документации API-ключом
"""

import logging
import time
from typing import Callable, Optional

from fastapi import Request, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .security import hash_api_key, get_client_ip

logger = logging.getLogger(__name__)

# Защищённые пути документации
PROTECTED_DOCS_PATHS = {'/verblud', '/verblud/redoc', '/verblud/openapi.json'}
# Имя cookie для хранения временного токена доступа к документации
DOCS_AUTH_COOKIE = "docs_auth_token"


class DocsProtectionMiddleware(BaseHTTPMiddleware):
    """
    Middleware для защиты документации API.

    Требует валидный API-ключ для доступа к /verblud, /verblud/redoc, /verblud/openapi.json.
    После успешной авторизации устанавливает cookie для последующих запросов (openapi.json).
    """

    def __init__(self, app, db_pool_getter: Callable):
        super().__init__(app)
        self.db_pool_getter = db_pool_getter

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Проверяем, является ли путь защищённой документацией
        if path not in PROTECTED_DOCS_PATHS:
            return await call_next(request)

        # Получаем API-ключ из: заголовка, query параметра или cookie
        api_key = (
            request.headers.get("x-api-key", "") or
            request.query_params.get("api_key", "") or
            request.cookies.get(DOCS_AUTH_COOKIE, "")
        )

        if not api_key:
            logger.warning(f"[API/Docs] Попытка доступа к {path} без API-ключа от {get_client_ip(request)}")
            return JSONResponse(
                status_code=401,
                content={
                    "error": "API key required",
                    "detail": "Для доступа к документации требуется API-ключ. "
                              "Укажите его в заголовке X-API-Key или параметре ?api_key=...",
                    "code": "DOCS_AUTH_REQUIRED"
                }
            )

        # Проверяем валидность ключа
        db_pool = self.db_pool_getter()
        if not db_pool:
            return JSONResponse(
                status_code=503,
                content={"error": "Database not available", "code": "DB_UNAVAILABLE"}
            )

        try:
            key_hash = hash_api_key(api_key)
            row = await db_pool.fetchrow(
                "SELECT id, is_active, name FROM api_keys WHERE key_hash = $1",
                key_hash
            )

            if not row:
                logger.warning(f"[API/Docs] Невалидный API-ключ для {path} от {get_client_ip(request)}")
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "Invalid API key",
                        "detail": "Указанный API-ключ не найден или недействителен.",
                        "code": "INVALID_API_KEY"
                    }
                )

            if not row['is_active']:
                logger.warning(f"[API/Docs] Деактивированный ключ '{row['name']}' для {path}")
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "API key disabled",
                        "detail": "Данный API-ключ деактивирован.",
                        "code": "API_KEY_DISABLED"
                    }
                )

            logger.info(f"[API/Docs] Доступ к {path} разрешён для ключа '{row['name']}'")

            # Выполняем запрос
            response = await call_next(request)

            # Если это главная страница документации и ключ был в query — устанавливаем cookie
            # чтобы последующие запросы (openapi.json) тоже были авторизованы
            if path in {'/verblud', '/verblud/redoc'} and request.query_params.get("api_key"):
                response.set_cookie(
                    key=DOCS_AUTH_COOKIE,
                    value=api_key,
                    max_age=3600,  # 1 час
                    httponly=True,
                    samesite="strict"
                )

            return response

        except Exception as e:
            logger.error(f"[API/Docs] Ошибка проверки ключа: {e}")
            return JSONResponse(
                status_code=500,
                content={"error": "Internal server error", "code": "INTERNAL_ERROR"}
            )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware для добавления security headers ко всем ответам.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"

        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware для логирования запросов.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()

        # Получаем информацию о запросе
        client_ip = get_client_ip(request)
        method = request.method
        path = request.url.path

        # Получаем префикс ключа для логирования (без раскрытия полного ключа)
        api_key = request.headers.get("x-api-key", "")
        key_prefix = api_key[:12] + "..." if api_key and len(api_key) > 12 else "no-key"

        try:
            response = await call_next(request)

            # Вычисляем время обработки
            process_time = (time.time() - start_time) * 1000

            # Логируем запрос
            logger.info(
                f"[API] {method} {path} | "
                f"Key: {key_prefix} | "
                f"IP: {client_ip} | "
                f"Status: {response.status_code} | "
                f"Time: {process_time:.2f}ms"
            )

            # Добавляем заголовок с временем обработки
            response.headers["X-Process-Time"] = f"{process_time:.2f}ms"

            return response

        except Exception as e:
            process_time = (time.time() - start_time) * 1000
            logger.error(
                f"[API] {method} {path} | "
                f"Key: {key_prefix} | "
                f"IP: {client_ip} | "
                f"Error: {str(e)} | "
                f"Time: {process_time:.2f}ms"
            )
            raise


class DynamicCORSMiddleware(BaseHTTPMiddleware):
    """
    Динамический CORS middleware.

    Проверяет allowed_origins из API-ключа и добавляет
    соответствующие CORS заголовки только для web-ключей.
    """

    def __init__(self, app, db_pool_getter: Callable):
        super().__init__(app)
        self.db_pool_getter = db_pool_getter

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        origin = request.headers.get("origin")

        # Если нет Origin, просто пропускаем
        if not origin:
            return await call_next(request)

        # Обрабатываем preflight OPTIONS запрос
        if request.method == "OPTIONS":
            return await self._handle_preflight(request, origin)

        # Получаем API-ключ
        api_key = request.headers.get("x-api-key", "")
        if not api_key:
            return await call_next(request)

        # Проверяем ключ в БД
        db_pool = self.db_pool_getter()
        if not db_pool:
            return await call_next(request)

        try:
            key_hash = hash_api_key(api_key)
            row = await db_pool.fetchrow(
                "SELECT key_type, allowed_origins FROM api_keys WHERE key_hash = $1 AND is_active = true",
                key_hash
            )

            if not row or row['key_type'] != 'web':
                # App-ключ или невалидный — без CORS
                return await call_next(request)

            allowed_origins = row.get('allowed_origins') or []

            # Проверяем, разрешён ли origin
            origin_allowed = False
            for allowed in allowed_origins:
                if allowed == '*':
                    origin_allowed = True
                    break
                if allowed.startswith('*.'):
                    domain = allowed[2:]
                    if origin.endswith(domain) or origin.endswith(f".{domain}"):
                        origin_allowed = True
                        break
                elif origin == allowed or origin == allowed.rstrip('/'):
                    origin_allowed = True
                    break

            response = await call_next(request)

            if origin_allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Access-Control-Allow-Credentials"] = "true"
                response.headers["Access-Control-Expose-Headers"] = "X-Process-Time, X-RateLimit-Limit, X-RateLimit-Remaining"

            return response

        except Exception as e:
            logger.error(f"[CORS] Ошибка проверки CORS: {e}")
            return await call_next(request)

    async def _handle_preflight(self, request: Request, origin: str) -> Response:
        """Обрабатывает preflight OPTIONS запрос."""
        api_key = request.headers.get("x-api-key", "")

        # Для preflight без ключа — разрешаем (ключ будет в основном запросе)
        # Это стандартное поведение CORS

        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-API-Key, Authorization",
            "Access-Control-Allow-Credentials": "true",
            "Access-Control-Max-Age": "600",  # Кэшируем preflight на 10 минут
        }

        return Response(status_code=200, headers=headers)


class AuditLoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware для логирования запросов в базу данных.

    Логирует все API запросы для аудита безопасности и аналитики.
    """

    def __init__(self, app, db_pool_getter: Callable):
        super().__init__(app)
        self.db_pool_getter = db_pool_getter

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()

        # Собираем информацию о запросе
        client_ip = get_client_ip(request)
        method = request.method
        path = request.url.path
        user_agent = request.headers.get("user-agent", "")

        # Получаем API-ключ для определения key_id
        api_key = request.headers.get("x-api-key", "")
        api_key_id = None

        if api_key:
            db_pool = self.db_pool_getter()
            if db_pool:
                try:
                    key_hash = hash_api_key(api_key)
                    row = await db_pool.fetchrow(
                        "SELECT id FROM api_keys WHERE key_hash = $1",
                        key_hash
                    )
                    if row:
                        api_key_id = row['id']
                except Exception:
                    pass  # Don't fail request if key lookup fails

        # Выполняем запрос
        response = None
        status_code = 500
        error_message = None

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            status_code = 500
            error_message = str(e)
            raise
        finally:
            # Вычисляем время обработки
            response_time_ms = (time.time() - start_time) * 1000

            # Логируем в БД асинхронно (не блокируем ответ)
            if api_key_id:  # Логируем только авторизованные запросы
                db_pool = self.db_pool_getter()
                if db_pool:
                    try:
                        # Импортируем здесь чтобы избежать циклических импортов
                        from .audit_log import log_api_request

                        # Логируем асинхронно без ожидания
                        import asyncio
                        asyncio.create_task(log_api_request(
                            db_pool=db_pool,
                            api_key_id=api_key_id,
                            endpoint=path,
                            method=method,
                            client_ip=client_ip,
                            status_code=status_code,
                            response_time_ms=response_time_ms,
                            user_agent=user_agent,
                            error_message=error_message
                        ))
                    except Exception as log_error:
                        logger.debug(f"[AUDIT] Failed to queue log: {log_error}")

        return response


def setup_cors(app, db_pool_getter: Callable):
    """
    Настраивает CORS для приложения.

    Args:
        app: FastAPI приложение
        db_pool_getter: Функция для получения пула БД
    """
    # Добавляем динамический CORS middleware
    app.add_middleware(DynamicCORSMiddleware, db_pool_getter=db_pool_getter)


def setup_audit_logging(app, db_pool_getter: Callable):
    """
    Настраивает аудит-логирование для приложения.

    Args:
        app: FastAPI приложение
        db_pool_getter: Функция для получения пула БД
    """
    app.add_middleware(AuditLoggingMiddleware, db_pool_getter=db_pool_getter)


# END OF FILE api/middleware.py
