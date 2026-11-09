# START OF FILE user_handlers/__init__.py

"""
Пакет user_handlers.

Этот пакет содержит все обработчики команд и диалогов (ConversationHandlers),
предназначенные для взаимодействия с обычными пользователями.

Логика разделена на модули по функциональному признаку для упрощения
поддержки и навигации по кодовой базе.
"""

# Импорт основных, не-диалоговых обработчиков из core.py
from .core import start, main_menu, personal_account, refund_usage

# Импорт ConversationHandler'ов из соответствующих модулей
from .qa_flow import user_conv_handler
from .assistant_flow import assistant_conv_handler, prosecutor_assistant_menu
from .interview_flow import interview_conv_handler
from .report_flow import report_conv_handler
from .subscription import subscription_conv_handler
from .history import history_conv_handler
from .complaint_flow import complaint_conv_handler
from .prosecution_complaint_flow import pch_conv_handler  # Added
from .test_loophole_flow import test_loophole_conv_handler  # /test — тестовый анализатор дыр
from .tester_application_flow import tester_application_conv_handler  # заявки в тестеры (?start=apply_tester)
from .tester_feedback_flow import tester_feedback_conv_handler  # канал обратной связи тестеров (баги/идеи)

# Явное определение публичного API пакета.
# Это список имен, которые будут импортированы при выполнении `from user_handlers import *`
# и является хорошей практикой для документирования интерфейса пакета.
__all__ = [
    # Core handlers
    'start',
    'main_menu',
    'personal_account',
    'refund_usage',
    # Conversation handlers
    'user_conv_handler',
    'assistant_conv_handler',
    'interview_conv_handler',
    'report_conv_handler',
    'subscription_conv_handler',
    'history_conv_handler',
    'complaint_conv_handler',
    'pch_conv_handler',  # Added
    'prosecutor_assistant_menu',  # Added
    'test_loophole_conv_handler',  # /test
    'tester_application_conv_handler',  # заявки в тестеры
    'tester_feedback_conv_handler',  # канал обратной связи тестеров
]

# END OF FILE user_handlers/__init__.py