"""Threshold-exceedance logic: the core product requirement.

v1 exceedance signal is quantile-derived (probability_from_quantiles):
interpolates P(value > threshold) from a station/pollutant/horizon's fitted
quantile regressors. This is threshold-agnostic (works for any configured
threshold without retraining) and degrades more gracefully than a from-
scratch classifier when historical exceedance-day counts are still small
(cold start - see models/train.py). A direct classifier is trained
alongside for comparison (models/lightgbm_pipeline.py); retraining_dag's
promote_if_better can switch which model_type is is_active later without
any change here or downstream, since both populate the same
forecasts.exceedance_probability/exceedance_flag columns.
"""

from datetime import date, datetime, time

import numpy as np
import pandas as pd

from common.config import get_settings, load_yaml_config, to_ist
from common.constants import Pollutant


def load_thresholds() -> dict:
    return load_yaml_config(get_settings().thresholds_config_path)


def get_category_lower_bound(thresholds: dict, pollutant: Pollutant, category: str) -> float:
    breakpoints = thresholds["pollutants"][pollutant.value]["breakpoints"]
    for bp in breakpoints:
        if bp["category"] == category:
            return float(bp["conc_range"][0])
    raise ValueError(f"Unknown AQI category {category!r} for pollutant {pollutant.value}")


def get_health_threshold_concentration(pollutant: Pollutant, thresholds: dict | None = None) -> float:
    thresholds = thresholds or load_thresholds()
    category = thresholds["health_threshold_category"]
    return get_category_lower_bound(thresholds, pollutant, category)


def get_aqi_category(thresholds: dict, pollutant: Pollutant, concentration: float) -> str:
    breakpoints = thresholds["pollutants"][pollutant.value]["breakpoints"]
    for bp in breakpoints:
        lo, hi = bp["conc_range"]
        if lo <= concentration <= hi:
            return bp["category"]
    return breakpoints[-1]["category"] if concentration > breakpoints[-1]["conc_range"][1] else breakpoints[0]["category"]


def probability_from_quantiles(quantile_predictions: dict[float, float], threshold: float) -> float:
    """Interpolate P(value > threshold) from a small set of fitted quantiles.

    Treats the quantile curve as a piecewise-linear approximation of the
    forecast's CDF. Outside the fitted quantile range, probability is
    clamped (extrapolating linearly past the ends of a thin quantile set is
    unstable) rather than extended further - the 0.1/0.9 default quantiles
    mean confident 0%/100% calls only happen when the threshold is clearly
    below/above the whole predicted spread.
    """
    if not quantile_predictions:
        raise ValueError("quantile_predictions must not be empty")

    q_sorted = sorted(quantile_predictions.items(), key=lambda kv: kv[0])
    probs = [q for q, _ in q_sorted]
    values = [v for _, v in q_sorted]

    if threshold <= values[0]:
        # Threshold at/below the lowest fitted quantile's value: at least
        # (1 - lowest_prob) chance of exceeding, floor rather than 1.0.
        return 1.0 - probs[0]
    if threshold >= values[-1]:
        return max(0.0, 1.0 - probs[-1])

    # value(prob) is increasing; find the bracketing pair and linearly
    # interpolate prob(threshold), then convert to exceedance probability.
    cdf_at_threshold = float(np.interp(threshold, values, probs))
    return max(0.0, min(1.0, 1.0 - cdf_at_threshold))


def classify_exceedance(probability: float, decision_threshold: float | None = None, thresholds: dict | None = None) -> bool:
    thresholds = thresholds or load_thresholds()
    decision_threshold = (
        decision_threshold
        if decision_threshold is not None
        else thresholds["exceedance_probability_decision_threshold"]
    )
    return probability >= decision_threshold


def daily_aggregate(
    hourly_series: pd.Series,
    method: str = "max",
    school_hours_ist: tuple[str, str] = ("08:00", "16:00"),
) -> pd.Series:
    """Collapse an hourly (UTC, tz-aware index) series into one value per
    IST calendar day - the actual decision unit schools plan against. Naive
    UTC-day bucketing would misassign late-night IST hours to the wrong day.
    """
    if hourly_series.empty:
        return hourly_series

    ist_index = hourly_series.index.tz_convert("Asia/Kolkata")
    df = pd.DataFrame({"value": hourly_series.values, "ist_time": ist_index})
    df["ist_date"] = df["ist_time"].dt.date

    if method == "school_hours_max" or method == "school_hours":
        start_t = time.fromisoformat(school_hours_ist[0])
        end_t = time.fromisoformat(school_hours_ist[1])
        df = df[df["ist_time"].dt.time.between(start_t, end_t)]
        agg = "max"
    elif method in ("max", "mean"):
        agg = method
    else:
        raise ValueError(f"Unknown daily_exceedance.aggregation method: {method}")

    grouped = df.groupby("ist_date")["value"].agg(agg)
    return grouped
