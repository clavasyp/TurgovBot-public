# START OF FILE api/schemas.py

"""
Pydantic-модели для валидации запросов и ответов API.
"""

from typing import Optional, Dict, List, Literal
from pydantic import BaseModel, Field
from datetime import datetime


# === QA (Ответы на вопросы) ===

class AskRequest(BaseModel):
    """Запрос на получение ответа на юридический вопрос."""
    server_id: str = Field(..., description="ID сервера (например, 'majestic_1')")
    question: str = Field(..., min_length=3, max_length=4000, description="Вопрос пользователя")
    answer_type: Literal["detailed", "brief"] = Field(
        default="detailed",
        description="Тип ответа: detailed (развёрнутый) или brief (краткий)"
    )


class AskResponse(BaseModel):
    """Ответ на юридический вопрос."""
    answer: str = Field(..., description="Ответ AI")
    server_id: str = Field(..., description="ID сервера")
    processing_time_ms: float = Field(..., description="Время обработки в миллисекундах")


# === Overlay (голосовой ассистент) ===

class AdviceRequest(BaseModel):
    """Запрос на короткий совет по ситуации (из транскрипта оверлея)."""
    server_id: str = Field(..., description="ID сервера (например, 'orlando')")
    situation: str = Field(..., min_length=3, max_length=4000,
                           description="Ситуация/вопрос (реплика собеседника из транскрипта)")
    history: Optional[str] = Field(None, max_length=6000,
                                   description="Контекст разговора (последние реплики)")
    deep: bool = Field(False, description="Глубокий разбор (Pro-модель вместо Flash)")
    top_k: Optional[int] = Field(None, ge=1, le=20, description="Сколько норм брать в контекст")
    mode: Optional[str] = Field(None, max_length=20, description="Роль: advocate | prosecutor | general")
    project_id: Optional[str] = Field(None, max_length=40,
                                      description="ID игрового проекта — для «Обзвона» выбирает свод правил "
                                                  "(majestic_rp | gta5rp). None → единый свод Majestic")
    session_id: Optional[str] = Field(None, max_length=64, description="ID сессии для rolling-памяти диалога")
    history_limit: Optional[int] = Field(None, ge=0, le=10,
                                         description="Сколько последних сообщений сессии помнить (0..10)")
    model_tier: Optional[str] = Field(None, max_length=10,
                                      description="A/B быстрой модели: 'flash' | 'lite'. None → дефолт сервера")


class AdviceResponse(BaseModel):
    """Короткий совет по ситуации со ссылками на статьи."""
    advice: str = Field(..., description="Текст совета")
    sources: List[str] = Field(default_factory=list, description="Номера статей-источников (RAG)")
    server_id: str = Field(..., description="ID сервера")
    chunks_used: int = Field(0, description="Сколько фрагментов законки попало в контекст")
    processing_time_ms: float = Field(..., description="Время обработки в миллисекундах")


class ChecklistRequest(BaseModel):
    """Запрос на оценку чек-листа процедуры под роль (живой чек-лист оверлея)."""
    server_id: str = Field(..., description="ID сервера")
    situation: str = Field(..., min_length=3, max_length=4000, description="Ситуация/описание происходящего")
    history: Optional[str] = Field(None, max_length=6000, description="Контекст разговора")
    mode: Optional[str] = Field(None, max_length=20, description="Роль: advocate | prosecutor")
    session_id: Optional[str] = Field(None, max_length=64, description="ID сессии для rolling-памяти")
    top_k: Optional[int] = Field(None, ge=1, le=20, description="Сколько норм брать в контекст")
    history_limit: Optional[int] = Field(None, ge=0, le=10,
                                         description="Сколько последних сообщений сессии помнить (0..10)")


class ChecklistItem(BaseModel):
    """Пункт чек-листа с оценённым статусом."""
    id: str = Field(..., description="ID пункта")
    text: str = Field(..., description="Текст пункта")
    status: str = Field("unclear", description="ok | violation | unclear")
    note: str = Field("", description="Короткая пометка")


class ChecklistResponse(BaseModel):
    """Чек-лист процедуры под роль с оценкой статусов."""
    server_id: str = Field(..., description="ID сервера")
    mode: Optional[str] = Field(None, description="Роль")
    items: List[ChecklistItem] = Field(default_factory=list, description="Пункты чек-листа")


# === Overlay Q&A (быстрый вопрос-ответ голосом, движок как у ТГ-бота) ===

class QaRequest(BaseModel):
    """Запрос быстрого Q&A из оверлея (вопрос голосом → короткий ответ по законке сервера)."""
    server_id: str = Field(..., description="ID сервера законки (выбирается в оверлее)")
    question: str = Field(..., min_length=1, max_length=4000, description="Вопрос пользователя (из транскрипта)")
    history: Optional[str] = Field(None, max_length=6000, description="Контекст прошлых реплик сессии (follow-up)")
    session_id: Optional[str] = Field(None, max_length=64, description="ID сессии для rolling-памяти")
    history_limit: Optional[int] = Field(None, ge=0, le=10,
                                         description="Сколько последних сообщений сессии помнить (0..10)")


class QaResponse(BaseModel):
    """Краткий ответ Q&A (как у бота)."""
    answer: str = Field(..., description="Ответ AI")
    server_id: str = Field(..., description="ID сервера")
    processing_time_ms: float = Field(..., description="Время обработки в миллисекундах")


class OverlayServerInfo(BaseModel):
    """Сервер законки для пикера в оверлее."""
    server_id: str = Field(..., description="ID сервера")
    display_name: str = Field(..., description="Отображаемое имя")
    rag: bool = Field(False, description="Есть ли RAG-свод (law_chunks) → доступны роли/совет (Адвокат/Прокурор/Общий)")
    search: bool = Field(False, description="Доступен ли умный поиск по законке (каталог построен + тир допущен)")


class OverlayProjectInfo(BaseModel):
    """Проект с его серверами (для каскадного выбора, как в боте)."""
    project_id: str = Field(..., description="ID проекта")
    display_name: str = Field(..., description="Отображаемое имя проекта")
    servers: List[OverlayServerInfo] = Field(default_factory=list, description="Серверы проекта")


class ServersResponse(BaseModel):
    """Список проектов→серверов законки для выбора в Q&A-режиме оверлея."""
    projects: List[OverlayProjectInfo] = Field(default_factory=list, description="Проекты с серверами")


# === Умный поиск по законке (/v1/overlay/laws/*) ===

class LawSearchRequest(BaseModel):
    """Поиск статей: голосовой/текстовый запрос → карточки. Без LLM в рантайме."""
    server_id: str = Field(..., description="ID сервера законки")
    query: str = Field(..., min_length=1, max_length=200, description="Запрос («какая статья за халатность», «12.1»)")
    limit: int = Field(8, ge=1, le=20, description="Максимум карточек в выдаче")


class LawRelatedItem(BaseModel):
    """Элемент «ещё релевантное» (предрассчитан офлайн)."""
    id: int = Field(..., description="ID статьи каталога")
    code: Optional[str] = Field(None, description="Номер статьи («12.1»)")
    title: Optional[str] = Field(None, description="Название статьи")
    doc_title: Optional[str] = Field(None, description="Кодекс/документ-источник")
    penalty_summary: Optional[str] = Field(None, description="Краткая санкция")
    score: float = Field(..., description="Похожесть (0..1)")


