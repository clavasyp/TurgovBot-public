# CMS Migration API - Документация интеграции

## Обзор

Этот документ описывает новые API-эндпоинты для миграции AI-функций с OpenAI (prosecutor-cms1) на Gemini (ProdBot API).

**Базовый URL:** `https://api.turgovprok.ru/v1/cms`

**Аутентификация:** API-ключ в заголовке `X-API-Key`

## Эндпоинты

### 1. POST /v1/cms/full-analysis

Полный AI-анализ жалобы. Заменяет `analyzeComplaintFull()` из `complaintAiService.js`.

**Request:**
```json
{
  "server_id": "la_puerta",
  "title": "ОБРАЩЕНИЕ В ОФИС ГЕНЕРАЛЬНОГО ПРОКУРОРА №123 ОТ ГР-А John Smith",
  "content": "Текст жалобы..."
}
```

**Response:**
```json
{
  "extractedData": {
    "complainantName": "John Smith",
    "respondent": "Officer Brown",
    "respondentPosition": "Сотрудник",
    "respondentOrg": "LSPD",
    "respondents": [
      {"name": "Officer Brown", "position": "Сотрудник", "org": "LSPD"}
    ],
    "complexity": "MEDIUM",
    "complaintNumber": 123
  },
  "facts": ["Заявитель был задержан", "Заявителю не зачитали права"],
  "legalCharacterization": ["Задержание без оснований", "Нарушение ст.19 ПК"],
  "requirements": ["Привлечь к ответственности", "Компенсация"],
  "formCheck": {
    "hasSignature": true,
    "hasEmail": true,
    "hasPassportCopy": true,
    "isValid": true
  },
  "summary": "John Smith жалуется на незаконное задержание сотрудником LSPD",
  "recommendations": [
    {"type": "action", "title": "Истребовать документы", "description": "..."}
  ],
  "evidence": [
    {"url": "https://youtube.com/...", "typeLabel": "видео 15 минут"}
  ],
  "pipelineFlags": {
    "requiresDocs": true,
    "requiresInterrogation": false,
    "requiresCourt": false
  },
  "processing_time_ms": 2345.67
}
```

---

### 2. POST /v1/cms/issue-tags

Получение юридических тегов для поиска по законам. Заменяет `getIssueSpottingTags()`.

**Request:**
```json
{
  "facts": [
    "Заявитель был задержан без оснований",
    "Заявителю не были зачитаны права Miranda"
  ]
}
```

**Response:**
```json
{
  "tags": ["незаконное задержание", "права Miranda", "процедура ареста"],
  "processing_time_ms": 1234.56
}
```

---

### 3. POST /v1/cms/evidence-labels

Классификация типов ссылок на доказательства. Заменяет `getEvidenceTypeLabels()`.

**Request:**
```json
{
  "text": "Прилагаю видео задержания https://youtube.com/... и скриншот переписки https://imgur.com/...",
  "urls": [
    "https://youtube.com/watch?v=abc123",
    "https://imgur.com/xyz789"
  ]
}
```

**Response:**
```json
{
  "labels": [
    {"url": "https://youtube.com/watch?v=abc123", "typeLabel": "видео задержания"},
    {"url": "https://imgur.com/xyz789", "typeLabel": "скриншот переписки"}
  ],
  "processing_time_ms": 890.12
}
```

---

### 4. POST /v1/cms/validate-form-ai

AI-валидация формы обращения. Заменяет `validateFormAI()`.

**Request:**
```json
{
  "title": "ОБРАЩЕНИЕ В ОФИС ГЕНЕРАЛЬНОГО ПРОКУРОРА №123 ОТ ГР-А John Smith",
  "content": "1. Имя и Фамилия: John Smith\n2. Сведения о нарушителе..."
}
```

**Response:**
```json
{
  "isValid": true,
  "confidenceScore": 85,
  "formQuality": "GOOD",
  "issues": [
    {
      "severity": "WARNING",
      "field": "evidence",
      "issue": "Не указаны доказательства",
      "suggestion": "Добавьте ссылки на видео или скриншоты"
    }
  ],
  "missingInfo": ["Точное время инцидента"],
  "contradictions": [],
  "extractedData": {
    "complainant": "John Smith",
    "defendant": "Officer Brown",
    "organization": "LSPD",
    "incidentDate": "15.01",
    "incidentTime": null
  },
  "recommendation": "ACCEPT_WITH_NOTICE",
  "summary": "Обращение в целом соответствует требованиям",
  "processing_time_ms": 1567.89
}
```

---

### 5. POST /v1/cms/case-file-draft

Генерация черновика кейс-файла. Заменяет `generateCaseFileDraft()`.

**Request:**
```json
{
  "complaint_title": "ОБРАЩЕНИЕ №123",
  "complaint_content": "Текст жалобы...",
  "facts_with_verdict": [
    {"text": "Заявитель был задержан", "status": "TRUE", "legalPositionText": "Нарушение ст.16 ЭК"},
    {"text": "Заявитель оскорбил сотрудника", "status": "FALSE"}
  ],
  "law_sections": ["Статья 16 ЭК: ...", "Статья 20 ЭК: ..."],
  "requirements": ["Привлечь к ответственности"]
}
```

**Response:**
```json
{
  "draft": {
    "FACTS": "Установлено, что 15 января 2026 года сотрудник LSPD Officer Brown...",
    "OVERVIEW_OF_VIOLATION": "3.1 Нарушение Этического кодекса штата Сан-Андреас",
    "LAW_CITING": "Этический кодекс распространяется на всех сотрудников...",
    "FACTS_CONNECTED_WITH_LAW": "Действия Officer Brown нарушают ст. 16 ЭК...",
    "TYPE_OF_CRIME": "административное правонарушение",
    "ARTICLES_VIOLATED": "ЭК ст. 16, ст. 20",
    "PENALTY": "Штраф $5,000, выговор"
  },
  "sections_count": 2,
  "processing_time_ms": 3456.78
}
```

