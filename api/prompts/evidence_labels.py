# START OF FILE api/prompts/evidence_labels.py

"""
Промпт для классификации типов доказательств (getEvidenceTypeLabels).

Точно повторяет логику complaintAiService.js из prosecutor-cms1.
"""


def get_evidence_labels_prompt(text: str, urls: list) -> str:
    """
    Генерирует промпт для определения типов ссылок на доказательства.

    Точно повторяет логику из complaintAiService.js getEvidenceTypeLabels.

    Args:
        text: Текст обращения
        urls: Список URL для классификации

    Returns:
        Строка с промптом для AI
    """
    # Ограничиваем текст как в оригинале
    truncated_text = (text or "")[:6000]
    urls_text = "\n".join(urls) if urls else "(нет ссылок)"

    return f"""По тексту обращения определи тип каждой ссылки. Верни ТОЛЬКО JSON-массив без markdown.

ТЕКСТ:
{truncated_text}

ССЫЛКИ:
{urls_text}

Формат ответа: {{ "labels": [ {{ "url": "точная ссылка как в списке", "typeLabel": "краткое описание, напр. видео на 20 минут, ссылка на постановление прокуратуры, скриншот переписки" }} ] }}
Для каждой ссылки из списка укажи один объект. typeLabel — на русском, кратко."""


# END OF FILE api/prompts/evidence_labels.py

