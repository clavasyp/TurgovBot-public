# services/ai_health_alert.py
"""
Алерты о КРИТИЧЕСКИХ сбоях доступа к Vertex AI, из-за которых AI отваливается
СРАЗУ У ВСЕХ и чинится только руками (вмешательство человека).

Зачем отдельно от обычных ошибок:
  * 429 RESOURCE_EXHAUSTED (квота) — штатная перегрузка, ретраится rate_limiter'ом,
    алерт супер-админу НЕ нужен (иначе флуд на каждом пике).
  * А вот 403 BILLING_DISABLED / PERMISSION_DENIED / CONSUMER_SUSPENDED /
    SERVICE_DISABLED / 401 UNAUTHENTICATED — это «AI лёг у всех»: включить биллинг,
    заменить сервис-аккаунт, включить Vertex API. Раньше уже было 2026-06-12
    (BILLING_DISABLED) — AI лежал у всех, а внятного алерта не приходило.

Точка перехвата — rate_limiter.execute_with_rate_limit: через него идут ВСЕ вызовы
Vertex (Q&A, ролевой/судебный помощник, жалобы, генератор вопросов, анализатор дыр,
советы оверлея). Поэтому один хук покрывает весь бот.

Особенности:
  * Дедуп по классу ошибки в окне: биллинг-даун = падает КАЖДЫЙ запрос, без дедупа
    супер-админа зафлудит сотнями одинаковых алертов (та же беда, что чинили в
    license_events). Первый алерт в окне доходит, остальные глушатся.
  * Самодостаточный отправитель через raw httpx — модуль низкоуровневый, PTB-context
    тут нет (зеркало services/license_events._send_telegram).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Dict, Optional

import httpx

import config

logger = logging.getLogger(__name__)

# Дедуп: класс ошибки -> монотонное время последнего отправленного алерта.
_LAST_ALERT: Dict[str, float] = {}

# Классы критичных ошибок доступа. Порядок важен: более конкретные раньше.
# Ключи — подстроки в str(exc).lower(); значение — человекочитаемый ярлык (= ключ дедупа).
# Ярлык класса «модель недоступна»: это НЕ ops-инцидент (биллинг/SA/API), а код-фикс —
# поправить имя модели в config.py и передеплоить. Отдельная ветка сообщения (без «проверь биллинг»).
MODEL_UNAVAILABLE_LABEL = "MODEL_NOT_FOUND (модель недоступна — правит разработчик, НЕ биллинг)"

_CRITICAL_PATTERNS = (
    # ВАЖНО первым: 404 на publisher-модель («Publisher model projects/…/models/X was not found
    # or your project does not have access») — это снятая/переименованная Google preview-модель,
    # а НЕ падение проекта. Раньше ловилось общей иглой "was not found" → мислейбл PROJECT_NOT_FOUND
    # с советом «проверь биллинг», хотя проект жив (инцидент 2026-07-09: gemini-3.1-flash-lite-preview).
    (MODEL_UNAVAILABLE_LABEL, ("publisher model",)),
    ("BILLING_DISABLED (биллинг выключен)",
     ("billing_disabled", "billing account", "billing has been disabled",
      "billing is disabled", "enable billing", "account is not in good standing")),
    ("CONSUMER_SUSPENDED (проект заблокирован)",
     ("consumer_suspended", "consumer has been suspended", "project has been suspended")),
    ("SERVICE_DISABLED / API выключен",
     ("service_disabled", "has not been used in project", "serviceusage.googleapis.com",
      "vertex ai api has not", "it is disabled")),
    ("PERMISSION_DENIED (нет прав у сервис-аккаунта)",
     ("permission_denied", "permission denied", "iam_permission_denied",
      "does not have permission")),
    ("UNAUTHENTICATED / мёртвый сервис-аккаунт",
     ("unauthenticated", "could not refresh", "reauth", "invalid_grant",
      "invalid authentication", "account not found", "service account")),
    # PROJECT_NOT_FOUND: только КОНКРЕТНЫЕ маркеры «проект не найден». Общий "was not found" убран —
    # он ловил model-404 (см. MODEL_NOT_FOUND выше) и слал ложный «AI лёг у всех / проверь биллинг».
    ("PROJECT_NOT_FOUND (проект не найден)",
     ("project_not_found", "failed to find the project")),
)


def _extract_code(exc: Exception) -> Optional[int]:
    """Достаём HTTP-код из исключения google-genai/api_core (атрибут .code или из текста)."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    m = re.search(r"\b(401|403)\b", str(exc))
    return int(m.group(1)) if m else None


def classify_gcp_error(exc: Exception) -> Optional[str]:
    """Возвращает ярлык класса КРИТИЧНОЙ ошибки доступа к Vertex или None.

    None = ошибка не из «AI лёг у всех» (429-квота, 5xx-транзиенты, таймаут очереди и
    т.п.) — на такие алерт не шлём.
    """
    s = str(exc).lower()
    # 429 — это НЕ наш класс (штатная квота). Явно отсекаем, даже если рядом есть
    # слова вроде "resource".
    if "429" in s or "resource_exhausted" in s or "resource exhausted" in s:
        return None
    for label, needles in _CRITICAL_PATTERNS:
        if any(n in s for n in needles):
            return label
    code = _extract_code(exc)
    if code in (401, 403):
        return f"ДОСТУП/АВТОРИЗАЦИЯ ({code})"
    return None


