from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

from app.gateway.adapters.pondi.credentials import PondiCredential
from app.gateway.adapters.pondi.mapping import ubl_document_type
from app.gateway.errors import invalid_ubl
from app.gateway.settings import get_gateway_settings


@dataclass(frozen=True)
class PondiCallResult:
    ok: bool
    ambiguous: bool
    payload: dict
    invoice_guid: str | None = None
    unique_id: str | None = None
    error_code: str | None = None
    wire_form: dict[str, str] | None = None


class PondiHttpError(Exception):
    def __init__(self, message: str, *, ambiguous: bool, retryable: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous
        self.retryable = retryable


transport_factory: Callable[[], httpx.BaseTransport | None] | None = None
before_request_hook: Callable[[], None] | None = None


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


class PondiHttpClient:
    def __init__(self, credential: PondiCredential):
        self.credential = credential
        settings = get_gateway_settings()
        transport = transport_factory() if transport_factory else None
        self._client = httpx.Client(
            base_url=credential.base_url,
            timeout=httpx.Timeout(
                settings.super_read_timeout,
                connect=settings.super_connect_timeout,
                write=settings.super_write_timeout,
            ),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PondiHttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def ping(self) -> dict:
        return self._request('GET', '/api/v2/ping', safe_read=True)

    def send_document(self, ubl: str, document_type: str) -> PondiCallResult:
        ubl_type = ubl_document_type(document_type)
        if ubl_type is None:
            raise invalid_ubl(f'{document_type} is not supported for Pondi UBL send.')
        if not ubl or '<' not in ubl:
            raise invalid_ubl('ubl must be an XML document.')
        try:
            payload = self._request(
                'POST',
                '/api/v2/document/send',
                json={
                    'document': ubl,
                    'softwareId': self.credential.software_id,
                },
                safe_read=False,
            )
        except PondiHttpError as exc:
            return PondiCallResult(
                ok=False,
                ambiguous=exc.ambiguous,
                payload={'details': str(exc)},
                error_code='AMBIGUOUS_PROVIDER_RESULT' if exc.ambiguous else 'PROVIDER_UNAVAILABLE',
            )
        if not isinstance(payload, dict):
            return PondiCallResult(ok=False, ambiguous=True, payload={}, error_code='AMBIGUOUS_PROVIDER_RESULT')
        doc_id = payload.get('id')
        if doc_id is None:
            return PondiCallResult(
                ok=False,
                ambiguous=True,
                payload=payload,
                error_code='AMBIGUOUS_PROVIDER_RESULT',
            )
        return PondiCallResult(
            ok=True,
            ambiguous=False,
            payload=payload,
            invoice_guid=str(doc_id),
            unique_id=str(doc_id),
        )

    def list_incoming(self, **filters) -> list[dict]:
        return self._list_pages('/api/v2/document/incoming', filters)

    def list_outgoing(self, **filters) -> list[dict]:
        return self._list_pages('/api/v2/document/outgoing', filters)

    def _list_pages(self, path: str, filters: dict) -> list[dict]:
        page_size = int(filters.get('limit') or 50)
        offset = int(filters.get('offset') or 0)
        single_page = 'offset' in filters
        rows: list[dict] = []
        seen: set[str] = set()
        while True:
            params = _list_params({**filters, 'limit': page_size, 'offset': offset})
            page = _as_list(self._request('GET', path, params=params, safe_read=True))
            if not page:
                break
            for item in page:
                ident = str(item.get('id') or '')
                if ident:
                    if ident in seen:
                        continue
                    seen.add(ident)
                rows.append(item)
            if single_page or len(page) < page_size:
                break
            offset += page_size
            if offset > 50_000:
                break
        return rows

    def get_document_ubl(self, document_id: str) -> str:
        payload = self._request('GET', f'/api/v2/document/get/{document_id}', safe_read=True)
        ubl = payload.get('document')
        if not isinstance(ubl, str) or '<' not in ubl:
            raise PondiHttpError('Pondi document UBL is missing', ambiguous=False)
        return ubl

    def change_status(self, document_id: str, status: int) -> PondiCallResult:
        try:
            payload = self._request(
                'POST',
                f'/api/v2/document/changestatus/{document_id}',
                json={
                    'status': status,
                    'changedOn': datetime.now(timezone.utc).isoformat(),
                },
                safe_read=False,
            )
        except PondiHttpError as exc:
            return PondiCallResult(
                ok=False,
                ambiguous=exc.ambiguous,
                payload={},
                invoice_guid=str(document_id),
                error_code='AMBIGUOUS_PROVIDER_RESULT' if exc.ambiguous else 'PROVIDER_UNAVAILABLE',
            )
        return PondiCallResult(
            ok=True,
            ambiguous=False,
            payload=payload if isinstance(payload, dict) else {},
            invoice_guid=str(document_id),
        )

    def reject_document(self, document_id: str, description: str) -> PondiCallResult:
        try:
            payload = self._request(
                'POST',
                f'/api/v2/ereporting/rejected/{document_id}',
                json={
                    'rejectionDate': datetime.now(timezone.utc).date().isoformat(),
                    'rejectionType': 1,
                    'rejectionReason': description or 'Rejected',
                },
                safe_read=False,
            )
        except PondiHttpError as exc:
            return PondiCallResult(
                ok=False,
                ambiguous=exc.ambiguous,
                payload={},
                invoice_guid=str(document_id),
                error_code='AMBIGUOUS_PROVIDER_RESULT' if exc.ambiguous else 'PROVIDER_UNAVAILABLE',
            )
        return PondiCallResult(
            ok=True,
            ambiguous=False,
            payload=payload if isinstance(payload, dict) else {},
            invoice_guid=str(document_id),
        )

    def check_ams(self, scheme: str, identifier: str) -> dict:
        return self._request(
            'POST',
            '/api/v2/ams/check',
            json={'schema': scheme, 'identifier': identifier},
            safe_read=True,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        safe_read: bool,
    ) -> dict | list:
        if before_request_hook is not None:
            before_request_hook()
        url = urljoin(self.credential.base_url + '/', path.lstrip('/'))
        if _host(url) != _host(self.credential.base_url):
            raise PondiHttpError('Refusing cross-host Pondi request', ambiguous=False)
        headers = {
            'Authorization': self.credential.api_key,
            'Accept': 'application/json',
        }
        if json is not None:
            headers['Content-Type'] = 'application/json'
        try:
            response = self._client.request(method, path, json=json, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise PondiHttpError('Provider timeout', ambiguous=not safe_read, retryable=safe_read) from exc
        except httpx.TransportError as exc:
            raise PondiHttpError(
                'Provider disconnected',
                ambiguous=not safe_read,
                retryable=safe_read,
            ) from exc
        if response.status_code >= 500:
            raise PondiHttpError(
                'Provider server error',
                ambiguous=not safe_read,
                retryable=True,
            )
        if response.status_code >= 400:
            detail = _error_text(response)
            raise PondiHttpError(detail, ambiguous=False, retryable=False)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise PondiHttpError('Provider returned non-JSON', ambiguous=not safe_read) from exc
        if not isinstance(payload, (dict, list)):
            raise PondiHttpError('Provider returned unexpected payload', ambiguous=False)
        return payload


def _list_params(filters: dict) -> dict[str, Any]:
    params: dict[str, Any] = {}
    date_from = filters.get('date_from') or filters.get('insertedFrom')
    date_to = filters.get('date_to') or filters.get('insertedTo')
    if date_from:
        params['insertedFrom'] = _as_datetime(str(date_from), end=False)
    if date_to:
        params['insertedTo'] = _as_datetime(str(date_to), end=True)
    if filters.get('limit'):
        params['limit'] = filters['limit']
    if filters.get('offset') is not None:
        params['offset'] = filters['offset']
    return params


def _as_datetime(value: str, *, end: bool) -> str:
    if 'T' in value:
        return value
    return f'{value}T23:59:59' if end else f'{value}T00:00:00'


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return f'Pondi HTTP {response.status_code}'
    if isinstance(data, dict):
        details = data.get('details') or data.get('error') or data.get('message')
        if details:
            return str(details)
    return f'Pondi HTTP {response.status_code}'


def _as_list(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ('items', 'documents', 'data'):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []
