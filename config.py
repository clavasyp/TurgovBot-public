# START OF FILE config.py

import os
import asyncio
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла
load_dotenv()

# НОВЫЙ ИМПОРТ данных из отдельного файла
from law_sources_data import LAW_SOURCES_DATA, PROJECTS_DATA, get_flat_servers

# --- Базовые пути ---
# Абсолютный путь к директории проекта для надежного разрешения путей к файлам.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_DIR = os.path.join(BASE_DIR, 'credentials')


def _resolve_credentials_path(filename: str) -> str:
    """Ищет credential-файл: сначала в credentials/, потом в корне проекта."""
    cred_path = os.path.join(CREDENTIALS_DIR, filename)
    if os.path.exists(cred_path):
        return cred_path
    return os.path.join(BASE_DIR, filename)


# --- Ключевые учетные данные и идентификаторы ---
# Секреты (токены, пароли) — из .env. GCP-настройки (имена файлов, проект,
# бакет) живут прямо в этом файле: чтобы поменять кред-файл, достаточно
# залить новый .json в credentials/ и заменить имя ниже — без правки .env.
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')

GCP_PROJECT_ID = 'turgovv5'
GCP_REGION = 'global'
VERTEX_CREDENTIALS_FILE = _resolve_credentials_path('turgovv5-94343fd8ddc7.json')
GCS_BUCKET_NAME = 'botlaw2'   # дефолтный бакет (исходный проект)
# Активный бакет может быть переопределён при смене проекта (горячая смена SA + перенос законки).
# Хранится в system_cache('active_gcs_bucket'); грузится на старте обоих процессов, обновляется при свопе.
# ВАЖНО: все GCS-консьюмеры (загрузка/чистка) должны звать active_gcs_bucket(), а не константу —
# иначе функции-чистильщики посчитают URI нового бакета «чужими» и заNULLят их.
_active_gcs_bucket = None


def active_gcs_bucket() -> str:
    return _active_gcs_bucket or GCS_BUCKET_NAME


def set_active_gcs_bucket(name) -> None:
    global _active_gcs_bucket
    _active_gcs_bucket = (str(name).strip() or None) if name else None

SPREADSHEET_ID = os.getenv('SPREADSHEET_ID', '1HxTfgBWm57l2oKyfGNcLeUb9LqKC2_cZZ7ZKk22DZx8')

# --- РАЗДЕЛЕНИЕ КЛЮЧЕЙ GOOGLE ---
# Путь к файлу для Google Sheets
GOOGLE_CREDENTIALS_FILE = _resolve_credentials_path('credentials.json')
# Путь к файлу для Speech-to-Text API
SPEECH_TO_TEXT_CREDENTIALS_FILE = _resolve_credentials_path('turgovv5-94343fd8ddc7.json')

# --- SOCKS5 ПРОКСИ ДЛЯ СКАЧИВАНИЯ ИЗОБРАЖЕНИЙ ---
# Формат: host:port:username:password
IMGUR_SOCKS5_PROXY = os.getenv('IMGUR_SOCKS5_PROXY', '')

# --- HTTP ПРОКСИ ДЛЯ ПАРСИНГА ФОРУМА ---
# Формат: host:port:username:password
FORUM_HTTP_PROXY = os.getenv('FORUM_HTTP_PROXY', '')

# --- УДАЛЁННЫЙ ПАРСЕР ---
PARSER_WORKER_URL = os.getenv('PARSER_WORKER_URL', 'http://parser-worker:8765')
PARSER_SECRET_KEY = os.getenv('PARSER_SECRET_KEY', '')


# --- НАСТРОЙКИ API СЕРВЕРА ---
API_SERVER_HOST = os.getenv('API_SERVER_HOST', '0.0.0.0')
API_SERVER_PORT = int(os.getenv('API_SERVER_PORT', '8000'))
API_VERSION = "v1"           # Версия API
API_DEFAULT_RPM = 60         # Лимит запросов в минуту по умолчанию

# === Device-auth оверлея (вход по тестер-статусу/подписке, БЕЗ секрета на клиенте) ===
# Десктоп-клиент логинится через device-code flow (код подтверждается в Telegram-боте),
# получает короткоживущий access-JWT + ротируемый refresh-токен. Логика — services/device_auth.py.
# Захардкоженного запасного секрета НЕТ: код открытый, и любой известный дефолт позволил бы
# кому угодно подписать валидный device-JWT. Если секрет не задан (локальная разработка, тесты),
# берётся случайный на время жизни процесса: токены не переживут рестарт, а межпроцессные
# вызовы bot→api с производным INTERNAL_API_SECRET не пройдут — это намеренный fail-closed.
# Прод ОБЯЗАН задать DEVICE_JWT_SECRET (или API_JWT_SECRET).
_LEGACY_DEV_SECRET = 'dev-overlay-secret-CHANGE-ME'   # старый публичный дефолт — никогда не принимается
_explicit_device_secret = os.getenv('DEVICE_JWT_SECRET') or os.getenv('API_JWT_SECRET') or ''
DEVICE_JWT_SECRET_EXPLICIT = bool(_explicit_device_secret) and _explicit_device_secret != _LEGACY_DEV_SECRET
if DEVICE_JWT_SECRET_EXPLICIT:
    DEVICE_JWT_SECRET = _explicit_device_secret
else:
    import secrets as _secrets
    DEVICE_JWT_SECRET = _secrets.token_urlsafe(48)
DEVICE_ACCESS_TTL_SECONDS = int(os.getenv('DEVICE_ACCESS_TTL_SECONDS', '900'))        # 15 мин
DEVICE_REFRESH_TTL_SECONDS = int(os.getenv('DEVICE_REFRESH_TTL_SECONDS', '2592000'))  # 30 дней
DEVICE_CODE_TTL_SECONDS = int(os.getenv('DEVICE_CODE_TTL_SECONDS', '600'))            # 10 мин на привязку

# Внутренний вызов bot→api (горячая смена сервис-аккаунта: бот дёргает reload-эндпоинт API,
# чтобы оверлей-процесс тоже переинициализировал genai-клиент). Только внутри docker-сети.
INTERNAL_API_BASE = os.getenv('INTERNAL_API_BASE', 'http://api:8000')
INTERNAL_API_SECRET = os.getenv('INTERNAL_API_SECRET') or DEVICE_JWT_SECRET
DEVICE_POLL_INTERVAL_SECONDS = int(os.getenv('DEVICE_POLL_INTERVAL_SECONDS', '3'))    # интервал поллинга клиентом
DEVICE_SUBSCRIPTION_RECHECK_SECONDS = int(os.getenv('DEVICE_SUBSCRIPTION_RECHECK_SECONDS', '300'))
DEVICE_RPM_LIMIT = int(os.getenv('DEVICE_RPM_LIMIT', '60'))           # лимит запросов на устройство/мин

# === Веб-вход сайта TurgovBot (services/telegram_login.py + api/routers/web_auth.py) ===
# Вход через Telegram Login Widget → серверная HMAC-проверка подписи (по сырому SHA-256 от
# токена бота) → httpOnly cookie с подписанным HS256-JWT сессии. Отдельный секрет, чтобы
# веб-сессии нельзя было подделать/спутать с device-JWT; fallback на DEVICE_JWT_SECRET допустим.
_explicit_web_secret = os.getenv('WEB_SESSION_SECRET') or ''
WEB_SESSION_SECRET = _explicit_web_secret or DEVICE_JWT_SECRET
WEB_SESSION_TTL_SECONDS = int(os.getenv('WEB_SESSION_TTL_SECONDS', '604800'))  # 7 дней
AUTH_DATE_MAX_AGE = int(os.getenv('AUTH_DATE_MAX_AGE', '900'))                 # макс. возраст подписи виджета, 15 мин (анти-replay)
# FAIL-CLOSED: если ни WEB_SESSION_SECRET, ни DEVICE_JWT_SECRET/API_JWT_SECRET явно не заданы
# в окружении — веб-вход ВЫКЛЮЧЕН целиком (случайный секрет процесса не годится: сессии
# разъехались бы между процессами и умирали бы на рестарте).
# Прод ОБЯЗАН задать WEB_SESSION_SECRET (или реальный DEVICE_JWT_SECRET).
WEB_SESSION_SECRET_OK = (
    (bool(_explicit_web_secret) and _explicit_web_secret != _LEGACY_DEV_SECRET)
    or DEVICE_JWT_SECRET_EXPLICIT
)
# Разрешённые Origin для МУТИРУЮЩИХ веб-эндпоинтов (CSRF-хардненинг), список через запятую.
# Пусто = проверка same-origin по Host (за Caddy фронт+API на одном домене). В dev с
# раздельными портами укажи сюда http://localhost:3000.
WEB_ALLOWED_ORIGINS = tuple(o.strip() for o in os.getenv('WEB_ALLOWED_ORIGINS', '').split(',') if o.strip())

# --- Лицензирование по КЛЮЧУ + HWID-привязка (services/license.py) ---
# Бот выдаёт ключ (RPOV-XXXXX-...), юзер активирует в оверлее → привязка к железу (HWID).
# 1 ключ = 1 ПК; сам-сброс привязки LICENSE_REBIND_MAX раз в окне LICENSE_REBIND_WINDOW_DAYS,
# дальше — сброс админом. Бан юзера в боте → бан его HWID (нельзя переактивироваться на том же ПК).
LICENSE_KEY_PREFIX = os.getenv('LICENSE_KEY_PREFIX', 'RPOV')
LICENSE_REBIND_MAX = int(os.getenv('LICENSE_REBIND_MAX', '2'))               # сколько раз юзер сам перепривяжет ключ к новому ПК
LICENSE_REBIND_WINDOW_DAYS = int(os.getenv('LICENSE_REBIND_WINDOW_DAYS', '30'))  # окно кулдауна между ребайндами
LICENSE_CHALLENGE_TTL_SECONDS = int(os.getenv('LICENSE_CHALLENGE_TTL_SECONDS', '120'))  # TTL nonce для signed-challenge
LICENSE_FUZZY_THRESHOLD = float(os.getenv('LICENSE_FUZZY_THRESHOLD', '0.65'))  # доля совпавших HWID-компонентов = «та же машина»

