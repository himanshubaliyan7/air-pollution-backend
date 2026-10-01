"""GET /overview: every active station in one response. It must say exactly what
the per-station endpoints say, and silence must never read as "go"."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from common.constants import MAX_INPUT_STALENESS_HOURS, Pollutant
from db.models import StationAqiSnapshot
from tests.integration.forecast_helpers import add_station, seed_forecast_run

NOW = datetime.now(timezone.utc)
HOUR = NOW.replace(minute=0, second=0, microsecond=0)


def _snapshots(db, station_id, hours_ago, sub_indices):
    for pollutant, value in sub_indices.items():
        db.add(StationAqiSnapshot(
            station_id=station_id, pollutant_id=pollutant, source_updated_at=HOUR - timedelta(hours=hours_ago),
            sub_index_min=value / 2, sub_index_max=value * 2, sub_index_avg=value, fetched_at=NOW, source="cpcb-caaqms",
        ))
    db.commit()


def _seed(db):
    full = add_station(db, "full")
    _snapshots(db, full, 4, {"PM2.5": 90, "PM10": 80, "NO2": 20})  # superseded by the next hour, except CO
    _snapshots(db, full, 4, {"CO": 30})
    _snapshots(db, full, 3, {"PM2.5": 250, "PM10": 140, "NO2": 27})
    seed_forecast_run(db, full, HOUR - timedelta(hours=30))  # an older run that must be ignored
    seed_forecast_run(db, full, HOUR - timedelta(hours=2), flagged_horizons=(72,))
    seed_forecast_run(db, full, HOUR - timedelta(hours=5), pollutant=Pollutant.NO2)

    partial = add_station(db, "partial")
    seed_forecast_run(db, partial, HOUR - timedelta(hours=1), horizons=(24, 48))  # nothing flagged, days missing

    stale = add_station(db, "stale")
    _snapshots(db, stale, 20, {"PM2.5": 250, "PM10": 140, "NO2": 27})
    seed_forecast_run(db, stale, HOUR - timedelta(hours=MAX_INPUT_STALENESS_HOURS + 1), flagged_horizons=())

    empty = add_station(db, "empty")
    add_station(db, "inactive", active=False)
    return full, partial, stale, empty


def test_overview_matches_the_per_station_endpoints(db_session):
    from api.main import app

    ids = _seed(db_session)
    client = TestClient(app)
    response = client.get("/api/v1/overview")
    assert response.status_code == 200 and response.headers["cache-control"] == "public, max-age=120"
    body = response.json()
    by_id = {s["station_id"]: s for s in body["stations"]}
    assert set(by_id) == set(ids)  # the inactive station is left out

    for sid in ids:
        current = client.get(f"/api/v1/stations/{sid}/current-aqi").json()
        got = by_id[sid]["current_aqi"]
        if current["is_current"]:
            assert got == {k: current[k] for k in got}
        else:
            assert got["is_current"] is False and got["overall"] is None and got["pollutants"] == []
        assert body["attribution"] == current["attribution"]

        assert [o["pollutant"] for o in by_id[sid]["outlooks"]] == ["pm25", "no2"]
        for outlook in by_id[sid]["outlooks"]:
            single = client.get(f"/api/v1/forecast/{sid}/exceedance", params={"pollutant": outlook["pollutant"]}).json()
            if single["is_current"]:
                assert outlook == single
            else:  # the overview does not look up how old a stale run is
                assert outlook == {**single, "forecast_made_at": None}


def test_overview_content_and_no_data_is_never_go(db_session):
    from api.main import app

    full, partial, stale, empty = _seed(db_session)
    by_id = {s["station_id"]: s for s in TestClient(app).get("/api/v1/overview").json()["stations"]}

    current = by_id[full]["current_aqi"]
    assert current["overall"] == {"aqi": 250, "category": "poor", "driver": "PM2.5"}
    assert current["at_or_above_health_threshold"] is True
    assert [p["pollutant_id"] for p in current["pollutants"]] == ["CO", "NO2", "PM10", "PM2.5"]
    pm25, no2 = by_id[full]["outlooks"]
    assert pm25["overall_recommendation"] == "no-go" and len(pm25["days"]) == 5
    assert no2["overall_recommendation"] == "go" and by_id[full]["region_id"] == "delhi-ncr"

    verdicts = {sid: [o["overall_recommendation"] for o in by_id[sid]["outlooks"]] for sid in (partial, stale, empty)}
    assert verdicts == {partial: ["no-data", "no-data"], stale: ["no-data", "no-data"], empty: ["no-data", "no-data"]}
    assert len(by_id[partial]["outlooks"][0]["days"]) == 2  # the known days are still shown
    assert by_id[stale]["current_aqi"]["is_current"] is False and by_id[stale]["current_aqi"]["as_of"] is not None
    assert by_id[empty]["current_aqi"] == {
        "as_of": None, "is_current": False, "overall": None, "at_or_above_health_threshold": None, "pollutants": []}


def test_overview_filters_by_region_and_days_ahead(db_session):
    from api.main import app

    full, *_ = _seed(db_session)
    add_station(db_session, "elsewhere", lat=12.97, lon=77.59)
    client = TestClient(app)
    assert "openaq:elsewhere" in {s["station_id"] for s in client.get("/api/v1/overview").json()["stations"]}
    in_region = client.get("/api/v1/overview", params={"region_id": "delhi-ncr", "days_ahead": 2}).json()["stations"]
    assert "openaq:elsewhere" not in {s["station_id"] for s in in_region}
    pm25 = next(s for s in in_region if s["station_id"] == full)["outlooks"][0]
    assert len(pm25["days"]) == 2 and pm25["overall_recommendation"] == "go"  # the flagged day is day 3
    assert client.get("/api/v1/overview", params={"region_id": "nowhere"}).json()["stations"] == []
