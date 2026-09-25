"""forecast_anchor decides which hour a station's forecast is made from.
Regression: anchoring on "now" made every real-time forecast come back empty,
because OpenAQ data lags (most Delhi NCR stations trail by days, the rest by
~2h) and the feature frame is all-NaN for an hour with no observation.
"""

from datetime import datetime, timedelta, timezone

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
    at_limit = NOW - timedelta(hours=MAX_INPUT_STALENESS_HOURS)
    assert forecast_anchor(at_limit, NOW) == at_limit
    assert forecast_anchor(at_limit - timedelta(hours=1), NOW) is None


def test_typical_12h_cpcb_lag_is_forecastable():
    # CPCB stations reach OpenAQ ~12 h late; they must not all read as no-data.
    newest = NOW - timedelta(hours=12)
    assert forecast_anchor(newest, NOW) == newest


def test_generator_and_api_agree_on_freshness_for_non_hour_aligned_clocks():
    """Regression: the generator compared against a floored 'now' and the API
    against the raw clock, so a forecast the generator accepted (run at :45)
    could read as no-data immediately. Both now use common.freshness."""
    from api.routers.forecasts import is_forecast_current

    anchor = datetime(2026, 9, 20, 9, tzinfo=timezone.utc)  # newest reading at 09:00
    limit = anchor + timedelta(hours=MAX_INPUT_STALENESS_HOURS)
    for minute in (0, 1, 30, 45, 59):
        now = limit + timedelta(minutes=minute)  # exactly the limit in whole hours behind
        assert forecast_anchor(anchor, now) == anchor, minute
        assert is_forecast_current(anchor, now), minute

    for minute in (0, 45):
        now = limit + timedelta(hours=1, minutes=minute)  # one whole hour past the limit
        assert forecast_anchor(anchor, now) is None, minute
        assert not is_forecast_current(anchor, now), minute


def test_sub_hour_reading_and_non_aligned_now():
    newest = datetime(2026, 9, 20, 13, 30, tzinfo=timezone.utc)
    now = datetime(2026, 9, 20, 15, 45, tzinfo=timezone.utc)
    assert forecast_anchor(newest, now) == datetime(2026, 9, 20, 13, tzinfo=timezone.utc)


def test_current_conditions_stay_stricter_than_forecast_inputs():
    # Regression: current_aqi reused the forecast freshness rule, so raising the
    # forecast-input limit would have served day-old CPCB readings as "now".
    from common.freshness import is_input_fresh, is_reading_current

    twenty_hours_old = NOW - timedelta(hours=20)
    assert is_input_fresh(twenty_hours_old, NOW)
    assert not is_reading_current(twenty_hours_old, NOW)
    assert is_reading_current(NOW - timedelta(hours=2), NOW)
    assert not is_reading_current(None, NOW)
