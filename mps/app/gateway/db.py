from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.gateway.settings import GatewaySettings, get_gateway_settings

SCHEMA = 'gateway'

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def create_gateway_engine(settings: GatewaySettings | None = None) -> Engine:
    settings = settings or get_gateway_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={'options': f'-csearch_path={SCHEMA}'},
    )


def configure_engine(settings: GatewaySettings | None = None) -> Engine:
    global _engine, _SessionLocal
    settings = settings or get_gateway_settings()
    _engine = create_gateway_engine(settings)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def check_database(settings: GatewaySettings | None = None) -> None:
    engine = configure_engine(settings)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))


def get_session() -> Generator[Session, None, None]:
    if _SessionLocal is None:
        configure_engine()
    assert _SessionLocal is not None
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
