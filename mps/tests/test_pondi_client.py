from __future__ import annotations

import json

import httpx
import pytest

from app.gateway.adapters.pondi import client as pondi_client
from app.gateway.adapters.pondi.client import PondiHttpClient, PondiHttpError
from app.gateway.adapters.pondi.credentials import EnvJsonCredentialResolver
from app.gateway.adapters.registry import get_adapter
from app.gateway.errors import GatewayError
from app.gateway.settings import get_gateway_settings
from tests.pondi_http import CRED_REF, UBL, VAT_ID, credentials_json, PondiScript


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv('GATEWAY_DATABASE_URL', 'postgresql+psycopg://x:x@postgis:5432/x')
    monkeypatch.setenv('GATEWAY_JWT_SECRET', 'test-gateway-secret')
    monkeypatch.setenv('GATEWAY_PONDI_CREDENTIALS_JSON', credentials_json())
    get_gateway_settings.cache_clear()
    pondi_client.transport_factory = None
    pondi_client.before_request_hook = None
    yield
    pondi_client.transport_factory = None
    pondi_client.before_request_hook = None
    get_gateway_settings.cache_clear()


def _credential():
    return EnvJsonCredentialResolver().resolve(CRED_REF)


def test_registry_returns_pondi_adapter():
    adapter = get_adapter('pondi')
    assert adapter.name == 'pondi'
    assert adapter.capabilities()['supports']['outbound_send'] is True


def test_credentials_hide_api_key():
    credential = _credential()
    assert 'test-pondi-key' not in repr(credential)
    assert credential.vat_id == VAT_ID
    assert credential.account_key == VAT_ID


def test_unknown_credential_fails_before_network():
    script = PondiScript()
    script.install()
    with pytest.raises(GatewayError) as exc:
        EnvJsonCredentialResolver().resolve('missing-ref')
    assert exc.value.code == 'PROVIDER_NOT_CONFIGURED'
    assert script.send_calls == 0


def test_http_base_url_is_rejected(monkeypatch):
    monkeypatch.setenv(
        'GATEWAY_PONDI_CREDENTIALS_JSON',
        json.dumps(
            {
                CRED_REF: {
                    'base_url': 'http://pondi.test.example',
                    'api_key': 'test-pondi-key',
                    'vat_id': VAT_ID,
                }
            }
        ),
    )
    get_gateway_settings.cache_clear()
    with pytest.raises(GatewayError) as exc:
        EnvJsonCredentialResolver().resolve(CRED_REF)
    assert exc.value.code == 'PROVIDER_NOT_CONFIGURED'


def test_send_uses_authorization_header_and_software_id():
    script = PondiScript()
    script.install()
    with PondiHttpClient(_credential()) as client:
        result = client.send_document(UBL, 'INVOICE')
    assert result.ok is True
    assert result.invoice_guid == '4242'
    sent = next(req for req in script.requests if req.url.path.endswith('/api/v2/document/send'))
    assert sent.headers['Authorization'] == 'test-pondi-key'
    body = json.loads(sent.content.decode('utf-8'))
    assert body['softwareId'] == 'RACUNAI'
    assert '<' in body['document']
    assert 'test-pondi-key' not in str(result)


def test_timeout_on_send_is_ambiguous():
    script = PondiScript()
    script.send_response = httpx.ReadTimeout('slow')
    script.install()
    with PondiHttpClient(_credential()) as client:
        result = client.send_document(UBL, 'INVOICE')
    assert result.ok is False
    assert result.ambiguous is True


def test_list_incoming_and_get_ubl():
    script = PondiScript()
    script.incoming = [{'id': 7, 'status': 4}]
    script.install()
    with PondiHttpClient(_credential()) as client:
        rows = client.list_incoming(date_from='2026-01-01', date_to='2026-09-15')
        ubl = client.get_document_ubl('7')
    assert rows[0]['id'] == 7
    assert ubl.startswith('<Invoice')
    listed = next(req for req in script.requests if req.url.path.endswith('/document/incoming'))
    assert 'insertedFrom' in str(listed.url)


def test_list_incoming_paginates_until_short_page():
    script = PondiScript()
    script.incoming = [{'id': i, 'status': 4} for i in range(1, 121)]
    script.install()
    with PondiHttpClient(_credential()) as client:
        rows = client.list_incoming()
    assert [row['id'] for row in rows] == list(range(1, 121))
    assert script.list_calls == 3


def test_400_on_safe_read_is_not_ambiguous():
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={'error': 'bad', 'details': 'nope'})

    pondi_client.transport_factory = lambda: httpx.MockTransport(transport)
    with PondiHttpClient(_credential()) as client:
        with pytest.raises(PondiHttpError) as exc:
            client.list_incoming()
    assert exc.value.ambiguous is False
    assert 'nope' in str(exc.value)
