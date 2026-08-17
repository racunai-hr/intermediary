from __future__ import annotations

from app.gateway.adapters.super.client import SuperCallResult, SuperHttpClient
from app.gateway.adapters.super.credentials import CredentialResolver, SuperCredential, default_resolver
from app.gateway.adapters.super.mapping import ubl_document_type
from app.gateway.errors import capability_not_supported, provider_not_configured
from app.gateway.models import Binding


class SuperAdapter:
    name = 'super'

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
        with SuperHttpClient(credential) as client:
            return client.get_invoice_list(**filters)

    def get_inbound_ubl(self, credential: SuperCredential, invoice_guid: str) -> str:
        with SuperHttpClient(credential) as client:
            return client.get_invoice_ubl(invoice_guid)

    def list_outbound_statuses(self, credential: SuperCredential, guids: list[str], **filters) -> list[dict]:
        with SuperHttpClient(credential) as client:
            return client.get_sending_invoice_statuses(guids, **filters)


def require_super_credential(adapter: SuperAdapter, credential_ref: str | None) -> SuperCredential:
    if not credential_ref:
        raise provider_not_configured()
    return adapter.resolve(credential_ref)
