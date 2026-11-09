# START OF FILE rate_limiter.py

"""
Модуль для управления rate limiting запросов к Vertex AI.

Предотвращает ошибки ResourceExhausted путем:
1. Глобального ограничения QPM (Queries Per Minute) для каждой модели
2. Очереди запросов с приоритетами
3. Адаптивного throttling при обнаружении ошибок
4. Мониторинга и логирования использования API
"""

import asyncio
import logging
import os
import random
import time
from collections import deque, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Callable, Any, Dict

from google import genai
from google.genai import errors as genai_errors
from google.api_core import exceptions as google_exceptions
from google.oauth2 import service_account


class RequestPriority(Enum):
    """Приоритеты запросов к API."""
    CRITICAL = 1    # Критические запросы (основные ответы пользователям)
    HIGH = 2        # Высокий приоритет (роутеры для активных диалогов)
    NORMAL = 3      # Обычные запросы (оптимизаторы, некритичные функции)
    LOW = 4         # Низкий приоритет (фоновые задачи)


@dataclass
class RequestInfo:
    """Информация о запросе в очереди."""
    func: Callable
    priority: RequestPriority
    timestamp: float
    model_name: str
    user_id: Optional[int] = None


_RETRYABLE_SERVER_STATUS_CODES = frozenset({500, 502, 503, 504})


class TransientVertexError(TimeoutError):
    """Vertex exhausted its bounded retries for a temporary server-side failure.

    It intentionally subclasses ``TimeoutError``: existing bot and web flows already
    handle that class as a temporary overload and show a retry action instead of
    reporting a critical application crash.  The original SDK exception is kept as
    ``__cause__`` without leaking Google's HTML error page into user/admin messages.
    """

    def __init__(self, model_name: str, status_code: int, attempts: int):
        self.model_name = model_name
        self.status_code = status_code
        self.attempts = attempts
        super().__init__(
            f"Vertex AI временно недоступен (HTTP {status_code}); "
            f"исчерпано попыток: {attempts}"
        )


def _http_status_code(exc: BaseException) -> Optional[int]:
    """Read a numeric HTTP status without parsing a potentially huge error body."""
    code = getattr(exc, "code", None)
    if callable(code):
        try:
            code = code()
        except TypeError:
            return None
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def is_retryable_vertex_server_error(exc: BaseException) -> bool:
    """True only for known-transient Google/GenAI HTTP server errors."""
    return (
        isinstance(exc, (genai_errors.ServerError, google_exceptions.ServerError))
        and _http_status_code(exc) in _RETRYABLE_SERVER_STATUS_CODES
    )


