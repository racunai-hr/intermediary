from __future__ import annotations

from app.gateway.adapters.pondi.client import PondiCallResult, PondiHttpClient, PondiHttpError
from app.gateway.adapters.pondi.credentials import CredentialResolver, PondiCredential, default_resolver
from app.gateway.adapters.pondi.mapping import (
    MAPPING_VERSION,
    list_item_id,
    map_inbound_status,
    map_outbound_status,
    ubl_document_type,
)
from app.gateway.errors import capability_not_supported, provider_not_configured
from app.gateway.models import Binding


class PondiAdapter:
    name = 'pondi'
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

    def resolve(self, credential_ref: str | None) -> PondiCredential:
        return self.resolver.resolve(credential_ref)

    def ping(self, credential: PondiCredential) -> dict:
        with PondiHttpClient(credential) as client:
            payload = client.ping()
        return payload if isinstance(payload, dict) else {'status': 'OK'}

    def lookup(self, credential: PondiCredential, scheme: str, identifier: str) -> dict:
        with PondiHttpClient(credential) as client:
            payload = client.check_ams(scheme, identifier)
        if not isinstance(payload, dict):
            payload = {}
        published = payload.get('publishedOnAms')
        if published is None:
            published = payload.get('published')
        return {
            'reachable': bool(published),
            'scheme': scheme,
            'identifier': identifier,
        }

    def send(self, credential: PondiCredential, ubl: str, document_type: str) -> PondiCallResult:
        if ubl_document_type(document_type) is None:
            raise capability_not_supported(f'{document_type} is not supported for Pondi UBL send.')
        with PondiHttpClient(credential) as client:
            return client.send_document(ubl, document_type)

    def add_payment(
        self,
        credential: PondiCredential,
        invoice_guid: str,
        payment_date: str,
        amount: str,
        payment_method: str,
        mark_paid_in_full: bool,
    ) -> PondiCallResult:
        del payment_date, amount, payment_method
        status = 7 if mark_paid_in_full else 8
        with PondiHttpClient(credential) as client:
            return client.change_status(invoice_guid, status)

    def reject(
        self,
        credential: PondiCredential,
        invoice_guid: str,
        reason_type: int,
        description: str,
    ) -> PondiCallResult:
        del reason_type
        with PondiHttpClient(credential) as client:
            return client.reject_document(invoice_guid, description)

    def list_inbound(self, credential: PondiCredential, **filters) -> list[dict]:
        with PondiHttpClient(credential) as client:
            return client.list_incoming(**filters)

    def get_inbound_ubl(self, credential: PondiCredential, invoice_guid: str) -> str:
        with PondiHttpClient(credential) as client:
            return client.get_document_ubl(invoice_guid)

    def fetch_inbound_with_ubl(self, credential: PondiCredential, **filters) -> list[tuple[dict, str | None]]:
        return self._fetch_with_ubl(credential, 'incoming', **filters)

    def fetch_outbound_with_ubl(self, credential: PondiCredential, **filters) -> list[tuple[dict, str | None]]:
        return self._fetch_with_ubl(credential, 'outgoing', **filters)

    def _fetch_with_ubl(
        self,
        credential: PondiCredential,
        kind: str,
        **filters,
    ) -> list[tuple[dict, str | None]]:
        lister = 'list_incoming' if kind == 'incoming' else 'list_outgoing'
        batch: list[tuple[dict, str | None]] = []
        with PondiHttpClient(credential) as client:
            for item in getattr(client, lister)(**filters):
                guid = list_item_id(item)
                if not guid:
                    batch.append((item, None))
                    continue
                try:
                    ubl = client.get_document_ubl(guid)
                except PondiHttpError:
                    batch.append((item, None))
                    continue
                batch.append((item, ubl))
        return batch

    def list_outbound_statuses(self, credential: PondiCredential, guids: list[str], **filters) -> list[dict]:
        wanted = {str(item) for item in guids}
        with PondiHttpClient(credential) as client:
            rows = client.list_outgoing(**filters)
        return [item for item in rows if list_item_id(item) in wanted]

    def list_item_id(self, item: dict) -> str:
        return list_item_id(item)

    def map_inbound_item(self, item: dict):
        return map_inbound_status(item.get('status'))

    def map_outbound_item(self, item: dict):
        return map_outbound_status(item.get('status'))


def require_pondi_credential(adapter: PondiAdapter, credential_ref: str | None) -> PondiCredential:
    if not credential_ref:
        raise provider_not_configured()
    return adapter.resolve(credential_ref)
