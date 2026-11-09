# START OF FILE api/prompts/__init__.py

"""
Промпты для API-эндпоинтов complaint_analyzer.

Этот модуль содержит все промпты, используемые в API для миграции
с сайта prosecutor-cms1. Промпты максимально точно повторяют логику OpenAI
на стороне сайта для обеспечения идентичного качества ответов.
"""

from .complaint_analysis import (
    get_full_complaint_analysis_prompt,
    get_full_complaint_analysis_prompt_with_laws,
    COMPLAINT_FULL_ANALYSIS_SCHEMA,
)

from .issue_spotting import (
    get_issue_spotting_prompt,
    get_issue_spotting_prompt_with_laws,
    ISSUE_SPOTTING_SCHEMA,
)

from .evidence_labels import (
    get_evidence_labels_prompt,
)

from .form_validation import (
    get_form_validation_ai_prompt,
    FORM_VALIDATION_SCHEMA,
)

from .case_file_draft import (
    get_case_file_draft_prompt,
    CASE_FILE_DRAFT_SCHEMA,
    REQUIRED_KEYS,
)

from .adaptive_plan import (
    get_generate_plan_prompt,
    get_adapt_plan_prompt,
    PLAN_SCHEMA,
    PLAN_ADAPTATION_SCHEMA,
)

from .passport_ocr import (
    get_passport_ocr_prompt,
    PASSPORT_OCR_SCHEMA,
)

from .passport_resolver import (
    get_passport_resolver_prompt,
)

from .law_router import (
    get_cms_law_router_prompt,
)

# END OF FILE api/prompts/__init__.py

