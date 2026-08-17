from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.gateway.canonical import decode_cursor, encode_cursor, require_oib, sha256_text, validate_amount
from app.gateway.errors import (
    document_not_found,
    invalid_request,
    invalid_ubl,
)
from app.gateway.models import Attempt, Document, Payment
from app.gateway.services.bindings import require_active_binding
from app.gateway.services.attempts import (
    KIND_E_REPORTING_REJECT,
    KIND_OUTBOUND_SEND,
    KIND_PAYMENT,
    create_attempt,
)
from app.gateway.services.outbox import record_event

DOCUMENT_TYPES = {'INVOICE', 'CREDIT_NOTE'}
WORKFLOW_STATUSES = {'RECEIVED', 'APPROVED', 'CLEARED', 'POSTED', 'UNKNOWN'}


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise invalid_request(f'{field} must be a UUID.') from exc


def serialize_document(document: Document) -> dict:
    body: dict = {
        'document_id': str(document.document_id),
        'taxpayer_oib': document.taxpayer_oib,
        'direction': document.direction,
        'document_type': document.document_type,
        'bound_provider': document.bound_provider,
        'binding_id': str(document.binding_id) if document.binding_id else None,
        'attempt_id': str(document.attempt_id),
        'provider_refs': document.provider_refs or {},
        'processing': {
            'state': document.processing_state,
            'reason': document.processing_reason,
        },
    }
    if document.direction == 'OUTBOUND':
        body.update(
            {
                'exchange_status': document.exchange_status,
                'fiscalization_status': document.fiscalization_status,
                'recipient_status': document.recipient_status,
                'payment_status': document.payment_status,
            }
        )
    else:
        body.update(
            {
                'intake_status': document.intake_status,
                'intake_fiscalization_status': document.intake_fiscalization_status,
                'internal_workflow_status': document.internal_workflow_status,
                'e_reporting_status': document.e_reporting_status,
            }
        )
    return body


def create_outbound(session: Session, taxpayer_oib: str, payload: dict) -> dict:
    require_oib(taxpayer_oib)
    if payload.get('taxpayer_oib') and payload['taxpayer_oib'] != taxpayer_oib:
        raise invalid_request('taxpayer_oib does not match the path or token subject.')
    if payload.get('direction') not in (None, 'OUTBOUND'):
        raise invalid_request('direction must be OUTBOUND.')
    document_type = payload.get('document_type')
    if document_type not in DOCUMENT_TYPES:
        raise invalid_ubl('document_type must be INVOICE or CREDIT_NOTE.')
    ubl = payload.get('ubl')
    if not ubl or not isinstance(ubl, str) or '<' not in ubl:
        raise invalid_ubl('ubl must be an XML document.')
    document_id = _parse_uuid(payload.get('document_id'), 'document_id')
    existing = session.get(Document, document_id)
    if existing is not None:
        if (
            existing.taxpayer_oib != taxpayer_oib
            or existing.direction != 'OUTBOUND'
            or (existing.ubl_sha256 != sha256_text(ubl))
        ):
            raise invalid_request('Existing document identity cannot be changed.')
        return serialize_document(existing)
    binding = require_active_binding(session, taxpayer_oib)
    document = Document(
        document_id=document_id,
        taxpayer_oib=taxpayer_oib,
        direction='OUTBOUND',
        document_type=document_type,
        binding_id=binding.id,
        bound_provider=binding.provider,
        ubl=ubl,
        ubl_sha256=sha256_text(ubl),
        attempt_id=uuid.uuid4(),
        exchange_status='QUEUED',
        fiscalization_status='PENDING',
        recipient_status='PENDING',
        payment_status='UNPAID',
        processing_state=None,
        processing_reason=None,
        provider_refs={},
    )
    session.add(document)
    session.flush()
    create_attempt(
        session,
        attempt_id=document.attempt_id,
        kind=KIND_OUTBOUND_SEND,
        document_id=document.document_id,
    )
    record_event(
        session,
        event_type='outbound.exchange_status_changed',
        taxpayer_oib=taxpayer_oib,
        document_id=document.document_id,
        payload={'exchange_status': 'QUEUED', 'processing': document.processing_reason},
    )
    return serialize_document(document)


def get_document(session: Session, document_id: str, direction: str | None = None) -> Document:
    doc = session.get(Document, _parse_uuid(document_id, 'document_id'))
    if doc is None or (direction and doc.direction != direction):
        raise document_not_found()
    return doc


