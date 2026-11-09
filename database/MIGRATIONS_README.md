# Миграции базы данных с Alembic

## Установка

```bash
pip install -r requirements.txt
```

## Первоначальная настройка (для существующей БД)

Если у вас уже есть база данных с таблицами, выполните:

```bash
# Пометить текущую БД как уже имеющую базовую миграцию
python -m database.migrate stamp 001_initial
```

Это пометит вашу БД как находящуюся на первой ревизии без выполнения создания таблиц.

## Для новой установки

```bash
# Применить все миграции (создаст таблицы)
python -m database.migrate upgrade
```

## Основные команды

### Применить миграции
```bash
# Применить все миграции до последней версии
python -m database.migrate upgrade

# Применить до конкретной ревизии
python -m database.migrate upgrade 001_initial
```

### Откатить миграции
```bash
# Откатить на одну миграцию назад
python -m database.migrate downgrade

# Откатить до конкретной ревизии
python -m database.migrate downgrade 001_initial

# Откатить до начального состояния (ОСТОРОЖНО!)
python -m database.migrate downgrade base
```

### Посмотреть статус
```bash
# Текущая ревизия БД
python -m database.migrate current

# История всех миграций
python -m database.migrate history
```

### Создать новую миграцию

#### Автоматически (рекомендуется)
```bash
# Сравнит модели в database/models.py с БД и создаст миграцию
python -m database.migrate autogenerate "добавлено поле email в users"
```

#### Вручную через Alembic CLI
```bash
# Создать пустую миграцию
alembic revision -m "описание миграции"

# Создать автогенерируемую миграцию
alembic revision --autogenerate -m "описание миграции"
```

## Пример: добавление нового поля

1. **Добавьте поле в модель** (`database/models.py`):
```python
class User(Base):
    # ...existing fields...
    email = Column(Text, nullable=True)  # Новое поле
```

2. **Создайте миграцию**:
```bash
python -m database.migrate autogenerate "добавлено поле email в users"
```

3. **Проверьте созданный файл** в `alembic/versions/`

4. **Примените миграцию**:
```bash
python -m database.migrate upgrade
```

## Пример: добавление новой таблицы

1. **Добавьте модель** в `database/models.py`:
```python
class NewTable(Base):
    __tablename__ = 'new_table'
    
    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
```

2. **Создайте и примените миграцию**:
```bash
python -m database.migrate autogenerate "добавлена таблица new_table"
python -m database.migrate upgrade
```

## Структура файлов

```
ProdBot/
├── alembic.ini              # Конфигурация Alembic
├── alembic/
│   ├── env.py              # Скрипт окружения (подключение к БД)
│   ├── script.py.mako      # Шаблон для новых миграций
│   └── versions/           # Файлы миграций
│       ├── __init__.py
│       └── 001_initial_*.py  # Базовая миграция
├── database/
│   ├── models.py           # SQLAlchemy модели (ORM)
│   ├── migrate.py          # Утилита для управления миграциями
│   ├── manager.py          # DatabaseManager (asyncpg) - без изменений
│   └── init_db.py          # Старый скрипт (deprecated)
```

## Важно!

- **Не удаляйте файлы миграций** из `alembic/versions/` - это сломает историю
- **Всегда проверяйте автосгенерированные миграции** перед применением
- **Делайте бэкап БД** перед применением миграций в production
- Модели в `database/models.py` используются только для Alembic - основной код продолжает использовать `asyncpg` напрямую

## Переменные окружения

Убедитесь, что в `.env` файле настроены:

```env
DB_USER=your_user
DB_PASS=your_password
DB_HOST=localhost
DB_PORT=5432
DB_NAME=your_database
```

