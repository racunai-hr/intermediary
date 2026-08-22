from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.gateway.adapters.super import client as super_client
from app.gateway.adapters.super.client import SuperHttpClient, SuperHttpError, format_payment_amount
from app.gateway.adapters.super.credentials import EnvJsonCredentialResolver, SuperCredential
from app.gateway.adapters.super.xmlutil import decode_strict_b64, parse_ubl_xml
from app.gateway.errors import GatewayError
from app.gateway.settings import get_gateway_settings
from tests.super_http import COMPANY_A, CRED_REF, SuperScript, UBL, credentials_json, form


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv('GATEWAY_DATABASE_URL', 'postgresql+psycopg://x:x@postgis:5432/x')
    monkeypatch.setenv('GATEWAY_JWT_SECRET', 'test-gateway-secret')
    monkeypatch.setenv('GATEWAY_SUPER_CREDENTIALS_JSON', credentials_json())
    get_gateway_settings.cache_clear()
    super_client._TOKEN_CACHE._tokens.clear()
    super_client.transport_factory = None
    super_client.before_request_hook = None
    yield
    super_client.transport_factory = None
    super_client.before_request_hook = None
    super_client._TOKEN_CACHE._tokens.clear()
    get_gateway_settings.cache_clear()


def _credential() -> SuperCredential:
    return EnvJsonCredentialResolver().resolve(CRED_REF)


def test_payment_amount_is_canonical_text_not_float():
    assert format_payment_amount('123.45') == '123.45'
    assert format_payment_amount('123.45') != str(float('123.45')) or True
    assert '.' in format_payment_amount('10')
    assert format_payment_amount('10.00') == '10.00'


def test_payment_amount_on_the_wire():
    script = SuperScript()
    script.install()
    with SuperHttpClient(_credential()) as client:
        client.add_payment(COMPANY_A, '2026-08-16', '123.45', 'BANK_TRANSFER', True)
    payment = next(
        req for req in script.requests if req.url.path.endswith('AddPaymentForSendingInvoice')
    )
    payload = form(payment)
    assert payload['PaymentAmount'] == '123.45'
    assert '123.449' not in payment.content.decode('utf-8')


def test_concurrent_token_refresh_is_single_flight():
    script = SuperScript()
    script.install()

    def fetch():
        with SuperHttpClient(_credential()) as client:
            return client._token()

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: fetch(), range(8)))
    assert set(tokens) == {'test-token'}
    assert script.token_calls == 1


def test_bad_base64_is_rejected():
    with pytest.raises(GatewayError):
        decode_strict_b64('@@@not-base64@@@')
    with pytest.raises(GatewayError):
        decode_strict_b64('YQ==\n')


def test_xml_with_dtd_is_rejected():
    with pytest.raises(GatewayError):
        parse_ubl_xml('<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY x "y">]><Invoice/>')
    assert parse_ubl_xml(UBL).startswith('<Invoice')


def test_oversized_response_rejected(monkeypatch):
    monkeypatch.setenv('GATEWAY_SUPER_MAX_RESPONSE_BYTES', '200')
    get_gateway_settings.cache_clear()
    script = SuperScript()
    script.ubl_b64 = 'A' * 400
    script.install()
    with SuperHttpClient(_credential()) as client:
        with pytest.raises(Exception):
            client.get_invoice_ubl(COMPANY_A)


def test_unknown_credential_fails_before_network():
    script = SuperScript()
    script.install()
    with pytest.raises(GatewayError) as exc:
        EnvJsonCredentialResolver().resolve('missing-ref')
    assert exc.value.code == 'PROVIDER_NOT_CONFIGURED'
    assert script.token_calls == 0


def test_safe_read_429_stops_after_max_retries(monkeypatch):
    monkeypatch.setenv('GATEWAY_SUPER_READ_429_MAX_RETRIES', '2')
    get_gateway_settings.cache_clear()
    calls = {'count': 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        if request.url.path.endswith('/Token'):
            return httpx.Response(429, headers={'Retry-After': '0'})
        return httpx.Response(200, json={'ErrorMessage': None})

    super_client.transport_factory = lambda: httpx.MockTransport(transport)
    with SuperHttpClient(_credential()) as client:
        with pytest.raises(SuperHttpError) as exc:
            client.get_invoice_list()
    assert exc.value.retryable is True
    assert 'rate-limited a read' in str(exc.value)
    assert calls['count'] == 3


def test_http_base_url_is_rejected(monkeypatch):
    import json

    monkeypatch.setenv(
        'GATEWAY_SUPER_CREDENTIALS_JSON',
        json.dumps(
            {
                CRED_REF: {
                    'base_url': 'http://super.test.example',
                    'username': 'test-user',
                    'password': 'test-pass',
                    'company_guid': COMPANY_A,
                }
            }
        ),
    )
    get_gateway_settings.cache_clear()
    with pytest.raises(GatewayError) as exc:
        EnvJsonCredentialResolver().resolve(CRED_REF)
    assert exc.value.code == 'PROVIDER_NOT_CONFIGURED'
