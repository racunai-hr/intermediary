from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from app.gateway.adapters.super import client as super_client

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'super'
UBL = (FIXTURES / 'ubl.xml').read_text()
UBL_B64 = base64.b64encode(UBL.encode('utf-8')).decode('ascii')
COMPANY_A = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
COMPANY_B = 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff'
INVOICE_GUID = '11111111-2222-3333-4444-555555555555'
CRED_REF = 'test-cred'
OTHER_CRED_REF = 'other-cred'


def credentials_json() -> str:
    return json.dumps(
        {
            CRED_REF: {
                'base_url': 'https://super.test.example',
                'username': 'test-user',
                'password': 'test-pass',
                'company_guid': COMPANY_A,
            },
            OTHER_CRED_REF: {
                'base_url': 'https://super.test.example',
                'username': 'other-user',
                'password': 'other-pass',
                'company_guid': COMPANY_B,
            },
        }
    )


class SuperScript:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.send_calls = 0
        self.payment_calls = 0
        self.reject_calls = 0
        self.list_calls = 0
        self.send_response: dict | Exception | None = {
            'Guid': INVOICE_GUID,
            'UniqueId': 1,
            'ErrorMessage': None,
        }
        self.payment_response: dict | Exception | None = {'ErrorMessage': None}
        self.reject_response: dict | Exception | None = {'ErrorMessage': None}
        self.invoices: list[dict] = []
        self.statuses: list[dict] = []
        self.ubl_b64 = UBL_B64
        self.published = True

    def install(self) -> None:
        script = self

        def factory():
            return httpx.MockTransport(script)

        super_client.transport_factory = factory

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith('/Token'):
            self.token_calls += 1
            return _json({'access_token': 'test-token', 'expires_in': 840})
        if path.endswith('/api/Ams/CheckParticipant'):
            return _json({'IsPublished': self.published, 'ErrorMessage': None})
        if path.endswith('/api/SendingInvoice/SendSendingInvoiceUbl'):
            self.send_calls += 1
            return _result(self.send_response)
        if path.endswith('/api/EReporting/AddPaymentForSendingInvoice'):
            self.payment_calls += 1
            return _result(self.payment_response)
        if path.endswith('/api/EReporting/RejectInvoice'):
            self.reject_calls += 1
            return _result(self.reject_response)
        if path.endswith('/api/Invoice/GetInvoiceList'):
            self.list_calls += 1
            return _json({'Invoices': self.invoices, 'ErrorMessage': None})
        if path.endswith('/api/Invoice/GetInvoice'):
            return _json({'InvoiceUBL': self.ubl_b64, 'ErrorMessage': None})
        if path.endswith('/api/SendingInvoice/GetSendingInvoiceStatuses'):
            return _json({'SendingInvoiceStatuses': self.statuses, 'ErrorMessage': None})
        return _json({'ErrorMessage': 'unknown path'}, status=404)


def _result(value: dict | Exception | None) -> httpx.Response:
    if isinstance(value, Exception):
        raise value
    if value is None:
        return _json({'ErrorMessage': None})
    return _json(value)


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def form(request: httpx.Request) -> dict[str, str]:
    parsed = super_client.last_form_from_request(request)
    return {key: values[0] for key, values in parsed.items()}
