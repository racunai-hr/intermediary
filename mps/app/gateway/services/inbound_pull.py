from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gateway.adapters.pondi.client import PondiHttpError
from app.gateway.adapters.super.adapter import SuperAdapter
from app.gateway.adapters.super.client import SuperHttpError
from app.gateway.canonical import sha256_text
from app.gateway.errors import GatewayError
from app.gateway.models import Binding, Document, PollCheckpoint
from app.gateway.services.outbox import record_event
from app.gateway.settings import get_gateway_settings


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _item_unique(item: dict) -> int | None:
    unique = item.get('UniqueId') or item.get('uniqueId') or item.get('id')
    if unique is None:
        return None
    try:
        return int(unique)
    except (TypeError, ValueError):
        return None


def _account_key(credential) -> str:
    return getattr(credential, 'account_key', None) or getattr(credential, 'company_guid', '') or ''


def checkpoint_filters(
    session: Session,
    oib: str,
    account_key: str,
    kind: str,
    provider: str,
) -> tuple[PollCheckpoint, dict]:
    row = (
        session.query(PollCheckpoint)
        .filter_by(taxpayer_oib=oib, provider=provider, account_key=account_key, kind=kind)
        .one_or_none()
    )
    if row is None:
        row = PollCheckpoint(
            taxpayer_oib=oib,
            provider=provider,
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


def known_inbound_guids(
    session: Session,
    *,
    taxpayer_oib: str,
    provider: str,
    account_key: str,
) -> set[str]:
    rows = (
        session.query(Document.provider_invoice_guid)
        .filter_by(
            taxpayer_oib=taxpayer_oib,
            direction='INBOUND',
            bound_provider=provider,
            provider_account_key=account_key,
        )
        .filter(Document.provider_invoice_guid.isnot(None))
        .all()
    )
    return {row[0] for row in rows if row[0]}


def fetch_inbound_batch(
    adapter,
    credential,
    filters: dict,
    *,
    known_guids: set[str] | None = None,
) -> list[tuple[dict, str | None]]:
    fetcher = getattr(adapter, 'fetch_inbound_with_ubl', None)
    if callable(fetcher):
        return fetcher(credential, **filters)
    known = known_guids or set()
    invoices = adapter.list_inbound(credential, **filters)
    batch: list[tuple[dict, str | None]] = []
    for item in invoices:
        guid = adapter.list_item_id(item)
        if not guid:
            batch.append((item, None))
            continue
        if guid in known:
            batch.append((item, None))
            continue
        try:
            ubl = adapter.get_inbound_ubl(credential, guid)
        except (GatewayError, SuperHttpError, PondiHttpError):
            batch.append((item, None))
            continue
        batch.append((item, ubl))
    return batch


def fetch_outbound_batch(adapter, credential, filters: dict) -> list[tuple[dict, str | None]]:
    fetcher = getattr(adapter, 'fetch_outbound_with_ubl', None)
    if callable(fetcher):
        return fetcher(credential, **filters)
    return []


def _document_type(item: dict) -> str:
    raw = str(item.get('documentType') or item.get('document_type') or 'Invoice')
    compact = raw.replace('_', '').replace('-', '').lower()
    if compact == 'creditnote':
        return 'CREDIT_NOTE'
    return 'INVOICE'


def persist_inbound_batch(
    session: Session,
    *,
    binding: Binding,
    credential,
    batch: list[tuple[dict, str | None]],
    checkpoint: PollCheckpoint,
    adapter,
) -> dict:
    created = 0
    errors = 0
    max_unique = checkpoint.last_unique_id
    account_key = _account_key(credential)
    provider = adapter.name
    mapping_version = getattr(adapter, 'mapping_version', '')
    for item, ubl in batch:
        guid = adapter.list_item_id(item)
        if not guid:
            errors += 1
            continue
        existing = (
            session.query(Document)
            .filter_by(
                bound_provider=provider,
                provider_account_key=account_key,
                provider_invoice_guid=guid,
                direction='INBOUND',
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.taxpayer_oib != binding.taxpayer_oib:
                continue
            _apply_inbound_status(session, existing, item, account_key, adapter)
            unique = _item_unique(item)
            if unique is not None:
                max_unique = max(unique, max_unique or 0)
            continue
        if ubl is None:
            errors += 1
            continue
        mapped = adapter.map_inbound_item(item)
        document = Document(
            document_id=uuid.uuid4(),
            taxpayer_oib=binding.taxpayer_oib,
            direction='INBOUND',
            document_type='INVOICE',
            binding_id=binding.id,
            bound_provider=provider,
            ubl=ubl,
            ubl_sha256=sha256_text(ubl),
            attempt_id=uuid.uuid4(),
            intake_status=mapped.intake.value,
            intake_fiscalization_status=mapped.intake_fiscalization.value,
            internal_workflow_status='RECEIVED',
            e_reporting_status=mapped.e_reporting.value,
            processing_state='BLOCKED' if not mapped.known else None,
            processing_reason='REQUIRES_REVIEW' if not mapped.known else None,
            provider_account_key=account_key,
            provider_invoice_guid=guid,
            provider_refs={
                'company_guid': account_key,
                'invoice_guid': guid,
                'mapping_version': mapping_version,
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
                    bound_provider=provider,
                    provider_account_key=account_key,
                    provider_invoice_guid=guid,
                    direction='INBOUND',
                )
                .one_or_none()
            )
            if existing is not None and existing.taxpayer_oib == binding.taxpayer_oib:
                _apply_inbound_status(session, existing, item, account_key, adapter)
            continue
        created += 1
        record_event(
            session,
            event_type='inbound.intake_available',
            taxpayer_oib=binding.taxpayer_oib,
            document_id=document.document_id,
            payload={'invoice_guid': guid},
        )
        unique = _item_unique(item)
        if unique is not None:
            max_unique = max(unique, max_unique or 0)
    checkpoint.watermark_date = _today().isoformat()
    if max_unique is not None:
        checkpoint.last_unique_id = max_unique
    checkpoint.updated_at = datetime.now(timezone.utc)
    session.flush()
    return {'created': created, 'errors': errors}


def persist_historical_outbound_batch(
    session: Session,
    *,
    taxpayer_oib: str,
    credential,
    batch: list[tuple[dict, str | None]],
    checkpoint: PollCheckpoint,
    adapter,
    outbound_config=None,
) -> dict:
    """Store already-sent provider invoices. Never queues a send attempt."""
    created = 0
    errors = 0
    max_unique = checkpoint.last_unique_id
    account_key = _account_key(credential)
    provider = adapter.name
    mapping_version = getattr(adapter, 'mapping_version', '')
    for item, ubl in batch:
        guid = adapter.list_item_id(item)
        if not guid or ubl is None:
            errors += 1
            continue
        existing = (
            session.query(Document)
            .filter_by(
                bound_provider=provider,
                provider_account_key=account_key,
                provider_invoice_guid=guid,
                direction='OUTBOUND',
            )
            .order_by(Document.created_at.desc())
            .first()
        )
        if existing is not None:
            if existing.taxpayer_oib != taxpayer_oib:
                continue
            apply_outbound_status_row(
                session,
                taxpayer_oib=taxpayer_oib,
                account_key=account_key,
                item=item,
                adapter=adapter,
            )
            unique = _item_unique(item)
            if unique is not None:
                max_unique = max(unique, max_unique or 0)
            continue
        mapped = adapter.map_outbound_item(item)
        document = Document(
            document_id=uuid.uuid4(),
            taxpayer_oib=taxpayer_oib,
            direction='OUTBOUND',
            document_type=_document_type(item),
            binding_id=None,
            outbound_provider_config_id=getattr(outbound_config, 'id', None),
            outbound_provider_generation=getattr(outbound_config, 'generation', None),
            bound_provider=provider,
            ubl=ubl,
            ubl_sha256=sha256_text(ubl),
            attempt_id=uuid.uuid4(),
            exchange_status=mapped.exchange.value,
            fiscalization_status=mapped.fiscalization.value,
            recipient_status=mapped.recipient.value,
            payment_status=mapped.payment.value,
            processing_state='BLOCKED' if not mapped.known else None,
            processing_reason='REQUIRES_REVIEW' if not mapped.known else None,
            provider_account_key=account_key,
            provider_invoice_guid=guid,
            provider_refs={
                'company_guid': account_key,
                'invoice_guid': guid,
                'mapping_version': mapping_version,
                'historical_intake': True,
                'provider_status': {
                    'code': mapped.exchange.provider_code,
                    'text': mapped.exchange.provider_text,
                },
            },
        )
        session.add(document)
        session.flush()
        created += 1
        unique = _item_unique(item)
        if unique is not None:
            max_unique = max(unique, max_unique or 0)
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
    adapter=None,
) -> bool:
    mapper = adapter or SuperAdapter()
    guid = mapper.list_item_id(item)
    if not guid:
        return False
    document = (
        session.query(Document)
        .filter_by(
            bound_provider=mapper.name,
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
    mapped = mapper.map_outbound_item(item)
    document.exchange_status = mapped.exchange.value
    document.fiscalization_status = mapped.fiscalization.value
    document.recipient_status = mapped.recipient.value
    document.payment_status = mapped.payment.value
    refs = dict(document.provider_refs or {})
    refs['mapping_version'] = getattr(mapper, 'mapping_version', '')
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


def _apply_inbound_status(session: Session, document: Document, item: dict, account_key: str, adapter) -> None:
    if document.provider_account_key != account_key:
        return
    mapped = adapter.map_inbound_item(item)
    document.intake_status = mapped.intake.value
    refs = dict(document.provider_refs or {})
    refs['mapping_version'] = getattr(adapter, 'mapping_version', '')
    refs['provider_status'] = {
        'code': mapped.intake.provider_code,
        'text': mapped.intake.provider_text,
    }
    document.provider_refs = refs
    if not mapped.known:
        document.processing_state = 'BLOCKED'
        document.processing_reason = 'REQUIRES_REVIEW'
    session.flush()
