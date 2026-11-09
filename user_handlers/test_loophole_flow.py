# START OF FILE user_handlers/test_loophole_flow.py

"""
Flow анализатора дыр, абузов и противоречий в законке.

Доступ к анализатору: подписчики и выше (is_subscriber / is_vip / админ / тестер) ТОЛЬКО
через кнопку главного меню (callback start_loophole). Команда /test больше НЕ открывает
анализатор — это отдельный ТЕСТЕРСКИЙ раздел (скачать оверлей, статус роли и т.п.),
см. test_command/_build_tester_view (возвращает ConversationHandler.END).
Запросы анализатора выполняются не напрямую, а через очередь (services.law_loophole.queue):
основная очередь обрабатывает один анализ за раз с приоритетом VIP/админов.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
from database.manager import db_manager
from services.device_auth import compute_access_tier
from subscription_manager import check_subscription
from utils import (
    get_enabled_projects,
    get_enabled_servers,
    safe_answer_callback_query,
)

import asyncpg

from services.law_loophole import (
    AnalysisType,
    LawContextDocument,
    SelectionMode,
    ModelProfile,
    choose_model_profile,
    list_laws_for_selection,
)
from services.law_loophole.queue import (
    enqueue_job,
    get_active_job_for_user,
    get_queue_position,
    set_position_msg_id,
    cancel_job,
    get_job,
    is_queue_paused,
    is_loophole_enabled,
    send_loophole_report,
)
from services.law_loophole.analysis_cache import (
    is_caching_enabled,
    compute_fingerprint,
    get_cached_result,
)

logger = logging.getLogger(__name__)

# --- Conversation states ---
(
    TEST_PROJECT,
    TEST_SERVER,
    TEST_TYPE,
    TEST_SOURCE,
    TEST_PROFILE,
    TEST_MANUAL,
    TEST_TEXT_INPUT,  # выделенное состояние для ввода текста запроса (auto_rag/hybrid)
    TEST_CONFIRM,
) = range(8)


# --- Callback prefixes ---
CB_PROJECT = "test_loop_project_"
CB_SERVER = "test_loop_server_"
CB_TYPE = "test_loop_type_"
CB_SOURCE = "test_loop_source_"
CB_PROFILE = "test_loop_profile_"
CB_DOC_TOGGLE = "test_loop_doc_"
CB_DOC_DONE = "test_loop_doc_done"
CB_CONFIRM_RUN = "test_loop_run"
CB_CANCEL = "test_loop_cancel"

# Callback кнопки пользователя для задач в очереди (приходят ВНЕ conversation).
CB_MY_POSITION = "loophole_mypos_"
CB_MY_CANCEL = "loophole_mycancel_"

# Callback входа из главного меню (кнопка «Анализатор законов»).
CB_START_LOOPHOLE = "start_loophole"

# Callback встроенного гайда «Как это работает».
CB_GUIDE = "test_loop_guide"
CB_GUIDE_BACK = "test_loop_guide_back"


# --- Лейблы для UI ---
ANALYSIS_TYPE_LABELS = {
    "quick_scan": "⚡ Быстрый скан",
    "deep_abuse": "🕳 Глубокий абуз-анализ",
    "case_analysis": "📋 Анализ кейса",
    "new_law_review": "📝 Проверка нового закона",
    "full_audit": "🔍 Полный аудит",
    "devil_advocate": "😈 Адвокат дьявола",
}

SELECTION_MODE_LABELS = {
    "auto_rag": "🤖 Auto RAG",
    "full_server": "📚 Все законы сервера",
    "manual_select": "✋ Выбрать вручную",
    "hybrid": "🧪 Hybrid (ручной + RAG)",
}

PROFILE_LABELS = {
    "fast": "⚡ Быстро",
    "balanced": "⚖ Стандартно",
    "deep": "🧠 Глубоко",
    "expert": "🎓 Экспертный аудит",
}

USER_DATA_KEY = "_test_loophole"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _is_tester_or_admin(user_id: int) -> bool:
    rec = await db_manager.fetchrow(
        "SELECT is_admin, is_tester FROM users WHERE user_id = $1", user_id
    )
    is_admin = (rec and rec.get("is_admin")) or user_id == config.SUPER_ADMIN_ID
    is_tester = bool(rec and rec.get("is_tester"))
    return bool(is_admin or is_tester)


async def _loophole_access_allowed(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Доступ к анализатору: VIP / админ / тестер / супер-админ — без проверки канала;
    остальным — ЖИВАЯ проверка подписки на канал (флаг is_subscriber в БД может быть
    устаревшим, поэтому переспрашиваем Telegram и синхронизируем кэш)."""
    if user_id == config.SUPER_ADMIN_ID:
        return True
    rec = await db_manager.fetchrow(
        "SELECT is_admin, is_vip, is_tester FROM users WHERE user_id = $1", user_id
    )
    if rec and (rec.get("is_admin") or rec.get("is_vip") or rec.get("is_tester")):
        return True

    # Обычный пользователь — проверяем подписку на канал в реальном времени.
    is_subbed = await check_subscription(user_id, context)
    try:
        await db_manager.execute(
            "UPDATE users SET is_subscriber = $1 WHERE user_id = $2", is_subbed, user_id
        )
    except Exception:
        logger.warning("[LOOPHOLE] failed to sync is_subscriber for %s", user_id, exc_info=True)
    return bool(is_subbed)


async def _compute_priority(user_id: int) -> int:
    """Приоритет задачи в очереди: админ > VIP > обычный подписчик."""
    rec = await db_manager.fetchrow(
        "SELECT is_admin, is_vip FROM users WHERE user_id = $1", user_id
    )
    if (rec and rec.get("is_admin")) or user_id == config.SUPER_ADMIN_ID:
        return config.LOOPHOLE_PRIORITY_ADMIN
    if rec and rec.get("is_vip"):
        return config.LOOPHOLE_PRIORITY_VIP
    return config.LOOPHOLE_PRIORITY_SUBSCRIBER


