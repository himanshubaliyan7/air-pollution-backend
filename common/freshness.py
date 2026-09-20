"""One definition of "fresh enough" shared by forecast generation and the API,
so the two cannot disagree about when a forecast stops being usable.

Both sides compare on whole-hour boundaries: a forecast is anchored on an
observed hour (the `forecast_made_at` value), and is usable while that hour is
at most MAX_INPUT_STALENESS_HOURS behind the current hour.
"""

from datetime import datetime, timedelta

from common.constants import MAX_INPUT_STALENESS_HOURS


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def is_input_fresh(anchor: datetime, now: datetime) -> bool:
    return floor_hour(now) - floor_hour(anchor) <= timedelta(hours=MAX_INPUT_STALENESS_HOURS)
