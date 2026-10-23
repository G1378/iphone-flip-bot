"""add job channels (one Discord channel per phone while it is in the workshop)

Revision ID: c4a9d2e8b317
Revises: b8d4e1f7a265
Create Date: 2026-10-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4a9d2e8b317'
down_revision: Union[str, None] = 'b8d4e1f7a265'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'job_channels',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('phone_id', sa.Integer(), sa.ForeignKey('phones.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=False),
        sa.Column('panel_message_id', sa.BigInteger()),
        sa.Column('panel_hash', sa.String(40)),
        sa.Column('created_by_discord_id', sa.BigInteger()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_job_channels_channel_id', 'job_channels', ['channel_id'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_job_channels_channel_id', table_name='job_channels')
    op.drop_table('job_channels')
