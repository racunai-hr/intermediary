from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gateway.adapters.base import UnimplementedAdapter
from app.gateway.errors import (
    binding_not_active,
    capability_not_supported,
    invalid_request,
)
from app.gateway.models import Binding

SECRET_FIELDS = {'username', 'password', 'api_key', 'secret', 'token'}


def _reject_secrets(payload: dict) -> None:
    lowered = {str(key).lower() for key in payload}
    if SECRET_FIELDS & lowered:
        raise invalid_request('Provider credentials must not be sent; use credential_ref.')


def list_bindings(session: Session, taxpayer_oib: str) -> list[Binding]:
    return (
        session.query(Binding)
        .filter_by(taxpayer_oib=taxpayer_oib)
        .order_by(Binding.created_at.asc())
        .all()
    )


def put_binding(session: Session, taxpayer_oib: str, payload: dict) -> Binding:
    _reject_secrets(payload)
    provider = payload.get('provider')
    if not provider:
        raise invalid_request('provider is required.')
    if provider == 'racunai_direct':
        raise capability_not_supported('racunai_direct is disabled for this API version.')
    if payload.get('credential_ref') is not None and not isinstance(payload.get('credential_ref'), str):
        raise invalid_request('credential_ref must be a string.')
    binding = Binding(
        taxpayer_oib=taxpayer_oib,
        provider=provider,
        status='PENDING_CONFIRMATION',
        credential_ref=payload.get('credential_ref'),
        revision=1,
    )
    session.add(binding)
    session.flush()
    return binding


def confirm_binding(session: Session, taxpayer_oib: str, payload: dict) -> Binding:
    _reject_secrets(payload)
    method = payload.get('method')
    recorded_by = payload.get('recorded_by')
    recorded_at = payload.get('recorded_at')
    if not method or not recorded_by or not recorded_at:
        raise invalid_request('method, recorded_by and recorded_at are required.')
    binding_id = payload.get('binding_id')
    query = session.query(Binding).filter_by(taxpayer_oib=taxpayer_oib)
    if binding_id:
        binding = query.filter_by(id=binding_id).one_or_none()
    else:
        binding = (
            query.filter_by(status='PENDING_CONFIRMATION')
            .order_by(Binding.created_at.desc())
            .first()
        )
    if binding is None:
        raise binding_not_active('No pending inbound binding to confirm.')
    if binding.provider == 'racunai_direct':
        raise capability_not_supported('racunai_direct is disabled for this API version.')
    if UnimplementedAdapter().capabilities()['supports']['inbound_intake'] is False:
        # Adapter cannot receive; confirmation evidence is still recorded, activation is allowed
        # only as the legal address change. SuperAdapter is not present, but Model A binding
        # may be marked ACTIVE so documents can pin binding_id.
        pass
    try:
        recorded_dt = datetime.fromisoformat(recorded_at.replace('Z', '+00:00'))
    except ValueError as exc:
        raise invalid_request('recorded_at must be ISO-8601.') from exc
    if recorded_dt.tzinfo is None:
        recorded_dt = recorded_dt.replace(tzinfo=timezone.utc)

    previous = (
        session.query(Binding)
        .filter_by(taxpayer_oib=taxpayer_oib, status='ACTIVE')
        .with_for_update()
        .all()
    )
    for item in previous:
        if item.id == binding.id:
            continue
        item.status = 'SUPERSEDED'
    session.flush()
    binding.status = 'ACTIVE'
    binding.confirmation_method = method
    binding.confirmed_by = recorded_by
    binding.confirmed_at = recorded_dt
    binding.confirmation_evidence_ref = payload.get('evidence_ref')
    try:
        session.flush()
    except IntegrityError as exc:
        raise binding_not_active('Another inbound binding is already active.') from exc
    return binding


def require_active_binding(session: Session, taxpayer_oib: str) -> Binding:
    binding = (
        session.query(Binding)
        .filter_by(taxpayer_oib=taxpayer_oib, status='ACTIVE')
        .one_or_none()
    )
    if binding is None:
        raise binding_not_active()
    if binding.provider == 'racunai_direct':
        raise capability_not_supported('racunai_direct is disabled for this API version.')
    return binding


def serialize_binding(binding: Binding) -> dict:
    return {
        'binding_id': str(binding.id),
        'taxpayer_oib': binding.taxpayer_oib,
        'provider': binding.provider,
        'status': binding.status,
        'credential_ref': binding.credential_ref,
        'revision': binding.revision,
        'confirmation': {
            'method': binding.confirmation_method,
            'recorded_by': binding.confirmed_by,
            'recorded_at': binding.confirmed_at.isoformat() if binding.confirmed_at else None,
            'evidence_ref': binding.confirmation_evidence_ref,
        },
    }
