"""tasks.generate_forecasts: anchoring, staleness skipping, idempotent upsert
and new-crossing detection, against the real (test) database.

Most tests replace models.predict.forecast with a controllable fake so flags and
values can be flipped between runs without training anything; the last tests
train real (tiny) LightGBM models end to end. The clock is frozen at a
non-hour-aligned 15:45 UTC so nothing depends on the wall clock.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from common.config import get_settings
from common.constants import DEFAULT_HORIZONS_HOURS, MAX_INPUT_STALENESS_HOURS, Pollutant
from db.models import Forecast
from models import predict
from orchestration.plugins.common import tasks
from tests.clock import fixed_datetime
from tests.integration.forecast_helpers import (
    NOW,
    NOW_HOUR,
    FakePredict,
    add_model_run,
    add_reading,
    add_station,
)

UTC = timezone.utc
N_H = len(DEFAULT_HORIZONS_HOURS)


@pytest.fixture()
def clock(monkeypatch):
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(NOW))
    return NOW


def _setup(db, monkeypatch, readings, *, pollutants=(Pollutant.PM25,), **fake_kwargs):
    """readings: {station_name: newest_reading_datetime_or_None}. Each named
    station gets a model_runs row per pollutant and (if given) one PM2.5 reading."""
    model_ids = {}
    for name, newest in readings.items():
        sid = add_station(db, name)
        for pol in pollutants:
            model_ids[(sid, pol)] = add_model_run(db, sid, pol)
        if newest is not None:
            add_reading(db, sid, newest)
    fake = FakePredict(model_ids=model_ids, **fake_kwargs)
    monkeypatch.setattr(predict, "forecast", fake)
    return fake


def _rows(db):
    db.expire_all()
    return db.execute(select(Forecast).order_by(Forecast.station_id, Forecast.target_time)).scalars().all()


def _count(db):
    db.expire_all()
    return db.execute(select(func.count()).select_from(Forecast)).scalar_one()


# ------------------------------------------------------------ upsert idempotence

def test_second_run_at_same_anchor_upserts_instead_of_duplicating_or_failing(db_session, monkeypatch, clock):
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR - timedelta(hours=2)})

    first = tasks.generate_forecasts()
    assert first["forecasts_written"] == N_H
    assert _count(db_session) == N_H
    before = {r.horizon_hours: r.point_forecast for r in _rows(db_session)}

    fake.point = 500.0  # a re-run at the same anchor produces different numbers
    second = tasks.generate_forecasts()

    assert second["forecasts_written"] == N_H
    assert _count(db_session) == N_H  # same key set -> updated in place
    after = {r.horizon_hours: r.point_forecast for r in _rows(db_session)}
    assert after != before
    assert after == {h: 500.0 + h for h in DEFAULT_HORIZONS_HOURS}


def test_upsert_updates_every_value_column_but_not_the_key(db_session, monkeypatch, clock):
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR}, flag=False)
    tasks.generate_forecasts()
    fake.flag, fake.point = True, 300.0
    tasks.generate_forecasts()

    row = next(r for r in _rows(db_session) if r.horizon_hours == 24)
    assert (row.point_forecast, row.quantile_low, row.quantile_high) == (324.0, 314.0, 354.0)
    assert row.exceedance_probability == 0.9 and row.exceedance_flag is True
    assert row.forecast_made_at == NOW_HOUR


def test_advancing_anchor_adds_a_new_run_and_keeps_the_old_one(db_session, monkeypatch, clock):
    _setup(db_session, monkeypatch, {"a": NOW_HOUR - timedelta(hours=1)})
    tasks.generate_forecasts()
    add_reading(db_session, "openaq:a", NOW_HOUR)  # feed caught up by an hour
    tasks.generate_forecasts()

    assert _count(db_session) == 2 * N_H
    assert {r.forecast_made_at for r in _rows(db_session)} == {NOW_HOUR - timedelta(hours=1), NOW_HOUR}


# ------------------------------------------------------------- new crossings

def test_first_ever_flagged_forecast_is_a_crossing_per_horizon_and_is_json_safe(db_session, monkeypatch, clock):
    _setup(db_session, monkeypatch, {"a": NOW_HOUR}, flag=True)
    result = tasks.generate_forecasts()

    crossings = result["new_crossings"]
    assert len(crossings) == N_H
    assert json.loads(json.dumps(crossings)) == crossings  # survives an Airflow XCom hop
    assert {c["target_time"] for c in crossings} == {(NOW_HOUR + timedelta(hours=h)).isoformat() for h in DEFAULT_HORIZONS_HOURS}
    c = crossings[0]
    assert c["station_id"] == "openaq:a" and c["station_name"] == "Station a" and c["pollutant"] == "pm25"
    assert c["forecast_made_at"] == NOW_HOUR.isoformat()


def test_rerun_of_already_flagged_forecast_reports_no_duplicate_crossing(db_session, monkeypatch, clock):
    _setup(db_session, monkeypatch, {"a": NOW_HOUR}, flag=True)
    assert len(tasks.generate_forecasts()["new_crossings"]) == N_H
    assert tasks.generate_forecasts()["new_crossings"] == []
    assert tasks.generate_forecasts()["new_crossings"] == []
    assert _count(db_session) == N_H


def test_false_to_true_flip_is_reported_exactly_once(db_session, monkeypatch, clock):
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR}, flag=False)
    assert tasks.generate_forecasts()["new_crossings"] == []

    fake.flag = True
    flip = tasks.generate_forecasts()["new_crossings"]
    assert len(flip) == N_H
    assert all(c["forecast_made_at"] == NOW_HOUR.isoformat() for c in flip)

    assert tasks.generate_forecasts()["new_crossings"] == []  # already flagged now
    assert all(r.exceedance_flag for r in _rows(db_session))


def test_only_the_horizons_that_flip_are_reported(db_session, monkeypatch, clock):
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR}, flag=False)
    tasks.generate_forecasts()
    fake.flag_by_horizon = {72: True, 120: True}
    crossings = tasks.generate_forecasts()["new_crossings"]
    assert sorted(c["target_time"] for c in crossings) == [
        (NOW_HOUR + timedelta(hours=72)).isoformat(),
        (NOW_HOUR + timedelta(hours=120)).isoformat(),
    ]


def test_true_to_false_never_reports_a_crossing_and_is_recorded(db_session, monkeypatch, clock):
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR}, flag=True)
    tasks.generate_forecasts()
    fake.flag = False
    assert tasks.generate_forecasts()["new_crossings"] == []
    assert not any(r.exceedance_flag for r in _rows(db_session))


def test_flap_true_false_true_reports_the_second_rise(db_session, monkeypatch, clock):
    """Characterisation: crossings are edge-triggered on the latest stored flag,
    so a flag that drops and rises again is reported again (the email layer
    de-duplicates on alert_log - see test_alerts.py)."""
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR}, flag=True)
    assert len(tasks.generate_forecasts()["new_crossings"]) == N_H
    fake.flag = False
    tasks.generate_forecasts()
    fake.flag = True
    assert len(tasks.generate_forecasts()["new_crossings"]) == N_H


def test_flag_from_an_earlier_run_for_the_same_target_hour_suppresses_a_new_crossing(db_session, monkeypatch):
    """Yesterday's 48h forecast already flagged today's 24h target hour, so the
    later run's 24h forecast for that hour is not a 'new' crossing."""
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(NOW - timedelta(hours=24)))
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR - timedelta(hours=24)}, flag_by_horizon={48: True})
    first = tasks.generate_forecasts()
    assert [c["target_time"] for c in first["new_crossings"]] == [(NOW_HOUR + timedelta(hours=24)).isoformat()]

    monkeypatch.setattr(tasks, "datetime", fixed_datetime(NOW))
    add_reading(db_session, "openaq:a", NOW_HOUR)
    fake.flag_by_horizon = {24: True, 48: True}  # the 24h forecast targets the same hour again
    second = tasks.generate_forecasts()

    targets = [c["target_time"] for c in second["new_crossings"]]
    assert (NOW_HOUR + timedelta(hours=24)).isoformat() not in targets  # already flagged by yesterday's run
    assert targets == [(NOW_HOUR + timedelta(hours=48)).isoformat()]  # only the genuinely new target hour


