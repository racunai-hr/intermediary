"""Versioned outbound provider configs and document stamp.

Revision ID: 0003_outbound_provider
Revises: 0002_superadapter
Create Date: 2026-08-17
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0003_outbound_provider'
down_revision: Union[str, Sequence[str], None] = '0002_superadapter'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'outbound_provider_configs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('taxpayer_oib', sa.String(11), nullable=False),
        sa.Column('provider', sa.String(64), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('credential_ref', sa.String(255)),
        sa.Column('provider_account_key', sa.String(64)),
        sa.Column('created_by', sa.String(255)),
        sa.Column('change_reason', sa.String(255)),
        sa.Column('superseded_at', sa.DateTime(timezone=True)),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            'taxpayer_oib',
            'generation',
            name='uq_gateway_outbound_provider_oib_generation',
        ),
        schema='gateway',
    )
    op.create_index(
        'ix_gateway_outbound_provider_configs_taxpayer_oib',
        'outbound_provider_configs',
        ['taxpayer_oib'],
        schema='gateway',
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_gateway_outbound_provider_actual_oib "
        "ON gateway.outbound_provider_configs (taxpayer_oib) "
        "WHERE status IN ('CONFIGURED', 'DISABLED')"
    )
    op.add_column(
        'documents',
        sa.Column(
            'outbound_provider_config_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('gateway.outbound_provider_configs.id'),
        ),
        schema='gateway',
    )
    op.add_column(
        'documents',
        sa.Column('outbound_provider_generation', sa.Integer()),
        schema='gateway',
    )


def downgrade() -> None:
    op.drop_column('documents', 'outbound_provider_generation', schema='gateway')
    op.drop_column('documents', 'outbound_provider_config_id', schema='gateway')
    op.execute('DROP INDEX IF EXISTS gateway.uq_gateway_outbound_provider_actual_oib')
    op.drop_index(
        'ix_gateway_outbound_provider_configs_taxpayer_oib',
        table_name='outbound_provider_configs',
        schema='gateway',
    )
    op.drop_table('outbound_provider_configs', schema='gateway')
