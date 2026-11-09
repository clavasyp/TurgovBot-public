# START OF FILE api/prompts/passport_resolver.py

"""
Промпт для определения паспорта среди ссылок (resolvePassportUrlFromCandidates).

Точно повторяет логику complaintAiService.js из prosecutor-cms1.
"""


def get_passport_resolver_prompt(content: str, urls: list) -> str:
    """
    Генерирует промпт для определения, какая ссылка ведёт на паспорт.

    Args:
        content: Текст обращения
        urls: Список URL-ссылок

    Returns:
        Строка с промптом для AI
    """
    # Ограничиваем текст как в оригинале
    truncated_text = (content or "")[:8000]
    urls_text = "\n".join([f"{i + 1}. {u}" for i, u in enumerate(urls)])

    return f"""В тексте обращения заявитель приложил несколько ссылок. Одна из них — ксерокопия паспорта заявителя (п. 6 формы). Остальные — доказательства (видео, скриншоты и т.п.).

ТЕКСТ ОБРАЩЕНИЯ:
{truncated_text}

ССЫЛКИ (ровно одна из них — паспорт):
{urls_text}

Определи, какая из ссылок ведёт на ксерокопию паспорта заявителя. Ответь ТОЛЬКО одним JSON-объектом: {{ "passportUrl": "точная ссылка из списка" }} или {{ "passportUrl": null }}, если ни одна не является паспортом."""


# END OF FILE api/prompts/passport_resolver.py

