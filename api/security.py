# START OF FILE api/security.py

"""
Модуль безопасности API.

Содержит:
- Проверку и валидацию API-ключей
- Rate limiting на уровне ключа
- Валидацию Origin/Referer для web-ключей
- Блокировку браузерных запросов для app-ключей
- Проверку IP whitelist
"""

import hashlib
import ipaddress
import secrets
import logging
import time
from collections import defaultdict
from typing import Optional, Dict, Tuple
from datetime import datetime

import asyncpg
from fastapi import Request, HTTPException, status

# Настройка логирования
logger = logging.getLogger(__name__)


# === ГЕНЕРАЦИЯ И ХЕШИРОВАНИЕ КЛЮЧЕЙ ===

def generate_api_key() -> Tuple[str, str, str]:
    """
    Генерирует новый API-ключ.

    Returns:
        Tuple[str, str, str]: (полный ключ, хеш ключа, префикс для UI)
    """
    # Генерируем случайные байты
    random_bytes = secrets.token_urlsafe(32)
    # Формируем ключ с префиксом
    full_key = f"sk_live_{random_bytes}"
    # Вычисляем SHA256 хеш
    key_hash = hashlib.sha256(full_key.encode()).hexdigest()
    # Префикс для отображения в UI
    key_prefix = full_key[:12]  # "sk_live_XXXX"

    return full_key, key_hash, key_prefix


def hash_api_key(key: str) -> str:
    """Вычисляет SHA256 хеш API-ключа."""
    return hashlib.sha256(key.encode()).hexdigest()


# === IN-MEMORY RATE LIMITER ===

