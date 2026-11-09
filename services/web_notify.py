# START OF FILE services/web_notify.py

"""
Веб-уведомления в Telegram (для сайта): отправка сообщений в каналы заявок/поддержки
через Bot API (httpx, из api-процесса — без python-telegram-bot) + загрузка пруфов в GCS.

Карточка ВИП-заявки постится с кнопками vip_ok_<id>/vip_no_<id>/vip_info_<id> — их
обрабатывает ЖИВОЙ бот (top-level хендлеры accept/reject/request_vip_info), выдавая
users.is_unlimited на одобрении. Так веб-заявка встраивается в существующий флоу ревью.
"""

import asyncio
import html
import logging
import uuid
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

import httpx

import config
from services import vip_applications

logger = logging.getLogger(__name__)

ALLOWED_PROOF_PREFIXES = ("image/", "video/")
MAX_PROOF_FILES = 10
MAX_PROOF_BYTES = 25 * 1024 * 1024  # 25 МБ на файл
_SIGNED_URL_DAYS = 7

_EXT_BY_TYPE = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
}


# --- Bot API ---

async def tg_send_message(chat_id: int, text: str, reply_markup: Optional[Dict] = None) -> Optional[Dict]:
    """Отправляет сообщение через Bot API. Возвращает result или None при ошибке."""
    token = config.TELEGRAM_BOT_TOKEN
    if not token or not chat_id:
        logger.warning("[web_notify] нет токена/chat_id — сообщение не отправлено")
        return None
    payload = {
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
        data = r.json()
        if not data.get("ok"):
            logger.warning("[web_notify] sendMessage не ок: %s", data)
            return None
        return data.get("result")
    except Exception as e:
        logger.warning("[web_notify] ошибка отправки в %s: %s", chat_id, e)
        return None


# --- GCS-загрузка пруфов ---

def _ext_for(filename: str, content_type: str) -> str:
    if content_type in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[content_type]
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[1][:8]
    return ""


async def upload_proof(user_id: int, filename: str, content: bytes, content_type: str) -> str:
    """Загружает пруф в GCS (vip_proofs/<uid>/<uuid>.<ext>) и возвращает подписанную ссылку (7 дней)."""
    def _do() -> str:
        from google.cloud import storage  # ленивый импорт — тяжёлая зависимость
        client = storage.Client.from_service_account_json(config.VERTEX_CREDENTIALS_FILE)
        bucket = client.bucket(config.active_gcs_bucket())
        blob_name = f"vip_proofs/{user_id}/{uuid.uuid4().hex}{_ext_for(filename, content_type)}"
        blob = bucket.blob(blob_name)
        blob.upload_from_string(content, content_type=content_type or "application/octet-stream")
        return blob.generate_signed_url(version="v4", expiration=timedelta(days=_SIGNED_URL_DAYS), method="GET")
    return await asyncio.to_thread(_do)


# --- Резолв каналов ---

async def resolve_vip_channel() -> Optional[int]:
    """Канал ВИП-заявок: system_cache → env VIP_APPLICATIONS_CHAT_ID → личка супер-админа."""
    cid = await vip_applications.get_channel_chat_id()
    if cid:
        return cid
    if config.VIP_APPLICATIONS_CHAT_ID:
        return config.VIP_APPLICATIONS_CHAT_ID
    return config.SUPER_ADMIN_ID or None


def resolve_support_chat() -> Optional[int]:
    """Чат поддержки: env WEB_SUPPORT_CHAT_ID → личка супер-админа."""
    if config.WEB_SUPPORT_CHAT_ID:
        return config.WEB_SUPPORT_CHAT_ID
    return config.SUPER_ADMIN_ID or None


# --- Карточка ВИП-заявки + тикет поддержки ---

async def post_vip_card(
    app_id: int, user_id: int, full_name: str, username: Optional[str],
    answers: Dict[str, str], proof_urls: List[str],
) -> Optional[Tuple[int, int]]:
    """Постит карточку веб-заявки в канал с кнопками одобрения (их ловит живой бот)."""
    chat_id = await resolve_vip_channel()
    if not chat_id:
        return None
    uname = f"@{username}" if username else "—"
    proofs = "\n".join(
        f'• <a href="{html.escape(u)}">пруф {i + 1}</a>' for i, u in enumerate(proof_urls)
    ) or "—"
    text = (
        f"💎 <b>Заявка на ВИП (с сайта)</b> #{app_id}\n\n"
        f"👤 <b>{html.escape(full_name)}</b> {html.escape(uname)} (<code>{user_id}</code>)\n"
        f"🎮 Сервер: {html.escape(str(answers.get('server', '')))}\n"
        f"🪪 Логин: {html.escape(str(answers.get('login', '')))}\n\n"
        f"📝 {html.escape(str(answers.get('description', '')))}\n\n"
        f"📎 Пруфы (ссылки действительны {_SIGNED_URL_DAYS} дней):\n{proofs}"
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"vip_ok_{app_id}"},
                {"text": "❌ Отклонить", "callback_data": f"vip_no_{app_id}"},
            ],
            [{"text": "📝 На доработку", "callback_data": f"vip_info_{app_id}"}],
        ]
    }
    result = await tg_send_message(chat_id, text, reply_markup=reply_markup)
    if not result:
        return None
    try:
        return int(result["chat"]["id"]), int(result["message_id"])
    except Exception:
        return None


async def post_support_ticket(
    user_id: int, full_name: str, username: Optional[str], topic: str, message: str,
    ticket_id: Optional[int] = None,
) -> Optional[Tuple[int, int]]:
    """Постит тикет поддержки с сайта в чат поддержки. Возвращает (chat_id, message_id) или None."""
    chat_id = resolve_support_chat()
    if not chat_id:
        return None
    uname = f"@{username}" if username else "—"
    head = f"🆘 <b>Обращение в поддержку (сайт)</b>" + (f" #{ticket_id}" if ticket_id else "")
    text = (
        f"{head}\n\n"
        f"👤 <b>{html.escape(full_name)}</b> {html.escape(uname)} (<code>{user_id}</code>)\n"
        f"🏷 Тема: {html.escape(topic)}\n\n"
        f"{html.escape(message)}"
    )
    result = await tg_send_message(chat_id, text)
    if not result:
        return None
    try:
        return int(result["chat"]["id"]), int(result["message_id"])
    except Exception:
        return None


# END OF FILE services/web_notify.py
