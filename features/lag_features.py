"""Pure functions: lag and rolling-window features from a per-station time series.

No I/O here - these operate on an already-loaded pandas DataFrame so the
exact same code runs in both the batch feature-engineering DAG and
real-time inference (see build_features.py), which is what keeps train and
serve from ever computing lags differently.
"""

import pandas as pd


def compute_lags(series: pd.Series, lags: list[int]) -> pd.DataFrame:
    """series must be indexed by an hourly, UTC, tz-aware DatetimeIndex with
    no gaps (reindex to a full hourly range with NaN for missing hours
    before calling, so a lag reflects wall-clock hours-ago, not row count)."""
    return pd.DataFrame({f"lag_{h}h": series.shift(h) for h in lags}, index=series.index)


def compute_rolling(series: pd.Series, windows: list[int], stats: list[str] = ("mean", "std")) -> pd.DataFrame:
    out = {}
    for w in windows:
        rolling = series.rolling(window=w, min_periods=max(1, w // 2))
        if "mean" in stats:
            out[f"rolling_mean_{w}h"] = rolling.mean()
        if "std" in stats:
            out[f"rolling_std_{w}h"] = rolling.std()
    return pd.DataFrame(out, index=series.index)


def reindex_hourly(series: pd.Series) -> pd.Series:
    """Fill gaps in an hourly series with NaN so lag/rolling windows measure
    wall-clock time rather than row count - a station outage must not
    silently compress a 6-hour gap into a 1-row shift."""
    if series.empty:
        return series
    full_index = pd.date_range(series.index.min(), series.index.max(), freq="h", tz="UTC")
    return series.reindex(full_index)
