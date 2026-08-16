# Intermediary database provision

Creates `racunai_intermediary` / role `racunai_intermediary` / schema `gateway` on the existing PostGIS server.

Run from deploy/CI (or WSL stage host), **not** as a manual step inside the production `racunai_mps` container.

```bash
export POSTGRES_ADMIN_URL='postgresql://<admin>@postgis:5432/postgres'
export GATEWAY_DB_PASSWORD='...'
python3 intermediary/deploy/provision_database.py
```

From the host, `postgis` may not resolve; use the admin URL that reaches the server (for example `127.0.0.1:5432` only for local admin). Application containers must use `postgis:5432`.

The admin role needs `CREATEDB` and `CREATEROLE`. The gateway role is `NOSUPERUSER` and cannot connect to other databases we do not grant.

On this host the PostGIS admin path is:

```bash
export GATEWAY_DB_PASSWORD='...'
./intermediary/deploy/provision_via_postgis_container.sh
```

That uses `docker exec postgis` (postgres admin), not `racunai_mps`.

After provision, the app user runs Alembic (`alembic upgrade head`) — never `CREATE DATABASE`.
