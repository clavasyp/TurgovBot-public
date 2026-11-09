# START OF FILE api/prompts/complaint_analysis.py

"""
Промпт для полного анализа жалобы (analyzeComplaintFull).

Точно повторяет логику complaintAiService.js из prosecutor-cms1.
"""

# JSON Schema для structured output — полный анализ жалобы
COMPLAINT_FULL_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "extractedData": {
            "type": "object",
            "properties": {
                "complainantName": {
                    "type": ["string", "null"],
                    "description": "ФИО заявителя"
                },
                "respondents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Имя ответчика или идентификационный знак/жетон/нашивка (без должности и организации)"
                            },
                            "position": {
                                "type": ["string", "null"],
                                "description": "Должность ответчика или null"
                            },
                            "org": {
                                "type": ["string", "null"],
                                "description": "Организация ответчика или null"
                            }
                        },
                        "required": ["name", "position", "org"]
                    },
                    "description": "Массив ответчиков/обвиняемых"
                },
                "complexity": {
                    "type": "string",
                    "enum": ["LOW", "MEDIUM", "HIGH"],
                    "description": "Сложность рассмотрения"
                },
                "complaintNumber": {
                    "type": ["integer", "null"],
                    "description": "Номер обращения из заголовка"
                }
            },
            "required": ["complainantName", "respondents", "complexity", "complaintNumber"]
        },
        "facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Только нейтральное действие/событие (без оценки законности). 5–12 штук."
        },
        "legalCharacterization": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Для каждого факта — краткая правовая оценка своими словами БЕЗ ссылок на статьи/кодексы/законы. Длина = длина facts."
        },
        "requirements": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Требования, просьбы заявителя и неозвученные требования."
        },
        "formCheck": {
            "type": "object",
            "properties": {
                "hasSignature": {"type": "boolean"},
                "hasEmail": {"type": "boolean"},
                "hasPassportCopy": {"type": "boolean"},
                "isValid": {"type": "boolean"}
            },
            "required": ["hasSignature", "hasEmail", "hasPassportCopy", "isValid"]
        },
        "summary": {
            "type": "string",
            "description": "Краткое изложение в 1-2 предложениях"
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["action", "warning", "info"]
                    },
                    "title": {"type": "string"},
                    "description": {"type": "string"}
                },
                "required": ["type", "title", "description"]
            },
            "description": "0–5 рекомендаций для прокурора"
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL ссылки на доказательство"
                    },
                    "typeLabel": {
                        "type": "string",
                        "description": "Краткое описание на русском"
                    }
                },
                "required": ["url", "typeLabel"]
            },
            "description": "Все упомянутые ссылки на доказательства"
        },
        "pipelineFlags": {
            "type": "object",
            "properties": {
                "requiresDocs": {
                    "type": "boolean",
                    "description": "Нужны ли дополнительные документы от организации"
                },
                "requiresInterrogation": {
                    "type": "boolean",
                    "description": "Целесообразен ли допрос"
                },
                "requiresCourt": {
                    "type": "boolean",
                    "description": "Целесообразно ли направление в суд"
                }
            },
            "required": ["requiresDocs", "requiresInterrogation", "requiresCourt"]
        }
    },
    "required": [
        "extractedData", "facts", "legalCharacterization", "requirements",
        "formCheck", "summary", "recommendations", "evidence", "pipelineFlags"
    ]
}


