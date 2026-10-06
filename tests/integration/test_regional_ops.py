"""Scheduled jobs with one station in each region (Delhi NCR and Mumbai): each region is
judged on its own, so one dark city is not hidden by the other."""

import logging
from datetime import datetime, timedelta, timezone

import pytest

from common.constants import Pollutant, SensorSourceName
from db.models import ExceedanceEvaluation, ModelRun, Station, StationAqiSnapshot
from orchestration.plugins.common import tasks
from orchestration.plugins.common.region_health import collect_region_health
from orchestration.plugins.common.watchdog import evaluate_health
from tests.integration.forecast_helpers import add_model_run, add_reading, add_station, seed_forecast_run
from tests.integration.region_fixture import DELHI_LAT, DELHI_LON, MUMBAI_LAT, MUMBAI_LON, two_regions  # noqa: F401

NOW = datetime.now(timezone.utc)


def _two(db):
    d = add_station(db, "delhi", lat=DELHI_LAT, lon=DELHI_LON)
    m = add_station(db, "mumbai", lat=MUMBAI_LAT, lon=MUMBAI_LON)
    return d, m


class _Source:
    def __init__(self, by_bbox):
        self.by_bbox, self.calls = by_bbox, []

    def __call__(self, api_key):
        return self

    def location_last_data_times(self, *, bbox, country):
        self.calls.append((bbox, country))
        return self.by_bbox.get(bbox[0], {})


def _patch(monkeypatch, src):
    monkeypatch.setitem(tasks.SENSOR_SOURCE_REGISTRY, tasks.ACTIVE_SOURCE, src)


def test_station_activity_queries_each_region_and_deactivates_mumbai_stations(db_session, monkeypatch, two_regions):
    _two(db_session)
    src = _Source({76.6: {"delhi": NOW}, 72.70: {"mumbai": NOW - timedelta(days=30)}})
    _patch(monkeypatch, src)

    assert tasks.refresh_station_activity() == {"deactivated": 1, "reactivated": 0}
    assert sorted(b[0] for b, _ in src.calls) == [72.70, 76.6]
    db_session.expire_all()
    assert {s.source_location_id: s.is_active for s in db_session.query(Station)} == {"delhi": True, "mumbai": False}


def test_one_regions_empty_answer_leaves_its_stations_alone_but_not_the_other_region(db_session, monkeypatch, two_regions):
    d, m = _two(db_session)
    _patch(monkeypatch, _Source({76.6: {"delhi": NOW - timedelta(days=30)}}))  # Mumbai: empty answer

    with pytest.raises(RuntimeError, match="mumbai"):
        tasks.refresh_station_activity()
    db_session.expire_all()
    # Delhi's answer was applied, Mumbai's stations were not touched.
    assert {s.source_location_id: s.is_active for s in db_session.query(Station)} == {"delhi": False, "mumbai": True}


def test_watchdog_inputs_are_per_region_so_a_dark_mumbai_is_reported(db_session, two_regions):
    d, m = _two(db_session)
    add_reading(db_session, d, NOW - timedelta(hours=1))
    db_session.add(StationAqiSnapshot(station_id=d, pollutant_id="PM2.5", source_updated_at=NOW - timedelta(hours=1),
                                      fetched_at=NOW))
    db_session.commit()
    seed_forecast_run(db_session, d, NOW.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1))

    health = collect_region_health(db_session, NOW, two_regions)
    by_id = {h.region_id: h for h in health}
    assert by_id["delhi-ncr"].active_stations == 1 and by_id["mumbai"].active_stations == 1
    assert by_id["delhi-ncr"].stations_with_fresh_snapshot == 1 and by_id["mumbai"].newest_snapshot is None
    assert by_id["mumbai"].newest_sensor_reading is None and by_id["mumbai"].stations_with_current_forecast == 0
    assert by_id["delhi-ncr"].has_forecast_models is True and by_id["mumbai"].has_forecast_models is False
    keys = {i.key for i in evaluate_health(NOW, regions=health, sensor_ingestion_enabled=True)}
    # Mumbai has never been fitted: no forecasts to be missing, its other rules still apply.
    assert keys == {"aqi-feed-stale:mumbai", "sensor-data-stale:mumbai"}

    add_model_run(db_session, m)  # once Mumbai has a model, missing forecasts are a fault there too
    health = collect_region_health(db_session, NOW, two_regions)
    assert {h.region_id: h.has_forecast_models for h in health} == {"delhi-ncr": True, "mumbai": True}
    keys = {i.key for i in evaluate_health(NOW, regions=health, sensor_ingestion_enabled=True)}
    assert keys == {"aqi-feed-stale:mumbai", "sensor-data-stale:mumbai", "forecast-coverage-low:mumbai"}


def test_quality_check_warns_for_the_dark_region_by_name(db_session, caplog, two_regions):
    d, m = _two(db_session)
    add_reading(db_session, d, NOW - timedelta(hours=1))
    with caplog.at_level(logging.WARNING, logger=tasks.logger.name):
        tasks.ingestion_data_quality_check(sensor_rows=5, weather_rows=5)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and warnings[0].startswith("Mumbai:")


def _evaluation(db, station_id, model_id, recent, flagged):
    db.add(ExceedanceEvaluation(
        station_id=station_id, pollutant=Pollutant.PM25, horizon_hours=24, model_id=model_id,
        evaluation_window_start=recent - timedelta(days=1), evaluation_window_end=recent,
        recall=float(flagged), mae=1.0, rmse=1.0, n_exceedance_days_actual=1,
        n_exceedance_days_predicted=int(flagged), computed_at=recent,
    ))


def test_daily_drift_is_judged_per_region_and_names_it(db_session, two_regions):
    d, m = _two(db_session)
    models = {}
    for sid in (d, m):
        models[sid] = add_model_run(db_session, sid)
        db_session.query(ModelRun).filter_by(model_id=models[sid]).update({"target": "daily_mean"})
    recent = NOW - timedelta(days=1)
    for _ in range(25):  # Delhi flags every bad day, Mumbai none: pooled recall would be 50%, no alarm
        _evaluation(db_session, d, models[d], recent, True)
        _evaluation(db_session, m, models[m], recent, False)
    db_session.commit()

    message = tasks.daily_verdict_drift(db_session, NOW)
    assert message is not None and "Mumbai: only 0% of the 25" in message and "Delhi" not in message


def test_forecasts_use_the_thresholds_of_the_stations_region(db_session, monkeypatch, two_regions):
    from common.regions import Region
    from models import predict
    from tests.integration.forecast_helpers import FakePredict

    d, m = _two(db_session)
    for sid in (d, m):
        add_reading(db_session, sid, NOW - timedelta(hours=1))
    fake = FakePredict(model_ids={(d, Pollutant.PM25): add_model_run(db_session, d),
                                  (m, Pollutant.PM25): add_model_run(db_session, m)})
    seen = {}

    def spy(session, station_id, pollutant, horizon, as_of, **kw):
        seen[station_id] = kw["thresholds"]
        return fake(session, station_id, pollutant, horizon, as_of)

    monkeypatch.setattr(predict, "forecast", spy)
    monkeypatch.setattr(Region, "thresholds", lambda self: {"region": self.id})  # stands in for each region's file
    tasks.generate_forecasts()
    assert seen == {d: {"region": "delhi-ncr"}, m: {"region": "mumbai"}}
