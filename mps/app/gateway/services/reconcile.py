from __future__ import annotations

from sqlalchemy.orm import Session

from app.gateway.adapters.pondi.client import PondiHttpError
from app.gateway.adapters.registry import adapter_is_live, get_adapter
from app.gateway.adapters.super.client import SuperHttpError
from app.gateway.canonical import require_oib
from app.gateway.errors import (
    GatewayError,
    document_not_found,
    from_provider_http_error,
    invalid_request,
)
from app.gateway.models import Binding, Document, Reconciliation
from app.gateway.services.outbound_provider import get_actual, get_config
from app.gateway.services.inbound_pull import (
    apply_outbound_status_row,
    checkpoint_filters,
    fetch_inbound_batch,
    fetch_outbound_batch,
    persist_historical_outbound_batch,
    persist_inbound_batch,
)


def _provider_error(exc: Exception) -> GatewayError:
    if isinstance(exc, GatewayError):
        return exc
    if isinstance(exc, (SuperHttpError, PondiHttpError)):
        return from_provider_http_error(exc)
    raise TypeError(f'Unexpected provider error type: {type(exc)!r}')


def _account_key(credential) -> str:
    return getattr(credential, 'account_key', None) or getattr(credential, 'company_guid', '') or ''


def _mark_reconciliation_failed(session_factory, reconciliation_id, exc: BaseException) -> None:
    from uuid import UUID

    ident = reconciliation_id if not isinstance(reconciliation_id, str) else UUID(reconciliation_id)
    message = str(exc) or exc.__class__.__name__
    session = session_factory()
    try:
        job = session.get(Reconciliation, ident)
        if job is None:
            session.commit()
            return
        if job.status == 'RUNNING':
            job.status = 'FAILED'
            job.error_code = 'INTERNAL_ERROR'
            job.error_message = message
            job.retryable = False
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
        outbound_config = get_actual(session, job.taxpayer_oib)
        outbound_adapter = None
        outbound_credential = None
        outbound_filters = None
        historical_outbound = False
        if binding is not None:
            if inbound_adapter is None or not adapter_is_live(inbound_adapter):
                job.status = 'FAILED'
                job.error_code = 'CAPABILITY_NOT_SUPPORTED'
                job.error_message = 'Reconciliation requires a live inbound binding.'
                job.retryable = False
                session.commit()
                return
            try:
                inbound_credential = inbound_adapter.resolve(binding.credential_ref)
                _, inbound_filters = checkpoint_filters(
                    session,
                    job.taxpayer_oib,
                    _account_key(inbound_credential),
                    'inbound_list',
                    inbound_adapter.name,
                )
            except GatewayError as exc:
                job.status = 'FAILED'
                job.error_code = exc.code
                job.error_message = exc.message
                job.retryable = False
                session.commit()
                return
        if (
            outbound_config is not None
            and outbound_config.status == 'CONFIGURED'
            and outbound_config.credential_ref
        ):
            outbound_adapter = get_adapter(outbound_config.provider)
            if outbound_adapter is not None and adapter_is_live(outbound_adapter):
                try:
                    outbound_credential = outbound_adapter.resolve(outbound_config.credential_ref)
                    if hasattr(outbound_adapter, 'fetch_outbound_with_ubl'):
                        historical_outbound = True
                        _, outbound_filters = checkpoint_filters(
                            session,
                            job.taxpayer_oib,
                            _account_key(outbound_credential),
                            'outbound_list',
                            outbound_adapter.name,
                        )
                except GatewayError:
                    outbound_adapter = None
                    outbound_credential = None
                    outbound_filters = None
                    historical_outbound = False
        outbound_docs = (
            session.query(Document)
            .filter_by(
                taxpayer_oib=job.taxpayer_oib,
                direction='OUTBOUND',
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
        raise
    finally:
        session.close()

    inbound_error = inbound_setup_error
    outbound_error = None
    batch: list = []
    outbound_batch: list = []
    status_rows: list[tuple[str, dict, object]] = []
    try:
        if inbound_credential is not None and inbound_filters is not None and inbound_adapter is not None:
            try:
                batch = fetch_inbound_batch(inbound_adapter, inbound_credential, inbound_filters)
            except (GatewayError, SuperHttpError, PondiHttpError) as exc:
                inbound_error = _provider_error(exc)
        if historical_outbound and outbound_credential is not None and outbound_filters is not None:
            try:
                outbound_batch = fetch_outbound_batch(outbound_adapter, outbound_credential, outbound_filters)
            except (GatewayError, SuperHttpError, PondiHttpError) as exc:
                outbound_error = _provider_error(exc)
        grouped: dict[str, list] = {}
        configs_by_id: dict = {}
        for document, config in outbound_groups:
            grouped.setdefault(str(config.id), []).append(document)
            configs_by_id[str(config.id)] = config
        try:
            if not historical_outbound:
                for config_id, docs in grouped.items():
                    config = configs_by_id[config_id]
                    adapter = get_adapter(config.provider)
                    if not adapter_is_live(adapter):
                        continue
                    try:
                        credential = adapter.resolve(config.credential_ref)
                    except GatewayError:
                        continue
                    account_key = _account_key(credential)
                    if docs[0].provider_account_key and account_key != docs[0].provider_account_key:
                        continue
                    guids = [doc.provider_invoice_guid for doc in docs if doc.provider_invoice_guid]
                    if not guids:
                        continue
                    for item in adapter.list_outbound_statuses(credential, guids):
                        status_rows.append((docs[0].provider_account_key or account_key, item, adapter))
        except (GatewayError, SuperHttpError, PondiHttpError) as exc:
            outbound_error = _provider_error(exc)
    except Exception as exc:
        _mark_reconciliation_failed(session_factory, ident, exc)
        raise

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
                session,
                job.taxpayer_oib,
                _account_key(inbound_credential),
                'inbound_list',
                inbound_adapter.name if inbound_adapter is not None else binding.provider,
            )
            inbound_stats = persist_inbound_batch(
                session,
                binding=binding,
                credential=inbound_credential,
                batch=batch,
                checkpoint=checkpoint,
                adapter=inbound_adapter,
            )
        outbound_stats = {'created': 0, 'errors': 0}
        if (
            outbound_error is None
            and historical_outbound
            and outbound_credential is not None
            and outbound_adapter is not None
        ):
            config = get_actual(session, job.taxpayer_oib)
            checkpoint, _ = checkpoint_filters(
                session,
                job.taxpayer_oib,
                _account_key(outbound_credential),
                'outbound_list',
                outbound_adapter.name,
            )
            outbound_stats = persist_historical_outbound_batch(
                session,
                taxpayer_oib=job.taxpayer_oib,
                credential=outbound_credential,
                batch=outbound_batch,
                checkpoint=checkpoint,
                adapter=outbound_adapter,
                outbound_config=config,
            )
        updated = 0
        if outbound_error is None:
            for account_key, item, adapter in status_rows:
                if apply_outbound_status_row(
                    session,
                    taxpayer_oib=job.taxpayer_oib,
                    account_key=account_key,
                    item=item,
                    adapter=adapter,
                ):
                    updated += 1
        if inbound_error and outbound_error:
            job.status = 'FAILED'
            job.error_code = inbound_error.code
            job.error_message = inbound_error.message
            job.retryable = inbound_error.retryable
        elif inbound_error or outbound_error or inbound_stats.get('errors') or outbound_stats.get('errors'):
            err = inbound_error or outbound_error
            job.status = 'COMPLETED_WITH_ERRORS'
            job.error_code = err.code if err else 'REQUIRES_REVIEW'
            job.error_message = err.message if err else 'Some documents could not be stored.'
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
