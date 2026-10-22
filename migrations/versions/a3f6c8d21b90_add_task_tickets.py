"""add task tickets (templates, tasks, trigger state)

Revision ID: a3f6c8d21b90
Revises: 7c1d9e5a3b42
Create Date: 2026-10-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f6c8d21b90'
down_revision: Union[str, None] = '7c1d9e5a3b42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    ts = lambda: [  # noqa: E731
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]

    op.create_table(
        'task_templates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(80), nullable=False, unique=True),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('category', sa.String(40), nullable=False),
        sa.Column('scope', sa.String(10), nullable=False),
        sa.Column('trigger', sa.String(120)),
        sa.Column('auto_complete_trigger', sa.String(120)),
        sa.Column('priority', sa.String(10), nullable=False),
        sa.Column('due_after_days', sa.Integer()),
        sa.Column('recur_days', sa.Integer()),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        *ts(),
    )
    op.create_index('ix_task_templates_scope', 'task_templates', ['scope'])

    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('internal_id', sa.String(20), nullable=False),
        sa.Column('title', sa.String(200), nullable=False),
        sa.Column('description', sa.Text()),
        sa.Column('category', sa.String(40), nullable=False),
        sa.Column('scope', sa.String(10), nullable=False),
        sa.Column('priority', sa.String(10), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('template_id', sa.Integer(), sa.ForeignKey('task_templates.id', ondelete='SET NULL')),
        sa.Column('phone_id', sa.Integer(), sa.ForeignKey('phones.id', ondelete='SET NULL')),
        sa.Column('assignee_discord_id', sa.BigInteger()),
        sa.Column('due_date', sa.Date()),
        sa.Column('blocked_reason', sa.Text()),
        sa.Column('notes', sa.Text()),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('released', sa.Boolean(), nullable=False),
        sa.Column('created_by_discord_id', sa.BigInteger()),
        sa.Column('completed_by_discord_id', sa.BigInteger()),
        sa.Column('completed_at', sa.DateTime(timezone=True)),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('posted_version', sa.Integer(), nullable=False),
        sa.Column('discord_channel_id', sa.BigInteger()),
        sa.Column('discord_message_id', sa.BigInteger()),
        sa.Column('overdue_notified', sa.Boolean(), nullable=False),
        *ts(),
    )
    op.create_index('ix_tasks_internal_id', 'tasks', ['internal_id'], unique=True)
    op.create_index('ix_tasks_status', 'tasks', ['status'])
    op.create_index('ix_tasks_phone_id', 'tasks', ['phone_id'])
    op.create_index('ix_tasks_released', 'tasks', ['released'])
    op.create_index('ix_tasks_discord_message_id', 'tasks', ['discord_message_id'], unique=True)

    op.create_table(
        'task_trigger_state',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entity_type', sa.String(10), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('last_status', sa.String(30)),
        sa.UniqueConstraint('entity_type', 'entity_id', name='uq_task_trigger_entity'),
    )


def downgrade() -> None:
    op.drop_table('task_trigger_state')
    op.drop_table('tasks')
    op.drop_table('task_templates')