def _my_position_kb(job_id: int) -> InlineKeyboardMarkup:
    """Кнопки управления своей задачей в очереди."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Моя позиция", callback_data=f"{CB_MY_POSITION}{job_id}")],
        [InlineKeyboardButton("✖ Отменить запрос", callback_data=f"{CB_MY_CANCEL}{job_id}")],
    ])


def _enqueued_text(pos: int) -> str:
    return (
        "🕳 <b>Запрос принят и поставлен в очередь.</b>\n\n"
        f"Ваша позиция в очереди: <b>{pos}</b>.\n"
        "Когда подойдёт очередь — анализ запустится автоматически, "
        "отчёт придёт в этот чат. Можно продолжать пользоваться ботом."
    )


def _state(context: ContextTypes.DEFAULT_TYPE) -> dict:
    if USER_DATA_KEY not in context.user_data:
        context.user_data[USER_DATA_KEY] = {}
    return context.user_data[USER_DATA_KEY]


def _clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(USER_DATA_KEY, None)


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)]])


# ---------------------------------------------------------------------------
# Команда /test — ТЕСТЕРСКИЙ раздел (скачать оверлей и пр.). Анализатор дыр сюда
# больше НЕ привязан (он в проде — через кнопку меню start_loophole).
# ---------------------------------------------------------------------------


async def _fetch_overlay_manifest() -> Optional[Dict[str, Any]]:
    """Live-манифест апдейтов оверлея (актуальная версия/ссылка). None при ошибке/таймауте."""
    url = f"{config.OVERLAY_PUBLIC_URL.rstrip('/')}/updates/manifest.json"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.json()
    except Exception:
        logger.warning("[TEST] overlay manifest fetch failed", exc_info=True)
        return None


async def _build_tester_view(user_id: int):
    """Текст + клавиатура тестерского раздела /test."""
    rec = await db_manager.fetchrow(
        "SELECT is_admin, is_tester, is_vip, is_subscriber FROM users WHERE user_id = $1",
        user_id,
    )
    roles = []
    if (rec and rec.get("is_admin")) or user_id == config.SUPER_ADMIN_ID:
        roles.append("админ")
    if rec and rec.get("is_tester"):
        roles.append("тестер")
    if rec and rec.get("is_vip"):
        roles.append("VIP")
    if rec and rec.get("is_subscriber"):
        roles.append("подписчик")
    role_str = ", ".join(roles) or "—"

    manifest = await _fetch_overlay_manifest()
    base = config.OVERLAY_PUBLIC_URL.rstrip("/")
    if manifest and manifest.get("url"):
        ver = manifest.get("version") or config.OVERLAY_LATEST_VERSION
        dl_url = manifest["url"]
        notes = (manifest.get("notes") or "").replace("<", "").replace(">", "")
    else:
        ver = config.OVERLAY_LATEST_VERSION
        dl_url = f"{base}/updates/RpOverlay-Setup-{ver}.exe"
        notes = ""

    lines = [
        "🧪 <b>Тестовый раздел</b>",
        f"Твоя роль: <b>{role_str}</b>",
        "",
        f"🖥 <b>Голосовой оверлей</b> — версия <b>{ver}</b>",
    ]
    if notes:
        lines.append(f"<i>Что нового: {notes}</i>")
    lines += [
        "",
        "<b>Как подключить:</b>",
        "1. Скачай и установи клиент кнопкой ниже (Windows).",
        "2. Возьми ключ активации кнопкой «🔑 Мой ключ» и скопируй его.",
        "3. Запусти оверлей и вставь ключ в окно активации.",
        "Ключ привязывается к этому ПК (1 ключ = 1 компьютер). Дальше клиент обновляется сам.",
        "",
        "🐞 Нашёл баг или есть идея — жми «✍️ Сообщить баг / идею» ниже. "
        "Сообщение уйдёт администрации, ответ придёт сюда.",
    ]
    # Кнопка отдаёт .exe ФАЙЛОМ прямо в чат (callback overlay_download), а не ведёт на сайт.
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Мой ключ", callback_data="overlay_get_key")],
        [InlineKeyboardButton("⬇️ Скачать оверлей", callback_data="overlay_download")],
        [InlineKeyboardButton("📖 Гайд (тестерский)", callback_data="overlay_guide_tester")],
        [InlineKeyboardButton("✍️ Сообщить баг / идею", callback_data="tester_feedback_start")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    return "\n".join(lines), kb


async def _build_overlay_view(user_id: int):
    """Текст + клавиатура ПУБЛИЧНОГО раздела оверлея (кнопка «🎤 Голосовой оверлей» из главного
    меню). Это вид для ОБЫЧНОГО пользователя: Q&A-сборка + обычный гайд. Тестерский раздел
    (полная сборка, тестерский гайд, репорт багов) — отдельно, в /test."""
    info = await _overlay_version_and_url("qa")
    ver = info[0] if info else None
    head = "🎤 <b>Голосовой оверлей</b>" + (f" — версия <b>{ver}</b>" if ver else "")
    lines = [
        head,
        "Маленькая программа поверх игры: голосом задаёшь вопрос по законам RP-сервера, "
        "а ответ всплывает в углу экрана со ссылкой на нужный пункт законки.",
        "",
        "<b>Как начать:</b>",
        "1. Скачай и установи кнопкой ниже (Windows).",
        "2. Возьми ключ кнопкой «🔑 Мой ключ» и скопируй его.",
        "3. Запусти оверлей и вставь ключ в окно активации.",
        "Ключ привязывается к этому ПК (1 ключ = 1 компьютер). При первом запуске разово "
        "докачается модель распознавания речи (пара минут) — дальше клиент обновляется сам.",
        "",
        "Подробнее — кнопка «📖 Гайд».",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Мой ключ", callback_data="overlay_get_key")],
        [InlineKeyboardButton("⬇️ Скачать оверлей", callback_data="overlay_download_qa")],
        [InlineKeyboardButton("📖 Гайд", callback_data="overlay_guide")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    return "\n".join(lines), kb


async def _build_overlay_promo_view(user_id: int):
    """Промо-экран оверлея для тех, у кого ПОКА НЕТ доступа (кнопка видна всем как реклама).
    Объясняет, что это, и как получить доступ — ВИП за ввод промокода на сервере."""
    lines = [
        "🎤 <b>Голосовой оверлей</b> — твой юрист прямо в игре",
        "",
        "Маленькая программа поверх игры: голосом задаёшь вопрос по законам RP-сервера, "
        "а ответ всплывает в углу экрана со ссылкой на нужный пункт законки. Спорный момент "
        "возник прямо в процессе — не нужно выходить из игры и листать документы.",
        "",
        "• Спрашиваешь голосом, не отвлекаясь от игры",
        "• Ответ — со ссылкой на конкретную статью свода",
        "• Windows, обновляется сам",
        "",
        "🔑 <b>Как получить доступ</b>",
        "Оверлей доступен <b>ВИП-пользователям</b>. ВИП выдаётся за <b>ввод промокода на "
        "сервере</b>: введи промокод в игре, подай заявку с пруфами — после одобрения оверлей "
        "сразу появится здесь, в меню.",
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Получить ВИП за промокод", callback_data="vip_promo_start")],
        [InlineKeyboardButton("📖 Подробнее об оверлее", callback_data="overlay_guide")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    return "\n".join(lines), kb


async def overlay_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка главного меню «🎤 Голосовой оверлей» (видна ВСЕМ). У кого есть доступ — ПУБЛИЧНЫЙ
    раздел (Q&A-сборка + гайд); у кого нет — промо-экран «доступ за ВИП по промокоду».
    НЕ тестерский раздел: тестерское (полная сборка, тестерский гайд, репорты) живёт в /test."""
    query = update.callback_query
    user_id = update.effective_user.id
    await safe_answer_callback_query(query)
    tier = await compute_access_tier(user_id)
    if tier or user_id == config.SUPER_ADMIN_ID:
        text, kb = await _build_overlay_view(user_id)
    else:
        text, kb = await _build_overlay_promo_view(user_id)
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
    except Exception:
        await context.bot.send_message(user_id, text, reply_markup=kb, parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# Гайд по оверлею — ДВА разных, по входу:
#   • публичный (главное меню → overlay_guide)        → обычный Q&A-гайд (всем);
#   • тестерский (/test → overlay_guide_tester)        → тестерский гайд (база Q&A + тестерская часть).
# Команда /guide: Q&A всем + тестерская часть тестерам.
# ---------------------------------------------------------------------------

# Базовая часть — про публичный Q&A-оверлей. Её видят все.
_GUIDE_QA_TEXT = (
    "📖 <b>Голосовой оверлей — как пользоваться</b>\n\n"
    "Это маленькая программа поверх игры: ты голосом задаёшь вопрос по законам "
    "RP-сервера, а она прямо в углу экрана пишет ответ со ссылкой на нужный пункт "
    "законки. Удобно, когда спорный момент возник прямо в процессе и лезть в документы "
    "некогда.\n\n"
    "<b>Установка.</b> Открой в главном меню раздел «🎤 Голосовой оверлей» и нажми "
    "«⬇️ Скачать оверлей» — бот пришлёт .exe прямо сюда. Запусти, поставь, открой. "
    "Работает на Windows.\n\n"
    "<b>Активация.</b> Там же, в разделе «🎤 Голосовой оверлей», нажми «🔑 Мой ключ», "
    "скопируй ключ и вставь его в окно активации оверлея. Один ключ — один компьютер. "
    "Поменял железо или пересел за другой ПК — там же кнопки «🔄 Сменить ПК» и "
    "«♻️ Перевыпустить».\n\n"
    "<b>Первый запуск.</b> При первой активации оверлей докачает модель распознавания речи "
    "— это разовая загрузка, пару минут, дальше всё работает уже без неё. Не пугайся "
    "паузы на старте.\n\n"
    "<b>Если не подключается.</b> Иногда провайдер режет основной сервер (бывает в РФ). На "
    "этот случай есть резервный канал. В режиме «Авто» оверлей при каждом запуске сам "
    "проверяет основной и, если тот недоступен, переключается на резервный РФ-сервер — "
    "делать ничего не нужно (и когда блокировку снимут, так же сам вернётся на основной). "
    "Если хочешь зафиксировать резервный наглухо — Настройки → Соединение → «Канал связи» → "
    "«Резервный (РФ)»: этот выбор запоминается и применяется после перезапуска.\n\n"
    "<b>Выбор сервера.</b> В настройках укажи свой сервер-законку — от этого зависит, по "
    "каким законам оверлей будет отвечать. Выбрал не тот — ответы будут не из той книги.\n\n"
    "<b>Как спросить.</b> Зажми клавишу-кнопку (PTT, push-to-talk), пока она зажата — "
    "проговори вопрос, отпусти. Оверлей распознает речь, найдёт нужный пункт и покажет "
    "ответ карточкой поверх игры. Говори обычным темпом, целыми фразами — так точнее.\n\n"
    "<b>Горячие клавиши.</b> Кнопку PTT и остальные хоткеи можно переназначить в "
    "настройках — поставь то, что не конфликтует с твоими игровыми биндами. Там же "
    "прячется/показывается само окно.\n\n"
    "<b>Где ответ.</b> Ответ появляется карточкой в окне оверлея поверх игры и остаётся на "
    "экране, пока не спросишь следующее — новый ответ встаёт на место прежнего. Чтобы окно "
    "не мешало, его можно свернуть в тонкую строку или целиком спрятать горячей клавишей, а "
    "потом вернуть тем же сочетанием."
)

# Дополнительная часть — только для тестеров (полная сборка с локальным движком и ролями).
_GUIDE_TESTER_TEXT = (
    "🧪 <b>Тестерская сборка — что в ней ещё есть</b>\n\n"
    "У тебя полная версия, в ней включён локальный движок и режимы-роли. Это всё ещё "
    "обкатывается, так что баги — это нормально, как раз их и ловим.\n\n"
    "<b>Локальный движок.</b> Распознавание речи и разбор сцены могут считаться прямо на "
    "твоём ПК, без отправки звука на сервер. Модели для этого качаются кнопкой в "
    "настройках (раздел про локальный движок) — это разовая загрузка на несколько сотен "
    "мегабайт.\n\n"
    "<b>Роли.</b> Кроме обычного Q&A есть режимы под ситуацию:\n"
    "• <b>Адвокат</b> — подсказывает, чем закрыться и как защититься.\n"
    "• <b>Прокурор</b> — наоборот, ищет, к чему придраться и в чём обвинить.\n"
    "• <b>Общий</b> — нейтральный разбор без перекоса в сторону.\n"
    "Роль выбирается в оверлее перед тем, как слушать сцену.\n\n"
    "<b>«Слушать сцену».</b> Кнопка (или хоткей R) включает режим, когда оверлей сам слушает "
    "диалог за столом и подкидывает советы по ходу — не нужно зажимать PTT на каждую "
    "реплику. Твой микрофон он помечает как «Вы», голоса собеседников из игры разбирает "
    "отдельно. Выключаешь той же кнопкой.\n\n"
    "<b>Как репортить.</b> Заметил баг, кривой ответ или просто идею — жми «✍️ Сообщить баг "
    "/ идею» в /test. Опиши, что делал и что пошло не так (а если ответ был странный — "
    "приложи свой вопрос и что выдал оверлей). Это уходит прямо администрации, ответ "
    "придёт сюда в чат."
)


async def guide_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /guide — гайд по оверлею. Q&A-гайд получают все; тестерам ДОПОЛНИТЕЛЬНО,
    отдельным сообщением, приходит тестерский гайд (это два разных гайда, не один склеенный)."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        _GUIDE_QA_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )
    if await _is_tester_or_admin(user_id):
        await update.message.reply_text(
            _GUIDE_TESTER_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )


async def overlay_guide_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «📖 Гайд» из ПУБЛИЧНОГО раздела (главное меню «🎤 Голосовой оверлей»):
    обычный Q&A-гайд — для всех, без тестерской части."""
    query = update.callback_query
    user_id = update.effective_user.id
    await safe_answer_callback_query(query)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    try:
        await query.edit_message_text(
            _GUIDE_QA_TEXT, reply_markup=kb,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception:
        await context.bot.send_message(
            user_id, _GUIDE_QA_TEXT, reply_markup=kb,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )


async def overlay_guide_tester_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «📖 Гайд (тестерский)» из ТЕСТЕРСКОГО раздела /test: тестерский гайд. Базовую
    Q&A-часть шлём первым сообщением (она нужна и тестеру), тестерскую — вторым; это ДРУГОЙ
    гайд, чем у обычного пользователя."""
    query = update.callback_query
    user_id = update.effective_user.id
    await safe_answer_callback_query(query)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    try:
        await query.edit_message_text(
            _GUIDE_QA_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception:
        await context.bot.send_message(
            user_id, _GUIDE_QA_TEXT, parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    await context.bot.send_message(
        user_id, _GUIDE_TESTER_TEXT, reply_markup=kb,
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /test — тестерский раздел (скачать оверлей и т.п.). НЕ входит в анализатор.
    Возвращает END: /test больше не открывает анализатор дыр (он — через кнопку меню)."""
    user_id = update.effective_user.id
    if not await _is_tester_or_admin(user_id):
        await update.message.reply_text(
            "🚫 Доступ к тестовым функциям ограничен.\n"
            "Этот раздел доступен только пользователям с ролью тестера."
        )
        return ConversationHandler.END

    _clear_state(context)
    text, kb = await _build_tester_view(user_id)
    await update.message.reply_text(
        text, reply_markup=kb, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )
    return ConversationHandler.END


# --- Лицензионный ключ оверлея (выдаётся боту юзеру, активируется в софте) ---
def _key_card(key: str, *, fresh: bool = False):
    """Текст+клавиатура карточки ключа."""
    head = "🔑 <b>Новый ключ активации</b>" if fresh else "🔑 <b>Ключ активации оверлея</b>"
    tail = ("Старый ключ больше не действует. " if fresh else "")
    text = (
        f"{head}\n\n"
        f"<code>{key}</code>\n\n"
        f"{tail}Вставь его в окно активации оверлея (кнопка «Активировать»). "
        "Ключ привязывается к этому ПК (1 ключ = 1 компьютер).\n"
        "Сменил ПК или поменял железо — нажми «🔄 Сменить ПК» (ограниченное число раз), "
        "затем активируй ключ на новом компьютере. Нужен совсем новый ключ — «♻️ Перевыпустить»."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Сменить ПК", callback_data="overlay_change_pc")],
        [InlineKeyboardButton("♻️ Перевыпустить ключ", callback_data="overlay_regen_key")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    return text, kb


async def _show_key(query, context, user_id: int, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        await context.bot.send_message(user_id, text, reply_markup=kb, parse_mode=ParseMode.HTML)


async def overlay_get_key_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «🔑 Мой ключ»: показать активный ключ лицензии (выдать, если ещё нет)."""
    query = update.callback_query
    user_id = update.effective_user.id
    from services.device_auth import compute_access_tier
    from services import license as license_service
    tier = await compute_access_tier(user_id)
    if not tier:
        await safe_answer_callback_query(query, "🚫 Доступ к оверлею только для ВИП и тестеров.", show_alert=True)
        return
    await safe_answer_callback_query(query)
    key = await license_service.get_active_key(user_id) or await license_service.issue_key(user_id, tier)
    text, kb = _key_card(key)
    await _show_key(query, context, user_id, text, kb)


async def overlay_regen_key_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«♻️ Перевыпустить» — предупреждение перед выдачей НОВОГО ключа (отвяжет текущий ПК)."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    text = (
        "⚠️ <b>Перевыпуск ключа</b>\n\n"
        "Будет создан <b>новый</b> ключ, а старый перестанет работать — оверлей на текущем ПК "
        "отвяжется (нужно активировать заново новым ключом).\n\nПеревыпустить?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, перевыпустить", callback_data="overlay_regen_key_yes")],
        [InlineKeyboardButton("◀️ Отмена", callback_data="overlay_get_key")],
    ])
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def overlay_regen_key_yes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждённый перевыпуск: issue_key гасит старый ключ (его устройства отвалятся на refresh)."""
    query = update.callback_query
    user_id = update.effective_user.id
    from services.device_auth import compute_access_tier
    from services import license as license_service
    tier = await compute_access_tier(user_id)
    if not tier:
        await safe_answer_callback_query(query, "🚫 Доступ закрыт.", show_alert=True)
        return
    await safe_answer_callback_query(query, "Готово — выдан новый ключ.")
    key = await license_service.issue_key(user_id, tier)
    text, kb = _key_card(key, fresh=True)
    await _show_key(query, context, user_id, text, kb)


async def overlay_change_pc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«🔄 Сменить ПК» — предупреждение перед самосбросом привязки ключа к текущему компьютеру."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    text = (
        "🔄 <b>Сменить ПК</b>\n\n"
        "Привязка ключа к текущему компьютеру будет <b>сброшена</b> — после этого ты сможешь "
        "активировать тот же ключ на другом ПК. Текущий компьютер отвяжется.\n\n"
        "Число самостоятельных смен ограничено. Сбросить привязку?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, сбросить привязку", callback_data="overlay_change_pc_yes")],
        [InlineKeyboardButton("◀️ Назад", callback_data="overlay_get_key")],
    ])
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def overlay_change_pc_yes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждённый самосброс привязки к ПК (self_reset_hwid, бюджет = rebind_max попыток)."""
    query = update.callback_query
    user_id = update.effective_user.id
    from services.device_auth import compute_access_tier
    from services import license as license_service
    tier = await compute_access_tier(user_id)
    if not tier:
        await safe_answer_callback_query(query, "🚫 Доступ закрыт.", show_alert=True)
        return
    res = await license_service.self_reset_hwid(user_id)
    if res.get("error") == "rebind_limit":
        await safe_answer_callback_query(
            query, "Лимит самостоятельных смен ПК исчерпан — обратись к админу.", show_alert=True)
        return
    if res.get("error") == "no_key":
        await safe_answer_callback_query(query, "У тебя нет активного ключа.", show_alert=True)
        return
    await safe_answer_callback_query(query, "Готово — привязка сброшена.")
    key = await license_service.get_active_key(user_id) or ""
    remaining = res.get("remaining", 0)
    note = "Привязка уже была свободна. " if res.get("already_free") else "Привязка к прежнему ПК сброшена. "
    text = (
        "🔄 <b>Привязка к ПК сброшена</b>\n\n"
        f"<code>{key}</code>\n\n"
        f"{note}Теперь активируй этот ключ на нужном компьютере "
        "(кнопка «Активировать» в окне оверлея).\n"
        f"Осталось самостоятельных смен: <b>{remaining}</b>."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
    ])
    await _show_key(query, context, user_id, text, kb)


