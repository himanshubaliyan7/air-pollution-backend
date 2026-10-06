"""seed_stations --region, the backfill filters and sensor-id lookup, and the
region backtest block served by GET /regions. Test database only."""

import sys
from datetime import timedelta

import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from common.constants import Pollutant, SensorSourceName
from db.models import RawSensorReading, Station
from ingestion.sources.base import StationMetadata
from scripts import backfill_archive, seed_stations
from tests.integration.forecast_helpers import NOW, add_reading, add_station


class FakeSource:
    """Stands in for OpenAQSource: records what it was asked and returns canned stations."""

    calls: list = []

    def __init__(self, api_key):
        pass

    def list_stations(self, *, bbox, country, default_city="", state=""):
        FakeSource.calls.append((bbox, country, default_city, state))
        return [
            StationMetadata("501", "Bandra", 19.06, 72.86, default_city or "Mumbai-locality", state),
            StationMetadata("502", "Nerul", 19.01, 73.02, default_city, state),
        ] if bbox[0] < 73 else [StationMetadata("601", "Anand Vihar", 28.64, 77.31, default_city or "Delhi", state)]


@pytest.fixture()
def seeded_env(db_engine, tmp_path, monkeypatch):
    FakeSource.calls = []
    monkeypatch.setattr(seed_stations, "SENSOR_SOURCE_REGISTRY", {SensorSourceName.OPENAQ: FakeSource})
    monkeypatch.setattr(seed_stations, "review_path", lambda region_id: tmp_path / f"stations_{region_id}.yaml")
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    monkeypatch.setattr(seed_stations, "get_session", lambda: Session())
    return tmp_path


def _run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["seed_stations", *args])
    seed_stations.main()


def test_seed_mumbai_uses_the_region_bbox_leaves_city_empty_and_writes_its_own_file(db_session, seeded_env, monkeypatch):
    _run(monkeypatch, "--region", "mumbai")
    assert FakeSource.calls == [((72.70, 18.80, 73.35, 19.55), "IN", "", "")]
    rows = {s.station_id: s for s in db_session.execute(select(Station)).scalars()}
    assert set(rows) == {"openaq:501", "openaq:502"}
    assert rows["openaq:502"].city == "" and rows["openaq:502"].state == ""  # for the CPCB feed to fill
    review = yaml.safe_load((seeded_env / "stations_mumbai.yaml").read_text())
    assert review["region"] == "mumbai" and len(review["stations"]) == 2
    assert not (seeded_env / "stations_delhi-ncr.yaml").exists()  # another region's file is never touched


def test_unwritable_review_file_prints_the_yaml_and_still_succeeds(db_session, seeded_env, monkeypatch, capsys):
    """config/ is read-only in the container: the committed seed must not end in a traceback."""
    missing = seeded_env / "no-such-dir" / "review.yaml"
    _run(monkeypatch, "--region", "mumbai", "--review-file", str(missing))
    out = capsys.readouterr().out
    assert "Could not write" in out and "region: mumbai" in out and "openaq:501" in out
    assert db_session.query(Station).count() == 2


def test_review_file_option_overrides_the_default_path(db_session, seeded_env, monkeypatch):
    target = seeded_env / "custom.yaml"
    _run(monkeypatch, "--region", "mumbai", "--review-file", str(target))
    assert yaml.safe_load(target.read_text())["region"] == "mumbai"
    assert not (seeded_env / "stations_mumbai.yaml").exists()


def test_seed_defaults_to_delhi_and_keeps_its_stored_defaults(db_session, seeded_env, monkeypatch):
    _run(monkeypatch)
    assert FakeSource.calls == [((76.6, 28.2, 77.6, 29.0), "IN", "Delhi", "Delhi")]
    s = db_session.execute(select(Station)).scalar_one()
    assert (s.station_id, s.city, s.state) == ("openaq:601", "Delhi", "Delhi")
    assert (seeded_env / "stations_delhi-ncr.yaml").exists()