# --- Серверный детект аномалий лицензии (Слой C плана усиления защиты) ---
# Журнал license_events (миграция 013) + детекторы дрейфа HWID / мульти-IP / велосити, алерты супер-админу.
# Решение всегда серверное и градуированное (soft/medium/hard) — клиента не «кирпичим» на одиночном сигнале.
LICENSE_DETECT_ENABLED = os.getenv('LICENSE_DETECT_ENABLED', 'true').lower() in ('1', 'true', 'yes')
# Окно и порог детектора «один pubkey с многих IP» (шеринг ключа / невозможная география).
# ⚠️ Порог калибровать с учётом аудитории: юзеры из РФ ходят через VPN/мобильный CGNAT с ротацией
# выходных IP — 4-6 реальных адресов за сутки набираются ЛЕГИТИМНО (pubkey при этом device-bound
# через DPAPI, т.е. «много IP» само по себе ≈ одна машина, скачущая по сети, а не шеринг).
LICENSE_MULTI_IP_WINDOW_HOURS = int(os.getenv('LICENSE_MULTI_IP_WINDOW_HOURS', '24'))
LICENSE_MULTI_IP_THRESHOLD = int(os.getenv('LICENSE_MULTI_IP_THRESHOLD', '8'))   # distinct IP на pubkey за окно
# Велосити активаций на один ключ (выжигание ребайндов / массовая активация).
LICENSE_VELOCITY_WINDOW_SECONDS = int(os.getenv('LICENSE_VELOCITY_WINDOW_SECONDS', '3600'))
LICENSE_ACTIVATE_VELOCITY_MAX = int(os.getenv('LICENSE_ACTIVATE_VELOCITY_MAX', '5'))  # activate-событий на ключ за окно
# «Много ключей с одной машины» (перепродажа/шеринг): сколько разных ключей на один pubkey терпим
# в скользящем окне. Окно нужно, чтобы легитимная перепокупка (старый ключ истёк → купил новый)
# не давала ВЕЧНЫЙ medium-сигнал на каждом verify. MAX=2: «QA-ключ + платный ключ» в один месяц —
# частый ЛЕГИТИМНЫЙ кейс (тестеры), 3+ ключей с машины — уже подозрительно.
LICENSE_KEYS_PER_MACHINE_MAX = int(os.getenv('LICENSE_KEYS_PER_MACHINE_MAX', '2'))
LICENSE_KEYS_PER_MACHINE_WINDOW_DAYS = int(os.getenv('LICENSE_KEYS_PER_MACHINE_WINDOW_DAYS', '30'))
# Дедуп/тротлинг алертов по (pubkey, набор детекторов, severity). Окна РАЗНЫЕ по severity: детекторы
# типа multi_ip/keys_per_machine — СОСТОЯНИЯ (висят истинными сутками/неделями), а verify дёргается
# клиентом на каждый запуск И каждую переавторизацию (каденс живого клиента = 840с: access-TTL 900с
# минус минута форы рефреша) → окно дедупа КОРОЧЕ 840с = алерт на каждом verify, личку заливало.
# Повторы одного и того же состояния: hard — раз в 6ч, medium/soft — раз в сутки. ЭСКАЛАЦИЯ
# (smaller→больший severity) шлётся мгновенно всегда — severity входит в ключ дедупа.
LICENSE_ALERT_DEDUP_SECONDS = int(os.getenv('LICENSE_ALERT_DEDUP_SECONDS', '21600'))        # hard
LICENSE_ALERT_DEDUP_SOFT_SECONDS = int(os.getenv('LICENSE_ALERT_DEDUP_SOFT_SECONDS', '86400'))
LICENSE_ALERT_DEDUP_MEDIUM_SECONDS = int(os.getenv('LICENSE_ALERT_DEDUP_MEDIUM_SECONDS', '86400'))
# ГЛОБАЛЬНЫЙ потолок алертов из НЕаутентифицированной телеметрии (анти-флуд супер-админа): телеметрия с
# подделанным pubkey не должна залить личку (дедуп по pubkey обходится ротацией pubkey).
LICENSE_TELEMETRY_ALERT_MAX_PER_MIN = int(os.getenv('LICENSE_TELEMETRY_ALERT_MAX_PER_MIN', '20'))
# Окно дедупа алертов о КРИТ. сбоях доступа к Vertex (биллинг выключен / мёртвый SA / 403 / API off).
# При биллинг-дауне падает КАЖДЫЙ запрос → без дедупа супер-админа зафлудит. Шлём раз в это окно на
# класс ошибки (см. services/ai_health_alert.py). Дефолт 10 мин.
AI_OUTAGE_ALERT_DEDUP_SECONDS = int(os.getenv('AI_OUTAGE_ALERT_DEDUP_SECONDS', '600'))
# IP релей-сервера (РФ-обход): через него ходит ВСЯ когорта заблокированных юзеров, и после перехода
# на CF-Connecting-IP они все видны как ОДИН ip (CF перезаписывает заголовок на IP подключившегося —
# релея). Чтобы общий rate-limit-бакет не душил когорту 429-ми (verify обязателен на каждом запуске,
# волна рестартов при блокирующем обновлении синхронизирует всех), лимит для этих IP умножается.
# Адреса задаются только через .env (список через запятую, IPv4/IPv6): в открытом коде их не держим.
OVERLAY_RELAY_IPS = frozenset(
    ip.strip() for ip in os.getenv('OVERLAY_RELAY_IPS', '').split(',') if ip.strip()
)
OVERLAY_RELAY_RPM_MULT = int(os.getenv('OVERLAY_RELAY_RPM_MULT', '10'))
# Второй приёмник алертов (приватный лог-канал) — chat_id, опционально. 0/пусто = только личка супер-админа.
SECURITY_LOG_CHANNEL_ID = int(os.getenv('SECURITY_LOG_CHANNEL_ID', '0') or '0')
# Канал/группа для заявок на ВИП за промокод (туда падают пруфы + карточка с кнопками Одобрить/Отклонить).
# Бота добавить туда админом. 0/пусто = заявки идут в личку супер-админа (фолбэк).
VIP_APPLICATIONS_CHAT_ID = int(os.getenv('VIP_APPLICATIONS_CHAT_ID', '0') or '0')
# Чат/группа для обращений в поддержку с САЙТА (тикеты /v1/web/support). 0/пусто = личка супер-админа.
WEB_SUPPORT_CHAT_ID = int(os.getenv('WEB_SUPPORT_CHAT_ID', '0') or '0')
# Авто-бан-каскад на сигнале HARD (дрейф+мульти-IP+integrity вместе). ПО УМОЛЧАНИЮ ВЫКЛ —
# сперва наблюдаем точность алертов, включаем осознанно (иначе риск «кирпичить» легит по эвристике).
LICENSE_AUTO_BAN_ENABLED = os.getenv('LICENSE_AUTO_BAN_ENABLED', 'false').lower() in ('1', 'true', 'yes')

# --- Удалённый кил-свитч (Слой F): pending-очередь подписанных пер-устройство команд ---
# Команды подписываются ОФЛАЙН-ключом релиза (того же, что манифесты апдейтов; приватного на проде НЕТ).
# Сервер верифицирует подпись ЭТИМ публичным ключом перед постановкой в очередь (отсев мусора) и просто
# ретранслирует команду клиенту через verify(). Публичный ключ = те же 32 байта, что вшиты в клиент
# (src/update_pubkey.h RPOVERLAY_UPDATE_PUBKEY).
OVERLAY_UPDATE_PUBKEY_HEX = os.getenv(
    'OVERLAY_UPDATE_PUBKEY_HEX',
    'c18cfcf64de9e129178f51b5a22d383bde7ef5e64d46e29e4a859b5bfaf5310e',
)
# Секрет для постановки команды в очередь (POST /v1/license/command/enqueue). Офлайн-подписант шлёт его
# в заголовке. Даже без секрета подделать команду нельзя (нужен приватный ключ), но секрет режет спам.
KILL_SWITCH_INTERNAL_SECRET = os.getenv('KILL_SWITCH_INTERNAL_SECRET') or INTERNAL_API_SECRET

# Поддомен для защищённого канала wss/https (терминируется Caddy). Напр. 'api.example.com'.
OVERLAY_DOMAIN = os.getenv('OVERLAY_DOMAIN', '')
# Публичный адрес раздачи апдейтов оверлея (Caddy /updates/) — для команды /test (скачать клиент).
OVERLAY_PUBLIC_URL = os.getenv('OVERLAY_PUBLIC_URL', 'https://overlay.turgovbot.ru')
# Фолбэк-версия установщика, если live-манифест /updates/manifest.json недоступен.
OVERLAY_LATEST_VERSION = os.getenv('OVERLAY_LATEST_VERSION', '0.1.3')
# Локальный каталог раздачи апдейтов (том ./updates смонтирован в контейнер бота — см.
# docker-compose). Бот читает manifest.json и .exe ОТСЮДА (без сети/Cloudflare → всегда
# актуальная версия, без таймаутов). Фолбэк на HTTP, если файла нет.
OVERLAY_UPDATES_DIR = os.getenv('OVERLAY_UPDATES_DIR', '/app/updates')

# --- Формирование DATABASE_URL из отдельных компонентов (единая точка) ---
_db_user = os.getenv('DB_USER')
_db_pass = os.getenv('DB_PASS')
_db_host = os.getenv('DB_HOST', 'localhost')
_db_port = os.getenv('DB_PORT', '5432')
_db_name = os.getenv('DB_NAME', 'bot_database')
DATABASE_URL = f"postgres://{_db_user}:{_db_pass}@{_db_host}:{_db_port}/{_db_name}"
DATABASE_URL_SYNC = f"postgresql+psycopg2://{_db_user}:{_db_pass}@{_db_host}:{_db_port}/{_db_name}"


# >>> НАЧАЛО ИЗМЕНЕНИЙ: БЛОК НАСТРОЕК МОДЕЛЕЙ AI <<<
# --- ОБНОВЛЕННЫЙ БЛОК: НАСТРОЙКИ МОДЕЛЕЙ AI ---

# --- Блок для юридических консультаций (основной RAG) ---
# Модель для маршрутизации (выбора релевантных законов).
ROUTER_MODEL_NAME = 'gemini-3.1-flash-lite'
# Модель для оптимизации вопросов (используем flash вместо pro для снижения нагрузки).
QUESTION_OPTIMIZER_MODEL_NAME = 'gemini-3-flash-preview'
# Основная модель для ответов на юридические вопросы.
LEGAL_CONSULTANT_MODEL_NAME = 'gemini-3-flash-preview'

# Запасная «слабая» модель для РУЧНОГО разового обхода перегруза.
# Когда основная модель упирается в 429, под сообщением «AI перегружен» юзеру
# показывается кнопка «Отправить более слабой модели» — она РАЗОВО шлёт тот же
# запрос сюда. У flash-lite своя бОльшая Vertex-квота (60 QPM против голодной
# preview-квоты flash), поэтому почти всегда свободна. Основная модель этим НЕ
# меняется — это одноразовый ручной fallback по кнопке.
WEAK_FALLBACK_MODEL_NAME = os.getenv('WEAK_FALLBACK_MODEL_NAME', 'gemini-3.1-flash-lite')

# --- Блок для Ролевого Помощника ---
# НОВАЯ МОДЕЛЬ: Интеллектуальный фильтр законов для Ассистента.
ASSISTANT_ROUTER_MODEL_NAME = 'gemini-3.1-flash-lite'
# Основная модель для ведения диалогов в "Ролевом Помощнике".
# Требует максимальной производительности и большого контекстного окна.
ROLE_ASSISTANT_MODEL_NAME = 'gemini-3-flash-preview'
# Потолок ответа. Без него длинный разбор упирался в дефолтный лимит вывода и обрывался
# на MAX_TOKENS: модель возвращала пустой text, а пользователь не получал вообще ничего.
ASSISTANT_MAX_OUTPUT_TOKENS = int(os.getenv('ASSISTANT_MAX_OUTPUT_TOKENS', '8192'))
# thinking_level: minimal/low/medium/high; ПУСТО = не слать (аварийный откат, как в QA2).
# У gemini-3-flash-preview дефолт HIGH — на деле в сотни тысяч токенов это давало
# минуты латентности. Ставим явно medium: качество разбора держится, время предсказуемо.
ASSISTANT_THINKING_LEVEL = os.getenv('ASSISTANT_THINKING_LEVEL', 'medium')
# Потолок истории дела, передаваемой модели (символы). Дела доходят до 432 000 символов
# (135 сообщений) — вся история переотправлялась на каждом ходу, что било и по цене, и по
# качеству («потеря середины»). Первое сообщение (фабула) сохраняется всегда, дальше берётся
# хвост диалога в пределах лимита. 0 = без ограничения (прежнее поведение).
ASSISTANT_HISTORY_MAX_CHARS = int(os.getenv('ASSISTANT_HISTORY_MAX_CHARS', '120000'))
# Как подавать законы помощнику и генератору жалоб:
#   'articles'  — подборка конкретных статей (каталог + FRIDA, тот же путь, что у Q&A v2);
#   'documents' — прежний режим: роутер по заголовкам + целые кодексы через File API.
# Каталог статей есть не у всех серверов (его достраивают), поэтому режим 'articles'
# сам откатывается на 'documents', если подборка пустая — миграция безопасна.
ASSISTANT_LAW_CONTEXT_MODE = os.getenv('ASSISTANT_LAW_CONTEXT_MODE', 'articles').strip().lower()
# Потолок подборки статей в символах. У Q&A v2 это 100k на короткий вопрос; здесь запрос
# делит контекст с историей дела, поэтому берём меньше.
ASSISTANT_LAW_CONTEXT_MAX_CHARS = int(os.getenv('ASSISTANT_LAW_CONTEXT_MAX_CHARS', '60000'))
# Карта дела: вместо того чтобы просто выбросить середину переписки по лимиту выше,
# старая часть дела сжимается в структурированную сводку (стороны, эпизоды, доказательства,
# статьи, договорённости) и переносится дальше вместе со свежим хвостом диалога.
# false = прежнее поведение (грубая обрезка с маркером «N сообщений опущено»).
ASSISTANT_DIGEST_ENABLED = os.getenv('ASSISTANT_DIGEST_ENABLED', 'true').strip().lower() in ('1', 'true', 'yes')
# Модель для сжатия. Работа механическая (пересказ без выводов), поэтому дешёвая lite-модель;
# карта строится редко — только когда дело перерастает лимит истории.
ASSISTANT_DIGEST_MODEL_NAME = os.getenv('ASSISTANT_DIGEST_MODEL_NAME', 'gemini-3.1-flash-lite')
# Сколько символов резервируется под карту в бюджете истории (и потолок её длины).
ASSISTANT_DIGEST_MAX_CHARS = int(os.getenv('ASSISTANT_DIGEST_MAX_CHARS', '12000'))
# Потолок исходника для одного сжатия. Больше этого объёма за раз не отправляем: карта
# обновляется инкрементально, так что остаток попадёт в неё следующим ходом.
ASSISTANT_DIGEST_SOURCE_MAX_CHARS = int(os.getenv('ASSISTANT_DIGEST_SOURCE_MAX_CHARS', '250000'))
# Сколько таких порций сжимать за один ход пользователя. Два прохода перекрывают самое
# большое дело на проде (432 000 символов), а больше — это уже минуты ожидания на одном ходу.
ASSISTANT_DIGEST_MAX_PASSES = int(os.getenv('ASSISTANT_DIGEST_MAX_PASSES', '2'))

# --- Блок для Судебного Помощника ---
# Основная модель для судебного помощника
COURT_ASSISTANT_MODEL_NAME = 'gemini-3-flash-preview'
# Модель для OCR и анализа изображений (постановления судей)
COURT_OCR_MODEL_NAME = 'gemini-3.1-flash-lite'
# Роутер законов для судьи
COURT_ROUTER_MODEL_NAME = 'gemini-3.1-flash-lite'

# --- Блок для системы прецедентов ---
# Модель для генерации краткого описания прецедентов (быстрая и дешёвая)
PRECEDENT_SUMMARY_MODEL_NAME = 'gemini-3.1-flash-lite'
# Роутер для фильтрации релевантных прецедентов (используем общий роутер)
PRECEDENT_ROUTER_MODEL_NAME = ROUTER_MODEL_NAME

# --- Блок для прочих функций ---
# Модель для генерации вопросов для собеседований.
QUESTION_GENERATOR_MODEL_NAME = 'gemini-3-flash-preview'


# --- Q&A v2 (ЭКСПЕРИМЕНТ): админская команда /qa2, ТОЛЬКО сервер QA_V2_SERVER_ID ---
# Новый пайплайн юрконсультанта: контекст = ПОДБОРКА СТАТЕЙ (каталожный поиск law_articles
# + векторный RAG law_chunks, union без порогов — полнота важнее ранжирования) вместо
# целых кодексов файлами; модель нового поколения с ЯВНЫМ thinking; инструмент search_laws —
# модель сама дозапрашивает недостающие нормы юридически точной формулировкой.
# Основной пользовательский путь НЕ затронут. Выкатка на всех — после сравнения ответов.
QA_V2_SERVER_ID = os.getenv('QA_V2_SERVER_ID', 'la_puerta')
QA_V2_MODEL_NAME = os.getenv('QA_V2_MODEL_NAME', 'gemini-3.5-flash-lite')
# thinking_level: minimal/low/medium/high; ПУСТО = не слать thinking (аварийный откат, как
# LOOPHOLE_THINKING_ENABLED). Было medium; поднято до high по итогам сверки 19 кейсов с
# законкой: на medium модель «не дочитывает» длинный контекст (кейс r07 — v1 на high дала
# верный вывод, v2 на medium выдумала содержание статьи; trap-gap пропускал пункты в
# процитированных перечнях). Сам по себе high проблему не закрыл: позднее v2c нашёл
# п. «д» SANG, но пропустил п. «е» о побеге из-под стражи в других ведомственных нормах.
QA_V2_THINKING_LEVEL = os.getenv('QA_V2_THINKING_LEVEL', 'high')
# Потолок подборки в символах (~«100 статей»). Режет только экстремально широкие запросы;
# важен не порядок статей, а чтобы нужная ПОПАЛА в подборку — мусор модель отбросит сама.
QA_V2_CONTEXT_MAX_CHARS = int(os.getenv('QA_V2_CONTEXT_MAX_CHARS', '100000'))
QA_V2_SEARCH_LIMIT = int(os.getenv('QA_V2_SEARCH_LIMIT', '40'))    # каталожная нога (law_articles)
QA_V2_VECTOR_TOP_K = int(os.getenv('QA_V2_VECTOR_TOP_K', '24'))    # семян векторной ноги (law_chunks)
# Цикл дозапроса search_laws: потолок раундов задан В КОДЕ (промпту не доверяем),
# уже переданные статьи в дозапрос не возвращаются (дедуп), «пустой» дозапрос честно
# сообщается модели, чтобы она отвечала по имеющемуся, а не долбила поиск.
QA_V2_TOOL_MAX_ROUNDS = int(os.getenv('QA_V2_TOOL_MAX_ROUNDS', '3'))
QA_V2_TOOL_SEARCH_LIMIT = int(os.getenv('QA_V2_TOOL_SEARCH_LIMIT', '20'))
# Добор СОСЕДНИХ статей документа (аналог «взять главу»): для топ-N каталожных находок
# подтягиваются ±RADIUS статей того же документа по порядку. Чинит пробелы ретривала вида
# «вопрос нашёл ст. 17 и 20 ПК, а решающая ст. 16 рядом — но лексически далека от запроса»
# (кейс r07 сверки). 0 = выключено. Соседи идут в конец подборки (под нож потолка первыми).
QA_V2_NEIGHBOR_RADIUS = int(os.getenv('QA_V2_NEIGHBOR_RADIUS', '2'))
QA_V2_NEIGHBOR_TOP = int(os.getenv('QA_V2_NEIGHBOR_TOP', '12'))


