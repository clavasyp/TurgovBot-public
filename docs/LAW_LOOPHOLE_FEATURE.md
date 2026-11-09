# Тестовый функционал «Анализатор дыр законки» (`/test`)

> Документ написан для владельца бота. Описывает: что было добавлено, какие
> файлы менять при настройке, как функционал работает изнутри и как им
> пользоваться. Сама фича **тестовая**, в основном меню её нет, доступ
> только через команду `/test` и только для тестеров/админов.

---

## 1. Что было добавлено (карта файлов)

### Новые модули

| Файл | Назначение |
|---|---|
| `api/prompts/law_loopholes.py` | 14 функций-промптов: системный, роутер RAG, single-doc анализ, кросс-документ, role-abuse сценарии, синтез, форматирование отчёта, case-анализ, ревью нового закона, devil's advocate, batch-группировка, error recovery. |
| `services/law_loophole/__init__.py` | Реэкспорт публичного API пакета. |
| `services/law_loophole/bundle.py` | **Source Selection Policy.** Класс `LawContextBundle` + функция `build_law_context_bundle()`. Решает, какие законы попадут в анализатор. |
| `services/law_loophole/model_policy.py` | **Model Policy.** 4 профиля (fast/balanced/deep/expert), автоапгрейд при больших контекстах. |
| `services/law_loophole/analyzer.py` | Пайплайн анализа: параллельные single-doc проходы + кросс-документный, парсинг JSON, валидация, дедуп, синтез. |
| `services/law_loophole/report.py` | Локальный (без ИИ) форматтер отчёта в Telegram-HTML. |
| `user_handlers/test_loophole_flow.py` | `ConversationHandler` с UX-флоу команды `/test`. |
| `tests/__init__.py`, `tests/conftest.py` | Инфраструктура тестов. |
| `tests/test_law_loophole.py` | 32 unit-теста, все зелёные. |
| `pytest.ini` | `asyncio_mode = auto`, `testpaths = tests`. |

### Изменённые файлы

| Файл | Что изменено |
|---|---|
| `config.py` | Добавлены константы моделей `LOOPHOLE_ROUTER/ANALYZER/SYNTHESIS_MODEL_*` для каждого профиля, лимиты `LOOPHOLE_MAX_MANUAL_DOCS_*`, порог батчинга `LOOPHOLE_BATCH_THRESHOLD_CHARS`. |
| `user_handlers/__init__.py` | Экспорт `test_loophole_conv_handler`. |
| `main.py` | Регистрация `test_loophole_conv_handler` **до** `user_conv_handler`, чтобы `/test` перехватывался раньше основного flow. |

### Зависимости

- Установлен `pytest-asyncio` (только для тестов).

---

## 2. Что нужно настроить перед запуском в проде

### 2.1. Имена моделей — **обязательно проверить**

Файл: `config.py`, блок «Модели для анализатора дыр законки».

```python
_LOOPHOLE_FLASH_LITE = 'gemini-3.1-flash-lite-preview'
_LOOPHOLE_FLASH      = 'gemini-3-flash-preview'
_LOOPHOLE_PRO        = 'gemini-2.5-pro'
```

Это **placeholder-имена**. Замените на те, что реально доступны в вашем
Vertex AI проекте (`GCP_PROJECT_ID`). По профилям модели распределены так:

| Профиль | Router (выбор законов) | Analyzer (поиск дыр) | Synthesis (сведение) |
|---|---|---|---|
| `fast` | Flash Lite | Flash | Flash |
| `balanced` | Flash Lite | Flash | Flash |
| `deep` | Flash Lite | **Pro** | **Pro** |
| `expert` | Flash | **Pro** | **Pro** |

> Если в Vertex доступна только Pro — поставьте Pro во все три поля
> (получите медленнее, но качественнее анализ).

### 2.2. Лимиты документов на профиль

```python
LOOPHOLE_MAX_MANUAL_DOCS_FAST     = 5
LOOPHOLE_MAX_MANUAL_DOCS_BALANCED = 10
LOOPHOLE_MAX_MANUAL_DOCS_DEEP     = 20
LOOPHOLE_MAX_MANUAL_DOCS_EXPERT   = 40
LOOPHOLE_BATCH_THRESHOLD_CHARS    = 200_000
```

Если решите ужесточить (например, упереться в TPM Vertex) — уменьшите.
Превышение лимита **автоматически апгрейдит профиль** на более тяжёлый
(см. `choose_model_profile()` в `services/law_loophole/model_policy.py`).

### 2.3. GCS-бакет

