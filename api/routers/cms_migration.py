# START OF FILE api/routers/cms_migration.py

"""
API эндпоинты для миграции CMS (prosecutor-cms1).

Эндпоинты, которые точно повторяют формат OpenAI-ответов с сайта:
- /v1/cms/full-analysis — полный анализ жалобы (analyzeComplaintFull)
- /v1/cms/issue-tags — юридические теги по фактам (getIssueSpottingTags)
- /v1/cms/evidence-labels — классификация ссылок (getEvidenceTypeLabels)
- /v1/cms/validate-form-ai — AI-валидация формы (validateFormAI)
- /v1/cms/case-file-draft — черновик кейс-файла (generateCaseFileDraft)
- /v1/cms/generate-plan — генерация плана (generatePlanAI)
- /v1/cms/adapt-plan — адаптация плана (adaptPlanAI)
- /v1/cms/passport-ocr — OCR паспорта (extractPassportData)
"""

import logging
import time
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, status

from ..schemas import (
    CMSFullAnalysisRequest, CMSFullAnalysisResponse,
    CMSCombinedAnalysisRequest, CMSCombinedAnalysisResponse, CMSCombinedAnalysisFullData,
    IssueTagsRequest, IssueTagsResponse,
    EvidenceLabelsRequest, EvidenceLabelsResponse,
    ValidateFormAIRequest, ValidateFormAIResponse,
    CaseFileDraftRequest, CaseFileDraftResponse, CaseFileDraft,
    CaseFileDraftWithRagRequest, CaseFileDraftWithRagResponse,
    CaseFileDraftRegenerateRequest,
    GeneratePlanRequest, GeneratePlanResponse,
    AdaptPlanRequest, AdaptPlanResponse,
    PassportOCRRequest, PassportOCRResponse,
    ResolvePassportUrlRequest, ResolvePassportUrlResponse,
    FreeFormResponseRequest, FreeFormResponseResponse,
    ExtractedComplaintData, FormCheckResult, PipelineFlags,
    FormExtractedData, RespondentInfo, Recommendation, EvidenceItem,
    FormIssue, PlanPhase, PlanStep, DecisionPoint, DecisionOption, PlanChange,
    ErrorResponse
)
from ..dependencies import get_api_key_data

