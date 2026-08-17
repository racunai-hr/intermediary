from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from app.gateway.adapters.super.credentials import SuperCredential
from app.gateway.adapters.super.mapping import payment_method_code
from app.gateway.adapters.super.xmlutil import decode_strict_b64, encode_ubl_b64, parse_ubl_xml
from app.gateway.errors import invalid_ubl
from app.gateway.settings import get_gateway_settings

@dataclass(frozen=True)
class SuperCallResult:
    ok: bool
    ambiguous: bool
    payload: dict
    invoice_guid: str | None = None
    unique_id: str | None = None
    error_code: str | None = None
    wire_form: dict[str, str] | None = None


class SuperHttpError(Exception):
    def __init__(self, message: str, *, ambiguous: bool, retryable: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous
        self.retryable = retryable


class TokenCache:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._tokens: dict[str, tuple[str, datetime]] = {}

    def lock_for(self, credential_ref: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(credential_ref, threading.Lock())

    def get(self, credential_ref: str, skew_seconds: int) -> str | None:
        cached = self._tokens.get(credential_ref)
        if not cached:
            return None
        token, expires = cached
        if datetime.now(timezone.utc) + timedelta(seconds=skew_seconds) >= expires:
            return None
        return token

    def set(self, credential_ref: str, token: str, expires_at: datetime) -> None:
        self._tokens[credential_ref] = (token, expires_at)

    def clear(self, credential_ref: str) -> None:
        self._tokens.pop(credential_ref, None)


_TOKEN_CACHE = TokenCache()
before_request_hook: Callable[[], None] | None = None
transport_factory: Callable[[], httpx.BaseTransport | None] | None = None


def format_payment_amount(amount: str | Decimal) -> str:
    value = amount if isinstance(amount, Decimal) else Decimal(amount)
    return f'{value:.2f}'


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


class SuperHttpClient:
    def __init__(self, credential: SuperCredential):
        self.credential = credential
        settings = get_gateway_settings()
        self._connect_timeout = settings.super_connect_timeout
        self._read_timeout = settings.super_read_timeout
        self._write_timeout = settings.super_write_timeout
        self._max_bytes = settings.super_max_response_bytes
        self._skew = settings.super_token_skew_seconds
        transport = transport_factory() if transport_factory else None
        self._client = httpx.Client(
            base_url=credential.base_url,
            timeout=httpx.Timeout(
                self._read_timeout,
                connect=self._connect_timeout,
                write=self._write_timeout,
            ),
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SuperHttpClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _token(self, force: bool = False) -> str:
        ref = self.credential.credential_ref
        lock = _TOKEN_CACHE.lock_for(ref)
        with lock:
            if not force:
                cached = _TOKEN_CACHE.get(ref, self._skew)
                if cached:
                    return cached
            payload = self._request(
                'POST',
                '/Token',
                data={
                    'grant_type': 'password',
                    'username': self.credential.username,
                    'password': self.credential.password,
                },
                authenticated=False,
                safe_read=True,
            )
            token = payload.get('access_token')
            if not token:
                raise SuperHttpError('Token endpoint did not return access_token', ambiguous=False)
            expires_in = int(payload.get('expires_in') or 840)
            _TOKEN_CACHE.set(
                ref,
                token,
                datetime.now(timezone.utc) + timedelta(seconds=max(expires_in - self._skew, 30)),
            )
            return token

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str],
        authenticated: bool,
        safe_read: bool,
        retried_401: bool = False,
        retried_connect: bool = False,
    ) -> dict[str, Any]:
        if before_request_hook is not None:
            before_request_hook()
        url = urljoin(self.credential.base_url + '/', path.lstrip('/'))
        if _host(url) != _host(self.credential.base_url):
            raise SuperHttpError('Refusing cross-host Super request', ambiguous=False)
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Accept': 'application/json',
        }
        if authenticated:
            headers['Authorization'] = f'Bearer {self._token()}'
        try:
            response = self._client.request(method, path, data=data, headers=headers)
        except httpx.ConnectTimeout as exc:
            if not retried_connect:
                return self._request(
                    method,
                    path,
                    data=data,
                    authenticated=authenticated,
                    safe_read=safe_read,
                    retried_401=retried_401,
                    retried_connect=True,
                )
            raise SuperHttpError(
                'Provider connect timeout',
                ambiguous=not safe_read,
                retryable=safe_read,
            ) from exc
        except httpx.TimeoutException as exc:
            raise SuperHttpError(
                'Provider timeout',
                ambiguous=not safe_read,
                retryable=safe_read,
            ) from exc
        except httpx.TransportError as exc:
            raise SuperHttpError(
                'Provider disconnected',
                ambiguous=not safe_read,
                retryable=safe_read,
            ) from exc

        location = response.headers.get('Location')
        if response.is_redirect and location:
            if _host(urljoin(url, location)) != _host(self.credential.base_url):
                raise SuperHttpError('Refusing cross-host Super redirect', ambiguous=False)

        if response.status_code == 401 and authenticated and not retried_401:
            _TOKEN_CACHE.clear(self.credential.credential_ref)
            return self._request(
                method,
                path,
                data=data,
                authenticated=True,
                safe_read=safe_read,
                retried_401=True,
                retried_connect=retried_connect,
            )

        if response.status_code == 429 and safe_read:
            retry_after = response.headers.get('Retry-After', '1')
            try:
                delay = min(max(float(retry_after), 0), 5)
            except ValueError:
                delay = 1
            time.sleep(delay)
            return self._request(
                method,
                path,
                data=data,
                authenticated=authenticated,
                safe_read=True,
                retried_401=retried_401,
                retried_connect=True,
            )

        content = response.content
        if len(content) > self._max_bytes:
            raise SuperHttpError('Provider response exceeds size limit', ambiguous=not safe_read)
        content_length = response.headers.get('Content-Length')
        if content_length and int(content_length) > self._max_bytes:
            raise SuperHttpError('Provider response exceeds size limit', ambiguous=not safe_read)

        if response.status_code >= 500:
            raise SuperHttpError(
                'Provider returned 5xx',
                ambiguous=not safe_read,
                retryable=safe_read,
            )
        if response.status_code == 429:
            raise SuperHttpError('Provider rate-limited a write', ambiguous=True)
        if response.status_code >= 400:
            raise SuperHttpError(f'Provider returned {response.status_code}', ambiguous=False)

        try:
            payload = response.json()
        except ValueError as exc:
            raise SuperHttpError('Provider returned non-JSON', ambiguous=not safe_read) from exc
        if not isinstance(payload, dict):
            raise SuperHttpError('Provider returned a non-object JSON body', ambiguous=not safe_read)
        error = payload.get('ErrorMessage') or payload.get('errorMessage')
        if error:
            raise SuperHttpError(str(error), ambiguous=False)
        return payload

    def _post(self, path: str, data: dict[str, str], *, write: bool) -> dict[str, Any]:
        body = {**data, 'MessageId': data.get('MessageId') or str(uuid.uuid4())}
        return self._request(
            'POST',
            path,
            data=body,
            authenticated=True,
            safe_read=not write,
        )

    def check_participant(self, scheme: str, identifier: str) -> dict[str, Any]:
        return self._post(
            '/api/Ams/CheckParticipant',
            {'Scheme': scheme, 'Identifier': identifier},
            write=False,
        )

    def send_sending_invoice_ubl(self, ubl: str, document_type: int) -> SuperCallResult:
        form = {
            'CompanyGuid': self.credential.company_guid,
            'Base64EncodedUbl': encode_ubl_b64(ubl),
            'UblDocumentType': str(document_type),
        }
        try:
            payload = self._post('/api/SendingInvoice/SendSendingInvoiceUbl', form, write=True)
        except SuperHttpError as exc:
            return SuperCallResult(
                ok=False,
                ambiguous=exc.ambiguous,
                payload={},
                error_code='AMBIGUOUS_PROVIDER_RESULT' if exc.ambiguous else 'PROVIDER_UNAVAILABLE',
                wire_form={'UblDocumentType': form['UblDocumentType']},
            )
        guid = payload.get('Guid') or payload.get('guid')
        unique = payload.get('UniqueId') or payload.get('uniqueId')
        if not guid:
            return SuperCallResult(
                ok=False,
                ambiguous=True,
                payload=payload,
                error_code='AMBIGUOUS_PROVIDER_RESULT',
                wire_form={'UblDocumentType': form['UblDocumentType']},
            )
        return SuperCallResult(
            ok=True,
            ambiguous=False,
            payload=payload,
            invoice_guid=str(guid),
            unique_id=str(unique) if unique is not None else None,
            wire_form={'UblDocumentType': form['UblDocumentType']},
        )

    def get_sending_invoice_statuses(
        self,
        guids: list[str],
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        data = {'CompanyGuid': self.credential.company_guid}
        if date_from:
            data['DateFrom'] = date_from
        if date_to:
            data['DateTo'] = date_to
        if guids:
            data['SendingInvoiceGuidList'] = ','.join(guids)
        payload = self._post('/api/SendingInvoice/GetSendingInvoiceStatuses', data, write=False)
        return payload.get('SendingInvoiceStatuses') or payload.get('InvoiceStatuses') or []

    def get_invoice_list(
        self,
        *,
        date_from: str | None = None,
        date_to: str | None = None,
        received_from: str | None = None,
        received_to: str | None = None,
        unique_from: int | None = None,
        unique_to: int | None = None,
    ) -> list[dict]:
        data = {'CompanyGuid': self.credential.company_guid}
        if date_from:
            data['DateFrom'] = date_from
        if date_to:
            data['DateTo'] = date_to
        if received_from:
            data['ReceivedDateFrom'] = received_from
        if received_to:
            data['ReceivedDateTo'] = received_to
        if unique_from is not None:
            data['UniqueIdFrom'] = str(unique_from)
        if unique_to is not None:
            data['UniqueIdTo'] = str(unique_to)
        payload = self._post('/api/Invoice/GetInvoiceList', data, write=False)
        return payload.get('Invoices') or []

    def get_invoice_ubl(self, guid: str) -> str:
        payload = self._post('/api/Invoice/GetInvoice', {'Guid': guid}, write=False)
        encoded = payload.get('InvoiceUBL') or payload.get('invoiceUBL') or ''
        xml = decode_strict_b64(str(encoded))
        if len(xml) > get_gateway_settings().super_max_response_bytes:
            raise invalid_ubl('Decoded UBL exceeds size limit.')
        return parse_ubl_xml(xml)

    def reject_invoice(self, guid: str, reason_type: int, description: str) -> SuperCallResult:
        form = {
            'CompanyGuid': self.credential.company_guid,
            'Guid': guid,
            'RejectReasonType': str(reason_type),
            'RejectionReasonDescription': description or 'Rejected',
        }
        try:
            payload = self._post('/api/EReporting/RejectInvoice', form, write=True)
        except SuperHttpError as exc:
            return SuperCallResult(
                ok=False,
                ambiguous=exc.ambiguous,
                payload={},
                error_code='AMBIGUOUS_PROVIDER_RESULT' if exc.ambiguous else 'PROVIDER_UNAVAILABLE',
            )
        return SuperCallResult(ok=True, ambiguous=False, payload=payload, invoice_guid=guid)

    def add_payment(
        self,
        guid: str,
        payment_date: str,
        amount: str,
        payment_method: str,
        mark_paid_in_full: bool,
    ) -> SuperCallResult:
        wire_amount = format_payment_amount(amount)
        form = {
            'CompanyGuid': self.credential.company_guid,
            'Guid': guid,
            'PaymentDate': payment_date,
            'PaymentAmount': wire_amount,
            'PaymentMethod': str(payment_method_code(payment_method)),
            'MarkAsPaidInFull': 'true' if mark_paid_in_full else 'false',
        }
        try:
            payload = self._post('/api/EReporting/AddPaymentForSendingInvoice', form, write=True)
        except SuperHttpError as exc:
            return SuperCallResult(
                ok=False,
                ambiguous=exc.ambiguous,
                payload={},
                error_code='AMBIGUOUS_PROVIDER_RESULT' if exc.ambiguous else 'PROVIDER_UNAVAILABLE',
                wire_form={'PaymentAmount': wire_amount},
            )
        return SuperCallResult(
            ok=True,
            ambiguous=False,
            payload=payload,
            invoice_guid=guid,
            wire_form={'PaymentAmount': wire_amount},
        )


def last_form_from_request(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode('utf-8'), keep_blank_values=True)