# ---------------------------------------------------------- staleness counting

def test_skipped_stale_counts_station_pollutant_pairs_with_inclusive_boundary(db_session, monkeypatch, clock):
    readings = {
        "fresh": NOW_HOUR,  # 0h behind
        "edge": NOW_HOUR - timedelta(hours=MAX_INPUT_STALENESS_HOURS),  # exactly at the limit: still fresh
        "stale": NOW_HOUR - timedelta(hours=MAX_INPUT_STALENESS_HOURS + 1),
        "never": None,
    }
    _setup(db_session, monkeypatch, readings)
    # A station that is not active must not be counted or forecast at all.
    add_station(db_session, "inactive", active=False)
    add_reading(db_session, "openaq:inactive", NOW_HOUR)

    result = tasks.generate_forecasts()

    # PM2.5: fresh + edge are forecast (5 horizons each); stale + never skipped.
    # NO2: no station has any NO2 reading -> all 4 active stations skipped.
    assert result["forecasts_written"] == 2 * N_H
    assert result["skipped_stale"] == 2 + 4
    assert {r.station_id for r in _rows(db_session)} == {"openaq:fresh", "openaq:edge"}


def test_stale_and_fresh_stations_in_one_run(db_session, monkeypatch, clock):
    fake = _setup(
        db_session, monkeypatch,
        {"fresh": NOW_HOUR - timedelta(hours=2), "stale": NOW_HOUR - timedelta(days=3)},
    )
    result = tasks.generate_forecasts()

    assert result["forecasts_written"] == N_H
    assert {c[0] for c in fake.calls} == {"openaq:fresh"}  # the model is never even asked about the stale one
    rows = _rows(db_session)
    assert {r.station_id for r in rows} == {"openaq:fresh"}
    assert {r.forecast_made_at for r in rows} == {NOW_HOUR - timedelta(hours=2)}