class LawArticleCard(BaseModel):
    """Карточка статьи в выдаче поиска."""
    id: int = Field(..., description="ID статьи каталога")
    code: Optional[str] = Field(None, description="Номер статьи («12.1»)")
    kind: str = Field("article", description="Тип: article|chapter|section|clause|other")
    title: Optional[str] = Field(None, description="Название статьи")
    doc_title: Optional[str] = Field(None, description="Кодекс/документ-источник")
    article_ref: Optional[str] = Field(None, description="Сырой заголовок (для копирования в рапорт)")
    penalty_summary: Optional[str] = Field(None, description="Краткая санкция для карточки")
    snippet: str = Field("", description="Начало текста статьи (полный — GET /laws/article/{id})")
    score: float = Field(..., description="Скор слияния (RRF)")
    sources: List[str] = Field(
        default_factory=list,
        description="Ноги, нашедшие статью (code/title/synonym_exact/synonym/fts/trgm/vector)",
    )


class LawSearchResponse(BaseModel):
    """Выдача умного поиска."""
    query: str = Field(..., description="Исходный запрос")
    results: List[LawArticleCard] = Field(default_factory=list, description="Карточки статей")
    related: List[LawRelatedItem] = Field(default_factory=list, description="Ещё релевантное (к топ-результату)")
    degraded: bool = Field(False, description="true = одна из частей поиска дала сбой/таймаут или FRIDA-индекс неполон")
    search_mode: Literal["full", "partial", "keyword_only"] = Field(
        "full",
        description=(
            "full = все применимые части исправны; partial = часть применимых частей недоступна; "
            "keyword_only = смысловой vector был нужен, но FRIDA недоступна"
        ),
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="Машиночитаемые причины неполного ответа (например vector_timeout, related_error)",
    )


class LawArticleResponse(BaseModel):
    """Полная статья (раскрытие карточки)."""
    id: int
    server_id: str
    code: Optional[str] = None
    kind: str = "article"
    title: Optional[str] = None
    doc_title: Optional[str] = None
    article_ref: Optional[str] = None
    penalty_summary: Optional[str] = None
    penalty_json: Optional[dict] = Field(None, description="Структура санкции (fine/prison/arrest/wanted_level/other)")
    full_text: str = Field(..., description="Полный текст статьи")
    related: List[LawRelatedItem] = Field(default_factory=list)


# === Criminal Case Writer (Создание уголовных дел) ===

class CCWStartRequest(BaseModel):
    """Запрос на создание новой сессии уголовного дела."""
    server_id: str = Field(..., description="ID сервера")
    case_number: str = Field(..., pattern=r"^[A-Z]{2,5}-\d{1,5}$", description="Номер дела (например, 'DJR-001')")


class CCWStartResponse(BaseModel):
    """Ответ с информацией о созданной сессии."""
    session_id: int = Field(..., description="ID сессии")
    status: str = Field(..., description="Статус сессии")
    case_number: str = Field(..., description="Номер дела")


class CCWFactsRequest(BaseModel):
    """Запрос на добавление фактов дела."""
    facts: str = Field(..., min_length=10, max_length=10000, description="Описание обстоятельств дела")


class CCWFactsResponse(BaseModel):
    """Ответ после добавления фактов."""
    session_id: int
    status: str
    message: str


class CCWAnalyzeRequest(BaseModel):
    """Запрос на анализ (Phase A) - получение уточняющих вопросов."""
    part_type: Literal[
        "inventory", "accused_card", "case_materials",
        "indictment", "summons_list", "prosecution_demands"
    ] = Field(..., description="Тип части дела для анализа")


class CCWAnalyzeResponse(BaseModel):
    """Ответ с анализом и уточняющими вопросами (Phase A)."""
    session_id: int
    part_type: str
    analysis: str = Field(..., description="Анализ контекста")
    questions: List[str] = Field(default_factory=list, description="Уточняющие вопросы (если есть)")
    has_questions: bool = Field(..., description="Есть ли уточняющие вопросы")
    processing_time_ms: float


class CCWAnswerRequest(BaseModel):
    """Запрос на отправку ответов на уточняющие вопросы."""
    part_type: Literal[
        "inventory", "accused_card", "case_materials",
        "indictment", "summons_list", "prosecution_demands"
    ] = Field(..., description="Тип части дела")
    answers: List[str] = Field(..., min_length=1, description="Ответы на уточняющие вопросы")


class CCWAnswerResponse(BaseModel):
    """Ответ после сохранения ответов."""
    session_id: int
    part_type: str
    status: str
    message: str


class CCWGenerateRequest(BaseModel):
    """Запрос на генерацию части дела."""
    part_type: Literal[
        "inventory", "accused_card", "case_materials",
        "indictment", "summons_list", "prosecution_demands"
    ] = Field(..., description="Тип части дела для генерации")
    skip_questions: bool = Field(default=False, description="Пропустить уточняющие вопросы и сразу генерировать")


class CCWGenerateResponse(BaseModel):
    """Ответ с сгенерированной частью дела."""
    session_id: int
    part_type: str
    content: str = Field(..., description="Сгенерированный контент")
    version: int = Field(..., description="Версия части")
    processing_time_ms: float


class CCWPartInfo(BaseModel):
    """Информация о части дела."""
    part_type: str
    content: Optional[str] = None
    version: int = 0
    is_final: bool = False


class CCWCaseResponse(BaseModel):
    """Полная информация о сессии уголовного дела."""
    session_id: int
    case_number: str
    server_id: str
    status: str
    created_at: datetime
    parts: Dict[str, CCWPartInfo] = Field(default_factory=dict, description="Части дела")


# === Общие модели ===

class HealthResponse(BaseModel):
    """Ответ health check."""
    status: str = "ok"
    version: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ErrorResponse(BaseModel):
    """Модель ошибки."""
    error: str
    detail: Optional[str] = None
    code: str


class ApiKeyInfo(BaseModel):
    """Информация об API-ключе (для отладки, без секретов)."""
    key_prefix: str
    name: str
    key_type: str
    is_active: bool
    requests_per_minute: int
    total_requests: int


# === Complaint Analyzer API ===

class ComplaintParseRequest(BaseModel):
    """Запрос на парсинг жалобы по URL."""
    complaint_url: str = Field(..., description="URL жалобы на форуме")


class ComplaintParseResponse(BaseModel):
    """Ответ с распарсенным текстом жалобы."""
    title: str = Field(..., description="Заголовок темы")
    content: str = Field(..., description="Текст жалобы")
    thread_id: Optional[str] = Field(None, description="ID темы на форуме")


class ComplaintCheckFormRequest(BaseModel):
    """Запрос на проверку формы жалобы."""
    server_id: str = Field(..., description="ID сервера")
    complaint_text: str = Field(..., min_length=10, description="Текст жалобы для проверки")


