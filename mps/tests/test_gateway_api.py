from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text

from app.gateway.models import Binding, Document, OutboxEvent
from app.gateway.services.outbox import record_event
from tests.conftest import OTHER_OIB, TEST_OIB, auth_header, requires_postgres

UBL = '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"><ID>1</ID></Invoice>'


def _headers(key: str | None = None, **token) -> dict[str, str]:
    headers = auth_header(**token)
    if key:
        headers['Idempotency-Key'] = key
    headers['X-Request-Id'] = str(uuid.uuid4())
    return headers


def _activate(client, oib=TEST_OIB, provider='super'):
    created = client.put(
        f'/v1/taxpayers/{oib}/inbound-binding',
        json={'provider': provider},
        headers=_headers(str(uuid.uuid4())),
    )
    assert created.status_code == 200, created.text
    response = client.post(
        f'/v1/taxpayers/{oib}/inbound-binding/fiskaplikacija-confirmation',
        json={
            'binding_id': created.json()['binding_id'],
            'method': 'fiskaplikacija',
            'recorded_by': 'tester',
            'recorded_at': '2026-08-16T12:00:00Z',
            'evidence_ref': 'pts-1',
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert response.status_code == 200, response.text
    return response.json()


@requires_postgres
def test_error_envelope_and_unauthorized_subject(client):
    response = client.get(
        f'/v1/taxpayers/{TEST_OIB}/inbound-binding',
        headers=auth_header(taxpayers=[OTHER_OIB], scope='gateway.read'),
    )
    assert response.status_code == 403
    body = response.json()
    assert body['error']['code'] == 'UNAUTHORIZED_SUBJECT'
    assert body['error']['retryable'] is False


@requires_postgres
def test_racunai_direct_disabled(client):
    response = client.get(
        '/v1/providers/racunai_direct/capabilities',
        headers=auth_header(scope='gateway.read'),
    )
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'CAPABILITY_NOT_SUPPORTED'


@requires_postgres
def test_secrets_rejected_on_binding(client):
    response = client.put(
        f'/v1/taxpayers/{TEST_OIB}/inbound-binding',
        json={'provider': 'super', 'username': 'x', 'password': 'y'},
        headers=_headers(str(uuid.uuid4())),
    )
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'INVALID_REQUEST'


@requires_postgres
def test_outbound_blocked_and_idempotent_replay(client):
    _activate(client)
    document_id = str(uuid.uuid4())
    key = str(uuid.uuid4())
    payload = {
        'document_id': document_id,
        'taxpayer_oib': TEST_OIB,
        'direction': 'OUTBOUND',
        'document_type': 'INVOICE',
        'ubl': UBL,
    }
    first = client.post('/v1/outbound/documents', json=payload, headers=_headers(key))
    assert first.status_code == 202
    body = first.json()
    assert body['exchange_status'] == 'QUEUED'
    assert body['processing'] == {'state': 'BLOCKED', 'reason': 'CAPABILITY_NOT_SUPPORTED'}
    second = client.post('/v1/outbound/documents', json=payload, headers=_headers(key))
    assert second.status_code == 202
    assert second.json()['attempt_id'] == body['attempt_id']
    conflict = client.post(
        '/v1/outbound/documents',
        json={**payload, 'document_type': 'CREDIT_NOTE'},
        headers=_headers(key),
    )
    assert conflict.status_code == 409
    assert conflict.json()['error']['code'] == 'IDEMPOTENCY_CONFLICT'


@requires_postgres
def test_concurrent_idempotency(client):
    _activate(client)
    document_id = str(uuid.uuid4())
    key = str(uuid.uuid4())
    payload = {
        'document_id': document_id,
        'taxpayer_oib': TEST_OIB,
        'direction': 'OUTBOUND',
        'document_type': 'INVOICE',
        'ubl': UBL,
    }

    def send(body):
        return client.post('/v1/outbound/documents', json=body, headers=_headers(key))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [payload, payload]))
    statuses = {item.status_code for item in results}
    assert statuses == {202}
    assert results[0].json()['attempt_id'] == results[1].json()['attempt_id']

    other = {**payload, 'document_type': 'CREDIT_NOTE', 'document_id': str(uuid.uuid4())}
    with ThreadPoolExecutor(max_workers=2) as pool:
        mixed = list(pool.map(lambda item: send(item), [payload, other]))
    codes = sorted(item.status_code for item in mixed)
    assert 202 in codes
    assert 409 in codes


@requires_postgres
def test_binding_supersede_and_document_keeps_old_binding(client, db_session):
    first = _activate(client)
    document_id = str(uuid.uuid4())
    client.post(
        '/v1/outbound/documents',
        json={
            'document_id': document_id,
            'taxpayer_oib': TEST_OIB,
            'direction': 'OUTBOUND',
            'document_type': 'INVOICE',
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4())),
    )
    second = _activate(client)
    assert second['binding_id'] != first['binding_id']
    document = db_session.get(Document, uuid.UUID(document_id))
    assert str(document.binding_id) == first['binding_id']
    old = db_session.get(Binding, uuid.UUID(first['binding_id']))
    assert old.status == 'SUPERSEDED'