def test_all_stale_writes_nothing_and_does_not_raise(db_session, monkeypatch, clock):
    _setup(db_session, monkeypatch, {"a": NOW_HOUR - timedelta(days=9), "b": None})
    result = tasks.generate_forecasts()
    assert result == {"forecasts_written": 0, "skipped_stale": 4, "new_crossings": []}
    assert _count(db_session) == 0


def test_staleness_is_judged_per_pollutant(db_session, monkeypatch, clock):
    sid = add_station(db_session, "a")
    ids = {(sid, p): add_model_run(db_session, sid, p) for p in Pollutant}
    add_reading(db_session, sid, NOW_HOUR - timedelta(hours=1), Pollutant.PM25)
    add_reading(db_session, sid, NOW_HOUR - timedelta(days=2), Pollutant.NO2)  # NO2 sensor went quiet
    monkeypatch.setattr(predict, "forecast", FakePredict(model_ids=ids))

    result = tasks.generate_forecasts()
    assert result["skipped_stale"] == 1 and result["forecasts_written"] == N_H
    assert {r.pollutant for r in _rows(db_session)} == {Pollutant.PM25}


def test_only_newest_reading_matters_for_freshness(db_session, monkeypatch, clock):
    """A station with a long history but a stale newest reading is skipped."""
    sid = add_station(db_session, "a")
    ids = {(sid, Pollutant.PM25): add_model_run(db_session, sid)}
    for h in range(1, 50):
        add_reading(db_session, sid, NOW_HOUR - timedelta(hours=h + 8))
    monkeypatch.setattr(predict, "forecast", FakePredict(model_ids=ids))
    assert tasks.generate_forecasts()["forecasts_written"] == 0


# ------------------------------------------------------- anchor / forecast_made_at

@pytest.mark.parametrize(
    "newest, expected_anchor",
    [
        (datetime(2026, 9, 20, 13, 0, tzinfo=UTC), datetime(2026, 9, 20, 13, tzinfo=UTC)),
        (datetime(2026, 9, 20, 13, 30, tzinfo=UTC), datetime(2026, 9, 20, 13, tzinfo=UTC)),  # OpenAQ-style :30
        (datetime(2026, 9, 20, 14, 59, 59, tzinfo=UTC), datetime(2026, 9, 20, 14, tzinfo=UTC)),
        (datetime(2026, 9, 20, 15, 30, tzinfo=UTC), datetime(2026, 9, 20, 15, tzinfo=UTC)),
        (datetime(2026, 9, 20, 16, 10, tzinfo=UTC), datetime(2026, 9, 20, 15, tzinfo=UTC)),  # reading "from the future"
    ],
)
def test_forecast_made_at_equals_anchor_hour_and_targets_follow_it(db_session, monkeypatch, clock, newest, expected_anchor):
    fake = _setup(db_session, monkeypatch, {"a": newest})
    result = tasks.generate_forecasts()

    assert result["forecasts_written"] == N_H
    rows = _rows(db_session)
    assert {r.forecast_made_at for r in rows} == {expected_anchor}
    assert all(r.forecast_made_at.minute == 0 and r.forecast_made_at.second == 0 for r in rows)
    assert {r.horizon_hours: r.target_time for r in rows} == {
        h: expected_anchor + timedelta(hours=h) for h in DEFAULT_HORIZONS_HOURS
    }
    assert {c[3] for c in fake.calls} == {expected_anchor}  # predict is asked to forecast from the anchor, not from "now"


# -------------------------------------------------------------- missing models

