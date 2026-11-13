# Hermes API and background-worker image.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY hermes/ hermes/
COPY scripts/ scripts/
RUN pip install --no-cache-dir -e .

# Writable location for the SQLite conversation store (a production deploy
# would set HERMES_DB to a mounted volume or use Postgres).
RUN mkdir -p /app/data
ENV HERMES_DB=/app/data/hermes.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "hermes.app:app", "--host", "0.0.0.0", "--port", "8000"]
