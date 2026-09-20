"""Cyclical time encodings and Delhi NCR calendar flags.

All bucketing here uses IST calendar days (common.config.ist_calendar_date),
never naive UTC-day splits, since a naive UTC split would misassign
late-night IST hours to the wrong calendar day - this matters both for the
is_diwali_window / is_stubble_season flags and, more importantly, for the
daily exceedance aggregation in models/exceedance.py.
"""

import math
from datetime import date, datetime

import pandas as pd

from common.config import to_ist


def add_cyclical(df: pd.DataFrame, index_is_utc: bool = True) -> pd.DataFrame:
    idx = df.index
    ist_idx = idx.tz_convert("Asia/Kolkata") if index_is_utc else idx

    hour = ist_idx.hour + ist_idx.minute / 60.0
    dow = ist_idx.dayofweek
    doy = ist_idx.dayofyear

    out = pd.DataFrame(index=idx)
    out["hour_sin"] = [math.sin(2 * math.pi * h / 24) for h in hour]
    out["hour_cos"] = [math.cos(2 * math.pi * h / 24) for h in hour]
    out["dow_sin"] = [math.sin(2 * math.pi * d / 7) for d in dow]
    out["dow_cos"] = [math.cos(2 * math.pi * d / 7) for d in dow]
    out["doy_sin"] = [math.sin(2 * math.pi * d / 365.25) for d in doy]
    out["doy_cos"] = [math.cos(2 * math.pi * d / 365.25) for d in doy]
    return out


def _parse_month_day(md: str, year: int) -> date:
    month, day = (int(x) for x in md.split("-"))
    return date(year, month, day)


def add_calendar_flags(df: pd.DataFrame, calendar_config: dict, index_is_utc: bool = True) -> pd.DataFrame:
    idx = df.index
    ist_dates = [to_ist(ts.to_pydatetime()).date() if index_is_utc else ts.date() for ts in idx]

    diwali_dates = {datetime.fromisoformat(d).date() for d in calendar_config.get("diwali_dates", [])}
    diwali_window = {d + pd.Timedelta(days=offset) for d in diwali_dates for offset in range(-2, 3)}

    stubble_cfg = calendar_config.get("stubble_burning_season", {})
    start_md = stubble_cfg.get("start_month_day", "10-01")
    end_md = stubble_cfg.get("end_month_day", "11-30")

    def in_stubble_season(d: date) -> bool:
        start = _parse_month_day(start_md, d.year)
        end = _parse_month_day(end_md, d.year)
        return start <= d <= end

    out = pd.DataFrame(index=idx)
    out["is_diwali_window"] = [d in diwali_window for d in ist_dates]
    out["is_stubble_season"] = [in_stubble_season(d) for d in ist_dates]
    return out
