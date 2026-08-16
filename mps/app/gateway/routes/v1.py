from __future__ import annotations

from typing import Any
from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.gateway.adapters.base import UnimplementedAdapter
from app.gateway.auth import Principal, require_scope, require_taxpayer
from app.gateway.canonical import request_hash, require_oib
from app.gateway.db import get_session
from app.gateway.errors import capability_not_supported, invalid_request
from app.gateway.services import documents as document_service
from app.gateway.services.bindings import (
    confirm_binding,
    list_bindings,
    put_binding,
    serialize_binding,
)
from app.gateway.services.documents import serialize_document
from app.gateway.services.idempotency import run_idempotent
from app.gateway.services.reconcile import (
    get_reconciliation,
    serialize_reconciliation,
    start_reconciliation,
)

router = APIRouter(prefix='/v1')


def _idempotency_key(idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')) -> str:
    if not idempotency_key:
        raise invalid_request('Idempotency-Key is required.')
    return idempotency_key


def _json(response: Response, status: int, body: dict) -> dict:
    response.status_code = status
    return body


@router.get('/providers/{provider}/capabilities')
def provider_capabilities(
    provider: str,
    principal: Principal = Depends(require_scope('gateway.read')),
):
    if provider == 'racunai_direct':
        raise capability_not_supported('racunai_direct is disabled for this API version.')
    caps = UnimplementedAdapter().capabilities()
    return {'provider': provider, **caps}


@router.get('/taxpayers/{oib}/inbound-binding')
def get_inbound_binding(
    oib: str,
    principal: Principal = Depends(require_scope('gateway.read')),
    session: Session = Depends(get_session),
):
    require_oib(oib)
    require_taxpayer(principal, oib)
    items = [serialize_binding(item) for item in list_bindings(session, oib)]
    return {'items': items}


@router.put('/taxpayers/{oib}/inbound-binding')
def upsert_inbound_binding(
    oib: str,
    payload: dict[str, Any],
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope('gateway.admin')),
    session: Session = Depends(get_session),
    key: str = Depends(_idempotency_key),
):
    require_oib(oib)
    require_taxpayer(principal, oib)

    def action():
        binding = put_binding(session, oib, payload)
        return 200, serialize_binding(binding)

    status, body = run_idempotent(
        session,
        principal=principal.subject,
        key=key,
        request_hash=request_hash(
            method='PUT', path=request.url.path, taxpayer_oib=oib, body=payload
        ),
        action=action,
    )
    return _json(response, status, body)


@router.post('/taxpayers/{oib}/inbound-binding/fiskaplikacija-confirmation')
def confirm_inbound_binding(
    oib: str,
    payload: dict[str, Any],
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope('gateway.admin')),
    session: Session = Depends(get_session),
    key: str = Depends(_idempotency_key),
):
    require_oib(oib)
    require_taxpayer(principal, oib)

    def action():
        binding = confirm_binding(session, oib, payload)
        return 200, serialize_binding(binding)

    status, body = run_idempotent(
        session,
        principal=principal.subject,
        key=key,
        request_hash=request_hash(
            method='POST', path=request.url.path, taxpayer_oib=oib, body=payload
        ),
        action=action,
    )
    return _json(response, status, body)


@router.post('/participants/lookup')
def lookup_participant(
    payload: dict[str, Any],
    principal: Principal = Depends(require_scope('gateway.write')),
):
    if not payload.get('scheme') or not payload.get('identifier') or not payload.get('document_type'):
        raise invalid_request('scheme, identifier and document_type are required.')
    raise capability_not_supported('Participant lookup requires a provider adapter.')


@router.post('/outbound/documents')
def create_outbound_document(
    payload: dict[str, Any],
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope('gateway.write')),
    session: Session = Depends(get_session),
    key: str = Depends(_idempotency_key),
):
    oib = require_oib(str(payload.get('taxpayer_oib') or ''))
    require_taxpayer(principal, oib)

    def action():
        body = document_service.create_outbound(session, oib, payload)
        return 202, body

    status, body = run_idempotent(
        session,
        principal=principal.subject,
        key=key,
        request_hash=request_hash(
            method='POST',
            path=request.url.path,
            taxpayer_oib=oib,
            body={k: v for k, v in payload.items() if k != 'ubl'},
            ubl=str(payload.get('ubl') or ''),
        ),
        action=action,
    )
    return _json(response, status, body)


@router.get('/outbound/documents/{document_id}')
def get_outbound_document(
    document_id: str,
    principal: Principal = Depends(require_scope('gateway.read')),
    session: Session = Depends(get_session),
):
    document = document_service.get_document(session, document_id, 'OUTBOUND')
    require_taxpayer(principal, document.taxpayer_oib)
    return serialize_document(document)


