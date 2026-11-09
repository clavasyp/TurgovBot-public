# START OF FILE api/__init__.py

"""
REST API модуль для внешнего доступа к функциям бота.

Обеспечивает:
- RAG-ответы на юридические вопросы
- Создание уголовных дел (Criminal Case Writer)
- Защищённый доступ через API-ключи
- Rate limiting и CORS
"""

from .main import app
from .security import verify_api_key, check_rate_limit

__all__ = ['app', 'verify_api_key', 'check_rate_limit']

# END OF FILE api/__init__.py
