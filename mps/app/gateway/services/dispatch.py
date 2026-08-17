from __future__ import annotations

import logging
import threading
import uuid

from sqlalchemy.orm import Session, sessionmaker

from app.gateway.adapters.registry import get_adapter
from app.gateway.adapters.super.adapter import SuperAdapter
from app.gateway.adapters.super.mapping import MAPPING_VERSION, reject_reason_code
from app.gateway.db import get_engine
from app.gateway.errors import GatewayError
from app.gateway.models import Document, Payment
from app.gateway.services import attempts as attempt_service
from app.gateway.services.outbound_provider import get_actual, get_config
from app.gateway.services.outbox import record_event

logger = logging.getLogger(__name__)

holding_db = threading.local()


def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def _set_holding(value: bool) -> None:
    holding_db.value = value


def is_holding_db() -> bool:
    return bool(getattr(holding_db, 'value', False))


def _provider_refs(document: Document) -> dict:
    refs = dict(document.provider_refs or {})
    refs['mapping_version'] = MAPPING_VERSION
    return refs


def process_attempt(attempt_id: uuid.UUID | str) -> None:
    ident = attempt_id if isinstance(attempt_id, uuid.UUID) else uuid.UUID(str(attempt_id))
    owner = str(uuid.uuid4())
    factory = _session_factory()

    _set_holding(True)
    session = factory()
    try:
        claimed = attempt_service.claim_attempt(session, ident, owner)
        if claimed is None:
            session.commit()
            return
        document = session.get(Document, claimed.document_id)
        if document is None:
            attempt_service.finish_attempt(session, claimed, attempt_service.STATUS_FAILED, 'DOCUMENT_NOT_FOUND')
            session.commit()
            return
        adapter = get_adapter(document.bound_provider)
        if not isinstance(adapter, SuperAdapter):
            document.processing_state = 'BLOCKED'
            document.processing_reason = 'CAPABILITY_NOT_SUPPORTED'
            attempt_service.finish_attempt(
                session, claimed, attempt_service.STATUS_FAILED, 'CAPABILITY_NOT_SUPPORTED'
            )
            session.commit()
            return
        stamped = get_config(session, document.outbound_provider_config_id)
        actual = get_actual(session, document.taxpayer_oib)
        if document.direction == 'OUTBOUND' and not claimed.write_intended:
            if actual is not None and actual.status == 'DISABLED':
                document.processing_state = 'BLOCKED'
                document.processing_reason = 'BLOCKED_PROVIDER_DISABLED'
                attempt_service.release_unsent(session, claimed)
                session.commit()
                return
            if stamped is None or not stamped.credential_ref:
                document.processing_state = 'BLOCKED'
                document.processing_reason = 'BLOCKED_CREDENTIAL_UNAVAILABLE'
                attempt_service.release_unsent(session, claimed)
                session.commit()
                return
        credential_ref = stamped.credential_ref if stamped else None
        if document.direction != 'OUTBOUND':
            from app.gateway.models import Binding

            binding = session.get(Binding, document.binding_id) if document.binding_id else None
            credential_ref = binding.credential_ref if binding else credential_ref
        try:
            credential = adapter.resolve(credential_ref)
        except GatewayError:
            document.processing_state = 'BLOCKED'
            document.processing_reason = (
                'BLOCKED_CREDENTIAL_UNAVAILABLE'
                if document.direction == 'OUTBOUND'
                else 'PROVIDER_NOT_CONFIGURED'
            )
            attempt_service.release_unsent(session, claimed)
            session.commit()
            return
        if (
            document.direction == 'OUTBOUND'
            and document.provider_account_key
            and credential.company_guid != document.provider_account_key
        ):
            document.processing_state = 'BLOCKED'
            document.processing_reason = 'BLOCKED_CREDENTIAL_UNAVAILABLE'
            attempt_service.release_unsent(session, claimed)
            session.commit()
            return

        kind = claimed.kind
        ubl = document.ubl
        document_type = document.document_type
        invoice_guid = document.provider_invoice_guid
        payment = session.get(Payment, claimed.payment_id) if claimed.payment_id else None
        reject_payload = (document.provider_refs or {}).get('pending_rejection') or {}
        if document.direction != 'OUTBOUND':
            document.provider_account_key = credential.company_guid
        refs = dict(document.provider_refs or {})
        refs['company_guid'] = document.provider_account_key or credential.company_guid
        document.provider_refs = refs
        if claimed.is_write:
            attempt_service.mark_write_intended(session, claimed)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        _set_holding(False)

    result = None
    apply_error = None
    try:
        if kind == attempt_service.KIND_OUTBOUND_SEND:
            result = adapter.send(credential, ubl, document_type)
        elif kind == attempt_service.KIND_PAYMENT:
            if payment is None or not invoice_guid:
                apply_error = 'REQUIRES_REVIEW'
            else:
                result = adapter.add_payment(
                    credential,
                    invoice_guid,
                    payment.paid_at.date().isoformat(),
                    payment.amount,
                    payment.payment_method,
                    payment.settlement == 'FULL',
                )
        elif kind == attempt_service.KIND_E_REPORTING_REJECT:
            if not invoice_guid:
                apply_error = 'REQUIRES_REVIEW'
            else:
                result = adapter.reject(
                    credential,
                    invoice_guid,
                    reject_reason_code(str(reject_payload.get('reason_code') or 'OTHER')),
                    str(reject_payload.get('reason_text') or ''),
                )
        else:
            apply_error = 'CAPABILITY_NOT_SUPPORTED'
    except GatewayError as exc:
        apply_error = exc.code
        result = None
    except Exception:
        logger.exception('provider call failed')
        apply_error = 'AMBIGUOUS_PROVIDER_RESULT' if kind else 'PROVIDER_UNAVAILABLE'
        result = None

    _set_holding(True)
    session = factory()
    try:
        from app.gateway.models import Attempt

        attempt = session.get(Attempt, ident)
        document = session.get(Document, claimed.document_id)
        payment = session.get(Payment, claimed.payment_id) if claimed.payment_id else None
        if attempt is None or document is None:
            session.commit()
            return
        _commit_result(session, attempt, document, payment, kind, result, apply_error)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        _set_holding(False)


