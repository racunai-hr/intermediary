from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.gateway.models import Attempt
from app.gateway.settings import get_gateway_settings

KIND_OUTBOUND_SEND = 'outbound_send'
KIND_PAYMENT = 'payment'
KIND_E_REPORTING_REJECT = 'e_reporting_reject'

STATUS_PENDING = 'PENDING'
STATUS_CLAIMED = 'CLAIMED'
STATUS_COMPLETED = 'COMPLETED'
STATUS_UNKNOWN = 'UNKNOWN'
STATUS_REQUIRES_REVIEW = 'REQUIRES_REVIEW'
STATUS_FAILED = 'FAILED'

TERMINAL = {STATUS_COMPLETED, STATUS_UNKNOWN, STATUS_REQUIRES_REVIEW, STATUS_FAILED}


def create_attempt(
    session: Session,
    *,
    attempt_id: uuid.UUID,
    kind: str,
    document_id: uuid.UUID,
    payment_id: uuid.UUID | None = None,
    is_write: bool = True,
) -> Attempt:
    attempt = Attempt(
        id=attempt_id,
        kind=kind,
        document_id=document_id,
        payment_id=payment_id,
        status=STATUS_PENDING,
        is_write=is_write,
        write_intended=False,
    )
    session.add(attempt)
    session.flush()
    return attempt


def claim_attempt(session: Session, attempt_id: uuid.UUID, owner: str) -> Attempt | None:
    now = datetime.now(timezone.utc)
    attempt = (
        session.query(Attempt)
        .filter(Attempt.id == attempt_id)
        .with_for_update(skip_locked=True)
        .one_or_none()
    )
    if attempt is None:
        return None
    if attempt.status in TERMINAL:
        return None
    if attempt.status == STATUS_CLAIMED and attempt.lease_until and attempt.lease_until > now:
        return None
    if attempt.write_intended:
        attempt.status = STATUS_UNKNOWN
        attempt.result_code = 'AMBIGUOUS_PROVIDER_RESULT'
        attempt.updated_at = now
        session.flush()
        return None
    lease = get_gateway_settings().super_lease_seconds
    attempt.status = STATUS_CLAIMED
    attempt.lease_owner = owner
    attempt.lease_until = now + timedelta(seconds=lease)
    attempt.updated_at = now
    session.flush()
    return attempt


def mark_write_intended(session: Session, attempt: Attempt) -> None:
    attempt.write_intended = True
    attempt.updated_at = datetime.now(timezone.utc)
    session.flush()


def finish_attempt(session: Session, attempt: Attempt, status: str, result_code: str | None) -> None:
    attempt.status = status
    attempt.result_code = result_code
    attempt.lease_until = None
    attempt.updated_at = datetime.now(timezone.utc)
    session.flush()


def release_unsent(session: Session, attempt: Attempt) -> None:
    attempt.status = STATUS_PENDING
    attempt.lease_until = None
    attempt.lease_owner = None
    attempt.write_intended = False
    attempt.updated_at = datetime.now(timezone.utc)
    session.flush()
