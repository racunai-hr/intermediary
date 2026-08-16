from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.gateway.models import OutboxEvent


def record_event(
    session: Session,
    *,
    event_type: str,
    taxpayer_oib: str,
    document_id: uuid.UUID | None,
    payload: dict | None = None,
) -> OutboxEvent:
    next_seq = 1
    if document_id is not None:
        current = (
            session.query(func.coalesce(func.max(OutboxEvent.sequence), 0))
            .filter(OutboxEvent.document_id == document_id)
            .scalar()
        )
        next_seq = int(current) + 1
    event = OutboxEvent(
        event_type=event_type,
        sequence=next_seq,
        taxpayer_oib=taxpayer_oib,
        document_id=document_id,
        payload=payload or {},
    )
    session.add(event)
    session.flush()
    return event
