# START OF FILE api/prompts/adaptive_plan.py

"""
Промпты для генерации и адаптации плана работы (generatePlanAI, adaptPlanAI).

Точно повторяет логику adaptivePlanService.js из prosecutor-cms1.
"""

import json

# JSON Schema для генерации плана
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "planType": {
            "type": "string",
            "enum": ["STANDARD", "WITH_DOCS_REQUEST", "REJECTION", "INTERROGATION", "COMPLEX"],
            "description": "Тип плана"
        },
        "estimatedDuration": {
            "type": "string",
            "description": "Оценка времени, напр. '3-5 дней'"
        },
        "complexity": {
            "type": "string",
            "enum": ["LOW", "MEDIUM", "HIGH"]
        },
        "phases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "order": {"type": "integer"},
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "CREATE_DOCUMENT", "ENTER_LINK", "UPLOAD_PROOF",
                                        "WAIT_RESPONSE", "MAKE_DECISION", "NOTIFY_PARTY",
                                        "VERIFY_INFO", "CUSTOM"
                                    ]
                                },
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "documentType": {
                                    "type": ["string", "null"],
                                    "enum": [
                                        "REQUEST_INFO", "INITIATE_CASE", "CLOSE_CASE",
                                        "FINE_ORDER", "DISMISSAL_ORDER", "INTERROGATION_ORDER",
                                        "CANCEL_ORDER", "COMPLAINT_RESPONSE", None
                                    ]
                                },
                                "prefillData": {
                                    "type": ["object", "null"],
                                    "description": "Данные для подстановки в документ"
                                },
                                "choices": {
                                    "type": ["array", "null"],
                                    "items": {"type": "string"}
                                },
                                "deadline": {
                                    "type": ["integer", "null"],
                                    "description": "Дедлайн в часах"
                                },
                                "isOptional": {"type": "boolean"},
                                "conditions": {
                                    "type": ["object", "null"],
                                    "properties": {
                                        "skipIf": {"type": ["string", "null"]},
                                        "requiredIf": {"type": ["string", "null"]}
                                    }
                                }
                            },
                            "required": ["order", "type", "title", "description"]
                        }
                    }
                },
                "required": ["name", "description", "steps"]
            }
        },
        "decisionPoints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "afterStep": {"type": "integer"},
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "choice": {"type": "string"},
                                "addSteps": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                }
                            },
                            "required": ["choice", "addSteps"]
                        }
                    }
                },
                "required": ["afterStep", "question", "options"]
            }
        },
        "riskFactors": {
            "type": "array",
            "items": {"type": "string"}
        },
        "recommendations": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": ["planType", "estimatedDuration", "complexity", "phases", "decisionPoints", "riskFactors", "recommendations"]
}

# JSON Schema для адаптации плана
PLAN_ADAPTATION_SCHEMA = {
    "type": "object",
    "properties": {
        "needsAdaptation": {
            "type": "boolean",
            "description": "Требуется ли адаптация плана"
        },
        "reason": {
            "type": "string",
            "description": "Причина адаптации"
        },
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["ADD", "REMOVE", "MODIFY", "SKIP"]
                    },
                    "stepOrder": {
                        "type": ["integer", "null"],
                        "description": "Номер шага для изменения"
                    },
                    "stepData": {
                        "type": ["object", "null"],
                        "description": "Новые данные шага"
                    },
                    "reason": {"type": "string"}
                },
                "required": ["action", "reason"]
            }
        },
        "newEstimatedDuration": {
            "type": ["string", "null"],
            "description": "Новая оценка времени"
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": ["needsAdaptation", "reason", "changes", "warnings"]
}


def get_generate_plan_prompt(context: dict) -> str:
    """
    Генерирует промпт для создания плана работы по жалобе.

    Точно повторяет логику из adaptivePlanService.js generatePlanAI.

    Args:
        context: Контекст жалобы (complaint, analysis, defendantHistory)

    Returns:
        Строка с промптом для AI
    """
    context_json = json.dumps(context, ensure_ascii=False, indent=2, default=str)

    return f"""Ты - руководитель прокуратуры игрового сервера. Создай план работы по жалобе.

КОНТЕКСТ ЖАЛОБЫ:
{context_json}

ДОСТУПНЫЕ ТИПЫ ШАГОВ:
- CREATE_DOCUMENT: создание постановления (типы: REQUEST_INFO, INITIATE_CASE, CLOSE_CASE, FINE_ORDER, DISMISSAL_ORDER, INTERROGATION_ORDER)
- ENTER_LINK: ввод ссылки на опубликованный документ
- UPLOAD_PROOF: загрузка доказательства/скриншота
- WAIT_RESPONSE: ожидание ответа (с дедлайном в часах)
- MAKE_DECISION: принятие решения (с вариантами выбора)
- NOTIFY_PARTY: уведомление стороны
- VERIFY_INFO: проверка информации
- CUSTOM: кастомный шаг

Ты - эксперт по организации работы прокуратуры в GTA5RP. Создавай детальные планы работы по жалобам.

Ответь только JSON по заданной схеме, без markdown."""


def get_adapt_plan_prompt(
    current_plan: dict,
    new_information: dict,
    completed_steps: list
) -> str:
    """
    Генерирует промпт для адаптации существующего плана.

    Точно повторяет логику из adaptivePlanService.js adaptPlanAI.

    Args:
        current_plan: Текущий план
        new_information: Новая информация (reason, documentsReceived, etc.)
        completed_steps: Список номеров выполненных шагов

    Returns:
        Строка с промптом для AI
    """
    plan_json = json.dumps(current_plan, ensure_ascii=False, indent=2, default=str)
    info_json = json.dumps(new_information, ensure_ascii=False, indent=2, default=str)
    completed_str = ", ".join(map(str, completed_steps)) if completed_steps else "нет"

    return f"""Ты - руководитель прокуратуры игрового сервера. Адаптируй план работы с учётом новой информации.

ТЕКУЩИЙ ПЛАН:
{plan_json}

НОВАЯ ИНФОРМАЦИЯ:
{info_json}

ВЫПОЛНЕННЫЕ ШАГИ: {completed_str}

Проанализируй, нужно ли адаптировать план с учётом новой информации.
Если нужно — укажи конкретные изменения.

Ответь только JSON по заданной схеме, без markdown."""


# END OF FILE api/prompts/adaptive_plan.py

