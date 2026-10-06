"""The daily-mean forecast as a model-free rule (owner decision 2026-10-03).

A day's mean k days ahead = the station's mean over the last 24 hours times a
ratio, and the ratio's 10 / 50 / 90 % quantiles come from history: every
station's log(day mean k days after / last-24h mean) over the training window,
the stations of one region together (config/regions.yaml). Two cities differ
in day-to-day behaviour, so a region never borrows another region's ratios.

scripts/daily_verdict_backtest.py (2026-10-02/03) chose it: as exact as one
LightGBM model for all stations, more exact than one per station, and the best
calibrated (91 % of the 2025 onset days fell below its 90 % quantile). It
also cannot learn a sensor fault or last year's calendar, which is what broke
the per-station models that week.

The fitted ratios are stored on ordinary model_runs rows (hyperparams), one
per station, pollutant, horizon and quantile, so the registry, the forecast
run and the outlook work as for any other quantile model.
"""

import logging
from datetime import datetime
from typing import NamedTuple

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.constants import DEFAULT_HORIZONS_HOURS, Pollutant
from common.regions import region_for_point
from db.models import Feature, Station
from db.readings import hourly_readings
from features.feature_store import get_feature_set_version
from models.daily import day_ahead_labels, local_day_means

logger = logging.getLogger(__name__)

KIND = "level_ratio"
LEVEL_FEATURE = "rolling_mean_24h"
FLOOR = 1.0  # ug/m3: keeps the ratio finite for an instrument reading zero

# (region, pollutant, window_start, window_end, quantiles) -> fitted ratios. A
# weekly retrain asks once per station and horizon with the same window; the
# fit reads every station of the region, so it is done once per region and run.
_cache: dict = {}


class Fit(NamedTuple):
    """One horizon's fit: the quantiles of the log ratio, the labelled rows
    behind them, and how many distinct local days and stations they cover."""
    quantiles: dict[float, float]
    rows: int
    days: int
    stations: int


def is_level_model(model_run) -> bool:
    return (model_run.hyperparams or {}).get("kind") == KIND


def log_ratio(day_mean: np.ndarray, level: np.ndarray) -> np.ndarray:
    return np.log(np.maximum(day_mean, FLOOR) / np.maximum(level, FLOOR))


def forecast_values(level: float, log_ratios: dict[float, float]) -> dict[float, float]:
    """The day's mean at each quantile, from the last-24h mean."""
    return {q: max(level, FLOOR) * float(np.exp(r)) for q, r in log_ratios.items()}


def clear_cache() -> None:
    _cache.clear()


def fit_log_ratio_quantiles(
    session: Session, pollutant: Pollutant, window_start: datetime, window_end: datetime, quantiles: list[float],
    region_id: str | None = None,
) -> dict[int, Fit]:
    """{horizon_hours: Fit} over the active stations of `region_id`. A horizon
    with no labelled row is absent. `region_id=None` pools every active station
    (the fit before regions existed; only for comparison and offline analysis,
    never for a station that is served)."""
    key = (region_id, pollutant, window_start, window_end, tuple(quantiles))
    if key in _cache:
        return _cache[key]

    from models.outlook import station_timezone  # outlook imports the serving side; keep it local

    ratios: dict[int, list[np.ndarray]] = {h: [] for h in DEFAULT_HORIZONS_HOURS}
    days: dict[int, set] = {h: set() for h in DEFAULT_HORIZONS_HOURS}
    version = get_feature_set_version()
    for station in session.execute(select(Station).where(Station.is_active.is_(True))).scalars():
        if region_id is not None and _region_id(station) != region_id:
            continue
        rows = session.execute(
            select(Feature.feature_time, Feature.features[LEVEL_FEATURE].astext)
            .where(Feature.station_id == station.station_id, Feature.pollutant == pollutant,
                   Feature.feature_set_version == version,
                   Feature.feature_time >= window_start, Feature.feature_time <= window_end)
        ).all()
        level = pd.Series(
            pd.to_numeric([value for _, value in rows], errors="coerce"),
            index=pd.DatetimeIndex([t for t, _ in rows], tz="UTC"),
        ).dropna()
        readings = hourly_readings(session, station.station_id, pollutant, window_start, window_end)
        if level.empty or not readings:
            continue
        tz_name = station_timezone(station)
        series = pd.Series([v for _, v in readings], index=pd.DatetimeIndex([t for t, _ in readings], tz="UTC"))
        day_means = local_day_means(series.groupby(level=0).first(), tz_name)
        for horizon in DEFAULT_HORIZONS_HOURS:
            labels = day_ahead_labels(level.index, day_means, tz_name, horizon // 24)
            known = ~np.isnan(labels)
            if not known.any():
                continue
            ratios[horizon].append(log_ratio(labels[known], level.to_numpy()[known]))
            days[horizon].update(level.index[known].tz_convert(tz_name).date)

    fitted = {}
    for horizon, parts in ratios.items():
        values = np.concatenate(parts) if parts else np.array([])
        if values.size:
            fitted[horizon] = Fit(dict(zip(quantiles, np.quantile(values, quantiles).tolist())), int(values.size),
                                  len(days[horizon]), len(parts))
    logger.info("Level ratios for %s in %s: %s", pollutant.value, region_id or "all stations",
                {h: (round(fitted[h].quantiles[0.5], 3), fitted[h].rows) for h in sorted(fitted)})
    _cache[key] = fitted
    return fitted


def _region_id(station: Station) -> str | None:
    region = region_for_point(station.lat, station.lon)
    return region.id if region else None