# ---------------------------------------------------------------------------
# Скачивание установщика оверлея ФАЙЛОМ прямо в Telegram (а не ссылкой на сайт).
# file_id загруженного .exe кэшируется в system_cache по версии → повторная отдача мгновенна.
# ---------------------------------------------------------------------------

# Два варианта установщика (РАСКАТКА «форвард»: full остаётся на ОСНОВНОМ канале — существующих не трогаем):
#   • тестерская full — RpOverlay-Setup-<ver>.exe     / manifest.json     (основной канал, текущая)
#   • публичная Q&A   — RpOverlay-QA-Setup-<ver>.exe  / manifest-qa.json  (новая публичная, отдельный канал)
# Оба лежат в локальном томе /updates. file_id кэшируется ОТДЕЛЬНО по варианту.
_INSTALLER_FILE_ID_KEY_QA = "overlay_installer_file_id_qa"
_INSTALLER_FILE_ID_KEY_FULL = "overlay_installer_file_id_full"

# Описание варианта: (manifest-файл, шаблон имени .exe, ключ кэша file_id).
_INSTALLER_VARIANTS = {
    "full": ("manifest.json", "RpOverlay-Setup-{ver}.exe", _INSTALLER_FILE_ID_KEY_FULL),
    "qa": ("manifest-qa.json", "RpOverlay-QA-Setup-{ver}.exe", _INSTALLER_FILE_ID_KEY_QA),
}


