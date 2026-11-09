# START OF FILE main.py

import asyncio
from datetime import timedelta, datetime
import logging
import os
import multiprocessing  # <<< НОВЫЙ ИМПОРТ
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, TypeHandler
)
from concurrent.futures import ProcessPoolExecutor

# Локальные импорты.
from database.manager import db_manager
import config
from utils import populate_servers_from_config, setup_logging
from knowledge_base_manager import update_all_servers_knowledge_base
from admin_handlers import (
    handle_forwarded_message, admin_conv_handler,  # Импортируем готовый admin_conv_handler
    bypass_kb_protection_handler,
    accept_vip_application, reject_vip_application,  # заявки на ВИП: одобрить/отклонить (из канала)
    request_vip_info, handle_vip_revision_reply, vip_rev_reply_filter,  # ВИП: отправить на доработку
)
from user_handlers.vip_application_flow import vip_application_conv_handler  # анкета ВИП за промокод
from admin_support_tickets import close_support_ticket  # обращения поддержки (сайт): закрыть из карточки

# >>> НАЧАЛО ИЗМЕНЕНИЙ: ИМПОРТ ИЗ НОВОГО ПАКЕТА user_handlers <<<
from user_handlers import (
    start,
    main_menu,
    personal_account,
    user_conv_handler,
    assistant_conv_handler,
    interview_conv_handler,
    report_conv_handler,
    subscription_conv_handler,
    history_conv_handler,
    complaint_conv_handler,
    pch_conv_handler,
    prosecutor_assistant_menu,
    test_loophole_conv_handler,  # /test — тестовый анализатор дыр (только тестеры/админы)
    tester_application_conv_handler,  # заявки в тестеры (?start=apply_tester)
    tester_feedback_conv_handler,  # канал обратной связи тестеров (баги/идеи → репорт)
)
from user_handlers.criminal_case_flow import criminal_case_conv_handler
from user_handlers.core import reset_stale_block_flag  # мидлвар: гасит залипший is_blocked при активности юзера
from user_handlers.ai_retry import handle_ai_retry, CALLBACK_PATTERN as AI_RETRY_PATTERN  # кнопки «Повторить»/«Слабее» на перегрузе
# >>> КОНЕЦ ИЗМЕНЕНИЙ <<<

from config import (
    TELEGRAM_BOT_TOKEN, KNOWLEDGE_BASE_REFRESH_HOURS, SUPER_ADMIN_ID
)


def _install_callback_query_answer_patch() -> None:
    # Юзер жмёт кнопку, которой больше 15 секунд (например, старое сообщение
    # после простоя бота) — Telegram отвечает "Query is too old". Без патча
    # обработчик падает на query.answer() и не успевает обновить сообщение.
    # Проглатываем именно этот класс ошибок, остальные пропускаем.
    from telegram import CallbackQuery
    from telegram.error import BadRequest

    original_answer = CallbackQuery.answer

    async def safe_answer(self, *args, **kwargs):
        try:
            return await original_answer(self, *args, **kwargs)
        except BadRequest as exc:
            msg = str(exc)
            if "Query is too old" in msg or "query id is invalid" in msg:
                logging.debug(f"[safe_answer] устаревший callback проигнорирован: {msg}")
                return None
            raise

    CallbackQuery.answer = safe_answer