@router.post('/outbound/documents/{document_id}/payments')
def create_payment(
    document_id: str,
    payload: dict[str, Any],
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope('gateway.write')),
    session: Session = Depends(get_session),
    key: str = Depends(_idempotency_key),
):
    document = document_service.get_document(session, document_id, 'OUTBOUND')
    require_taxpayer(principal, document.taxpayer_oib)

    def action():
        return 202, document_service.add_payment(session, document_id, payload)

    status, body = run_idempotent(
        session,
        principal=principal.subject,
        key=key,
        request_hash=request_hash(
            method='POST', path=request.url.path, taxpayer_oib=document.taxpayer_oib, body=payload
        ),
        action=action,
    )
    return _json(response, status, body)


@router.get('/taxpayers/{oib}/inbound/documents')
def list_inbound_documents(
    oib: str,
    cursor: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(require_scope('gateway.read')),
    session: Session = Depends(get_session),
):
    require_oib(oib)
    require_taxpayer(principal, oib)
    return document_service.list_inbound(session, oib, cursor, limit)


@router.get('/inbound/documents/{document_id}')
def get_inbound_document(
    document_id: str,
    principal: Principal = Depends(require_scope('gateway.read')),
    session: Session = Depends(get_session),
):
    document = document_service.get_document(session, document_id, 'INBOUND')
    require_taxpayer(principal, document.taxpayer_oib)
    return serialize_document(document)


@router.get('/inbound/documents/{document_id}/ubl')
def get_inbound_ubl(
    document_id: str,
    principal: Principal = Depends(require_scope('gateway.read')),
    session: Session = Depends(get_session),
):
    document = document_service.get_document(session, document_id, 'INBOUND')
    require_taxpayer(principal, document.taxpayer_oib)
    return Response(content=document.ubl, media_type='application/xml')


@router.get('/inbound/documents/{document_id}/evidence')
def get_inbound_evidence(
    document_id: str,
    principal: Principal = Depends(require_scope('gateway.read')),
    session: Session = Depends(get_session),
):
    document = document_service.get_document(session, document_id, 'INBOUND')
    require_taxpayer(principal, document.taxpayer_oib)
    return document_service.evidence(document)


@router.post('/inbound/documents/{document_id}/workflow-status')
def set_workflow_status(
    document_id: str,
    payload: dict[str, Any],
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope('gateway.write')),
    session: Session = Depends(get_session),
    key: str = Depends(_idempotency_key),
):
    document = document_service.get_document(session, document_id, 'INBOUND')
    require_taxpayer(principal, document.taxpayer_oib)

    def action():
        return 200, document_service.set_workflow(session, document_id, payload)

    status, body = run_idempotent(
        session,
        principal=principal.subject,
        key=key,
        request_hash=request_hash(
            method='POST', path=request.url.path, taxpayer_oib=document.taxpayer_oib, body=payload
        ),
        action=action,
    )
    return _json(response, status, body)


@router.post('/inbound/documents/{document_id}/e-reporting/rejection')
def reject_inbound(
    document_id: str,
    payload: dict[str, Any],
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope('gateway.write')),
    session: Session = Depends(get_session),
    key: str = Depends(_idempotency_key),
):
    document = document_service.get_document(session, document_id, 'INBOUND')
    require_taxpayer(principal, document.taxpayer_oib)

    def action():
        return document_service.reject_e_reporting(session, document_id, payload)

    status, body = run_idempotent(
        session,
        principal=principal.subject,
        key=key,
        request_hash=request_hash(
            method='POST', path=request.url.path, taxpayer_oib=document.taxpayer_oib, body=payload
        ),
        action=action,
    )
    return _json(response, status, body)


@router.post('/taxpayers/{oib}/reconciliations')
def create_reconciliation(
    oib: str,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_scope('gateway.write')),
    session: Session = Depends(get_session),
    key: str = Depends(_idempotency_key),
):
    require_oib(oib)
    require_taxpayer(principal, oib)

    def action():
        return 202, start_reconciliation(session, oib)

    status, body = run_idempotent(
        session,
        principal=principal.subject,
        key=key,
        request_hash=request_hash(
            method='POST', path=request.url.path, taxpayer_oib=oib, body={}
        ),
        action=action,
    )
    return _json(response, status, body)


@router.get('/reconciliations/{reconciliation_id}')
def read_reconciliation(
    reconciliation_id: str,
    principal: Principal = Depends(require_scope('gateway.read')),
    session: Session = Depends(get_session),
):
    job = get_reconciliation(session, reconciliation_id)
    require_taxpayer(principal, job.taxpayer_oib)
    return serialize_reconciliation(job)
