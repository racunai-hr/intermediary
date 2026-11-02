from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.gateway.adapters.super.client import SuperCallResult, SuperHttpClient
from app.gateway.adapters.super.credentials import CredentialResolver, SuperCredential, default_resolver
from app.gateway.adapters.super.mapping import (
    MAPPING_VERSION,
    map_inbound_status,
    map_outbound_status,
    ubl_document_type,
)
from app.gateway.errors import capability_not_supported, provider_not_configured
from app.gateway.models import Binding
from app.gateway.settings import get_gateway_settings


def _poll_today() -> date:
    return datetime.now(timezone.utc).date()


def inbound_received_from(checkpoint_start: str | None) -> str:
    """Earlier of the checkpoint start and a fixed lookback.

    The checkpoint watermark moves to today even when a poll stored nothing.
    A two-day overlap would then walk past an invoice the previous response missed.
    """
    lookback_start = _poll_today() - timedelta(days=get_gateway_settings().super_inbound_lookback_days)
    if not checkpoint_start:
        return lookback_start.isoformat()
    given = date.fromisoformat(checkpoint_start[:10])
    return min(given, lookback_start).isoformat()


class SuperAdapter:
    name = 'super'
    mapping_version = MAPPING_VERSION

    def __init__(self, resolver: CredentialResolver | None = None):
        self.resolver = resolver or default_resolver()

    def capabilities(self) -> dict:
        return {
            'document_types': ['INVOICE', 'CREDIT_NOTE'],
            'supports': {
                'outbound_send': True,
                'outbound_payment': True,
                'inbound_intake': True,
                'inbound_e_reporting_rejection': True,
                'inbound_workflow_status': True,
                'participant_lookup': True,
            },
        }

    def readiness(self, binding: Binding | None, credential_ref: str | None = None) -> dict:
        ref = credential_ref or (binding.credential_ref if binding else None)
        available = False
        if ref:
            try:
                self.resolver.resolve(ref)
                available = True
            except Exception:
                available = False
        return {
            'configured': bool(ref),
            'active_binding': bool(binding and binding.status == 'ACTIVE'),
            'credential_available': available,
        }

    def resolve(self, credential_ref: str | None) -> SuperCredential:
        return self.resolver.resolve(credential_ref)

    def lookup(self, credential: SuperCredential, scheme: str, identifier: str) -> dict:
        with SuperHttpClient(credential) as client:
            payload = client.check_participant(scheme, identifier)
        published = payload.get('IsPublished')
        if published is None:
            published = payload.get('isPublished')
        return {
            'reachable': bool(published),
            'scheme': scheme,
            'identifier': identifier,
        }

    def send(self, credential: SuperCredential, ubl: str, document_type: str) -> SuperCallResult:
        ubl_type = ubl_document_type(document_type)
        if ubl_type is None:
            raise capability_not_supported(f'{document_type} is not supported for Super UBL send.')
        with SuperHttpClient(credential) as client:
            return client.send_sending_invoice_ubl(ubl, ubl_type)

    def add_payment(
        self,
        credential: SuperCredential,
        invoice_guid: str,
        payment_date: str,
        amount: str,
        payment_method: str,
        mark_paid_in_full: bool,
    ) -> SuperCallResult:
        with SuperHttpClient(credential) as client:
            return client.add_payment(
                invoice_guid, payment_date, amount, payment_method, mark_paid_in_full
            )

    def reject(
        self,
        credential: SuperCredential,
        invoice_guid: str,
        reason_type: int,
        description: str,
    ) -> SuperCallResult:
        with SuperHttpClient(credential) as client:
            return client.reject_invoice(invoice_guid, reason_type, description)

    def list_inbound(self, credential: SuperCredential, **filters) -> list[dict]:
        # Issue date is ANDed with received date, so an invoice issued before the
        # window and received inside it would be dropped. UniqueId is a global
        # counter; UniqueIdFrom = last-50 does not cover the gap between polls.
        with SuperHttpClient(credential) as client:
            return client.get_invoice_list(
                received_from=inbound_received_from(filters.get('received_from')),
                received_to=filters.get('received_to'),
            )

    def get_inbound_ubl(self, credential: SuperCredential, invoice_guid: str) -> str:
        with SuperHttpClient(credential) as client:
            return client.get_invoice_ubl(invoice_guid)

    def list_outbound_statuses(self, credential: SuperCredential, guids: list[str], **filters) -> list[dict]:
        with SuperHttpClient(credential) as client:
            return client.get_sending_invoice_statuses(
                guids,
                date_from=filters.get('date_from'),
                date_to=filters.get('date_to'),
            )

    def list_item_id(self, item: dict) -> str:
        return str(item.get('Guid') or item.get('guid') or '')

    def map_inbound_item(self, item: dict):
        return map_inbound_status(item.get('InvoiceStatus') or item.get('invoiceStatus'))

    def map_outbound_item(self, item: dict):
        return map_outbound_status(item.get('SendingInvoiceStatus') or item.get('sendingInvoiceStatus'))


def require_super_credential(adapter: SuperAdapter, credential_ref: str | None) -> SuperCredential:
    if not credential_ref:
        raise provider_not_configured()
    return adapter.resolve(credential_ref)
