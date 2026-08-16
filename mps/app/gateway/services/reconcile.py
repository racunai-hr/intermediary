from __future__ import annotations

from sqlalchemy.orm import Session

from app.gateway.canonical import require_oib
from app.gateway.models import Reconciliation


def start_reconciliation(session: Session, taxpayer_oib: str) -> dict:
    require_oib(taxpayer_oib)
    job = Reconciliation(
        taxpayer_oib=taxpayer_oib,
        status='FAILED',
        error_code='CAPABILITY_NOT_SUPPORTED',
        error_message='Provider reconciliation requires an adapter.',
        retryable=False,
    )
    session.add(job)
    session.flush()
    return serialize_reconciliation(job)


def get_reconciliation(session: Session, reconciliation_id: str) -> Reconciliation:
    from uuid import UUID

    from app.gateway.errors import document_not_found, invalid_request

    try:
        ident = UUID(reconciliation_id)
    except ValueError as exc:
        raise invalid_request('reconciliation_id must be a UUID.') from exc
    job = session.get(Reconciliation, ident)
    if job is None:
        raise document_not_found('Reconciliation was not found.')
    return job


def serialize_reconciliation(job: Reconciliation) -> dict:
    body = {
        'reconciliation_id': str(job.reconciliation_id),
        'taxpayer_oib': job.taxpayer_oib,
        'status': job.status,
    }
    if job.error_code:
        body['error'] = {
            'code': job.error_code,
            'message': job.error_message,
            'retryable': job.retryable,
        }
    return body