# --- Блок для анализатора дыр, абузов и противоречий в законке (TEST FEATURE) ---
# Доступен только тестерам и админам через команду /test.
# Профили моделей: fast / balanced / deep / expert.
# Не хардкодить эти имена в бизнес-логике — использовать get_loophole_models() из services.law_loophole.model_policy.
_LOOPHOLE_FLASH_LITE = 'gemini-3.1-flash-lite'
_LOOPHOLE_FLASH = 'gemini-3-flash-preview'
# «Модель глубокого тира» (deep/expert analyzer+synthesis+chain+red-team).
# Было 'gemini-3.1-pro-preview'; заменено на GA 'gemini-3.5-flash' (решение заказчика):
# с thinking_level="high" (Этап 3) обходит 3.1 Pro на агентных задачах, дешевле и
# с нормальной прод-квотой. ⚠️ у 3.5-flash дефолтный thinking=MEDIUM → на тяжёлых
# стадиях ОБЯЗАТЕЛЬНО ставить thinking_level="high" явно (иначе анализ тихо просядет).
# Имя _LOOPHOLE_PRO оставлено историческим, чтобы не трогать ссылки deep/expert.
_LOOPHOLE_PRO = 'gemini-3.5-flash'

# fast
LOOPHOLE_ROUTER_MODEL_FAST = _LOOPHOLE_FLASH_LITE
LOOPHOLE_ANALYZER_MODEL_FAST = _LOOPHOLE_FLASH
LOOPHOLE_SYNTHESIS_MODEL_FAST = _LOOPHOLE_FLASH

# balanced
LOOPHOLE_ROUTER_MODEL_BALANCED = _LOOPHOLE_FLASH_LITE
LOOPHOLE_ANALYZER_MODEL_BALANCED = _LOOPHOLE_FLASH
LOOPHOLE_SYNTHESIS_MODEL_BALANCED = _LOOPHOLE_FLASH

# deep
LOOPHOLE_ROUTER_MODEL_DEEP = _LOOPHOLE_FLASH_LITE
LOOPHOLE_ANALYZER_MODEL_DEEP = _LOOPHOLE_PRO
LOOPHOLE_SYNTHESIS_MODEL_DEEP = _LOOPHOLE_PRO

# expert
LOOPHOLE_ROUTER_MODEL_EXPERT = _LOOPHOLE_FLASH
LOOPHOLE_ANALYZER_MODEL_EXPERT = _LOOPHOLE_PRO
LOOPHOLE_SYNTHESIS_MODEL_EXPERT = _LOOPHOLE_PRO

# --- thinking_level по стадиям анализатора (Gemini-3, google-genai>=1.51) ---
# '' / None = не задавать thinking (модель на своём дефолте).
# ⚠️ у gemini-3.5-flash дефолтный thinking = MEDIUM → на тяжёлых стадиях нужен
# 'high' явно, иначе анализ тихо просядет (предупреждение Google migration-гайда).
# LOOPHOLE_THINKING_ENABLED — глобальный выключатель: False = не слать thinking
# нигде (аварийный откат, если модель отвергнет thinking_level с 400, БЕЗ правки кода).
LOOPHOLE_THINKING_ENABLED = True
LOOPHOLE_THINKING_ROUTER = 'low'         # flash-lite; СОЗНАТЕЛЬНО не подключён в queue.py-
                                         # роутер: он upfront → 400 на thinking сломал бы весь
                                         # анализ. Подключить после живого смоука (flash-lite принимает thinking_level).
LOOPHOLE_THINKING_ANALYZER = 'high'      # single-doc + cross-doc (глубокий поиск схем)
LOOPHOLE_THINKING_EXTRACTION = 'medium'  # norm extraction (Этап 4)
LOOPHOLE_THINKING_SYNTHESIS = 'high'     # склейка цепочек (Этап 9)
LOOPHOLE_THINKING_ROLES = 'high'         # goal-directed roles (Этап 6)
LOOPHOLE_THINKING_CHAIN = 'high'         # exploit-chain builder (Этап 7)
LOOPHOLE_THINKING_REDTEAM = 'high'       # red-team (Этап 8)

# Профили, на которых включён ПОЛНЫЙ конвейер v2 (S1 norm-extraction → goal-roles →
# exploit-chain → red-team → LLM-синтез). Пусто = выключено везде (все профили работают
# как раньше: single + cross + python-дедуп). Этап 11 включит ('deep', 'expert').
# fast/balanced остаются «лёгкими» (только улучшенные CORE-промпты, без доп. стадий).
LOOPHOLE_FULL_PIPELINE_PROFILES = ('deep', 'expert')

# Лимиты выбора документов (manual_select / hybrid)
LOOPHOLE_MAX_MANUAL_DOCS_FAST = 5
LOOPHOLE_MAX_MANUAL_DOCS_BALANCED = 10
LOOPHOLE_MAX_MANUAL_DOCS_DEEP = 20
LOOPHOLE_MAX_MANUAL_DOCS_EXPERT = 40

# Примерный лимит размера контекста (в символах) на один проход анализа.
# При превышении включается batch-пайплайн.
LOOPHOLE_BATCH_THRESHOLD_CHARS = 200_000

# --- Очередь анализатора дыр в законке ---
# Сколько анализов основная очередь выполняет ОДНОВРЕМЕННО (без учёта
# админских out-of-band запусков, которые идут параллельно).
LOOPHOLE_QUEUE_CONCURRENCY = 1
# Период фолбэк-поллинга воркера (сек), если событие пробуждения было потеряно.
LOOPHOLE_QUEUE_POLL_INTERVAL = 5.0
# Приоритеты в очереди: больше число = выше в очереди.
LOOPHOLE_PRIORITY_SUBSCRIBER = 0
LOOPHOLE_PRIORITY_VIP = 10
LOOPHOLE_PRIORITY_ADMIN = 20
# Максимум попыток выполнения одной задачи (защита от «ядовитой» задачи,
# зацикливающей воркер после рестартов).
LOOPHOLE_MAX_JOB_ATTEMPTS = 2
# Макс. одновременных модельных вызовов в pass1 анализатора (per-document).
# Раньше gather запускал ВСЕ документы разом (33–39) → шквал 429 RESOURCE_EXHAUSTED от
# Vertex и тихий ложный «0 дыр». Ограничение даёт rate_limiter'у пейсить под QPM модели.
LOOPHOLE_PASS1_CONCURRENCY = 3
# Пауза воркера после исчерпания квоты (429), сек — чтобы не спамить Vertex и дать квоте
# освободиться перед повтором. Задача при этом МОЛЧА остаётся в очереди (юзеру не пишем).
LOOPHOLE_QUOTA_COOLDOWN_SEC = 60
# ВРЕМЕННЫЙ фолбэк pro→flash. Если pro-задание (expert/deep) висит в системе дольше этого
# порога (pro-модель не пробивается сквозь 429), его анализ выполняется на FALLBACK-модели,
# чтобы пользователь получил результат, а не ждал бесконечно. 0/None = фолбэк выключен.
LOOPHOLE_PRO_FALLBACK_AFTER_SEC = 1800            # 30 минут
LOOPHOLE_PRO_FALLBACK_MODEL = _LOOPHOLE_FLASH     # 'gemini-3-flash-preview' (умнее flash-lite)
# Порог «много дыр»: если найдено БОЛЬШЕ — отчёт отдаётся оформленным HTML-документом
# (удобнее кучи сообщений), иначе (<= порога, в т.ч. 0) — обычными Telegram-сообщениями.
LOOPHOLE_DOC_THRESHOLD = 3

# --- Семантический ретривал (RAG) для голосового ассистента ---
# Эмбеддинг-модель Google (лидер мультиязычного MTEB, хорошо держит русский +
# cross-lingual «разговорный запрос → формальная норма»).
# gemini-embedding-001 @ 3072 (GA): полная размерность 3072 (как у -2), но БАТЧ (100/вызов) +
# нормальная квота → индексация за ~1ч вместо суток. Квота preview -2-preview = ~10-12 req/min
# (per-base-model, не поднимается) сделала массовую индексацию на -2 непрактичной (~28-55ч).
# Путь -001 — SDK embed_content (батч + rate_limiter + нативный task_type). Ложится в ТОТ ЖЕ
# halfvec(3072)-столбец (миграция не нужна). Чтобы вернуть -2 — переэмбеддить корпус заново
# (пространства -001 и -2 несовместимы). Код -2 (raw :embedContent + AIMD-пейсер) сохранён в
# services/embeddings.py под флагом _is_v2_model() — оживёт, если вернуть имя на gemini-embedding-2*.
EMBEDDING_MODEL_NAME = 'gemini-embedding-001'
EMBEDDING_DIMENSIONS = 3072           # полная размерность (как у -2; без MRL-усечения → норма не нужна)
EMBEDDING_PG_TYPE = 'halfvec'         # тип столбца pgvector: halfvec (HNSW до 4000 dims) — для 3072 обычный vector (≤2000) не годится
EMBEDDING_REGION = 'us-central1'      # -2-preview доступна ТОЛЬКО здесь (global/прочие = 404)
EMBEDDING_CONCURRENCY = 1             # -2-preview: строго последовательно (чистая атрибуция 429 для AIMD-пейсера)
EMBEDDING_PACE_START_SEC = 6.5        # стартовый интервал адаптивного пейсера -2 (квота ~10-12/min в пик)
EMBEDDING_PACE_MIN_SEC = 0.5          # пол интервала: ночью лимит расширяется → разгон до latency-bound (~40/min)
EMBEDDING_PACE_MAX_SEC = 30.0         # потолок при устойчивом 429
EMBEDDING_BATCH_SIZE = 100            # размер батча ТОЛЬКО для SDK-фолбэка -001 (у -2 не используется)
RETRIEVAL_TOP_K = 8                   # сколько чанков отдавать в контекст совета
RAG_CHUNK_MAX_CHARS = 1200            # макс. размер чанка закона (символов)
RAG_CHUNK_OVERLAP_CHARS = 150         # перекрытие при дроблении крупных блоков
# Kill-switch авто-переиндексации law_chunks в цикле обновления БЗ (только изменённые доки,
# только серверы с уже построенным RAG-индексом). off/false в env → как раньше, вручную.
LAW_INDEX_AUTO_REINDEX = os.getenv('LAW_INDEX_AUTO_REINDEX', 'true').strip().lower() in ('1', 'true', 'yes', 'on')

