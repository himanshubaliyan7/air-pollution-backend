"""Idempotent upsert of normalized SensorReading DTOs into raw_sensor_readings.

Re-running ingestion for a window that was already loaded (e.g. an Airflow
task retry) must not create duplicate rows or fail - ON CONFLICT DO UPDATE
on the natural key (station_id, pollutant, observed_at, source) makes a
re-run a no-op / value-refresh rather than an error.
"""

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from common.constants import SensorSourceName
from db.models import RawSensorReading
from ingestion.config import make_station_id
from ingestion.sources.base import SensorReading


def load_sensor_readings(
    session: Session,
    readings: list[SensorReading],
    source: SensorSourceName,
) -> int:
    if not readings:
        return 0

    now = datetime.now(timezone.utc)
    rows = [
        {
            "station_id": make_station_id(source, r.source_location_id),
            "pollutant": r.pollutant,
            "observed_at": r.observed_at,
            "source": source,
            "value": r.value,
            "unit": r.unit,
            "source_record_id": r.source_record_id,
            "ingested_at": now,
        }
        for r in readings
    ]

    stmt = insert(RawSensorReading).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["station_id", "pollutant", "observed_at", "source"],
        set_={
            "value": stmt.excluded.value,
            "unit": stmt.excluded.unit,
            "source_record_id": stmt.excluded.source_record_id,
            "ingested_at": stmt.excluded.ingested_at,
        },
    )
    session.execute(stmt)
    session.commit()
    return len(rows)