def _alert_allowed(key: str, window_seconds: float) -> bool:
    """Дедуп: не чаще одного алерта данного класса в окне window_seconds."""
    now = time.monotonic()
    last = _LAST_ALERT.get(key)
    if last is not None and (now - last) < window_seconds:
        return False
    _LAST_ALERT[key] = now
    # Лёгкая чистка, чтобы словарь не рос (классов мало, но на всякий).
    if len(_LAST_ALERT) > 64:
        for k in [k for k, ts in _LAST_ALERT.items() if (now - ts) > window_seconds]:
            _LAST_ALERT.pop(k, None)
    return True


def _esc(s) -> str:
    t = str(s if s is not None else "")
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _send_telegram(chat_id: int, html: str, attempts: int = 3) -> bool:
    """Отправка супер-админу через raw httpx с retry/backoff (PTB-context тут нет)."""
    token = getattr(config, "TELEGRAM_BOT_TOKEN", None)
    if not token or not chat_id:
        logger.error("[ai_health_alert] нет TELEGRAM_BOT_TOKEN/chat_id — алерт не отправлен")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": html, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    delay = 0.5
    for i in range(attempts):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=payload, timeout=10.0)
            if r.status_code == 200:
                return True
            logger.warning("[ai_health_alert] alert HTTP %s (try %s)", r.status_code, i + 1)
        except Exception as e:
            logger.warning("[ai_health_alert] alert send упал (try %s): %s", i + 1, e)
        if i + 1 < attempts:
            await asyncio.sleep(delay)
            delay *= 2
    return False


async def maybe_alert_ai_outage(model_name: str, exc: Exception) -> bool:
    """Если exc — критичный сбой доступа к Vertex, шлёт дедуплицированный алерт
    супер-админу. Возвращает True, если алерт реально отправлен.

    Полностью безопасна: любые свои ошибки гасит в лог, наружу не пробрасывает
    (вызывается из except-веток rate_limiter — не должна ничего ломать)."""
    try:
        label = classify_gcp_error(exc)
        if not label:
            return False

        window = float(getattr(config, "AI_OUTAGE_ALERT_DEDUP_SECONDS", 600))
        if not _alert_allowed(label, window):
            logger.warning(
                "[ai_health_alert] КРИТ. сбой доступа к Vertex (%s) — алерт подавлен дедупом (окно %.0fс)",
                label, window,
            )
            return False

        msg = str(exc)
        if len(msg) > 600:
            msg = msg[:600] + "…"

        project = getattr(config, "GCP_PROJECT_ID", "?")
        # Модель снята/переименована — это код-фикс (config.py + деплой), НЕ биллинг: свой заголовок
        # и совет, чтобы не гонять на пустую проверку Cloud Console. Прочее — ops-инцидент как раньше.
        if label == MODEL_UNAVAILABLE_LABEL:
            header = "⚠️ <b>Vertex — МОДЕЛЬ НЕДОСТУПНА (не биллинг)</b>"
            howto = (
                "➡️ Проект/биллинг тут <b>ни при чём</b> — Google снял/переименовал модель "
                "(частое у <code>-preview</code>).\n"
                "• замени имя модели в <b>config.py</b> на рабочее и передеплой;\n"
                "• рабочий lite-алиас: <code>gemini-3.1-flash-lite</code> (без <code>-preview</code>).\n"
            )
        else:
            header = "🚨 <b>GCP / БИЛЛИНГ — AI НЕ РАБОТАЕТ У ВСЕХ</b>"
            howto = (
                "➡️ Это чинится <b>только вручную</b>. Проверь в Cloud Console:\n"
                "• <b>Billing</b> — включён ли биллинг проекта;\n"
                "• <b>сервис-аккаунт</b> (живой/не удалён, есть роль Vertex AI User);\n"
                "• включён ли <b>Vertex AI API</b>.\n"
            )
        text = (
            f"{header}\n\n"
            f"<b>Класс:</b> {_esc(label)}\n"
            f"<b>Модель:</b> <code>{_esc(model_name)}</code>\n"
            f"<b>Проект (config):</b> <code>{_esc(project)}</code>\n"
            f"<b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"<b>Сообщение:</b>\n<code>{_esc(msg)}</code>\n\n"
            f"{howto}"
            f"(дальнейшие такие алерты подавлены ~{int(window // 60)} мин, чтобы не флудить)"
        )
        ok = await _send_telegram(int(getattr(config, "SUPER_ADMIN_ID", 0) or 0), text)
        if ok:
            logger.error(
                "[ai_health_alert] КРИТ. сбой доступа к Vertex (%s) на модели %s — алерт отправлен супер-админу",
                label, model_name,
            )
        return ok
    except Exception as e:  # noqa: BLE001 — алерт не должен ронять вызывающий код
        logger.error("[ai_health_alert] не удалось обработать/отправить алерт о сбое AI: %s", e)
        return False
