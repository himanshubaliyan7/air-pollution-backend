"""Daily-mean targets and the graded verdict (owner decision 2026-10-02).

A day's verdict is the CPCB category of that day's MEAN concentration - CPCB's
own definition (24-hour averaging) - not "one forecast hour above the
threshold", which called nearly every day no-go. The models forecast the mean
of each of the next five station-local calendar days; this module holds the
pure parts shared by training, serving and the backtest: the labels, and the
rule that turns a day's quantile forecasts into a category and a verdict.
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from common.constants import Pollutant
from models.exceedance import probability_from_quantiles

# A day's mean is only a label (or an evaluation truth) when most of the day
# was measured; scripts/winter_eval.py used the same 18 of 24.
MIN_HOURS_PER_DAY = 18


def local_day_means(series: pd.Series, tz_name: str, min_hours: int = MIN_HOURS_PER_DAY) -> pd.Series:
    """Mean per station-local calendar day, for days with at least `min_hours`
    readings. `series` is hourly with a tz-aware UTC index; the result is
    indexed by the day's local midnight as a naive Timestamp."""
    if series.empty:
        return pd.Series(dtype="float64")
    days = local_days(series.index, tz_name)
    grouped = series.groupby(days)
    means = grouped.mean()
    return means[grouped.count() >= min_hours]


def local_days(index: pd.DatetimeIndex, tz_name: str) -> pd.DatetimeIndex:
    """The local calendar day of each instant, as naive midnight Timestamps."""
    return index.tz_convert(tz_name).tz_localize(None).normalize()


def day_ahead_labels(as_of: pd.DatetimeIndex, day_means: pd.Series, tz_name: str, days_ahead: int) -> np.ndarray:
    """For each as_of instant, the mean of the local day `days_ahead` days after
    the one it falls in (NaN where that day has no usable mean)."""
    target_days = local_days(as_of, tz_name) + pd.Timedelta(days=days_ahead)
    return day_means.reindex(target_days).to_numpy(dtype="float64")


def target_day_start(as_of: datetime, tz_name: str, days_ahead: int) -> datetime:
    """Local midnight (as UTC) of the day `days_ahead` days after the one
    `as_of` falls in: the target_time of a daily-mean forecast."""
    zone = ZoneInfo(tz_name)
    day = as_of.astimezone(zone).date() + timedelta(days=days_ahead)
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)


def monotone(quantile_predictions: dict[float, float]) -> dict[float, float]:
    """Separately fitted quantile models can cross (the 0.9 model below the 0.5
    one). Sorting the values restores a valid distribution; the interpolation
    in probability_from_quantiles needs one."""
    quantiles = sorted(quantile_predictions)
    values = sorted(quantile_predictions[q] for q in quantiles)
    return dict(zip(quantiles, values))


def category_for_day(quantile_predictions: dict[float, float], thresholds: dict, pollutant: Pollutant) -> str:
    """The worst category the day's mean reaches with at least the configured
    probability (thresholds["exceedance_probability_decision_threshold"])."""
    decision = thresholds["exceedance_probability_decision_threshold"]
    quantile_predictions = monotone(quantile_predictions)
    breakpoints = thresholds["pollutants"][pollutant.value]["breakpoints"]
    category = breakpoints[0]["category"]
    for bp in breakpoints[1:]:
        if probability_from_quantiles(quantile_predictions, float(bp["conc_range"][0])) >= decision:
            category = bp["category"]
    return category


def verdict_for_category(category: str, thresholds: dict, pollutant: Pollutant) -> str:
    """go below the health-threshold category, caution from it, no-go from
    thresholds["no_go_category"] upwards."""
    order = [bp["category"] for bp in thresholds["pollutants"][pollutant.value]["breakpoints"]]
    rank = order.index(category)
    if rank >= order.index(thresholds["no_go_category"]):
        return "no-go"
    if rank >= order.index(thresholds["health_threshold_category"]):
        return "caution"
    return "go"
