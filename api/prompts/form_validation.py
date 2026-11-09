# START OF FILE api/prompts/form_validation.py

"""
Промпт для AI-валидации формы обращения (validateFormAI).

Точно повторяет логику formValidatorService.js из prosecutor-cms1.
"""

# JSON Schema для structured output
FORM_VALIDATION_SCHEMA = {
    "type": "object",
    "properties": {
        "isValid": {
            "type": "boolean",
            "description": "Соответствует ли обращение регламенту"
        },
        "confidenceScore": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Уверенность в оценке 0-100"
        },
        "formQuality": {
            "type": "string",
            "enum": ["EXCELLENT", "GOOD", "ACCEPTABLE", "POOR", "INVALID"],
            "description": "Качество заполнения формы"
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {
                        "type": "string",
                        "enum": ["CRITICAL", "WARNING", "INFO"]
                    },
                    "field": {"type": "string"},
                    "issue": {"type": "string"},
                    "suggestion": {"type": "string"}
                },
                "required": ["severity", "field", "issue", "suggestion"]
            },
            "description": "Найденные проблемы"
        },
        "missingInfo": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Список недостающей информации"
        },
        "contradictions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Найденные противоречия в тексте"
        },
        "extractedData": {
            "type": "object",
            "properties": {
                "complainant": {
                    "type": ["string", "null"],
                    "description": "Имя заявителя если найдено"
                },
                "defendant": {
                    "type": ["string", "null"],
                    "description": "Имя нарушителя если найдено"
                },
                "organization": {
                    "type": ["string", "null"],
                    "description": "Организация нарушителя"
                },
                "incidentDate": {
                    "type": ["string", "null"],
                    "description": "Дата если найдена"
                },
                "incidentTime": {
                    "type": ["string", "null"],
                    "description": "Время если найдено"
                }
            },
            "required": ["complainant", "defendant", "organization", "incidentDate", "incidentTime"]
        },
        "recommendation": {
            "type": "string",
            "enum": ["ACCEPT", "ACCEPT_WITH_NOTICE", "REQUEST_CORRECTION", "REJECT"],
            "description": "Рекомендация по обращению"
        },
        "summary": {
            "type": "string",
            "description": "Краткое заключение о качестве обращения"
        }
    },
    "required": [
        "isValid", "confidenceScore", "formQuality", "issues",
        "missingInfo", "contradictions", "extractedData",
        "recommendation", "summary"
    ]
}


def get_form_validation_ai_prompt(title: str, content: str) -> str:
    """
    Генерирует промпт для AI-валидации формы обращения.

    Точно повторяет логику из formValidatorService.js validateFormAI.

    Args:
        title: Заголовок обращения
        content: Текст обращения

    Returns:
        Строка с промптом для AI
    """
    return f"""Ты - эксперт по валидации обращений в Офис Генерального Прокурора игрового сервера.

РЕГЛАМЕНТ ПОДАЧИ ОБРАЩЕНИЙ:
- Заголовок должен быть: "ОБРАЩЕНИЕ В ОФИС ГЕНЕРАЛЬНОГО ПРОКУРОРА №XXX ОТ ГР-А Имя Фамилия"
- Номер обращения должен следовать за предыдущим
- Обязательные поля:
  1. Имя и Фамилия заявителя
  2. Сведения о нарушителе (имя, фамилия, организация, идентификационный знак)
  3. Подробное описание ситуации
  4. Дата и время события
  5. Доказательства нарушения (при наличии)
  6. Контактный адрес электронной почты, ксерокопия паспорта
  7. Подпись и её расшифровка

ОБРАЩЕНИЕ ДЛЯ ПРОВЕРКИ:

Заголовок: {title}

Содержание:
{content}

ЗАДАЧА:
1. Проверь соответствие обращения регламенту
2. Оцени полноту и качество информации
3. Найди несоответствия и противоречия
4. Оцени серьёзность нарушений формы

Ответь ТОЛЬКО в формате JSON по заданной схеме, без markdown и пояснений."""


# END OF FILE api/prompts/form_validation.py

