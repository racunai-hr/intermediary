#!/usr/bin/env python3
"""Provision racunai_intermediary database, role, and gateway schema.

Run from deploy/CI against the PostGIS server. Do not run as the application
user inside the production container. The admin URL must be a role with
CREATEDB and CREATEROLE — not the gateway application role.

Environment:
  POSTGRES_ADMIN_URL          postgresql://admin@postgis:5432/postgres
  GATEWAY_DB_PASSWORD         password for role racunai_intermediary
  GATEWAY_DATABASE            default racunai_intermediary
  GATEWAY_DB_USER             default racunai_intermediary
  GATEWAY_DB_SCHEMA           default gateway
"""

from __future__ import annotations

import os
import sys

import psycopg
from psycopg import sql

DATABASE = os.environ.get('GATEWAY_DATABASE', 'racunai_intermediary')
USER = os.environ.get('GATEWAY_DB_USER', 'racunai_intermediary')
SCHEMA = os.environ.get('GATEWAY_DB_SCHEMA', 'gateway')
ADMIN_URL = os.environ.get('POSTGRES_ADMIN_URL', '')
PASSWORD = os.environ.get('GATEWAY_DB_PASSWORD', '')


def _require_env() -> None:
    if not ADMIN_URL:
        sys.exit('POSTGRES_ADMIN_URL is required')
    if not PASSWORD:
        sys.exit('GATEWAY_DB_PASSWORD is required')
    if USER in {'postgres', 'racunai'} or USER.endswith('_superuser'):
        sys.exit(f'refusing to provision privileged role name: {USER}')


def _role_exists(cur, name: str) -> bool:
    cur.execute('SELECT 1 FROM pg_roles WHERE rolname = %s', (name,))
    return cur.fetchone() is not None


def _db_exists(cur, name: str) -> bool:
    cur.execute('SELECT 1 FROM pg_database WHERE datname = %s', (name,))
    return cur.fetchone() is not None


def provision() -> None:
    _require_env()
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        with conn.cursor() as cur:
            if not _role_exists(cur, USER):
                cur.execute(
                    sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(
                        sql.Identifier(USER),
                        sql.Literal(PASSWORD),
                    )
                )
            else:
                cur.execute(
                    sql.SQL('ALTER ROLE {} WITH LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE').format(
                        sql.Identifier(USER),
                        sql.Literal(PASSWORD),
                    )
                )
            if not _db_exists(cur, DATABASE):
                cur.execute(
                    sql.SQL('CREATE DATABASE {} OWNER {}').format(
                        sql.Identifier(DATABASE),
                        sql.Identifier(USER),
                    )
                )
            cur.execute(
                sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(DATABASE))
            )
            cur.execute(
                sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                    sql.Identifier(DATABASE),
                    sql.Identifier(USER),
                )
            )

    db_url = ADMIN_URL.rsplit('/', 1)[0] + '/' + DATABASE
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
            cur.execute(
                sql.SQL('CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}').format(
                    sql.Identifier(SCHEMA),
                    sql.Identifier(USER),
                )
            )
            cur.execute(
                sql.SQL('REVOKE ALL ON SCHEMA {} FROM PUBLIC').format(sql.Identifier(SCHEMA))
            )
            cur.execute(
                sql.SQL('GRANT USAGE, CREATE ON SCHEMA {} TO {}').format(
                    sql.Identifier(SCHEMA),
                    sql.Identifier(USER),
                )
            )
            cur.execute(
                sql.SQL(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} '
                    'GRANT ALL ON TABLES TO {}'
                ).format(
                    sql.Identifier(USER),
                    sql.Identifier(SCHEMA),
                    sql.Identifier(USER),
                )
            )
            cur.execute(
                sql.SQL(
                    'ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} '
                    'GRANT ALL ON SEQUENCES TO {}'
                ).format(
                    sql.Identifier(USER),
                    sql.Identifier(SCHEMA),
                    sql.Identifier(USER),
                )
            )
            cur.execute(
                sql.SQL('GRANT ALL ON ALL TABLES IN SCHEMA {} TO {}').format(
                    sql.Identifier(SCHEMA),
                    sql.Identifier(USER),
                )
            )
            cur.execute(
                sql.SQL('GRANT ALL ON ALL SEQUENCES IN SCHEMA {} TO {}').format(
                    sql.Identifier(SCHEMA),
                    sql.Identifier(USER),
                )
            )

    print(f'provisioned database={DATABASE} user={USER} schema={SCHEMA}')


if __name__ == '__main__':
    provision()
