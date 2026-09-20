"""refresh_station_activity: deactivates stations OpenAQ reports dark for 30+
days, reactivates ones that resume, and never wipes the network on an empty
answer. Also the API's view of station currency."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from common.constants import SensorSourceName
from db.models import Station

NOW = datetime.now(timezone.utc)


def _station(loc, active=True):
    return Station(
        station_id=f"openaq:{loc}", name=f"S{loc}", lat=28.6, lon=77.2, city="Delhi", state="Delhi",
        source=SensorSourceName.OPENAQ, source_location_id=loc, is_active=active, created_at=NOW,
    )


class _FakeSource:
    def __init__(self, last_seen):
        self._last_seen = last_seen

    def __call__(self, api_key):
        return self

    def location_last_data_times(self, **_):
        return self._last_seen


def _patch_source(monkeypatch, last_seen):
    from orchestration.plugins.common import tasks

    monkeypatch.setitem(tasks.SENSOR_SOURCE_REGISTRY, tasks.ACTIVE_SOURCE, _FakeSource(last_seen))


def test_refresh_deactivates_dark_reactivates_resumed_and_ignores_unknown(db_session, monkeypatch):
    db_session.add_all([_station("live"), _station("dark"), _station("never"), _station("back", active=False), _station("unknown")])
    db_session.commit()
    _patch_source(monkeypatch, {
        "live": NOW - timedelta(hours=2),
        "dark": NOW - timedelta(days=45),
        "never": None,
        "back": NOW - timedelta(days=1),
    })

    from orchestration.plugins.common.tasks import refresh_station_activity

    assert refresh_station_activity() == {"deactivated": 2, "reactivated": 1}
    db_session.expire_all()
    active = {s.source_location_id: s.is_active for s in db_session.query(Station).all()}
    assert active == {"live": True, "dark": False, "never": False, "back": True, "unknown": True}


def test_refresh_refuses_empty_answer(db_session, monkeypatch):
    db_session.add(_station("live"))
    db_session.commit()
    _patch_source(monkeypatch, {})

    from orchestration.plugins.common.tasks import refresh_station_activity

    with pytest.raises(RuntimeError):
        refresh_station_activity()
    db_session.expire_all()
    assert db_session.query(Station).one().is_active is True


def test_api_lists_current_forecast_stations_first_and_stale_forecast_is_no_data(db_session):
    from tests.integration.test_api import _seed_station_and_forecast  # noqa: PLC0415
    from api.main import app
    from db.models import Forecast
    from sqlalchemy import update

    current = _seed_station_and_forecast(db_session)  # made_at = now -> current
    db_session.add(_station("zzz-no-forecast"))
    db_session.commit()

    client = TestClient(app)
    rows = client.get("/api/v1/stations").json()
    assert rows[0]["station_id"] == current and rows[0]["has_current_forecast"] is True
    assert [r["has_current_forecast"] for r in rows] == [True, False]

    # Age the forecast past the staleness limit: it must stop reading as actionable.
    db_session.execute(update(Forecast).values(forecast_made_at=NOW - timedelta(hours=12)))
    db_session.commit()
    rows = client.get("/api/v1/stations").json()
    assert all(r["has_current_forecast"] is False for r in rows)
    body = client.get(f"/api/v1/forecast/{current}/exceedance", params={"pollutant": "pm25"}).json()
    assert body["overall_recommendation"] == "no-data" and body["days"] == []
    assert client.get(f"/api/v1/stations/{current}").json()["has_current_forecast"] is False