def get_full_complaint_analysis_prompt(title: str, content: str) -> str:
    """
    Генерирует промпт для полного анализа жалобы.

    Точно повторяет логику из complaintAiService.js.

    Args:
        title: Заголовок жалобы
        content: Текст жалобы

    Returns:
        Строка с промптом для AI
    """
    return f"""Ты — помощник прокурора. Проанализируй обращение в прокуратуру.

ЗАГОЛОВОК:
{title or '(без заголовка)'}

ТЕКСТ ОБРАЩЕНИЯ:
{content}

Правила:
- extractedData.respondents — массив ответчиков/обвиняемых (может быть несколько). Для каждого: name — имя или идентификационный знак/жетон/нашивка без должности и организации; position и org — должность и организация или null.
- facts — только нейтральное действие или событие: что заявитель утверждает, что произошло (без оценки законности). Один факт — одно утверждение. Нельзя объединять в одном факте действие и правовую оценку. Примеры: неправильно — «Заявитель был незаконно арестован» (действие и оценка вместе); правильно — факт «Заявитель был арестован», legalCharacterization «Арест был незаконным». Неправильно — «Заявитель был задержан без протокола» в одном факте; правильно — факт «Заявитель был задержан», legalCharacterization «Задержание без протокола / нарушение процедуры». 5–12 фактов.
- legalCharacterization — массив той же длины что и facts: для каждого факта — краткая правовая оценка своими словами БЕЗ ссылок на конкретные статьи, кодексы или законы (например «превышение полномочий», «арест был незаконным», «нарушение процедуры задержания», «незаконное применение силы»). НЕ указывай номера статей, названия кодексов или законов. Оценку законности указывать только здесь, не в facts. Пустая строка если квалификация не ясна.
- requirements — массив требований, просьб заявителя и неозвученных требований: что заявитель явно просит или подразумевает (например: отменить постановление, наказать сотрудника, вернуть изъятое, провести проверку). Может быть несколько формулировок; каждая — отдельный элемент массива.
- formCheck.hasPassportCopy — true, если заявитель приложил ксерокопию паспорта или указал, что приложена по ссылке / см. выше / в приложении, и в тексте есть ссылка на изображение или файл (достаточно наличия URL к картинке или хосту изображений).
- formCheck.isValid — true только если hasSignature, hasEmail и hasPassportCopy все true.
- summary — одно-два предложения: кто, на кого, суть.
- recommendations — 0–5 коротких рекомендаций для прокурора (type: action | warning | info). НЕ ссылайся на конкретные статьи, кодексы или законы — формулируй действия своими словами.
- evidence — массив всех упомянутых в тексте ссылок на доказательства: каждая ссылка (URL) на видео, скриншот, вложение, изображение. Для каждого элемента: url — точная ссылка как в тексте, typeLabel — краткое описание на русском (например: «видео 20 минут», «скриншот переписки», «вложение форума»). Если ссылок нет — пустой массив.
- pipelineFlags: requiresDocs — true если для принятия решения недостаточно материалов и нужно истребовать документы у организации; requiresInterrogation — true если по обстоятельствам дела целесообразно назначить допрос; requiresCourt — true если характер нарушений или требования заявителя указывают на необходимость направления дела в суд. В сомнительных случаях ставить false.

ВАЖНО: Нигде в ответе (facts, legalCharacterization, recommendations, summary) НЕ указывай номера статей, названия кодексов или законов. Формулируй всё своими словами без юридических ссылок.

Ответь только JSON по заданной схеме, без пояснений и без markdown."""


def get_full_complaint_analysis_prompt_with_laws(title: str, content: str, laws_text: str) -> str:
    """
    Генерирует промпт для полного анализа жалобы С контекстом законов.

    Расширенная версия с RAG-фильтром: AI использует предоставленные законы
    для более точной правовой квалификации фактов.

    Args:
        title: Заголовок жалобы
        content: Текст жалобы
        laws_text: Текст релевантных законов (из RAG-фильтра)

    Returns:
        Строка с промптом для AI
    """
    laws_section = ""
    if laws_text and laws_text.strip():
        laws_section = ""  # Законы больше не передаются в контекст AI-анализа

    return f"""Ты — помощник прокурора игрового сервера. Проанализируй обращение в прокуратуру.

ЗАГОЛОВОК:
{title or '(без заголовка)'}

ТЕКСТ ОБРАЩЕНИЯ:
{content}
{laws_section}
Правила:
- extractedData.respondents — массив ответчиков/обвиняемых (может быть несколько). Для каждого: name — имя или идентификационный знак/жетон/нашивка без должности и организации; position и org — должность и организация или null.
- facts — только нейтральное действие или событие: что заявитель утверждает, что произошло (без оценки законности). Один факт — одно утверждение. Нельзя объединять в одном факте действие и правовую оценку. Примеры: неправильно — «Заявитель был незаконно арестован» (действие и оценка вместе); правильно — факт «Заявитель был арестован», legalCharacterization «Арест был незаконным». 5–12 фактов.
- legalCharacterization — массив той же длины что и facts: для каждого факта — краткая правовая оценка своими словами БЕЗ ссылок на конкретные статьи, кодексы или законы (например «превышение полномочий», «арест был незаконным», «нарушение процедуры задержания»). НЕ указывай номера статей, названия кодексов или законов. Пустая строка если квалификация не ясна.
- requirements — массив требований и просьб заявителя.
- formCheck.hasPassportCopy — true, если заявитель приложил ксерокопию паспорта или указал, что приложена по ссылке.
- formCheck.isValid — true только если hasSignature, hasEmail и hasPassportCopy все true.
- summary — одно-два предложения: кто, на кого, суть.
- recommendations — 0–5 коротких рекомендаций для прокурора (type: action | warning | info). НЕ ссылайся на конкретные статьи, кодексы или законы — формулируй действия своими словами.
- evidence — массив всех упомянутых в тексте ссылок на доказательства.
- pipelineFlags: requiresDocs — true если нужно истребовать документы; requiresInterrogation — true если целесообразен допрос; requiresCourt — true если дело требует суда.

ВАЖНО: Нигде в ответе (facts, legalCharacterization, recommendations, summary) НЕ указывай номера статей, названия кодексов или законов. Формулируй всё своими словами без юридических ссылок.

Ответь только JSON по заданной схеме, без пояснений и без markdown."""