def test_no_models_at_all_writes_nothing_without_error(db_session, clock):
    """Real predict.forecast, no model_runs rows: every horizon is skipped
    without error, and it is not counted as stale input."""
    sid = add_station(db_session, "a")
    add_reading(db_session, sid, NOW_HOUR)
    result = tasks.generate_forecasts()
    assert result == {"forecasts_written": 0, "skipped_stale": 1, "new_crossings": []}  # skipped_stale = the NO2 pair
    assert _count(db_session) == 0


def test_horizons_without_a_model_are_skipped_others_written(db_session, monkeypatch, clock):
    _setup(db_session, monkeypatch, {"a": NOW_HOUR}, missing_horizons={48, 96})
    result = tasks.generate_forecasts()
    assert result["forecasts_written"] == N_H - 2
    assert sorted(r.horizon_hours for r in _rows(db_session)) == [24, 72, 120]


def test_a_missing_model_for_one_station_does_not_block_the_others(db_session, monkeypatch, clock):
    fake = _setup(db_session, monkeypatch, {"a": NOW_HOUR, "b": NOW_HOUR})

    def selective(session, station_id, pollutant, horizon_hours, as_of_time, **kw):
        if station_id == "openaq:a":
            return None
        return fake(session, station_id, pollutant, horizon_hours, as_of_time, **kw)

    monkeypatch.setattr(predict, "forecast", selective)
    assert tasks.generate_forecasts()["forecasts_written"] == N_H
    assert {r.station_id for r in _rows(db_session)} == {"openaq:b"}


# ------------------------------------------------- real models, end to end

@pytest.fixture()
def trained(db_session, monkeypatch, tmp_path):
    """Trains real tiny models (24h and 48h only) on the synthetic dataset from
    test_train_predict, whose newest reading is 2026-01-17 15:00 UTC."""
    from models.train import train_station_pollutant_horizon
    from tests.integration.test_train_predict import _seed_full_dataset

    monkeypatch.setattr(get_settings(), "model_artifacts_dir", tmp_path)  # keep artifacts out of the repo
    station_id, lat, lon, base, as_of_times = _seed_full_dataset(db_session)
    for horizon in (24, 48):
        train_station_pollutant_horizon(
            db_session, station_id, Pollutant.PM25, horizon, as_of_times[0], as_of_times[-1], holdout_days=2
        )
    newest = base + timedelta(hours=399)
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(newest + timedelta(minutes=45)))
    return station_id, lat, lon, newest


def test_real_models_end_to_end_idempotent_and_matches_direct_predict(db_session, trained):
    station_id, lat, lon, newest = trained
    assert newest == datetime(2026, 1, 17, 15, tzinfo=UTC)

    first = tasks.generate_forecasts()
    # Trained horizons only (24, 48); 72/96/120 have no model. NO2 has no readings.
    assert first["forecasts_written"] == 2
    assert first["skipped_stale"] == 1

    rows = _rows(db_session)
    assert sorted(r.horizon_hours for r in rows) == [24, 48]
    assert {r.forecast_made_at for r in rows} == {newest}
    assert {r.target_time for r in rows} == {newest + timedelta(hours=24), newest + timedelta(hours=48)}
    for r in rows:
        assert r.quantile_low <= r.point_forecast <= r.quantile_high
        assert 0.0 <= r.exceedance_probability <= 1.0

    direct = predict.forecast(db_session, station_id, Pollutant.PM25, 24, newest, station_lat=lat, station_lon=lon)
    row24 = next(r for r in rows if r.horizon_hours == 24)
    assert row24.point_forecast == pytest.approx(direct.point_forecast)
    assert row24.exceedance_flag == direct.exceedance_flag
    assert row24.model_id == direct.model_id

    def snapshot():
        return [(r.horizon_hours, r.point_forecast, r.quantile_low, r.quantile_high, r.exceedance_flag) for r in _rows(db_session)]

    before = snapshot()
    second = tasks.generate_forecasts()
    assert second["forecasts_written"] == 2
    assert second["new_crossings"] == []  # nothing that was flagged before is re-reported
    assert _count(db_session) == 2
    assert snapshot() == before


def test_real_models_stale_feed_is_skipped_and_boundary_is_six_whole_hours(db_session, trained, monkeypatch):
    _, _, _, newest = trained
    stale_clock = newest + timedelta(hours=MAX_INPUT_STALENESS_HOURS + 1, minutes=5)
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(stale_clock))
    result = tasks.generate_forecasts()
    assert result["forecasts_written"] == 0 and result["skipped_stale"] == 2
    assert _count(db_session) == 0

    fresh_clock = newest + timedelta(hours=MAX_INPUT_STALENESS_HOURS, minutes=59)
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(fresh_clock))
    assert tasks.generate_forecasts()["forecasts_written"] == 2  # still fresh at exactly 6 whole hours behind
