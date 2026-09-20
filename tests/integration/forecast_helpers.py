"""Shared seeding helpers for the generate_forecasts / alerting / API-safety
integration tests. All functions take the (test-DB-only) ``db_session``."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from common.constants import DEFAULT_HORIZONS_HOURS, ModelType, Pollutant, SensorSourceName
from db.models import Forecast, ModelRun, RawSensorReading, Station
from models.predict import ForecastResult

UTC = timezone.utc
# A fixed, deliberately NOT hour-aligned clock: 15:45 UTC.
NOW = datetime(2026, 9, 20, 15, 45, tzinfo=UTC)
NOW_HOUR = datetime(2026, 9, 20, 15, tzinfo=UTC)


def add_station(db, name: str, *, active: bool = True, lat: float = 28.6, lon: float = 77.2) -> str:
    station_id = f"openaq:{name}"
    db.add(
        Station(
            station_id=station_id, name=f"Station {name}", lat=lat, lon=lon, city="Delhi", state="Delhi",
            source=SensorSourceName.OPENAQ, source_location_id=name, is_active=active, created_at=NOW,
        )
    )
    db.commit()
    return station_id


def add_reading(db, station_id: str, observed_at: datetime, pollutant: Pollutant = Pollutant.PM25, value: float = 50.0):
    db.add(
        RawSensorReading(
            station_id=station_id, pollutant=pollutant, observed_at=observed_at, source=SensorSourceName.OPENAQ,
            value=value, unit="ug/m3", source_record_id=None, ingested_at=NOW,
        )
    )
    db.commit()


def add_model_run(db, station_id: str, pollutant: Pollutant = Pollutant.PM25, horizon: int = 24) -> uuid.UUID:
    model_id = uuid.uuid4()
    db.add(
        ModelRun(
            model_id=model_id, station_id=station_id, pollutant=pollutant, horizon_hours=horizon,
            model_type=ModelType.QUANTILE_REGRESSOR, quantile=0.5, feature_set_version="v1",
            artifact_path="unused", trained_at=NOW, training_window_start=NOW - timedelta(days=30),
            training_window_end=NOW, metrics={}, hyperparams={}, is_active=True,
        )
    )
    db.commit()
    return model_id


@dataclass
class FakePredict:
    """Stand-in for models.predict.forecast with test-controlled output, so a
    test can flip flags/values between runs without training anything.
    ``model_ids`` maps (station_id, pollutant) -> a model_runs row (FK target)."""

    model_ids: dict
    flag: bool = False
    point: float = 100.0
    missing_horizons: set = field(default_factory=set)
    flag_by_horizon: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)

    def __call__(self, session, station_id, pollutant, horizon_hours, as_of_time, **_):
        self.calls.append((station_id, pollutant, horizon_hours, as_of_time))
        if horizon_hours in self.missing_horizons:
            return None
        flag = self.flag_by_horizon.get(horizon_hours, self.flag)
        return ForecastResult(
            model_id=self.model_ids[(station_id, pollutant)],
            point_forecast=self.point + horizon_hours,
            quantile_low=self.point - 10 + horizon_hours,
            quantile_high=self.point + 30 + horizon_hours,
            exceedance_probability=0.9 if flag else 0.05,
            exceedance_flag=flag,
        )


def seed_forecast_run(
    db,
    station_id: str,
    made_at: datetime,
    *,
    pollutant: Pollutant = Pollutant.PM25,
    horizons=tuple(DEFAULT_HORIZONS_HOURS),
    flagged_horizons=(),
    probability: float = 0.05,
    model_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Writes one forecast run (one row per horizon) for a station/pollutant."""
    model_id = model_id or add_model_run(db, station_id, pollutant)
    for h in horizons:
        flagged = h in flagged_horizons
        db.add(
            Forecast(
                station_id=station_id, pollutant=pollutant, model_id=model_id, forecast_made_at=made_at,
                target_time=made_at + timedelta(hours=h), horizon_hours=h, point_forecast=100.0,
                quantile_low=80.0, quantile_high=130.0,
                exceedance_probability=0.9 if flagged else probability, exceedance_flag=flagged,
            )
        )
    db.commit()
    return model_id
