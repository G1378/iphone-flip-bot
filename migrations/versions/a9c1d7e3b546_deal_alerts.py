"""deal alerts: remember which eBay listings the deal scanner has already announced

Revision ID: a9c1d7e3b546
Revises: f7a1b3c9d2e5
Create Date: 2026-10-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1d7e3b546"
down_revision: Union[str, None] = "f7a1b3c9d2e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deal_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ebay_item_id", sa.String(60), nullable=False),
        sa.Column("phone_model_id", sa.Integer(), sa.ForeignKey("phone_models.id"), nullable=False),
        sa.Column("storage", sa.String(20)),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("bucket", sa.String(20), nullable=False),
        sa.Column("price", sa.Numeric(10, 2), nullable=False),
        sa.Column("postage", sa.Numeric(10, 2), nullable=False),
        sa.Column("max_buy", sa.Numeric(10, 2), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("seller", sa.String(100)),
        sa.Column("seller_feedback_pct", sa.Numeric(5, 1)),
        sa.Column("is_auction", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("flags", sa.String(300)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_deal_alerts_ebay_item_id", "deal_alerts", ["ebay_item_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_deal_alerts_ebay_item_id", table_name="deal_alerts")
    op.drop_table("deal_alerts")
