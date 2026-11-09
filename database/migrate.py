# START OF FILE database/migrate.py

"""
Утилита для управления миграциями базы данных через Alembic.

Использование:
    python -m database.migrate upgrade      # Применить все миграции
    python -m database.migrate downgrade    # Откатить последнюю миграцию
    python -m database.migrate current      # Показать текущую ревизию
    python -m database.migrate history      # Показать историю миграций
    python -m database.migrate stamp head   # Пометить текущую БД как актуальную
    python -m database.migrate autogenerate "описание"  # Создать новую миграцию автоматически
"""

import sys
import os

# Добавляем корень проекта в путь для импортов
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic.config import Config
from alembic import command


def get_alembic_config() -> Config:
    """Возвращает конфигурацию Alembic."""
    # Путь к alembic.ini относительно корня проекта
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    alembic_ini = os.path.join(base_dir, 'alembic.ini')

    if not os.path.exists(alembic_ini):
        print(f"ОШИБКА: Файл конфигурации не найден: {alembic_ini}")
        sys.exit(1)

    config = Config(alembic_ini)
    # Устанавливаем script_location относительно корня
    config.set_main_option('script_location', os.path.join(base_dir, 'alembic'))

    return config


def upgrade(revision: str = 'head'):
    """Применить миграции до указанной ревизии (по умолчанию до последней)."""
    config = get_alembic_config()
    print(f"📦 Применение миграций до ревизии: {revision}")
    command.upgrade(config, revision)
    print("✅ Миграции успешно применены!")


def downgrade(revision: str = '-1'):
    """Откатить миграции до указанной ревизии (по умолчанию на 1 назад)."""
    config = get_alembic_config()
    print(f"⏪ Откат миграций до ревизии: {revision}")
    command.downgrade(config, revision)
    print("✅ Откат успешно выполнен!")


def current():
    """Показать текущую ревизию базы данных."""
    config = get_alembic_config()
    print("📍 Текущая ревизия БД:")
    command.current(config, verbose=True)


def history():
    """Показать историю всех миграций."""
    config = get_alembic_config()
    print("📜 История миграций:")
    command.history(config, verbose=True)


def stamp(revision: str = 'head'):
    """Пометить текущую БД как находящуюся на указанной ревизии без выполнения миграций."""
    config = get_alembic_config()
    print(f"🏷️ Установка метки ревизии: {revision}")
    command.stamp(config, revision)
    print(f"✅ БД помечена как ревизия: {revision}")


def autogenerate(message: str):
    """Автоматически сгенерировать новую миграцию на основе изменений в моделях."""
    config = get_alembic_config()
    print(f"🔄 Генерация новой миграции: {message}")
    command.revision(config, message=message, autogenerate=True)
    print("✅ Миграция успешно создана! Проверьте файл в alembic/versions/")


def heads():
    """Показать все головные ревизии."""
    config = get_alembic_config()
    print("🎯 Головные ревизии:")
    command.heads(config, verbose=True)


def print_help():
    """Вывести справку по использованию."""
    print(__doc__)


def main():
    """Основная функция CLI."""
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    cmd = sys.argv[1].lower()

    commands = {
        'upgrade': lambda: upgrade(sys.argv[2] if len(sys.argv) > 2 else 'head'),
        'downgrade': lambda: downgrade(sys.argv[2] if len(sys.argv) > 2 else '-1'),
        'current': current,
        'history': history,
        'stamp': lambda: stamp(sys.argv[2] if len(sys.argv) > 2 else 'head'),
        'autogenerate': lambda: autogenerate(sys.argv[2] if len(sys.argv) > 2 else 'auto migration'),
        'heads': heads,
        'help': print_help,
        '--help': print_help,
        '-h': print_help,
    }

    if cmd in commands:
        try:
            commands[cmd]()
        except Exception as e:
            print(f"❌ Ошибка: {e}")
            sys.exit(1)
    else:
        print(f"❌ Неизвестная команда: {cmd}")
        print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()

# END OF FILE database/migrate.py

