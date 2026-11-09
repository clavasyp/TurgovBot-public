# user_handlers/ai_retry.py
"""
Кнопки повтора под сообщениями «AI-сервис перегружен».

Когда основная модель упирается в 429, под сообщением о перегрузе показываем
кнопки:
  • «🔄 Повторить» — повторить ТОТ ЖЕ запрос на ОСНОВНОЙ модели (вдруг лимит
    был временным).
  • «⚡ Отправить более слабой модели» — РАЗОВО отправить тот же запрос на
    запасную лёгкую модель (config.WEAK_FALLBACK_MODEL_NAME). Основная модель
    при этом НЕ меняется — это одноразовый ручной обход.

Кнопка ничего не «заменяет» — она лишь переотправляет уже введённый
вопрос/задание. Каждый поток (Q&A, иски/жалобы) регистрирует, что именно
переотправлять, через register_retry(); сами данные повтора живут в
context.user_data, поэтому переживают только до перезапуска бота — после
рестарта кнопка вежливо сообщит, что запрос устарел.
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_STORE_KEY = "_ai_retry"        # context.user_data[_STORE_KEY] = {token: payload}
_SEQ_KEY = "_ai_retry_seq"      # монотонный счётчик токенов на юзера
_MAX_KEPT = 8                   # держим только последние N payload'ов на юзера

# callback_data: air:<mode>:<token>; mode r = повторить (та же модель), w = слабее
CALLBACK_PATTERN = r"^air:[rw]:"

DEFAULT_RETRY_LABEL = "🔄 Повторить"
DEFAULT_WEAK_LABEL = "⚡ Отправить более слабой модели"


def register_retry(context: ContextTypes.DEFAULT_TYPE, kind: str, params: dict,
                   *, weak_allowed: bool = True) -> str:
    """Сохраняет данные для повтора и возвращает короткий token для callback_data.

    kind   — тип потока ('qa', 'complaint'); по нему handle_ai_retry выбирает,
             какую функцию повтора звать.
    params — что переотправлять (зависит от потока).
    """
    store = context.user_data.setdefault(_STORE_KEY, {})
    seq = int(context.user_data.get(_SEQ_KEY, 0)) + 1
    context.user_data[_SEQ_KEY] = seq
    token = str(seq)
    store[token] = {"kind": kind, "params": params, "weak_allowed": bool(weak_allowed)}
    # держим только последние N, чтобы user_data не разрастался
    if len(store) > _MAX_KEPT:
        for old in sorted(store, key=lambda k: int(k))[:-_MAX_KEPT]:
            store.pop(old, None)
    return token


def retry_keyboard(token: str, *, weak_allowed: bool = True,
                   retry_label: str = DEFAULT_RETRY_LABEL,
                   weak_label: str = DEFAULT_WEAK_LABEL) -> InlineKeyboardMarkup:
    """Клавиатура под сообщением о перегрузе."""
    rows = [[InlineKeyboardButton(retry_label, callback_data=f"air:r:{token}")]]
    if weak_allowed:
        rows.append([InlineKeyboardButton(weak_label, callback_data=f"air:w:{token}")])
    return InlineKeyboardMarkup(rows)


async def handle_ai_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Глобальный обработчик кнопок повтора (air:r / air:w)."""
    query = update.callback_query
    if query is None:
        return

    try:
        _, mode, token = query.data.split(":", 2)
    except (ValueError, AttributeError):
        await query.answer()
        return

    store = context.user_data.get(_STORE_KEY, {})
    payload = store.get(token)
    if not payload:
        await query.answer("Запрос устарел — задайте его заново.", show_alert=True)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    weak = (mode == "w")
    if weak and not payload.get("weak_allowed", True):
        # на всякий случай: слабая кнопка пришла там, где её не должно быть
        weak = False

    await query.answer("Отправляю запасной модели…" if weak else "Повторяю запрос…")

    # Убираем кнопки, чтобы не нажимали повторно по тому же сообщению.
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass

    kind = payload.get("kind")
    params = payload.get("params", {}) or {}
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is None:
        return

    try:
        if kind == "qa":
            # Ленивый импорт — избегаем циклической зависимости с qa_flow.
            from user_handlers.qa_flow import replay_qa_retry
            await replay_qa_retry(context, chat_id, params, weak=weak)
        elif kind == "complaint":
            from user_handlers.complaint_flow import replay_complaint_retry
            await replay_complaint_retry(context, chat_id, params, weak=weak)
        else:
            logger.warning("[AI_RETRY] Неизвестный kind=%s", kind)
            await context.bot.send_message(chat_id=chat_id,
                                           text="Не удалось повторить запрос — попробуйте задать заново.")
    except Exception as e:
        logger.error("[AI_RETRY] Ошибка повтора kind=%s weak=%s: %s", kind, weak, e, exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Не удалось повторить запрос. Попробуйте ещё раз чуть позже."
            )
        except Exception:
            pass
