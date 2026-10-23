"""one automatic ticket per phone

Replaces the per-phone template checklist with derived, single-ticket-per-phone
tickets: drops phone templates/tasks and the trigger-state table, adds
stage/content_hash/system_blocked, a phone_ticket_overrides table, and a partial
unique index that guarantees at most one open ticket per phone.

Revision ID: b8d4e1f7a265
Revises: a3f6c8d21b90
Create Date: 2026-10-22 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8d4e1f7a265'
down_revision: Union[str, None] = 'a3f6c8d21b90'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Phone checklists from the first version are replaced by derived tickets.
    op.execute("DELETE FROM tasks WHERE scope = 'PHONE'")
    op.execute("DELETE FROM task_templates WHERE scope = 'PHONE'")
    op.drop_table('task_trigger_state')

    op.drop_column('task_templates', 'trigger')
    op.drop_column('task_templates', 'auto_complete_trigger')
    op.drop_column('task_templates', 'due_after_days')
    op.drop_index('ix_tasks_released', table_name='tasks')
    op.drop_column('tasks', 'released')

    op.add_column('tasks', sa.Column('stage', sa.String(20)))
    op.add_column('tasks', sa.Column('content_hash', sa.String(40)))
    op.add_column('tasks', sa.Column('system_blocked', sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_index(
        'uq_task_one_open_ticket_per_phone', 'tasks', ['phone_id'], unique=True,
        postgresql_where=sa.text("phone_id IS NOT NULL AND status IN ('TODO','IN_PROGRESS','BLOCKED')"),
    )

    op.create_table(
        'phone_ticket_overrides',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('phone_id', sa.Integer(), sa.ForeignKey('phones.id', ondelete='CASCADE'), nullable=False),
        sa.Column('stage', sa.String(20), nullable=False),
        sa.Column('created_by_discord_id', sa.BigInteger()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('phone_id', 'stage', name='uq_phone_ticket_override'),
    )
    op.create_index('ix_phone_ticket_overrides_phone_id', 'phone_ticket_overrides', ['phone_id'])


def downgrade() -> None:
    op.drop_table('phone_ticket_overrides')
    op.drop_index('uq_task_one_open_ticket_per_phone', table_name='tasks')
    op.drop_column('tasks', 'system_blocked')
    op.drop_column('tasks', 'content_hash')
    op.drop_column('tasks', 'stage')
    op.add_column('tasks', sa.Column('released', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.create_index('ix_tasks_released', 'tasks', ['released'])
    op.add_column('task_templates', sa.Column('due_after_days', sa.Integer()))
    op.add_column('task_templates', sa.Column('auto_complete_trigger', sa.String(120)))
    op.add_column('task_templates', sa.Column('trigger', sa.String(120)))
    op.create_table(
        'task_trigger_state',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entity_type', sa.String(10), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('last_status', sa.String(30)),
        sa.UniqueConstraint('entity_type', 'entity_id', name='uq_task_trigger_entity'),
    )
