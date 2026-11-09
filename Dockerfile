# =============================================================================
# Dockerfile для TurgovBot (бот + API)
# =============================================================================
FROM python:3.11-slim

# Системные зависимости для psycopg2, Playwright Chromium и прочего
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libgtk-3-0 \
    libasound2 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Копируем зависимости и устанавливаем
COPY requirements.txt .
# torch — CPU-колесом ОТДЕЛЬНО (по умолчанию PyPI тянет CUDA-сборку ~+пара ГБ; нам GPU не нужен —
# локальный эмбеддер поиска FRIDA крутится на CPU). Ставим ДО requirements: точный pin
# удовлетворяется этой CPU-версией и не переустанавливается на CUDA.
RUN pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем Chromium для Playwright (court_case_parser использует его)
RUN playwright install chromium

# Копируем весь проект
COPY . .

# Создаём нужные директории
RUN mkdir -p /app/data /app/logs /app/temp_files

# Не-root пользователь для безопасности
RUN useradd --create-home appuser \
    && chown -R appuser:appuser /app \
    && mkdir -p /home/appuser/.cache && cp -r /root/.cache/ms-playwright /home/appuser/.cache/ \
    && chown -R appuser:appuser /home/appuser/.cache
USER appuser

# Health check для Docker / k8s
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)" || exit 1

# По умолчанию — запуск бота
CMD ["python", "main.py"]
