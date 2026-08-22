from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gateway.adapters.super.adapter import SuperAdapter
from app.gateway.adapters.super.credentials import SuperCredential
from app.gateway.adapters.super.mapping import MAPPING_VERSION, map_inbound_status, map_outbound_status
from app.gateway.canonical import sha256_text
from app.gateway.adapters.super.client import SuperHttpError
from app.gateway.errors import GatewayError
from app.gateway.models import Binding, Document, PollCheckpoint
from app.gateway.services.outbox import record_event
from app.gateway.settings import get_gateway_settings


def _today() -> date:
    return datetime.now(timezone.utc).date()


def checkpoint_filters(session: Session, oib: str, account_key: str, kind: str) -> tuple[PollCheckpoint, dict]:
    row = (
        session.query(PollCheckpoint)
        .filter_by(taxpayer_oib=oib, provider='super', account_key=account_key, kind=kind)
        .one_or_none()
    )
    if row is None:
        row = PollCheckpoint(
            taxpayer_oib=oib,
            provider='super',
            account_key=account_key,
            kind=kind,
        )
        session.add(row)
        session.flush()
    overlap = get_gateway_settings().super_poll_overlap_days
    end = _today()
    if row.last_unique_id is None:
        start = end - timedelta(days=400)
        unique_from = 1
    elif row.watermark_date:
        start = date.fromisoformat(row.watermark_date) - timedelta(days=overlap)
        unique_from = max(1, row.last_unique_id - 50)
    else:
        start = end - timedelta(days=max(overlap, 7))
        unique_from = max(1, row.last_unique_id - 50)
    return row, {
        'date_from': start.isoformat(),
        'date_to': end.isoformat(),
        'received_from': start.isoformat(),
        'received_to': end.isoformat(),
        'unique_from': unique_from,
    }


def fetch_inbound_batch(adapter: SuperAdapter, credential: SuperCredential, filters: dict) -> list[tuple[dict, str | None]]:
    invoices = adapter.list_inbound(credential, **filters)
    batch: list[tuple[dict, str | None]] = []
    for item in invoices:
        guid = str(item.get('Guid') or item.get('guid') or '')
        if not guid:
            batch.append((item, None))
            continue
        try:
            ubl = adapter.get_inbound_ubl(credential, guid)
        except (GatewayError, SuperHttpError):
            batch.append((item, None))
            continue
        batch.append((item, ubl))
    return batch


