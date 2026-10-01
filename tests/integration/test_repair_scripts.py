"""scripts/merge_duplicate_station.py, rebuild_features.py and coverage_diagnosis.py (owner-run)."""

from datetime import datetime, timedelta, timezone

import pytest

from common.constants import Pollutant, SensorSourceName
from db.models import AlertSubscription, RawSensorReading, Station, StationAqiSnapshot
from features.feature_store import read_features
from scripts.merge_duplicate_station import merge
from scripts.rebuild_features import rebuild

NOW = datetime.now(timezone.utc)
HOUR = NOW.replace(minute=0, second=0, microsecond=0)
HALF = HOUR + timedelta(minutes=30)  # the feed's lastupdate: a whole IST hour


def _station(sid, name, lat, lon):
    return Station(
        station_id=sid, name=name, lat=lat, lon=lon, city="Delhi", state="Delhi",
        source=SensorSourceName.OPENAQ, source_location_id=sid, is_active=True, created_at=NOW,
    )


def _snapshot(sid, pollutant, hours_ago, hourly):
    return StationAqiSnapshot(
        station_id=sid, pollutant_id=pollutant, source_updated_at=HALF - timedelta(hours=hours_ago),
        sub_index_avg=hourly, sub_index_hourly=hourly, fetched_at=NOW, source="cpcb-caaqms",
    )


def _seed(db):
    db.add_all([
        _station("openaq:5509", "Anand Vihar, Delhi - DPCC", 28.647622, 77.315809),
        _station("openaq:235", "Anand Vihar, New Delhi - DPCC", 28.6468, 77.3160),
    ])
    db.flush()
    for h in range(60, 110):  # the live entry's OpenAQ history stops 60 h ago
        db.add(RawSensorReading(
            station_id="openaq:235", pollutant=Pollutant.PM25, observed_at=HOUR - timedelta(hours=h),
            source=SensorSourceName.OPENAQ, value=50.0, unit="ug/m3", ingested_at=NOW,
        ))
    for h in range(0, 60):  # CPCB data since then went to the dead entry
        db.add(_snapshot("openaq:5509", "PM2.5", h, 100))
        db.add(_snapshot("openaq:5509", "PM10", h, 140))
    db.add(_snapshot("openaq:235", "PM2.5", 0, 120))  # the new matcher already stored this hour on the live entry
    db.add(AlertSubscription(email="a@example.org", station_ids=["openaq:5509", "openaq:1"], pollutants=["pm25"],
                             is_active=True, is_confirmed=True, created_at=NOW))
    db.commit()


def test_report_only_changes_nothing_and_swapped_arguments_are_refused(db_session):
    _seed(db_session)
    assert merge(db_session, "openaq:5509", "openaq:235", apply=False) is None
    assert db_session.query(StationAqiSnapshot).filter_by(station_id="openaq:5509").count() == 120
    with pytest.raises(ValueError, match="swapped"):
        merge(db_session, "openaq:235", "openaq:5509", apply=True)
    db_session.rollback()
    assert db_session.query(StationAqiSnapshot).filter_by(station_id="openaq:5509").count() == 120


def test_merge_moves_snapshots_derives_readings_and_retires_the_dead_entry(db_session):
    _seed(db_session)
    result = merge(db_session, "openaq:5509", "openaq:235", apply=True)
    assert result == {"snapshots_moved": 119, "snapshots_dropped": 1, "cpcb_readings": 60, "subscriptions_updated": 1}
    db_session.expire_all()

    assert db_session.query(StationAqiSnapshot).filter_by(station_id="openaq:5509").count() == 0
    kept = db_session.query(StationAqiSnapshot).filter_by(station_id="openaq:235", pollutant_id="PM2.5", source_updated_at=HALF).one()
    assert kept.sub_index_hourly == 120  # the live entry's own row for that hour wins
    cpcb = db_session.query(RawSensorReading).filter_by(station_id="openaq:235", source=SensorSourceName.CPCB).all()
    assert len(cpcb) == 60 and {r.pollutant for r in cpcb} == {Pollutant.PM25}
    assert db_session.get(Station, "openaq:5509").is_active is False
    assert db_session.query(AlertSubscription).one().station_ids == ["openaq:235", "openaq:1"]

    # With the gap filled, rebuilding features gives the live station complete lags again.
    rebuild(db_session, HOUR - timedelta(days=3), HOUR, [Pollutant.PM25], ["openaq:235"])
    # The newest reading is the hour that began 1.5 h before the feed's newest lastupdate.
    frame = read_features(db_session, "openaq:235", Pollutant.PM25, HOUR - timedelta(hours=10), HOUR - timedelta(hours=1))
    assert len(frame) == 10 and frame[[c for c in frame.columns if c.startswith("lag_")]].notna().all(axis=None)


def test_stations_too_far_apart_are_refused(db_session):
    db_session.add_all([_station("a", "A - DPCC", 28.60, 77.20), _station("b", "B - DPCC", 28.70, 77.30)])
    db_session.commit()
    with pytest.raises(ValueError, match="apart"):
        merge(db_session, "a", "b", apply=True)