class VertexAIRateLimiter:
    """
    Глобальный rate limiter для Vertex AI API.

    Управляет квотами на основе модели и предотвращает превышение лимитов QPM.
    """

    # Квоты QPM для разных моделей (консервативные значения с запасом).
    # ВАЖНО: порядок ключей значим — _get_model_base_name берёт ПЕРВЫЙ ключ,
    # на который model_name.startswith(...). Поэтому более специфичные имена
    # (…-flash-lite) идут РАНЬШЕ менее специфичных (…-flash).
    MODEL_QPM_LIMITS = {
        # Gemini 3.x — используются ботом (QA/ассистент) и анализатором законки.
        'gemini-3.1-pro': 10,           # pro preview: низкая квота — иначе 429 RESOURCE_EXHAUSTED
        'gemini-3-pro': 10,
        'gemini-3.1-flash-lite': 60,    # специфичнее, должен идти раньше '…-flash'
        'gemini-3.1-flash': 30,
        'gemini-3-flash': 30,
        # Gemini 2.x — легаси.
        'gemini-2.5-pro': 10,
        'gemini-2.5-flash-lite': 80,    # специфичнее, раньше '2.5-flash'
        'gemini-2.5-flash': 50,
        'gemini-2.0-flash': 60,
    }

    # Максимальное время ожидания запроса в очереди
    MAX_QUEUE_WAIT_SECONDS = 90

    # Период "охлаждения" после обнаружения ResourceExhausted
    COOLDOWN_SECONDS = 30

    # 5xx — не квотная ошибка: повторяем быстро, не включая 30-секундный cooldown.
    TRANSIENT_RETRY_BASE_SECONDS = 1.0
    TRANSIENT_RETRY_MAX_SECONDS = 8.0
    TRANSIENT_RETRY_JITTER_SECONDS = 0.5

    def __init__(self):
        """Инициализация rate limiter."""
        # Очереди запросов для каждой модели
        self.queues: dict[str, asyncio.Queue] = {}

        # История запросов за последнюю минуту для каждой модели
        self.request_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=1000))

        # Семафоры конкурентности на модель — сколько вызовов Vertex к одной модели может идти
        # ОДНОВРЕМЕННО. Раньше тут был asyncio.Lock (=1 в полёте) → API-процесс сериализовал все
        # qa/advise в один поток: под конкуренцией (несколько юзеров оверлея/ретраи) запросы
        # стояли в очереди и упирались в дедлайн/потолок Cloudflare. Конкурентность из env
        # VERTEX_MODEL_CONCURRENCY: дефолт 1 = ПРЕЖНЕЕ поведение (бот не трогаем), API-контейнеру
        # ставим больше. Pro-модели всегда 1 (низкая квота + собственный пейсинг 2.5-pro).
        self._model_concurrency = max(1, int(os.getenv('VERTEX_MODEL_CONCURRENCY', '1') or '1'))
        self.semaphores: dict[str, asyncio.Semaphore] = {}

        # Счетчики ошибок ResourceExhausted
        self.error_counts: dict[str, int] = defaultdict(int)
        self.last_error_time: dict[str, float] = {}

        # Флаг активации адаптивного throttling
        self.adaptive_throttling: dict[str, bool] = defaultdict(bool)

        # Статистика использования
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'queue_timeouts': 0,
            'resource_exhausted_errors': 0,
        }

        # Ссылки на фоновые задачи-алерты о критичных сбоях доступа к Vertex
        # (биллинг/SA/403). Держим ссылку, иначе asyncio.create_task может собрать
        # задачу GC до завершения отправки.
        self._alert_tasks: set = set()

        logging.info("✅ VertexAIRateLimiter инициализирован")

    def _get_model_base_name(self, model_name: str) -> str:
        """Извлекает базовое имя модели без версии."""
        for base_name in self.MODEL_QPM_LIMITS.keys():
            if model_name.startswith(base_name):
                return base_name
        # Фолбэк по подстроке (специфичные — раньше).
        if 'pro' in model_name and 'gemini-3' in model_name:
            return 'gemini-3.1-pro'
        if 'flash-lite' in model_name and 'gemini-3' in model_name:
            return 'gemini-3.1-flash-lite'
        if 'flash' in model_name and 'gemini-3' in model_name:
            return 'gemini-3-flash'
        if 'gemini-2.5-pro' in model_name:
            return 'gemini-2.5-pro'
        if 'gemini-2.5-flash' in model_name:
            return 'gemini-2.5-flash'
        if 'gemini-2.0-flash' in model_name:
            return 'gemini-2.0-flash'
        return model_name

    def _get_qpm_limit(self, model_name: str) -> int:
        """Получает QPM лимит для модели."""
        base_name = self._get_model_base_name(model_name)
        return self.MODEL_QPM_LIMITS.get(base_name, 50)  # Default: 50 QPM

    def _get_concurrency(self, base_model: str) -> int:
        """Сколько одновременных вызовов разрешено к модели. Pro — всегда 1 (низкая квота +
        пейсинг 2.5-pro завязан на сериализацию); остальным — VERTEX_MODEL_CONCURRENCY."""
        if 'pro' in base_model:
            return 1
        return self._model_concurrency

    def _cleanup_old_requests(self, model_name: str) -> None:
        """Удаляет запросы старше 60 секунд из истории."""
        current_time = time.time()
        history = self.request_history[model_name]

        while history and (current_time - history[0]) > 60:
            history.popleft()

    def _get_current_qpm(self, model_name: str) -> int:
        """Возвращает текущее количество запросов за последнюю минуту."""
        self._cleanup_old_requests(model_name)
        return len(self.request_history[model_name])

    def _can_make_request(self, model_name: str) -> bool:
        """Проверяет, можно ли сделать запрос без превышения лимита."""
        current_qpm = self._get_current_qpm(model_name)
        qpm_limit = self._get_qpm_limit(model_name)

        # Если включен адаптивный throttling, снижаем лимит на 50%
        if self.adaptive_throttling.get(model_name, False):
            qpm_limit = qpm_limit // 2

        return current_qpm < qpm_limit

    def _check_cooldown(self, model_name: str) -> bool:
        """Проверяет, находится ли модель в режиме охлаждения после ошибок."""
        last_error = self.last_error_time.get(model_name, 0)
        return (time.time() - last_error) < self.COOLDOWN_SECONDS

    async def _wait_for_capacity(self, model_name: str, max_wait: float = 30.0) -> bool:
        """
        Ожидает освобождения квоты для запроса.

        Returns:
            True если квота освободилась, False если превышен таймаут
        """
        start_time = time.time()

        while time.time() - start_time < max_wait:
            # Проверяем cooldown
            if self._check_cooldown(model_name):
                cooldown_remaining = self.COOLDOWN_SECONDS - (time.time() - self.last_error_time[model_name])
                logging.warning(
                    f"⏸ [RATE_LIMITER] Модель {model_name} в режиме охлаждения. "
                    f"Осталось: {cooldown_remaining:.1f} сек."
                )
                await asyncio.sleep(min(cooldown_remaining + 1, max_wait - (time.time() - start_time)))
                continue

            # Проверяем квоту
            if self._can_make_request(model_name):
                return True

            # Вычисляем время ожидания
            current_qpm = self._get_current_qpm(model_name)
            qpm_limit = self._get_qpm_limit(model_name)

            if self.adaptive_throttling.get(model_name, False):
                qpm_limit = qpm_limit // 2

            # Время до истечения самого старого запроса
            if self.request_history[model_name]:
                oldest_request_time = self.request_history[model_name][0]
                wait_time = max(0.1, 61 - (time.time() - oldest_request_time))
            else:
                wait_time = 1.0

            wait_time = min(wait_time, max_wait - (time.time() - start_time))

            if wait_time > 0:
                logging.debug(
                    f"⏳ [RATE_LIMITER] Модель {model_name}: {current_qpm}/{qpm_limit} QPM. "
                    f"Ожидание {wait_time:.1f} сек."
                )
                await asyncio.sleep(wait_time)

        return False

    async def execute_with_rate_limit(
        self,
        func: Callable,
        model_name: str,
        priority: RequestPriority = RequestPriority.NORMAL,
        user_id: Optional[int] = None,
        max_retries: int = 3,
    ) -> Any:
        """
        Выполняет функцию с учетом rate limiting.

        Args:
            func: Async функция для выполнения
            model_name: Имя модели Vertex AI
            priority: Приоритет запроса
            user_id: ID пользователя (для логирования)
            max_retries: Общее количество попыток при 429 и временных HTTP 5xx

        Returns:
            Результат выполнения функции

        Raises:
            Exception: Если все попытки исчерпаны
        """

        base_model = self._get_model_base_name(model_name)

        # Создаём семафор конкурентности для модели, если его нет.
        if base_model not in self.semaphores:
            self.semaphores[base_model] = asyncio.Semaphore(self._get_concurrency(base_model))

        self.stats['total_requests'] += 1

        attempts = max(1, int(max_retries))
        for attempt in range(attempts):
            try:
                # Ждем освобождения квоты
                capacity_available = await self._wait_for_capacity(
                    base_model,
                    max_wait=self.MAX_QUEUE_WAIT_SECONDS
                )

                if not capacity_available:
                    self.stats['queue_timeouts'] += 1
                    logging.error(
                        f"❌ [RATE_LIMITER] Таймаут ожидания квоты для {base_model}. "
                        f"User: {user_id}, Priority: {priority.name}"
                    )
                    raise TimeoutError(f"Превышено время ожидания квоты для модели {base_model}")

                # Захватываем слот семафора (до N одновременных) и выполняем запрос
                async with self.semaphores[base_model]:
                    # Добавляем минимальную задержку между запросами для gemini-2.5-pro (низкий лимит)
                    if 'gemini-2.5-pro' in base_model:
                        # Минимум 6 секунд между запросами (10 запросов в минуту)
                        if self.request_history[base_model]:
                            last_request_time = self.request_history[base_model][-1]
                            min_delay = 6.0
                            elapsed = time.time() - last_request_time
                            if elapsed < min_delay:
                                wait_time = min_delay - elapsed
                                logging.info(f"⏳ [RATE_LIMITER] Принудительная задержка {wait_time:.1f}с для {base_model}")
                                await asyncio.sleep(wait_time)

                    # Регистрируем запрос
                    self.request_history[base_model].append(time.time())

                    # Выполняем функцию
                    result = await func()

                    # Успешный запрос - сбрасываем счетчики ошибок
                    self.error_counts[base_model] = 0
                    if self.adaptive_throttling.get(base_model, False):
                        logging.info(f"✅ [RATE_LIMITER] Адаптивный throttling для {base_model} ОТКЛЮЧЕН")
                        self.adaptive_throttling[base_model] = False

                    self.stats['successful_requests'] += 1

                    current_qpm = self._get_current_qpm(base_model)
                    qpm_limit = self._get_qpm_limit(base_model)
                    logging.info(
                        f"✅ [RATE_LIMITER] Успешный запрос к {base_model}. "
                        f"QPM: {current_qpm}/{qpm_limit} ({(current_qpm/qpm_limit*100):.0f}%), "
                        f"User: {user_id}, Priority: {priority.name}"
                    )

                    return result

            except (google_exceptions.ResourceExhausted, genai_errors.ClientError) as e:
                # Проверяем, что это действительно 429 ошибка
                is_429 = False
                if isinstance(e, google_exceptions.ResourceExhausted):
                    is_429 = True
                elif isinstance(e, genai_errors.ClientError):
                    error_str = str(e)
                    is_429 = '429' in error_str or 'RESOURCE_EXHAUSTED' in error_str

                if not is_429:
                    # Не 429 — возможен КРИТ. сбой доступа (403 BILLING_DISABLED /
                    # PERMISSION_DENIED / CONSUMER_SUSPENDED / API off). Это «AI лёг у
                    # всех», чинится руками → шлём дедуплицированный алерт супер-админу
                    # и пробрасываем исключение дальше.
                    self.stats['failed_requests'] += 1
                    self._fire_outage_alert(base_model, e)
                    raise

                self.stats['resource_exhausted_errors'] += 1
                self.error_counts[base_model] += 1
                self.last_error_time[base_model] = time.time()

                # Включаем адаптивный throttling после 1+ ошибки
                if self.error_counts[base_model] >= 1:
                    self.adaptive_throttling[base_model] = True
                    logging.warning(
                        f"⚠️ [RATE_LIMITER] Адаптивный throttling для {base_model} ВКЛЮЧЕН. "
                        f"Ошибок: {self.error_counts[base_model]}"
                    )

                if attempt == attempts - 1:
                    self.stats['failed_requests'] += 1
                    logging.error(
                        f"❌ [RATE_LIMITER] Все попытки ({attempts}) исчерпаны для {base_model}. "
                        f"User: {user_id}"
                    )
                    raise

                # Exponential backoff с jitter
                base_delay = 2 ** attempt
                jitter = asyncio.get_event_loop().time() % 1  # 0-1 секунда
                delay = base_delay + jitter + self.COOLDOWN_SECONDS

                logging.warning(
                    f"⏳ [RATE_LIMITER] ResourceExhausted для {base_model}. "
                    f"Попытка {attempt + 1}/{attempts}. Ожидание {delay:.1f} сек. User: {user_id}"
                )

                await asyncio.sleep(delay)

            except (genai_errors.ServerError, google_exceptions.ServerError) as e:
                status_code = _http_status_code(e)
                if not is_retryable_vertex_server_error(e):
                    self.stats['failed_requests'] += 1
                    self._fire_outage_alert(base_model, e)
                    logging.error(
                        "❌ [RATE_LIMITER] Неповторяемая серверная ошибка %s для %s (HTTP %s)",
                        type(e).__name__, base_model, status_code,
                    )
                    raise

                if attempt == attempts - 1:
                    self.stats['failed_requests'] += 1
                    logging.error(
                        "❌ [RATE_LIMITER] Временная ошибка Vertex HTTP %s для %s: "
                        "исчерпаны %s попытки. User: %s",
                        status_code, base_model, attempts, user_id,
                    )
                    raise TransientVertexError(
                        base_model, int(status_code), attempts,
                    ) from e

                base_delay = min(
                    self.TRANSIENT_RETRY_MAX_SECONDS,
                    self.TRANSIENT_RETRY_BASE_SECONDS * (2 ** attempt),
                )
                delay = base_delay + random.uniform(0.0, self.TRANSIENT_RETRY_JITTER_SECONDS)
                logging.warning(
                    "⏳ [RATE_LIMITER] Временная ошибка Vertex HTTP %s для %s. "
                    "Попытка %s/%s, повтор через %.1f сек. User: %s",
                    status_code, base_model, attempt + 1, attempts, delay, user_id,
                )
                await asyncio.sleep(delay)

            except Exception as e:
                self.stats['failed_requests'] += 1
                # Часть критичных сбоев доступа приходит не как ClientError, а как
                # google_exceptions.PermissionDenied/Unauthenticated и т.п. — они падают
                # сюда. Тот же дедуплицированный алерт (классификатор сам отсеет
                # некритичное: таймаут очереди, 5xx-транзиенты).
                self._fire_outage_alert(base_model, e)
                logging.error(
                    f"❌ [RATE_LIMITER] Непредвиденная ошибка при запросе к {base_model}: {e}",
                    exc_info=True
                )
                raise

    def _fire_outage_alert(self, model_name: str, exc: Exception) -> None:
        """Фоновый дедуплицированный алерт супер-админу о КРИТ. сбое доступа к Vertex
        (биллинг выключен / мёртвый сервис-аккаунт / 403 / API off — «AI лёг у всех»).
        Fire-and-forget: не добавляем задержку в и без того упавший запрос. Сама
        классификация (что критично, а что нет — напр. 429 игнорим) и дедуп — внутри
        services.ai_health_alert.maybe_alert_ai_outage."""
        try:
            from services.ai_health_alert import maybe_alert_ai_outage
            task = asyncio.create_task(maybe_alert_ai_outage(model_name, exc))
            self._alert_tasks.add(task)
            task.add_done_callback(self._alert_tasks.discard)
        except Exception as e:
            logging.error(f"[RATE_LIMITER] не смог поставить алерт о сбое AI: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Возвращает статистику использования rate limiter."""
        stats: Dict[str, Any] = self.stats.copy()

        # Добавляем текущие QPM для всех моделей
        stats['current_qpm'] = {
            model: self._get_current_qpm(model)
            for model in self.MODEL_QPM_LIMITS.keys()
        }

        # Добавляем лимиты
        stats['qpm_limits'] = self.MODEL_QPM_LIMITS.copy()

        # Статус adaptive throttling
        stats['adaptive_throttling'] = dict(self.adaptive_throttling)

        # Счетчики ошибок
        stats['error_counts'] = dict(self.error_counts)

        return stats

    def reset_stats(self) -> None:
        """Сбрасывает статистику использования."""
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'queue_timeouts': 0,
            'resource_exhausted_errors': 0,
        }
        logging.info("📊 [RATE_LIMITER] Статистика сброшена")