class ComplaintCheckFormResponse(BaseModel):
    """Ответ проверки формы жалобы."""
    is_valid: bool = Field(..., description="Корректна ли форма")
    explanation: str = Field(..., description="Пояснение (для постановления об отказе)")
    issues: List[str] = Field(default_factory=list, description="Коды проблем из issue_types")
    processing_time_ms: float = Field(..., description="Время обработки в мс")


class ComplaintSubject(BaseModel):
    """Информация о субъекте жалобы."""
    target_type: Optional[str] = Field(None, description="'person' | 'organization'")
    nickname: Optional[str] = Field(None, description="Никнейм игрока")
    organization: Optional[str] = Field(None, description="Название организации")
    position: Optional[str] = Field(None, description="Должность")


class ComplaintAnalyzeRequest(BaseModel):
    """Запрос на анализ жалобы."""
    server_id: str = Field(..., description="ID сервера")
    complaint_text: str = Field(..., min_length=10, description="Текст жалобы для анализа")


class ComplaintAnalyzeResponse(BaseModel):
    """Ответ анализа жалобы."""
    complexity: str = Field(..., description="'low' | 'medium' | 'high'")
    needs_additional_materials: bool = Field(..., description="Нужны ли доп. материалы")
    subject: ComplaintSubject = Field(..., description="На кого жалоба")
    reviewer_level: str = Field(..., description="'any' | 'sgp' | 'zgp' | 'gp'")
    processing_time_ms: float = Field(..., description="Время обработки в мс")


class ComplaintExtractFactsRequest(BaseModel):
    """Запрос на извлечение фактов из жалобы."""
    server_id: str = Field(..., description="ID сервера")
    complaint_text: str = Field(..., min_length=10, description="Текст жалобы")


class ComplaintExtractFactsResponse(BaseModel):
    """Ответ с извлечёнными фактами."""
    facts: List[str] = Field(..., description="Юридически значимые факты")
    requirements: List[str] = Field(..., description="Требования заявителя")
    processing_time_ms: float = Field(..., description="Время обработки в мс")


class FormTemplateSetRequest(BaseModel):
    """Запрос на установку шаблона формы."""
    server_id: str = Field(..., description="ID сервера")
    form_url: str = Field(..., description="URL темы с образцом формы")


class FormTemplateResponse(BaseModel):
    """Ответ с информацией о шаблоне формы."""
    server_id: str
    form_url: Optional[str] = None
    form_content: Optional[str] = None
    updated_at: Optional[datetime] = None


class IssueType(BaseModel):
    """Тип проблемы формы."""
    code: str = Field(..., description="Код (snake_case)")
    name: str = Field(..., description="Название")
    description: Optional[str] = Field(None, description="Описание")
    is_required: bool = Field(False, description="Обязательное поле")


class IssueTypeCreateRequest(BaseModel):
    """Запрос на создание типа проблемы."""
    server_id: str = Field(..., description="ID сервера")
    code: str = Field(..., pattern=r'^[a-z][a-z0-9_]*$', description="Код (snake_case, латиница)")
    name: str = Field(..., min_length=1, max_length=255, description="Название")
    description: Optional[str] = Field(None, description="Описание")
    is_required: bool = Field(False, description="Обязательное поле")


class IssueTypeUpdateRequest(BaseModel):
    """Запрос на обновление типа проблемы."""
    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Название")
    description: Optional[str] = Field(None, description="Описание")
    is_required: Optional[bool] = Field(None, description="Обязательное поле")


class IssueTypeListResponse(BaseModel):
    """Список типов проблем для сервера."""
    server_id: str
    issue_types: List[IssueType]


# === Full Analysis (единый анализ) ===

class ComplaintFullAnalysisRequest(BaseModel):
    """Запрос на полный анализ жалобы (все виды анализа за один вызов)."""
    server_id: str = Field(..., description="ID сервера (например, 'la_puerta')")
    complaint_text: str = Field(..., min_length=10, description="Текст жалобы")
    title: Optional[str] = Field(None, description="Заголовок жалобы")
    defendant: Optional[str] = Field(None, description="Ответчик")
    complainant: Optional[str] = Field(None, description="Заявитель")


class ComplaintFullAnalysisResponse(BaseModel):
    """Ответ полного анализа жалобы."""
    check_form: ComplaintCheckFormResponse = Field(..., description="Результат проверки формы")
    analysis: ComplaintAnalyzeResponse = Field(..., description="Результат анализа сложности/субъекта")
    facts: ComplaintExtractFactsResponse = Field(..., description="Извлечённые факты и требования")
    processing_time_ms: float = Field(..., description="Общее время обработки в мс")


# === Document Generation (Генерация текста документов) ===

class DocumentTextBlocks(BaseModel):
    """Блоки текста для документа (разбитые по зонам редактирования)."""
    header_0: str = Field(..., description="Дата (26th of January 2026)")
    main_0: str = Field(..., description="Re: обращение... + Dear Mr...")
    main_1: str = Field(..., description="Основной текст документа")
    bottom_0: str = Field(..., description="Sincerely, + подпись")


class ComplaintDocumentGenerateRequest(BaseModel):
    """Запрос на AI-генерацию текста документа ответа на жалобу."""
    server_id: str = Field(..., description="ID сервера")
    document_type: Literal["accept", "reject", "close"] = Field(
        ...,
        description="Тип документа: accept (принять), reject (отклонить), close (завершить)"
    )
    # Контекст жалобы
    complaint_text: Optional[str] = Field(None, description="Текст жалобы")
    complaint_summary: Optional[str] = Field(None, description="AI-резюме жалобы")
    case_number: str = Field(..., description="Номер обращения")
    complainant: str = Field(..., description="ФИО заявителя")

    # Данные прокурора
    attorney: str = Field(..., description="ФИО прокурора")
    rank: str = Field("Прокурор", description="Должность прокурора")

    # Данные для reject
    rejection_reason: Optional[str] = Field(None, description="header_violation или text_violation")
    rejection_issues: Optional[List[str]] = Field(None, description="Список кодов проблем")

    # Данные для close
    defendant: Optional[str] = Field(None, description="ФИО ответчика")
    conclusion_type: Optional[str] = Field(None, description="court, fine, impossible, expired")
    charges: Optional[str] = Field(None, description="Статья обвинения")
    charges_type: Optional[str] = Field(None, description="criminal, administrative, ethical")
    sanction_sum: Optional[str] = Field(None, description="Сумма штрафа")
    jurisdiction_court: Optional[str] = Field(None, description="SC или FC")
    is_complainant_victim: bool = Field(False, description="Заявитель потерпевший")

    additional_context: Optional[str] = Field(None, description="Дополнительный контекст")


class ComplaintDocumentGenerateResponse(BaseModel):
    """Ответ с сгенерированным текстом документа."""
    text_blocks: DocumentTextBlocks = Field(..., description="Текст по зонам")
    raw_text: str = Field(..., description="Полный текст для копирования")
    document_type: str = Field(..., description="Тип документа")
    processing_time_ms: float = Field(..., description="Время обработки")


# === AGO Document AI Generation ===