class RateLimiter:
    """
    Простой in-memory rate limiter для API-ключей.

    Использует sliding window алгоритм для подсчёта запросов.
    """

    def __init__(self):
        # Словарь: key_id -> список timestamp'ов запросов
        self._requests: Dict[int, list] = defaultdict(list)
        # Блокировка для потокобезопасности
        self._cleanup_interval = 60  # Секунд между очистками
        self._last_cleanup = time.time()

    def _cleanup_old_requests(self, key_id: int, window_seconds: int = 60):
        """Удаляет устаревшие запросы из окна."""
        now = time.time()
        cutoff = now - window_seconds
        self._requests[key_id] = [ts for ts in self._requests[key_id] if ts > cutoff]

    def _global_cleanup(self):
        """Периодическая очистка всех старых записей."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return

        cutoff = now - 60
        for key_id in list(self._requests.keys()):
            self._requests[key_id] = [ts for ts in self._requests[key_id] if ts > cutoff]
            if not self._requests[key_id]:
                del self._requests[key_id]

        self._last_cleanup = now

    def check_and_record(self, key_id: int, rpm_limit: int) -> Tuple[bool, int]:
        """
        Проверяет лимит и записывает запрос.

        Args:
            key_id: ID API-ключа
            rpm_limit: Лимит запросов в минуту

        Returns:
            Tuple[bool, int]: (разрешено ли, текущее количество запросов)
        """
        self._global_cleanup()
        self._cleanup_old_requests(key_id)

        current_count = len(self._requests[key_id])

        if current_count >= rpm_limit:
            return False, current_count

        self._requests[key_id].append(time.time())
        return True, current_count + 1

    def get_current_usage(self, key_id: int) -> int:
        """Возвращает текущее количество запросов в окне."""
        self._cleanup_old_requests(key_id)
        return len(self._requests[key_id])


# Глобальный экземпляр rate limiter
rate_limiter = RateLimiter()


# === ПРОВЕРКА API-КЛЮЧА ===

async def verify_api_key(
    key: str,
    db_pool: asyncpg.Pool
) -> Optional[Dict]:
    """
    Проверяет API-ключ в базе данных.

    Args:
        key: API-ключ из заголовка
        db_pool: Пул соединений с БД

    Returns:
        Dict с данными ключа или None если ключ невалидный
    """
    if not key or not key.startswith("sk_live_"):
        return None

    key_hash = hash_api_key(key)

    query = """
        SELECT id, key_hash, key_prefix, name, owner_user_id, key_type,
               allowed_origins, allowed_ips, is_active, requests_per_minute,
               total_requests, last_ip
        FROM api_keys
        WHERE key_hash = $1
    """

    try:
        row = await db_pool.fetchrow(query, key_hash)
        if row:
            return dict(row)
        return None
    except Exception as e:
        logger.error(f"[API_SECURITY] Ошибка проверки ключа: {e}")
        return None


async def update_key_usage(
    key_id: int,
    client_ip: str,
    db_pool: asyncpg.Pool
):
    """Обновляет статистику использования ключа."""
    query = """
        UPDATE api_keys
        SET total_requests = total_requests + 1,
            last_used_at = NOW(),
            last_ip = $2
        WHERE id = $1
    """
    try:
        await db_pool.execute(query, key_id, client_ip)
    except Exception as e:
        logger.error(f"[API_SECURITY] Ошибка обновления статистики ключа: {e}")


# === ВАЛИДАЦИЯ ЗАПРОСОВ ===

def validate_origin(request: Request, api_key: Dict) -> bool:
    """
    Проверяет Origin/Referer для web-ключей.

    Для web-ключей Origin должен совпадать с allowed_origins.
    """
    if api_key['key_type'] != 'web':
        return True  # Для app-ключей не проверяем Origin

    allowed_origins = api_key.get('allowed_origins') or []
    if not allowed_origins:
        return True  # Если домены не заданы, разрешаем всё

    # Получаем Origin из заголовков
    origin = request.headers.get('origin', '')
    referer = request.headers.get('referer', '')

    # Извлекаем домен из referer
    if referer and not origin:
        from urllib.parse import urlparse
        parsed = urlparse(referer)
        origin = f"{parsed.scheme}://{parsed.netloc}"

    if not origin:
        # Нет Origin — это может быть серверный запрос, разрешаем
        # (для полной безопасности можно запретить)
        return True

    # Проверяем, есть ли origin в списке разрешённых
    for allowed in allowed_origins:
        # Поддержка wildcard поддоменов
        if allowed.startswith('*.'):
            domain = allowed[2:]
            if origin.endswith(domain) or origin.endswith(f".{domain}"):
                return True
        elif origin == allowed or origin == allowed.rstrip('/'):
            return True

    logger.warning(f"[API_SECURITY] Origin {origin} не в списке разрешённых для ключа {api_key['key_prefix']}")
    return False


def block_browser_requests(request: Request, api_key: Dict) -> bool:
    """
    Блокирует браузерные запросы для app-ключей.

    App-ключи предназначены для серверных приложений,
    поэтому запросы с браузерными заголовками блокируются.

    Returns:
        True если запрос должен быть заблокирован
    """
    if api_key['key_type'] != 'app':
        return False  # Для web-ключей не блокируем

    # Браузерные заголовки
    browser_indicators = [
        request.headers.get('origin'),
        request.headers.get('referer'),
        request.headers.get('sec-fetch-mode'),
        request.headers.get('sec-fetch-site'),
        request.headers.get('sec-fetch-dest'),
    ]

    # Если есть хотя бы один браузерный заголовок — блокируем
    if any(indicator for indicator in browser_indicators):
        logger.warning(
            f"[API_SECURITY] Браузерный запрос заблокирован для app-ключа {api_key['key_prefix']}"
        )
        return True

    return False


def check_ip_whitelist(request: Request, api_key: Dict) -> bool:
    """
    Проверяет IP-адрес клиента против whitelist.

    Поддерживает:
    - Точные IP-адреса: 192.168.1.1
    - CIDR нотацию: 192.168.1.0/24
    - Wildcard: 192.168.1.* (конвертируется в /24)

    Returns:
        True если IP разрешён, False если заблокирован
    """
    import ipaddress

    allowed_ips = api_key.get('allowed_ips') or []
    if not allowed_ips:
        return True  # Если whitelist пуст, разрешаем всё

    # Получаем IP клиента
    client_ip_str = get_client_ip(request)

    if client_ip_str == "unknown":
        logger.warning(f"[API_SECURITY] Не удалось определить IP клиента для ключа {api_key['key_prefix']}")
        return False

    try:
        client_ip = ipaddress.ip_address(client_ip_str)
    except ValueError:
        logger.warning(f"[API_SECURITY] Некорректный IP клиента: {client_ip_str}")
        return False

    for allowed in allowed_ips:
        try:
            # Обработка wildcard формата (192.168.1.*)
            if '*' in allowed:
                # Конвертируем wildcard в CIDR
                parts = allowed.split('.')
                cidr_parts = []
                mask_bits = 0
                for part in parts:
                    if part == '*':
                        cidr_parts.append('0')
                    else:
                        cidr_parts.append(part)
                        mask_bits += 8
                cidr_notation = '.'.join(cidr_parts) + f'/{mask_bits}'
                network = ipaddress.ip_network(cidr_notation, strict=False)
                if client_ip in network:
                    return True
            elif '/' in allowed:
                # CIDR нотация
                network = ipaddress.ip_network(allowed, strict=False)
                if client_ip in network:
                    return True
            else:
                # Точный IP-адрес
                if client_ip == ipaddress.ip_address(allowed):
                    return True
        except ValueError as e:
            logger.warning(f"[API_SECURITY] Некорректный IP в whitelist: {allowed}, ошибка: {e}")
            continue

    logger.warning(
        f"[API_SECURITY] IP {client_ip_str} не в whitelist для ключа {api_key['key_prefix']}. "
        f"Разрешённые: {allowed_ips}"
    )
    return False


def get_client_ip(request: Request) -> str:
    """Получает реальный IP клиента с учётом прокси."""
    # Заголовкам (CF-Connecting-IP/XFF/X-Real-IP) доверяем ТОЛЬКО когда непосредственный пир —
    # наш прокси (Caddy в docker-сети / localhost). Порт :8000 опубликован наружу (его использует
    # интеграция с IP-whitelist), и прямое подключение к нему даёт клиенту полный контроль над
    # заголовками → обход rate-limit/whitelist и отравление multi_ip-детекта. Для недоверенного
    # пира — всегда IP самого соединения.
    peer = request.client.host if request.client else ""
    try:
        peer_is_proxy = bool(peer) and ipaddress.ip_address(peer).is_private
    except ValueError:
        peer_is_proxy = False
    if not peer_is_proxy:
        return peer or "unknown"

    # Cloudflare: реальный IP посетителя. CF ПЕРЕЗАПИСЫВАЕТ этот заголовок на эдже (входящее от
    # клиента значение игнорируется), а Caddy проксирует его насквозь — за оранжевым облаком ему
    # можно доверять (от не-CF пиров Caddy его срезает, см. caddy/Caddyfile). Без него
    # X-Forwarded-For у бэкенда = IP CF-эджа (Caddy не доверяет чужому XFF и заменяет цепочку
    # адресом соединения) → ротация эджей выглядела как «мульти-IP» юзера.
    cf_ip = request.headers.get('cf-connecting-ip')
    if cf_ip:
        return cf_ip.strip()

    # X-Forwarded-For может содержать цепочку IP
    forwarded = request.headers.get('x-forwarded-for')
    if forwarded:
        # Берём первый IP в цепочке
        return forwarded.split(',')[0].strip()

    # X-Real-IP
    real_ip = request.headers.get('x-real-ip')
    if real_ip:
        return real_ip.strip()

    # Fallback на IP из соединения
    if request.client:
        return request.client.host

    return "unknown"


# === ПОЛНАЯ ПРОВЕРКА БЕЗОПАСНОСТИ ===

async def validate_request(
    request: Request,
    api_key: str,
    db_pool: asyncpg.Pool
) -> Dict:
    """
    Выполняет полную проверку безопасности запроса.

    Raises:
        HTTPException: При любой ошибке валидации

    Returns:
        Dict с данными API-ключа
    """
    # 1. Проверяем наличие ключа
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    # 2. Проверяем ключ в БД
    key_data = await verify_api_key(api_key, db_pool)
    if not key_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key"
        )

    # 3. Проверяем, активен ли ключ
    if not key_data['is_active']:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is disabled"
        )

    # 4. Проверяем Origin для web-ключей
    if not validate_origin(request, key_data):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin not allowed for this API key"
        )

    # 5. Блокируем браузерные запросы для app-ключей
    if block_browser_requests(request, key_data):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Browser requests not allowed for app API keys"
        )

    # 6. Проверяем IP whitelist
    if not check_ip_whitelist(request, key_data):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="IP address not in whitelist"
        )

    # 7. Проверяем rate limit
    allowed, current_count = rate_limiter.check_and_record(
        key_data['id'],
        key_data['requests_per_minute']
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded: {key_data['requests_per_minute']} requests per minute",
            headers={
                "Retry-After": "60",
                "X-RateLimit-Limit": str(key_data['requests_per_minute']),
                "X-RateLimit-Remaining": "0"
            }
        )

    # 8. Обновляем статистику использования
    client_ip = get_client_ip(request)
    await update_key_usage(key_data['id'], client_ip, db_pool)

    return key_data


def check_rate_limit(key_id: int, rpm_limit: int) -> Tuple[bool, int]:
    """
    Проверяет rate limit для ключа (вспомогательная функция).

    Returns:
        Tuple[bool, int]: (разрешено ли, текущее количество)
    """
    return rate_limiter.check_and_record(key_id, rpm_limit)


# END OF FILE api/security.py
