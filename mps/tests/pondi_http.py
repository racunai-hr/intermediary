from __future__ import annotations

import json

import httpx

from app.gateway.adapters.pondi import client as pondi_client

UBL = '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"><ID>1</ID></Invoice>'
CRED_REF = 'pondi-test-cred'
VAT_ID = '91381354893'
DOCUMENT_ID = 4242


def credentials_json() -> str:
    return json.dumps(
        {
            CRED_REF: {
                'base_url': 'https://pondi.test.example',
                'api_key': 'test-pondi-key',
                'vat_id': VAT_ID,
                'software_id': 'RACUNAI',
            }
        }
    )


class PondiScript:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.send_calls = 0
        self.list_calls = 0
        self.get_calls = 0
        self.ping_response: dict | Exception = {'status': 'OK', 'message': 'ok'}
        self.send_response: dict | Exception = {'id': DOCUMENT_ID, 'insertedOn': '2026-09-15T12:00:00Z', 'message': 'ok'}
        self.incoming: list[dict] = []
        self.outgoing: list[dict] = []
        self.ubl = UBL
        self.published = True

    def install(self) -> None:
        script = self

        def factory():
            return httpx.MockTransport(script)

        pondi_client.transport_factory = factory

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith('/api/v2/ping'):
            return _result(self.ping_response)
        if path.endswith('/api/v2/document/send'):
            self.send_calls += 1
            return _result(self.send_response)
        if path.endswith('/api/v2/document/incoming'):
            self.list_calls += 1
            return httpx.Response(200, json=_slice_list(self.incoming, request))
        if path.endswith('/api/v2/document/outgoing'):
            return httpx.Response(200, json=_slice_list(self.outgoing, request))
        if '/api/v2/document/get/' in path:
            self.get_calls += 1
            return httpx.Response(200, json={'document': self.ubl})
        if path.endswith('/api/v2/ams/check'):
            return httpx.Response(200, json={'publishedOnAms': self.published})
        return httpx.Response(404, json={'error': 'unknown path'})


def _slice_list(rows: list[dict], request: httpx.Request) -> list[dict]:
    limit = int(request.url.params.get('limit') or 50)
    offset = int(request.url.params.get('offset') or 0)
    return rows[offset : offset + limit]


def _result(value: dict | Exception) -> httpx.Response:
    if isinstance(value, Exception):
        raise value
    return httpx.Response(200, json=value)
