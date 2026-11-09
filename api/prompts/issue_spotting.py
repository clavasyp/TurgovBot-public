# START OF FILE api/prompts/issue_spotting.py

"""
Промпт для issue spotting (getIssueSpottingTags).

Точно повторяет логику complaintAiService.js из prosecutor-cms1.
"""

# JSON Schema для structured output
ISSUE_SPOTTING_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Юридические вопросы/темы для поиска по законам: например вымогательство взятки, незаконное задержание, изъятие имущества, процедура ареста"
        }
    },
    "required": ["tags"]
}

# Константы из оригинала
MAX_FACTS_FOR_ISSUE_SPOTTING = 50
MAX_CHARS_PER_FACT = 500


def get_issue_spotting_prompt(facts: list) -> str:
    """
    Генерирует промпт для определения юридических тегов по фактам.

    Точно повторяет логику из complaintAiService.js getIssueSpottingTags.

    Args:
        facts: Список фактов (строки)

    Returns:
        Строка с промптом для AI
    """
    # Фильтруем и ограничиваем факты как в оригинале
    filtered_facts = [
        f.strip()[:MAX_CHARS_PER_FACT]
        for f in facts
        if isinstance(f, str) and f.strip()
    ][:MAX_FACTS_FOR_ISSUE_SPOTTING]

    facts_text = "\n".join([f"{i + 1}. {f}" for i, f in enumerate(filtered_facts)])

    return f"""По списку фактов из обращения в прокуратуру определи юридические вопросы/темы для поиска по законам.

ФАКТЫ:
{facts_text}

Верни массив тегов (короткие формулировки): например "вымогательство взятки", "незаконное задержание", "изъятие имущества", "процедура ареста", "отказ в адвокате", "применение силы" и т.п. Только релевантные к фактам темы, без лишнего. 3–15 тегов.

Ответь только JSON по заданной схеме. Определяй юридические темы для поиска статей законов по фактам."""


def get_issue_spotting_prompt_with_laws(facts: list, laws_text: str) -> str:
    """
    Генерирует промпт для определения юридических тегов по фактам С контекстом законов.

    Расширенная версия: AI использует терминологию из предоставленных законов
    для более точного определения тегов.

    Args:
        facts: Список фактов (строки)
        laws_text: Текст релевантных законов (из RAG-фильтра)

    Returns:
        Строка с промптом для AI
    """
    # Фильтруем и ограничиваем факты как в оригинале
    filtered_facts = [
        f.strip()[:MAX_CHARS_PER_FACT]
        for f in facts
        if isinstance(f, str) and f.strip()
    ][:MAX_FACTS_FOR_ISSUE_SPOTTING]

    facts_text = "\n".join([f"{i + 1}. {f}" for i, f in enumerate(filtered_facts)])

    laws_section = ""
    if laws_text and laws_text.strip():
        laws_section = f"""
### ДОСТУПНЫЕ ЗАКОНЫ ДЛЯ СПРАВКИ:
{laws_text[:5000]}

ВАЖНО: Используй терминологию из этих законов при формулировке тегов. Теги должны соответствовать понятиям и статьям из законов сервера.
"""

    return f"""По списку фактов из обращения в прокуратуру игрового сервера определи юридические вопросы/темы для поиска по законам.

ФАКТЫ:
{facts_text}
{laws_section}
Верни массив тегов (короткие формулировки): например "вымогательство взятки", "незаконное задержание", "изъятие имущества", "процедура ареста", "отказ в адвокате", "применение силы" и т.п. 

Используй терминологию из законов сервера. Только релевантные к фактам темы, без лишнего. 3–15 тегов.

Ответь только JSON по заданной схеме."""


# END OF FILE api/prompts/issue_spotting.py