# --- Каталог статей для умного поиска по законке (law_articles, миграция 017) ---
# Офлайн-слой: LLM только при индексации (структура наказаний + синонимы), рантайм — чистый SQL.
LAW_CATALOG_AUTO_SYNC = os.getenv('LAW_CATALOG_AUTO_SYNC', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
LAW_CATALOG_AUTO_ENRICH = os.getenv('LAW_CATALOG_AUTO_ENRICH', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
LAW_CATALOG_ENRICH_MODEL = os.getenv('LAW_CATALOG_ENRICH_MODEL', 'gemini-3.5-flash')  # GA-модель (как loophole) — не жжёт preview-квоту
LAW_ENRICH_BATCH_ARTICLES = int(os.getenv('LAW_ENRICH_BATCH_ARTICLES', '8'))   # статей на один LLM-запрос обогащения
LAW_ENRICH_INTER_BATCH_DELAY_SECONDS = float(
    os.getenv('LAW_ENRICH_INTER_BATCH_DELAY_SECONDS', '0.2')
)  # отдельному долгому backfill задавать 2-4с, чтобы оставить квоту горячему API
LAW_RELATED_TOP_N = int(os.getenv('LAW_RELATED_TOP_N', '5'))                   # похожих статей в «ещё релевантное»
LAW_RELATED_MIN_SCORE = float(os.getenv('LAW_RELATED_MIN_SCORE', '0.55'))      # порог косинусной похожести для related

# --- Локальная модель эмбеддингов ТОЛЬКО для каталога поиска (ai-forever/FRIDA, sentence-transformers/CPU) ---
# ОТДЕЛЬНО от глобальной EMBEDDING_* выше (та обслуживает law_chunks/RAG-советы на Vertex — НЕ трогать).
# Зачем: облачный gemini-embedding-001 давал холодный SA-токен ~11с > таймаут → векторная нога поиска
# мертва в проде. Локальная модель на CPU = вектор жив с ПЕРВОГО запроса, ~50-150мс, без квоты/облака.
# FRIDA — топ-1 ruMTEB по русскому (1536d, CLS-пулинг, префиксы search_query/search_document). Гоняем через
# sentence-transformers (сам делает пулинг/L2/префиксы — нет риска ручного пулинга; цена — torch в образе).
# Пространство несовместимо с -001 → отдельная колонка law_articles.embedding_local vector(1536) (миграция 018),
# разовый пере-эмбеддинг корпуса (scripts/reembed_law_articles.py) — это НЕ обогащение (без LLM/квоты, ~10мин).
# Флаг OFF по умолчанию: Overlay честно работает keyword-only. Облачного fallback нет;
# откат между версиями FRIDA делается через отдельные space_id из миграции 020.
LAW_CATALOG_EMBED_ENABLED = os.getenv('LAW_CATALOG_EMBED_ENABLED', 'false').strip().lower() in ('1', 'true', 'yes', 'on')
LAW_CATALOG_EMBED_MODEL_DIR = os.getenv('LAW_CATALOG_EMBED_MODEL_DIR', '/app/models/frida')  # локальная папка модели (SentenceTransformer)
LAW_CATALOG_EMBED_DIMS = int(os.getenv('LAW_CATALOG_EMBED_DIMS', '1536'))       # FRIDA = 1536; ≤ 2000 → обычный vector
LAW_CATALOG_EMBED_PG_TYPE = os.getenv('LAW_CATALOG_EMBED_PG_TYPE', 'vector')   # 1536 ≤ 2000 → обычный vector, halfvec НЕ нужен
LAW_CATALOG_EMBED_QUERY_PREFIX = os.getenv('LAW_CATALOG_EMBED_QUERY_PREFIX', 'search_query: ')     # FRIDA требует префиксы
LAW_CATALOG_EMBED_DOC_PREFIX = os.getenv('LAW_CATALOG_EMBED_DOC_PREFIX', 'search_document: ')
LAW_CATALOG_EMBED_INTRA_THREADS = int(os.getenv('LAW_CATALOG_EMBED_INTRA_THREADS', '2'))  # torch CPU-потоки — не выедать все 8 ядер под общий api
# Версия всего векторного пространства, а не только имя модели. Менять при ЛЮБОМ изменении
# модели/ревизии/размерности/префиксов/пулинга/нормализации/рецепта текста документа.
# Runtime читает только векторы с этим space_id и включает его в ключ кэша запросов.
LAW_CATALOG_EMBED_SPACE_ID = os.getenv('LAW_CATALOG_EMBED_SPACE_ID', 'frida-v1').strip() or 'frida-v1'
LAW_CATALOG_EMBED_MODEL_NAME = os.getenv('LAW_CATALOG_EMBED_MODEL_NAME', 'ai-forever/FRIDA').strip()
LAW_CATALOG_EMBED_MODEL_REVISION = os.getenv(
    'LAW_CATALOG_EMBED_MODEL_REVISION',
    'sha256:47b1442ef6e7f94b87f9b22a6d66a48b3468a432c1d080f0495d1d533c042084',
).strip()
LAW_CATALOG_EMBED_POOLING = os.getenv('LAW_CATALOG_EMBED_POOLING', 'cls').strip().lower()
LAW_CATALOG_EMBED_NORMALIZED = os.getenv('LAW_CATALOG_EMBED_NORMALIZED', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
LAW_CATALOG_EMBED_RECIPE_VERSION = os.getenv('LAW_CATALOG_EMBED_RECIPE_VERSION', 'law-article-v1').strip()
LAW_CATALOG_EMBED_RUNTIME_VERSIONS = {
    'sentence-transformers': '3.4.1',
    'transformers': '4.57.6',
    'tokenizers': '0.22.2',
    'torch': '2.13.0+cpu',
    'numpy': '2.4.6',
}
# Фоновый API-worker durable-очереди (бот только ставит задания: модели в его контейнере нет).
# В штатном compose consumer живёт в отдельном law-embedding-worker. API всё ещё
# грузит ту же модель для query-векторов, но не должен забирать долгие backfill jobs.
# Явный флаг сохраняет возможность однопроцессного запуска для служебных окружений.
LAW_EMBED_RUN_WORKER = os.getenv('LAW_EMBED_RUN_WORKER', 'true').strip().lower() in ('1', 'true', 'yes', 'on')
LAW_EMBED_JOB_BATCH_SIZE = max(1, int(os.getenv('LAW_EMBED_JOB_BATCH_SIZE', '16')))
LAW_EMBED_JOB_POLL_SECONDS = max(0.1, float(os.getenv('LAW_EMBED_JOB_POLL_SECONDS', '2')))
LAW_EMBED_JOB_STALE_SECONDS = max(30, int(os.getenv('LAW_EMBED_JOB_STALE_SECONDS', '600')))
LAW_EMBED_JOB_MAX_ATTEMPTS = max(1, int(os.getenv('LAW_EMBED_JOB_MAX_ATTEMPTS', '5')))
LAW_EMBED_RECONCILE_SECONDS = max(10.0, float(os.getenv('LAW_EMBED_RECONCILE_SECONDS', '300')))
LAW_EMBED_CLI_DEADLINE_SECONDS = max(1.0, float(os.getenv('LAW_EMBED_CLI_DEADLINE_SECONDS', '1800')))
# Dedicated worker heartbeat/readiness. Инициализация читает и хеширует большой
# model.safetensors, поэтому имеет отдельный щедрый deadline; зависший init после
# него завершает процесс, и restart policy поднимает чистую попытку.
LAW_EMBED_WORKER_INIT_TIMEOUT_SECONDS = max(
    30.0, float(os.getenv('LAW_EMBED_WORKER_INIT_TIMEOUT_SECONDS', '120')),
)
# Hard watchdog одного неделимого torch chunk. Production benchmark: worst-case
# chunk из 4 документов ≈11с; 120с оставляет десятикратный запас, но не позволяет
# heartbeat оставаться зелёным при навсегда зависшем CPU-инференсе.
LAW_EMBED_JOB_CHUNK_TIMEOUT_SECONDS = max(
    30.0, float(os.getenv('LAW_EMBED_JOB_CHUNK_TIMEOUT_SECONDS', '120')),
)
LAW_EMBED_WORKER_HEARTBEAT_SECONDS = max(
    1.0, float(os.getenv('LAW_EMBED_WORKER_HEARTBEAT_SECONDS', '5')),
)
LAW_EMBED_WORKER_HEALTH_MAX_AGE_SECONDS = max(
    LAW_EMBED_WORKER_HEARTBEAT_SECONDS * 3,
    float(os.getenv('LAW_EMBED_WORKER_HEALTH_MAX_AGE_SECONDS', '45')),
)
LAW_EMBED_WORKER_HEALTH_PATH = os.getenv(
    'LAW_EMBED_WORKER_HEALTH_PATH', '/tmp/law-embedding-worker-health.json',
).strip() or '/tmp/law-embedding-worker-health.json'
# Лимит одного SQL-пересчёта графа похожих статей. Он заметно длиннее HTTP-таймаутов,
# но короче lease job: зависший план будет отменён PostgreSQL и уйдёт в durable retry.
LAW_RELATED_REBUILD_TIMEOUT = max(5.0, float(os.getenv('LAW_RELATED_REBUILD_TIMEOUT', '300')))
# Background batch дробится внутри общего priority-gate FRIDA; маленький chunk даёт поисковому
# запросу вклиниться между пачками и уложиться в 1-секундный deadline даже во время backfill.
LAW_EMBED_JOB_INFERENCE_CHUNK_SIZE = max(1, int(os.getenv('LAW_EMBED_JOB_INFERENCE_CHUNK_SIZE', '4')))
# Тюнинг RRF векторной ноги (после включения FRIDA):
# MIN_SIM — отсечка хвоста: соседи с косинусом ниже порога не идут в слияние (FRIDA-релевант ~0.3-0.58,
#   мусорный хвост ниже). BOOST_WEIGHT — вес вектора, когда ТОЧНЫЕ ноги (code/title/synonym_exact) пусты
#   (чистый перефраз, голосует только вектор): иначе rank-1 вектора 1.0/61 задавливается рыхлым fts-соседом.
LAW_VECTOR_MIN_SIM = float(os.getenv('LAW_VECTOR_MIN_SIM', '0.25'))
LAW_VECTOR_BOOST_WEIGHT = float(os.getenv('LAW_VECTOR_BOOST_WEIGHT', '1.8'))

# Рантайм умного поиска (/v1/overlay/laws/*). Облачного embedding-fallback здесь нет:
# недоступна локальная FRIDA — поиск честно продолжает работу в keyword-only режиме.
_LAW_VECTOR_QUERY_MODE = os.getenv('LAW_VECTOR_QUERY_MODE', 'cleaned').strip().lower()
LAW_VECTOR_QUERY_MODE = _LAW_VECTOR_QUERY_MODE if _LAW_VECTOR_QUERY_MODE in ('cleaned', 'original') else 'cleaned'
LAW_SEARCH_TOTAL_TIMEOUT = float(os.getenv('LAW_SEARCH_TOTAL_TIMEOUT', '8.0'))
LAW_SEARCH_SQL_TIMEOUT = float(os.getenv('LAW_SEARCH_SQL_TIMEOUT', '1.5'))
LAW_SEARCH_EMBED_TIMEOUT = float(os.getenv('LAW_SEARCH_EMBED_TIMEOUT', '2.0'))
LAW_SEARCH_CARDS_TIMEOUT = float(os.getenv('LAW_SEARCH_CARDS_TIMEOUT', '1.5'))
LAW_SEARCH_RELATED_TIMEOUT = float(os.getenv('LAW_SEARCH_RELATED_TIMEOUT', '0.75'))
LAW_SEARCH_ARTICLE_TIMEOUT = float(os.getenv('LAW_SEARCH_ARTICLE_TIMEOUT', '2.5'))
# Слот удерживается до ФАКТИЧЕСКОГО завершения CPU-инференса, даже если HTTP-поиск уже получил
# timeout. Так asyncio.to_thread-задачи не накапливаются без границ при перегрузе/зависании модели.
LAW_SEARCH_EMBED_MAX_INFLIGHT = max(1, int(os.getenv('LAW_SEARCH_EMBED_MAX_INFLIGHT', '1')))
# Keep-warm глобального Vertex embedding для RAG/советника. К поиску законов Overlay
# эта настройка больше не относится: там единственный vector backend — локальная FRIDA.
LAW_SEARCH_WARMUP_INTERVAL = float(os.getenv('LAW_SEARCH_WARMUP_INTERVAL', '240'))
LAW_SEARCH_RATE_PER_MIN = int(os.getenv('LAW_SEARCH_RATE_PER_MIN', '60'))       # запросов/мин на пользователя
# Тиры с доступом к поиску (через запятую); пусто = всем. Бета: тестеры/админы + 'laws'
# (универсальный ключ отдельного laws-билда — ему доступен ТОЛЬКО этот поиск).
# ⚠️ если LAW_SEARCH_TIERS переопределён в .env — при выкате laws-билда добавить туда laws.
LAW_SEARCH_TIERS = [t.strip() for t in os.getenv('LAW_SEARCH_TIERS', 'admin,tester,laws').split(',') if t.strip()]

# Best-effort quality telemetry for Overlay law search.  The HTTP hot path only
# performs a bounded Queue.put_nowait(); PostgreSQL writes happen in one batch
# worker and can never make a search fail.
LAW_SEARCH_LOG_ENABLED = os.getenv(
    'LAW_SEARCH_LOG_ENABLED', 'true',
).strip().lower() in ('1', 'true', 'yes', 'on')
LAW_SEARCH_LOG_RETENTION_DAYS = max(
    1, int(os.getenv('LAW_SEARCH_LOG_RETENTION_DAYS', '90')),
)
LAW_SEARCH_LOG_QUEUE_SIZE = max(
    100, int(os.getenv('LAW_SEARCH_LOG_QUEUE_SIZE', '2048')),
)
LAW_SEARCH_LOG_BATCH_SIZE = max(
    1, min(200, int(os.getenv('LAW_SEARCH_LOG_BATCH_SIZE', '50'))),
)
LAW_SEARCH_LOG_FLUSH_SECONDS = max(
    0.05, float(os.getenv('LAW_SEARCH_LOG_FLUSH_SECONDS', '1.0')),
)
LAW_SEARCH_LOG_DB_TIMEOUT = max(
    0.1, float(os.getenv('LAW_SEARCH_LOG_DB_TIMEOUT', '2.0')),
)
LAW_SEARCH_LOG_SHUTDOWN_TIMEOUT = max(
    0.1, float(os.getenv('LAW_SEARCH_LOG_SHUTDOWN_TIMEOUT', '3.0')),
)

# --- Голосовой оверлей: советник (Фаза 2) ---
OVERLAY_ADVICE_MODEL_FAST = os.getenv('OVERLAY_ADVICE_MODEL_FAST', 'gemini-3-flash-preview')   # быстрый совет (стрим); ENV-override для A/B flash↔flash-lite-preview
OVERLAY_ADVICE_MODEL_DEEP = 'gemini-3.1-pro-preview'   # глубокий разбор по хоткею (из оверлея НЕ вызывается)
OVERLAY_ADVICE_MODEL_LITE = os.getenv('OVERLAY_ADVICE_MODEL_LITE', 'gemini-3.1-flash-lite')  # A/B-кандидат flash-lite (60 QPM)
OVERLAY_REWRITE_MODEL = os.getenv('OVERLAY_REWRITE_MODEL', OVERLAY_ADVICE_MODEL_LITE)  # риврайтер ситуации → поисковые запросы перед ретривалом (дефолт = flash-lite; fail-open при сбое)
OVERLAY_RETRIEVAL_TOP_K = 14                           # сколько «семян»-чанков берём (потом догружаем статьи целиком)
OVERLAY_REWRITE_TOPK_PER_QUERY = 6                     # семян на КАЖДЫЙ запрос риврайтера (retrieve_multi: union статей под общий потолок)
OVERLAY_CONTEXT_MAX_CHARS = int(os.getenv('OVERLAY_CONTEXT_MAX_CHARS', '40000'))  # потолок суммарного контекста (полные статьи); 24000→40000: полнота статей при многогранной сцене, latency Gemini от объёма входа зависит слабо
# Верхний дедлайн обработки overlay-запросов (qa/advise/advise_stream). Цель — чтобы ответ
# УСПЕЛ прийти (как в боте), но запрос не висел дольше потолка Cloudflare (~120с → HTTP 524).
# 90с: укладываемся под порог CF и оставляем место под 1-2 ретрая лимитера при 429-контеншене
# с ботом (бот тот же ответ дожимает за ~75-87с). По таймауту — 503, но это крайний случай,
# а не норма. ⚠️ держать СТРОГО ниже реального CF-cut (~120с).
OVERLAY_AI_DEADLINE_SECONDS = int(os.getenv('OVERLAY_AI_DEADLINE_SECONDS', '90') or '90')
OVERLAY_MIN_SIMILARITY = 0.45                          # ниже — низкая уверенность ретривала (логируем/сигналим)

# --- Режим «Обзвон» (лидерская проверка): отвечает по СВОДУ ПРАВИЛ сервера + ИРЛ-праву (знания модели) ---
# Свод правил — отдельный серверо-нейтральный namespace в law_chunks (НЕ игровая законка). Режим
# leadership принудительно ретривит ИМЕННО его, независимо от выбранного на оверлее сервера.
LEADERSHIP_RULES_SERVER_ID = os.getenv('LEADERSHIP_RULES_SERVER_ID', 'majestic_rules')
LEADERSHIP_RULES_DISPLAY_NAME = os.getenv('LEADERSHIP_RULES_DISPLAY_NAME', 'Правила Majestic')
# Разделы форума с правилами (для разового скрейпа в свод — scripts/ingest_rules.py).
LEADERSHIP_RULES_SECTION_URLS = [
    'https://forum.majestic-rp.ru/forums/general-server-rules/',
    'https://forum.majestic-rp.ru/forums/rules-of-criminal-organizations/',
    'https://forum.majestic-rp.ru/forums/rules-of-state-organizations/',
]

# --- Голосовой оверлей: распознавание речи STT (Фаза 3) ---
# Speech-to-Text v2, модель chirp_3 (ru-RU GA в потоке). Требует включённого
# Speech-to-Text API на проекте и роли роли roles/speech.client у сервис-аккаунта.
STT_REGION = 'eu'             # chirp_3 GA-регионы: eu / us (для СНГ ближе eu)
STT_MODEL = 'chirp_3'
STT_LANGUAGE = 'ru-RU'
STT_SAMPLE_RATE = 16000       # PCM 16kHz mono (клиент шлёт LINEAR16)
STT_STREAM_REOPEN_SECONDS = 240  # переоткрывать стрим до 5-мин лимита (endless streaming)


ASSISTANT_LIMIT_RESET_HOURS = 72


# --- Роли и ограничения ---
SUPER_ADMIN_ID = int(os.getenv('SUPER_ADMIN_ID', '0'))  # 0 = суперадмина нет; задаётся в .env
# Дневные лимиты на использование функций для обычных пользователей.
DAILY_QUESTION_LIMIT = 8
DAILY_INTERVIEW_GEN_LIMIT = 1
ASSISTANT_LIMITS = {
    # Формат: 'role_stage': limit
    'advocate_field_work': 4,
    'advocate_case_analysis': 1,
    'prosecutor_field_work': 4,
    'prosecutor_case_analysis': 2,
    'judge_court_case': 3,  # Лимит для судебного помощника
}
# НОВОЕ: Период "остывания" между командами в секундах для защиты от спама.
COMMAND_RATE_LIMIT_SECONDS = 2


# >>> НАЧАЛО ИЗМЕНЕНИЙ: БЛОК ДЛЯ ПОДПИСКИ <<<
TELEGRAM_CHANNEL_URL = "https://t.me/turgovovka"  # Публичная ссылка на канал
# Используем username канала (@turgovovka) - он извлекается автоматически из URL
# Числовой ID оставляем пустым, т.к. username работает надёжнее
TELEGRAM_CHANNEL_ID = None  # Будет использован username из TELEGRAM_CHANNEL_URL

# Количество дополнительных запросов за подписку
SUBSCRIPTION_BONUS_QUESTION = 21
SUBSCRIPTION_BONUS_INTERVIEW_GEN = 4
ASSISTANT_SUBSCRIPTION_BONUSES = {
    'advocate_field_work': 10,
    'advocate_case_analysis': 5,
    'prosecutor_field_work': 12,
    'prosecutor_case_analysis': 10,
    'judge_court_case': 8,  # Бонус для судебного помощника
}


# --- Настройка фоновых задач ---
# Периодичность автоматического обновления базы знаний (в часах).
KNOWLEDGE_BASE_REFRESH_HOURS = 47


# --- НОВАЯ СТРУКТУРА КОНФИГУРАЦИИ СЕРВЕРОВ ---
# Мы присваиваем импортированные данные переменной SERVERS_CONFIG.
# Для всех остальных модулей проекта это будет выглядеть так, как будто ничего не поменялось.
SERVERS_CONFIG = LAW_SOURCES_DATA


def server_display_name(server_id) -> str:
    """Человеко-читаемое имя сервера БЕЗ KeyError. Для server_id из СОХРАНЁННЫХ данных (старые
    дела/жалобы) — там может лежать легаси/орфан/удалённый id (напр. 'Denver' с заглавной, тогда как
    в конфиге 'denver'). Резолв: точное совпадение → case-insensitive → сам server_id как фолбэк."""
    srv = SERVERS_CONFIG.get(server_id)
    if not srv:
        sid = str(server_id).lower()
        srv = next((v for k, v in SERVERS_CONFIG.items() if str(k).lower() == sid), None)
    return srv.get('display_name', str(server_id)) if srv else str(server_id)

# Иерархическая структура проектов (для UI выбора проекта)
PROJECTS_CONFIG = PROJECTS_DATA

ADMIN_LOG_SHEET_NAME = 'Admin_Logs'
ASSISTANT_LOG_SHEET_NAME = 'Assistant_Logs' # <<< НАЗВАНИЕ СТАРОГО ЛОГА (оставим на всякий случай)
DATA_DIR = os.path.join(BASE_DIR, "data")
CACHE_DURATION_SECONDS = 24 * 60 * 60
HISTORY_LIMIT = 8
GENERATION_HISTORY_PREFIX = "gen_req::"

#  Глобальные переменные для асинхронного управления
# >>> НАЧАЛО ИЗМЕНЕНИЙ: ЗАМЕНА ОДИНОЧНОЙ БЛОКИРОВКИ НА СЛОВАРЬ <<<
# Словарь блокировок для каждого сервера, чтобы предотвратить одновременное
# обновление ОДНОГО И ТОГО ЖЕ сервера, но разрешить параллельное обновление РАЗНЫХ.
# Ключ - server_id, значение - asyncio.Lock().
# Используем get_flat_servers() для получения всех серверов из всех проектов
SERVER_UPDATE_LOCKS = {server_id: asyncio.Lock() for server_id in get_flat_servers().keys()}
# >>> КОНЕЦ ИЗМЕНЕНИЙ <<<

# --- БЛОК: НАСТРОЙКИ ГЕНЕРАТОРА ВОПРОСОВ ---
# Словарь должностей. Ключ - для кода, значение - для кнопок. Легко расширять.
INTERVIEW_ROLES = {
    "prosecutor": "Прокурор",
    "advocate": "Адвокат"
}

# Словарь уровней сложности.
INTERVIEW_DIFFICULTIES = {
    "easy": "Простой",
    "medium": "Средний",
    "hard": "Сложный"
}

# Лимиты на количество генерируемых вопросов.
MIN_QUESTIONS = 5
MAX_QUESTIONS = 40

# --- ОБНОВЛЕННЫЙ БЛОК: Состояния для ConversationHandler (FSM) ---
# Определяет шаги диалогов с пользователем и администратором.
# Использование числового диапазона - стандартный подход для python-telegram-bot.
(
    # Пользовательские состояния (основной диалог)
    CHOOSING_PROJECT, CHOOSING_SERVER, CHOOSING_ANSWER_TYPE, AWAITING_QUESTION,
    AWAITING_CLARIFICATION,
    AWAITING_TRANSCRIPTION_CONFIRMATION,
    AWAITING_EDITED_TRANSCRIPTION,

    # Состояние для диалога оптимизации вопроса
    OPTIMIZING_QUESTION,

    AWAITING_OPTIMIZATION_CHOICE,
    AWAITING_OPTIMIZATION_CONFIRMATION,
    AWAITING_EDITED_OPTIMIZED_QUESTION,

    # Состояние для подписки
    CHECKING_SUBSCRIPTION,

    # Админские состояния
    CHOOSING_ACTION,
    MANAGE_BANS,
    MANAGE_VIPS,
    MANAGE_ADMINS,
    MANAGE_SERVERS,

    # Состояния ожидания ввода ID
    GET_ID_FOR_BAN, GET_ID_FOR_UNBAN,
    GET_ID_FOR_VIP_ADD, GET_ID_FOR_VIP_REMOVE,
    GET_ID_FOR_ADMIN_ADD, GET_ID_FOR_ADMIN_REMOVE,

    # Вложенные состояния управления серверами
    CHOOSE_SERVER_TO_TOGGLE,
    CHOOSE_SERVER_FOR_UPDATE,
    CHOOSE_SERVER_FOR_FORCE_UPDATE,

    # Состояния управления кэшем
    CHOOSE_SERVER_FOR_CACHE_REFRESH,
    CHOOSE_SERVER_FOR_CACHE_DELETE,

    # Старое состояние для репорта (будет заменено)
    AWAITING_REPORT_MESSAGE,

    # Состояния для рассылки
    AWAITING_BROADCAST_MESSAGE,
    CONFIRMING_BROADCAST,

    # Состояния для генератора вопросов
    CHOOSING_INTERVIEW_SERVER,
    CHOOSING_INTERVIEW_ROLE,
    CHOOSING_INTERVIEW_DIFFICULTY,
    AWAITING_INTERVIEW_QUANTITY,

    # Состояния для истории запросов
    VIEWING_HISTORY,
    VIEWING_HISTORY_MESSAGE,

    # Состояния для управления лимитами
    MANAGE_LIMITS,
    GET_ID_FOR_LIMIT_RESET,
    CONFIRM_RESET_ALL_LIMITS,

    # >>> НАЧАЛО ИЗМЕНЕНИЙ: НОВЫЕ СОСТОЯНИЯ ДЛЯ СИСТЕМЫ РЕПОРТОВ <<<
    # Пользовательские
    CHOOSING_REPORT_ACTION,      # Меню: "Создать" / "Мои репорты"
    AWAITING_REPORT_TITLE,       # Ожидание первого сообщения для нового репорта
    VIEWING_USER_REPORTS,        # Просмотр списка своих репортов
    AWAITING_USER_REPLY_IN_REPORT, # Ожидание текстового ответа от пользователя в репорте

    # Админские
    ADMIN_MANAGE_REPORTS,          # Меню админа: "Активные" / "Архив"
    ADMIN_VIEWING_REPORTS_LIST,    # Просмотр списка репортов (отфильтрованных)
    ADMIN_CONVERSATION_IN_REPORT,  # Админ в диалоге с пользователем по репорту
    AWAITING_ADMIN_REPLY_IN_REPORT, # Админ вводит текст ответа
    ADMIN_CONFIRM_CLOSE_ALL_REPORTS, # Подтверждение массового закрытия всех активных репортов

    # Управление прецедентами
    ADMIN_MANAGE_PRECEDENTS,         # Меню управления прецедентами
    ADMIN_CHOOSE_SERVER_PRECEDENTS,  # Выбор сервера для просмотра прецедентов
    ADMIN_VIEWING_PRECEDENTS_LIST,   # Просмотр списка прецедентов
    ADMIN_VIEWING_PRECEDENT_DETAIL,  # Просмотр детальной информации о прецеденте
    ADMIN_CHOOSE_SERVER_FORCE_PRECEDENTS,  # Выбор сервера для принудительного обновления прецедентов
    # >>> КОНЕЦ ИЗМЕНЕНИЙ <<<

    # --- >>> НАЧАЛО НОВОГО БЛОКА: СОСТОЯНИЯ ДЛЯ АССИСТЕНТА <<< ---
    CHOOSING_ASSISTANT_ROLE,
    AWAITING_STAGE_SELECTION,
    CHOOSING_ASSISTANT_STAGE,
    AWAITING_CASE_DESCRIPTION,
    ASSISTANT_DIALOGUE,
    VIEWING_ACTIVE_CASES,
    # --- >>> КОНЕЦ НОВОГО БЛОКА <<< ---

    # --- >>> НАЧАЛО: СОСТОЯНИЯ ДЛЯ СУДЕБНОГО ПОМОЩНИКА <<< ---
    AWAITING_COURT_CASE_URL,      # Ожидание URL темы форума
    PARSING_COURT_CASE,           # Процесс парсинга дела
    COURT_CASE_OVERVIEW,          # Показ краткой сводки дела
    COURT_DIALOGUE,               # Основной диалог с судебным помощником
    VIEWING_COURT_CASES,          # Просмотр списка активных судебных дел
    AWAITING_ADDITIONAL_MATERIALS, # Ожидание дополнительных материалов от судьи
    # --- >>> КОНЕЦ: СОСТОЯНИЯ ДЛЯ СУДЕБНОГО ПОМОЩНИКА <<< ---

    # --- >>> НАЧАЛО: СОСТОЯНИЯ ДЛЯ СОСТАВЛЕНИЯ ЖАЛОБ/ИСКОВ <<< ---
    CHOOSING_COMPLAINT_TYPE,       # Выбор типа: жалоба/иск
    CHOOSING_COMPLAINT_SERVER,     # Выбор сервера
    AWAITING_FORUM_URL,            # Ожидание ссылки на форум с формой
    AWAITING_SITUATION_DESCRIPTION, # Ожидание описания ситуации
    GENERATING_COMPLAINT,          # Процесс генерации жалобы
    # --- >>> КОНЕЦ: СОСТОЯНИЯ ДЛЯ СОСТАВЛЕНИЯ ЖАЛОБ/ИСКОВ <<< ---

    # Состояния для новой системы рассылок
    BROADCAST_CHOOSE_AUDIENCE,
    BROADCAST_AWAITING_MESSAGE,
    BROADCAST_ATTACH_BUTTONS,
    BROADCAST_CONFIRM,
    BROADCAST_RUNNING,

    # Состояния для управления тестерами
    MANAGE_TESTERS,
    GET_ID_FOR_TESTER_ADD,
    GET_ID_FOR_TESTER_REMOVE,

    # Состояния для поиска пользователей
    USER_SEARCH_INPUT,
    USER_SEARCH_RESULTS,
    USER_DETAILS_VIEW,
    ADMIN_VIEW_CASE_BY_ID,

    # Состояния для управления API
    MANAGE_API_KEYS,
    GET_API_KEY_NAME,
    GET_API_KEY_TYPE,
    GET_API_KEY_ORIGINS,
    GET_API_KEY_IPS,
    GET_API_KEY_RPM,
    CONFIRM_DELETE_API_KEY,
    VIEW_API_KEY_DETAILS,
    VIEW_API_KEYS_LIST,
    CONFIRM_ROTATE_API_KEY,

    # Состояния для промо-акций
    MANAGE_PROMO,
    GET_PROMO_END_DATE,

    # Состояния для Criminal Case Writer (CCW)
    CCW_CHOOSING_PROJECT,
    CCW_CHOOSING_SERVER,
    CCW_ENTER_CASE_NUMBER,
    CCW_COLLECT_FACTS,
    CCW_PHASE_A,
    CCW_PHASE_B,
    CCW_USER_REVIEW,
    CCW_NAVIGATE,
    CCW_EXPORT,

    # Состояния для Prosecution Complaint Handler (PCH)
    PCH_CHOOSING_PROJECT,
    PCH_CHOOSING_SERVER,
    PCH_AWAITING_URL,
    PCH_AWAITING_FORM,
    PCH_PHASE_A,
    PCH_USER_REVIEW,
    PCH_NAVIGATE,
    PCH_ADD_MATERIAL,
    PCH_EXPORT,

    # Состояния для управления очередью анализатора дыр в законке
    ADMIN_LOOPHOLE_QUEUE,         # список задач очереди
    ADMIN_LOOPHOLE_JOB_DETAIL,    # карточка задачи + действия
    ADMIN_LOOPHOLE_SET_POSITION,  # ввод новой позиции задачи
    ADMIN_LOOPHOLE_RESULTS,       # просмотр архива всех анализов
    ADMIN_SA_UPLOAD,              # ожидание .json сервис-аккаунта (суперадмин)
    ADMIN_SA_BUCKET,             # ввод имени нового бакета + перенос законки

    # Заявки в тестировщики (просмотр/отбор в админ-панели)
    ADMIN_TESTER_APPS,           # список заявок + карточка + принять/отклонить/тумблер

    # Заявки на ВИП за промокод (тумблер приёма + выгрузка открытых в личку)
    ADMIN_VIP_APPS,              # меню заявок на ВИП

    # Управление HWID-привязкой лицензий оверлея
    MANAGE_HWID,                 # меню HWID
    GET_ID_FOR_HWID_RESET,       # ввод user_id для сброса привязки ключа
    GET_HWID_FOR_BAN,            # ввод HWID-строки для бана
    GET_HWID_FOR_UNBAN,          # ввод HWID-строки для разбана
    GET_ID_FOR_HWID_VIEW,        # ввод user_id для просмотра привязки

) = range(126)  # Обновлено: + заявки на ВИП за промокод (ADMIN_VIP_APPS)

# --- БЛОК: НАСТРОЙКИ ГЕНЕРАТОРА ЖАЛОБ/ИСКОВ ---
# Модель для анализатора жалоб (извлечение фактов, определение сложности)
# Используем flash модель для скорости и дешевизны, так как задача аналитическая
COMPLAINT_ANALYZER_MODEL_NAME = 'gemini-3-flash-preview'

# Модель для фильтрации законов при составлении жалоб/исков
COMPLAINT_ROUTER_MODEL_NAME = 'gemini-3.1-flash-lite'
# Модель для составления жалоб и исков
COMPLAINT_GENERATOR_MODEL_NAME = 'gemini-3-flash-preview'

# Типы документов
COMPLAINT_TYPES = {
    "complaint": "Жалоба в прокуратуру",
    "lawsuit": "Иск в суд"
}

# --- КЛАССИФИКАТОРЫ ДЛЯ АНАЛИЗА ЖАЛОБ ---
# Уровни сложности жалобы
COMPLAINT_COMPLEXITY_LEVELS = {
    'easy',      # Очевидное нарушение, 1-2 доказательства, стандартная ситуация
    'medium',    # Требует внимательного просмотра, спорная ситуация
    'hard',      # Комплексное нарушение, много участников, требует проверки логов
    'expert'     # Жалоба на лидера/админа, массовые нарушения, требует куратора
}

# Требуемый уровень проверяющего
COMPLAINT_REVIEWER_LEVELS = {
    'any',           # Любой администратор/модератор
    'junior_admin',  # Младший администратор+
    'senior_admin',  # Старший администратор+
    'curator',       # Куратор фракции/администратор
    'chief_admin'    # ГА/ЗГА
}

# Тип субъекта жалобы (на кого жалоба)
COMPLAINT_TARGET_TYPES = {
    'player',        # Обычный игрок
    'leader',        # Лидер фракции
    'admin',         # Администратор
    'organization',  # Фракция/семья целиком
    'unknown'        # Не удалось определить
}

# Дефолтные типы проблем (для инициализации БД)
DEFAULT_COMPLAINT_ISSUE_TYPES = [
    {
        "code": "dm",
        "name": "Deathmatch (DM)",
        "description": "Убийство или нанесение урона без веской IC-причины.",
        "is_required": True
    },
    {
        "code": "db",
        "name": "DriveBy (DB)",
        "description": "Убийство или нанесение урона с использованием транспортного средства.",
        "is_required": True
    },
    {
        "code": "nrp",
        "name": "Non-RP Behavior",
        "description": "Действия, которые невозможны в реальности или нарушают атмосферу RP (PG, MG, и др.).",
        "is_required": True
    },
    {
        "code": "cheats",
        "name": "Стороннее ПО (Читы)",
        "description": "Использование запрещенного программного обеспечения.",
        "is_required": True
    },
    {
        "code": "other",
        "name": "Прочее",
        "description": "Нарушения, не попадающие в основные категории.",
        "is_required": False
    }
]

# Лимиты на количество генерируемых документов
DAILY_COMPLAINT_GEN_LIMIT = 3
SUBSCRIPTION_BONUS_COMPLAINT_GEN = 7

# --- БЛОК: НАСТРОЙКИ Criminal Case Writer (CCW) ---
CCW_MODEL_NAME = 'gemini-2.5-pro'
CCW_SESSION_TIMEOUT_HOURS = 24
CCW_TEMPLATE_DOC_ID = None  # Заглушка, если нет конкретного ID

# Порядок и названия частей уголовного дела
CRIMINAL_CASE_PARTS_ORDER = [
    "facts",       # Фабула, описание ситуации
    "charges",     # Инкриминируемые статьи
    "evidence",    # Доказательная база
    "demands"      # Требования обвинения/прокуратуры
]

CRIMINAL_CASE_PARTS_DISPLAY = {
    "facts": "Фабула дела",
    "charges": "Обвинение и квалификация",
    "evidence": "Доказательства",
    "demands": "Требования прокуратуры"
}

# --- БЛОК: НАСТРОЙКИ Prosecution Complaint Handler (PCH) ---
PCH_MODEL_NAME = 'gemini-2.5-flash'
PCH_SESSION_TIMEOUT_HOURS = 48
PCH_WARNING_BEFORE_HOURS = 6

# Порядок этапов обработки жалобы
PCH_STAGES_ORDER = [
    "intake",          # Приём и первичный анализ жалобы
    "planning",        # Планирование: определение нарушений и стратегии
    "clarification",   # Уточнение: сбор недостающей информации
    "finalization",    # Финализация: итоговый анализ и выводы
]

PCH_STAGES_DISPLAY = {
    "intake": "📥 Приём жалобы",
    "planning": "📋 Планирование",
    "clarification": "🔍 Уточнение",
    "finalization": "✅ Финализация",
}

# Типы документов, которые можно генерировать
PCH_DOCUMENT_TYPES = {
    "resolution": "Резолюция (ответ заявителю)",
    "warning": "Предупреждение нарушителю",
    "case_referral": "Направление в уголовное дело",
    "administrative_order": "Административное распоряжение",
    "summary_report": "Сводный отчёт по жалобе",
}

# Какие документы доступны на каком этапе
PCH_STAGE_AVAILABLE_DOCS = {
    "intake": ["summary_report"],
    "planning": ["summary_report"],
    "clarification": ["summary_report", "warning"],
    "finalization": ["resolution", "warning", "case_referral", "administrative_order", "summary_report"],
}

# END OF FILE config.py