**Ничего настраивать не нужно** — функционал использует тот же
`GCS_BUCKET_NAME`, что и весь остальной бот (`knowledge_base_manager`,
`precedents_manager`, `complaint_flow`). Законы читаются из таблицы
`law_documents`, поле `gemini_file_uri` (формат `gs://bucket/blob`),
передаются в Gemini через `types.Part.from_uri(...)`.

### 2.4. Доступ пользователей

Команда `/test` доступна только если в таблице `users` для пользователя:

- `is_admin = TRUE`, **или**
- `is_tester = TRUE`, **или**
- `user_id == config.SUPER_ADMIN_ID`.

Назначить тестера:

```sql
UPDATE users SET is_tester = TRUE WHERE user_id = <telegram_id>;
```

### 2.5. Данные в БД

Чтобы фича вообще работала, в `law_documents` должны быть строки с
непустым `gemini_file_uri` для нужного `server_id`. Это уже происходит
автоматически при загрузке законов через `knowledge_base_manager` — то
есть если боевой RAG работает, эта фича тоже заработает.

Проверить наличие законов:

```sql
SELECT server_id, COUNT(*) AS total,
       COUNT(*) FILTER (WHERE gemini_file_uri IS NOT NULL AND gemini_file_uri <> '') AS with_uri
FROM law_documents
GROUP BY server_id;
```

---

## 3. Как это работает (архитектура)

```
┌────────────────────────────────────────────────────────────────────┐
│                        Telegram: /test                              │
│           (только тестеры/админы / SUPER_ADMIN)                     │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                  user_handlers/test_loophole_flow.py
                  (ConversationHandler, 7 шагов)
                               │
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
   1) Выбор проекта                      Состояния:
   2) Выбор сервера                      TEST_PROJECT, TEST_SERVER,
   3) Выбор типа анализа                 TEST_TYPE, TEST_SOURCE,
   4) Выбор режима источников            TEST_PROFILE, TEST_MANUAL,
   5) Выбор профиля модели               TEST_CONFIRM
   6) (опц.) Ручной выбор документов
   7) Подтверждение → Запуск
                               │
                               ▼
        services.law_loophole.build_law_context_bundle()
        ──────────── Source Selection Policy ──────────
        Режимы:
        • full_server   — все законы сервера
        • manual_select — пользователь выбрал руками
        • auto_rag      — router-модель решает по запросу
        • hybrid        — manual + расширение через router
                               │
                               ▼ LawContextBundle
        services.law_loophole.choose_model_profile()
        ──────────── Model Policy ─────────────────────
        Если bundle большой / много документов — апгрейд
        профиля (fast → balanced → deep → expert).
                               │
                               ▼ LoopholeModels
        services.law_loophole.analyze_loopholes()
        ──────────── Analyzer Pipeline ────────────────
        a) для КАЖДОГО документа: single-doc проход
           (asyncio.gather, параллельно, через
            execute_vertex_request с RequestPriority.NORMAL
            и rate-limiter'ом).
        b) если >1 документа: 1 кросс-документный проход
           (поиск противоречий между ними).
        c) парсинг JSON (с поддержкой markdown-обёрток
           и мусорного текста вокруг).
        d) валидация (отбрасываем находки без source_documents
           и source_fragments, нормализуем severity/confidence/type).
        e) дедупликация по (title, vulnerability_type) с
           объединением источников и сценариев, держим
           максимальную severity.
        f) сортировка critical → high → medium → low.
        g) build_summary() — счётчики по серьёзности.
                               │
                               ▼ report dict
        services.law_loophole.format_loophole_report()
        — локальное HTML-форматирование (без ИИ),
          разбиение на сообщения по 3500 символов,
          html.escape всех пользовательских значений.
                               │
                               ▼
        Telegram: серия HTML-сообщений + JSON-дамп файлом.
```

---

## 4. Сценарии использования (что выбирать)

| Цель | Тип анализа | Режим источников | Профиль |
|---|---|---|---|
| Быстрая проверка одного нового закона | `quick_scan` | `manual_select` (1 закон) | `fast` |
| Глубокий аудит конкретного закона | `deep_abuse` | `manual_select` | `deep` |
| Поиск противоречий между несколькими | `quick_scan` | `manual_select` (2–10) | `balanced` |
| RAG-поиск по запросу: «найди дыры в правах полиции» | `quick_scan` | `auto_rag` | `balanced` |
| Полный аудит сервера | `full_audit` | `full_server` | `deep` или `expert` |
| Анализ кейса (как обойти законы) | `case_analysis` | `auto_rag` или `manual` | `deep` |

При больших объёмах система **сама** поднимет профиль — не бойтесь
выставить `balanced`, если контекст огромный, его всё равно обновят на
`deep`/`expert`.

---

## 5. Что возвращает анализ

