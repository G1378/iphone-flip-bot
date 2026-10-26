"""listing builder: item specifics + condition notes on listings, photos on phones

Revision ID: f7a1b3c9d2e5
Revises: e6c8a4d1f039
Create Date: 2026-10-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a1b3c9d2e5"
down_revision: Union[str, None] = "e6c8a4d1f039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("listings", sa.Column("condition_description", sa.Text(), nullable=True))
    op.add_column("listings", sa.Column("aspects", sa.JSON(), nullable=True))
    op.add_column("phones", sa.Column("photo_urls", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("phones", "photo_urls")
    op.drop_column("listings", "aspects")
    op.drop_column("listings", "condition_description")
