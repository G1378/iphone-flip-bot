"""exact phone models: link phones/donors to the catalog, infer part fitment

Revision ID: e6c8a4d1f039
Revises: d5b7f3a1c928
Create Date: 2026-10-24

Parts are model specific (base / mini / Plus / Pro / Pro Max all differ), so a
phone or donor now points at its exact catalog model, and every part must be
tagged with the models it fits. Existing rows are backfilled where the data
allows it; parts that still have no model afterwards simply match nothing until
someone tags them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e6c8a4d1f039"
down_revision: Union[str, None] = "d5b7f3a1c928"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ("phones", "donors"):
        op.add_column(table, sa.Column("phone_model_id", sa.Integer(), nullable=True))
        op.create_foreign_key(f"fk_{table}_phone_model", table, "phone_models", ["phone_model_id"], ["id"])
        op.create_index(f"ix_{table}_phone_model_id", table, ["phone_model_id"])
        op.execute(f"""
            UPDATE {table} SET phone_model_id = pm.id
            FROM phone_models pm
            WHERE pm.manufacturer = {table}.manufacturer
              AND pm.model = {table}.model
              AND pm.variant IS NOT DISTINCT FROM {table}.variant
        """)

    # Parts torn down from a donor fit the donor's model.
    op.execute("""
        INSERT INTO part_compatible_models (part_id, phone_model_id)
        SELECT p.id, d.phone_model_id FROM parts p
        JOIN donors d ON d.id = p.source_donor_id
        WHERE d.phone_model_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM part_compatible_models c WHERE c.part_id = p.id)
    """)
    # Parts received from an order line fit the model that line was ordered for.
    op.execute("""
        INSERT INTO part_compatible_models (part_id, phone_model_id)
        SELECT p.id, ol.phone_model_id FROM parts p
        JOIN order_lines ol ON ol.id = p.source_order_line_id
        WHERE ol.phone_model_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM part_compatible_models c WHERE c.part_id = p.id)
    """)


def downgrade() -> None:
    for table in ("donors", "phones"):
        op.drop_index(f"ix_{table}_phone_model_id", table_name=table)
        op.drop_constraint(f"fk_{table}_phone_model", table, type_="foreignkey")
        op.drop_column(table, "phone_model_id")