Сообщения в Telegram:

1. **Шапка** — счётчики по серьёзности:
   ```
   📊 Найдено уязвимостей: 7
   🔴 Критичных: 1
   🟠 Высоких: 3
   🟡 Средних: 2
   🟢 Низких: 1
   ```

2. **Список находок** (по одной на блок), каждая содержит:
   - заголовок (`<b>`)
   - тип уязвимости (vague_term, missing_sanction, direct_contradiction, role_abuse, ...)
   - severity и confidence (с эмодзи)
   - source_documents — какие законы затронуты
   - source_fragments — точные цитаты из текста
   - issue_summary — суть проблемы
   - abuse_scenarios — как этим можно злоупотребить
   - affected_roles — кто затронут (игроки/полиция/админы)
   - gameplay_impact — как ломает геймплей
   - fix_recommendation — как починить

3. **JSON-дамп** — полный отчёт прикладывается как `.json` файл (для
   копирования в issues / документацию).

---

## 6. Защитные механизмы

- **Rate limit:** все вызовы Vertex идут через `rate_limiter.execute_vertex_request`
  с `RequestPriority.NORMAL`, ретраями и переключением региона при ошибках.
- **Изоляция ошибок:** один упавший single-doc проход (asyncio.gather с
  `return_exceptions=True`) не валит весь анализ — остальные документы
  обработаются.
- **Невалидный JSON от модели не крашит бота** — `parse_json_response()`
  возвращает `None`, документ просто получит 0 находок.
- **Защита от инъекций в HTML** — все пользовательские/модельные
  значения проходят через `html.escape(..., quote=False)` перед вставкой
  в Telegram-сообщение.
- **Лимит документов** проверяется и в bundle (manual_select), и в
  Telegram-UX (нельзя ткнуть больше галочек, чем профиль позволяет).
- **`/test` не светится в основном меню** — пользователь, не знающий
  команду, фичу не увидит.

---

## 7. Как откатить / выключить фичу

Один из способов на выбор:

**Вариант A — отключить только команду** (модули остаются в репо):

В `main.py` закомментировать строку с регистрацией:

```python
# application.add_handler(test_loophole_conv_handler)
```

**Вариант B — полное удаление**:

```bash
git rm -r services/law_loophole/ user_handlers/test_loophole_flow.py \
         api/prompts/law_loopholes.py tests/test_law_loophole.py pytest.ini
```

И откатить блок `LOOPHOLE_*` в `config.py` + удалить импорт в
`user_handlers/__init__.py` и `main.py`.

---

## 8. Тесты

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_law_loophole.py -v
```

Текущий результат: **32 passed**.

Покрывают:
- все 4 режима выборки + ошибки/лимиты;
- политику моделей (профили, апгрейды);
- парсинг JSON (чистый, в markdown-обёртке, с мусорным текстом, невалид);
- валидацию (источники обязательны, нормализация severity/type);
- дедупликацию (мердж списков, держим максимальную severity);
- end-to-end анализ с замоканной моделью (3 ответа: 2 single-doc + 1 кросс);
- форматирование отчёта (HTML, эскейп `<script>`, пустой результат).

---

## 9. Известные ограничения

1. **Модели — placeholder.** Реальные имена должны прийти от вас (см. §2.1).
2. **Нет Cost-Estimator UI.** Стоимость анализа считается только в логах
   через rate-limiter; в UI пользователь увидит только время выполнения.
3. **Нет персиста отчёта в БД.** Сейчас отчёт уходит в Telegram и теряется
   после очистки чата. Если нужен журнал — потребуется создать таблицу
   `loophole_reports` (можно добавить отдельной задачей).
4. **Нет батчинга «по-настоящему».** Поле `requires_batching` уже
   рассчитывается, но сам пайплайн пока всегда отправляет все документы
   разом — для очень больших серверов (>30 законов) лучше использовать
   `manual_select`/`auto_rag`, чтобы вписаться в окно контекста.

---

## 10. Контрольный чек-лист перед первым запуском

- [ ] В `config.py` подставлены реальные имена моделей вместо
  `_LOOPHOLE_FLASH_LITE`, `_LOOPHOLE_FLASH`, `_LOOPHOLE_PRO`.
- [ ] У тестера выставлен `is_tester=TRUE` в `users`.
- [ ] В `law_documents` для целевого `server_id` есть записи с
  непустым `gemini_file_uri`.
- [ ] Бот перезапущен (чтобы `main.py` зарегистрировал новый handler).
- [ ] Тесты `pytest tests/test_law_loophole.py` проходят зелёным.
- [ ] В Telegram отправлено `/test` — бот отвечает выбором проекта.
