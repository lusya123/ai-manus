#!/bin/bash
# Start a Celery worker for the agent task backend (TASK_BACKEND=celery).
#
# Usage:
#   ./start_worker.sh [extra celery worker args...]
#
# Environment variables:
#   CELERY_LOG_LEVEL    Worker log level (default: INFO)
#   CELERY_CONCURRENCY  Number of worker processes; each agent task occupies
#                       one process for its whole run, so this bounds how many
#                       agent sessions execute in parallel (default: 4)

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# Use the exact environment installed during image build. Calling `uv run`
# here would implicitly sync the default dev dependency group at runtime.
exec "${SCRIPT_DIR}/.venv/bin/celery" -A app.worker.celery_app worker \
    --loglevel="${CELERY_LOG_LEVEL:-INFO}" \
    --concurrency="${CELERY_CONCURRENCY:-4}" \
    "$@"
