"""buy page: deal_alerts gets a source, a status, the economics and a link to the bought phone

Revision ID: d4a6b8c0e235
Revises: c3f5a7b9d124
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4a6b8c0e235"
down_revision: Union[str, None] = "c3f5a7b9d124"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "deal_alerts"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("source", sa.String(length=10), nullable=False, server_default="EBAY"))
    op.add_column(TABLE, sa.Column("status", sa.String(length=12), nullable=False, server_default="PENDING"))
    op.add_column(TABLE, sa.Column("buyer_fee", sa.Numeric(10, 2), nullable=False, server_default="0"))
    op.add_column(TABLE, sa.Column("max_item_price", sa.Numeric(10, 2), nullable=True))
    op.add_column(TABLE, sa.Column("image_url", sa.String(length=500), nullable=True))
    op.add_column(TABLE, sa.Column("seller_reviews", sa.Integer(), nullable=True))
    op.add_column(TABLE, sa.Column("resale_estimate", sa.Numeric(10, 2), nullable=True))
    op.add_column(TABLE, sa.Column("resale_basis", sa.String(length=80), nullable=True))
    op.add_column(TABLE, sa.Column("sale_costs", sa.Numeric(10, 2), nullable=True))
    op.add_column(TABLE, sa.Column("repair_estimate", sa.Numeric(10, 2), nullable=False, server_default="0"))
    op.add_column(TABLE, sa.Column("repair_basis", sa.String(length=400), nullable=True))
    op.add_column(TABLE, sa.Column("expected_profit", sa.Numeric(10, 2), nullable=True))
    op.add_column(TABLE, sa.Column("roi_pct", sa.Numeric(7, 1), nullable=True))
    op.add_column(TABLE, sa.Column("decision_note", sa.Text(), nullable=True))
    op.add_column(TABLE, sa.Column("decided_by_discord_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("phone_id", sa.Integer(), sa.ForeignKey("phones.id"), nullable=True))
    op.add_column(TABLE, sa.Column("message_channel_id", sa.BigInteger(), nullable=True))
    op.add_column(TABLE, sa.Column("message_id", sa.BigInteger(), nullable=True))
    # flags can now carry several notes (repair basis, fee assumptions...).
    op.alter_column(TABLE, "flags", existing_type=sa.String(length=300), type_=sa.String(length=600))
    # Alerts announced before the buy page existed were plain messages with no buttons: they must
    # not appear as pending cards that can never be actioned.
    op.execute("UPDATE deal_alerts SET status = 'ARCHIVED'")
    op.create_index("ix_deal_alerts_status", TABLE, ["status"])


def downgrade() -> None:
    op.drop_index("ix_deal_alerts_status", table_name=TABLE)
    op.alter_column(TABLE, "flags", existing_type=sa.String(length=600), type_=sa.String(length=300))
    for col in ("message_id", "message_channel_id", "phone_id", "decided_at", "decided_by_discord_id", "decision_note",
                "roi_pct", "expected_profit", "repair_basis", "repair_estimate", "sale_costs", "resale_basis",
                "resale_estimate", "seller_reviews", "image_url", "max_item_price", "buyer_fee", "status", "source"):
        op.drop_column(TABLE, col)