# Импортируем сервисы
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from services.complaint_analyzer_service import (
    analyze_complaint_full_cms,
    analyze_complaint_combined_cms,
    get_issue_spotting_tags_cms,
    get_evidence_type_labels_cms,
    validate_form_ai_cms,
    generate_case_file_draft_cms,
    generate_case_file_draft_with_rag_cms,
    generate_case_file_draft_with_feedback_cms,
    generate_plan_ai_cms,
    adapt_plan_ai_cms,
    extract_passport_data_cms,
    resolve_passport_url_cms,
    generate_free_form_response_cms
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/cms", tags=["CMS Migration"])


# === ПОЛНЫЙ АНАЛИЗ ЖАЛОБЫ ===

@router.post(
    "/full-analysis",
    response_model=CMSFullAnalysisResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="Полный анализ жалобы (CMS-совместимый)",
    description="""
Полный AI-анализ жалобы, идентичный analyzeComplaintFull() из complaintAiService.js.

**Возвращает:**
- `extractedData` — извлечённые данные (заявитель, ответчики, сложность)
- `facts` — нейтральные факты (5-12)
- `legalCharacterization` — правовая квалификация для каждого факта
- `requirements` — требования заявителя
- `formCheck` — проверка формы
- `summary` — краткое изложение
- `recommendations` — рекомендации для прокурора
- `evidence` — ссылки на доказательства
- `pipelineFlags` — флаги для выбора пайплайна

**Формат ответа 1:1 совместим с OpenAI-версией на сайте.**
"""
)
async def full_analysis_cms(
    request: CMSFullAnalysisRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Полный анализ жалобы (CMS-совместимый формат)."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/FULL_ANALYSIS] Запрос от ключа {api_key['key_prefix']}: server={request.server_id}")

        result = await analyze_complaint_full_cms(
            server_id=request.server_id,
            title=request.title,
            content=request.content
        )

        processing_time = (time.time() - start_time) * 1000

        # Конвертируем в Pydantic модели
        respondents = [
            RespondentInfo(**r) for r in result.get('extractedData', {}).get('respondents', [])
        ]

        extracted_data = ExtractedComplaintData(
            complainantName=result['extractedData'].get('complainantName'),
            respondent=result['extractedData'].get('respondent'),
            respondentPosition=result['extractedData'].get('respondentPosition'),
            respondentOrg=result['extractedData'].get('respondentOrg'),
            respondents=respondents,
            complexity=result['extractedData'].get('complexity', 'MEDIUM'),
            complaintNumber=result['extractedData'].get('complaintNumber')
        )

        form_check = FormCheckResult(**result.get('formCheck', {}))
        pipeline_flags = PipelineFlags(**result.get('pipelineFlags', {}))

        recommendations = [
            Recommendation(**r) for r in result.get('recommendations', [])
        ]
        evidence = [
            EvidenceItem(**e) for e in result.get('evidence', [])
        ]

        return CMSFullAnalysisResponse(
            extractedData=extracted_data,
            facts=result.get('facts', []),
            legalCharacterization=result.get('legalCharacterization', []),
            requirements=result.get('requirements', []),
            formCheck=form_check,
            summary=result.get('summary'),
            recommendations=recommendations,
            evidence=evidence,
            pipelineFlags=pipeline_flags,
            processing_time_ms=processing_time
        )

    except ValueError as e:
        logger.warning(f"[CMS/FULL_ANALYSIS] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[CMS/FULL_ANALYSIS] Runtime ошибка: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/FULL_ANALYSIS] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === КОМБИНИРОВАННЫЙ АНАЛИЗ (оптимизированный) ===

@router.post(
    "/combined-analysis",
    response_model=CMSCombinedAnalysisResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="Комбинированный анализ: fullAnalysis + issueTags (оптимизированный)",
    description="""
Комбинированный анализ жалобы: fullAnalysis + issueTags за **2 запроса** вместо 4.

**Оптимизация:**
- 1 запрос RAG-роутера (выбор законов для обеих задач)
- 1 запрос анализа (возвращает fullAnalysis + issueTags)

**Возвращает:**
- `fullAnalysis` — полный анализ жалобы (как /full-analysis)
- `issueTags` — юридические теги для поиска по законам
- `rag_laws_count` — количество законов из RAG-фильтра

**Экономия:** -50% запросов, -25% токенов по сравнению с раздельными вызовами.
"""
)
async def combined_analysis(
    request: CMSCombinedAnalysisRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Комбинированный анализ: fullAnalysis + issueTags за 2 запроса."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/COMBINED] Запрос от ключа {api_key['key_prefix']}: server={request.server_id}")

        result = await analyze_complaint_combined_cms(
            server_id=request.server_id,
            title=request.title,
            content=request.content
        )

        processing_time = (time.time() - start_time) * 1000

        # Конвертируем fullAnalysis в Pydantic модели
        fa = result.get('fullAnalysis', {})

        respondents = [
            RespondentInfo(**r) for r in fa.get('extractedData', {}).get('respondents', [])
        ]

        extracted_data = ExtractedComplaintData(
            complainantName=fa.get('extractedData', {}).get('complainantName'),
            respondent=fa.get('extractedData', {}).get('respondent'),
            respondentPosition=fa.get('extractedData', {}).get('respondentPosition'),
            respondentOrg=fa.get('extractedData', {}).get('respondentOrg'),
            respondents=respondents,
            complexity=fa.get('extractedData', {}).get('complexity', 'MEDIUM'),
            complaintNumber=fa.get('extractedData', {}).get('complaintNumber')
        )

        form_check = FormCheckResult(**fa.get('formCheck', {}))
        pipeline_flags = PipelineFlags(**fa.get('pipelineFlags', {}))

        recommendations = [
            Recommendation(**r) for r in fa.get('recommendations', [])
        ]
        evidence = [
            EvidenceItem(**e) for e in fa.get('evidence', [])
        ]

        full_analysis_data = CMSCombinedAnalysisFullData(
            extractedData=extracted_data,
            facts=fa.get('facts', []),
            legalCharacterization=fa.get('legalCharacterization', []),
            requirements=fa.get('requirements', []),
            formCheck=form_check,
            summary=fa.get('summary'),
            recommendations=recommendations,
            evidence=evidence,
            pipelineFlags=pipeline_flags
        )

        return CMSCombinedAnalysisResponse(
            fullAnalysis=full_analysis_data,
            issueTags=result.get('issueTags', []),
            processing_time_ms=processing_time,
            rag_laws_count=result.get('rag_laws_count', 0)
        )

    except ValueError as e:
        logger.warning(f"[CMS/COMBINED] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[CMS/COMBINED] Runtime ошибка: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/COMBINED] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ЮРИДИЧЕСКИЕ ТЕГИ ===

@router.post(
    "/issue-tags",
    response_model=IssueTagsResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Юридические теги по фактам",
    description="""
Получение юридических тегов для поиска по законам (аналог getIssueSpottingTags).

**Входные данные:** массив фактов (5-50)

**Выходные данные:** массив тегов (3-15), например:
- "вымогательство взятки"
- "незаконное задержание"
- "изъятие имущества"
"""
)
async def issue_tags(
    request: IssueTagsRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Получение юридических тегов по фактам."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/ISSUE_TAGS] Запрос от ключа {api_key['key_prefix']}: {len(request.facts)} фактов, server_id={request.server_id}")

        result = await get_issue_spotting_tags_cms(request.facts, request.server_id)

        processing_time = (time.time() - start_time) * 1000

        return IssueTagsResponse(
            tags=result.get('tags', []),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/ISSUE_TAGS] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === КЛАССИФИКАЦИЯ ССЫЛОК ===

@router.post(
    "/evidence-labels",
    response_model=EvidenceLabelsResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Классификация типов ссылок на доказательства",
    description="""
Классификация ссылок на доказательства по контексту текста (аналог getEvidenceTypeLabels).

**Возвращает для каждой ссылки:**
- `url` — точная ссылка
- `typeLabel` — тип (видео 20 минут, скриншот переписки и т.д.)
"""
)
async def evidence_labels(
    request: EvidenceLabelsRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Классификация типов ссылок на доказательства."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/EVIDENCE_LABELS] Запрос от ключа {api_key['key_prefix']}: {len(request.urls)} ссылок")

        result = await get_evidence_type_labels_cms(request.text, request.urls)

        processing_time = (time.time() - start_time) * 1000

        labels = [EvidenceItem(**item) for item in result.get('labels', [])]

        return EvidenceLabelsResponse(
            labels=labels,
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/EVIDENCE_LABELS] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === AI-ВАЛИДАЦИЯ ФОРМЫ ===

@router.post(
    "/validate-form-ai",
    response_model=ValidateFormAIResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="AI-валидация формы обращения",
    description="""
Глубокая AI-валидация формы обращения (аналог validateFormAI).

**Возвращает:**
- `isValid` — соответствует ли регламенту
- `confidenceScore` — уверенность 0-100
- `formQuality` — EXCELLENT/GOOD/ACCEPTABLE/POOR/INVALID
- `issues` — найденные проблемы
- `recommendation` — ACCEPT/ACCEPT_WITH_NOTICE/REQUEST_CORRECTION/REJECT
"""
)
async def validate_form_ai(
    request: ValidateFormAIRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """AI-валидация формы обращения."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/VALIDATE_FORM] Запрос от ключа {api_key['key_prefix']}")

        result = await validate_form_ai_cms(request.title, request.content)

        processing_time = (time.time() - start_time) * 1000

        issues = [FormIssue(**i) for i in result.get('issues', [])]
        extracted_data = FormExtractedData(**result.get('extractedData', {}))

        return ValidateFormAIResponse(
            isValid=result.get('isValid', False),
            confidenceScore=result.get('confidenceScore', 0),
            formQuality=result.get('formQuality', 'INVALID'),
            issues=issues,
            missingInfo=result.get('missingInfo', []),
            contradictions=result.get('contradictions', []),
            extractedData=extracted_data,
            recommendation=result.get('recommendation', 'REJECT'),
            summary=result.get('summary', ''),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/VALIDATE_FORM] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ЧЕРНОВИК КЕЙС-ФАЙЛА ===

@router.post(
    "/case-file-draft",
    response_model=CaseFileDraftResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Генерация черновика кейс-файла",
    description="""
Генерация черновика кейс-файла (аналог generateCaseFileDraft).

**Требует:**
- Текст жалобы
- Факты с пометкой прокурора (TRUE/FALSE)
- Тексты релевантных статей законов

**Возвращает поля:**
- FACTS — фактические обстоятельства
- OVERVIEW_OF_VIOLATION — обзор нарушения
- LAW_CITING — применимость закона
- FACTS_CONNECTED_WITH_LAW — связь фактов со статьями
- TYPE_OF_CRIME — тип нарушения
- ARTICLES_VIOLATED — нарушенные статьи
- PENALTY — наказание
"""
)
async def case_file_draft(
    request: CaseFileDraftRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Генерация черновика кейс-файла."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/CASE_FILE_DRAFT] Запрос от ключа {api_key['key_prefix']}")

        # Конвертируем факты
        facts_with_verdict = [
            {
                'text': f.text,
                'status': f.status,
                'legalPositionText': f.legalPositionText,
                'legalPositionStatus': f.legalPositionStatus
            }
            for f in request.facts_with_verdict
        ]

        result = await generate_case_file_draft_cms(
            complaint_title=request.complaint_title,
            complaint_content=request.complaint_content,
            facts_with_verdict=facts_with_verdict,
            law_sections=request.law_sections,
            requirements=request.requirements
        )

        processing_time = (time.time() - start_time) * 1000

        draft = CaseFileDraft(**result.get('draft', {}))

        return CaseFileDraftResponse(
            draft=draft,
            sections_count=result.get('sections_count', 0),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/CASE_FILE_DRAFT] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ЧЕРНОВИК КЕЙС-ФАЙЛА С RAG ===

@router.post(
    "/case-file-draft-with-rag",
    response_model=CaseFileDraftWithRagResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Генерация черновика кейс-файла с умным RAG-фильтром",
    description="""
Генерация черновика кейс-файла с автоматическим выбором законов через AI RAG-роутер.

**Отличие от /case-file-draft:**
- НЕ требует `law_sections` — API сам выбирает релевантные законы
- Законы загружаются из GCS и передаются в контекст AI
- Возвращает список использованных законов (`selected_laws`)

**Процесс:**
1. AI RAG-роутер анализирует жалобу и выбирает релевантные законы
2. Законы передаются как файлы из GCS в контекст генерации
3. AI генерирует черновик с учётом выбранных законов

**Возвращает:**
- `draft` — 7 полей кейс-файла
- `sections_count` — количество использованных законов
- `selected_laws` — названия выбранных законов (для отладки)
"""
)
async def case_file_draft_with_rag(
    request: CaseFileDraftWithRagRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Генерация черновика кейс-файла с RAG-фильтром законов."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/CASE_FILE_DRAFT_RAG] Запрос от ключа {api_key['key_prefix']}, server={request.server_id}")

        # Конвертируем факты
        facts_with_verdict = [
            {
                'text': f.text,
                'status': f.status,
                'legalPositionText': f.legalPositionText,
                'legalPositionStatus': f.legalPositionStatus
            }
            for f in request.facts_with_verdict
        ]

        result = await generate_case_file_draft_with_rag_cms(
            server_id=request.server_id,
            complaint_title=request.complaint_title,
            complaint_content=request.complaint_content,
            facts_with_verdict=facts_with_verdict,
            requirements=request.requirements,
            pipeline_context=request.pipeline_context
        )

        processing_time = (time.time() - start_time) * 1000

        draft = CaseFileDraft(**result.get('draft', {}))

        return CaseFileDraftWithRagResponse(
            draft=draft,
            sections_count=result.get('sections_count', 0),
            selected_laws=result.get('selected_laws', []),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/CASE_FILE_DRAFT_RAG] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ПЕРЕГЕНЕРАЦИЯ ЧЕРНОВИКА С ЗАМЕЧАНИЯМИ ===

@router.post(
    "/case-file-draft-regenerate",
    response_model=CaseFileDraftWithRagResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Перегенерация черновика кейс-файла с замечаниями прокурора",
    description="""
Перегенерация черновика кейс-файла с учётом замечаний прокурора.

**Принимает:**
- Все поля из case-file-draft-with-rag
- `previous_draft` — текущий черновик (7 полей)
- `prosecutor_feedback` — текст замечаний прокурора

**Логика:**
1. AI RAG-роутер выбирает законы
2. AI получает предыдущий черновик + замечания прокурора
3. Поля, упомянутые в замечаниях, перерабатываются
4. Остальные поля остаются без изменений

**Ответ идентичен /case-file-draft-with-rag.**
"""
)
async def case_file_draft_regenerate(
    request: CaseFileDraftRegenerateRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Перегенерация черновика кейс-файла с замечаниями прокурора."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/CASE_FILE_DRAFT_REGENERATE] Запрос от ключа {api_key['key_prefix']}, server={request.server_id}")

        # Конвертируем факты
        facts_with_verdict = [
            {
                'text': f.text,
                'status': f.status,
                'legalPositionText': f.legalPositionText,
                'legalPositionStatus': f.legalPositionStatus
            }
            for f in request.facts_with_verdict
        ]

        # Конвертируем предыдущий черновик в dict
        previous_draft = {
            'FACTS': request.previous_draft.FACTS,
            'OVERVIEW_OF_VIOLATION': request.previous_draft.OVERVIEW_OF_VIOLATION,
            'LAW_CITING': request.previous_draft.LAW_CITING,
            'FACTS_CONNECTED_WITH_LAW': request.previous_draft.FACTS_CONNECTED_WITH_LAW,
            'TYPE_OF_CRIME': request.previous_draft.TYPE_OF_CRIME,
            'ARTICLES_VIOLATED': request.previous_draft.ARTICLES_VIOLATED,
            'PENALTY': request.previous_draft.PENALTY,
        }

        result = await generate_case_file_draft_with_feedback_cms(
            server_id=request.server_id,
            complaint_title=request.complaint_title,
            complaint_content=request.complaint_content,
            facts_with_verdict=facts_with_verdict,
            requirements=request.requirements,
            previous_draft=previous_draft,
            prosecutor_feedback=request.prosecutor_feedback,
            pipeline_context=request.pipeline_context
        )

        processing_time = (time.time() - start_time) * 1000

        draft = CaseFileDraft(**result.get('draft', {}))

        return CaseFileDraftWithRagResponse(
            draft=draft,
            sections_count=result.get('sections_count', 0),
            selected_laws=result.get('selected_laws', []),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/CASE_FILE_DRAFT_REGENERATE] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ГЕНЕРАЦИЯ ПЛАНА ===

@router.post(
    "/generate-plan",
    response_model=GeneratePlanResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Генерация плана работы",
    description="""
Генерация адаптивного плана работы по жалобе (аналог generatePlanAI).

**Типы планов:**
- STANDARD — стандартный
- WITH_DOCS_REQUEST — с истребованием документов
- REJECTION — отклонение
- INTERROGATION — с допросом
- COMPLEX — комплексный
"""
)
async def generate_plan(
    request: GeneratePlanRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Генерация плана работы по жалобе."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/GENERATE_PLAN] Запрос от ключа {api_key['key_prefix']}")

        # Формируем контекст
        context = {
            'complaint': request.complaint.model_dump(),
            'analysis': request.analysis.model_dump(),
            'defendantHistory': request.defendantHistory.model_dump()
        }

        result = await generate_plan_ai_cms(context)

        processing_time = (time.time() - start_time) * 1000

        # Конвертируем фазы
        phases = []
        for phase_data in result.get('phases', []):
            steps = []
            for step_data in phase_data.get('steps', []):
                steps.append(PlanStep(
                    order=step_data.get('order', 0),
                    type=step_data.get('type', 'CUSTOM'),
                    title=step_data.get('title', ''),
                    description=step_data.get('description', ''),
                    documentType=step_data.get('documentType'),
                    prefillData=step_data.get('prefillData'),
                    choices=step_data.get('choices'),
                    deadline=step_data.get('deadline'),
                    isOptional=step_data.get('isOptional', False),
                    conditions=step_data.get('conditions')
                ))
            phases.append(PlanPhase(
                name=phase_data.get('name', ''),
                description=phase_data.get('description', ''),
                steps=steps
            ))

        # Конвертируем decisionPoints
        decision_points = []
        for dp_data in result.get('decisionPoints', []):
            options = []
            for opt_data in dp_data.get('options', []):
                options.append(DecisionOption(
                    choice=opt_data.get('choice', ''),
                    addSteps=opt_data.get('addSteps', [])
                ))
            decision_points.append(DecisionPoint(
                afterStep=dp_data.get('afterStep', 0),
                question=dp_data.get('question', ''),
                options=options
            ))

        return GeneratePlanResponse(
            planType=result.get('planType', 'STANDARD'),
            estimatedDuration=result.get('estimatedDuration', '3-5 дней'),
            complexity=result.get('complexity', 'MEDIUM'),
            phases=phases,
            decisionPoints=decision_points,
            riskFactors=result.get('riskFactors', []),
            recommendations=result.get('recommendations', []),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/GENERATE_PLAN] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === АДАПТАЦИЯ ПЛАНА ===

@router.post(
    "/adapt-plan",
    response_model=AdaptPlanResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Адаптация плана работы",
    description="""
Адаптация плана работы с учётом новой информации (аналог adaptPlanAI).

**Действия изменений:**
- ADD — добавить шаг
- REMOVE — удалить шаг
- MODIFY — изменить шаг
- SKIP — пропустить шаг
"""
)
async def adapt_plan(
    request: AdaptPlanRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Адаптация плана работы."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/ADAPT_PLAN] Запрос от ключа {api_key['key_prefix']}")

        result = await adapt_plan_ai_cms(
            current_plan=request.current_plan,
            new_information=request.new_information,
            completed_steps=request.completed_steps
        )

        processing_time = (time.time() - start_time) * 1000

        changes = [
            PlanChange(
                action=c.get('action', 'MODIFY'),
                stepOrder=c.get('stepOrder'),
                stepData=c.get('stepData'),
                reason=c.get('reason', '')
            )
            for c in result.get('changes', [])
        ]

        return AdaptPlanResponse(
            needsAdaptation=result.get('needsAdaptation', False),
            reason=result.get('reason', ''),
            changes=changes,
            newEstimatedDuration=result.get('newEstimatedDuration'),
            warnings=result.get('warnings', []),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/ADAPT_PLAN] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === OCR ПАСПОРТА ===

@router.post(
    "/passport-ocr",
    response_model=PassportOCRResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="OCR паспорта",
    description="""
Извлечение данных из изображения паспорта (аналог extractPassportData).

**Возвращает:**
- fullName — ФИО
- passportNumber — номер паспорта
- dateOfBirth — дата рождения (YYYY-MM-DD)
- gender — пол
- maritalStatus — семейное положение
- issuedDate — дата выдачи
- confidence — уверенность 0-100
- isValid — валидный ли документ
"""
)
async def passport_ocr(
    request: PassportOCRRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """OCR паспорта."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/PASSPORT_OCR] Запрос от ключа {api_key['key_prefix']}, URL: {request.image_url[:100]}...")

        result = await extract_passport_data_cms(request.image_url)

        processing_time = (time.time() - start_time) * 1000

        return PassportOCRResponse(
            fullName=result.get('fullName'),
            passportNumber=result.get('passportNumber'),
            dateOfBirth=result.get('dateOfBirth'),
            gender=result.get('gender'),
            maritalStatus=result.get('maritalStatus'),
            issuedDate=result.get('issuedDate'),
            photoDescription=result.get('photoDescription'),
            confidence=result.get('confidence', 0),
            readabilityIssues=result.get('readabilityIssues', []),
            isValid=result.get('isValid', False),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/PASSPORT_OCR] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ОПРЕДЕЛЕНИЕ ПАСПОРТА СРЕДИ ССЫЛОК ===

@router.post(
    "/resolve-passport-url",
    response_model=ResolvePassportUrlResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
    summary="Определение ссылки на паспорт",
    description="""
Определение какая из ссылок ведёт на ксерокопию паспорта (аналог resolvePassportUrlFromCandidates).

**Используется когда:**
- В обращении несколько ссылок
- Нужно найти среди них ксерокопию паспорта (п. 6 формы)

**Возвращает:**
- passportUrl — URL паспорта или null, если не найден
"""
)
async def resolve_passport_url(
    request: ResolvePassportUrlRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Определение ссылки на паспорт среди нескольких URL."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/RESOLVE_PASSPORT] Запрос от ключа {api_key['key_prefix']}: {len(request.urls)} ссылок")

        result = await resolve_passport_url_cms(request.content, request.urls)

        processing_time = (time.time() - start_time) * 1000

        return ResolvePassportUrlResponse(
            passportUrl=result.get('passportUrl'),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/RESOLVE_PASSPORT] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# === ГЕНЕРАЦИЯ СВОБОДНОЙ ФОРМЫ ОТВЕТА ===

@router.post(
    "/generate-free-form-response",
    response_model=FreeFormResponseResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Невалидные входные данные"},
        401: {"model": ErrorResponse, "description": "Отсутствует или невалидный API-ключ"},
        500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервера"},
        503: {"model": ErrorResponse, "description": "AI-сервис временно недоступен"},
    },
    summary="Генерация ответа в свободной форме (option_5)",
    description="""
AI-генерация официального ответа на обращение в свободной форме.

**Когда использовать:**
- Когда стандартные шаблоны (принять/отклонить/завершить) не подходят
- Для нестандартных ситуаций требующих индивидуального ответа
- Для информационных ответов без привязки к конкретному решению

**Входные данные:**
- `complaint_number` — номер обращения
- `complainant` — ФИО заявителя
- `complaint_summary` — краткое содержание обращения
- `prosecutor_instructions` — что именно написать в ответе (инструкции прокурора)
- `use_laws_rag` — подтянуть релевантные законы в контекст AI
- `attorney`, `rank` — данные прокурора для подписи

**Возвращает:**
- `main_text` — сгенерированный текст ответа (готовый для вставки в документ)

**Перегенерация:**
- При повторном вызове можно передать `regenerate_feedback` с указанием что исправить
"""
)
async def generate_free_form_response(
    request: FreeFormResponseRequest,
    api_key: Dict = Depends(get_api_key_data)
):
    """Генерация ответа в свободной форме (option_5)."""
    try:
        start_time = time.time()

        logger.info(f"[CMS/FREE_FORM] Запрос от ключа {api_key['key_prefix']}: complaint={request.complaint_number}")

        result = await generate_free_form_response_cms(
            server_id=request.server_id,
            complaint_number=request.complaint_number,
            complainant=request.complainant,
            complaint_summary=request.complaint_summary,
            prosecutor_instructions=request.prosecutor_instructions,
            use_laws_rag=request.use_laws_rag,
            attorney=request.attorney,
            rank=request.rank,
            regenerate_feedback=request.regenerate_feedback
        )

        processing_time = (time.time() - start_time) * 1000

        return FreeFormResponseResponse(
            main_text=result.get('main_text', ''),
            processing_time_ms=processing_time
        )

    except ValueError as e:
        logger.warning(f"[CMS/FREE_FORM] Ошибка валидации: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        logger.error(f"[CMS/FREE_FORM] Runtime ошибка: {e}")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.error(f"[CMS/FREE_FORM] Ошибка: {e}", exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")


# END OF FILE api/routers/cms_migration.py