class AgoGenerateTextRequest(BaseModel):
    """Запрос на AI-генерацию текста AGO-документа (постановления)."""
    server_id: str = Field(..., description="ID сервера (для загрузки законов)")
    ago_type: Literal[
        "REQUEST_INFO", "INITIATE_CASE", "CLOSE_CASE",
        "FINE_ORDER", "DISMISSAL_ORDER", "INTERROGATION_ORDER", "CANCEL_ORDER"
    ] = Field(..., description="Тип постановления")

    # Данные формы
    attorney: str = Field(..., description="ФИО прокурора")
    rank: str = Field("Прокурор", description="Должность прокурора")
    defendant: str = Field(..., description="ФИО ответчика/обвиняемого")
    complainant: Optional[str] = Field(None, description="ФИО заявителя")
    organization: Optional[str] = Field(None, description="Организация (LSPD, LSSD, FIB и т.д.)")
    case_number: Optional[str] = Field(None, description="Номер обращения/дела")
    discord_tag: Optional[str] = Field(None, description="Discord тег прокурора для связи")

    # Данные инцидента
    incident_date: Optional[str] = Field(None, description="Дата инцидента (DD.MM)")
    incident_time: Optional[str] = Field(None, description="Время инцидента (HH:MM)")

    # Type-specific поля
    arrest_type: Optional[str] = Field(None, description="Для REQUEST_INFO: arrest, suspension, ticket_sanction, fired, reprimand, severe_reprimand")
    charges: Optional[str] = Field(None, description="Статья обвинения")
    charges_type: Optional[Literal["criminal", "administrative", "ethical"]] = Field(None, description="Тип обвинения")
    is_witness: bool = Field(False, description="Прокурор — свидетель нарушения")
    finish_reason: Optional[Literal["court", "archive", "impossible"]] = Field(None, description="Для CLOSE_CASE: причина завершения")
    fine_amount: Optional[str] = Field(None, description="Для FINE_ORDER: сумма штрафа")
    dismissal_reason: Optional[Literal["criminal_record", "violation_of_law"]] = Field(None, description="Для DISMISSAL_ORDER: причина увольнения")
    previous_charges: Optional[str] = Field(None, description="Для DISMISSAL_ORDER: предыдущие обвинения")
    interrogation_date: Optional[str] = Field(None, description="Для INTERROGATION_ORDER: дата допроса (DD.MM)")
    interrogation_time: Optional[str] = Field(None, description="Для INTERROGATION_ORDER: время допроса (HH:MM)")
    cancel_ago_num: Optional[str] = Field(None, description="Для CANCEL_ORDER: номер отменяемого постановления")

    # Описание ситуации для AI
    situation_description: Optional[str] = Field(None, description="Описание ситуации / текст жалобы для AI-контекста")
    complaint_id: Optional[int] = Field(None, description="ID привязанного обращения (опционально)")


class AgoTextBlocks(BaseModel):
    """Блоки текста AGO-документа для редактирования."""
    title: str = Field(..., description="Заголовок постановления")
    preamble: str = Field(..., description="Преамбула: Властью данной мне...")
    section_1: str = Field("", description="Section 1. Введение")
    section_2: str = Field("", description="Section 2. Резолюция")
    section_3: Optional[str] = Field(None, description="Section 3 (опционально)")
    section_4: Optional[str] = Field(None, description="Section 4 (опционально)")


class AgoGenerateTextResponse(BaseModel):
    """Ответ AI-генерации текста AGO-документа."""
    title: str = Field(..., description="Заголовок постановления")
    main_text: str = Field(..., description="Основной текст постановления (для обратной совместимости)")
    text_blocks: Optional[AgoTextBlocks] = Field(None, description="Блоки текста для редактирования")
    ago_type: str = Field(..., description="Тип постановления")
    processing_time_ms: float = Field(..., description="Время обработки")


class CustomResolutionGenerateRequest(BaseModel):
    """Запрос на AI-генерацию кастомного постановления."""
    server_id: str = Field(..., description="ID сервера (для загрузки законов)")
    user_description: str = Field(..., min_length=20, max_length=5000, description="Описание от пользователя какое постановление нужно")
    context: Optional[str] = Field(None, max_length=10000, description="Дополнительный контекст (текст жалобы, данные дела)")
    complaint_id: Optional[int] = Field(None, description="ID привязанного обращения (опционально)")


class CustomResolutionGenerateResponse(BaseModel):
    """Ответ AI-генерации кастомного постановления."""
    title: str = Field(..., description="Заголовок постановления")
    main_text: str = Field(..., description="Основной текст постановления")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Full Analysis (analyzeComplaintFull) ===

class RespondentInfo(BaseModel):
    """Информация об ответчике."""
    name: str = Field(..., description="Имя ответчика или идентификационный знак")
    position: Optional[str] = Field(None, description="Должность ответчика")
    org: Optional[str] = Field(None, description="Организация ответчика")


class ExtractedComplaintData(BaseModel):
    """Извлечённые данные из обращения."""
    complainantName: Optional[str] = Field(None, description="ФИО заявителя")
    respondent: Optional[str] = Field(None, description="Имя первого ответчика (для обратной совместимости)")
    respondentPosition: Optional[str] = Field(None, description="Должность первого ответчика")
    respondentOrg: Optional[str] = Field(None, description="Организация первого ответчика")
    respondents: List[RespondentInfo] = Field(default_factory=list, description="Массив ответчиков")
    complexity: str = Field("MEDIUM", description="Сложность: LOW, MEDIUM, HIGH")
    complaintNumber: Optional[int] = Field(None, description="Номер обращения из заголовка")


class FormCheckResult(BaseModel):
    """Результат проверки формы."""
    hasSignature: bool = Field(False, description="Есть подпись")
    hasEmail: bool = Field(False, description="Есть email")
    hasPassportCopy: bool = Field(False, description="Есть ксерокопия паспорта")
    isValid: bool = Field(False, description="Форма валидна")


class Recommendation(BaseModel):
    """Рекомендация для прокурора."""
    type: str = Field("info", description="Тип: action, warning, info")
    title: str = Field(..., description="Заголовок")
    description: str = Field(..., description="Описание")


class EvidenceItem(BaseModel):
    """Элемент доказательства."""
    url: str = Field(..., description="URL ссылки на доказательство")
    typeLabel: str = Field("Ссылка", description="Краткое описание на русском")


class PipelineFlags(BaseModel):
    """Флаги для выбора шаблона пайплайна."""
    requiresDocs: bool = Field(False, description="Нужны доп. документы от организации")
    requiresInterrogation: bool = Field(False, description="Целесообразен допрос")
    requiresCourt: bool = Field(False, description="Целесообразно направление в суд")


class CMSFullAnalysisRequest(BaseModel):
    """Запрос на полный анализ жалобы (CMS-совместимый)."""
    server_id: str = Field(..., description="ID сервера")
    title: str = Field(..., description="Заголовок жалобы")
    content: str = Field(..., min_length=10, description="Текст жалобы")


