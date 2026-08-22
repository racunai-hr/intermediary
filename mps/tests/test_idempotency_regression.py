from __future__ import annotations

import uuid
from typing import Callable

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.gateway.db import get_engine
from app.gateway.errors import GatewayError
from app.gateway.models import Binding, IdempotencyKey, Reconciliation
from app.gateway.services.reconcile import run_reconciliation
from tests.conftest import TEST_OIB, requires_postgres
from tests.test_gateway_api import UBL, _configure_outbound, _headers
from tests.super_http import CRED_REF


def _fresh_oib() -> str:
    return f'{uuid.uuid4().int % 10**11:011d}'


def _tracking_session_factory() -> tuple[Callable[[], Session], list[Session], list[Session]]:
    closed: list[Session] = []
    rolled_back: list[Session] = []
    real_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)

    def factory() -> Session:
        session = real_factory()
        original_close = session.close
        original_rollback = session.rollback

        def tracked_close() -> None:
            closed.append(session)
            original_close()

        def tracked_rollback() -> None:
            rolled_back.append(session)
            original_rollback()

        session.close = tracked_close  # type: ignore[method-assign]
        session.rollback = tracked_rollback  # type: ignore[method-assign]
        return session

    return factory, closed, rolled_back


@requires_postgres
def test_idempotency_replay_skips_dispatch_write(client, monkeypatch):
    from app.gateway.settings import get_gateway_settings
    from tests.super_http import credentials_json

    import app.gateway.routes.v1 as v1_routes

    monkeypatch.setenv('GATEWAY_SUPER_CREDENTIALS_JSON', credentials_json())
    get_gateway_settings.cache_clear()
    monkeypatch.setattr(v1_routes, 'process_attempt', lambda *args, **kwargs: None)

    dispatch_calls: list[int] = []
    original_dispatch = v1_routes._dispatch_write

    def counting_dispatch(*args, **kwargs):
        dispatch_calls.append(1)
        return original_dispatch(*args, **kwargs)

    monkeypatch.setattr(v1_routes, '_dispatch_write', counting_dispatch)
    _configure_outbound(client)

    key = str(uuid.uuid4())
    payload = {
        'document_id': str(uuid.uuid4()),
        'taxpayer_oib': '11111111111',
        'direction': 'OUTBOUND',
        'document_type': 'INVOICE',
        'ubl': UBL,
    }
    first = client.post('/v1/outbound/documents', json=payload, headers=_headers(key))
    assert first.status_code == 202, first.text

    second = client.post('/v1/outbound/documents', json=payload, headers=_headers(key))
    assert second.status_code == 202, second.text
    assert second.json()['attempt_id'] == first.json()['attempt_id']
    assert len(dispatch_calls) == 1


@requires_postgres
def test_reconciliation_replay_invokes_run_reconciliation_once(client, monkeypatch):
    import app.gateway.routes.v1 as v1_routes

    run_calls: list[int] = []
    original_run = v1_routes.run_reconciliation

    def counting_run(*args, **kwargs):
        run_calls.append(1)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(v1_routes, 'run_reconciliation', counting_run)

    key = str(uuid.uuid4())
    first = client.post(
        f'/v1/taxpayers/{TEST_OIB}/reconciliations',
        json={},
        headers=_headers(key),
    )
    assert first.status_code == 202, first.text

    second = client.post(
        f'/v1/taxpayers/{TEST_OIB}/reconciliations',
        json={},
        headers=_headers(key),
    )
    assert second.status_code == 202, second.text
    assert second.json()['reconciliation_id'] == first.json()['reconciliation_id']
    assert len(run_calls) == 1


@requires_postgres
def test_idempotency_replay_preserves_final_stored_response(client, db_session, monkeypatch):
    import app.gateway.routes.v1 as v1_routes

    update_calls: list[dict] = []
    original_update = v1_routes.update_stored_response

    def counting_update(session, *, principal, key, http_status, body):
        update_calls.append(body)
        return original_update(
            session,
            principal=principal,
            key=key,
            http_status=http_status,
            body=body,
        )

    monkeypatch.setattr(v1_routes, 'update_stored_response', counting_update)

    key = str(uuid.uuid4())
    first = client.post(
        f'/v1/taxpayers/{TEST_OIB}/reconciliations',
        json={},
        headers=_headers(key),
    )
    assert first.status_code == 202, first.text

    final_body = {
        **first.json(),
        'status': 'COMPLETED',
        'marker': 'final-success-payload',
    }
    row = db_session.query(IdempotencyKey).filter_by(key=key).one()
    row.http_status = 202
    row.response_body = final_body
    db_session.commit()
    updates_before_replay = len(update_calls)

    second = client.post(
        f'/v1/taxpayers/{TEST_OIB}/reconciliations',
        json={},
        headers=_headers(key),
    )
    assert second.status_code == 202, second.text
    assert second.json() == final_body
    assert len(update_calls) == updates_before_replay

    db_session.expire_all()
    row_after = db_session.query(IdempotencyKey).filter_by(key=key).one()
    assert row_after.response_body == final_body


