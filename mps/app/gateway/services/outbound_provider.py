from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.gateway.adapters.super.adapter import SuperAdapter
from app.gateway.canonical import require_oib
from app.gateway.errors import capability_not_supported, invalid_request, provider_not_configured
from app.gateway.models import OutboundProviderConfig
from app.gateway.services.bindings import list_bindings

PROVIDERS = {'super'}
ACTUAL_STATUSES = {'CONFIGURED', 'DISABLED'}
PUT_STATUSES = {'CONFIGURED', 'DISABLED'}


def lock_oib(session: Session, taxpayer_oib: str) -> None:
    session.execute(text('SELECT pg_advisory_xact_lock(hashtext(:oib))'), {'oib': taxpayer_oib})


def get_actual(session: Session, taxpayer_oib: str) -> OutboundProviderConfig | None:
    return (
        session.query(OutboundProviderConfig)
        .filter(
            OutboundProviderConfig.taxpayer_oib == taxpayer_oib,
            OutboundProviderConfig.status.in_(tuple(ACTUAL_STATUSES)),
        )
        .one_or_none()
    )


def get_config(session: Session, config_id) -> OutboundProviderConfig | None:
    if config_id is None:
        return None
    return session.get(OutboundProviderConfig, config_id)


def outbound_readiness(config: OutboundProviderConfig | None) -> dict:
    if config is None or config.status != 'CONFIGURED' or not config.credential_ref:
        return {
            'configured': bool(config and config.status == 'CONFIGURED' and config.credential_ref),
            'credential_available': False,
            'provider_account_resolved': False,
            'ready': False,
        }
    try:
        credential = SuperAdapter().resolve(config.credential_ref)
    except Exception:
        return {
            'configured': True,
            'credential_available': False,
            'provider_account_resolved': False,
            'ready': False,
        }
    resolved = bool(credential.company_guid)
    available = True
    return {
        'configured': True,
        'credential_available': available,
        'provider_account_resolved': resolved,
        'ready': available and resolved,
    }


def inbound_readiness(session: Session, taxpayer_oib: str) -> dict:
    binding = next((item for item in list_bindings(session, taxpayer_oib) if item.status == 'ACTIVE'), None)
    return {'active_binding': bool(binding)}


def serialize_config(config: OutboundProviderConfig | None, *, include_missing: bool = False) -> dict:
    if config is None:
        if include_missing:
            return {
                'taxpayer_oib': None,
                'status': None,
                'outbound_readiness': outbound_readiness(None),
            }
        raise provider_not_configured('Outbound provider is not configured.')
    return {
        'id': str(config.id),
        'taxpayer_oib': config.taxpayer_oib,
        'provider': config.provider,
        'generation': config.generation,
        'status': config.status,
        'provider_account_key': config.provider_account_key,
        'change_reason': config.change_reason,
        'outbound_readiness': outbound_readiness(config),
    }


def put_outbound_provider(
    session: Session,
    taxpayer_oib: str,
    payload: dict,
    *,
    created_by: str | None,
) -> OutboundProviderConfig:
    oib = require_oib(taxpayer_oib)
    lock_oib(session, oib)
    status = str(payload.get('status') or 'CONFIGURED')
    if status not in PUT_STATUSES:
        raise invalid_request('status must be CONFIGURED or DISABLED.')
    change_reason = payload.get('change_reason')
    if not change_reason or not str(change_reason).strip():
        raise invalid_request('change_reason is required.')
    current = get_actual(session, oib)
    provider = payload.get('provider') or (current.provider if current else None)
    if provider == 'racunai_direct':
        raise capability_not_supported('racunai_direct is disabled for this API version.')
    if provider not in PROVIDERS:
        raise invalid_request('provider must be super.')
    credential_ref = payload.get('credential_ref')
    if credential_ref is None and current is not None:
        credential_ref = current.credential_ref
    if status == 'CONFIGURED' and not (credential_ref and str(credential_ref).strip()):
        raise invalid_request('CONFIGURED requires credential_ref.')
    account_key = None
    if status == 'CONFIGURED':
        try:
            credential = SuperAdapter().resolve(str(credential_ref))
            account_key = credential.company_guid
        except Exception:
            account_key = None
    elif current is not None:
        account_key = current.provider_account_key

    next_generation = 1
    max_row = (
        session.query(func.max(OutboundProviderConfig.generation))
        .filter_by(taxpayer_oib=oib)
        .scalar()
    )
    if max_row:
        next_generation = int(max_row) + 1
    now = datetime.now(timezone.utc)
    if current is not None:
        current.status = 'SUPERSEDED'
        current.superseded_at = now
        session.flush()

    row = OutboundProviderConfig(
        taxpayer_oib=oib,
        provider=str(provider),
        generation=next_generation,
        status=status,
        credential_ref=str(credential_ref).strip() if credential_ref else None,
        provider_account_key=account_key,
        created_by=created_by,
        change_reason=str(change_reason).strip(),
    )
    session.add(row)
    session.flush()
    return row


def require_ready_outbound(session: Session, taxpayer_oib: str) -> OutboundProviderConfig:
    oib = require_oib(taxpayer_oib)
    lock_oib(session, oib)
    config = get_actual(session, oib)
    readiness = outbound_readiness(config)
    if config is None or config.status != 'CONFIGURED' or not readiness['ready']:
        raise provider_not_configured('Outbound provider is not configured or not ready.')
    return config