def test_reseed_does_not_overwrite_city_or_state_corrected_by_the_cpcb_feed(db_session, seeded_env, monkeypatch):
    _run(monkeypatch, "--region", "mumbai")
    db_session.execute(Station.__table__.update().where(Station.station_id == "openaq:501").values(city="Mumbai", state="Maharashtra"))
    db_session.commit()
    _run(monkeypatch, "--region", "mumbai")
    s = db_session.execute(select(Station).where(Station.station_id == "openaq:501")).scalar_one()
    assert (s.city, s.state) == ("Mumbai", "Maharashtra")


def test_dry_run_writes_nothing_and_lists_the_stations(db_session, seeded_env, monkeypatch, capsys):
    _run(monkeypatch, "--region", "mumbai", "--dry-run")
    out = capsys.readouterr().out
    assert "openaq:501" in out and "DRY RUN - 2 stations" in out
    assert db_session.execute(select(Station)).first() is None
    assert list(seeded_env.iterdir()) == []


def _cpcb_reading(db, station_id, observed_at, record_id):
    db.add(RawSensorReading(
        station_id=station_id, pollutant=Pollutant.PM25, observed_at=observed_at, source=SensorSourceName.CPCB,
        value=80.0, unit="ug/m3", source_record_id=record_id, ingested_at=NOW,
    ))
    db.commit()


def test_live_sensor_ids_ignores_a_newer_cpcb_row_instead_of_crashing_on_int_cpcb(db_session):
    sid = add_station(db_session, "7")
    add_reading(db_session, sid, NOW - timedelta(hours=5))
    openaq_row = db_session.execute(select(RawSensorReading)).scalar_one()
    openaq_row.source_record_id = "4242:2026-09-20T10:30:00Z"
    db_session.commit()
    # The newest row for the pair is CPCB's: "cpcb:2026-09-20T15:00Z:80" has no sensor id.
    _cpcb_reading(db_session, sid, NOW - timedelta(hours=1), "cpcb:2026-09-20T15:00Z:80")
    assert backfill_archive.live_sensor_ids(db_session) == {sid: {Pollutant.PM25: 4242}}


def test_live_sensor_ids_skips_a_station_that_only_has_cpcb_rows(db_session):
    sid = add_station(db_session, "8")
    _cpcb_reading(db_session, sid, NOW - timedelta(hours=1), "cpcb:2026-09-20T15:00Z:80")
    assert backfill_archive.live_sensor_ids(db_session) == {}


def test_select_stations_filters_by_region_and_station_id(db_session):
    delhi = add_station(db_session, "d1")
    mumbai = add_station(db_session, "m1", lat=19.07, lon=72.88)
    mumbai2 = add_station(db_session, "m2", lat=19.0, lon=73.0)
    stations = list(db_session.execute(select(Station)).scalars())
    ids = lambda picked: sorted(s.station_id for s in picked)  # noqa: E731
    assert ids(backfill_archive.select_stations(stations)) == sorted([delhi, mumbai, mumbai2])
    assert ids(backfill_archive.select_stations(stations, "mumbai")) == sorted([mumbai, mumbai2])
    assert ids(backfill_archive.select_stations(stations, "mumbai", [mumbai2])) == [mumbai2]
    assert ids(backfill_archive.select_stations(stations, station_ids=[delhi])) == [delhi]
    with pytest.raises(ValueError):
        backfill_archive.select_stations(stations, "atlantis")


def test_regions_api_serves_delhis_backtest_and_null_for_mumbai(db_session):
    from api.main import app

    regions = {r["id"]: r for r in TestClient(app).get("/api/v1/regions").json()}
    assert regions["delhi-ncr"]["backtest"] == {
        "period": "2025-10-15 to 2025-11-30", "exact_grade_tomorrow": 0.61, "exact_grade_day_5": 0.52,
        "no_go_called_go_low": 0.02, "no_go_called_go_high": 0.05,
    }
    assert regions["mumbai"]["backtest"] is None
    assert TestClient(app).get("/api/v1/regions/mumbai").json()["backtest"] is None


def test_scripts_do_not_import_each_other():
    """Owner-run scripts are piped into the container, where the `scripts` package does not exist."""
    from pathlib import Path

    for path in (Path(__file__).resolve().parents[2] / "scripts").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "from scripts" not in text.replace("python -m scripts", "") and "import scripts" not in text, path.name
