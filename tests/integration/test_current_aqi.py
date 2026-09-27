"""Current-conditions CPCB AQI (direct feed, data.gov.in fallback): source order,
matching to our stations, idempotent storage, the activity rule, and the API
(including that an old reading is never served as current)."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from common.constants import SensorSourceName
from db.models import Station, StationAqiSnapshot
from ingestion.loaders.aqi_snapshot_loader import load_aqi_snapshots
from ingestion.sources.data_gov_in import ATTRIBUTION, AqiRecord

NOW = datetime.now(timezone.utc)
HOUR = NOW.replace(minute=0, second=0, microsecond=0)


def _station(sid, lat=28.6469, lon=77.3158, active=True):
    return Station(
        station_id=sid, name=sid, lat=lat, lon=lon, city="Delhi", state="Delhi",
        source=SensorSourceName.OPENAQ, source_location_id=sid, is_active=active, created_at=NOW,
    )


def _rec(pollutant, avg, lat=28.6469, lon=77.3158, at=HOUR, name="Anand Vihar"):
    return AqiRecord(name, "Delhi", "Delhi", lat, lon, pollutant, avg / 2, avg * 2, avg, at)


def test_matches_by_coordinates_stores_idempotently_and_counts_unmatched(db_session):
    db_session.add(_station("openaq:near"))
    db_session.commit()
    records = [_rec("PM2.5", 166), _rec("NO2", 27), _rec("PM10", 140, lat=28.7, lon=77.4, name="Far Away")]

    assert load_aqi_snapshots(db_session, records) == {"stored": 2, "matched_stations": 1, "unmatched_stations": 1}
    assert load_aqi_snapshots(db_session, records)["stored"] == 2  # re-run: same rows, no duplicates
    assert db_session.query(StationAqiSnapshot).count() == 2

    load_aqi_snapshots(db_session, [_rec("PM2.5", 170)])  # a revised value for the same hour replaces it
    db_session.expire_all()
    row = db_session.query(StationAqiSnapshot).filter_by(pollutant_id="PM2.5").one()
    assert row.sub_index_avg == 170 and row.station_id == "openaq:near"


def test_matches_inactive_stations_too(db_session):
    db_session.add(_station("openaq:dark", active=False))
    db_session.commit()
    assert load_aqi_snapshots(db_session, [_rec("PM2.5", 100)])["matched_stations"] == 1


def test_station_dark_on_openaq_is_kept_active_by_a_recent_snapshot(db_session, monkeypatch):
    from orchestration.plugins.common import tasks

    db_session.add_all([_station("openaq:has-aqi", lat=28.60, lon=77.20), _station("openaq:truly-dead", lat=28.90, lon=77.10)])
    db_session.commit()
    load_aqi_snapshots(db_session, [_rec("PM2.5", 100, lat=28.60, lon=77.20)])

    class Source:
        def __call__(self, api_key):
            return self

        def location_last_data_times(self, **_):
            return {"openaq:has-aqi": NOW - timedelta(days=90), "openaq:truly-dead": NOW - timedelta(days=90)}

    monkeypatch.setitem(tasks.SENSOR_SOURCE_REGISTRY, tasks.ACTIVE_SOURCE, Source())
    tasks.refresh_station_activity()
    db_session.expire_all()
    active = {s.station_id: s.is_active for s in db_session.query(Station).all()}
    assert active == {"openaq:has-aqi": True, "openaq:truly-dead": False}


def _sources(monkeypatch, cpcb, data_gov_in, key="k"):
    """Stub both feeds: each argument is a record list, or an exception to raise."""
    from common.config import get_settings
    from orchestration.plugins.common import tasks

    def stub(result):
        def fetch(self, *a, **k):
            if isinstance(result, Exception):
                raise result
            return result
        return fetch

    monkeypatch.setattr(get_settings(), "data_gov_in_api_key", key)
    monkeypatch.setattr(tasks.CpcbCaaqmsClient, "fetch", stub(cpcb))
    monkeypatch.setattr(tasks.DataGovInClient, "fetch", stub(data_gov_in))
    return tasks


def test_ingest_prefers_cpcb_and_stores_hourly_sub_index_and_source(db_session, monkeypatch):
    db_session.add(_station("openaq:near"))
    db_session.commit()
    rec = AqiRecord("Anand Vihar", "Delhi", "Delhi", 28.6469, 77.3158, "PM2.5", 5, 74, 23, HOUR, sub_index_hourly=11)
    tasks = _sources(monkeypatch, cpcb=[rec], data_gov_in=RuntimeError("must not be called"))

    assert tasks.ingest_current_aqi()["source"] == "cpcb-caaqms"
    row = db_session.query(StationAqiSnapshot).one()
    assert (row.sub_index_avg, row.sub_index_hourly, row.source) == (23, 11, "cpcb-caaqms")


def test_ingest_falls_back_to_data_gov_in_when_cpcb_fails_or_is_empty(monkeypatch):
    for cpcb in (RuntimeError("CPCB down"), []):
        tasks = _sources(monkeypatch, cpcb=cpcb, data_gov_in=[_rec("PM2.5", 100)])
        records, source = tasks._fetch_current_aqi(NOW)
        assert source == "data-gov-in" and len(records) == 1


def test_ingest_falls_back_when_cpcb_is_stale_but_keeps_the_newest_if_all_are(monkeypatch):
    old, older = _rec("PM2.5", 1, at=HOUR - timedelta(hours=8)), _rec("PM2.5", 2, at=HOUR - timedelta(hours=9))
    tasks = _sources(monkeypatch, cpcb=[old], data_gov_in=[_rec("PM2.5", 3)])
    assert tasks._fetch_current_aqi(NOW)[1] == "data-gov-in"
    tasks = _sources(monkeypatch, cpcb=[old], data_gov_in=[older])
    assert tasks._fetch_current_aqi(NOW) == ([old], "cpcb-caaqms")


def test_ingest_works_without_a_data_gov_in_key_and_fails_loudly_when_every_source_fails(monkeypatch):
    tasks = _sources(monkeypatch, cpcb=[_rec("PM2.5", 100)], data_gov_in=RuntimeError("no key, never called"), key="")
    assert tasks._fetch_current_aqi(NOW)[1] == "cpcb-caaqms"
    tasks = _sources(monkeypatch, cpcb=RuntimeError("CPCB down"), data_gov_in=[])
    with pytest.raises(RuntimeError, match="cpcb-caaqms: CPCB down; data-gov-in: no usable records"):
        tasks._fetch_current_aqi(NOW)


def test_api_current_aqi_overall_categories_and_attribution(db_session):
    from api.main import app

    db_session.add(_station("openaq:api"))
    db_session.commit()
    load_aqi_snapshots(db_session, [_rec("PM2.5", 166), _rec("PM10", 140), _rec("NO2", 27), _rec("CO", 80)])

    client = TestClient(app)
    body = client.get("/api/v1/stations/openaq:api/current-aqi").json()
    assert body["is_current"] is True and body["as_of"] is not None
    assert body["overall"] == {"aqi": 166, "category": "moderate", "driver": "PM2.5"}
    assert body["at_or_above_health_threshold"] is False
    assert {p["pollutant_id"]: p["category"] for p in body["pollutants"]} == {
        "CO": "satisfactory", "NO2": "good", "PM10": "moderate", "PM2.5": "moderate"}
    assert body["aqi_standard"] == "CPCB National AQI" and body["timezone"] == "Asia/Kolkata"
    assert body["attribution"] == ATTRIBUTION

    row = next(s for s in client.get("/api/v1/stations").json() if s["station_id"] == "openaq:api")
    assert row["has_current_aqi"] is True


def test_api_overall_is_null_when_cpcb_minimum_is_not_met(db_session):
    from api.main import app

    db_session.add(_station("openaq:two"))
    db_session.commit()
    load_aqi_snapshots(db_session, [_rec("PM2.5", 250), _rec("NO2", 27)])
    body = TestClient(app).get("/api/v1/stations/openaq:two/current-aqi").json()
    assert body["is_current"] is True and body["overall"] is None and len(body["pollutants"]) == 2
    assert body["at_or_above_health_threshold"] is None  # no overall AQI, so no verdict


def test_api_old_reading_is_reported_as_not_current_and_never_served(db_session):
    """Same rule as forecasts: a stale reading must not read as current conditions."""
    from api.main import app

    db_session.add(_station("openaq:old"))
    db_session.commit()
    load_aqi_snapshots(db_session, [_rec("PM2.5", 166), _rec("PM10", 140), _rec("NO2", 27)])
    db_session.execute(update(StationAqiSnapshot).values(source_updated_at=HOUR - timedelta(hours=20)))
    db_session.commit()

    client = TestClient(app)
    body = client.get("/api/v1/stations/openaq:old/current-aqi").json()
    assert body["is_current"] is False and body["overall"] is None and body["pollutants"] == []
    assert body["as_of"] is not None  # says how old the last reading was
    row = next(s for s in client.get("/api/v1/stations").json() if s["station_id"] == "openaq:old")
    assert row["has_current_aqi"] is False


def test_api_station_without_readings_and_unknown_station(db_session):
    from api.main import app

    db_session.add(_station("openaq:none"))
    db_session.commit()
    client = TestClient(app)
    body = client.get("/api/v1/stations/openaq:none/current-aqi").json()
    assert body["is_current"] is False and body["as_of"] is None and body["overall"] is None
    assert client.get("/api/v1/stations/nope/current-aqi").status_code == 404


def test_api_flags_a_reading_at_or_above_the_health_threshold(db_session):
    from api.main import app

    db_session.add(_station("openaq:bad"))
    db_session.commit()
    load_aqi_snapshots(db_session, [_rec("PM2.5", 250), _rec("PM10", 140), _rec("NO2", 27)])
    body = TestClient(app).get("/api/v1/stations/openaq:bad/current-aqi").json()
    assert body["overall"]["category"] == "poor" and body["at_or_above_health_threshold"] is True


def test_matched_station_takes_its_real_city_and_state_from_the_feed(db_session):
    """Regression: every station was stored with city/state "Delhi", including those in
    Noida, Gurugram and Ghaziabad, so a station list showed the wrong city."""
    db_session.add(_station("openaq:noida"))  # created with the hardcoded city/state "Delhi"
    db_session.commit()
    record = AqiRecord("Sector - 62, Noida", "Noida", "Uttar Pradesh", 28.6469, 77.3158, "PM2.5", 1, 3, 2, HOUR)

    load_aqi_snapshots(db_session, [record])
    db_session.expire_all()
    station = db_session.get(Station, "openaq:noida")
    assert (station.city, station.state) == ("Noida", "Uttar Pradesh")

    # A row with blank city/state must not blank out what we already know.
    load_aqi_snapshots(db_session, [AqiRecord("x", "", "", 28.6469, 77.3158, "PM2.5", 1, 3, 2, HOUR)])
    db_session.expire_all()
    assert db_session.get(Station, "openaq:noida").city == "Noida"


def test_exceedance_reports_forecast_time_and_currency(db_session):
    from tests.integration.test_api import _seed_station_and_forecast  # noqa: PLC0415
    from api.main import app
    from db.models import Forecast

    sid = _seed_station_and_forecast(db_session)
    client = TestClient(app)
    body = client.get(f"/api/v1/forecast/{sid}/exceedance").json()
    assert body["is_current"] is True and body["forecast_made_at"] is not None

    db_session.execute(update(Forecast).values(forecast_made_at=HOUR - timedelta(days=3)))
    db_session.commit()
    body = client.get(f"/api/v1/forecast/{sid}/exceedance").json()
    assert body["is_current"] is False and body["days"] == [] and body["overall_recommendation"] == "no-data"
    assert body["forecast_made_at"] is not None  # says how old the last run was
