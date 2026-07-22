# AgentHub API — container image (Render / Railway / Fly.io / any Docker host).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Build deps kept minimal; all native deps (asyncpg, argon2) ship wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000

# On every boot: apply migrations, (idempotently) seed agents, then serve.
# $PORT is injected by Render/Railway; falls back to 8000 locally.
CMD ["sh", "-c", "alembic upgrade head && python -m app.pipeline.seed_agents && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