def _read_local_manifest(name: str = "manifest.json") -> Optional[Dict[str, Any]]:
    """Манифест из ЛОКАЛЬНОГО тома /updates (без сети). None, если файла нет/битый."""
    try:
        p = os.path.join(config.OVERLAY_UPDATES_DIR, name)
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.warning("[TEST] чтение локального %s не удалось", name, exc_info=True)
    return None


async def _overlay_version_and_url(variant: str = "full") -> Optional[tuple[str, str]]:
    """Версия установщика + прямая ссылка для варианта ('full' | 'qa').
    Приоритет — ЛОКАЛЬНЫЙ манифест варианта (том /updates, без сети). Для 'full' (основной канал) есть
    фолбэки (HTTP-манифест, затем дефолт из config). Для 'qa' (новая публичная) фолбэков НЕТ: если
    manifest-qa.json ещё не залит — возвращаем None (сборка пока недоступна)."""
    base = config.OVERLAY_PUBLIC_URL.rstrip("/")
    manifest_name, exe_tpl, _ = _INSTALLER_VARIANTS[variant]

    local = _read_local_manifest(manifest_name)
    if local and local.get("version"):
        ver = local["version"]
        return ver, (local.get("url") or f"{base}/updates/{exe_tpl.format(ver=ver)}")

    if variant == "qa":
        # Публичная Q&A ещё не выложена — без молчаливых подмен на full.
        return None

    # full (основной канал): фолбэк на HTTP-манифест, затем на дефолт из config.
    manifest = await _fetch_overlay_manifest()
    if manifest and manifest.get("url"):
        ver = manifest.get("version") or config.OVERLAY_LATEST_VERSION
        return ver, manifest["url"]
    ver = config.OVERLAY_LATEST_VERSION
    return ver, f"{base}/updates/{exe_tpl.format(ver=ver)}"