def get_combined_analysis_prompt_with_laws(title: str, content: str, laws_text: str) -> str:
    """
    Генерирует промпт для КОМБИНИРОВАННОГО анализа жалобы (fullAnalysis + issueTags).

    Объединяет полный анализ и issue spotting в один запрос для экономии токенов.

    Args:
        title: Заголовок жалобы
        content: Текст жалобы
        laws_text: Текст релевантных законов (из RAG-фильтра)

    Returns:
        Строка с промптом для AI
    """
    laws_section = ""
    if laws_text and laws_text.strip():
        laws_section = ""  # Законы больше не передаются в контекст AI-анализа

    return f"""Ты — помощник прокурора игрового сервера. Проанализируй обращение в прокуратуру и выполни ДВЕ задачи:
1. Полный анализ жалобы (analysis)
2. Юридические теги для issue spotting (issueTags)

ЗАГОЛОВОК:
{title or '(без заголовка)'}

ТЕКСТ ОБРАЩЕНИЯ:
{content}
{laws_section}
### ЗАДАЧА 1: ПОЛНЫЙ АНАЛИЗ (analysis)

Правила для analysis:
- extractedData.respondents — массив ответчиков/обвиняемых (может быть несколько). Для каждого: name — имя или идентификационный знак/жетон/нашивка без должности и организации; position и org — должность и организация или null.
- facts — только нейтральное действие или событие: что заявитель утверждает, что произошло (без оценки законности). Один факт — одно утверждение. 5–12 фактов.
- legalCharacterization — массив той же длины что и facts: для каждого факта — краткая правовая оценка своими словами БЕЗ ссылок на конкретные статьи, кодексы или законы. НЕ указывай номера статей, названия кодексов или законов.
- requirements — массив требований и просьб заявителя.
- formCheck.hasPassportCopy — true, если приложена ксерокопия паспорта.
- formCheck.isValid — true только если hasSignature, hasEmail и hasPassportCopy все true.
- summary — одно-два предложения: кто, на кого, суть.
- recommendations — 0–5 рекомендаций для прокурора. НЕ ссылайся на статьи, кодексы или законы.
- evidence — массив ссылок на доказательства из текста.
- pipelineFlags: requiresDocs, requiresInterrogation, requiresCourt.

### ЗАДАЧА 2: ЮРИДИЧЕСКИЕ ТЕГИ (issueTags)

На основе фактов сформируй 3–10 юридических тегов. Каждый тег — краткое название правовой проблемы или нарушения (например: "Незаконное задержание", "Превышение полномочий", "Нарушение процедуры обыска", "Применение силы", "Отказ в медицинской помощи").

### ФОРМАТ ОТВЕТА

Верни JSON с двумя полями:
{{
  "analysis": {{ ... полный анализ по схеме ... }},
  "issueTags": ["тег1", "тег2", ...]
}}

Ответь только JSON, без пояснений и без markdown."""


# END OF FILE api/prompts/complaint_analysis.py