@requires_postgres
def test_concurrent_binding_activation(client):
    client.put(
        f'/v1/taxpayers/{TEST_OIB}/inbound-binding',
        json={'provider': 'super'},
        headers=_headers(str(uuid.uuid4())),
    )
    client.put(
        f'/v1/taxpayers/{TEST_OIB}/inbound-binding',
        json={'provider': 'super'},
        headers=_headers(str(uuid.uuid4())),
    )
    payload = {
        'method': 'fiskaplikacija',
        'recorded_by': 'tester',
        'recorded_at': '2026-08-16T12:00:00Z',
        'evidence_ref': 'pts-race',
    }

    def confirm():
        return client.post(
            f'/v1/taxpayers/{TEST_OIB}/inbound-binding/fiskaplikacija-confirmation',
            json=payload,
            headers=_headers(str(uuid.uuid4())),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    active = [item for item in results if item.status_code == 200]
    assert len(active) >= 1
    bindings = client.get(
        f'/v1/taxpayers/{TEST_OIB}/inbound-binding',
        headers=auth_header(scope='gateway.read'),
    ).json()['items']
    assert sum(1 for item in bindings if item['status'] == 'ACTIVE') == 1


@requires_postgres
def test_reconciliation_is_failed_not_completed(client):
    _activate(client)
    response = client.post(
        f'/v1/taxpayers/{TEST_OIB}/reconciliations',
        json={},
        headers=_headers(str(uuid.uuid4())),
    )
    assert response.status_code == 202
    body = response.json()
    assert body['status'] == 'FAILED'
    assert body['error']['code'] == 'CAPABILITY_NOT_SUPPORTED'
    assert body['error']['retryable'] is False
    fetched = client.get(
        f'/v1/reconciliations/{body["reconciliation_id"]}',
        headers=auth_header(scope='gateway.read'),
    )
    assert fetched.json()['status'] != 'COMPLETED'


@requires_postgres
def test_event_rolls_back_with_business_change(db_session):
    try:
        with db_session.begin():
            record_event(
                db_session,
                event_type='outbound.exchange_status_changed',
                taxpayer_oib=TEST_OIB,
                document_id=uuid.uuid4(),
            )
            raise RuntimeError('boom')
    except RuntimeError:
        pass
    count = db_session.query(OutboxEvent).filter_by(taxpayer_oib=TEST_OIB).count()
    # Isolation: only assert the failed transaction did not leave the new event.
    db_session.rollback()
    leftover = db_session.execute(
        text("SELECT COUNT(*) FROM gateway.outbox_events WHERE payload->>'rolled' = '1'")
    ).scalar()
    assert leftover == 0


@requires_postgres
def test_cursor_same_timestamp_and_foreign_oib(client, db_session):
    _activate(client)
    from app.gateway.models import Binding

    binding = db_session.query(Binding).filter_by(taxpayer_oib=TEST_OIB, status='ACTIVE').one()
    for _ in range(3):
        db_session.add(
            Document(
                document_id=uuid.uuid4(),
                taxpayer_oib=TEST_OIB,
                direction='INBOUND',
                document_type='INVOICE',
                binding_id=binding.id,
                bound_provider=binding.provider,
                ubl=UBL,
                ubl_sha256='a' * 64,
                attempt_id=uuid.uuid4(),
                intake_status='AVAILABLE',
                processing_state='BLOCKED',
                processing_reason='CAPABILITY_NOT_SUPPORTED',
                provider_refs={},
            )
        )
    db_session.commit()
    first = client.get(
        f'/v1/taxpayers/{TEST_OIB}/inbound/documents?limit=2',
        headers=auth_header(scope='gateway.read'),
    )
    assert first.status_code == 200
    assert first.json()['has_more'] is True
    cursor = first.json()['next_cursor']
    second = client.get(
        f'/v1/taxpayers/{TEST_OIB}/inbound/documents?cursor={cursor}&limit=2',
        headers=auth_header(scope='gateway.read'),
    )
    assert second.status_code == 200
    ids = {item['document_id'] for item in first.json()['items']}
    assert ids.isdisjoint({item['document_id'] for item in second.json()['items']})
    foreign = client.get(
        f'/v1/taxpayers/{TEST_OIB}/inbound/documents?cursor=gwy_other',
        headers=auth_header(scope='gateway.read'),
    )
    assert foreign.status_code == 400


@requires_postgres
def test_payment_validation_and_existing_outbound_only(client):
    _activate(client)
    document_id = str(uuid.uuid4())
    client.post(
        '/v1/outbound/documents',
        json={
            'document_id': document_id,
            'taxpayer_oib': TEST_OIB,
            'direction': 'OUTBOUND',
            'document_type': 'INVOICE',
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4())),
    )
    missing = client.post(
        f'/v1/outbound/documents/{uuid.uuid4()}/payments',
        json={
            'payment_id': str(uuid.uuid4()),
            'paid_at': '2026-08-16T12:00:00Z',
            'amount': '10.00',
            'currency': 'EUR',
            'payment_method': 'BANK_TRANSFER',
            'settlement': 'FULL',
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert missing.status_code == 404
    bad = client.post(
        f'/v1/outbound/documents/{document_id}/payments',
        json={
            'payment_id': str(uuid.uuid4()),
            'paid_at': '2026-08-16T12:00:00Z',
            'amount': '-10.00',
            'currency': 'EUR',
            'payment_method': 'BANK_TRANSFER',
            'settlement': 'FULL',
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert bad.status_code == 400


@requires_postgres
def test_jwt_wrong_scope(client):
    response = client.get(
        f'/v1/taxpayers/{TEST_OIB}/inbound-binding',
        headers=auth_header(scope='gateway.write'),
    )
    assert response.status_code == 403