async def _get_cached_installer_file_id(cache_key: str, version: str) -> Optional[str]:
    """Telegram file_id ранее загруженного .exe для этой версии/варианта (или None)."""
    try:
        val = await db_manager.fetchval(
            "SELECT value FROM system_cache WHERE key = $1", cache_key
        )
    except Exception:
        return None
    if not val:
        return None
    try:
        data = val if isinstance(val, dict) else json.loads(val)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("version") == version:
        return data.get("file_id")
    return None


async def _set_cached_installer_file_id(cache_key: str, version: str, file_id: str) -> None:
    """Запомнить file_id .exe для версии/варианта (system_cache, переживает рестарт)."""
    try:
        await db_manager.execute(
            """
            INSERT INTO system_cache (key, value, updated_at)
            VALUES ($1, $2::jsonb, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            cache_key,
            json.dumps({"version": version, "file_id": file_id}),
        )
    except Exception:
        logger.warning("[TEST] не удалось закэшировать file_id установщика", exc_info=True)


async def overlay_download_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «⬇️ Скачать оверлей»: бот сам отдаёт .exe установщик файлом в чат.

    Вариант зависит от ВХОДА: из ТЕСТЕРСКОГО раздела /test (callback overlay_download) тестер/
    админ получает full-сборку (RpOverlay-Full-Setup); из ПУБЛИЧНОГО раздела главного меню
    (callback overlay_download_qa) — всегда публичную Q&A (RpOverlay-Setup), даже если это
    админ. file_id кэшируется отдельно по варианту: кэш-хит → отдаём мгновенно; кэш-промах →
    тяжёлую загрузку/выгрузку (≈25 МБ) делаем в ФОНЕ (concurrent_updates выключен — апдейты
    идут по одному, блокирующая отправка тормозила бы остальных юзеров).
    """
    query = update.callback_query
    user_id = update.effective_user.id
    # Доступ к загрузке вообще закрыт всем, кроме тех, у кого есть tier на оверлей.
    tier = await compute_access_tier(user_id)
    if not tier:
        await safe_answer_callback_query(
            query, "🚫 Доступ к загрузке только для ВИП и тестеров.", show_alert=True
        )
        return
    await safe_answer_callback_query(query)

    # Публичный вход (overlay_download_qa) — ВСЕГДА Q&A, даже админу. Тестерский вход
    # (overlay_download) — full тестеру/админу, иначе Q&A.
    forced_qa = (query.data == "overlay_download_qa")
    is_full = (not forced_qa) and await _is_tester_or_admin(user_id)
    variant = "full" if is_full else "qa"
    _, exe_tpl, cache_key = _INSTALLER_VARIANTS[variant]

    info = await _overlay_version_and_url(variant)
    if info is None:
        # Только для qa: публичная Q&A-сборка ещё не залита (нет manifest-qa.json/.exe).
        await context.bot.send_message(
            user_id,
            "📦 Публичная версия оверлея пока недоступна — её ещё не выложили. Загляни чуть позже.",
        )
        return
    ver, dl_url = info
    filename = exe_tpl.format(ver=ver)
    if variant == "full":
        caption = (
            f"🖥 <b>Голосовой оверлей</b> — установщик <b>v{ver}</b> (тестерская сборка)\n"
            "Запусти .exe и следуй инструкции из /test (вход через Telegram)."
        )
    else:
        caption = (
            f"🖥 <b>Голосовой оверлей</b> — установщик <b>v{ver}</b>\n"
            "Запусти .exe, возьми ключ кнопкой «🔑 Мой ключ» и активируй им оверлей. "
            "Как пользоваться — кнопка «📖 Гайд»."
        )

    # Кэш: этот установщик уже загружали в Telegram — отдаём по file_id мгновенно.
    cached = await _get_cached_installer_file_id(cache_key, ver)
    if cached:
        try:
            await context.bot.send_document(
                chat_id=user_id, document=cached, filename=filename,
                caption=caption, parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            logger.warning("[TEST] отправка по кэш-file_id не удалась → перезагружаю", exc_info=True)

    # Кэш-промах: берём .exe (локальный том → иначе HTTP) и грузим в Telegram в фоне.
    local_path = os.path.join(config.OVERLAY_UPDATES_DIR, filename)
    notice = await context.bot.send_message(user_id, "⏳ Готовлю установщик, несколько секунд…")
    asyncio.create_task(
        _download_and_send_installer(
            context.bot, user_id, cache_key, ver, dl_url, local_path, filename, caption, notice
        )
    )


async def _download_and_send_installer(bot, user_id, cache_key, ver, dl_url, local_path, filename, caption, notice) -> None:
    """Фоновая задача: взять .exe (приоритет — локальный том /updates, иначе HTTP),
    отправить файлом, закэшировать file_id. При ошибке — деградируем до прямой ссылки."""
    async def _fallback(text: str) -> None:
        try:
            await notice.edit_text(text + "\n" + dl_url, disable_web_page_preview=True)
        except Exception:
            pass

    data = None
    # 1) Локальный файл (том /updates смонтирован в бот) — без сети, мгновенно.
    if local_path and os.path.isfile(local_path):
        try:
            with open(local_path, "rb") as fh:
                data = fh.read()
        except Exception:
            logger.warning("[TEST] чтение локального .exe не удалось: %s", local_path, exc_info=True)
            data = None
    # 2) Фолбэк — HTTP-скачивание с раздачи.
    if data is None:
        try:
            import httpx
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True
            ) as client:
                resp = await client.get(dl_url)
                resp.raise_for_status()
                data = resp.content
        except Exception:
            logger.error("[TEST] не удалось получить установщик: %s", dl_url, exc_info=True)
            await _fallback("⚠️ Не получилось подготовить файл. Прямая ссылка:")
            return

    # Лимит загрузки ботом — ~50 МБ.
    if len(data) > 49 * 1024 * 1024:
        await _fallback("⚠️ Файл слишком большой для Telegram. Прямая ссылка:")
        return

    bio = io.BytesIO(data)
    bio.name = filename
    try:
        msg = await bot.send_document(
            chat_id=user_id, document=bio, filename=filename,
            caption=caption, parse_mode=ParseMode.HTML,
            read_timeout=180, write_timeout=180, connect_timeout=30,
        )
        if msg and msg.document:
            await _set_cached_installer_file_id(cache_key, ver, msg.document.file_id)
        try:
            await notice.delete()
        except Exception:
            pass
    except Exception:
        logger.error("[TEST] не удалось отправить установщик файлом", exc_info=True)
        await _fallback("⚠️ Не удалось отправить файл. Прямая ссылка:")


async def start_loophole(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Вход через кнопку главного меню — доступ подписчикам и выше (с живой проверкой канала)."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    user_id = update.effective_user.id
    # Фича может быть отключена из админки — отбиваем вход даже по «застрявшей» кнопке.
    if not await is_loophole_enabled():
        try:
            await query.edit_message_text(
                "🕳 Анализатор законов сейчас временно отключён администратором.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return ConversationHandler.END
    if not await _loophole_access_allowed(user_id, context):
        try:
            await query.edit_message_text(
                "🔒 <b>Анализатор законов</b> доступен только подписчикам канала (и выше).\n\n"
                "Подпишитесь на канал и нажмите «Проверить подписку», затем снова "
                "откройте анализатор.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💎 Подписаться / проверить", callback_data="offer_subscription")],
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
                ]),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return ConversationHandler.END

    _clear_state(context)
    return await _show_projects(update, context)


