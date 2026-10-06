from datetime import datetime, timedelta, timezone

from orchestration.plugins.common.watchdog import RegionHealth, all_issue_keys, evaluate_health, min_stations

NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
H = timedelta(hours=1)


def _region(rid="delhi-ncr", name="Delhi NCR", **over):
    args = dict(region_id=rid, name=name, active_stations=82, newest_snapshot=NOW - H, stations_with_fresh_snapshot=70,
                newest_sensor_reading=NOW - H, stations_with_current_forecast=73, stations_with_fresh_input=80)
    args.update(over)
    return RegionHealth(**args)


def _check(*regions, enabled=True, **over):
    regions = regions or (_region(**over),)
    return {i.key for i in evaluate_health(NOW, regions=list(regions), sensor_ingestion_enabled=enabled)}


def test_delhi_minimums_stay_what_they_were_before_regions():
    assert min_stations(82, 0.25) == 20 and min_stations(82, 0.37) == 30
    assert (min_stations(80, 0.25), min_stations(80, 0.37)) == (20, 29)  # a station or two off at most
    assert (min_stations(2, 0.25), min_stations(2, 0.37)) == (2, 2)  # never more than the region has
    assert min_stations(10, 0.25) == 3  # the floor


def test_healthy_system_reports_nothing():
    assert _check() == set()


def test_stale_or_missing_aqi_feed_is_reported():
    assert _check(newest_snapshot=NOW - 4 * H) == {"aqi-feed-stale"}
    assert _check(newest_snapshot=None, stations_with_fresh_snapshot=0) == {"aqi-feed-stale"}
    assert _check(newest_snapshot=NOW - 3 * H) == set()  # boundary: exactly 3h is still fine


def test_collapsed_coverage_is_reported_even_when_the_feed_is_fresh():
    assert _check(stations_with_fresh_snapshot=5) == {"aqi-coverage-low"}
    assert _check(stations_with_fresh_snapshot=19) == {"aqi-coverage-low"}
    assert _check(stations_with_fresh_snapshot=20) == set()  # boundary: Delhi's old fixed minimum


def test_silent_sensor_ingestion_is_reported_but_not_while_deliberately_paused():
    """Regression: OpenAQ returned 401 for 6h+ while every run reported success."""
    assert _check(newest_sensor_reading=NOW - 30 * H) == {"sensor-data-stale"}
    assert _check(newest_sensor_reading=None) == {"sensor-data-stale"}
    assert _check(newest_sensor_reading=NOW - 30 * H, enabled=False, stations_with_current_forecast=0) == set()


def test_collapsed_forecast_coverage_is_reported_even_when_some_stations_stay_fresh():
    """Regression: CPCB went silent 2026-09-25 while 7 non-CPCB stations kept the newest reading fresh."""
    issues = evaluate_health(NOW, sensor_ingestion_enabled=True, regions=[
        _region(stations_with_current_forecast=7, stations_with_fresh_input=7)])
    assert [i.key for i in issues] == ["forecast-coverage-low"]
    assert "upstream" in issues[0].message
    assert _check(stations_with_current_forecast=30) == set()  # boundary: Delhi's old fixed minimum
    assert _check(stations_with_current_forecast=29) == {"forecast-coverage-low"}


def test_forecast_coverage_message_blames_the_pipeline_when_inputs_are_fresh():
    issues = evaluate_health(NOW, sensor_ingestion_enabled=True, regions=[
        _region(stations_with_current_forecast=2, stations_with_fresh_input=80)])
    assert "pipeline" in issues[0].message


MUMBAI = dict(rid="mumbai", name="Mumbai", active_stations=20, stations_with_fresh_snapshot=15,
              stations_with_current_forecast=14, stations_with_fresh_input=18)


def test_one_dark_region_is_reported_while_the_other_is_healthy():
    """The point of per-region rules: Delhi's fresh data must not hide a dead Mumbai."""
    dead = dict(MUMBAI, newest_snapshot=None, stations_with_fresh_snapshot=0, newest_sensor_reading=None,
                stations_with_current_forecast=0, stations_with_fresh_input=0)
    assert _check(_region(), _region(**dead)) == {
        "aqi-feed-stale:mumbai", "sensor-data-stale:mumbai", "forecast-coverage-low:mumbai"}
    assert _check(_region(newest_snapshot=None), _region(**MUMBAI)) == {"aqi-feed-stale"}  # Delhi keeps the bare key


def test_region_with_no_active_stations_raises_nothing():
    empty = dict(MUMBAI, active_stations=0, newest_snapshot=None, stations_with_fresh_snapshot=0,
                 newest_sensor_reading=None, stations_with_current_forecast=0, stations_with_fresh_input=0)
    assert _check(_region(), _region(**empty)) == set()


def test_minimums_scale_with_the_regions_active_stations():
    # Mumbai with 20 active: min 5 current AQI, 7 forecast (floor(0.25*20), floor(0.37*20)).
    assert _check(_region(), _region(**dict(MUMBAI, stations_with_fresh_snapshot=5, stations_with_current_forecast=7))) == set()
    assert _check(_region(), _region(**dict(MUMBAI, stations_with_fresh_snapshot=4))) == {"aqi-coverage-low:mumbai"}
    assert _check(_region(), _region(**dict(MUMBAI, stations_with_current_forecast=6))) == {"forecast-coverage-low:mumbai"}


def test_messages_name_the_region_and_the_counts():
    issues = evaluate_health(NOW, sensor_ingestion_enabled=True, regions=[_region(**dict(MUMBAI, stations_with_fresh_snapshot=2))])
    assert issues[0].key == "aqi-coverage-low" and issues[0].message.startswith("Mumbai:")  # sole region is the default
    assert "2 of 20" in issues[0].message


def test_resolved_keys_are_derived_from_the_regions_default_first():
    keys = all_issue_keys(["delhi-ncr", "mumbai"])
    assert keys[:4] == ["aqi-feed-stale", "aqi-coverage-low", "sensor-data-stale", "forecast-coverage-low"]
    assert keys[4:] == [k + ":mumbai" for k in keys[:4]]
