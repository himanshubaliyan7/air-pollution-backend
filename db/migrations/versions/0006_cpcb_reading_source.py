"""raw_sensor_readings: CPCB as a second reading source

Adds CPCB to the sensor-source enums, so hourly concentrations recovered from
CPCB's CAAQMS feed can be stored next to OpenAQ's (source is part of the
primary key, so both can hold the same hour). SQLAlchemy stores enum member
NAMES, hence 'CPCB'.

Revision ID: 0006_cpcb_reading_source
Revises: 0005_snapshot_hourly_sub_index
Create Date: 2026-10-01

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0006_cpcb_reading_source"
down_revision: Union[str, None] = "0005_snapshot_hourly_sub_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ENUM_TYPES = ("sensor_source_name_reading", "sensor_source_name")


def upgrade() -> None:
    # IF NOT EXISTS: 0001 builds the schema from the CURRENT models, so a
    # database created from scratch already has the value.
    for enum_type in _ENUM_TYPES:
        op.execute(f"ALTER TYPE {enum_type} ADD VALUE IF NOT EXISTS 'CPCB'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type; delete the CPCB rows instead
    # (DELETE FROM raw_sensor_readings WHERE source = 'CPCB') if rolling back.
    pass
