"""forecast_anchor decides which hour a station's forecast is made from.
Regression: anchoring on "now" made every real-time forecast come back empty,
because OpenAQ data lags (most Delhi NCR stations trail by days, the rest by
~2h) and the feature frame is all-NaN for an hour with no observation.
"""

from datetime import datetime, timezone

from common.constants import MAX_INPUT_STALENESS_HOURS
from orchestration.plugins.common.tasks import forecast_anchor

NOW = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)


def test_anchors_on_newest_observed_hour_not_now():
    newest = datetime(2026, 9, 20, 13, tzinfo=timezone.utc)
    assert forecast_anchor(newest, NOW) == newest


def test_sub_hour_timestamp_is_floored():
    newest = datetime(2026, 9, 20, 13, 30, tzinfo=timezone.utc)
    assert forecast_anchor(newest, NOW) == datetime(2026, 9, 20, 13, tzinfo=timezone.utc)


def test_never_anchors_in_the_future():
    assert forecast_anchor(datetime(2026, 9, 20, 16, tzinfo=timezone.utc), NOW) == NOW


def test_stale_and_missing_stations_are_skipped():
    assert forecast_anchor(datetime(2026, 9, 17, 13, tzinfo=timezone.utc), NOW) is None
    assert forecast_anchor(None, NOW) is None


def test_staleness_boundary_is_inclusive():
    at_limit = NOW.replace(hour=NOW.hour - MAX_INPUT_STALENESS_HOURS)
    assert forecast_anchor(at_limit, NOW) == at_limit
    assert forecast_anchor(at_limit.replace(hour=at_limit.hour - 1), NOW) is None


def test_generator_and_api_agree_on_freshness_for_non_hour_aligned_clocks():
    """Regression: the generator compared against a floored 'now' and the API
    against the raw clock, so a forecast the generator accepted (run at :45)
    could read as no-data immediately. Both now use common.freshness."""
    from api.routers.forecasts import is_forecast_current

    anchor = datetime(2026, 9, 20, 9, tzinfo=timezone.utc)  # newest reading at 09:00
    for minute in (0, 1, 30, 45, 59):
        now = datetime(2026, 9, 20, 15, minute, tzinfo=timezone.utc)  # 6h of whole hours behind
        assert forecast_anchor(anchor, now) == anchor, minute
        assert is_forecast_current(anchor, now), minute

    for minute in (0, 45):
        now = datetime(2026, 9, 20, 16, minute, tzinfo=timezone.utc)  # 7 whole hours behind
        assert forecast_anchor(anchor, now) is None, minute
        assert not is_forecast_current(anchor, now), minute


def test_sub_hour_reading_and_non_aligned_now():
    newest = datetime(2026, 9, 20, 13, 30, tzinfo=timezone.utc)
    now = datetime(2026, 9, 20, 15, 45, tzinfo=timezone.utc)
    assert forecast_anchor(newest, now) == datetime(2026, 9, 20, 13, tzinfo=timezone.utc)
