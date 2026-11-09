# START OF FILE services/law_loophole/__init__.py

"""
Анализатор дыр, абузов и противоречий в законке RP-серверов.

ВАЖНО: функционал тестовый, доступ — только через команду /test
для пользователей с ролью тестера или админа.

Слои:
- bundle.py        — Source Selection Policy + LawContextBundle
- model_policy.py  — выбор модели по профилю
- analyzer.py      — пайплайн v2 (single/cross + S1-S5 для deep/expert)
- report.py        — форматирование отчета для Telegram
- metrics.py       — метрики качества (golden-харнесс A/B старый↔v2)
"""

from .bundle import (
    LawContextDocument,
    LawContextBundle,
    SelectionMode,
    ModelProfile,
    AnalysisType,
    list_laws_for_selection,
    build_law_context_bundle,
)
from .model_policy import choose_model_profile, get_loophole_models, LoopholeModels
from .analyzer import analyze_loopholes, parse_json_response, validate_findings
from .report import format_loophole_report

__all__ = [
    'LawContextDocument',
    'LawContextBundle',
    'SelectionMode',
    'ModelProfile',
    'AnalysisType',
    'list_laws_for_selection',
    'build_law_context_bundle',
    'choose_model_profile',
    'get_loophole_models',
    'LoopholeModels',
    'analyze_loopholes',
    'parse_json_response',
    'validate_findings',
    'format_loophole_report',
]
