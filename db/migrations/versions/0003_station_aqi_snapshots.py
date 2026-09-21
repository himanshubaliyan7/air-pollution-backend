"""station_aqi_snapshots: hourly CPCB AQI sub-index snapshots from data.gov.in

Revision ID: 0003_station_aqi_snapshots
Revises: 0002_add_forecast
Create Date: 2026-09-21

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003_station_aqi_snapshots"
down_revision: Union[str, None] = "0002_add_forecast"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 0001 builds the schema with metadata.create_all, so a database created
    # from scratch already has this table; an existing one does not. checkfirst
    # makes this correct for both.
    from db.models import StationAqiSnapshot

    StationAqiSnapshot.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    from db.models import StationAqiSnapshot

    StationAqiSnapshot.__table__.drop(bind=op.get_bind(), checkfirst=True)
