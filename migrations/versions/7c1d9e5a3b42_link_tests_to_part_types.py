"""link tests to part types, faults to tests

Revision ID: 7c1d9e5a3b42
Revises: 4ada7231ba4e
Create Date: 2026-10-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c1d9e5a3b42'
down_revision: Union[str, None] = '4ada7231ba4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Mirrors app.services.defaults.DEFAULT_TEST_PART_TYPES at the time of writing
# (copied, not imported, so this migration never changes if the defaults do).
_BACKFILL = {
    "touchscreen": "Screen",
    "display": "Screen",
    "true_tone": "Screen",
    "face_id_touch_id": "Face ID Module",
    "front_camera": "Front Camera",
    "rear_cameras": "Rear Camera",
    "microphone": "Charging Port / Flex",
    "speakers": "Speaker",
    "charging": "Charging Port / Flex",
    "wireless_charging": "Back Glass",
    "wifi": "Logic Board",
    "bluetooth": "Logic Board",
    "mobile_signal": "Logic Board",
    "buttons": "Housing",
    "vibration": "Vibration Motor",
    "proximity_sensor": "Earpiece",
    "ambient_light_sensor": "Earpiece",
    "battery_health": "Battery",
    "sim_esim": "SIM Tray",
}


def upgrade() -> None:
    op.add_column('test_definitions', sa.Column('part_type_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_test_definitions_part_type_id', 'test_definitions', 'part_types', ['part_type_id'], ['id'])
    op.add_column('phone_faults', sa.Column('test_definition_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_phone_faults_test_definition_id', 'phone_faults', 'test_definitions', ['test_definition_id'], ['id'])

    # Existing deployments already have the default tests seeded; point them
    # at the matching part types. Tests/part types that were renamed or
    # deleted simply don't match and stay unmapped.
    conn = op.get_bind()
    for code, part_name in _BACKFILL.items():
        conn.execute(
            sa.text(
                "UPDATE test_definitions SET part_type_id = "
                "(SELECT id FROM part_types WHERE name = :part_name) "
                "WHERE code = :code AND part_type_id IS NULL"
            ),
            {"code": code, "part_name": part_name},
        )


def downgrade() -> None:
    op.drop_constraint('fk_phone_faults_test_definition_id', 'phone_faults', type_='foreignkey')
    op.drop_column('phone_faults', 'test_definition_id')
    op.drop_constraint('fk_test_definitions_part_type_id', 'test_definitions', type_='foreignkey')
    op.drop_column('test_definitions', 'part_type_id')
