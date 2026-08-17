"""SuperAdapter attempts, inbound dedup and poll checkpoints.

Revision ID: 0002_superadapter
Revises: 0001_gateway
Create Date: 2026-08-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0002_superadapter'
down_revision: Union[str, Sequence[str], None] = '0001_gateway'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('provider_account_key', sa.String(64)), schema='gateway')
    op.add_column('documents', sa.Column('provider_invoice_guid', sa.String(64)), schema='gateway')
    op.create_index(
        'ix_gateway_documents_provider_invoice',
        'documents',
        ['bound_provider', 'provider_account_key', 'provider_invoice_guid'],
        schema='gateway',
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_gateway_inbound_provider_account_guid "
        "ON gateway.documents (bound_provider, provider_account_key, provider_invoice_guid) "
        "WHERE direction = 'INBOUND' AND provider_invoice_guid IS NOT NULL"
    )
    op.create_table(
        'attempts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            'kind',
            sa.String(32),
            nullable=False,
        ),
        sa.Column(
            'document_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('gateway.documents.document_id'),
            nullable=False,
        ),
        sa.Column('payment_id', postgresql.UUID(as_uuid=True)),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('is_write', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('write_intended', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('lease_owner', sa.String(64)),
        sa.Column('result_code', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        schema='gateway',
    )
    op.create_index('ix_gateway_attempts_document_id', 'attempts', ['document_id'], schema='gateway')
    op.create_table(
        'poll_checkpoints',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('taxpayer_oib', sa.String(11), nullable=False),
        sa.Column('provider', sa.String(64), nullable=False),
        sa.Column('account_key', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('watermark_date', sa.String(10)),
        sa.Column('last_unique_id', sa.Integer()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            'taxpayer_oib',
            'provider',
            'account_key',
            'kind',
            name='uq_gateway_poll_checkpoint',
        ),
        schema='gateway',
    )


def downgrade() -> None:
    op.drop_table('poll_checkpoints', schema='gateway')
    op.drop_table('attempts', schema='gateway')
    op.execute('DROP INDEX IF EXISTS gateway.uq_gateway_inbound_provider_account_guid')
    op.drop_index('ix_gateway_documents_provider_invoice', table_name='documents', schema='gateway')
    op.drop_column('documents', 'provider_invoice_guid', schema='gateway')
    op.drop_column('documents', 'provider_account_key', schema='gateway')
