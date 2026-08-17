from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.gateway.adapters.super import client as super_client
from app.gateway.models import Document
from app.gateway.services.dispatch import is_holding_db, process_attempt
from app.gateway.settings import get_gateway_settings
from tests.conftest import auth_header, requires_postgres
from tests.super_http import (
    COMPANY_A,
    COMPANY_B,
    CRED_REF,
    INVOICE_GUID,
    OTHER_CRED_REF,
    SuperScript,
    UBL,
    credentials_json,
)
from tests.test_gateway_api import _configure_outbound, _headers

SUPER_OIB = '11111111111'
FOREIGN_OIB = '22222222222'


@pytest.fixture
def super_env(monkeypatch, client):
    monkeypatch.setenv('GATEWAY_SUPER_CREDENTIALS_JSON', credentials_json())
    get_gateway_settings.cache_clear()
    super_client._TOKEN_CACHE._tokens.clear()
    script = SuperScript()
    script.install()
    yield script
    super_client.transport_factory = None
    super_client.before_request_hook = None
    super_client._TOKEN_CACHE._tokens.clear()
    get_gateway_settings.cache_clear()


def _activate(client, oib=SUPER_OIB, credential_ref=CRED_REF):
    payload = {'provider': 'super'}
    if credential_ref is not None:
        payload['credential_ref'] = credential_ref
    created = client.put(
        f'/v1/taxpayers/{oib}/inbound-binding',
        json=payload,
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    assert created.status_code == 200, created.text
    response = client.post(
        f'/v1/taxpayers/{oib}/inbound-binding/fiskaplikacija-confirmation',
        json={
            'binding_id': created.json()['binding_id'],
            'method': 'fiskaplikacija',
            'recorded_by': 'tester',
            'recorded_at': '2026-08-16T12:00:00Z',
            'evidence_ref': 'pts-super',
        },
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _send(client, oib=SUPER_OIB, document_type='INVOICE'):
    document_id = str(uuid.uuid4())
    response = client.post(
        '/v1/outbound/documents',
        json={
            'document_id': document_id,
            'taxpayer_oib': oib,
            'direction': 'OUTBOUND',
            'document_type': document_type,
            'ubl': UBL,
        },
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    return response


@requires_postgres
def test_super_capabilities_true_without_credential(client):
    response = client.get('/v1/providers/super/capabilities', headers=auth_header(scope='gateway.read'))
    assert response.status_code == 200
    body = response.json()
    assert body['supports']['outbound_send'] is True
    assert body['outbound_readiness']['ready'] is False
    assert body['inbound_readiness']['active_binding'] is False


@requires_postgres
def test_capability_true_missing_credential_is_not_configured(client, super_env):
    response = _send(client, oib=f'{uuid.uuid4().int % 10**11:011d}')
    assert response.status_code == 409
    assert response.json()['error']['code'] == 'PROVIDER_NOT_CONFIGURED'
    assert super_env.send_calls == 0


@requires_postgres
def test_two_workers_one_super_post(client, super_env, monkeypatch):
    _configure_outbound(client, oib=SUPER_OIB)
    monkeypatch.setattr('app.gateway.routes.v1.process_attempt', lambda *args, **kwargs: None)
    response = _send(client)
    assert response.status_code == 202
    attempt_id = response.json()['attempt_id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: process_attempt(attempt_id), range(2)))
    assert super_env.send_calls == 1


@requires_postgres
def test_db_transaction_not_open_during_http(client, super_env):
    seen = []

    def hook():
        seen.append(is_holding_db())

    super_client.before_request_hook = hook
    _configure_outbound(client, oib=SUPER_OIB)
    response = _send(client)
    assert response.status_code == 202
    assert response.json()['exchange_status'] == 'SUBMITTED'
    assert seen
    assert all(flag is False for flag in seen)


@requires_postgres
def test_timeout_on_send_does_not_retry(client, super_env):
    super_env.send_response = httpx.ReadTimeout('slow')
    _configure_outbound(client, oib=SUPER_OIB)
    response = _send(client)
    assert response.status_code == 202
    body = response.json()
    assert body['exchange_status'] == 'UNKNOWN'
    assert body['processing']['reason'] == 'AMBIGUOUS_PROVIDER_RESULT'
    assert super_env.send_calls == 1
    process_attempt(body['attempt_id'])
    assert super_env.send_calls == 1


@requires_postgres
def test_ambiguous_without_unique_proof_requires_review(client, super_env):
    super_env.send_response = {'ErrorMessage': None}
    _configure_outbound(client, oib=SUPER_OIB)
    response = _send(client)
    assert response.status_code == 202
    body = response.json()
    assert body['processing']['reason'] in {'REQUIRES_REVIEW', 'AMBIGUOUS_PROVIDER_RESULT'}
    assert body['provider_refs'].get('invoice_guid') is None


@requires_postgres
def test_inbound_same_guid_is_one_document(client, super_env):
    _activate(client)
    super_env.invoices = [
        {'Guid': INVOICE_GUID, 'UniqueId': 10, 'InvoiceStatus': 10},
        {'Guid': INVOICE_GUID, 'UniqueId': 10, 'InvoiceStatus': 10},
    ]
    first = client.post(
        f'/v1/taxpayers/{SUPER_OIB}/reconciliations',
        json={},
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    second = client.post(
        f'/v1/taxpayers/{SUPER_OIB}/reconciliations',
        json={},
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    assert first.json()['status'] in {'COMPLETED', 'COMPLETED_WITH_ERRORS'}
    assert second.json()['status'] in {'COMPLETED', 'COMPLETED_WITH_ERRORS'}
    listed = client.get(
        f'/v1/taxpayers/{SUPER_OIB}/inbound/documents',
        headers=auth_header(scope='gateway.read', taxpayers=['*']),
    )
    items = [
        item
        for item in listed.json()['items']
        if item['provider_refs'].get('invoice_guid') == INVOICE_GUID
    ]
    assert len(items) == 1


@requires_postgres
def test_same_guid_other_company_does_not_collide(client, super_env):
    _activate(client, oib=SUPER_OIB, credential_ref=CRED_REF)
    _activate(client, oib=FOREIGN_OIB, credential_ref=OTHER_CRED_REF)
    super_env.invoices = [{'Guid': INVOICE_GUID, 'UniqueId': 3, 'InvoiceStatus': 10}]
    client.post(
        f'/v1/taxpayers/{SUPER_OIB}/reconciliations',
        json={},
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    client.post(
        f'/v1/taxpayers/{FOREIGN_OIB}/reconciliations',
        json={},
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    first = [
        item
        for item in client.get(
            f'/v1/taxpayers/{SUPER_OIB}/inbound/documents',
            headers=auth_header(scope='gateway.read', taxpayers=['*']),
        ).json()['items']
        if item['provider_refs'].get('invoice_guid') == INVOICE_GUID
        and item['provider_refs'].get('company_guid') == COMPANY_A
    ]
    second = [
        item
        for item in client.get(
            f'/v1/taxpayers/{FOREIGN_OIB}/inbound/documents',
            headers=auth_header(scope='gateway.read', taxpayers=['*']),
        ).json()['items']
        if item['provider_refs'].get('invoice_guid') == INVOICE_GUID
        and item['provider_refs'].get('company_guid') == COMPANY_B
    ]
    assert len(first) == 1
    assert len(second) == 1
    assert first[0]['document_id'] != second[0]['document_id']
    assert first[0]['provider_refs']['company_guid'] == COMPANY_A
    assert second[0]['provider_refs']['company_guid'] == COMPANY_B


@requires_postgres
def test_status_for_other_oib_does_not_update(client, super_env, db_session):
    _configure_outbound(client, oib=SUPER_OIB)
    sent = _send(client)
    document_id = sent.json()['document_id']
    from app.gateway.services.inbound_pull import apply_outbound_status_row

    changed = apply_outbound_status_row(
        db_session,
        taxpayer_oib=FOREIGN_OIB,
        account_key=COMPANY_A,
        item={'Guid': INVOICE_GUID, 'SendingInvoiceStatus': 60},
    )
    assert changed is False
    db_session.expire_all()
    document = db_session.get(Document, uuid.UUID(document_id))
    assert document.exchange_status != 'DELIVERED'


@requires_postgres
def test_payment_and_reject_timeout_no_second_post(client, super_env):
    _activate(client)
    _configure_outbound(client, oib=SUPER_OIB)
    sent = _send(client)
    document_id = sent.json()['document_id']
    super_env.payment_response = httpx.ReadTimeout('slow')
    payment = client.post(
        f'/v1/outbound/documents/{document_id}/payments',
        json={
            'payment_id': str(uuid.uuid4()),
            'paid_at': '2026-08-16T12:00:00Z',
            'amount': '123.45',
            'currency': 'EUR',
            'payment_method': 'BANK_TRANSFER',
            'settlement': 'FULL',
        },
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    assert payment.status_code == 202
    assert payment.json()['fiscalization_status'] == 'UNKNOWN'
    assert super_env.payment_calls == 1
    process_attempt(payment.json()['attempt_id'])
    assert super_env.payment_calls == 1

    inbound_guid = '99999999-aaaa-bbbb-cccc-dddddddddddd'
    super_env.invoices = [{'Guid': inbound_guid, 'UniqueId': 8, 'InvoiceStatus': 10}]
    client.post(
        f'/v1/taxpayers/{SUPER_OIB}/reconciliations',
        json={},
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    inbound_id = next(
        item['document_id']
        for item in client.get(
            f'/v1/taxpayers/{SUPER_OIB}/inbound/documents',
            headers=auth_header(scope='gateway.read', taxpayers=['*']),
        ).json()['items']
        if item['provider_refs'].get('invoice_guid') == inbound_guid
    )
    super_env.reject_response = httpx.ReadTimeout('slow')
    rejected = client.post(
        f'/v1/inbound/documents/{inbound_id}/e-reporting/rejection',
        json={'reason_code': 'OTHER', 'reason_text': 'test'},
        headers=_headers(str(uuid.uuid4()), taxpayers=['*']),
    )
    assert rejected.status_code == 202
    assert rejected.json()['e_reporting_status'] == 'UNKNOWN'
    assert super_env.reject_calls == 1


@requires_postgres
def test_lookup_uses_super(client, super_env):
    _activate(client)
    response = client.post(
        '/v1/participants/lookup',
        json={
            'scheme': 'iso6523-actorid-upis',
            'identifier': '9934:33333333333',
            'document_type': 'INVOICE',
            'taxpayer_oib': SUPER_OIB,
        },
        headers=auth_header(scope='gateway.write', taxpayers=['*']),
    )
    assert response.status_code == 200
    assert response.json()['reachable'] is True
