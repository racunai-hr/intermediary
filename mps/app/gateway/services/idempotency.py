from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.gateway.errors import idempotency_conflict
from app.gateway.models import IdempotencyKey


def run_idempotent(
    session: Session,
    *,
    principal: str,
    key: str,
    request_hash: str,
    action: Callable[[], tuple[int, dict[str, Any]]],
) -> tuple[int, dict[str, Any]]:
    placeholder = IdempotencyKey(
        service_principal=principal,
        key=key,
        request_hash=request_hash,
        http_status=0,
        response_body={},
    )
    try:
        with session.begin_nested():
            session.add(placeholder)
            session.flush()
        owned = True
    except IntegrityError:
        owned = False

    if not owned:
        row = (
            session.query(IdempotencyKey)
            .filter_by(service_principal=principal, key=key)
            .with_for_update()
            .one()
        )
        if row.request_hash != request_hash:
            raise idempotency_conflict()
        return row.http_status, row.response_body

    status, body = action()
    placeholder.http_status = status
    placeholder.response_body = body
    session.flush()
    return status, body


def update_stored_response(
    session: Session,
    *,
    principal: str,
    key: str,
    http_status: int,
    body: dict[str, Any],
) -> None:
    row = (
        session.query(IdempotencyKey)
        .filter_by(service_principal=principal, key=key)
        .one_or_none()
    )
    if row is None:
        return
    row.http_status = http_status
    row.response_body = body
    session.flush()