---

### 6. POST /v1/cms/generate-plan

Генерация плана работы по жалобе. Заменяет `generatePlanAI()`.

**Request:**
```json
{
  "complaint": {
    "number": 123,
    "title": "Жалоба на LSPD",
    "defendant": "Officer Brown",
    "defendantOrg": "LSPD",
    "complainant": "John Smith",
    "complexity": "MEDIUM",
    "requiresDocs": true
  },
  "analysis": {
    "formValidation": {"isValid": true},
    "extractedFacts": ["Факт 1", "Факт 2"],
    "legalAnalysis": null,
    "requiredInfo": null
  },
  "defendantHistory": {
    "previousCases": 2,
    "previousFines": 1,
    "riskScore": 45
  }
}
```

**Response:**
```json
{
  "planType": "WITH_DOCS_REQUEST",
  "estimatedDuration": "5-7 дней",
  "complexity": "MEDIUM",
  "phases": [
    {
      "name": "Возбуждение производства",
      "description": "Принятие жалобы к рассмотрению",
      "steps": [
        {
          "order": 1,
          "type": "CREATE_DOCUMENT",
          "title": "Возбудить производство",
          "description": "Создайте постановление о возбуждении",
          "documentType": "INITIATE_CASE",
          "prefillData": {"defendant": "Officer Brown"},
          "isOptional": false
        }
      ]
    }
  ],
  "decisionPoints": [...],
  "riskFactors": ["Повторное нарушение ответчика"],
  "recommendations": ["Рассмотреть возможность допроса"],
  "processing_time_ms": 2100.00
}
```

---

### 7. POST /v1/cms/adapt-plan

Адаптация плана с учётом новой информации. Заменяет `adaptPlanAI()`.

**Request:**
```json
{
  "current_plan": {"planType": "WITH_DOCS_REQUEST", "phases": [...]},
  "new_information": {
    "reason": "Получены документы от LSPD",
    "documentsReceived": true,
    "newEvidence": "Видео подтверждает факты"
  },
  "completed_steps": [1, 2, 3]
}
```

**Response:**
```json
{
  "needsAdaptation": true,
  "reason": "Получены документы, можно перейти к принятию решения",
  "changes": [
    {
      "action": "SKIP",
      "stepOrder": 4,
      "reason": "Документы уже получены"
    },
    {
      "action": "ADD",
      "stepData": {"type": "MAKE_DECISION", "title": "Принять решение"},
      "reason": "Все материалы собраны"
    }
  ],
  "newEstimatedDuration": "2-3 дня",
  "warnings": [],
  "processing_time_ms": 1800.00
}
```

---

### 8. POST /v1/cms/passport-ocr

OCR паспорта гражданина. Заменяет `extractPassportData()`.

**Request:**
```json
{
  "image_url": "https://example.com/passport.png"
}
```

**Response:**
```json
{
  "fullName": "John Smith",
  "passportNumber": "SA-123456",
  "dateOfBirth": "1990-05-15",
  "gender": "М",
  "maritalStatus": "не женат",
  "issuedDate": "2025-01-01",
  "photoDescription": "Мужчина, короткие тёмные волосы",
  "confidence": 95,
  "readabilityIssues": [],
  "isValid": true,
  "processing_time_ms": 1234.56
}
```

---

### 9. POST /v1/cms/resolve-passport-url

Определение ссылки на паспорт среди нескольких URL. Заменяет `resolvePassportUrlFromCandidates()`.

**Используется когда:**
- В обращении несколько ссылок (видео, скриншоты, паспорт)
- Нужно определить, какая из них — ксерокопия паспорта (п. 6 формы)

**Request:**
```json
{
  "content": "Текст обращения с несколькими ссылками...",
  "urls": [
    "https://youtube.com/watch?v=abc123",
    "https://imgur.com/xyz789",
    "https://imgur.com/passport123"
  ]
}
```

**Response:**
```json
{
  "passportUrl": "https://imgur.com/passport123",
  "processing_time_ms": 567.89
}
```

Если паспорт не найден:
```json
{
  "passportUrl": null,
  "processing_time_ms": 234.56
}
```

---

## Миграция на стороне сайта

### Пример: Замена analyzeComplaintFull

**Было (complaintAiService.js):**
```javascript
const result = await analyzeComplaintFull({ title, content });
```

**Стало:**
```javascript
const apiClient = require('./prodBotApiClient');

const result = await apiClient.post('/v1/cms/full-analysis', {
  server_id: 'la_puerta',
  title,
  content
});
// result.data уже в нужном формате
```

### Создание API-клиента

```javascript
// services/prodBotApiClient.js
const axios = require('axios');

const PRODBOT_API_URL = process.env.PRODBOT_API_URL || 'https://api.turgovprok.ru';
const PRODBOT_API_KEY = process.env.PRODBOT_API_KEY;

const client = axios.create({
  baseURL: PRODBOT_API_URL,
  headers: {
    'X-API-Key': PRODBOT_API_KEY,
    'Content-Type': 'application/json'
  },
  timeout: 60000
});

module.exports = client;
```

---

## Обработка ошибок

| Код | Описание |
|-----|----------|
| 400 | Невалидные входные данные |
| 401 | Отсутствует или невалидный API-ключ |
| 500 | Внутренняя ошибка сервера |
| 503 | AI-сервис временно недоступен |

При ошибке 503 рекомендуется retry с exponential backoff.

