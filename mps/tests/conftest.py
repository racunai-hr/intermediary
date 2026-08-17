from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

os.environ.setdefault('GATEWAY_JWT_SECRET', 'test-gateway-secret')
os.environ.setdefault('GATEWAY_JWT_ISS', 'racunai-api')
os.environ.setdefault('GATEWAY_JWT_AUD', 'racunai-intermediary')

TEST_OIB = '36619131370'
OTHER_OIB = '12345678901'


def postgres_url() -> str | None:
    return os.environ.get('GATEWAY_TEST_DATABASE_URL') or os.environ.get('GATEWAY_DATABASE_URL')


def pytest_configure():
    url = postgres_url()
    if url:
        os.environ['GATEWAY_DATABASE_URL'] = url
        from alembic import command
        from alembic.config import Config

        import sys

        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        cfg = Config(str(root / 'alembic.ini'))
        cfg.set_main_option('script_location', str(root / 'alembic'))
        cfg.set_main_option('sqlalchemy.url', url)
        command.upgrade(cfg, 'head')


requires_postgres = pytest.mark.skipif(
    not postgres_url(),
    reason='GATEWAY_DATABASE_URL or GATEWAY_TEST_DATABASE_URL is required',
)


def make_token(**overrides) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        'iss': os.environ['GATEWAY_JWT_ISS'],
        'aud': os.environ['GATEWAY_JWT_AUD'],
        'exp': now + timedelta(minutes=5),
        'jti': str(uuid.uuid4()),
        'sub': 'racunai-api',
        'scope': 'gateway.read gateway.write gateway.admin',
        'taxpayers': ['*'],
    }
    payload.update(overrides)
    return jwt.encode(payload, os.environ['GATEWAY_JWT_SECRET'], algorithm='HS256')


def auth_header(**overrides) -> dict[str, str]:
    return {'Authorization': f'Bearer {make_token(**overrides)}'}


@pytest.fixture
def client(monkeypatch):
    url = postgres_url()
    if not url:
        pytest.skip('PostgreSQL URL is required')
    monkeypatch.setenv('GATEWAY_DATABASE_URL', url)
    from app.gateway.settings import get_gateway_settings
    from app.gateway.db import configure_engine

    get_gateway_settings.cache_clear()
    configure_engine()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session(client) -> Session:
    from app.gateway.db import get_engine
    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=get_engine(), expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def compose_file() -> Path:
    return Path('/opt/stacks/racunai.hr/docker-compose.yml')
