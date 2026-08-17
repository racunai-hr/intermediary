from __future__ import annotations

from sqlalchemy.orm import Session

from app.gateway.adapters.registry import get_adapter
from app.gateway.adapters.super.adapter import SuperAdapter
from app.gateway.canonical import require_oib
from app.gateway.errors import GatewayError, document_not_found, invalid_request
from app.gateway.models import Binding, Document, Reconciliation
from app.gateway.services.outbound_provider import get_config
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
        inbound_adapter = get_adapter(binding.provider) if binding else None
        inbound_credential = None
        inbound_filters = None
        inbound_setup_error = None
        if binding is not None:
            if not isinstance(inbound_adapter, SuperAdapter):
                job.status = 'FAILED'
                job.error_code = 'CAPABILITY_NOT_SUPPORTED'
                job.error_message = 'Reconciliation requires a Super inbound binding.'
                job.retryable = False
                session.commit()
                return
            try:
                inbound_credential = inbound_adapter.resolve(binding.credential_ref)
                _, inbound_filters = checkpoint_filters(
                    session, job.taxpayer_oib, inbound_credential.company_guid, 'inbound_list'
                )
            except GatewayError as exc:
                job.status = 'FAILED'
                job.error_code = exc.code
                job.error_message = exc.message
                job.retryable = False
                session.commit()
                return
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
        outbound_groups: list[tuple] = []
        for document in outbound_docs:
            config = get_config(session, document.outbound_provider_config_id)
            if config is None or not config.credential_ref:
                continue
            outbound_groups.append((document, config))
        session.commit()
    except Exception:
        session.rollback()
        session.close()
        raise
    session.close()

    inbound_error = inbound_setup_error
    outbound_error = None
    batch: list = []
    status_rows: list[tuple[str, dict]] = []
    if inbound_credential is not None and inbound_filters is not None and inbound_adapter is not None:
        try:
            batch = fetch_inbound_batch(inbound_adapter, inbound_credential, inbound_filters)
        except GatewayError as exc:
            inbound_error = exc
    grouped: dict[str, list] = {}
    configs_by_id: dict = {}
    for document, config in outbound_groups:
        grouped.setdefault(str(config.id), []).append(document)
        configs_by_id[str(config.id)] = config
    try:
        for config_id, docs in grouped.items():
            config = configs_by_id[config_id]
            adapter = get_adapter(config.provider)
            if not isinstance(adapter, SuperAdapter):
                continue
            credential = adapter.resolve(config.credential_ref)
            if docs[0].provider_account_key and credential.company_guid != docs[0].provider_account_key:
                continue
            guids = [doc.provider_invoice_guid for doc in docs if doc.provider_invoice_guid]
            if not guids:
                continue
            for item in adapter.list_outbound_statuses(credential, guids):
                status_rows.append((docs[0].provider_account_key or credential.company_guid, item))
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
        inbound_stats = {'created': 0, 'errors': 0}
        if inbound_error is None and binding is not None and inbound_credential is not None:
            checkpoint, _ = checkpoint_filters(
                session, job.taxpayer_oib, inbound_credential.company_guid, 'inbound_list'
            )
            inbound_stats = persist_inbound_batch(
                session,
                binding=binding,
                credential=inbound_credential,
                batch=batch,
                checkpoint=checkpoint,
            )
        updated = 0
        if outbound_error is None:
            for account_key, item in status_rows:
                if apply_outbound_status_row(
                    session,
                    taxpayer_oib=job.taxpayer_oib,
                    account_key=account_key,
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
