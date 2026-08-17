from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.gateway.db import SCHEMA


class Base(DeclarativeBase):
    pass


class Binding(Base):
    __tablename__ = 'bindings'
    __table_args__ = (
        Index(
            'uq_gateway_active_binding_oib',
            'taxpayer_oib',
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        {'schema': SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    taxpayer_oib: Mapped[str] = mapped_column(String(11), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(255))
    confirmation_method: Mapped[str | None] = mapped_column(String(128))
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_evidence_ref: Mapped[str | None] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Document(Base):
    __tablename__ = 'documents'
    __table_args__ = (
        Index(
            'uq_gateway_inbound_provider_account_guid',
            'bound_provider',
            'provider_account_key',
            'provider_invoice_guid',
            unique=True,
            postgresql_where=text(
                "direction = 'INBOUND' AND provider_invoice_guid IS NOT NULL"
            ),
        ),
        Index(
            'ix_gateway_documents_provider_invoice',
            'bound_provider',
            'provider_account_key',
            'provider_invoice_guid',
        ),
        {'schema': SCHEMA},
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    taxpayer_oib: Mapped[str] = mapped_column(String(11), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    document_type: Mapped[str] = mapped_column(String(32), nullable=False)
    binding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f'{SCHEMA}.bindings.id')
    )
    bound_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    ubl: Mapped[str] = mapped_column(Text, nullable=False)
    ubl_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    exchange_status: Mapped[str | None] = mapped_column(String(32))
    fiscalization_status: Mapped[str | None] = mapped_column(String(32))
    recipient_status: Mapped[str | None] = mapped_column(String(32))
    payment_status: Mapped[str | None] = mapped_column(String(32))
    intake_status: Mapped[str | None] = mapped_column(String(32))
    intake_fiscalization_status: Mapped[str | None] = mapped_column(String(32))
    internal_workflow_status: Mapped[str | None] = mapped_column(String(32))
    e_reporting_status: Mapped[str | None] = mapped_column(String(32))
    processing_state: Mapped[str | None] = mapped_column(String(32))
    processing_reason: Mapped[str | None] = mapped_column(String(64))
    provider_refs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    provider_account_key: Mapped[str | None] = mapped_column(String(64))
    provider_invoice_guid: Mapped[str | None] = mapped_column(String(64))
    cursor_seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    binding: Mapped[Binding | None] = relationship()
    payments: Mapped[list[Payment]] = relationship(back_populates='document')


class Payment(Base):
    __tablename__ = 'payments'
    __table_args__ = {'schema': SCHEMA}

    payment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f'{SCHEMA}.documents.document_id'), nullable=False
    )
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    amount: Mapped[str] = mapped_column(String(32), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(64), nullable=False)
    settlement: Mapped[str] = mapped_column(String(16), nullable=False)
    fiscalization_status: Mapped[str] = mapped_column(String(32), nullable=False)
    processing_state: Mapped[str] = mapped_column(String(32), nullable=False)
    processing_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[Document] = relationship(back_populates='payments')


class IdempotencyKey(Base):
    __tablename__ = 'idempotency_keys'
    __table_args__ = (
        UniqueConstraint('service_principal', 'key', name='uq_gateway_idempotency_principal_key'),
        {'schema': SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_principal: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Reconciliation(Base):
    __tablename__ = 'reconciliations'
    __table_args__ = {'schema': SCHEMA}

    reconciliation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    taxpayer_oib: Mapped[str] = mapped_column(String(11), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retryable: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxEvent(Base):
    __tablename__ = 'outbox_events'
    __table_args__ = {'schema': SCHEMA}

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    taxpayer_oib: Mapped[str] = mapped_column(String(11), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Attempt(Base):
    __tablename__ = 'attempts'
    __table_args__ = (
        Index('ix_gateway_attempts_document_id', 'document_id'),
        {'schema': SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f'{SCHEMA}.documents.document_id'), nullable=False
    )
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    is_write: Mapped[bool] = mapped_column(nullable=False, default=True)
    write_intended: Mapped[bool] = mapped_column(nullable=False, default=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    result_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PollCheckpoint(Base):
    __tablename__ = 'poll_checkpoints'
    __table_args__ = (
        UniqueConstraint(
            'taxpayer_oib',
            'provider',
            'account_key',
            'kind',
            name='uq_gateway_poll_checkpoint',
        ),
        {'schema': SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    taxpayer_oib: Mapped[str] = mapped_column(String(11), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    account_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    watermark_date: Mapped[str | None] = mapped_column(String(10))
    last_unique_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
