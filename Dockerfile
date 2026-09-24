# AgentHub API — container image (Render / Railway / Fly.io / any Docker host).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RUN_MIGRATIONS_ON_BOOT=false

WORKDIR /app

# Build deps kept minimal; all native deps (asyncpg, argon2) ship wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 agenthub

# Reproducible install from the lockfile (fix-doc F11).
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

# Non-root runtime user (fix-doc F16); app files stay root-owned/read-only.
USER agenthub

EXPOSE 8000

COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
# `exec` so uvicorn becomes the process (clean SIGTERM handling). $PORT is
# injected by Render/Railway; defaults to 8000 locally.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
