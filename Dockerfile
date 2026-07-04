# Hermes demo image for Hugging Face Spaces: the Streamlit chat simulator +
# owner dashboard, seeded with demo data at build time.
#
# NOTE: This is the Space/demo image and lives only on the `huggingface`
# branch. The real connectable API image (uvicorn hermes.app:app) lives on
# `main`.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY hermes/ hermes/
RUN pip install --no-cache-dir -e ".[dashboard]"

COPY scripts/ scripts/
RUN python scripts/seed_demo_data.py

ENV HERMES_DB=/app/demo/hermes_demo.db
ENV HOME=/tmp
EXPOSE 7860

CMD ["streamlit", "run", "hermes/dashboard.py", "--server.port=7860", "--server.address=0.0.0.0", "--server.headless=true", "--server.enableCORS=false", "--server.enableXsrfProtection=false"]