class CMSFullAnalysisResponse(BaseModel):
    """Ответ полного анализа жалобы (формат CMS)."""
    extractedData: ExtractedComplaintData = Field(..., description="Извлечённые данные")
    facts: List[str] = Field(default_factory=list, description="Нейтральные факты")
    legalCharacterization: List[str] = Field(default_factory=list, description="Правовая квалификация для каждого факта")
    requirements: List[str] = Field(default_factory=list, description="Требования заявителя")
    formCheck: FormCheckResult = Field(..., description="Проверка формы")
    summary: Optional[str] = Field(None, description="Краткое изложение")
    recommendations: List[Recommendation] = Field(default_factory=list, description="Рекомендации")
    evidence: List[EvidenceItem] = Field(default_factory=list, description="Доказательства")
    pipelineFlags: PipelineFlags = Field(..., description="Флаги пайплайна")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Combined Analysis (fullAnalysis + issueTags) ===

class CMSCombinedAnalysisRequest(BaseModel):
    """Запрос на комбинированный анализ (fullAnalysis + issueTags)."""
    server_id: str = Field(..., description="ID сервера")
    title: str = Field(..., description="Заголовок жалобы")
    content: str = Field(..., min_length=10, description="Текст жалобы")


class CMSCombinedAnalysisFullData(BaseModel):
    """Данные полного анализа (без processing_time_ms)."""
    extractedData: ExtractedComplaintData = Field(..., description="Извлечённые данные")
    facts: List[str] = Field(default_factory=list, description="Нейтральные факты")
    legalCharacterization: List[str] = Field(default_factory=list, description="Правовая квалификация")
    requirements: List[str] = Field(default_factory=list, description="Требования заявителя")
    formCheck: FormCheckResult = Field(..., description="Проверка формы")
    summary: Optional[str] = Field(None, description="Краткое изложение")
    recommendations: List[Recommendation] = Field(default_factory=list, description="Рекомендации")
    evidence: List[EvidenceItem] = Field(default_factory=list, description="Доказательства")
    pipelineFlags: PipelineFlags = Field(..., description="Флаги пайплайна")


class CMSCombinedAnalysisResponse(BaseModel):
    """Ответ комбинированного анализа."""
    fullAnalysis: CMSCombinedAnalysisFullData = Field(..., description="Полный анализ жалобы")
    issueTags: List[str] = Field(default_factory=list, description="Юридические теги для поиска")
    processing_time_ms: float = Field(..., description="Время обработки")
    rag_laws_count: int = Field(0, description="Количество законов из RAG-фильтра")


# === CMS Migration: Issue Spotting Tags ===

class IssueTagsRequest(BaseModel):
    """Запрос на получение юридических тегов."""
    facts: List[str] = Field(..., min_length=1, description="Список фактов")
    server_id: Optional[str] = Field(None, description="ID сервера для RAG-фильтра законов (опционально)")


class IssueTagsResponse(BaseModel):
    """Ответ с юридическими тегами."""
    tags: List[str] = Field(default_factory=list, description="Юридические теги для поиска")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Evidence Labels ===

class EvidenceLabelsRequest(BaseModel):
    """Запрос на классификацию ссылок на доказательства."""
    text: str = Field(..., description="Текст обращения")
    urls: List[str] = Field(..., min_length=1, description="Список URL для классификации")


class EvidenceLabelsResponse(BaseModel):
    """Ответ с типами ссылок."""
    labels: List[EvidenceItem] = Field(default_factory=list, description="Классифицированные ссылки")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Form Validation AI ===

class FormIssue(BaseModel):
    """Проблема формы."""
    severity: str = Field(..., description="Серьёзность: CRITICAL, WARNING, INFO")
    field: str = Field(..., description="Поле с проблемой")
    issue: str = Field(..., description="Описание проблемы")
    suggestion: str = Field(..., description="Предложение по исправлению")


class FormExtractedData(BaseModel):
    """Извлечённые данные из формы."""
    complainant: Optional[str] = Field(None, description="Имя заявителя")
    defendant: Optional[str] = Field(None, description="Имя нарушителя")
    organization: Optional[str] = Field(None, description="Организация нарушителя")
    incidentDate: Optional[str] = Field(None, description="Дата инцидента")
    incidentTime: Optional[str] = Field(None, description="Время инцидента")


class ValidateFormAIRequest(BaseModel):
    """Запрос на AI-валидацию формы."""
    title: str = Field(..., description="Заголовок обращения")
    content: str = Field(..., min_length=10, description="Текст обращения")


class ValidateFormAIResponse(BaseModel):
    """Ответ AI-валидации формы."""
    isValid: bool = Field(..., description="Соответствует ли регламенту")
    confidenceScore: int = Field(..., ge=0, le=100, description="Уверенность 0-100")
    formQuality: str = Field(..., description="Качество: EXCELLENT, GOOD, ACCEPTABLE, POOR, INVALID")
    issues: List[FormIssue] = Field(default_factory=list, description="Проблемы")
    missingInfo: List[str] = Field(default_factory=list, description="Недостающая информация")
    contradictions: List[str] = Field(default_factory=list, description="Противоречия")
    extractedData: FormExtractedData = Field(..., description="Извлечённые данные")
    recommendation: str = Field(..., description="ACCEPT, ACCEPT_WITH_NOTICE, REQUEST_CORRECTION, REJECT")
    summary: str = Field(..., description="Краткое заключение")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Case File Draft ===

class FactWithVerdict(BaseModel):
    """Факт с пометкой прокурора."""
    text: str = Field(..., description="Текст факта")
    status: str = Field(..., description="TRUE или FALSE")
    legalPositionText: Optional[str] = Field(None, description="Правовая квалификация")
    legalPositionStatus: Optional[str] = Field(None, description="TRUE, FALSE или null")


class CaseFileDraftRequest(BaseModel):
    """Запрос на генерацию черновика кейс-файла."""
    complaint_id: Optional[str] = Field(None, description="ID обращения")
    complaint_title: str = Field(..., description="Заголовок жалобы")
    complaint_content: str = Field(..., description="Текст жалобы")
    facts_with_verdict: List[FactWithVerdict] = Field(..., description="Факты с пометкой прокурора")
    law_sections: List[str] = Field(default_factory=list, description="Тексты релевантных статей")
    requirements: List[str] = Field(default_factory=list, description="Требования заявителя")


class CaseFileDraft(BaseModel):
    """Содержимое черновика кейс-файла."""
    FACTS: str = Field(..., description="Фактические обстоятельства")
    OVERVIEW_OF_VIOLATION: str = Field(..., description="Обзор нарушения")
    LAW_CITING: str = Field(..., description="Применимость закона")
    FACTS_CONNECTED_WITH_LAW: str = Field(..., description="Связь фактов со статьями")
    TYPE_OF_CRIME: str = Field(..., description="Тип нарушения")
    ARTICLES_VIOLATED: str = Field(..., description="Нарушенные статьи")
    PENALTY: str = Field(..., description="Наказание")


class CaseFileDraftResponse(BaseModel):
    """Ответ с черновиком кейс-файла."""
    draft: CaseFileDraft = Field(..., description="Черновик")
    sections_count: int = Field(0, description="Количество использованных статей")
    processing_time_ms: float = Field(..., description="Время обработки")


