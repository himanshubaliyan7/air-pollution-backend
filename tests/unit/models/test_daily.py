import numpy as np
import pandas as pd
import pytest

from common.constants import Pollutant
from models.daily import (
    category_for_day,
    day_ahead_labels,
    local_day_means,
    monotone,
    verdict_for_category,
)
from models.exceedance import load_thresholds

IST = "Asia/Kolkata"


def _hourly(start: str, values) -> pd.Series:
    return pd.Series(list(values), index=pd.date_range(start, periods=len(values), freq="h", tz="UTC"), dtype="float64")


def test_day_means_use_the_local_calendar_day_not_the_utc_one():
    # 2026-01-10 00:00 IST is 2026-01-09 18:30 UTC, so the first full hour of the 10th is 19:00 UTC on the 9th.
    series = _hourly("2026-01-09 19:00", [100.0] * 24 + [200.0] * 24)
    means = local_day_means(series, IST)
    assert means.to_dict() == {pd.Timestamp("2026-01-10"): 100.0, pd.Timestamp("2026-01-11"): 200.0}


def test_a_day_with_too_few_hours_has_no_mean():
    series = _hourly("2026-01-09 19:00", [100.0] * 24 + [200.0] * 17)
    assert list(local_day_means(series, IST).index) == [pd.Timestamp("2026-01-10")]
    assert local_day_means(pd.Series(dtype="float64"), IST).empty


def test_labels_are_the_mean_of_the_day_k_days_after_the_as_of_day():
    means = pd.Series({pd.Timestamp("2026-01-11"): 150.0, pd.Timestamp("2026-01-12"): 90.0})
    as_of = pd.DatetimeIndex(["2026-01-10 02:00", "2026-01-10 18:00", "2026-01-10 19:00"], tz="UTC")
    # 18:00 UTC is 23:30 IST on the 10th; 19:00 UTC is already the 11th in Delhi.
    assert day_ahead_labels(as_of, means, IST, 1).tolist() == [150.0, 150.0, 90.0]
    labels = day_ahead_labels(as_of, means, IST, 2)
    assert labels[:2].tolist() == [90.0, 90.0] and np.isnan(labels[2])


def test_crossed_quantiles_are_put_back_in_order():
    assert monotone({0.1: 80.0, 0.5: 60.0, 0.9: 120.0}) == {0.1: 60.0, 0.5: 80.0, 0.9: 120.0}


@pytest.mark.parametrize("quantiles, decision, category", [
    ({0.1: 20.0, 0.5: 40.0, 0.9: 55.0}, 0.5, "satisfactory"),
    ({0.1: 60.0, 0.5: 100.0, 0.9: 130.0}, 0.5, "poor"),
    ({0.1: 60.0, 0.5: 100.0, 0.9: 130.0}, 0.2, "very_poor"),  # a cautious rule grades the same day higher
    ({0.1: 130.0, 0.5: 180.0, 0.9: 240.0}, 0.5, "very_poor"),
    ({0.1: 260.0, 0.5: 300.0, 0.9: 400.0}, 0.5, "severe"),
    ({0.1: 300.0, 0.5: 180.0, 0.9: 240.0}, 0.5, "very_poor"),  # crossed quantiles
])
def test_category_is_the_worst_one_reached_with_the_decision_probability(quantiles, decision, category):
    thresholds = {**load_thresholds(), "exceedance_probability_decision_threshold": decision}
    assert category_for_day(quantiles, thresholds, Pollutant.PM25) == category


def test_verdict_is_go_below_poor_caution_for_poor_and_no_go_from_very_poor():
    thresholds = load_thresholds()
    verdicts = {c: verdict_for_category(c, thresholds, Pollutant.PM25)
                for c in ("good", "satisfactory", "moderate", "poor", "very_poor", "severe")}
    assert verdicts == {"good": "go", "satisfactory": "go", "moderate": "go",
                        "poor": "caution", "very_poor": "no-go", "severe": "no-go"}
