#!/usr/bin/env bash
# Provision via `docker exec postgis` (admin). Never run this inside racunai_mps.
set -euo pipefail

CONTAINER="${POSTGIS_CONTAINER:-postgis}"
DATABASE="${GATEWAY_DATABASE:-racunai_intermediary}"
USER_NAME="${GATEWAY_DB_USER:-racunai_intermediary}"
SCHEMA="${GATEWAY_DB_SCHEMA:-gateway}"
PASSWORD="${GATEWAY_DB_PASSWORD:-}"

if [[ -z "$PASSWORD" ]]; then
  echo "GATEWAY_DB_PASSWORD is required" >&2
  exit 1
fi
if [[ "$USER_NAME" == "postgres" || "$USER_NAME" == "racunai" ]]; then
  echo "refusing privileged role name: $USER_NAME" >&2
  exit 1
fi

docker exec -i "$CONTAINER" psql -U postgres -v ON_ERROR_STOP=1 \
  -v user_name="$USER_NAME" \
  -v database="$DATABASE" \
  -v password="$PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'user_name', :'password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user_name')\gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE', :'user_name', :'password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'user_name')\gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'database', :'user_name')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database')\gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'database')\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database', :'user_name')\gexec
SQL

docker exec -i "$CONTAINER" psql -U postgres -d "$DATABASE" -v ON_ERROR_STOP=1 \
  -v user_name="$USER_NAME" \
  -v schema_name="$SCHEMA" <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION %I', :'schema_name', :'user_name')\gexec
SELECT format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', :'schema_name')\gexec
SELECT format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', :'schema_name', :'user_name')\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT ALL ON TABLES TO %I', :'user_name', :'schema_name', :'user_name')\gexec
SELECT format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT ALL ON SEQUENCES TO %I', :'user_name', :'schema_name', :'user_name')\gexec
SQL

echo "provisioned ${DATABASE} / ${USER_NAME} / ${SCHEMA} via ${CONTAINER}"
