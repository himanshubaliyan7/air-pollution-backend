"""Thin task callables the DAGs import - each one is a call into the
already-built, already-tested ingestion/features/models packages. No
business logic lives here; this module only adapts those functions to
Airflow's PythonOperator calling convention and adds per-station looping +
logging, so the DAG files themselves stay declarative.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from common.config import get_settings
from common.constants import (
    DEFAULT_HORIZONS_HOURS,
    MAX_INPUT_STALENESS_HOURS,
    ModelType,
    Pollutant,
    SensorSourceName,
)
from db.models import Forecast, ModelRun, RawSensorReading, Station
from db.session import get_session
from features.build_features import build_feature_frame
from features.feature_store import write_features
from ingestion.config import ACTIVE_SOURCE, SENSOR_SOURCE_REGISTRY, make_station_id
from ingestion.loaders.sensor_loader import load_sensor_readings
from ingestion.loaders.weather_loader import load_weather_readings
from ingestion.weather.era5_client import ERA5Client
from ingestion.weather.grid import DELHI_NCR_AREA
from ingestion.weather.open_meteo_client import OpenMeteoClient
from alerting.notifier import send_exceedance_alert
from alerting.subscriber_store import find_subscribers
from models import evaluation, exceedance, predict, registry, train

logger = logging.getLogger(__name__)


def _active_stations(session) -> list[Station]:
    return list(session.execute(select(Station).where(Station.is_active.is_(True))).scalars().all())


# ---------------------------------------------------------------- ingestion

def ingest_sensor_readings(lookback_hours: int = 6) -> int:
    session = get_session()
    try:
        settings = get_settings()
        source_cls = SENSOR_SOURCE_REGISTRY[ACTIVE_SOURCE]
        source = source_cls(api_key=settings.openaq_api_key)

        stations = _active_stations(session)
        if not stations:
            logger.warning("No active stations - run scripts/seed_stations.py first")
            return 0

        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=lookback_hours)
        readings = source.fetch_readings(
            source_location_ids=[s.source_location_id for s in stations],
            pollutants=list(Pollutant),
            start=start,
            end=end,
        )
        return load_sensor_readings(session, readings, ACTIVE_SOURCE)
    finally:
        session.close()


def ingest_era5_recent(lookback_hours: int = 48) -> int:
    """Pulls the recent window (necessarily ERA5T for the last ~5 days)."""
    session = get_session()
    try:
        client = ERA5Client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=lookback_hours)
        readings = client.fetch(start, end, area=DELHI_NCR_AREA)
        return load_weather_readings(session, readings)
    finally:
        session.close()


def reconcile_era5_final(days_ago_center: int = 6, window_hours: int = 24) -> int:
    """Re-requests the window where ERA5T should now have been finalized to
    ERA5 - loader's supersede logic (see ingestion/loaders/weather_loader.py)
    marks the old ERA5T rows once the final values land."""
    session = get_session()
    try:
        client = ERA5Client()
        center = datetime.now(timezone.utc) - timedelta(days=days_ago_center)
        start = center - timedelta(hours=window_hours // 2)
        end = center + timedelta(hours=window_hours // 2)
        readings = client.fetch(start, end, area=DELHI_NCR_AREA)
        return load_weather_readings(session, readings)
    finally:
        session.close()


def ingest_open_meteo_forecast(forecast_days: int = 7) -> int:
    """Fills the real gap ERA5/ERA5T leaves for the last ~5 days and near
    future (see ingestion/weather/open_meteo_client.py) - re-run hourly
    alongside the ERA5 tasks so forecast_dag always has *some* weather for
    the current hour, even though ERA5 itself never will in real time."""
    session = get_session()
    try:
        client = OpenMeteoClient()
        readings = client.fetch(forecast_days=forecast_days, area=DELHI_NCR_AREA)
        return load_weather_readings(session, readings)
    finally:
        session.close()


def summarize_data_age(newest_by_station: dict[str, datetime | None], now: datetime) -> dict[str, int]:
    """Buckets stations by how old their newest reading is. Upstream OpenAQ/CPCB
    data lags badly (verified 2026-09-20: most stations days behind), and that
    silently decides how many stations can be forecast at all - so every
    ingestion run reports it."""
    buckets = {"<=6h": 0, "<=24h": 0, "<=72h": 0, ">72h": 0, "never": 0}
    for newest in newest_by_station.values():
        if newest is None:
            buckets["never"] += 1
            continue
        hours = (now - newest).total_seconds() / 3600
        key = "<=6h" if hours <= 6 else "<=24h" if hours <= 24 else "<=72h" if hours <= 72 else ">72h"
        buckets[key] += 1
    return buckets


def ingestion_data_quality_check(sensor_rows: int, weather_rows: int) -> None:
    session = get_session()
    try:
        stations = _active_stations(session)
        newest = {
            sid: ts
            for sid, ts in session.execute(
                select(RawSensorReading.station_id, func.max(RawSensorReading.observed_at)).group_by(
                    RawSensorReading.station_id
                )
            )
        }
    finally:
        session.close()

    if stations:
        ages = summarize_data_age({s.station_id: newest.get(s.station_id) for s in stations}, datetime.now(timezone.utc))
        logger.info("Active stations by age of newest reading: %s (of %d)", ages, len(stations))
        if ages["<=6h"] == 0:
            logger.warning(
                "No active station has a reading newer than %dh - no forecasts can be generated", MAX_INPUT_STALENESS_HOURS
            )

    if stations and sensor_rows == 0 and weather_rows == 0:
        logger.error(
            "Ingestion wrote zero rows this run despite %d active stations - "
            "likely an API/credential problem, not just a quiet hour",
            len(stations),
        )
        raise RuntimeError("ingestion_data_quality_check: zero rows ingested with active stations configured")


def refresh_station_activity(max_dark_days: int = 30) -> dict[str, int]:
    """Marks stations inactive when OpenAQ says they have not reported for
    `max_dark_days` (or ever), and reactivates any that have resumed. Dark
    stations otherwise cost several API calls per hourly ingestion run for
    nothing (39 of 124 Delhi NCR locations had been dark for 30+ days,
    verified 2026-09-20)."""
    from ingestion.config import DELHI_NCR_BBOX, DELHI_NCR_COUNTRY_ISO

    session = get_session()
    try:
        source = SENSOR_SOURCE_REGISTRY[ACTIVE_SOURCE](api_key=get_settings().openaq_api_key)
        last_seen = source.location_last_data_times(bbox=DELHI_NCR_BBOX, country=DELHI_NCR_COUNTRY_ISO)
        if not last_seen:
            # An empty answer means the call misbehaved, not that every
            # station vanished - never deactivate the whole network on that.
            raise RuntimeError("OpenAQ returned no locations; refusing to change station activity")

        cutoff = datetime.now(timezone.utc) - timedelta(days=max_dark_days)
        deactivated = reactivated = 0
        for station in session.execute(select(Station)).scalars().all():
            if station.source_location_id not in last_seen:
                continue  # unknown to OpenAQ this time; leave as is
            newest = last_seen[station.source_location_id]
            should_be_active = newest is not None and newest >= cutoff
            if station.is_active and not should_be_active:
                station.is_active = False
                deactivated += 1
            elif not station.is_active and should_be_active:
                station.is_active = True
                reactivated += 1
        session.commit()
        logger.info("Station activity refreshed: %d deactivated, %d reactivated", deactivated, reactivated)
        return {"deactivated": deactivated, "reactivated": reactivated}
    finally:
        session.close()


# --------------------------------------------------------- feature engineering

def compute_and_write_features(lookback_hours: int = 6) -> int:
    session = get_session()
    try:
        stations = _active_stations(session)
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        as_of_times = [end - timedelta(hours=h) for h in range(lookback_hours)]

        total = 0
        for station in stations:
            for pollutant in Pollutant:
                frame = build_feature_frame(
                    session, station.station_id, pollutant, as_of_times,
                    station_lat=station.lat, station_lon=station.lon,
                )
                total += write_features(session, station.station_id, pollutant, frame)
        return total
    finally:
        session.close()


# -------------------------------------------------------------------- forecast

def forecast_anchor(newest_reading: datetime | None, now_hour: datetime) -> datetime | None:
    """The as_of hour to forecast from for one station/pollutant, or None if
    its newest reading is missing or older than MAX_INPUT_STALENESS_HOURS.

    Anchors on the newest hour that actually has a reading, not on "now": the
    upstream feed lags, and build_feature_frame only yields lag/rolling values
    for an as_of that has an observation (which is also exactly what training
    saw, so there is no train/serve skew). Horizons are 24-120h, so a few
    hours of anchor lag is immaterial."""
    if newest_reading is None:
        return None
    as_of = min(now_hour, newest_reading.replace(minute=0, second=0, microsecond=0))
    if now_hour - as_of > timedelta(hours=MAX_INPUT_STALENESS_HOURS):
        return None
    return as_of


def generate_forecasts() -> dict:
    session = get_session()
    try:
        stations = _active_stations(session)
        thresholds = exceedance.load_thresholds()
        now_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

        latest_reading = {
            (sid, pol): ts
            for sid, pol, ts in session.execute(
                select(
                    RawSensorReading.station_id,
                    RawSensorReading.pollutant,
                    func.max(RawSensorReading.observed_at),
                ).group_by(RawSensorReading.station_id, RawSensorReading.pollutant)
            )
        }

        written = 0
        skipped_stale = 0
        new_crossings: list[dict] = []
        for station in stations:
            for pollutant in Pollutant:
                as_of = forecast_anchor(latest_reading.get((station.station_id, pollutant)), now_hour)
                if as_of is None:
                    skipped_stale += 1
                    continue

                for horizon in DEFAULT_HORIZONS_HOURS:
                    result = predict.forecast(
                        session, station.station_id, pollutant, horizon, as_of,
                        station_lat=station.lat, station_lon=station.lon, thresholds=thresholds,
                    )
                    if result is None:
                        continue

                    target_time = as_of + timedelta(hours=horizon)

                    was_flagged = session.execute(
                        select(Forecast.exceedance_flag)
                        .where(
                            Forecast.station_id == station.station_id,
                            Forecast.pollutant == pollutant,
                            Forecast.target_time == target_time,
                        )
                        .order_by(Forecast.forecast_made_at.desc())
                        .limit(1)
                    ).scalar_one_or_none()

                    # Upsert: the anchor is the newest observed hour, which does
                    # not advance between hourly runs while the upstream feed
                    # is lagging, so a re-run legitimately hits the same key.
                    values = {
                        "station_id": station.station_id,
                        "pollutant": pollutant,
                        "model_id": result.model_id,
                        "forecast_made_at": as_of,
                        "target_time": target_time,
                        "horizon_hours": horizon,
                        "point_forecast": result.point_forecast,
                        "quantile_low": result.quantile_low,
                        "quantile_high": result.quantile_high,
                        "exceedance_probability": result.exceedance_probability,
                        "exceedance_flag": result.exceedance_flag,
                    }
                    session.execute(
                        pg_insert(Forecast)
                        .values(**values)
                        .on_conflict_do_update(
                            index_elements=["station_id", "pollutant", "model_id", "forecast_made_at", "target_time"],
                            set_={k: v for k, v in values.items()
                                  if k not in ("station_id", "pollutant", "model_id", "forecast_made_at", "target_time")},
                        )
                    )
                    written += 1

                    if result.exceedance_flag and not was_flagged:
                        # JSON-serializable (str/isoformat) so this survives
                        # an Airflow XCom hop to trigger_alerts_for_crossings.
                        new_crossings.append(
                            {
                                "station_id": station.station_id,
                                "station_name": station.name,
                                "pollutant": pollutant.value,
                                "target_time": target_time.isoformat(),
                                "forecast_made_at": as_of.isoformat(),
                                "model_id": str(result.model_id),
                                "quantile_high": result.quantile_high,
                                "exceedance_probability": result.exceedance_probability,
                            }
                        )
        session.commit()
        logger.info(
            "Forecasts written: %d; station/pollutant pairs skipped for input older than %dh: %d",
            written, MAX_INPUT_STALENESS_HOURS, skipped_stale,
        )
        return {"forecasts_written": written, "skipped_stale": skipped_stale, "new_crossings": new_crossings}
    finally:
        session.close()


# ----------------------------------------------------------------- evaluation

def evaluate_recent_forecasts(evaluation_window_days: int = 1) -> int:
    from db.models import ExceedanceEvaluation

    session = get_session()
    try:
        thresholds = exceedance.load_thresholds()
        stations = _active_stations(session)
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=evaluation_window_days)

        written = 0
        for station in stations:
            for pollutant in Pollutant:
                threshold_conc = exceedance.get_health_threshold_concentration(pollutant, thresholds)

                for horizon in DEFAULT_HORIZONS_HOURS:
                    forecast_rows = session.execute(
                        select(Forecast).where(
                            Forecast.station_id == station.station_id,
                            Forecast.pollutant == pollutant,
                            Forecast.horizon_hours == horizon,
                            Forecast.target_time >= window_start,
                            Forecast.target_time <= now,
                        )
                    ).scalars().all()
                    if not forecast_rows:
                        continue

                    actual_rows = session.execute(
                        select(RawSensorReading.observed_at, RawSensorReading.value).where(
                            RawSensorReading.station_id == station.station_id,
                            RawSensorReading.pollutant == pollutant,
                            RawSensorReading.observed_at >= window_start,
                            RawSensorReading.observed_at <= now,
                        )
                    ).all()
                    actual_by_time = {r.observed_at: r.value for r in actual_rows}

                    paired = [
                        (actual_by_time[r.target_time], r.point_forecast, r.exceedance_flag)
                        for r in forecast_rows
                        if r.target_time in actual_by_time
                    ]
                    if not paired:
                        continue

                    import numpy as np

                    y_true = np.array([p[0] for p in paired])
                    y_pred = np.array([p[1] for p in paired])
                    y_pred_flag = np.array([int(p[2]) for p in paired])
                    y_true_flag = (y_true > threshold_conc).astype(int)

                    reg_metrics = evaluation.regression_metrics(y_true, y_pred)
                    class_metrics = evaluation.exceedance_classification_metrics(y_true_flag, y_pred_flag)

                    session.add(
                        ExceedanceEvaluation(
                            station_id=station.station_id,
                            pollutant=pollutant,
                            horizon_hours=horizon,
                            model_id=forecast_rows[0].model_id,
                            evaluation_window_start=window_start,
                            evaluation_window_end=now,
                            precision=class_metrics["precision"],
                            recall=class_metrics["recall"],
                            f1=class_metrics["f1"],
                            mae=reg_metrics["mae"],
                            rmse=reg_metrics["rmse"],
                            n_exceedance_days_actual=class_metrics["n_exceedance_days_actual"],
                            n_exceedance_days_predicted=class_metrics["n_exceedance_days_predicted"],
                            computed_at=now,
                        )
                    )
                    written += 1
        session.commit()
        return written
    finally:
        session.close()


# -------------------------------------------------------------------- alerts

def trigger_alerts_for_crossings(new_crossings: list[dict]) -> int:
    """Takes generate_forecasts()'s new_crossings (JSON-safe dicts, e.g. from
    an Airflow XCom pull) and emails every matching active subscriber."""
    if not new_crossings:
        return 0

    session = get_session()
    try:
        thresholds = exceedance.load_thresholds()
        sent = 0
        for crossing in new_crossings:
            pollutant = crossing["pollutant"]
            subscribers = find_subscribers(session, crossing["station_id"], pollutant)
            if not subscribers:
                continue

            aqi_category = exceedance.get_aqi_category(thresholds, Pollutant(pollutant), crossing["quantile_high"])
            for subscriber in subscribers:
                sent += int(
                    send_exceedance_alert(
                        session,
                        subscriber,
                        station_id=crossing["station_id"],
                        station_name=crossing["station_name"],
                        pollutant=pollutant,
                        aqi_category=aqi_category,
                        forecast_value=crossing["quantile_high"],
                        model_id=uuid.UUID(crossing["model_id"]),
                        forecast_made_at=datetime.fromisoformat(crossing["forecast_made_at"]),
                        target_time=datetime.fromisoformat(crossing["target_time"]),
                    )
                )
        return sent
    finally:
        session.close()


# ----------------------------------------------------------------- retraining

def retrain_all(training_window_days: int = 365, holdout_days: int = 30) -> dict:
    session = get_session()
    try:
        stations = _active_stations(session)
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=training_window_days)

        results = {}
        for station in stations:
            for pollutant in Pollutant:
                for horizon in DEFAULT_HORIZONS_HOURS:
                    ids = train.train_station_pollutant_horizon(
                        session, station.station_id, pollutant, horizon, window_start, now, holdout_days
                    )
                    results[f"{station.station_id}/{pollutant.value}/{horizon}h"] = len(ids)
        return results
    finally:
        session.close()
