FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Run as an unprivileged user, never root.
RUN useradd --create-home --uid 1000 appuser

# Install dependencies first so this layer is cached when only source code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

# Created here (owned by appuser) so the named volume mounted on it inherits the ownership.
RUN mkdir -p /app/storage && chown appuser:appuser /app/storage

USER appuser

EXPOSE 8000

# The same image runs the API (default), the Celery worker and migrations (see docker-compose.yml).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
