import math
import random
from datetime import datetime, timedelta, timezone

import pandas as pd

from common.constants import ModelType, Pollutant, SensorSourceName, WeatherProductType
from db.models import ModelRun, RawSensorReading, RawWeatherReading, Station
from features.build_features import build_feature_frame
from features.feature_store import write_features
from ingestion.weather.grid import nearest_grid_cell_id
from models import predict, registry
from models.train import train_station_pollutant_horizon

N_HOURS = 400
HORIZON_HOURS = 24


def _seed_full_dataset(db_session):
    station_id = "openaq:train-test"
    lat, lon = 28.6, 77.25
    db_session.add(
        Station(
            station_id=station_id,
            name="Train Test Station",
            lat=lat,
            lon=lon,
            city="Delhi",
            state="Delhi",
            source=SensorSourceName.OPENAQ,
            source_location_id="train-test",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rng = random.Random(42)
    grid_id = nearest_grid_cell_id(lat, lon)

    for h in range(N_HOURS):
        ts = base + timedelta(hours=h)
        # A learnable diurnal pattern with noise, so quantile models have
        # actual signal to fit (not pure noise, which would make the
        # holdout classification metrics meaningless for a smoke test).
        value = 80 + 40 * math.sin(2 * math.pi * (h % 24) / 24) + rng.gauss(0, 5)
        value = max(5.0, value)
        db_session.add(
            RawSensorReading(
                station_id=station_id,
                pollutant=Pollutant.PM25,
                observed_at=ts,
                source=SensorSourceName.OPENAQ,
                value=value,
                unit="ug/m3",
                source_record_id=f"train-test:{h}",
                ingested_at=datetime.now(timezone.utc),
            )
        )
        db_session.add(
            RawWeatherReading(
                grid_cell_id=grid_id,
                observed_at=ts,
                product_type=WeatherProductType.ERA5,
                u_wind=rng.gauss(1, 0.5),
                v_wind=rng.gauss(1, 0.5),
                wind_speed=abs(rng.gauss(2, 1)),
                wind_direction=rng.uniform(0, 360),
                relative_humidity=rng.uniform(30, 70),
                is_superseded=False,
                ingested_at=datetime.now(timezone.utc),
            )
        )
    db_session.commit()

    # Materialize features (what feature_engineering_dag would do in batch),
    # for the range that has enough lookback history AND a realizable label
    # at +HORIZON_HOURS.
    as_of_times = [base + timedelta(hours=h) for h in range(48, N_HOURS - HORIZON_HOURS)]
    frame = build_feature_frame(db_session, station_id, Pollutant.PM25, as_of_times, station_lat=lat, station_lon=lon)
    write_features(db_session, station_id, Pollutant.PM25, frame)

    return station_id, lat, lon, base, as_of_times


def test_train_registers_model_with_metrics_and_predict_produces_sane_forecast(db_session):
    station_id, lat, lon, base, as_of_times = _seed_full_dataset(db_session)

    window_start = as_of_times[0]
    window_end = as_of_times[-1]

    registered_ids = train_station_pollutant_horizon(
        db_session,
        station_id,
        Pollutant.PM25,
        HORIZON_HOURS,
        window_start,
        window_end,
        holdout_days=2,
    )

    assert len(registered_ids) == 4  # 3 quantiles + 1 classifier

    rows = db_session.query(ModelRun).filter(ModelRun.station_id == station_id).all()
    assert len(rows) == 4
    for row in rows:
        assert row.metrics  # non-empty
    median_row = next(r for r in rows if r.model_type == ModelType.QUANTILE_REGRESSOR and r.quantile == 0.5)
    assert "f1" in median_row.metrics
    assert "mae" in median_row.metrics
    assert 0.0 <= median_row.metrics["precision"] <= 1.0
    assert 0.0 <= median_row.metrics["recall"] <= 1.0

    # First-ever training run for this key must bootstrap-activate (nothing
    # to compare against yet) - see registry.promote_if_better.
    active = registry.get_active_models(db_session, station_id, Pollutant.PM25, HORIZON_HOURS, ModelType.QUANTILE_REGRESSOR)
    assert len(active) == 3
    assert {r.quantile for r in active} == {0.1, 0.5, 0.9}

    as_of = window_end
    result = predict.forecast(db_session, station_id, Pollutant.PM25, HORIZON_HOURS, as_of, station_lat=lat, station_lon=lon)

    assert result is not None
    assert result.quantile_low <= result.point_forecast <= result.quantile_high
    assert 0.0 <= result.exceedance_probability <= 1.0
    assert isinstance(result.exceedance_flag, bool)


def test_predict_returns_none_instead_of_crashing_on_missing_artifact(db_session):
    """A missing/corrupt model artifact for one station must not raise and
    take down a forecast_dag run for every other station - see
    models/predict.py's load_booster try/except."""
    station_id, lat, lon, base, as_of_times = _seed_full_dataset(db_session)
    train_station_pollutant_horizon(
        db_session, station_id, Pollutant.PM25, HORIZON_HOURS, as_of_times[0], as_of_times[-1], holdout_days=2
    )

    active = registry.get_active_models(db_session, station_id, Pollutant.PM25, HORIZON_HOURS, ModelType.QUANTILE_REGRESSOR)
    for row in active:
        row.artifact_path = "/nonexistent/path/model.txt"
    db_session.commit()

    result = predict.forecast(
        db_session, station_id, Pollutant.PM25, HORIZON_HOURS, as_of_times[-1], station_lat=lat, station_lon=lon
    )
    assert result is None