def add_payment(session: Session, document_id: str, payload: dict) -> dict:
    document = get_document(session, document_id, 'OUTBOUND')
    amount = validate_amount(payload.get('amount'))
    payment_id = _parse_uuid(payload.get('payment_id'), 'payment_id')
    if payload.get('settlement') not in {'PARTIAL', 'FULL'}:
        raise invalid_request('settlement must be PARTIAL or FULL.')
    if not payload.get('currency') or not payload.get('payment_method') or not payload.get('paid_at'):
        raise invalid_request('paid_at, currency and payment_method are required.')
    try:
        paid_at = datetime.fromisoformat(str(payload['paid_at']).replace('Z', '+00:00'))
    except ValueError as exc:
        raise invalid_request('paid_at must be ISO-8601.') from exc
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=timezone.utc)
    existing = session.get(Payment, payment_id)
    if existing is not None:
        return _serialize_payment(session, existing)
    payment = Payment(
        payment_id=payment_id,
        document_id=document.document_id,
        paid_at=paid_at,
        amount=amount,
        currency=str(payload['currency']),
        payment_method=str(payload['payment_method']),
        settlement=payload['settlement'],
        fiscalization_status='PENDING',
        processing_state='READY',
        processing_reason='',
    )
    session.add(payment)
    document.payment_status = 'PARTIALLY_PAID' if payload['settlement'] == 'PARTIAL' else 'PAID'
    session.flush()
    attempt = create_attempt(
        session,
        attempt_id=uuid.uuid4(),
        kind=KIND_PAYMENT,
        document_id=document.document_id,
        payment_id=payment.payment_id,
    )
    record_event(
        session,
        event_type='outbound.payment_status_changed',
        taxpayer_oib=document.taxpayer_oib,
        document_id=document.document_id,
        payload={'payment_id': str(payment_id), 'payment_status': document.payment_status},
    )
    return _serialize_payment(session, payment, attempt.id)


def _serialize_payment(session: Session, payment: Payment, attempt_id: uuid.UUID | None = None) -> dict:
    if attempt_id is None:
        row = session.query(Attempt).filter_by(payment_id=payment.payment_id).one_or_none()
        attempt_id = row.id if row else None
    return {
        'payment_id': str(payment.payment_id),
        'document_id': str(payment.document_id),
        'attempt_id': str(attempt_id) if attempt_id else None,
        'paid_at': payment.paid_at.isoformat(),
        'amount': payment.amount,
        'currency': payment.currency,
        'payment_method': payment.payment_method,
        'settlement': payment.settlement,
        'fiscalization_status': payment.fiscalization_status,
        'processing': {
            'state': payment.processing_state,
            'reason': payment.processing_reason,
        },
    }


def list_inbound(session: Session, taxpayer_oib: str, cursor: str | None, limit: int) -> dict:
    require_oib(taxpayer_oib)
    limit = min(max(limit, 1), 100)
    query = session.query(Document).filter_by(taxpayer_oib=taxpayer_oib, direction='INBOUND')
    if cursor:
        seq = decode_cursor(cursor, taxpayer_oib)
        query = query.filter(Document.cursor_seq > seq)
    rows = query.order_by(Document.cursor_seq.asc()).limit(limit + 1).all()
    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = encode_cursor(taxpayer_oib, page[-1].cursor_seq) if page and has_more else None
    return {
        'items': [serialize_document(item) for item in page],
        'next_cursor': next_cursor,
        'has_more': has_more,
    }


def set_workflow(session: Session, document_id: str, payload: dict) -> dict:
    document = get_document(session, document_id, 'INBOUND')
    status = payload.get('internal_workflow_status')
    if status not in WORKFLOW_STATUSES:
        raise invalid_request('internal_workflow_status is not allowed.')
    document.internal_workflow_status = status
    session.flush()
    return serialize_document(document)


def reject_e_reporting(session: Session, document_id: str, payload: dict) -> dict:
    document = get_document(session, document_id, 'INBOUND')
    if not payload.get('reason_code'):
        raise invalid_request('reason_code is required.')
    document.e_reporting_status = 'PENDING'
    document.processing_state = None
    document.processing_reason = None
    refs = dict(document.provider_refs or {})
    refs['pending_rejection'] = {
        'reason_code': payload.get('reason_code'),
        'reason_text': payload.get('reason_text') or '',
    }
    document.provider_refs = refs
    session.flush()
    attempt = create_attempt(
        session,
        attempt_id=uuid.uuid4(),
        kind=KIND_E_REPORTING_REJECT,
        document_id=document.document_id,
    )
    record_event(
        session,
        event_type='inbound.e_reporting_status_changed',
        taxpayer_oib=document.taxpayer_oib,
        document_id=document.document_id,
        payload={'reason_code': payload.get('reason_code'), 'e_reporting_status': 'PENDING'},
    )
    body = serialize_document(document)
    body['attempt_id'] = str(attempt.id)
    return body


def evidence(document: Document) -> dict:
    return {
        'items': [
            {
                'kind': 'ubl',
                'sha256': document.ubl_sha256,
                'uri': f'/v1/{document.direction.lower()}/documents/{document.document_id}/ubl',
            }
        ]
    }