@requires_postgres
def test_run_reconciliation_missing_job_closes_session():
    factory, closed, _rolled_back = _tracking_session_factory()
    run_reconciliation(factory, uuid.uuid4())
    assert len(closed) == 1


@requires_postgres
def test_run_reconciliation_non_super_binding_closes_session(db_session):
    oib = _fresh_oib()
    job = Reconciliation(
        taxpayer_oib=oib,
        status='QUEUED',
        error_code=None,
        error_message=None,
        retryable=False,
    )
    binding = Binding(
        taxpayer_oib=oib,
        provider='other',
        status='ACTIVE',
        credential_ref='unused',
    )
    db_session.add(job)
    db_session.add(binding)
    db_session.commit()
    job_id = job.reconciliation_id

    factory, closed, _rolled_back = _tracking_session_factory()
    run_reconciliation(factory, job_id)
    assert len(closed) == 1

    verify = sessionmaker(bind=get_engine(), expire_on_commit=False)()
    try:
        stored = verify.get(Reconciliation, job_id)
        assert stored is not None
        assert stored.status == 'FAILED'
        assert stored.error_code == 'CAPABILITY_NOT_SUPPORTED'
    finally:
        verify.close()


@requires_postgres
def test_run_reconciliation_credential_failure_closes_session(db_session, monkeypatch):
    oib = _fresh_oib()
    job = Reconciliation(
        taxpayer_oib=oib,
        status='QUEUED',
        error_code=None,
        error_message=None,
        retryable=False,
    )
    binding = Binding(
        taxpayer_oib=oib,
        provider='super',
        status='ACTIVE',
        credential_ref='missing-cred',
    )
    db_session.add(job)
    db_session.add(binding)
    db_session.commit()
    job_id = job.reconciliation_id

    def bad_resolve(self, credential_ref):
        raise GatewayError('CREDENTIAL_ERROR', 'resolve failed', 409, retryable=False)

    monkeypatch.setattr(
        'app.gateway.adapters.super.adapter.SuperAdapter.resolve',
        bad_resolve,
    )

    factory, closed, _rolled_back = _tracking_session_factory()
    run_reconciliation(factory, job_id)
    assert len(closed) == 1

    verify = sessionmaker(bind=get_engine(), expire_on_commit=False)()
    try:
        stored = verify.get(Reconciliation, job_id)
        assert stored is not None
        assert stored.status == 'FAILED'
        assert stored.error_code == 'CREDENTIAL_ERROR'
    finally:
        verify.close()


@requires_postgres
def test_run_reconciliation_setup_exception_rolls_back_and_closes_session(db_session, monkeypatch):
    from app.gateway.settings import get_gateway_settings
    from tests.super_http import credentials_json

    monkeypatch.setenv('GATEWAY_SUPER_CREDENTIALS_JSON', credentials_json())
    get_gateway_settings.cache_clear()

    oib = _fresh_oib()
    job = Reconciliation(
        taxpayer_oib=oib,
        status='QUEUED',
        error_code=None,
        error_message=None,
        retryable=False,
    )
    binding = Binding(
        taxpayer_oib=oib,
        provider='super',
        status='ACTIVE',
        credential_ref=CRED_REF,
    )
    db_session.add(job)
    db_session.add(binding)
    db_session.commit()
    job_id = job.reconciliation_id

    def exploding_checkpoint(*args, **kwargs):
        raise RuntimeError('setup exploded')

    monkeypatch.setattr(
        'app.gateway.services.reconcile.checkpoint_filters',
        exploding_checkpoint,
    )

    factory, closed, rolled_back = _tracking_session_factory()
    with pytest.raises(RuntimeError, match='setup exploded'):
        run_reconciliation(factory, job_id)

    assert len(closed) == 1
    assert len(rolled_back) == 1
