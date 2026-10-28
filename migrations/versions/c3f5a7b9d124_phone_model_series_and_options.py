"""phone model series, logical order and dropdown options (storage, colourways)

Revision ID: c3f5a7b9d124
Revises: b2e4f6a8c013
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3f5a7b9d124"
down_revision: Union[str, None] = "b2e4f6a8c013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("phone_models", sa.Column("series", sa.String(length=60), nullable=True))
    op.add_column("phone_models", sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100000"))
    op.add_column("phone_models", sa.Column("storage_options", sa.JSON(), nullable=True))
    op.add_column("phone_models", sa.Column("colourways", sa.JSON(), nullable=True))
    # The series / order / options for the existing and the new models are filled in by the bot on
    # start-up (config_service.ensure_default_phone_models), which also adds the models that were missing.


def downgrade() -> None:
    op.drop_column("phone_models", "colourways")
    op.drop_column("phone_models", "storage_options")
    op.drop_column("phone_models", "sort_order")
    op.drop_column("phone_models", "series")
