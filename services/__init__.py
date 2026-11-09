# START OF FILE services/__init__.py

"""
Сервисный слой для бизнес-логики.

Содержит переиспользуемые сервисы, которые могут вызываться
как из Telegram-бота, так и из REST API.
"""

from .qa_service import ask_question
from .criminal_case_service import (
    create_ccw_session,
    add_facts_to_session,
    analyze_for_questions,
    save_phase_a_answers,
    generate_case_part,
    get_session_data,
    get_all_parts
)
from .complaint_analyzer_service import (
    parse_complaint_from_url,
    get_form_template,
    set_form_template,
    get_issue_types,
    add_issue_type,
    update_issue_type,
    delete_issue_type,
    check_complaint_form,
    analyze_complaint,
    extract_complaint_facts
)

__all__ = [
    # QA
    'ask_question',
    # Criminal Case Writer
    'create_ccw_session',
    'add_facts_to_session',
    'analyze_for_questions',
    'save_phase_a_answers',
    'generate_case_part',
    'get_session_data',
    'get_all_parts',
    # Complaint Analyzer
    'parse_complaint_from_url',
    'get_form_template',
    'set_form_template',
    'get_issue_types',
    'add_issue_type',
    'update_issue_type',
    'delete_issue_type',
    'check_complaint_form',
    'analyze_complaint',
    'extract_complaint_facts'
]

# END OF FILE services/__init__.py