# --- Глобальный клиент Vertex AI (единственный, регион "global") ---
_genai_client: Optional[genai.Client] = None

# Глобальный экземпляр rate limiter
vertex_rate_limiter = VertexAIRateLimiter()


def init_client(project_id: str, credentials_file: str, region: str = "global") -> genai.Client:
    """
    Инициализирует глобальный genai.Client для Vertex AI.

    Должен быть вызван при старте бота.

    Args:
        project_id: ID проекта GCP
        credentials_file: Путь к файлу учётных данных
        region: Регион GCP (по умолчанию "global")

    Returns:
        Инициализированный genai.Client
    """
    global _genai_client

    credentials = service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=['https://www.googleapis.com/auth/cloud-platform']
    )

    _genai_client = genai.Client(
        vertexai=True,
        project=project_id,
        location=region,
        credentials=credentials
    )

    logging.info(f"✅ [RATE_LIMITER] Vertex AI Client инициализирован (регион: {region})")
    return _genai_client


def get_client() -> genai.Client:
    """
    Возвращает глобальный genai.Client.

    Returns:
        genai.Client

    Raises:
        RuntimeError: Если клиент не инициализирован
    """
    if _genai_client is None:
        raise RuntimeError("genai.Client не инициализирован. Вызовите init_client() при старте бота.")
    return _genai_client


