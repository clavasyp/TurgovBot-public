# START OF FILE database/__init__.py

"""
Инициализационный файл для пакета database.

Этот файл делает основные компоненты пакета (db_manager, assistant_logger)
доступными для импорта из других частей приложения.
"""

from .manager import db_manager
from . import assistant_logger

__all__ = [
    'db_manager',
    'assistant_logger',
]

# END OF FILE database/__init__.py