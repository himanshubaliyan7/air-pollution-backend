import pandas as pd
import pytest

from common.constants import Pollutant
from models.exceedance import (
    classify_exceedance,
    daily_aggregate,
    get_aqi_category,
    get_category_lower_bound,
    get_health_threshold_concentration,
    load_thresholds,
    probability_from_quantiles,
)


@pytest.fixture(scope="module")
def thresholds():
    return load_thresholds()


def test_category_lower_bound_matches_config(thresholds):
    assert get_category_lower_bound(thresholds, Pollutant.PM25, "poor") == 91.0


def test_health_threshold_concentration_uses_configured_category(thresholds):
    conc = get_health_threshold_concentration(Pollutant.PM25, thresholds)
    assert conc == get_category_lower_bound(thresholds, Pollutant.PM25, "poor")


def test_get_aqi_category_roundtrip(thresholds):
    assert get_aqi_category(thresholds, Pollutant.PM25, 50) == "satisfactory"
    assert get_aqi_category(thresholds, Pollutant.PM25, 95) == "poor"


@pytest.mark.parametrize(
    "pollutant, value, expected",
    [
        (Pollutant.PM25, 30.5, "good"),
        (Pollutant.PM25, 60.5, "satisfactory"),
        (Pollutant.PM25, 90.5, "moderate"),
        (Pollutant.PM25, 91.0, "poor"),
        (Pollutant.PM25, 120.5, "poor"),
        (Pollutant.PM25, 250.5, "very_poor"),
        (Pollutant.PM25, 999.0, "severe"),
        (Pollutant.NO2, 180.5, "moderate"),
        (Pollutant.NO2, 400.5, "very_poor"),
        (Pollutant.PM25, -1.0, "good"),
    ],
)
def test_get_aqi_category_between_integer_ranges_never_falls_to_good(thresholds, pollutant, value, expected):
    assert get_aqi_category(thresholds, pollutant, value) == expected


def test_probability_from_quantiles_midpoint_interpolation():
    # median forecast = 100, 10th pct = 60, 90th pct = 140 -> roughly
    # symmetric spread. A threshold exactly at the median should give ~50%.
    preds = {0.1: 60.0, 0.5: 100.0, 0.9: 140.0}
    prob = probability_from_quantiles(preds, threshold=100.0)
    assert abs(prob - 0.5) < 1e-9


def test_probability_from_quantiles_below_range_is_high():
    preds = {0.1: 60.0, 0.5: 100.0, 0.9: 140.0}
    prob = probability_from_quantiles(preds, threshold=10.0)
    assert prob == 0.9  # 1 - lowest fitted prob (0.1)


def test_probability_from_quantiles_above_range_is_low():
    preds = {0.1: 60.0, 0.5: 100.0, 0.9: 140.0}
    prob = probability_from_quantiles(preds, threshold=500.0)
    assert prob == pytest.approx(0.1, abs=1e-9)


def test_classify_exceedance_uses_configured_decision_threshold():
    assert classify_exceedance(0.5) is True  # default decision threshold is 0.3
    assert classify_exceedance(0.1) is False


def test_daily_aggregate_buckets_by_ist_calendar_day_not_utc():
    # 2026-01-01 19:00 UTC = 2026-01-02 00:30 IST - must land on the IST day.
    idx = pd.DatetimeIndex(
        ["2026-01-01T18:00:00Z", "2026-01-01T19:00:00Z", "2026-01-01T20:00:00Z"], tz="UTC"
    )
    series = pd.Series([100.0, 200.0, 50.0], index=idx)

    daily_max = daily_aggregate(series, method="max")

    # 18:00 UTC = 23:30 IST (2026-01-01), 19:00/20:00 UTC = 00:30/01:30 IST (2026-01-02).
    import datetime as dt

    assert daily_max[dt.date(2026, 1, 1)] == 100.0
    assert daily_max[dt.date(2026, 1, 2)] == 200.0
