from __future__ import annotations

from sqlalchemy.orm import Session

from app.gateway.adapters.registry import get_adapter
from app.gateway.adapters.super.adapter import SuperAdapter
from app.gateway.canonical import require_oib
from app.gateway.errors import GatewayError, document_not_found, invalid_request
from app.gateway.models import Binding, Document, Reconciliation
from app.gateway.services.inbound_pull import (
    apply_outbound_status_row,
    checkpoint_filters,
    fetch_inbound_batch,
    persist_inbound_batch,
)


def start_reconciliation(session: Session, taxpayer_oib: str) -> dict:
    require_oib(taxpayer_oib)
    job = Reconciliation(
        taxpayer_oib=taxpayer_oib,
        status='QUEUED',
        error_code=None,
        error_message=None,
        retryable=False,
    )
    session.add(job)
    session.flush()
    return serialize_reconciliation(job)


def run_reconciliation(session_factory, reconciliation_id) -> None:
    from uuid import UUID

    ident = reconciliation_id if not isinstance(reconciliation_id, str) else UUID(reconciliation_id)
    session = session_factory()
    try:
        job = session.get(Reconciliation, ident)
        if job is None:
            session.commit()
            return
        job.status = 'RUNNING'
        session.commit()
        binding = (
            session.query(Binding)
            .filter_by(taxpayer_oib=job.taxpayer_oib, status='ACTIVE')
            .one_or_none()
        )
        adapter = get_adapter(binding.provider) if binding else None
        if binding is None or not isinstance(adapter, SuperAdapter):
            job.status = 'FAILED'
            job.error_code = 'CAPABILITY_NOT_SUPPORTED' if binding else 'BINDING_NOT_ACTIVE'
            job.error_message = 'Reconciliation requires an active Super binding.'
            job.retryable = False
            session.commit()
            return
        try:
            credential = adapter.resolve(binding.credential_ref)
        except GatewayError as exc:
            job.status = 'FAILED'
            job.error_code = exc.code
            job.error_message = exc.message
            job.retryable = False
            session.commit()
            return
        checkpoint, filters = checkpoint_filters(
            session, job.taxpayer_oib, credential.company_guid, 'inbound_list'
        )
        outbound_docs = (
            session.query(Document)
            .filter_by(
                taxpayer_oib=job.taxpayer_oib,
                direction='OUTBOUND',
                bound_provider='super',
            )
            .filter(Document.provider_invoice_guid.isnot(None))
            .all()
        )
        guids = [doc.provider_invoice_guid for doc in outbound_docs if doc.provider_invoice_guid]
        session.commit()
    except Exception:
        session.rollback()
        session.close()
        raise
    session.close()

    inbound_error = None
    outbound_error = None
    batch: list = []
    statuses: list = []
    try:
        batch = fetch_inbound_batch(adapter, credential, filters)
    except GatewayError as exc:
        inbound_error = exc
    try:
        if guids:
            statuses = adapter.list_outbound_statuses(credential, guids)
    except GatewayError as exc:
        outbound_error = exc

    session = session_factory()
    try:
        job = session.get(Reconciliation, ident)
        binding = (
            session.query(Binding)
            .filter_by(taxpayer_oib=job.taxpayer_oib, status='ACTIVE')
            .one_or_none()
        )
        checkpoint, _ = checkpoint_filters(session, job.taxpayer_oib, credential.company_guid, 'inbound_list')
        inbound_stats = {'created': 0, 'errors': 0}
        if inbound_error is None and binding is not None:
            inbound_stats = persist_inbound_batch(
                session,
                binding=binding,
                credential=credential,
                batch=batch,
                checkpoint=checkpoint,
            )
        updated = 0
        if outbound_error is None:
            for item in statuses:
                if apply_outbound_status_row(
                    session,
                    taxpayer_oib=job.taxpayer_oib,
                    account_key=credential.company_guid,
                    item=item,
                ):
                    updated += 1
        if inbound_error and outbound_error:
            job.status = 'FAILED'
            job.error_code = inbound_error.code
            job.error_message = inbound_error.message
            job.retryable = inbound_error.retryable
        elif inbound_error or outbound_error or inbound_stats.get('errors'):
            err = inbound_error or outbound_error
            job.status = 'COMPLETED_WITH_ERRORS'
            job.error_code = err.code if err else 'REQUIRES_REVIEW'
            job.error_message = err.message if err else 'Some inbound documents could not be stored.'
            job.retryable = bool(err.retryable) if err else False
        else:
            job.status = 'COMPLETED'
            job.error_code = None
            job.error_message = None
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_reconciliation(session: Session, reconciliation_id: str) -> Reconciliation:
    from uuid import UUID

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
