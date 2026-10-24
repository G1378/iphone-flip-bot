"""parts are confirmed after fitting, not tested before: record failures

Adds part_failures (what happened to a part that didn't work, its supplier,
the business loss and any refund) and the "worked in" marker on parts.

Revision ID: d5b7f3a1c928
Revises: c4a9d2e8b317
Create Date: 2026-10-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5b7f3a1c928'
down_revision: Union[str, None] = 'c4a9d2e8b317'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('parts', sa.Column('confirmed_working_at', sa.DateTime(timezone=True)))
    op.add_column('parts', sa.Column('confirmed_working_in', sa.String(20)))

    op.create_table(
        'part_failures',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('part_id', sa.Integer(), sa.ForeignKey('parts.id'), nullable=False),
        sa.Column('part_type_id', sa.Integer(), sa.ForeignKey('part_types.id'), nullable=False),
        sa.Column('phone_id', sa.Integer(), sa.ForeignKey('phones.id', ondelete='SET NULL')),
        sa.Column('repair_part_id', sa.Integer(), sa.ForeignKey('repair_parts.id', ondelete='SET NULL')),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('orders.id', ondelete='SET NULL')),
        sa.Column('supplier', sa.String(120)),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('reason', sa.Text()),
        sa.Column('was_installed', sa.Boolean(), nullable=False),
        sa.Column('loss_amount', sa.Numeric(10, 2), nullable=False),
        sa.Column('expense_id', sa.Integer(), sa.ForeignKey('expenses.id', ondelete='SET NULL')),
        sa.Column('return_status', sa.String(20)),
        sa.Column('refund_amount', sa.Numeric(10, 2)),
        sa.Column('refunded_at', sa.Date()),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('tasks.id', ondelete='SET NULL')),
        sa.Column('created_by_discord_id', sa.BigInteger()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_part_failures_part_id', 'part_failures', ['part_id'])
    op.create_index('ix_part_failures_phone_id', 'part_failures', ['phone_id'])
    op.create_index('ix_part_failures_supplier', 'part_failures', ['supplier'])


def downgrade() -> None:
    op.drop_table('part_failures')
    op.drop_column('parts', 'confirmed_working_in')
    op.drop_column('parts', 'confirmed_working_at')
