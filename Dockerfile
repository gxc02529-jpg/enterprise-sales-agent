FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[infra]"

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/exports \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000 8001
CMD ["uvicorn", "sales_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]

