"""Initial gateway schema.

Revision ID: 0001_gateway
Revises:
Create Date: 2026-08-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001_gateway'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE SCHEMA IF NOT EXISTS gateway')
    op.create_table(
        'bindings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('taxpayer_oib', sa.String(11), nullable=False),
        sa.Column('provider', sa.String(64), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('credential_ref', sa.String(255)),
        sa.Column('confirmation_method', sa.String(128)),
        sa.Column('confirmed_by', sa.String(255)),
        sa.Column('confirmed_at', sa.DateTime(timezone=True)),
        sa.Column('confirmation_evidence_ref', sa.String(255)),
        sa.Column('revision', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema='gateway',
    )
    op.create_index('ix_gateway_bindings_taxpayer_oib', 'bindings', ['taxpayer_oib'], schema='gateway')
    op.execute(
        "CREATE UNIQUE INDEX uq_gateway_active_binding_oib "
        "ON gateway.bindings (taxpayer_oib) WHERE status = 'ACTIVE'"
    )
    op.create_table(
        'documents',
        sa.Column('document_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('taxpayer_oib', sa.String(11), nullable=False),
        sa.Column('direction', sa.String(16), nullable=False),
        sa.Column('document_type', sa.String(32), nullable=False),
        sa.Column('binding_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gateway.bindings.id')),
        sa.Column('bound_provider', sa.String(64), nullable=False),
        sa.Column('ubl', sa.Text(), nullable=False),
        sa.Column('ubl_sha256', sa.String(64), nullable=False),
        sa.Column('attempt_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('exchange_status', sa.String(32)),
        sa.Column('fiscalization_status', sa.String(32)),
        sa.Column('recipient_status', sa.String(32)),
        sa.Column('payment_status', sa.String(32)),
        sa.Column('intake_status', sa.String(32)),
        sa.Column('intake_fiscalization_status', sa.String(32)),
        sa.Column('internal_workflow_status', sa.String(32)),
        sa.Column('e_reporting_status', sa.String(32)),
        sa.Column('processing_state', sa.String(32)),
        sa.Column('processing_reason', sa.String(64)),
        sa.Column('provider_refs', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('cursor_seq', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema='gateway',
    )
    op.create_index('ix_gateway_documents_taxpayer_oib', 'documents', ['taxpayer_oib'], schema='gateway')
    op.create_index('ix_gateway_documents_cursor_seq', 'documents', ['cursor_seq'], schema='gateway')
    op.create_table(
        'payments',
        sa.Column('payment_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('document_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('gateway.documents.document_id'), nullable=False),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('amount', sa.String(32), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('payment_method', sa.String(64), nullable=False),
        sa.Column('settlement', sa.String(16), nullable=False),
        sa.Column('fiscalization_status', sa.String(32), nullable=False),
        sa.Column('processing_state', sa.String(32), nullable=False),
        sa.Column('processing_reason', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema='gateway',
    )
    op.create_table(
        'idempotency_keys',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('service_principal', sa.String(255), nullable=False),
        sa.Column('key', sa.String(255), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('http_status', sa.Integer(), nullable=False),
        sa.Column('response_body', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('service_principal', 'key', name='uq_gateway_idempotency_principal_key'),
        schema='gateway',
    )
    op.create_table(
        'reconciliations',
        sa.Column('reconciliation_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('taxpayer_oib', sa.String(11), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('error_code', sa.String(64)),
        sa.Column('error_message', sa.Text()),
        sa.Column('retryable', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema='gateway',
    )
    op.create_index('ix_gateway_reconciliations_oib', 'reconciliations', ['taxpayer_oib'], schema='gateway')
    op.create_table(
        'outbox_events',
        sa.Column('event_id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('event_type', sa.String(128), nullable=False),
        sa.Column('event_version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('taxpayer_oib', sa.String(11), nullable=False),
        sa.Column('document_id', postgresql.UUID(as_uuid=True)),
        sa.Column('payload', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema='gateway',
    )


def downgrade() -> None:
    op.drop_table('outbox_events', schema='gateway')
    op.drop_table('reconciliations', schema='gateway')
    op.drop_table('idempotency_keys', schema='gateway')
    op.drop_table('payments', schema='gateway')
    op.drop_table('documents', schema='gateway')
    op.drop_table('bindings', schema='gateway')