# --- Обратная совместимость ---
# Эти функции оставлены чтобы не ломать импорты в других модулях.
# Они просто делегируют к get_client().

def get_current_client() -> Optional[genai.Client]:
    """Возвращает глобальный клиент. Обратная совместимость."""
    return _genai_client


def get_client_for_model(model_name: str) -> Optional[genai.Client]:
    """Возвращает глобальный клиент. Обратная совместимость."""
    return _genai_client


async def mark_current_region_success():
    """No-op. Оставлено для обратной совместимости."""
    pass


async def mark_current_region_error():
    """No-op. Оставлено для обратной совместимости."""
    pass


# --- Удобные обёртки ---

async def execute_vertex_request(
    func: Callable,
    model_name: str,
    priority: RequestPriority = RequestPriority.NORMAL,
    user_id: Optional[int] = None,
    max_retries: int = 3,
) -> Any:
    """
    Удобная обертка для выполнения запросов к Vertex AI с rate limiting.

    Пример использования:
        async def make_request():
            client = get_client()
            return await client.aio.models.generate_content(
                model="gemini-2.5-pro",
                contents="Hello"
            )

        response = await execute_vertex_request(
            make_request,
            model_name="gemini-2.5-pro",
            priority=RequestPriority.HIGH,
            user_id=12345
        )
    """
    return await vertex_rate_limiter.execute_with_rate_limit(
        func=func,
        model_name=model_name,
        priority=priority,
        user_id=user_id,
        max_retries=max_retries,
    )


def get_rate_limiter_stats() -> Dict[str, Any]:
    """Возвращает статистику rate limiter."""
    return vertex_rate_limiter.get_stats()


# END OF FILE rate_limiter.py