def test_coverage_diagnosis_names_the_stage_that_blocks_a_forecast(db_session, monkeypatch, capsys):
    from scripts import coverage_diagnosis

    _seed(db_session)
    merge(db_session, "openaq:5509", "openaq:235", apply=True)
    rebuild(db_session, HOUR - timedelta(days=3), HOUR, [Pollutant.PM25], None)
    capsys.readouterr()

    monkeypatch.setattr("sys.argv", ["coverage_diagnosis"])
    coverage_diagnosis.main()
    lines = capsys.readouterr().out.splitlines()
    pm25 = next(line for line in lines if line.startswith("openaq:235") and "| pm25 |" in line)
    assert "no model: features exist" in pm25 and "| 50 " in pm25 and "| 60 " in pm25  # 50 OpenAQ hours, 60 CPCB
    assert any(line.startswith("openaq:235") and "| no2 | no readings" in line for line in lines)
    assert not any(line.startswith("openaq:5509") for line in lines)  # inactive after the merge


def test_train_missing_models_trains_only_combinations_without_an_active_model(db_session, monkeypatch, capsys):
    from common.constants import DEFAULT_HORIZONS_HOURS, ModelType
    from db.models import ModelRun
    from models.train import load_model_config
    from scripts import train_missing_models
    from tests.integration.test_train_predict import N_HOURS, _seed_full_dataset

    station_id, _, _, base, _ = _seed_full_dataset(db_session)
    config = load_model_config()
    config["training"] = {**config["training"], "holdout_days": 7}  # the seeded history is 16 days long
    monkeypatch.setattr(train_missing_models, "load_model_config", lambda: config)
    now = base + timedelta(hours=N_HOURS)
    n = len(DEFAULT_HORIZONS_HOURS)

    assert train_missing_models.train_missing(db_session, apply=False, now=now) == {
        ("pm25", "trainable"): n, ("no2", "too few rows"): n}
    assert db_session.query(ModelRun).count() == 0

    assert train_missing_models.train_missing(db_session, apply=True, now=now)[("pm25", "trained")] == n
    active = db_session.query(ModelRun).filter_by(is_active=True, model_type=ModelType.QUANTILE_REGRESSOR).all()
    assert {(r.station_id, r.pollutant, r.horizon_hours) for r in active} == {
        (station_id, Pollutant.PM25, h) for h in DEFAULT_HORIZONS_HOURS}

    # A second run finds nothing left to train for PM2.5 and replaces nothing.
    before = db_session.query(ModelRun).count()
    assert train_missing_models.train_missing(db_session, apply=True, now=now) == {("no2", "too few rows"): n}
    assert db_session.query(ModelRun).count() == before


def test_fix_station_coordinates_moves_name_matched_stations_and_guards_the_weather_cell(db_session):
    from ingestion.loaders.aqi_snapshot_loader import load_aqi_snapshots
    from ingestion.sources.data_gov_in import AqiRecord
    from scripts.fix_station_coordinates import fix

    db_session.add_all([
        _station("openaq:6356", "Pusa, Delhi - DPCC", 28.639645, 77.146262),  # both Pusa stations on one wrong point
        _station("openaq:5404", "Pusa, Delhi - IMD", 28.639645, 77.146263),
        _station("openaq:6980", "Sector-1, Noida - UPPCB", 28.589247, 77.321962),
        _station("openaq:5570", "Aya Nagar, New Delhi - IMD", 28.474261, 77.131606),  # the fix crosses a weather cell
        _station("openaq:5586", "Sirifort, Delhi - CPCB", 28.5504, 77.2159),  # already in place
    ])
    db_session.commit()
    feed = {
        ("Pusa, Delhi - DPCC", 28.636818, 77.173597),
        ("Pusa, Delhi - IITM", 28.63611, 77.173332),
        ("Sector-1, Noida - UPPCB", 28.5898, 77.3101),
        ("Aya Nagar, Delhi - IITM", 28.4706914, 77.1099364),
        ("Sirifort, Delhi - CPCB", 28.5504, 77.2160),
        ("Unknown Place - CPCB", 28.9, 77.9),
    }

    def coordinates():
        db_session.expire_all()
        return {s.station_id: (s.lat, s.lon) for s in db_session.query(Station).all()}

    before = coordinates()
    report = fix(db_session, feed, apply=False, allow_grid_change=False)
    assert sorted(report["moved"]) == ["openaq:5404", "openaq:6356", "openaq:6980"]
    assert report["skipped"] == ["openaq:5570"] and report["problems"] == []
    db_session.rollback()
    assert coordinates() == before  # a report changes nothing

    fix(db_session, feed, apply=True, allow_grid_change=False)
    after = coordinates()
    assert after["openaq:6356"] == (28.636818, 77.173597) and after["openaq:5404"] == (28.63611, 77.173332)
    assert after["openaq:6980"] == (28.5898, 77.3101)
    assert after["openaq:5570"] == before["openaq:5570"] and after["openaq:5586"] == before["openaq:5586"]

    # Now 83 m apart, each Pusa station still gets its own feed station.
    records = [AqiRecord(name, "Delhi", "Delhi", lat, lon, "PM2.5", 1, 3, 2, HALF)
               for name, lat, lon in feed if name.startswith("Pusa")]
    assert load_aqi_snapshots(db_session, records)["matched_stations"] == 2

    assert fix(db_session, feed, apply=True, allow_grid_change=True)["moved"] == ["openaq:5570"]
    assert coordinates()["openaq:5570"] == (28.4706914, 77.1099364)
    again = fix(db_session, feed, apply=True, allow_grid_change=True)
    assert again["moved"] == [] and again["skipped"] == []  # nothing left to correct
