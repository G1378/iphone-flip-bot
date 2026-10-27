"""visual condition grades (frame / back glass), battery N/A, part colour

Revision ID: b2e4f6a8c013
Revises: a9c1d7e3b546
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2e4f6a8c013"
down_revision: Union[str, None] = "a9c1d7e3b546"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("phones", sa.Column("battery_health_na", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("phones", sa.Column("frame_grade", sa.String(length=2), nullable=True))
    op.add_column("phones", sa.Column("back_glass_grade", sa.String(length=2), nullable=True))
    op.add_column("parts", sa.Column("colour", sa.String(length=40), nullable=True))
    op.add_column("order_lines", sa.Column("colour", sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column("order_lines", "colour")
    op.drop_column("parts", "colour")
    op.drop_column("phones", "back_glass_grade")
    op.drop_column("phones", "frame_grade")
    op.drop_column("phones", "battery_health_na")
