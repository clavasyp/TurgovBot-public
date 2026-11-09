# START OF FILE api/prompts/passport_ocr.py

"""
Промпт для OCR паспорта (extractPassportData).

Точно повторяет логику citizenService.js из prosecutor-cms1.
"""

# JSON Schema для structured output
PASSPORT_OCR_SCHEMA = {
    "type": "object",
    "properties": {
        "fullName": {
            "type": ["string", "null"],
            "description": "Имя Фамилия"
        },
        "passportNumber": {
            "type": ["string", "null"],
            "description": "Номер паспорта (ID-идентификатор)"
        },
        "dateOfBirth": {
            "type": ["string", "null"],
            "description": "Дата рождения в формате YYYY-MM-DD если видна, иначе null"
        },
        "gender": {
            "type": ["string", "null"],
            "description": "Пол (М/Ж или мужской/женский) если виден"
        },
        "maritalStatus": {
            "type": ["string", "null"],
            "description": "Семейное положение: не женат, женат, не замужем, замужем - если видно"
        },
        "issuedDate": {
            "type": ["string", "null"],
            "description": "Дата выдачи если видна"
        },
        "photoDescription": {
            "type": ["string", "null"],
            "description": "Краткое описание фото для сверки"
        },
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Уверенность в распознавании 0-100"
        },
        "readabilityIssues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Проблемы с читаемостью если есть"
        },
        "isValid": {
            "type": "boolean",
            "description": "Является ли документ валидным паспортом"
        }
    },
    "required": [
        "fullName", "passportNumber", "dateOfBirth", "gender",
        "maritalStatus", "issuedDate", "photoDescription",
        "confidence", "readabilityIssues", "isValid"
    ]
}


def get_passport_ocr_prompt() -> str:
    """
    Генерирует промпт для OCR паспорта.

    Точно повторяет логику из citizenService.js extractPassportData.

    Returns:
        Строка с промптом для AI (без изображения, оно добавляется отдельно)
    """
    return """Извлеки данные из этого игрового паспорта гражданина.

Ответь ТОЛЬКО в формате JSON, без markdown:
{
  "fullName": "Имя Фамилия",
  "passportNumber": "номер паспорта (ID-идентификатор)",
  "dateOfBirth": "дата рождения в формате YYYY-MM-DD если видна, иначе null",
  "gender": "пол (М/Ж или мужской/женский) если виден",
  "maritalStatus": "семейное положение: например не женат, женат, не замужем, замужем - если видно на паспорте",
  "issuedDate": "дата выдачи если видна",
  "photoDescription": "краткое описание фото для сверки",
  "confidence": 0-100,
  "readabilityIssues": ["проблемы с читаемостью если есть"],
  "isValid": true/false
}"""


# END OF FILE api/prompts/passport_ocr.py