_install_callback_query_answer_patch()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует необработанные исключения и уведомляет супер-админа."""
    import traceback
    import html
    from telegram.error import BadRequest, NetworkError, TimedOut

    err = context.error

    # Шумные ожидаемые ошибки — логируем тихо и не пушим админу.
    # 1) "Query is too old" — пользователь жмёт устаревшую callback-кнопку
    #    (TG даёт 15 сек на answer_callback_query). Чинить нечего.
    # 2) Сетевые транзиенты polling-цикла (ReadError, TimedOut, etc.) —
    #    python-telegram-bot ретраит их сам, лезть в админку не нужно.
    if isinstance(err, BadRequest) and "Query is too old" in str(err):
        logging.info(f"[error_handler] Устаревший callback от пользователя: {err}")
        return
    if isinstance(err, (NetworkError, TimedOut)):
        logging.warning(f"[error_handler] Сетевой транзиент (будет ретрай): {type(err).__name__}: {err}")
        return

    # Детальное логирование
    logging.error(f"❌ [ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК] Необработанное исключение", exc_info=context.error)

    # Формирование детального отчета для суперадмина
    error_type = type(context.error).__name__ if context.error else "Unknown"
    error_msg = str(context.error)[:1000] if context.error else "No error message"

    # Извлечение информации об обновлении
    update_info = "No update info"
    user_id = "Unknown"

    if update:
        try:
            if hasattr(update, 'effective_user') and update.effective_user:
                user_id = update.effective_user.id

            if hasattr(update, 'effective_message') and update.effective_message:
                msg_text = update.effective_message.text[:200] if update.effective_message.text else "No text"
                update_info = f"Message: {msg_text}"
            elif hasattr(update, 'callback_query') and update.callback_query:
                callback_data = update.callback_query.data[:200] if update.callback_query.data else "No data"
                update_info = f"Callback: {callback_data}"
            else:
                update_info = str(update)[:500]
        except Exception as e:
            logging.warning(f"Не удалось извлечь информацию из update: {e}")
            update_info = str(update)[:500]

    # Получаем traceback
    tb_str = ""
    if context.error:
        try:
            tb_str = ''.join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
            tb_str = tb_str[-2000:]  # Последние 2000 символов
        except Exception:
            tb_str = "Could not format traceback"

    # Экранируем HTML-символы для безопасной отправки в Telegram
    error_type_escaped = html.escape(str(error_type))
    error_msg_escaped = html.escape(str(error_msg))
    update_info_escaped = html.escape(str(update_info))
    tb_str_escaped = html.escape(str(tb_str))

    # Формируем сообщение для суперадмина
    admin_message = (
        f"🚨 <b>КРИТИЧЕСКАЯ ОШИБКА В БОТЕ</b> 🚨\n\n"
        f"⏰ <b>Время:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"👤 <b>Пользователь:</b> <code>{user_id}</code>\n"
        f"⚠️ <b>Тип ошибки:</b> <code>{error_type_escaped}</code>\n\n"
        f"📝 <b>Сообщение:</b>\n<code>{error_msg_escaped}</code>\n\n"
        f"📋 <b>Контекст:</b>\n<code>{update_info_escaped}</code>\n\n"
        f"🔍 <b>Traceback (последние строки):</b>\n<pre>{tb_str_escaped}</pre>"
    )

    # Отправка суперадмину
    try:
        # Разбиваем на части если слишком длинное
        max_len = 4000
        if len(admin_message) > max_len:
            parts = [admin_message[i:i+max_len] for i in range(0, len(admin_message), max_len)]
            for i, part in enumerate(parts, 1):
                await context.bot.send_message(
                    chat_id=SUPER_ADMIN_ID,
                    text=f"<b>Часть {i}/{len(parts)}</b>\n\n{part}",
                    parse_mode='HTML'
                )
        else:
            await context.bot.send_message(
                chat_id=SUPER_ADMIN_ID,
                text=admin_message,
                parse_mode='HTML'
            )
        logging.info(f"✅ Уведомление об ошибке отправлено суперадмину (ID: {SUPER_ADMIN_ID})")
    except Exception as e:
        logging.error(f"❌ Не удалось отправить уведомление об ошибке суперадмину: {e}", exc_info=e)


async def scheduler_callback(context: ContextTypes.DEFAULT_TYPE):
    """Задача, выполняемая по расписанию: обновление всех баз знаний."""
    logging.info("Планировщик: Запуск автоматического обновления всех баз знаний.")
    start_time = asyncio.get_running_loop().time()
    successful_updates, failed_servers = await update_all_servers_knowledge_base()
    duration = asyncio.get_running_loop().time() - start_time
    report_text = (
        f"🤖 **Отчет авто-обновления БЗ**\n"
        f"Завершено: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
        f"Длительность: `{duration:.2f} сек.`\n"
        f"✅ Успешно: {successful_updates}/{len(config.SERVERS_CONFIG)}\n"
    )
    if failed_servers:
        report_text += f"❌ Ошибки: `{', '.join(failed_servers)}`"
    try:
        await context.bot.send_message(chat_id=SUPER_ADMIN_ID, text=report_text, parse_mode="Markdown")
        logging.info("Планировщик: Отчет об авто-обновлении успешно отправлен супер-админу.")
    except Exception as e:
        logging.error(f"Планировщик: Не удалось отправить отчет супер-админу (ID: {SUPER_ADMIN_ID}).", exc_info=e)



async def schedule_kb_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    """Планирует или перепланирует задачу обновления БЗ."""
    job_name = "job_kb_refresh"
    # Удаляем старую задачу, если она есть, чтобы избежать дублирования
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()
        logging.info(f"Планировщик: Удалена предыдущая задача '{job_name}'.")
    interval = timedelta(hours=KNOWLEDGE_BASE_REFRESH_HOURS)
    context.job_queue.run_repeating(scheduler_callback, interval=interval, first=interval, name=job_name)
    logging.info(f"Планировщик: Авто-обновление БЗ запланировано. Интервал: {interval}.")


async def restart_scheduler_and_notify(context: ContextTypes.DEFAULT_TYPE):
    """Перезапускает планировщик и уведомляет администратора."""
    await schedule_kb_refresh_job(context)
    try:
        await context.bot.send_message(
            chat_id=SUPER_ADMIN_ID,
            text=f"⚙️ Таймер авто-обновления БЗ сброшен. Следующее обновление через {KNOWLEDGE_BASE_REFRESH_HOURS} часов."
        )
    except Exception as e:
        logging.warning(f"Не удалось уведомить админа о сбросе таймера. Ошибка: {e}")


async def post_init(application: Application) -> None:
    """Выполняется один раз после запуска бота для инициализации ресурсов."""
    logging.info("===== НАЧАЛО ИНИЦИАЛИЗАЦИИ БОТА (post_init) =====")
    try:
        logging.info("Шаг 1/5: Подключение к базе данных...")
        await db_manager.connect()
        logging.info("Шаг 1/5: Успешно подключено к БД.")

        logging.info("Шаг 2/5: Синхронизация серверов из конфига в БД...")
        await populate_servers_from_config()
        logging.info("Шаг 2/5: Синхронизация серверов завершена.")

        logging.info("Шаг 3/5: Инициализация Vertex AI SDK...")
        if not os.path.exists(config.VERTEX_CREDENTIALS_FILE):
            error_message = f"КРИТИЧЕСКАЯ ОШИБКА: Файл учетных данных Vertex AI '{config.VERTEX_CREDENTIALS_FILE}' не найден. Бот не может быть запущен."
            logging.critical(error_message)
            raise FileNotFoundError(error_message)

        from rate_limiter import init_client
        from services.sa_admin import project_id_for

        # project_id берём ИЗ активного сервис-аккаунта (его можно сменить из админки на SA
        # из ДРУГОГО проекта — см. services/sa_admin). Фолбэк — config.GCP_PROJECT_ID.
        _vertex_pid = project_id_for(config.VERTEX_CREDENTIALS_FILE)
        # Инициализируем единственный клиент Vertex AI (регион "global")
        client = init_client(
            project_id=_vertex_pid,
            credentials_file=config.VERTEX_CREDENTIALS_FILE,
            region=config.GCP_REGION
        )
        application.bot_data['genai_client'] = client
        logging.info(
            f"Шаг 3/5: Vertex AI SDK инициализирован. Регион: {config.GCP_REGION}"
        )

        # Активный GCS-бакет (мог быть переопределён при смене проекта/SA — см. services/sa_admin).
        try:
            from services.sa_admin import load_active_bucket
            _ab = await load_active_bucket()
            if _ab:
                logging.info(f"Активный GCS-бакет из настроек: {_ab}")
        except Exception:
            logging.warning("Не удалось загрузить активный GCS-бакет", exc_info=True)

        logging.info("Шаг 4/5: Создание пула процессов для CPU-bound задач...")
        process_pool = ProcessPoolExecutor(max_workers=2)
        application.bot_data['process_pool'] = process_pool
        logging.info(f"Шаг 4/5: Пул процессов (`ProcessPoolExecutor`) успешно создан.")

        # Режим тех. работ: восстанавливаем сохранённое состояние (переживает рестарт bot/БД).
        try:
            from utils import get_maintenance_mode
            application.bot_data['maintenance_mode'] = await get_maintenance_mode()
        except Exception:
            application.bot_data['maintenance_mode'] = False
            logging.warning("Не удалось загрузить флаг тех. работ — по умолчанию ВЫКЛ", exc_info=True)
        logging.info(
            "Режим технических работ: %s",
            "ВКЛЮЧЕН" if application.bot_data.get('maintenance_mode') else "ВЫКЛЮЧЕН",
        )

        logging.info("Шаг 5/5: Настройка фоновых задач (планировщик)...")
        application.bot_data['scheduler_callback'] = lambda: restart_scheduler_and_notify(application)
        await schedule_kb_refresh_job(application)


        logging.info("Шаг 5/5: Планировщик успешно настроен.")

        # Очередь анализатора дыр в законке: восстановление зависших задач + воркер.
        logging.info("Запуск воркера очереди анализатора дыр...")
        from services.law_loophole.queue import LoopholeQueueWorker, recover_orphaned_jobs
        await recover_orphaned_jobs()  # ДО старта воркера
        loophole_worker = LoopholeQueueWorker(application)
        application.bot_data['loophole_worker'] = loophole_worker
        application.bot_data['loophole_wake_event'] = loophole_worker.wake_event
        loophole_worker.task = application.create_task(loophole_worker.run())
        logging.info("Воркер очереди анализатора дыр запущен.")

    except Exception as e:
        logging.critical(f"КРИТИЧЕСКАЯ ОШИБКА: Не удалось инициализировать базовые сервисы. Бот не будет запущен.",
                         exc_info=e)
        raise
    logging.info("===== ИНИЦИАЛИЗАЦИЯ БОТА УСПЕШНО ЗАВЕРШЕНА =====")


async def post_shutdown(application: Application) -> None:
    """Выполняется при остановке бота для корректного освобождения ресурсов."""
    logging.info("===== НАЧАЛО КОРРЕКТНОГО ЗАВЕРШЕНИЯ РАБОТЫ БОТА (post_shutdown) =====")

    # Остановка API сервера если запущен
    logging.info("Шаг 1/3: Остановка API сервера...")
    api_process = application.bot_data.get('api_server_process')
    if api_process and api_process.poll() is None:
        try:
            api_process.terminate()
            api_process.wait(timeout=5)
            logging.info(f"Шаг 1/3: API сервер (PID: {api_process.pid}) успешно остановлен.")
        except Exception as e:
            logging.warning(f"Шаг 1/3: Ошибка остановки API сервера: {e}")
            try:
                api_process.kill()
            except:
                pass
    else:
        logging.info("Шаг 1/3: API сервер не был запущен.")

    # Остановка воркера очереди анализатора дыр (до закрытия пула БД).
    loophole_worker = application.bot_data.get('loophole_worker')
    if loophole_worker is not None:
        try:
            loophole_worker.stop()
            logging.info("Воркер очереди анализатора дыр остановлен.")
        except Exception as e:
            logging.warning(f"Ошибка остановки воркера очереди: {e}")

    logging.info("Шаг 2/3: Закрытие пула процессов...")
    if 'process_pool' in application.bot_data:
        application.bot_data['process_pool'].shutdown(wait=True)
        logging.info("Шаг 2/3: Пул процессов успешно закрыт.")

    logging.info("Шаг 3/3: Закрытие пула соединений с БД...")
    await db_manager.close()
    logging.info("Шаг 3/3: Пул соединений с БД успешно закрыт.")

    logging.info("===== КОРРЕКТНОЕ ЗАВЕРШЕНИЕ РАБОТЫ БОТА ЗАВЕРШЕНО =====")


def main() -> None:
    """Основная функция для запуска бота."""
    # --- >>> НАЧАЛО ИСПРАВЛЕНИЯ <<< ---
    # Эта строка критически важна для корректной работы multiprocessing в Windows.
    multiprocessing.freeze_support()
    # --- >>> КОНЕЦ ИСПРАВЛЕНИЯ <<< ---

    setup_logging()
    logging.info("Запуск main()...")

    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    application.add_error_handler(error_handler)

    # >>> НАЧАЛО ИЗМЕНЕНИЙ: РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ИЗ НОВЫХ МОДУЛЕЙ <<<

    # 1. Команда для проверки статуса rate limiter (только для супер-админа)
    async def check_api_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Быстрая проверка статуса rate limiter."""
        if update.effective_user.id != SUPER_ADMIN_ID:
            return

        from rate_limiter import vertex_rate_limiter
        stats = vertex_rate_limiter.get_stats()

        text = "📊 <b>Vertex AI Rate Limiter Status</b>\n\n"

        # QPM для каждой модели
        current_qpm = stats.get('current_qpm', {})
        qpm_limits = stats.get('qpm_limits', {})

        for model_name, limit in qpm_limits.items():
            current = current_qpm.get(model_name, 0)
            percentage = (current / limit) * 100 if limit > 0 else 0
            emoji = "🟢" if percentage < 50 else "🟡" if percentage < 80 else "🔴"
            text += f"{emoji} <code>{model_name}</code>: {current}/{limit} QPM ({percentage:.0f}%)\n"

        text += f"\n✅ Успешных: {stats['successful_requests']}\n"
        text += f"❌ Ошибок 429: {stats['resource_exhausted_errors']}\n"

        adaptive = stats.get('adaptive_throttling', {})
        if any(adaptive.values()):
            text += "\n⚠️ <b>Adaptive Throttling активен!</b>"

        await update.message.reply_text(text, parse_mode='HTML')

    application.add_handler(CommandHandler("apistatus", check_api_status))

    # 1.5. Мидлвар: любой входящий апдейт = юзер снова доступен → гасим залипший is_blocked
    # (флаг ставится только при Forbidden в рассылке и раньше не сбрасывался, из-за чего
    # рубился доступ к оверлею даже у ВИП). group=-1 → срабатывает раньше основных хендлеров
    # и НЕ потребляет апдейт (проходит дальше в group 0). Тело в try/except внутри функции.
    application.add_handler(TypeHandler(Update, reset_stale_block_flag), group=-1)

    # 2. ConversationHandlers из пакета user_handlers
    # ВАЖНО: ConversationHandler'ы регистрируются первыми, чтобы их fallbacks
    # имели приоритет над общим CommandHandler("start")
    application.add_handler(assistant_conv_handler)
    application.add_handler(criminal_case_conv_handler)  # Criminal Case Writer
    application.add_handler(pch_conv_handler)  # Prosecution Complaint Handler
    application.add_handler(subscription_conv_handler)
    application.add_handler(report_conv_handler)
    application.add_handler(interview_conv_handler)
    application.add_handler(history_conv_handler)
    application.add_handler(complaint_conv_handler)
    application.add_handler(test_loophole_conv_handler)  # анализатор дыр законки (кнопка меню + /test)
    application.add_handler(tester_application_conv_handler)  # заявки в тестеры (?start=apply_tester)
    application.add_handler(tester_feedback_conv_handler)  # канал обратной связи тестеров (баги/идеи → репорт)
    application.add_handler(vip_application_conv_handler)  # заявки на ВИП за промокод (кнопка меню + ?start=apply_vip)
    application.add_handler(user_conv_handler)  # <-- Основной RAG-диалог, должен быть одним из последних

    # 3. ConversationHandler из admin_handlers
    application.add_handler(admin_conv_handler)

    # 3.0. Одобрение/отклонение заявок на ВИП — TOP-LEVEL (кнопки в канале заявок, вне любого диалога).
    # Регистрируем ПОСЛЕ admin_conv: если админ внутри диалога, conv не съедает vip_ok_/vip_no_
    # (нет таких паттернов в его состояниях) → отрабатывает этот хендлер. Гейт на админа — внутри.
    application.add_handler(CallbackQueryHandler(accept_vip_application, pattern=r'^vip_ok_\d+$'))
    application.add_handler(CallbackQueryHandler(reject_vip_application, pattern=r'^vip_no_\d+$'))
    # «На доработку»: кнопка + захват ответа админа (узкий фильтр по сентинелу — посторонние реплаи не трогает).
    application.add_handler(CallbackQueryHandler(request_vip_info, pattern=r'^vip_info_\d+$'))
    application.add_handler(MessageHandler(vip_rev_reply_filter & filters.TEXT & ~filters.COMMAND, handle_vip_revision_reply))

    # 3.0.2. Закрытие обращений поддержки (сайт) — TOP-LEVEL (кнопка в личной карточке, вне диалога).
    application.add_handler(CallbackQueryHandler(close_support_ticket, pattern=r'^supp_close_\d+$'))

    # 3.0.1. Кнопки «🔄 Повторить» / «⚡ Отправить более слабой модели» под сообщениями
    # «AI перегружен» (Q&A, иски/жалобы). TOP-LEVEL, вне диалогов: фоновые задачи шлют эти
    # сообщения уже после выхода из ConversationHandler, паттерн air: уникален → conv не съедает.
    application.add_handler(CallbackQueryHandler(handle_ai_retry, pattern=AI_RETRY_PATTERN))

    # 3.1. Глобальный обработчик отмены запущенной рассылки
    # (рассылка работает async через job_queue ПОСЛЕ выхода из ConversationHandler)
    from admin_handlers import cancel_running_broadcast
    application.add_handler(CallbackQueryHandler(cancel_running_broadcast, pattern=r'^bc_cancel_running_\d+$'))

    # 3.2. Глобальный обработчик кнопки обхода защиты БЗ
    # Приходит напрямую (не через ConversationHandler), поэтому регистрируется отдельно
    application.add_handler(CallbackQueryHandler(bypass_kb_protection_handler, pattern=r'^bypass_prot_'))

    # 3.3. Глобальные кнопки пользователя для задач в очереди анализатора дыр
    # (приходят ПОСЛЕ выхода из ConversationHandler)
    from user_handlers.test_loophole_flow import (
        user_loophole_my_position, user_loophole_cancel, overlay_download_handler,
        overlay_get_key_handler, overlay_regen_key_handler, overlay_regen_key_yes_handler,
        overlay_change_pc_handler, overlay_change_pc_yes_handler,
        guide_command, overlay_guide_handler, overlay_guide_tester_handler, overlay_menu_handler,
    )
    application.add_handler(CallbackQueryHandler(user_loophole_my_position, pattern=r'^loophole_mypos_\d+$'))
    application.add_handler(CallbackQueryHandler(user_loophole_cancel, pattern=r'^loophole_mycancel_\d+$'))
    # Кнопка «Скачать оверлей» — бот отдаёт .exe установщик файлом. overlay_download — тестерский
    # вход /test (full тестеру/админу); overlay_download_qa — публичный вход меню (всегда Q&A).
    application.add_handler(CallbackQueryHandler(overlay_download_handler, pattern=r'^overlay_download(_qa)?$'))
    # Лицензионный ключ оверлея: показать/выдать + перевыпуск (с подтверждением).
    application.add_handler(CallbackQueryHandler(overlay_get_key_handler, pattern=r'^overlay_get_key$'))
    application.add_handler(CallbackQueryHandler(overlay_regen_key_handler, pattern=r'^overlay_regen_key$'))
    application.add_handler(CallbackQueryHandler(overlay_regen_key_yes_handler, pattern=r'^overlay_regen_key_yes$'))
    # «🔄 Сменить ПК» — самосброс привязки ключа к компьютеру (бюджет rebind_max), с подтверждением.
    application.add_handler(CallbackQueryHandler(overlay_change_pc_handler, pattern=r'^overlay_change_pc$'))
    application.add_handler(CallbackQueryHandler(overlay_change_pc_yes_handler, pattern=r'^overlay_change_pc_yes$'))
    # /guide + кнопки «📖 Гайд». Публичный (главное меню) → обычный Q&A-гайд; тестерский (/test) →
    # тестерский гайд. Команда /guide шлёт Q&A всем + тестерскую часть тестерам.
    application.add_handler(CommandHandler("guide", guide_command))
    application.add_handler(CallbackQueryHandler(overlay_guide_handler, pattern=r'^overlay_guide$'))
    application.add_handler(CallbackQueryHandler(overlay_guide_tester_handler, pattern=r'^overlay_guide_tester$'))
    # «🎤 Голосовой оверлей» из главного меню (кнопка показывается только админам — софт-запуск).
    application.add_handler(CallbackQueryHandler(overlay_menu_handler, pattern=r'^overlay_menu$'))
    # /invite_testers (пишется В ГРУППЕ тестеров) — бот зовёт всех тестеров по ссылке-приглашению.
    from admin_handlers import invite_testers_command
    application.add_handler(CommandHandler("invite_testers", invite_testers_command))

    # /set_vip_channel (пишется В ГРУППЕ заявок) — назначить этот чат приёмником заявок на ВИП.
    from admin_handlers import set_vip_channel_command
    application.add_handler(CommandHandler("set_vip_channel", set_vip_channel_command))

    # /qa2 — ЭКСПЕРИМЕНТ Q&A v2 (подборка статей + thinking + search_laws). ТОЛЬКО админы
    # (гейт внутри, посторонним молчит), ТОЛЬКО сервер config.QA_V2_SERVER_ID (La Puerta).
    # Основной Q&A-путь НЕ затронут; выкатка на всех — после сравнения ответов v1/v2.
    from user_handlers.qa_v2_flow import qa2_command
    application.add_handler(CommandHandler("qa2", qa2_command))

    # 4. Базовая команда /start (для пользователей ВНЕ активного диалога)
    # ВАЖНО: Регистрируется ПОСЛЕ ConversationHandler'ов, чтобы fallbacks внутри них
    # обрабатывали /start когда пользователь находится в активном диалоге
    application.add_handler(CommandHandler("start", start))

    # 5. Обработчики кнопок верхнего уровня (главное меню, личный кабинет)
    application.add_handler(CallbackQueryHandler(main_menu, pattern='^main_menu$'))
    application.add_handler(CallbackQueryHandler(personal_account, pattern='^personal_account$'))
    application.add_handler(CallbackQueryHandler(prosecutor_assistant_menu, pattern='^prosecutor_assistant_menu$'))

    # 6. Прочие обработчики
    application.add_handler(MessageHandler(filters.FORWARDED, handle_forwarded_message))

    # >>> КОНЕЦ ИЗМЕНЕНИЙ <<<

    logging.info("Бот запускает polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    main()

# END OF FILE main.py
