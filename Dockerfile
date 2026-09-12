# Throughline API + worker image (Python 3.12)
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

COPY pyproject.toml README.md alembic.ini ./
COPY throughline ./throughline
COPY alembic ./alembic
COPY tests ./tests

# Install package + deps into site-packages. PYTHONPATH=/app prefers the
# bind-mounted source tree when Compose mounts the repo for live reload.
RUN pip install --no-cache-dir ".[dev]"

EXPOSE 8000

CMD ["uvicorn", "throughline.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
