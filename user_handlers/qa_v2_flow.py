# START OF FILE user_handlers/qa_v2_flow.py

"""
/qa2 — ЭКСПЕРИМЕНТ Q&A v2. Только админы, только сервер config.QA_V2_SERVER_ID.

Использование (в личке с ботом):
    /qa2 <вопрос>            — детальный ответ
    /qa2 кратко <вопрос>     — краткий формат

Пайплайн: services/qa_v2_service.ask_question_v2 (подборка статей + thinking +
search_laws). Основной пользовательский Q&A НЕ затронут. После ответа отдельным
сообщением приходит диагностический футер (модель/статьи/дозапросы/время) — для
сравнения с текущей реализацией.
"""

import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

import config
from database.manager import db_manager
from utils import sanitize_telegram_html
from services.qa_v2_service import ask_question_v2, QaV2EmptyContext
from .qa_flow import _split_long_message

logger = logging.getLogger(__name__)


async def _is_admin(user_id: int) -> bool:
    rec = await db_manager.fetchrow(
        "SELECT is_admin FROM users WHERE user_id = $1", user_id
    )
    return bool((rec and rec.get('is_admin')) or user_id == config.SUPER_ADMIN_ID)


def _format_diag_footer(diag: dict) -> str:
    """Диагностика прогона отдельным сообщением (HTML). Запросы модели экранируются."""
    lines = [
        f"🧪 <b>Q&A v2</b> | {html.escape(diag.get('model', '?'))} | "
        f"thinking={html.escape(str(diag.get('thinking', '?')))}",
        f"📚 статей в подборке: {diag.get('initial_count', 0)}"
        f" (+{diag.get('total_added', 0)} дозапросами)"
        f" | отброшено потолком: {diag.get('initial_dropped', 0)}"
        f" | ноги: каталог={diag.get('legs', {}).get('catalog', '?')},"
        f" вектор={diag.get('legs', {}).get('vector', '?')}",
    ]
    rounds = diag.get('tool_rounds') or []
    if rounds:
        qs = '; '.join(
            f"«{html.escape(r.get('query') or '∅')}» (+{r.get('added', 0)})" for r in rounds
        )
        lines.append(f"🔎 дозапросы search_laws ({len(rounds)}): {qs}")
    else:
        lines.append("🔎 дозапросов search_laws не было")
    lines.append(
        f"⏱ {diag.get('elapsed_sec', 0.0):.1f}с"
        f" | вызовов модели: {diag.get('model_calls', 0)}"
        f" | стартовый контекст ~{diag.get('initial_chars', 0)} симв."
    )
    return '<i>' + '\n'.join(lines) + '</i>'


async def qa2_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа /qa2. Гейт на админа — молча игнорируем посторонних (команда не
    анонсируется), чтобы не подсвечивать скрытую функциональность."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    if not await _is_admin(user.id):
        return

    args = context.args or []
    q_type = 'detailed'
    if args and args[0].lower() in ('кратко', 'brief'):
        q_type = 'brief'
        args = args[1:]
    question = ' '.join(args).strip()

    server_id = config.QA_V2_SERVER_ID
    server_name = config.SERVERS_CONFIG.get(server_id, {}).get('display_name', server_id)

    if not question:
        await message.reply_text(
            "🧪 <b>Q&A v2 (эксперимент)</b>\n"
            f"Сервер: <b>{html.escape(server_name)}</b> | модель: {html.escape(config.QA_V2_MODEL_NAME)}\n\n"
            "Использование:\n"
            "<code>/qa2 вопрос</code> — детальный ответ\n"
            "<code>/qa2 кратко вопрос</code> — краткий ответ",
            parse_mode='HTML',
        )
        return

    status_msg = await message.reply_text(
        f"🧪 v2 [{server_name}]: собираю подборку статей и думаю…"
    )
    try:
        result = await ask_question_v2(server_id, question, q_type, user_id=user.id)
    except QaV2EmptyContext:
        await status_msg.edit_text(
            f"🧪 v2: для сервера «{server_name}» не построен ни каталог статей, ни "
            "векторный индекс — подборка пуста, эксперимент невозможен."
        )
        return
    except Exception as e:
        logger.error("[QA_V2] сбой /qa2 для %s", user.id, exc_info=e)
        await status_msg.edit_text(f"🧪 v2: ошибка прогона — {str(e)[:300]}")
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    cleaned = sanitize_telegram_html(result['answer'])
    for part in _split_long_message(cleaned, limit=4096):
        if part.strip():
            await message.reply_text(part, parse_mode='HTML')
    await message.reply_text(
        _format_diag_footer(result['diag']),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )

# END OF FILE user_handlers/qa_v2_flow.py