class CaseFileDraftWithRagRequest(BaseModel):
    """Запрос на генерацию черновика кейс-файла с умным RAG-фильтром."""
    server_id: str = Field(..., description="ID сервера для RAG законов")
    complaint_id: Optional[str] = Field(None, description="ID обращения (опционально)")
    complaint_title: str = Field(..., description="Заголовок жалобы")
    complaint_content: str = Field(..., max_length=15000, description="Текст жалобы")
    facts_with_verdict: List[FactWithVerdict] = Field(..., description="Факты с пометкой прокурора")
    requirements: List[str] = Field(default_factory=list, description="Требования заявителя")
    pipeline_context: Optional[str] = Field(None, max_length=10000, description="Контекст из пайплайна (комментарии, допросы, решения прокурора)")
    # law_sections НЕТ — RAG сам выберет и передаст в контекст


class CaseFileDraftWithRagResponse(BaseModel):
    """Ответ с черновиком кейс-файла (RAG-версия)."""
    draft: CaseFileDraft = Field(..., description="Черновик")
    sections_count: int = Field(0, description="Количество использованных законов")
    selected_laws: List[str] = Field(default_factory=list, description="Названия выбранных законов")
    processing_time_ms: float = Field(..., description="Время обработки")


class CaseFileDraftRegenerateRequest(BaseModel):
    """Запрос на перегенерацию черновика кейс-файла с замечаниями прокурора."""
    server_id: str = Field(..., description="ID сервера для RAG законов")
    complaint_id: Optional[str] = Field(None, description="ID обращения (опционально)")
    complaint_title: str = Field(..., description="Заголовок жалобы")
    complaint_content: str = Field(..., max_length=15000, description="Текст жалобы")
    facts_with_verdict: List[FactWithVerdict] = Field(..., description="Факты с пометкой прокурора")
    requirements: List[str] = Field(default_factory=list, description="Требования заявителя")
    previous_draft: CaseFileDraft = Field(..., description="Предыдущий черновик (7 полей)")
    prosecutor_feedback: str = Field(..., min_length=3, max_length=2000, description="Замечания прокурора")
    pipeline_context: Optional[str] = Field(None, max_length=10000, description="Контекст из пайплайна (комментарии, допросы, решения прокурора)")


# === CMS Migration: Adaptive Plan ===

class ComplaintContext(BaseModel):
    """Контекст жалобы для генерации плана."""
    number: Optional[int] = Field(None, description="Номер обращения")
    title: Optional[str] = Field(None, description="Заголовок")
    defendant: Optional[str] = Field(None, description="Ответчик")
    defendantOrg: Optional[str] = Field(None, description="Организация ответчика")
    complainant: Optional[str] = Field(None, description="Заявитель")
    complexity: Optional[str] = Field(None, description="Сложность")
    requiresDocs: bool = Field(False, description="Нужны документы")


class AnalysisContext(BaseModel):
    """Контекст анализа для плана."""
    formValidation: Optional[dict] = Field(None, description="Результат валидации формы")
    extractedFacts: List[str] = Field(default_factory=list, description="Извлечённые факты")
    legalAnalysis: Optional[dict] = Field(None, description="Правовой анализ")
    requiredInfo: Optional[dict] = Field(None, description="Требуемая информация")


class DefendantHistory(BaseModel):
    """История ответчика."""
    previousCases: int = Field(0, description="Предыдущие дела")
    previousFines: int = Field(0, description="Предыдущие штрафы")
    riskScore: Optional[int] = Field(None, description="Оценка риска")


class GeneratePlanRequest(BaseModel):
    """Запрос на генерацию плана."""
    complaint: ComplaintContext = Field(..., description="Контекст жалобы")
    analysis: AnalysisContext = Field(..., description="Контекст анализа")
    defendantHistory: DefendantHistory = Field(default_factory=DefendantHistory, description="История ответчика")


class PlanStep(BaseModel):
    """Шаг плана."""
    order: int = Field(..., description="Порядковый номер")
    type: str = Field(..., description="Тип шага")
    title: str = Field(..., description="Название")
    description: str = Field(..., description="Описание")
    documentType: Optional[str] = Field(None, description="Тип документа")
    prefillData: Optional[dict] = Field(None, description="Данные для подстановки")
    choices: Optional[List[str]] = Field(None, description="Варианты выбора")
    deadline: Optional[int] = Field(None, description="Дедлайн в часах")
    isOptional: bool = Field(False, description="Опциональный шаг")
    conditions: Optional[dict] = Field(None, description="Условия")


class PlanPhase(BaseModel):
    """Фаза плана."""
    name: str = Field(..., description="Название фазы")
    description: str = Field(..., description="Описание")
    steps: List[PlanStep] = Field(default_factory=list, description="Шаги")


class DecisionOption(BaseModel):
    """Вариант решения."""
    choice: str = Field(..., description="Выбор")
    addSteps: List[str] = Field(default_factory=list, description="Добавляемые шаги")


class DecisionPoint(BaseModel):
    """Точка принятия решения."""
    afterStep: int = Field(..., description="После какого шага")
    question: str = Field(..., description="Вопрос")
    options: List[DecisionOption] = Field(default_factory=list, description="Варианты")


class GeneratePlanResponse(BaseModel):
    """Ответ с планом работы."""
    planType: str = Field(..., description="Тип плана")
    estimatedDuration: str = Field(..., description="Оценка времени")
    complexity: str = Field(..., description="Сложность")
    phases: List[PlanPhase] = Field(default_factory=list, description="Фазы")
    decisionPoints: List[DecisionPoint] = Field(default_factory=list, description="Точки решений")
    riskFactors: List[str] = Field(default_factory=list, description="Факторы риска")
    recommendations: List[str] = Field(default_factory=list, description="Рекомендации")
    processing_time_ms: float = Field(..., description="Время обработки")


class AdaptPlanRequest(BaseModel):
    """Запрос на адаптацию плана."""
    current_plan: dict = Field(..., description="Текущий план")
    new_information: dict = Field(..., description="Новая информация")
    completed_steps: List[int] = Field(default_factory=list, description="Выполненные шаги")


class PlanChange(BaseModel):
    """Изменение плана."""
    action: str = Field(..., description="ADD, REMOVE, MODIFY, SKIP")
    stepOrder: Optional[int] = Field(None, description="Номер шага")
    stepData: Optional[dict] = Field(None, description="Данные шага")
    reason: str = Field(..., description="Причина")


class AdaptPlanResponse(BaseModel):
    """Ответ адаптации плана."""
    needsAdaptation: bool = Field(..., description="Требуется адаптация")
    reason: str = Field(..., description="Причина")
    changes: List[PlanChange] = Field(default_factory=list, description="Изменения")
    newEstimatedDuration: Optional[str] = Field(None, description="Новая оценка времени")
    warnings: List[str] = Field(default_factory=list, description="Предупреждения")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Passport OCR ===

class PassportOCRRequest(BaseModel):
    """Запрос на OCR паспорта."""
    image_url: str = Field(..., description="URL изображения паспорта")


class PassportOCRResponse(BaseModel):
    """Ответ OCR паспорта."""
    fullName: Optional[str] = Field(None, description="Имя Фамилия")
    passportNumber: Optional[str] = Field(None, description="Номер паспорта")
    dateOfBirth: Optional[str] = Field(None, description="Дата рождения YYYY-MM-DD")
    gender: Optional[str] = Field(None, description="Пол")
    maritalStatus: Optional[str] = Field(None, description="Семейное положение")
    issuedDate: Optional[str] = Field(None, description="Дата выдачи")
    photoDescription: Optional[str] = Field(None, description="Описание фото")
    confidence: int = Field(0, ge=0, le=100, description="Уверенность 0-100")
    readabilityIssues: List[str] = Field(default_factory=list, description="Проблемы читаемости")
    isValid: bool = Field(False, description="Валидный документ")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Resolve Passport URL ===

