"""Single feature-computation entrypoint, shared by batch and inference.

features/feature_engineering_dag.py (batch, writes to the `features` table)
and models/predict.py (real-time inference) both call build_feature_frame()
directly - there is no second implementation of lag/rolling/time-feature
logic anywhere else in the codebase. This is what prevents train/serve skew:
whatever transform training saw is exactly what inference sees, because it's
the same function call.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.constants import LAG_HOURS, ROLLING_WINDOWS_HOURS, Pollutant, WeatherProductType
from db.models import RawWeatherReading
from db.readings import hourly_readings
from features.calendar.delhi_calendar import load_calendar_config
from features.lag_features import compute_lags, compute_rolling, reindex_hourly
from features.time_features import add_calendar_flags, add_cyclical
from features.weather_features import join_weather
from ingestion.weather.grid import nearest_grid_cell_id

MAX_LOOKBACK_HOURS = max(LAG_HOURS + ROLLING_WINDOWS_HOURS)


def _fetch_pollutant_series(
    session: Session, station_id: str, pollutant: Pollutant, start: datetime, end: datetime
) -> pd.Series:
    # One value per hour, OpenAQ first and CPCB filling gaps (db/readings.py).
    rows = hourly_readings(session, station_id, pollutant, start, end)
    if not rows:
        return pd.Series(dtype="float64")
    idx = pd.DatetimeIndex([t for t, _ in rows], tz="UTC")
    series = pd.Series([v for _, v in rows], index=idx)
    # The same instant can arrive with different tz offsets; keep one value.
    return series.groupby(series.index).first()


def _fetch_weather_frame(session: Session, grid_cell_id: str, start: datetime, end: datetime) -> pd.DataFrame:
    stmt = select(
        RawWeatherReading.observed_at,
        RawWeatherReading.wind_speed,
        RawWeatherReading.wind_direction,
        RawWeatherReading.relative_humidity,
        RawWeatherReading.product_type,
    ).where(
        RawWeatherReading.grid_cell_id == grid_cell_id,
        RawWeatherReading.observed_at >= start,
        RawWeatherReading.observed_at <= end,
    )
    rows = session.execute(stmt).all()
    if not rows:
        return pd.DataFrame(columns=["wind_speed", "wind_direction", "relative_humidity"])

    df = pd.DataFrame(
        {
            "observed_at": [r.observed_at for r in rows],
            "wind_speed": [r.wind_speed for r in rows],
            "wind_direction": [r.wind_direction for r in rows],
            "relative_humidity": [r.relative_humidity for r in rows],
            "product_type": [r.product_type for r in rows],
        }
    )
    # Prefer real reanalysis over the near-term forecast fallback when
    # more than one product exists for the same hour: ERA5 (final) >
    # ERA5T (preliminary) > FORECAST (Open-Meteo, only ever covers the
    # last ~5 days ERA5 hasn't published yet - see open_meteo_client.py).
    _RANK = {WeatherProductType.ERA5: 2, WeatherProductType.ERA5T: 1, WeatherProductType.FORECAST: 0}
    df["_rank"] = df["product_type"].map(_RANK)
    df = df.sort_values("_rank").drop_duplicates(subset="observed_at", keep="last")
    df = df.set_index(pd.DatetimeIndex(df["observed_at"], tz="UTC")).sort_index()
    return df[["wind_speed", "wind_direction", "relative_humidity"]]


def build_feature_frame(
    session: Session,
    station_id: str,
    pollutant: Pollutant,
    as_of_times: list[datetime],
    station_lat: float,
    station_lon: float,
) -> pd.DataFrame:
    """Returns a DataFrame indexed by as_of_time (UTC), one row per requested
    timestamp, with every lag/rolling/cyclical/calendar/weather column. Rows
    for an as_of_time with insufficient history simply have NaN lag/rolling
    columns - callers (training, predict.py) decide how to handle that."""
    if not as_of_times:
        return pd.DataFrame()

    window_start = min(as_of_times) - timedelta(hours=MAX_LOOKBACK_HOURS)
    window_end = max(as_of_times)

    raw_series = _fetch_pollutant_series(session, station_id, pollutant, window_start, window_end)
    hourly_series = reindex_hourly(raw_series)

    if hourly_series.empty:
        # No history at all for this station/pollutant/window - return a
        # frame of NaNs at the requested timestamps rather than erroring, so
        # a cold-start station doesn't take down a whole batch run.
        idx = pd.DatetimeIndex(as_of_times, tz="UTC") if as_of_times[0].tzinfo else pd.DatetimeIndex(as_of_times)
        return pd.DataFrame(index=idx)

    lag_df = compute_lags(hourly_series, LAG_HOURS)
    rolling_df = compute_rolling(hourly_series, ROLLING_WINDOWS_HOURS)
    base = pd.concat([lag_df, rolling_df], axis=1)

    cyclical_df = add_cyclical(base)
    calendar_df = add_calendar_flags(base, load_calendar_config())
    combined = pd.concat([base, cyclical_df, calendar_df], axis=1)

    grid_cell_id = nearest_grid_cell_id(station_lat, station_lon)
    weather_df = _fetch_weather_frame(session, grid_cell_id, window_start, window_end)
    combined = join_weather(combined, weather_df)

    requested_idx = pd.DatetimeIndex(as_of_times, tz="UTC" if as_of_times[0].tzinfo else None)
    return combined.reindex(requested_idx)
