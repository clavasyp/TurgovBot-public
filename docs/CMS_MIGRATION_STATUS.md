# CMS Migration - Интеграция ProdBot API

## Статус: ✅ Реализовано

Миграция AI-функций с OpenAI (prosecutor-cms1) на Gemini (ProdBot API) завершена.

## Созданные файлы

### ProdBot (API сервер)

| Файл | Описание |
|------|----------|
| `api/routers/cms_migration.py` | 9 API-эндпоинтов для CMS |
| `api/schemas.py` | Pydantic-модели запросов/ответов |
| `api/prompts/complaint_analysis.py` | Промпт анализа жалобы |
| `api/prompts/issue_spotting.py` | Промпт юридических тегов |
| `api/prompts/evidence_labels.py` | Промпт классификации ссылок |
| `api/prompts/form_validation.py` | Промпт AI-валидации формы |
| `api/prompts/case_file_draft.py` | Промпт черновика дела |
| `api/prompts/adaptive_plan.py` | Промпты генерации/адаптации плана |
| `api/prompts/passport_ocr.py` | Промпт OCR паспорта |
| `api/prompts/passport_resolver.py` | Промпт определения паспорта |
| `services/complaint_analyzer_service.py` | 9 CMS-функций (добавлено) |
| `docs/CMS_MIGRATION_API.md` | Документация API |

### prosecutor-cms1 (сайт)

| Файл | Описание |
|------|----------|
| `services/prodBotApiClient.js` | HTTP-клиент для ProdBot API |
| `services/aiServiceAdapter.js` | Адаптер OpenAI ↔ ProdBot |
| `docs/MIGRATION_TO_PRODBOT_API.md` | Руководство по миграции |
| `.env.prodbot.example` | Пример переменных окружения |

## API-эндпоинты

| Эндпоинт | Функция OpenAI | Статус |
|----------|----------------|--------|
| `POST /v1/cms/full-analysis` | `analyzeComplaintFull` | ✅ |
| `POST /v1/cms/issue-tags` | `getIssueSpottingTags` | ✅ |
| `POST /v1/cms/evidence-labels` | `getEvidenceTypeLabels` | ✅ |
| `POST /v1/cms/validate-form-ai` | `validateFormAI` | ✅ |
| `POST /v1/cms/case-file-draft` | `generateCaseFileDraft` | ✅ |
| `POST /v1/cms/generate-plan` | `generatePlanAI` | ✅ |
| `POST /v1/cms/adapt-plan` | `adaptPlanAI` | ✅ |
| `POST /v1/cms/passport-ocr` | `extractPassportData` | ✅ |
| `POST /v1/cms/resolve-passport-url` | `resolvePassportUrlFromCandidates` | ✅ |

## Архитектура

```
prosecutor-cms1 (сайт)              ProdBot API (Gemini)
┌──────────────────────────┐       ┌─────────────────────────┐
│                          │       │                         │
│  Existing Services       │       │  /v1/cms/full-analysis  │
│  ┌────────────────────┐  │       │  /v1/cms/issue-tags     │
│  │ aiServiceAdapter   │──┼──────►│  /v1/cms/evidence-labels│
│  │ (feature flags)    │  │ HTTP  │  /v1/cms/validate-form  │
│  └────────────────────┘  │       │  /v1/cms/case-file-draft│
│         │                │       │  /v1/cms/generate-plan  │
│         ▼ fallback       │       │  /v1/cms/adapt-plan     │
│  ┌────────────────────┐  │       │  /v1/cms/passport-ocr   │
│  │ OpenAI Services    │  │       │  /v1/cms/resolve-passport│
│  │ (original)         │  │       └─────────────────────────┘
│  └────────────────────┘  │                   │
│                          │                   ▼
└──────────────────────────┘       ┌─────────────────────────┐
                                   │  Vertex AI (Gemini)     │
                                   │  Multi-region failover  │
                                   └─────────────────────────┘
```

## Быстрый старт

### На стороне сайта (prosecutor-cms1)

1. Добавьте в `.env`:
```env
USE_PRODBOT_API=true
PRODBOT_API_URL=https://api.turgovprok.ru
PRODBOT_API_KEY=your_api_key_here
```

2. Замените импорты:
```javascript
// Было
const complaintAiService = require('./complaintAiService');
const result = await complaintAiService.analyzeComplaintFull({ title, content });

// Стало
const aiService = require('./aiServiceAdapter');
const result = await aiService.analyzeComplaintFull({ serverId: 'la_puerta', title, content });
```

### Постепенная миграция

Включайте функции по одной:
```env
USE_PRODBOT_FULL_ANALYSIS=true
# Тестируем...
USE_PRODBOT_ISSUE_TAGS=true
# Тестируем...
# И т.д.
```

## Особенности реализации

### Промпты
Все промпты точно повторяют оригинальные из JavaScript-версий:
- `complaintAiService.js` → `api/prompts/complaint_analysis.py`
- `formValidatorService.js` → `api/prompts/form_validation.py`
- `caseFileDraftService.js` → `api/prompts/case_file_draft.py`
- `adaptivePlanService.js` → `api/prompts/adaptive_plan.py`
- `citizenService.js` → `api/prompts/passport_ocr.py`

### Формат ответов
Формат ответов 1:1 совместим с OpenAI-версией. Адаптер автоматически нормализует данные.

### Fallback
При ошибке ProdBot API адаптер автоматически переключается на OpenAI:
```
[aiServiceAdapter] ProdBot analyzeComplaintFull failed, falling back to OpenAI: Connection timeout
```

### Retry
Встроен retry с exponential backoff для 429 и 5xx ошибок.

## Тестирование

```bash
# Проверка health
curl https://api.turgovprok.ru/health

# Тест full-analysis
curl -X POST https://api.turgovprok.ru/v1/cms/full-analysis \
  -H "X-API-Key: your_key" \
  -H "Content-Type: application/json" \
  -d '{"server_id":"test","title":"Test","content":"Test complaint..."}'
```

## Мониторинг

Логи в формате:
```
[ProdBot API] 2026-02-12T10:30:00.000Z analyzeComplaintFull SUCCESS 2345ms
[CMS_FULL_ANALYSIS] Успешно: 8 фактов, 1 ответчиков
```