class ResolvePassportUrlRequest(BaseModel):
    """Запрос на определение ссылки на паспорт среди нескольких URL."""
    content: str = Field(..., description="Текст обращения")
    urls: List[str] = Field(..., min_length=2, description="Список URL-ссылок (минимум 2)")


class ResolvePassportUrlResponse(BaseModel):
    """Ответ с URL паспорта."""
    passportUrl: Optional[str] = Field(None, description="URL ксерокопии паспорта или null")
    processing_time_ms: float = Field(..., description="Время обработки")


# === CMS Migration: Free Form Response (option_5) ===

class FreeFormResponseRequest(BaseModel):
    """Запрос на AI-генерацию свободной формы ответа."""
    server_id: str = Field(..., description="ID сервера для RAG законов")
    complaint_number: str = Field(..., description="Номер обращения")
    complainant: str = Field(..., description="ФИО заявителя")
    complaint_summary: str = Field(..., max_length=5000, description="Краткое содержание жалобы / заголовок")
    prosecutor_instructions: str = Field(..., min_length=10, max_length=3000, description="Инструкции прокурора (что ответить)")
    use_laws_rag: bool = Field(False, description="Использовать RAG законов для генерации")
    attorney: str = Field(..., description="ФИО прокурора")
    rank: str = Field("Прокурор", description="Должность прокурора")
    regenerate_feedback: Optional[str] = Field(None, max_length=1000, description="Что исправить при перегенерации")


class FreeFormResponseResponse(BaseModel):
    """Ответ с AI-генерированным текстом свободной формы."""
    main_text: str = Field(..., description="Сгенерированный текст ответа")
    processing_time_ms: float = Field(..., description="Время обработки")


# === Веб-вход и ядро сайта TurgovBot (/v1/web/*) ===
# Формы совместимы с фронтом сайта (src/lib/types.ts в репозитории сайта).

class WebLoginRequest(BaseModel):
    """Payload от Telegram Login Widget (data-onauth). Имена полей — как у виджета."""
    id: int = Field(..., description="Telegram user id")
    first_name: str = Field(..., description="Имя")
    last_name: Optional[str] = Field(None, description="Фамилия")
    username: Optional[str] = Field(None, description="@username (без @)")
    photo_url: Optional[str] = Field(None, description="URL аватара")
    auth_date: int = Field(..., description="Unix-время выпуска подписи виджетом")
    hash: str = Field(..., description="HMAC-подпись виджета")


class WebUserProfile(BaseModel):
    """Профиль пользователя для шапки сайта (types.ts::UserProfile)."""
    user_id: int
    full_name: str
    username: Optional[str] = None
    photo_url: Optional[str] = None
    status: str = Field(..., description="Тир для бейджа (types.ts::Tier)")
    status_label: str = Field(..., description="Человекочитаемый бейдж, напр. '💎 Подписчик'")


class WebAccountFlags(BaseModel):
    """Сырые флаги ролей (types.ts::AccountFlags)."""
    is_admin: bool = False
    is_vip: bool = False
    is_unlimited: bool = False
    is_subscriber: bool = False
    is_tester: bool = False
    is_banned: bool = False


class WebLimitItem(BaseModel):
    """Один лимит (types.ts::LimitItem). limit=-1 → безлимит."""
    key: str = Field(..., description="questions | interview | complaint | assistant usage_type")
    label: str
    used: int
    limit: int = Field(..., description="-1 = безлимит")
    remaining: int


class WebLimits(BaseModel):
    """Сводка лимитов (types.ts::Limits)."""
    unlimited: bool = False
    daily_reset_seconds: int = 0
    assistant_reset_seconds: int = 0
    daily: List[WebLimitItem] = Field(default_factory=list)
    assistant: List[WebLimitItem] = Field(default_factory=list)


class WebQuestionTemplate(BaseModel):
    """Шаблон вопросов по умолчанию (types.ts::QuestionTemplate)."""
    default_project_id: Optional[str] = None
    default_server_id: Optional[str] = None
    default_question_type: Optional[str] = None


class MeResponse(BaseModel):
    """Профиль + флаги + лимиты + шаблон (types.ts::Me). Ответ /login и /me."""
    user: WebUserProfile
    flags: WebAccountFlags
    limits: WebLimits
    template: WebQuestionTemplate


class WebAskRequest(BaseModel):
    """Запрос на ответ на юр. вопрос с сайта (types.ts::AskRequest)."""
    server_id: str = Field(..., description="ID сервера")
    question: str = Field(..., min_length=3, max_length=4000, description="Вопрос пользователя")
    answer_type: Literal["detailed", "brief"] = Field(
        default="detailed", description="detailed (развёрнутый) | brief (краткий)"
    )
    optimize: bool = Field(False, description="Зарезервировано (оптимизация запроса)")


class WebCitation(BaseModel):
    """Ссылка на норму (types.ts::Citation)."""
    code: Optional[str] = None
    article: Optional[str] = None
    text: str


class WebAskResponse(BaseModel):
    """Ответ на юр. вопрос (types.ts::AskResponse)."""
    answer: str
    citations: Optional[List[WebCitation]] = None
    source_url: Optional[str] = None
    source_label: Optional[str] = None
    server_id: str
    processing_time_ms: Optional[float] = None


class WebServerOption(BaseModel):
    """Сервер для пикера сайта (types.ts::ServerOption)."""
    server_id: str
    display_name: str
    rag: bool = False


class WebProjectOption(BaseModel):
    """Проект с серверами (types.ts::ProjectOption)."""
    project_id: str
    display_name: str
    servers: List[WebServerOption] = Field(default_factory=list)


class WebServersResponse(BaseModel):
    """Каскад проект→серверы для сайта."""
    projects: List[WebProjectOption] = Field(default_factory=list)


class WebHistoryItem(BaseModel):
    """Элемент истории (types.ts::HistoryItem)."""
    id: int
    question: str
    timestamp: str


class WebHistoryPage(BaseModel):
    """Страница истории (types.ts::HistoryPage)."""
    total: int
    page: int
    total_pages: int
    items: List[WebHistoryItem] = Field(default_factory=list)


class WebHistoryDetail(BaseModel):
    """Деталь диалога (types.ts::HistoryDetail)."""
    question: str
    answer: str
    timestamp: str


class WebTemplateUpdateResponse(BaseModel):
    """Ответ на сохранение шаблона."""
    ok: bool = True
    template: WebQuestionTemplate


# --- Генератор вопросов для собеседований (/v1/web/interview) ---

class WebOption(BaseModel):
    """Пара ключ→подпись для пикеров фронта (роль/сложность)."""
    key: str
    label: str