def persist_inbound_batch(
    session: Session,
    *,
    binding: Binding,
    credential: SuperCredential,
    batch: list[tuple[dict, str | None]],
    checkpoint: PollCheckpoint,
) -> dict:
    created = 0
    errors = 0
    max_unique = checkpoint.last_unique_id
    for item, ubl in batch:
        guid = str(item.get('Guid') or item.get('guid') or '')
        if not guid or ubl is None:
            errors += 1
            continue
        existing = (
            session.query(Document)
            .filter_by(
                bound_provider='super',
                provider_account_key=credential.company_guid,
                provider_invoice_guid=guid,
                direction='INBOUND',
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.taxpayer_oib != binding.taxpayer_oib:
                continue
            _apply_inbound_status(session, existing, item, credential.company_guid)
            unique = item.get('UniqueId') or item.get('uniqueId')
            if unique is not None:
                max_unique = max(int(unique), max_unique or 0)
            continue
        mapped = map_inbound_status(item.get('InvoiceStatus') or item.get('invoiceStatus'))
        document = Document(
            document_id=uuid.uuid4(),
            taxpayer_oib=binding.taxpayer_oib,
            direction='INBOUND',
            document_type='INVOICE',
            binding_id=binding.id,
            bound_provider='super',
            ubl=ubl,
            ubl_sha256=sha256_text(ubl),
            attempt_id=uuid.uuid4(),
            intake_status=mapped.intake.value,
            intake_fiscalization_status=mapped.intake_fiscalization.value,
            internal_workflow_status='RECEIVED',
            e_reporting_status=mapped.e_reporting.value,
            processing_state='BLOCKED' if not mapped.known else None,
            processing_reason='REQUIRES_REVIEW' if not mapped.known else None,
            provider_account_key=credential.company_guid,
            provider_invoice_guid=guid,
            provider_refs={
                'company_guid': credential.company_guid,
                'invoice_guid': guid,
                'mapping_version': MAPPING_VERSION,
                'provider_status': {
                    'code': mapped.intake.provider_code,
                    'text': mapped.intake.provider_text,
                },
            },
        )
        try:
            with session.begin_nested():
                session.add(document)
                session.flush()
        except IntegrityError:
            existing = (
                session.query(Document)
                .filter_by(
                    bound_provider='super',
                    provider_account_key=credential.company_guid,
                    provider_invoice_guid=guid,
                    direction='INBOUND',
                )
                .one_or_none()
            )
            if existing is not None and existing.taxpayer_oib == binding.taxpayer_oib:
                _apply_inbound_status(session, existing, item, credential.company_guid)
            continue
        created += 1
        record_event(
            session,
            event_type='inbound.intake_available',
            taxpayer_oib=binding.taxpayer_oib,
            document_id=document.document_id,
            payload={'invoice_guid': guid},
        )
        unique = item.get('UniqueId') or item.get('uniqueId')
        if unique is not None:
            max_unique = max(int(unique), max_unique or 0)
    checkpoint.watermark_date = _today().isoformat()
    if max_unique is not None:
        checkpoint.last_unique_id = max_unique
    checkpoint.updated_at = datetime.now(timezone.utc)
    session.flush()
    return {'created': created, 'errors': errors}


def apply_outbound_status_row(
    session: Session,
    *,
    taxpayer_oib: str,
    account_key: str,
    item: dict,
) -> bool:
    guid = str(item.get('Guid') or item.get('guid') or '')
    if not guid:
        return False
    document = (
        session.query(Document)
        .filter_by(
            bound_provider='super',
            provider_account_key=account_key,
            provider_invoice_guid=guid,
            direction='OUTBOUND',
            taxpayer_oib=taxpayer_oib,
        )
        .order_by(Document.created_at.desc())
        .first()
    )
    if document is None:
        return False
    mapped = map_outbound_status(item.get('SendingInvoiceStatus') or item.get('sendingInvoiceStatus'))
    document.exchange_status = mapped.exchange.value
    document.fiscalization_status = mapped.fiscalization.value
    document.recipient_status = mapped.recipient.value
    document.payment_status = mapped.payment.value
    refs = dict(document.provider_refs or {})
    refs['mapping_version'] = MAPPING_VERSION
    refs['provider_status'] = {
        'code': mapped.exchange.provider_code,
        'text': mapped.exchange.provider_text,
    }
    document.provider_refs = refs
    if not mapped.known:
        document.processing_state = 'BLOCKED'
        document.processing_reason = 'REQUIRES_REVIEW'
    session.flush()
    record_event(
        session,
        event_type='outbound.exchange_status_changed',
        taxpayer_oib=document.taxpayer_oib,
        document_id=document.document_id,
        payload={'exchange_status': document.exchange_status, 'provider_code': mapped.exchange.provider_code},
    )
    return True


def _apply_inbound_status(session: Session, document: Document, item: dict, account_key: str) -> None:
    if document.provider_account_key != account_key:
        return
    mapped = map_inbound_status(item.get('InvoiceStatus') or item.get('invoiceStatus'))
    document.intake_status = mapped.intake.value
    refs = dict(document.provider_refs or {})
    refs['mapping_version'] = MAPPING_VERSION
    refs['provider_status'] = {
        'code': mapped.intake.provider_code,
        'text': mapped.intake.provider_text,
    }
    document.provider_refs = refs
    if not mapped.known:
        document.processing_state = 'BLOCKED'
        document.processing_reason = 'REQUIRES_REVIEW'
    session.flush()
