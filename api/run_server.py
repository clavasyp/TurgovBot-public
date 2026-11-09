# START OF FILE api/run_server.py

"""
Скрипт для запуска API сервера.

Используется для запуска через subprocess из админки Telegram бота.
Запуск: python api/run_server.py
       python api/run_server.py --clear-cache  # с очисткой кэша Python
"""

import os
import sys
import signal
import logging
import shutil

# Добавляем корневую директорию в path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import uvicorn
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()


def clear_pycache():
    """Очистка __pycache__ папок для предотвращения проблем с устаревшим кэшем."""
    api_dir = os.path.dirname(os.path.abspath(__file__))
    pycache_dirs = [
        os.path.join(api_dir, '__pycache__'),
        os.path.join(api_dir, 'routers', '__pycache__'),
    ]
    for pycache_dir in pycache_dirs:
        if os.path.exists(pycache_dir):
            try:
                shutil.rmtree(pycache_dir)
                print(f"[API] Очищен кэш: {pycache_dir}")
            except Exception as e:
                print(f"[API] Не удалось очистить кэш {pycache_dir}: {e}")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - [%(name)s] - %(message)s'
)
logger = logging.getLogger(__name__)

# Импортируем конфигурацию после загрузки .env
from config import API_SERVER_HOST, API_SERVER_PORT


def signal_handler(signum, frame):
    """Обработчик сигналов для graceful shutdown."""
    logger.info(f"[API] Получен сигнал {signum}, завершение работы...")
    sys.exit(0)


def main():
    """Главная функция запуска сервера."""
    # Очистка кэша только если передан флаг --clear-cache
    if '--clear-cache' in sys.argv:
        clear_pycache()
        sys.argv.remove('--clear-cache')

    # Устанавливаем обработчики сигналов
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info(f"[API] Запуск сервера на {API_SERVER_HOST}:{API_SERVER_PORT}")

    # Проверяем импорт schemas до запуска uvicorn для лучшей диагностики
    try:
        from api.schemas import ComplaintDocumentGenerateResponse
        logger.info("[API] ✅ schemas.ComplaintDocumentGenerateResponse успешно импортирован")
    except Exception as e:
        import traceback
        full_traceback = traceback.format_exc()
        logger.error(f"[API] ❌ Ошибка импорта schemas:\n{full_traceback}")
        print("=" * 80)
        print("ПОЛНЫЙ TRACEBACK ОШИБКИ ИМПОРТА:")
        print("=" * 80)
        print(full_traceback)
        print("=" * 80)
        sys.exit(1)

    try:
        uvicorn.run(
            "api.main:app",
            host=API_SERVER_HOST,
            port=API_SERVER_PORT,
            log_level="info",
            access_log=True,
            reload=False,  # Отключаем hot reload для production
            workers=1,     # Один воркер для простоты
        )
    except Exception as e:
        import traceback
        full_traceback = traceback.format_exc()
        logger.error(f"[API] Ошибка запуска сервера:\n{full_traceback}")
        print("=" * 80)
        print("ПОЛНЫЙ TRACEBACK ОШИБКИ ЗАПУСКА:")
        print("=" * 80)
        print(full_traceback)
        print("=" * 80)
        sys.exit(1)


if __name__ == "__main__":
    main()


# END OF FILE api/run_server.py
