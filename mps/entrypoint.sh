#!/bin/sh
set -eu

if [ -z "${GATEWAY_DATABASE_URL:-}" ]; then
  echo "GATEWAY_DATABASE_URL is required" >&2
  exit 1
fi

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
