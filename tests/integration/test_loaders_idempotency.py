from datetime import datetime, timezone

from sqlalchemy import select

from common.constants import Pollutant, SensorSourceName, WeatherProductType
from db.models import RawSensorReading, RawWeatherReading, Station
from ingestion.loaders.sensor_loader import load_sensor_readings
from ingestion.loaders.weather_loader import load_weather_readings
from ingestion.config import make_station_id
from ingestion.sources.base import SensorReading
from ingestion.weather.era5_client import WeatherReading


def _make_station(db_session, station_id=None):
    station_id = station_id or make_station_id(SensorSourceName.OPENAQ, "111")
    db_session.add(
        Station(
            station_id=station_id,
            name="Test Station",
            lat=28.6,
            lon=77.2,
            city="Delhi",
            state="Delhi",
            source=SensorSourceName.OPENAQ,
            source_location_id="111",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    return station_id


def test_sensor_reading_upsert_is_idempotent_and_updates_value(db_session):
    _make_station(db_session)
    observed_at = datetime(2026, 9, 19, 6, tzinfo=timezone.utc)
    reading = SensorReading(
        source_location_id="111",
        pollutant=Pollutant.PM25,
        value=120.0,
        unit="ug/m3",
        observed_at=observed_at,
        source_record_id="9001:2026-09-19T06:00:00Z",
    )

    n1 = load_sensor_readings(db_session, [reading], SensorSourceName.OPENAQ)
    n2 = load_sensor_readings(db_session, [reading], SensorSourceName.OPENAQ)

    assert n1 == 1
    assert n2 == 1  # re-run reports the same row count, doesn't error

    rows = db_session.execute(select(RawSensorReading)).scalars().all()
    assert len(rows) == 1  # no duplicate inserted
    assert rows[0].value == 120.0

    # Re-running with a revised value should update in place, not duplicate.
    revised = SensorReading(
        source_location_id="111",
        pollutant=Pollutant.PM25,
        value=131.5,
        unit="ug/m3",
        observed_at=observed_at,
        source_record_id="9001:2026-09-19T06:00:00Z",
    )
    load_sensor_readings(db_session, [revised], SensorSourceName.OPENAQ)
    db_session.expire_all()  # loader upserts via Core, bypassing the ORM identity map
    rows = db_session.execute(select(RawSensorReading)).scalars().all()
    assert len(rows) == 1
    assert rows[0].value == 131.5


def test_weather_reading_era5t_is_superseded_by_final_era5(db_session):
    observed_at = datetime(2026, 9, 14, 6, tzinfo=timezone.utc)
    era5t = WeatherReading(
        grid_cell_id="28.60_77.20",
        lat=28.6,
        lon=77.2,
        observed_at=observed_at,
        u_wind=1.0,
        v_wind=2.0,
        wind_speed=2.24,
        wind_direction=180.0,
        relative_humidity=55.0,
        product_type=WeatherProductType.ERA5T,
    )
    load_weather_readings(db_session, [era5t])

    rows = db_session.execute(select(RawWeatherReading)).scalars().all()
    assert len(rows) == 1
    assert rows[0].product_type == WeatherProductType.ERA5T
    assert rows[0].is_superseded is False

    era5_final = WeatherReading(
        grid_cell_id="28.60_77.20",
        lat=28.6,
        lon=77.2,
        observed_at=observed_at,
        u_wind=1.05,
        v_wind=1.95,
        wind_speed=2.26,
        wind_direction=178.0,
        relative_humidity=54.0,
        product_type=WeatherProductType.ERA5,
    )
    load_weather_readings(db_session, [era5_final])

    rows = db_session.execute(select(RawWeatherReading)).scalars().all()
    assert len(rows) == 2  # both rows preserved, not overwritten in place

    era5t_row = next(r for r in rows if r.product_type == WeatherProductType.ERA5T)
    era5_row = next(r for r in rows if r.product_type == WeatherProductType.ERA5)
    assert era5t_row.is_superseded is True
    assert era5_row.is_superseded is False
    assert era5_row.wind_speed == 2.26
