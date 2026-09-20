"""Idempotent upsert of ERA5/ERA5T WeatherReading DTOs into raw_weather_readings.

ERA5T rows are never deleted or overwritten in place when the final ERA5
value arrives - a new row is inserted with product_type=era5, and the
matching older era5t row (same grid_cell_id/observed_at) is flagged
is_superseded=True. This keeps historical feature/training runs
reproducible: a run made while only ERA5T was available can still be traced
back to exactly the values it used.
"""

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from common.constants import WeatherProductType
from db.models import RawWeatherReading
from ingestion.weather.era5_client import WeatherReading


def load_weather_readings(session: Session, readings: list[WeatherReading]) -> int:
    if not readings:
        return 0

    now = datetime.now(timezone.utc)
    rows = [
        {
            "grid_cell_id": r.grid_cell_id,
            "observed_at": r.observed_at,
            "product_type": r.product_type,
            "u_wind": r.u_wind,
            "v_wind": r.v_wind,
            "wind_speed": r.wind_speed,
            "wind_direction": r.wind_direction,
            "relative_humidity": r.relative_humidity,
            "is_superseded": False,
            "ingested_at": now,
        }
        for r in readings
    ]

    stmt = insert(RawWeatherReading).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["grid_cell_id", "observed_at", "product_type"],
        set_={
            "u_wind": stmt.excluded.u_wind,
            "v_wind": stmt.excluded.v_wind,
            "wind_speed": stmt.excluded.wind_speed,
            "wind_direction": stmt.excluded.wind_direction,
            "relative_humidity": stmt.excluded.relative_humidity,
            "ingested_at": stmt.excluded.ingested_at,
        },
    )
    session.execute(stmt)

    final_rows = [r for r in readings if r.product_type == WeatherProductType.ERA5]
    for r in final_rows:
        session.execute(
            update(RawWeatherReading)
            .where(
                RawWeatherReading.grid_cell_id == r.grid_cell_id,
                RawWeatherReading.observed_at == r.observed_at,
                RawWeatherReading.product_type == WeatherProductType.ERA5T,
            )
            .values(is_superseded=True)
        )

    session.commit()
    return len(rows)