class WebInterviewOptions(BaseModel):
    """Опции генератора (источник истины — config): роли, сложности, диапазон количества."""
    roles: List[WebOption] = Field(default_factory=list)
    difficulties: List[WebOption] = Field(default_factory=list)
    min_questions: int
    max_questions: int


class WebInterviewRequest(BaseModel):
    """Запрос на генерацию вопросов с сайта."""
    server_id: str = Field(..., description="ID сервера")
    role: str = Field(..., description="Ключ должности из INTERVIEW_ROLES")
    difficulty: str = Field(..., description="Ключ сложности из INTERVIEW_DIFFICULTIES")
    quantity: int = Field(..., ge=5, le=40, description="Кол-во вопросов (MIN_QUESTIONS..MAX_QUESTIONS)")


class WebInterviewQuestion(BaseModel):
    """Распарсенный вопрос (номер + текст)."""
    number: int
    text: str


class WebInterviewResponse(BaseModel):
    """Результат генерации: сырой текст + распарсенный список."""
    text: str = Field(..., description="Полный текст ответа модели (для «копировать всё»)")
    questions: List[WebInterviewQuestion] = Field(default_factory=list)
    server_id: str
    role: str
    difficulty: str
    quantity: int
    processing_time_ms: Optional[float] = None


# --- Уголовный писатель (/v1/web/criminal) ---

class WebCriminalStartRequest(BaseModel):
    """Открыть сессию уголовного дела."""
    server_id: str = Field(..., description="ID сервера")
    case_number: str = Field(..., min_length=2, max_length=50, description="Номер дела, напр. DJR-001")


class WebCriminalPart(BaseModel):
    """Сгенерированная часть дела (последняя версия)."""
    part_type: str
    display: str
    content: str
    version: int


class WebCriminalStartResponse(BaseModel):
    """Ответ на старт сессии: id + порядок/подписи частей для пошаговой генерации."""
    session_id: int
    case_number: str
    server_id: str
    parts_order: List[str] = Field(default_factory=list)
    parts_display: Dict[str, str] = Field(default_factory=dict)


class WebCriminalFactsRequest(BaseModel):
    """Фабула дела (обстоятельства)."""
    facts: str = Field(..., min_length=10, max_length=10000, description="Описание обстоятельств дела")


class WebCriminalGenerateRequest(BaseModel):
    """Сгенерировать одну часть дела."""
    part_type: str = Field(..., description="Ключ части из CRIMINAL_CASE_PARTS_ORDER")


class WebCriminalGenerateResponse(BaseModel):
    """Результат генерации части."""
    part: WebCriminalPart


class WebCriminalCaseResponse(BaseModel):
    """Полное состояние сессии: метаданные + все части."""
    session_id: int
    case_number: str
    server_id: str
    server_name: str
    status: str
    parts: List[WebCriminalPart] = Field(default_factory=list)


# --- Жалобы и иски (/v1/web/complaint) ---

class WebComplaintOptions(BaseModel):
    """Типы документов (источник истины — config.COMPLAINT_TYPES)."""
    types: List[WebOption] = Field(default_factory=list)


class WebComplaintRequest(BaseModel):
    """Запрос на составление жалобы/иска."""
    server_id: str = Field(..., description="ID сервера")
    complaint_type: str = Field(..., description="complaint | lawsuit")
    forum_url: str = Field(..., min_length=8, max_length=500,
                           description="Ссылка на тему форума с формой (обязательна)")
    situation_description: str = Field(..., min_length=50, max_length=8000,
                                       description="Описание ситуации")


class WebComplaintResponse(BaseModel):
    """Результат: HTML-документ + BB-код для форума."""
    complaint_type: str
    complaint_type_name: str
    generated_html: str
    generated_bbcode: str
    legality_warning: Optional[str] = None
    server_id: str
    processing_time_ms: Optional[float] = None


# --- Ролевой помощник (/v1/web/assistant) ---

class WebAssistantTool(BaseModel):
    """Инструмент роли (быстрое действие)."""
    name: str
    label: str


class WebAssistantOptions(BaseModel):
    """Справочник: роли, стадии, стадии-по-ролям, инструменты, окно лимита."""
    roles: List[WebOption] = Field(default_factory=list)
    stages: Dict[str, str] = Field(default_factory=dict)
    stages_by_role: Dict[str, List[str]] = Field(default_factory=dict)
    tools: Dict[str, Dict[str, List[WebAssistantTool]]] = Field(default_factory=dict)
    reset_hours: int = 72


class WebAssistantCreateRequest(BaseModel):
    """Создать дело: роль/стадия/сервер + фабула."""
    server_id: str
    rp_role: str
    case_stage: str
    description: str = Field(..., min_length=10, max_length=10000)


class WebAssistantCreateResponse(BaseModel):
    """Ответ на создание дела: id + первый ответ ассистента."""
    case_id: int
    reply: str


class WebAssistantMessage(BaseModel):
    """Сообщение в деле."""
    role: str = Field(..., description="user | ai")
    content: str


class WebAssistantPostRequest(BaseModel):
    """Реплика пользователя ИЛИ активация инструмента (одно из двух)."""
    text: Optional[str] = Field(None, max_length=8000)
    tool: Optional[str] = None


class WebAssistantReplyResponse(BaseModel):
    """Ответ ассистента на реплику/инструмент."""
    reply: str


class WebAssistantCaseResponse(BaseModel):
    """Полное состояние дела: метаданные + история + инструменты роли/стадии."""
    case_id: int
    server_id: str
    rp_role: str
    rp_role_label: str
    case_stage: str
    case_stage_label: str
    status: str
    messages: List[WebAssistantMessage] = Field(default_factory=list)
    tools: List[WebAssistantTool] = Field(default_factory=list)


class WebAssistantCaseListItem(BaseModel):
    """Элемент списка активных дел."""
    case_id: int
    server_id: str
    rp_role: str
    rp_role_label: str
    case_stage: str
    case_stage_label: str
    created_at: str


class WebAssistantCaseList(BaseModel):
    """Список активных дел пользователя."""
    cases: List[WebAssistantCaseListItem] = Field(default_factory=list)


# --- Доступ и бонусы / ВИП-заявка (/v1/web/access) ---

class WebAccessStatusResponse(BaseModel):
    """Статус доступа: текущий тир-флаги + приём ВИП-заявок + статус своей заявки."""
    is_unlimited: bool = False
    is_vip: bool = False
    is_subscriber: bool = False
    intake_enabled: bool = True
    application_status: Optional[str] = Field(None, description="pending|approved|rejected|needs_info|None")
    subscribe_url: str
    max_files: int = 10


class WebVipApplicationResponse(BaseModel):
    """Ответ на подачу ВИП-заявки."""
    ok: bool = True
    application_id: int
    status: str = "pending"


# --- Поддержка (/v1/web/support) ---

class WebSupportOptions(BaseModel):
    """Темы обращений + прямой контакт."""
    topics: List[WebOption] = Field(default_factory=list)
    contact_url: str


class WebSupportTicketRequest(BaseModel):
    """Обращение в поддержку."""
    topic: str = Field(..., max_length=80)
    message: str = Field(..., min_length=5, max_length=4000)


class WebSupportTicketResponse(BaseModel):
    ok: bool = True


# END OF FILE api/schemas.py
