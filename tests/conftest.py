# START OF FILE tests/conftest.py
"""
Pytest конфиг: гарантируем, что корень проекта и sys.path позволяют
импортировать config.py и пакеты services/, api/, user_handlers/.

Также подменяем минимально необходимые env-переменные, чтобы config.py
импортировался без ошибок.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Минимальный env, чтобы config.py не падал.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASS", "test")
os.environ.setdefault("DB_NAME", "test")
os.environ.setdefault("SUPER_ADMIN_ID", "1")
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", "")