_GUIDE_TEXT = (
    "🕳 <b>Анализатор законов — как это работает</b>\n\n"
    "Инструмент ищет в правилах сервера <b>дыры, противоречия и абузы</b>: размытые "
    "формулировки, пробелы, отсутствие процедур/сроков/санкций, коллизии полномочий и "
    "способы обойти норму по букве. Каждая находка оценивается с 4 позиций — "
    "<i>игрок-абузер, адвокат, прокурор, администратор</i> — и сопровождается "
    "рекомендацией, как закрыть дыру.\n"
    "<i>Только внутриигровое право RP-сервера, не реальное.</i>\n\n"
    "⏳ <b>Очередь.</b> Запросы выполняются по одному за раз; у VIP — приоритет. "
    "Один активный запрос на пользователя: пока идёт текущий, новый не создать "
    "(показывается ваша позиция). Отчёт придёт в чат, ботом можно пользоваться дальше.\n\n"
    "<b>1. Тип анализа</b>\n"
    "⚡ <b>Быстрый скан</b> — поверхностный обзор главных уязвимостей.\n"
    "🕳 <b>Глубокий абуз-анализ</b> — детальный поиск способов абуза и обхода.\n"
    "📋 <b>Анализ кейса</b> — разбор конкретной ситуации: слабые места, аргументы "
    "защиты и обвинения (ситуацию нужно описать текстом).\n"
    "📝 <b>Проверка нового закона</b> — сверяет проект закона с действующими: "
    "конфликты, риски, точечные правки (текст закона нужно прислать).\n"
    "🔍 <b>Полный аудит</b> — максимально подробный разбор по всем выбранным документам.\n"
    "😈 <b>Адвокат дьявола</b> — ищет, как умный игрок обойдёт закон по букве.\n\n"
    "<b>2. Источник законов</b>\n"
    "🤖 <b>Auto RAG</b> — ИИ сам подберёт нужные законы по вашему текстовому запросу.\n"
    "📚 <b>Все законы сервера</b> — берёт весь свод: самый широкий охват, но дольше.\n"
    "✋ <b>Выбрать вручную</b> — вы сами отмечаете документы из списка.\n"
    "🧪 <b>Hybrid</b> — ваш ручной выбор + ИИ докинет смежные документы по запросу.\n\n"
    "<b>3. Профиль модели</b> (глубина против скорости)\n"
    "⚡ <b>Быстро</b> — лёгкая модель, до 5 документов, быстро и дёшево.\n"
    "⚖ <b>Стандартно</b> — до 10 документов, баланс скорости и качества.\n"
    "🧠 <b>Глубоко</b> — мощная pro-модель, до 20 документов, тщательнее.\n"
    "🎓 <b>Экспертный аудит</b> — pro + пакетный режим, до 40 документов, "
    "максимально полно (дольше всего).\n\n"
    "<i>Совет: под конкретный вопрос — Auto RAG + Глубокий абуз-анализ; "
    "для ревизии всего свода — Все законы + Полный аудит + Глубоко/Эксперт.</i>"
)


def _build_projects_view(projects):
    """Строит (текст, клавиатуру) экрана выбора проекта (стартовая страница функции)."""
    keyboard = [[InlineKeyboardButton("ℹ️ Как это работает", callback_data=CB_GUIDE)]]
    for project_id in projects:
        pdata = config.PROJECTS_CONFIG.get(project_id, {})
        keyboard.append(
            [InlineKeyboardButton(
                pdata.get("display_name", project_id),
                callback_data=f"{CB_PROJECT}{project_id}",
            )]
        )
    keyboard.append([InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)])

    text = (
        "🕳 <b>Анализатор дыр в законке</b>\n\n"
        "Поиск противоречий, пробелов и абуз-сценариев в правилах сервера.\n"
        "Запросы обрабатываются в порядке очереди.\n\n"
        "Не знаете, что выбрать — нажмите «ℹ️ Как это работает».\n\n"
        "🌐 <b>Шаг 1:</b> выберите проект."
    )
    return text, InlineKeyboardMarkup(keyboard)


