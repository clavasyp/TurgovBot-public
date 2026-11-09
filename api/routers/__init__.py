# START OF FILE api/routers/__init__.py

"""
API роутеры.
"""

from .qa import router as qa_router
from .criminal_case import router as criminal_case_router
from .complaint_analyzer import router as complaint_analyzer_router
from .cms_migration import router as cms_migration_router
from .overlay import router as overlay_router
from .overlay_laws import router as overlay_laws_router
from .voice import router as voice_router
from .auth import router as auth_router
from .license import router as license_router
from .web_auth import router as web_auth_router
from .web_tools import router as web_tools_router
from .web_assistant import router as web_assistant_router
from .web_account import router as web_account_router

__all__ = ['qa_router', 'criminal_case_router', 'complaint_analyzer_router',
           'cms_migration_router', 'overlay_router', 'overlay_laws_router', 'voice_router',
           'auth_router', 'license_router',
           'web_auth_router', 'web_tools_router', 'web_assistant_router', 'web_account_router']

# END OF FILE api/routers/__init__.py
