#!/bin/sh
# AgentHub API container entrypoint.
#
# By default the container ONLY serves the app — migrations run as a
# separate release/pre-deploy step (see README "Deployment"). Set
# RUN_MIGRATIONS_ON_BOOT=true for disposable/dev environments to migrate and
# (idempotently) seed agents on every boot.
set -e

if [ "$RUN_MIGRATIONS_ON_BOOT" = "true" ]; then
    echo "RUN_MIGRATIONS_ON_BOOT=true: applying migrations + agent seed"
    alembic upgrade head
    python -m app.pipeline.seed_agents
fi

exec "$@"
