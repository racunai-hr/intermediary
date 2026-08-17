from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from app.gateway.models import Document, OutboundProviderConfig
from app.gateway.services.dispatch import process_attempt
from tests.conftest import auth_header, requires_postgres
from tests.super_http import COMPANY_A, COMPANY_B, CRED_REF, OTHER_CRED_REF
from tests.test_gateway_api import UBL, _configure_outbound, _headers
from tests.test_super_adapter import super_env  # noqa: F401

def unique_oib() -> str:
    return f'{uuid.uuid4().int % 10**11:011d}'


@requires_postgres
def test_get_put_hides_credential_ref(client, super_env):
    oib = unique_oib()
    created = _configure_outbound(client, oib=oib, credential_ref=CRED_REF)
    assert created['status'] == 'CONFIGURED'
    assert created['generation'] == 1
    assert created['provider_account_key'] == COMPANY_A
    assert created['outbound_readiness']['ready'] is True
    assert 'credential_ref' not in created
    fetched = client.get(
        f'/v1/taxpayers/{oib}/outbound-provider',
        headers=auth_header(scope='gateway.read'),
    )
    assert fetched.status_code == 200
    assert 'credential_ref' not in fetched.text
    assert fetched.json()['id'] == created['id']


@requires_postgres
def test_send_without_inbound_binding(client, super_env):
    oib = unique_oib()
    _configure_outbound(client, oib=oib)
    response = client.post(
        '/v1/outbound/documents',
        json={
            'document_id': str(uuid.uuid4()),
            'taxpayer_oib': oib,
            'direction': 'OUTBOUND',
            'document_type': 'INVOICE',
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body['exchange_status'] == 'SUBMITTED'
    assert body['binding_id'] is None
    assert body['outbound_provider_generation'] == 1
    assert 'credential_ref' not in response.text
    assert super_env.send_calls == 1


@requires_postgres
def test_unconfigured_409_does_not_capture_key_then_succeeds(client, super_env):
    oib = unique_oib()
    document_id = str(uuid.uuid4())
    key = str(uuid.uuid4())
    payload = {
        'document_id': document_id,
        'taxpayer_oib': oib,
        'direction': 'OUTBOUND',
        'document_type': 'INVOICE',
        'ubl': UBL,
    }
    first = client.post('/v1/outbound/documents', json=payload, headers=_headers(key))
    assert first.status_code == 409
    _configure_outbound(client, oib=oib)
    second = client.post('/v1/outbound/documents', json=payload, headers=_headers(key))
    assert second.status_code == 202, second.text
    assert second.json()['exchange_status'] == 'SUBMITTED'


@requires_postgres
def test_credential_change_does_not_rebind_document(client, super_env, db_session):
    oib = unique_oib()
    first = _configure_outbound(client, oib=oib, credential_ref=CRED_REF)
    sent = client.post(
        '/v1/outbound/documents',
        json={
            'document_id': str(uuid.uuid4()),
            'taxpayer_oib': oib,
            'direction': 'OUTBOUND',
            'document_type': 'INVOICE',
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert sent.status_code == 202
    second = _configure_outbound(client, oib=oib, credential_ref=OTHER_CRED_REF)
    assert second['generation'] == 2
    assert second['provider_account_key'] == COMPANY_B
    document = db_session.get(Document, uuid.UUID(sent.json()['document_id']))
    assert str(document.outbound_provider_config_id) == first['id']
    assert document.outbound_provider_generation == 1
    assert document.provider_account_key == COMPANY_A
    old = db_session.get(OutboundProviderConfig, uuid.UUID(first['id']))
    assert old.status == 'SUPERSEDED'


@requires_postgres
def test_parallel_puts_distinct_generations(client, super_env, db_session):
    oib = unique_oib()

    def put(reason):
        return client.put(
            f'/v1/taxpayers/{oib}/outbound-provider',
            json={
                'provider': 'super',
                'credential_ref': CRED_REF,
                'status': 'CONFIGURED',
                'change_reason': reason,
            },
            headers=_headers(str(uuid.uuid4())),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(put, ['one', 'two']))
    assert all(item.status_code == 200 for item in results)
    generations = sorted(item.json()['generation'] for item in results)
    assert generations == [1, 2]
    actual = [
        row
        for row in db_session.query(OutboundProviderConfig).filter_by(taxpayer_oib=oib)
        if row.status in {'CONFIGURED', 'DISABLED'}
    ]
    assert len(actual) == 1
    assert actual[0].generation == 2


@requires_postgres
def test_disable_blocks_new_and_pending_dispatch(client, super_env, monkeypatch):
    oib = unique_oib()
    _configure_outbound(client, oib=oib)
    monkeypatch.setattr('app.gateway.routes.v1.process_attempt', lambda *args, **kwargs: None)
    queued = client.post(
        '/v1/outbound/documents',
        json={
            'document_id': str(uuid.uuid4()),
            'taxpayer_oib': oib,
            'direction': 'OUTBOUND',
            'document_type': 'INVOICE',
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert queued.status_code == 202
    assert queued.json()['exchange_status'] == 'QUEUED'
    _configure_outbound(client, oib=oib, status='DISABLED', change_reason='stop')
    blocked_new = client.post(
        '/v1/outbound/documents',
        json={
            'document_id': str(uuid.uuid4()),
            'taxpayer_oib': oib,
            'direction': 'OUTBOUND',
            'document_type': 'INVOICE',
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert blocked_new.status_code == 409
    process_attempt(queued.json()['attempt_id'])
    fetched = client.get(
        f"/v1/outbound/documents/{queued.json()['document_id']}",
        headers=auth_header(scope='gateway.read'),
    )
    assert fetched.json()['processing']['reason'] == 'BLOCKED_PROVIDER_DISABLED'
    assert super_env.send_calls == 0


@requires_postgres
def test_payment_uses_stamped_account(client, super_env):
    oib = unique_oib()
    _configure_outbound(client, oib=oib, credential_ref=CRED_REF)
    sent = client.post(
        '/v1/outbound/documents',
        json={
            'document_id': str(uuid.uuid4()),
            'taxpayer_oib': oib,
            'direction': 'OUTBOUND',
            'document_type': 'INVOICE',
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4())),
    )
    _configure_outbound(client, oib=oib, credential_ref=OTHER_CRED_REF)
    payment = client.post(
        f"/v1/outbound/documents/{sent.json()['document_id']}/payments",
        json={
            'payment_id': str(uuid.uuid4()),
            'paid_at': '2026-08-16T12:00:00Z',
            'amount': '123.45',
            'currency': 'EUR',
            'payment_method': 'BANK_TRANSFER',
            'settlement': 'FULL',
        },
        headers=_headers(str(uuid.uuid4())),
    )
    assert payment.status_code == 202
    assert super_env.payment_calls == 1
    assert sent.json()['provider_refs']['company_guid'] == COMPANY_A
