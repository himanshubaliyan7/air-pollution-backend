"""model_runs, forecasts: which target a model forecasts

Daily-mean models (owner decision 2026-10-02: graded daily verdicts) live next
to the hourly ones, so both tables record the target. Every existing row is an
hourly one, which the default supplies.

Revision ID: 0007_forecast_target
Revises: 0006_cpcb_reading_source
Create Date: 2026-10-02

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007_forecast_target"
down_revision: Union[str, None] = "0006_cpcb_reading_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("model_runs", "forecasts")


def upgrade() -> None:
    # IF NOT EXISTS: 0001 builds the schema from the CURRENT models, so a
    # database created from scratch already has the column. A constant default
    # is a catalogue-only change in Postgres 11+: no table rewrite.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS target VARCHAR NOT NULL DEFAULT 'hourly'")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS target")
