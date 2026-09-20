from datetime import datetime, timedelta, timezone

import pandas as pd

from common.constants import Pollutant, SensorSourceName, WeatherProductType
from db.models import RawSensorReading, RawWeatherReading, Station
from features.build_features import build_feature_frame
from ingestion.weather.grid import nearest_grid_cell_id


def _seed_station(db_session, station_id="openaq:999", lat=28.6, lon=77.25):
    db_session.add(
        Station(
            station_id=station_id,
            name="Feature Test Station",
            lat=lat,
            lon=lon,
            city="Delhi",
            state="Delhi",
            source=SensorSourceName.OPENAQ,
            source_location_id="999",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()
    return station_id, lat, lon


def test_build_feature_frame_lags_match_hand_calculation(db_session):
    station_id, lat, lon = _seed_station(db_session)
    base = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)

    # 60 hours of a simple ramp: value = hour_index, so lag/rolling values
    # are trivially hand-verifiable.
    for h in range(60):
        db_session.add(
            RawSensorReading(
                station_id=station_id,
                pollutant=Pollutant.PM25,
                observed_at=base + timedelta(hours=h),
                source=SensorSourceName.OPENAQ,
                value=float(h),
                unit="ug/m3",
                source_record_id=f"test:{h}",
                ingested_at=datetime.now(timezone.utc),
            )
        )

    grid_id = nearest_grid_cell_id(lat, lon)
    for h in range(60):
        db_session.add(
            RawWeatherReading(
                grid_cell_id=grid_id,
                observed_at=base + timedelta(hours=h),
                product_type=WeatherProductType.ERA5,
                u_wind=1.0,
                v_wind=0.0,
                wind_speed=1.0,
                wind_direction=270.0,
                relative_humidity=50.0,
                is_superseded=False,
                ingested_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()

    as_of = base + timedelta(hours=59)  # value at as_of is 59
    frame = build_feature_frame(
        db_session, station_id, Pollutant.PM25, [as_of], station_lat=lat, station_lon=lon
    )

    assert len(frame) == 1
    row = frame.iloc[0]

    # value(t)=hour_index, so lag_1h at t=59 must equal value at t=58 = 58.
    assert row["lag_1h"] == 58.0
    assert row["lag_24h"] == 35.0  # 59 - 24
    # rolling_mean_6h at t=59 covers hours 54..59 -> mean(54..59) = 56.5
    assert row["rolling_mean_6h"] == 56.5

    assert row["wind_speed"] == 1.0
    assert row["relative_humidity"] == 50.0
    assert isinstance(row["hour_sin"], float)


def test_build_feature_frame_cold_start_station_returns_nan_not_error(db_session):
    station_id, lat, lon = _seed_station(db_session, station_id="openaq:no-history")
    as_of = datetime(2026, 1, 1, tzinfo=timezone.utc)

    frame = build_feature_frame(db_session, station_id, Pollutant.PM25, [as_of], station_lat=lat, station_lon=lon)

    assert len(frame) == 1
    assert frame.index[0] == pd.Timestamp(as_of)
