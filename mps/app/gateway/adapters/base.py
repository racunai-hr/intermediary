from __future__ import annotations

from typing import Protocol

from app.gateway.errors import capability_not_supported


class ProviderAdapter(Protocol):
    name: str

    def capabilities(self) -> dict: ...


class UnimplementedAdapter:
    name = 'unimplemented'

    def capabilities(self) -> dict:
        return {
            'document_types': ['INVOICE', 'CREDIT_NOTE'],
            'supports': {
                'outbound_send': False,
                'outbound_payment': False,
                'inbound_intake': False,
                'inbound_e_reporting_rejection': False,
                'inbound_workflow_status': False,
                'participant_lookup': False,
            },
        }

    def require_capability(self, capability: str) -> None:
        raise capability_not_supported(f'{capability} is not implemented for this provider.')
