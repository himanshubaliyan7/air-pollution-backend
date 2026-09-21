from datetime import datetime, timedelta, timezone

from orchestration.plugins.common.watchdog import evaluate_health

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
H = timedelta(hours=1)


def _check(**over):
    args = dict(aqi_feed_enabled=True, newest_snapshot=NOW - H, stations_with_fresh_snapshot=70,
                sensor_ingestion_enabled=True, newest_sensor_reading=NOW - H)
    args.update(over)
    return {i.key for i in evaluate_health(NOW, **args)}


def test_healthy_system_reports_nothing():
    assert _check() == set()


def test_stale_or_missing_aqi_feed_is_reported():
    assert _check(newest_snapshot=NOW - 4 * H) == {"aqi-feed-stale"}
    assert _check(newest_snapshot=None, stations_with_fresh_snapshot=0) == {"aqi-feed-stale"}
    assert _check(newest_snapshot=NOW - 3 * H) == set()  # boundary: exactly 3h is still fine


def test_collapsed_coverage_is_reported_even_when_the_feed_is_fresh():
    assert _check(stations_with_fresh_snapshot=5) == {"aqi-coverage-low"}


def test_aqi_checks_are_skipped_when_the_feed_is_not_configured():
    assert _check(aqi_feed_enabled=False, newest_snapshot=None, stations_with_fresh_snapshot=0) == set()


def test_silent_sensor_ingestion_is_reported_but_not_while_deliberately_paused():
    """Regression: OpenAQ returned 401 for 6h+ while every run reported success."""
    assert _check(newest_sensor_reading=NOW - 30 * H) == {"sensor-data-stale"}
    assert _check(newest_sensor_reading=None) == {"sensor-data-stale"}
    assert _check(newest_sensor_reading=NOW - 30 * H, sensor_ingestion_enabled=False) == set()