def _commit_result(session, attempt, document, payment, kind, result, apply_error) -> None:
    if apply_error == 'REQUIRES_REVIEW':
        document.processing_state = 'BLOCKED'
        document.processing_reason = 'REQUIRES_REVIEW'
        if payment is not None:
            payment.processing_state = 'BLOCKED'
            payment.processing_reason = 'REQUIRES_REVIEW'
            payment.fiscalization_status = 'UNKNOWN'
        attempt_service.finish_attempt(session, attempt, attempt_service.STATUS_REQUIRES_REVIEW, 'REQUIRES_REVIEW')
        record_event(
            session,
            event_type='outbound.exchange_status_changed'
            if kind != attempt_service.KIND_E_REPORTING_REJECT
            else 'inbound.e_reporting_status_changed',
            taxpayer_oib=document.taxpayer_oib,
            document_id=document.document_id,
            payload={'processing': 'REQUIRES_REVIEW'},
        )
        return

    if result is None or not result.ok:
        code = apply_error or (result.error_code if result else 'AMBIGUOUS_PROVIDER_RESULT')
        ambiguous = bool(result.ambiguous) if result else code == 'AMBIGUOUS_PROVIDER_RESULT'
        status = attempt_service.STATUS_UNKNOWN if ambiguous else attempt_service.STATUS_FAILED
        reason = 'AMBIGUOUS_PROVIDER_RESULT' if ambiguous else code
        if kind == attempt_service.KIND_OUTBOUND_SEND:
            document.exchange_status = 'UNKNOWN' if ambiguous else 'FAILED'
            document.processing_state = 'BLOCKED'
            document.processing_reason = reason
        elif kind == attempt_service.KIND_PAYMENT and payment is not None:
            payment.fiscalization_status = 'UNKNOWN' if ambiguous else 'FAILED'
            payment.processing_state = 'BLOCKED'
            payment.processing_reason = reason
            document.processing_state = 'BLOCKED'
            document.processing_reason = reason
        elif kind == attempt_service.KIND_E_REPORTING_REJECT:
            document.e_reporting_status = 'UNKNOWN' if ambiguous else 'FAILED'
            document.processing_state = 'BLOCKED'
            document.processing_reason = reason
        attempt_service.finish_attempt(session, attempt, status, reason)
        record_event(
            session,
            event_type='outbound.exchange_status_changed'
            if kind == attempt_service.KIND_OUTBOUND_SEND
            else (
                'outbound.payment_status_changed'
                if kind == attempt_service.KIND_PAYMENT
                else 'inbound.e_reporting_status_changed'
            ),
            taxpayer_oib=document.taxpayer_oib,
            document_id=document.document_id,
            payload={'processing': reason},
        )
        return

    if kind == attempt_service.KIND_OUTBOUND_SEND:
        if not result.invoice_guid:
            document.exchange_status = 'UNKNOWN'
            document.processing_state = 'BLOCKED'
            document.processing_reason = 'REQUIRES_REVIEW'
            attempt_service.finish_attempt(
                session, attempt, attempt_service.STATUS_REQUIRES_REVIEW, 'REQUIRES_REVIEW'
            )
            record_event(
                session,
                event_type='outbound.exchange_status_changed',
                taxpayer_oib=document.taxpayer_oib,
                document_id=document.document_id,
                payload={'processing': 'REQUIRES_REVIEW'},
            )
            return
        refs = _provider_refs(document)
        refs['invoice_guid'] = result.invoice_guid
        refs['company_guid'] = (document.provider_refs or {}).get('company_guid')
        if result.unique_id:
            refs['unique_id'] = result.unique_id
        document.provider_refs = refs
        document.provider_invoice_guid = result.invoice_guid
        document.exchange_status = 'SUBMITTED'
        document.processing_state = None
        document.processing_reason = None
        attempt_service.finish_attempt(session, attempt, attempt_service.STATUS_COMPLETED, None)
        record_event(
            session,
            event_type='outbound.exchange_status_changed',
            taxpayer_oib=document.taxpayer_oib,
            document_id=document.document_id,
            payload={'exchange_status': 'SUBMITTED', 'invoice_guid': result.invoice_guid},
        )
        return

    if kind == attempt_service.KIND_PAYMENT and payment is not None:
        payment.fiscalization_status = 'PENDING'
        payment.processing_state = 'READY'
        payment.processing_reason = ''
        document.processing_state = None
        document.processing_reason = None
        attempt_service.finish_attempt(session, attempt, attempt_service.STATUS_COMPLETED, None)
        record_event(
            session,
            event_type='outbound.payment_status_changed',
            taxpayer_oib=document.taxpayer_oib,
            document_id=document.document_id,
            payload={'payment_id': str(payment.payment_id), 'fiscalization_status': 'SUBMITTED'},
        )
        return

    if kind == attempt_service.KIND_E_REPORTING_REJECT:
        document.e_reporting_status = 'SUBMITTED'
        document.processing_state = None
        document.processing_reason = None
        refs = _provider_refs(document)
        refs.pop('pending_rejection', None)
        document.provider_refs = refs
        attempt_service.finish_attempt(session, attempt, attempt_service.STATUS_COMPLETED, None)
        record_event(
            session,
            event_type='inbound.e_reporting_status_changed',
            taxpayer_oib=document.taxpayer_oib,
            document_id=document.document_id,
            payload={'e_reporting_status': 'SUBMITTED'},
        )