async def _show_projects(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    projects = await get_enabled_projects()
    if not projects:
        await update.effective_message.reply_text("⚠ Нет доступных проектов в конфигурации.")
        return ConversationHandler.END

    text, markup = _build_projects_view(projects)
    await update.effective_message.reply_text(
        text, reply_markup=markup, parse_mode=ParseMode.HTML
    )
    return TEST_PROJECT


async def on_guide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показывает встроенный гайд по функции (остаётся в состоянии выбора проекта)."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("◀️ Назад к выбору", callback_data=CB_GUIDE_BACK),
    ]])
    try:
        await query.edit_message_text(
            _GUIDE_TEXT, reply_markup=keyboard,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    except Exception:
        await query.message.reply_text(
            _GUIDE_TEXT, reply_markup=keyboard,
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
    return TEST_PROJECT


async def on_guide_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Возврат из гайда к выбору проекта."""
    query = update.callback_query
    await safe_answer_callback_query(query)
    projects = await get_enabled_projects()
    if not projects:
        await query.edit_message_text("⚠ Нет доступных проектов в конфигурации.")
        return ConversationHandler.END
    text, markup = _build_projects_view(projects)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception:
        await query.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    return TEST_PROJECT


async def on_project_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    project_id = query.data[len(CB_PROJECT):]
    _state(context)["project_id"] = project_id

    servers = await get_enabled_servers(project_id)
    if not servers:
        await query.edit_message_text("⚠ В проекте нет включенных серверов.")
        _clear_state(context)
        return ConversationHandler.END

    keyboard = []
    for server_id in servers:
        sdata = config.SERVERS_CONFIG.get(server_id, {})
        keyboard.append(
            [InlineKeyboardButton(
                sdata.get("display_name", server_id),
                callback_data=f"{CB_SERVER}{server_id}",
            )]
        )
    keyboard.append([InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)])

    await query.edit_message_text(
        "🎮 <b>Шаг 2:</b> выберите сервер.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return TEST_SERVER


async def on_server_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    server_id = query.data[len(CB_SERVER):]
    _state(context)["server_id"] = server_id

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"{CB_TYPE}{atype}")]
        for atype, label in ANALYSIS_TYPE_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)])

    await query.edit_message_text(
        "🧭 <b>Шаг 3:</b> выберите тип анализа.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return TEST_TYPE


async def on_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    atype = query.data[len(CB_TYPE):]
    _state(context)["analysis_type"] = atype

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"{CB_SOURCE}{mode}")]
        for mode, label in SELECTION_MODE_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)])

    await query.edit_message_text(
        "📚 <b>Шаг 4:</b> выберите источник законов.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return TEST_SOURCE


async def on_source_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    mode = query.data[len(CB_SOURCE):]
    _state(context)["selection_mode"] = mode

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"{CB_PROFILE}{p}")]
        for p, label in PROFILE_LABELS.items()
    ]
    keyboard.append([InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)])

    await query.edit_message_text(
        "🧠 <b>Шаг 5:</b> выберите профиль модели.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return TEST_PROFILE


async def on_profile_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    profile = query.data[len(CB_PROFILE):]
    state = _state(context)
    state["model_profile"] = profile

    mode = state.get("selection_mode")

    # Для manual / hybrid — выбор документов кнопками.
    if mode in ("manual_select", "hybrid"):
        return await _show_manual_picker(update, context)

    # Для auto_rag — спрашиваем текст запроса.
    if mode == "auto_rag":
        await query.edit_message_text(
            "✍ <b>Шаг 6:</b> введите запрос/тему для анализа одним сообщением.\n"
            "Пример: <i>как развалить задержание за неподчинение</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_kb(),
        )
        return TEST_TEXT_INPUT

    # Для full_server — сразу к подтверждению.
    return await _show_confirmation(update, context)


# ---------------------------------------------------------------------------
# Manual picker
# ---------------------------------------------------------------------------


async def _load_docs_for_picker(state: dict) -> List[LawContextDocument]:
    docs = state.get("_docs_cache")
    if docs is None:
        docs = await list_laws_for_selection(state["server_id"])
        state["_docs_cache"] = docs
    return docs


async def _show_manual_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _state(context)
    docs = await _load_docs_for_picker(state)
    if not docs:
        await update.callback_query.edit_message_text(
            "⚠ В базе нет законов с gemini_file_uri для этого сервера."
        )
        _clear_state(context)
        return ConversationHandler.END

    selected: List[int] = state.setdefault("manual_law_ids", [])
    profile: ModelProfile = state.get("model_profile", "balanced")
    limit = {
        "fast": config.LOOPHOLE_MAX_MANUAL_DOCS_FAST,
        "balanced": config.LOOPHOLE_MAX_MANUAL_DOCS_BALANCED,
        "deep": config.LOOPHOLE_MAX_MANUAL_DOCS_DEEP,
        "expert": config.LOOPHOLE_MAX_MANUAL_DOCS_EXPERT,
    }[profile]

    keyboard: List[List[InlineKeyboardButton]] = []
    for d in docs:
        marker = "✅" if d.id in selected else "▫️"
        title = d.title if len(d.title) <= 50 else d.title[:47] + "…"
        keyboard.append(
            [InlineKeyboardButton(f"{marker} {title}", callback_data=f"{CB_DOC_TOGGLE}{d.id}")]
        )

    keyboard.append([InlineKeyboardButton(
        f"➡ Готово ({len(selected)}/{limit})",
        callback_data=CB_DOC_DONE,
    )])
    keyboard.append([InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)])

    text = (
        f"📑 <b>Шаг 6:</b> выберите законы для анализа.\n"
        f"Выбрано: <b>{len(selected)}/{limit}</b>"
    )
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
            )
        except Exception:
            await update.effective_message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
            )
    else:
        await update.effective_message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML
        )
    return TEST_MANUAL


async def on_doc_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    doc_id = int(query.data[len(CB_DOC_TOGGLE):])
    state = _state(context)
    selected: List[int] = state.setdefault("manual_law_ids", [])
    profile: ModelProfile = state.get("model_profile", "balanced")
    limit = {
        "fast": config.LOOPHOLE_MAX_MANUAL_DOCS_FAST,
        "balanced": config.LOOPHOLE_MAX_MANUAL_DOCS_BALANCED,
        "deep": config.LOOPHOLE_MAX_MANUAL_DOCS_DEEP,
        "expert": config.LOOPHOLE_MAX_MANUAL_DOCS_EXPERT,
    }[profile]

    if doc_id in selected:
        selected.remove(doc_id)
    else:
        if len(selected) >= limit:
            await safe_answer_callback_query(
                query, f"Лимит для профиля '{profile}': {limit}", show_alert=True
            )
            return TEST_MANUAL
        selected.append(doc_id)
    return await _show_manual_picker(update, context)


async def on_doc_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await safe_answer_callback_query(query)
    state = _state(context)
    selected: List[int] = state.get("manual_law_ids", [])
    if not selected:
        await safe_answer_callback_query(query, "Выберите хотя бы один документ", show_alert=True)
        return TEST_MANUAL

    if state.get("selection_mode") == "hybrid":
        await query.edit_message_text(
            "✍ Введите текст запроса для расширения через RAG (или /skip):",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_kb(),
        )
        return TEST_TEXT_INPUT

    return await _show_confirmation(update, context)


async def on_user_request_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обработчик текстового ввода. Живёт в выделенном state TEST_TEXT_INPUT,
    чтобы НЕ перехватывать сообщения юзера в других состояниях."""
    state = _state(context)
    text = (update.message.text or "").strip()
    if text and text != "/skip":
        state["user_request"] = text[:4000]
    return await _show_confirmation(update, context)


# ---------------------------------------------------------------------------
# Confirmation + run
# ---------------------------------------------------------------------------


async def _show_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _state(context)
    server_id = state.get("server_id")
    sdata = config.SERVERS_CONFIG.get(server_id, {})

    lines = [
        "<b>📋 Подтверждение запуска анализа</b>",
        f"Сервер: <b>{sdata.get('display_name', server_id)}</b>",
        f"Тип анализа: {ANALYSIS_TYPE_LABELS.get(state.get('analysis_type'), '—')}",
        f"Источник: {SELECTION_MODE_LABELS.get(state.get('selection_mode'), '—')}",
        f"Профиль модели: {PROFILE_LABELS.get(state.get('model_profile'), '—')}",
    ]
    if state.get("manual_law_ids"):
        lines.append(f"Документов выбрано: <b>{len(state['manual_law_ids'])}</b>")
    if state.get("user_request"):
        ur = state["user_request"]
        lines.append("Запрос: <i>" + (ur if len(ur) <= 200 else ur[:200] + "…") + "</i>")

    keyboard = [
        [InlineKeyboardButton("🚀 Запустить", callback_data=CB_CONFIRM_RUN)],
        [InlineKeyboardButton("✖ Отмена", callback_data=CB_CANCEL)],
    ]
    target = update.effective_message
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML,
            )
            return TEST_CONFIRM
        except Exception:
            pass
    await target.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )
    return TEST_CONFIRM


