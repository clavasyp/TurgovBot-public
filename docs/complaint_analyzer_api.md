# Complaint Analyzer API — Документация

## Обзор

Complaint Analyzer API предоставляет stateless-эндпоинты для анализа жалоб на RP-сервере. API предназначен для интеграции с сайтом и позволяет:

1. **Парсить жалобы** по URL темы форума
2. **Проверять форму** жалобы на соответствие шаблону
3. **Анализировать** сложность, субъект и уровень рецензента
4. **Извлекать факты** и требования заявителя
5. **Управлять настройками** (шаблоны форм, типы проблем)

---

## Архитектура

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│     Сайт        │────▶│   REST API       │────▶│  AI (Gemini)    │
│  (Frontend)     │     │  /v1/complaint/* │     │                 │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                               │
                               ▼
                        ┌──────────────────┐
                        │   PostgreSQL     │
                        │  (шаблоны, типы) │
                        └──────────────────┘
```

---

## Workflow использования

### Основной сценарий: анализ жалобы

```
1. POST /v1/complaint/parse
   ├── Вход: complaint_url (URL темы на форуме)
   └── Выход: title, content, thread_id
                  │
                  ▼ (сохраняем content на фронте)
                  
2. POST /v1/complaint/check-form
   ├── Вход: server_id, complaint_text
   └── Выход: is_valid, explanation, issues[]
                  │
                  ▼
                  
3. POST /v1/complaint/analyze
   ├── Вход: server_id, complaint_text  
   └── Выход: complexity, needs_additional_materials, subject, reviewer_level
                  │
                  ▼
                  
4. POST /v1/complaint/extract-facts
   ├── Вход: server_id, complaint_text
   └── Выход: facts[], requirements[]
```

**Важно:** Парсинг (`/parse`) вызывается **один раз**. Полученный `content` передаётся во все остальные эндпоинты как `complaint_text`.

---

## Эндпоинты

### Парсинг жалобы

#### `POST /v1/complaint/parse`

Парсит тему форума с жалобой.

**Запрос:**
```json
{
    "complaint_url": "https://forum.majestic-rp.ru/threads/123456/"
}
```

**Ответ:**
```json
{
    "title": "Жалоба на игрока Nick_Name",
    "content": "Полный текст жалобы со всеми сообщениями...",
    "thread_id": "123456"
}
```

---

### Проверка формы

#### `POST /v1/complaint/check-form`

Проверяет соответствие жалобы установленному шаблону формы.

**Запрос:**
```json
{
    "server_id": "majestic_1",
    "complaint_text": "Текст жалобы из /parse..."
}
```

**Ответ:**
```json
{
    "is_valid": false,
    "explanation": "В жалобе отсутствует указание никнейма нарушителя и приложенные доказательства не соответствуют требованиям формы подачи.",
    "issues": ["nickname", "evidence"],
    "processing_time_ms": 1523.45
}
```

**Поля ответа:**
- `is_valid` — корректна ли форма (boolean)
- `explanation` — текст для постановления об отказе (если `is_valid=false`)
- `issues` — коды проблем из `issue_types` сервера

---

### Анализ жалобы

#### `POST /v1/complaint/analyze`

Определяет параметры жалобы для маршрутизации.

**Запрос:**
```json
{
    "server_id": "majestic_1",
    "complaint_text": "Текст жалобы из /parse..."
}
```

**Ответ:**
```json
{
    "complexity": "medium",
    "needs_additional_materials": true,
    "subject": {
        "target_type": "person",
        "nickname": "Ivan_Petrov",
        "organization": "LSPD",
        "position": "Officer"
    },
    "reviewer_level": "sgp",
    "processing_time_ms": 2105.67
}
```

**Поля ответа:**
- `complexity` — сложность: `low` | `medium` | `high`
- `needs_additional_materials` — нужны ли доп. материалы
- `subject` — информация о субъекте жалобы
- `reviewer_level` — минимальный уровень рецензента:
  - `any` — любой прокурор
  - `sgp` — старший прокурор и выше
  - `zgp` — зам. генпрокурора и выше
  - `gp` — только генеральный прокурор

---

### Извлечение фактов

#### `POST /v1/complaint/extract-facts`

Извлекает юридически значимые факты и требования.

**Запрос:**
```json
{
    "server_id": "majestic_1",
    "complaint_text": "Текст жалобы из /parse..."
}
```

**Ответ:**
```json
{
    "facts": [
        "15.01.2026 в 21:30 игрок Ivan_Petrov произвёл задержание без предъявления обвинения",
        "При задержании не были зачитаны права по Миранде",
        "Игрок находился на территории LSPD более 2 часов без оформления протокола"
    ],
    "requirements": [
        "Привлечь Ivan_Petrov к дисциплинарной ответственности",
        "Провести служебную проверку в LSPD"
    ],
    "processing_time_ms": 1876.23
}
```

---

## Управление настройками сервера

### Шаблон формы

Шаблон формы — образец правильного заполнения жалобы. Устанавливается Генеральным Прокурором сервера.

#### `GET /v1/complaint/form-template/{server_id}`

Получить текущий шаблон.

**Ответ:**
```json
{
    "server_id": "majestic_1",
    "form_url": "https://forum.majestic-rp.ru/threads/образец-жалобы/",
    "form_content": "Текст образца формы...",
    "updated_at": "2026-01-20T15:30:00Z"
}
```

#### `PUT /v1/complaint/form-template`

Установить/обновить шаблон (парсит URL автоматически).

**Запрос:**
```json
{
    "server_id": "majestic_1",
    "form_url": "https://forum.majestic-rp.ru/threads/образец-жалобы/"
}
```

---

### Типы проблем (Issue Types)

Типы проблем — закрытый список возможных нарушений формы. При первом обращении автоматически создаются дефолтные типы.

#### `GET /v1/complaint/issue-types/{server_id}`

Получить список типов проблем.

**Ответ:**
```json
{
    "server_id": "majestic_1",
    "issue_types": [
        {
            "code": "header",
            "name": "Заголовок",
            "description": "Некорректный заголовок темы",
            "is_required": true
        },
        {
            "code": "nickname",
            "name": "Никнейм",
            "description": "Не указан или неверный никнейм",
            "is_required": true
        }
    ]
}
```

#### `POST /v1/complaint/issue-types`

Создать новый тип проблемы.

**Запрос:**
```json
{
    "server_id": "majestic_1",
    "code": "timestamp",
    "name": "Время события",
    "description": "Не указано точное время происшествия",
    "is_required": false
}
```

#### `PUT /v1/complaint/issue-types/{server_id}/{code}`

Обновить существующий тип.

**Запрос:**
```json
{
    "name": "Новое название",
    "description": "Новое описание",
    "is_required": true
}
```

#### `DELETE /v1/complaint/issue-types/{server_id}/{code}`

Удалить тип проблемы. Возвращает `204 No Content`.

---

## Дефолтные типы проблем

При первом обращении к `GET /issue-types/{server_id}` автоматически создаются:

| Код | Название | Описание | Обязательное |
|-----|----------|----------|--------------|
| `header` | Заголовок | Некорректный заголовок темы | ✅ |
| `nickname` | Никнейм | Не указан или неверный никнейм | ✅ |
| `oos_mg_info` | OOC/МГ информация | Содержит OOC или метагейм | ❌ |
| `description` | Описание | Неполное описание ситуации | ✅ |
| `evidence` | Доказательства | Отсутствуют или недостаточны | ✅ |
| `requirements` | Требования | Не указаны требования заявителя | ✅ |
| `format` | Формат | Нарушен формат заполнения | ❌ |
| `attachments` | Вложения | Проблемы с вложениями | ❌ |
| `other` | Другое | Прочие нарушения | ❌ |

---

## Аутентификация

Все запросы требуют заголовок `X-API-Key`:

```
X-API-Key: your-api-key-here
```

---

## Коды ошибок

| HTTP код | Описание |
|----------|----------|
| 400 | Невалидные входные данные |
| 401 | Отсутствует или невалидный API-ключ |
| 404 | Ресурс не найден |
| 409 | Конфликт (например, дублирование кода issue_type) |
| 500 | Внутренняя ошибка сервера |
| 503 | AI-сервис временно недоступен |

---

## Пример интеграции (JavaScript)

```javascript
const API_BASE = 'https://api.example.com';
const API_KEY = 'your-api-key';

async function analyzeComplaint(complaintUrl, serverId) {
    const headers = {
        'Content-Type': 'application/json',
        'X-API-Key': API_KEY
    };

    // 1. Парсим жалобу
    const parseRes = await fetch(`${API_BASE}/v1/complaint/parse`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ complaint_url: complaintUrl })
    });
    const { content, title } = await parseRes.json();

    // 2. Проверяем форму
    const checkRes = await fetch(`${API_BASE}/v1/complaint/check-form`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ server_id: serverId, complaint_text: content })
    });
    const formResult = await checkRes.json();

    // 3. Анализируем
    const analyzeRes = await fetch(`${API_BASE}/v1/complaint/analyze`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ server_id: serverId, complaint_text: content })
    });
    const analysisResult = await analyzeRes.json();

    // 4. Извлекаем факты
    const factsRes = await fetch(`${API_BASE}/v1/complaint/extract-facts`, {
        method: 'POST',
        headers,
        body: JSON.stringify({ server_id: serverId, complaint_text: content })
    });
    const factsResult = await factsRes.json();

    return {
        title,
        form: formResult,
        analysis: analysisResult,
        facts: factsResult
    };
}
```

---

## База данных

### Таблица `complaint_form_templates`

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL | PK |
| server_id | VARCHAR(100) | ID сервера (unique) |
| form_url | TEXT | URL темы с образцом |
| form_content | TEXT | Распарсенный контент образца |
| updated_at | TIMESTAMP | Время последнего обновления |

### Таблица `complaint_issue_types`

| Колонка | Тип | Описание |
|---------|-----|----------|
| id | SERIAL | PK |
| server_id | VARCHAR(100) | ID сервера |
| code | VARCHAR(100) | Код типа (snake_case) |
| name | VARCHAR(255) | Название |
| description | TEXT | Описание |
| is_required | BOOLEAN | Обязательное поле |
| created_at | TIMESTAMP | Время создания |

**Уникальный индекс:** `(server_id, code)`

---

## Применение миграции

```bash
alembic upgrade head
```
