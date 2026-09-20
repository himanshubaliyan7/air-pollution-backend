import pandas as pd

from features.lag_features import compute_lags, compute_rolling, reindex_hourly


def _hourly_series(values, start="2026-01-01T00:00:00Z"):
    idx = pd.date_range(start, periods=len(values), freq="h", tz="UTC")
    return pd.Series(values, index=idx)


def test_compute_lags_shifts_by_wall_clock_hours():
    series = _hourly_series([10.0, 20.0, 30.0, 40.0, 50.0])
    lags = compute_lags(series, lags=[1, 2])

    # At t=4 (value 50), lag_1h should be the t=3 value (40), lag_2h the t=2 value (30).
    assert lags["lag_1h"].iloc[4] == 40.0
    assert lags["lag_2h"].iloc[4] == 30.0
    # First row has no history.
    assert pd.isna(lags["lag_1h"].iloc[0])


def test_compute_rolling_mean_matches_hand_calculation():
    series = _hourly_series([10.0, 20.0, 30.0, 40.0])
    rolling = compute_rolling(series, windows=[2], stats=["mean"])

    # rolling_mean_2h at t=1 (value 20) = mean(10, 20) = 15.
    assert rolling["rolling_mean_2h"].iloc[1] == 15.0
    # at t=3 (value 40) = mean(30, 40) = 35.
    assert rolling["rolling_mean_2h"].iloc[3] == 35.0


def test_reindex_hourly_fills_gaps_with_nan_so_lag_is_wall_clock_not_row_count():
    idx = pd.DatetimeIndex(
        ["2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "2026-01-01T04:00:00Z"], tz="UTC"
    )
    series = pd.Series([1.0, 2.0, 3.0], index=idx)

    reindexed = reindex_hourly(series)

    assert len(reindexed) == 5  # 00:00..04:00 inclusive, gaps filled
    assert pd.isna(reindexed.iloc[2])  # 02:00 has no data
    assert pd.isna(reindexed.iloc[3])  # 03:00 has no data

    lags = compute_lags(reindexed, lags=[1])
    # lag_1h at 04:00 must be NaN (03:00 had no reading), not silently equal
    # to the 01:00 value that a row-count-based shift would produce.
    assert pd.isna(lags["lag_1h"].iloc[4])