async def on_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Ставит запрос в очередь анализатора и СРАЗУ завершает ConversationHandler.
    Сам анализ выполняет фоновый воркер очереди (concurrency=1, приоритет VIP/админ).
    Один пользователь может иметь только один активный запрос (требование очереди).
    """
    query = update.callback_query
    await safe_answer_callback_query(query)
    state = _state(context)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    # Снимок параметров: state будет очищен сразу после возврата.
    params = {
        "mode": state["selection_mode"],
        "profile": state.get("model_profile", "balanced"),
        "analysis_type": state.get("analysis_type", "quick_scan"),
        "server_id": state["server_id"],
        "project_id": state.get("project_id"),
        "user_request": state.get("user_request"),
        "manual_law_ids": state.get("manual_law_ids") or None,
    }

    # Кэш «весь свод»: при старте проверяем, нет ли уже готового результата.
    fingerprint = None
    if params["mode"] == "full_server" and await is_caching_enabled():
        fingerprint = await compute_fingerprint(
            params["server_id"], params["analysis_type"], params["profile"], "full_server"
        )
        if fingerprint:
            cached = await get_cached_result(fingerprint)
            if cached:
                try:
                    await query.edit_message_text(
                        "✅ Такой анализ по этому своду уже выполнялся — отправляю готовый результат…",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                await send_loophole_report(context.bot, chat_id, cached)
                _clear_state(context)
                return ConversationHandler.END

    # Требование: один активный запрос на пользователя.
    existing = await get_active_job_for_user(user_id)
    if existing:
        await _show_existing_position(query, existing)
        _clear_state(context)
        return ConversationHandler.END

    priority = await _compute_priority(user_id)
    try:
        job_id = await enqueue_job(user_id, chat_id, params, priority, fingerprint=fingerprint)
    except asyncpg.UniqueViolationError:
        # Гонка double-tap: задача уже создана параллельным нажатием.
        existing = await get_active_job_for_user(user_id)
        if existing:
            await _show_existing_position(query, existing)
        _clear_state(context)
        return ConversationHandler.END

    pos = await get_queue_position(job_id)
    sent_msg_id = None
    try:
        msg = await query.edit_message_text(
            _enqueued_text(pos), reply_markup=_my_position_kb(job_id), parse_mode=ParseMode.HTML
        )
        sent_msg_id = getattr(msg, "message_id", None)
        if sent_msg_id is None and query.message:
            sent_msg_id = query.message.message_id
    except Exception:
        try:
            sent = await context.bot.send_message(
                chat_id=chat_id, text=_enqueued_text(pos),
                reply_markup=_my_position_kb(job_id), parse_mode=ParseMode.HTML,
            )
            sent_msg_id = sent.message_id
        except Exception:
            logger.warning("[LOOPHOLE] failed to send enqueue confirmation")
    if sent_msg_id:
        await set_position_msg_id(job_id, sent_msg_id)

    # Разбудить воркера очереди, чтобы он сразу проверил новую задачу.
    wake = context.application.bot_data.get("loophole_wake_event")
    if wake is not None:
        wake.set()

    _clear_state(context)
    return ConversationHandler.END


async def _show_existing_position(query, job: Dict[str, Any]) -> None:
    """Показывает пользователю статус его уже существующей активной задачи."""
    if job["status"] == "running":
        txt = "⏳ Ваш анализ уже выполняется. Дождитесь отчёта — он придёт в этот чат."
    else:
        pos = await get_queue_position(job["id"])
        txt = ("⏳ Ваш запрос уже в очереди.\n"
               f"Текущая позиция: <b>{pos}</b>.")
    try:
        await query.edit_message_text(
            txt, reply_markup=_my_position_kb(job["id"]), parse_mode=ParseMode.HTML
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Глобальные кнопки пользователя (приходят ВНЕ conversation, регистрируются в main.py)
# ---------------------------------------------------------------------------

async def user_loophole_my_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        job_id = int(query.data[len(CB_MY_POSITION):])
    except (ValueError, TypeError):
        await safe_answer_callback_query(query)
        return
    job = await get_job(job_id)
    user_id = update.effective_user.id
    if not job or job["user_id"] != user_id:
        await query.answer("Запрос не найден.", show_alert=True)
        return
    if job["status"] == "running":
        await query.answer("Ваш анализ уже выполняется. Ожидайте отчёт.", show_alert=True)
    elif job["status"] == "queued":
        pos = await get_queue_position(job_id)
        if await is_queue_paused():
            await query.answer(
                f"Ваша позиция: {pos}. Обработка временно приостановлена администратором — "
                "запрос сохранён и продолжится после возобновления.",
                show_alert=True,
            )
        else:
            await query.answer(f"Ваша позиция в очереди: {pos}", show_alert=True)
    else:
        await query.answer("Запрос уже завершён или отменён.", show_alert=True)


async def user_loophole_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    try:
        job_id = int(query.data[len(CB_MY_CANCEL):])
    except (ValueError, TypeError):
        await safe_answer_callback_query(query)
        return
    user_id = update.effective_user.id
    cancelled = await cancel_job(job_id, user_id=user_id)
    if cancelled:
        await query.answer("Запрос отменён.")
        try:
            await query.edit_message_text(
                "✖ Ваш запрос на анализ отменён.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
                ]]),
            )
        except Exception:
            pass
    else:
        # Либо уже выполняется (running), либо не принадлежит пользователю/завершён.
        job = await get_job(job_id)
        if job and job["user_id"] == user_id and job["status"] == "running":
            await query.answer("Анализ уже выполняется — отмена недоступна.", show_alert=True)
        else:
            await query.answer("Запрос не найден или уже завершён.", show_alert=True)



async def on_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await safe_answer_callback_query(update.callback_query)
        try:
            await update.callback_query.edit_message_text("✖ Анализ отменён.")
        except Exception:
            pass
    else:
        await update.effective_message.reply_text("✖ Анализ отменён.")
    _clear_state(context)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# ConversationHandler
# ---------------------------------------------------------------------------


test_loophole_conv_handler = ConversationHandler(
    entry_points=[
        CommandHandler("test", test_command),                                   # тестеры/админы
        CallbackQueryHandler(start_loophole, pattern=f"^{CB_START_LOOPHOLE}$"),  # кнопка меню (подписчики+)
    ],
    states={
        TEST_PROJECT: [
            CallbackQueryHandler(on_guide, pattern=f"^{CB_GUIDE}$"),
            CallbackQueryHandler(on_guide_back, pattern=f"^{CB_GUIDE_BACK}$"),
            CallbackQueryHandler(on_project_chosen, pattern=f"^{CB_PROJECT}"),
        ],
        TEST_SERVER: [CallbackQueryHandler(on_server_chosen, pattern=f"^{CB_SERVER}")],
        TEST_TYPE: [CallbackQueryHandler(on_type_chosen, pattern=f"^{CB_TYPE}")],
        TEST_SOURCE: [CallbackQueryHandler(on_source_chosen, pattern=f"^{CB_SOURCE}")],
        TEST_PROFILE: [CallbackQueryHandler(on_profile_chosen, pattern=f"^{CB_PROFILE}")],
        TEST_MANUAL: [
            # ВАЖНО: on_doc_done должен быть ПЕРВЫМ, иначе on_doc_toggle с
            # паттерном f"^{CB_DOC_TOGGLE}" перехватит "test_loop_doc_done"
            # и попытается int("done"). Также делаем паттерн toggle строго
            # цифровым через \d+, чтобы избежать таких коллизий впредь.
            CallbackQueryHandler(on_doc_done, pattern=f"^{CB_DOC_DONE}$"),
            CallbackQueryHandler(on_doc_toggle, pattern=rf"^{CB_DOC_TOGGLE}\d+$"),
        ],
        TEST_TEXT_INPUT: [
            # MessageHandler живёт ТОЛЬКО в этом состоянии — никогда не "съедает"
            # сообщения юзера после завершения /test.
            MessageHandler(filters.TEXT & ~filters.COMMAND, on_user_request_text),
        ],
        TEST_CONFIRM: [
            CallbackQueryHandler(on_run, pattern=f"^{CB_CONFIRM_RUN}$"),
        ],
    },
    fallbacks=[
        CallbackQueryHandler(on_cancel, pattern=f"^{CB_CANCEL}$"),
        CommandHandler("cancel", on_cancel),
        # Гарантия выхода: /start и /test в любом состоянии корректно завершают
        # этот разговор (и позволяют PTB передать /start в user_conv_handler).
        CommandHandler("start", on_cancel),
        CommandHandler("test", test_command),
    ],
    per_message=False,
    allow_reentry=True,
    name="test_loophole_conv",
)


# END OF FILE user_handlers/test_loophole_flow.py
