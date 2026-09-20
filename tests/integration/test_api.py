import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from common.constants import ModelType, Pollutant, SensorSourceName
from db.models import Forecast, ModelRun, Station


def _seed_station_and_forecast(db_session):
    station_id = "openaq:api-test"
    db_session.add(
        Station(
            station_id=station_id,
            name="API Test Station",
            lat=28.6,
            lon=77.2,
            city="Delhi",
            state="Delhi",
            source=SensorSourceName.OPENAQ,
            source_location_id="api-test",
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    db_session.commit()

    trained_at = datetime.now(timezone.utc)
    model_id = uuid.uuid4()
    db_session.add(
        ModelRun(
            model_id=model_id,
            station_id=station_id,
            pollutant=Pollutant.PM25,
            horizon_hours=24,
            model_type=ModelType.QUANTILE_REGRESSOR,
            quantile=0.5,
            feature_set_version="v1",
            artifact_path="unused",
            trained_at=trained_at,
            training_window_start=trained_at - timedelta(days=30),
            training_window_end=trained_at,
            metrics={"f1": 0.8, "precision": 0.75, "recall": 0.9, "mae": 12.3, "rmse": 15.1},
            hyperparams={},
            is_active=True,
        )
    )
    db_session.commit()

    made_at = datetime.now(timezone.utc)
    for i, horizon in enumerate([24, 48, 72]):
        db_session.add(
            Forecast(
                station_id=station_id,
                pollutant=Pollutant.PM25,
                model_id=model_id,
                forecast_made_at=made_at,
                target_time=made_at + timedelta(hours=horizon),
                horizon_hours=horizon,
                point_forecast=100.0 + i * 10,
                quantile_low=80.0 + i * 10,
                quantile_high=130.0 + i * 10,
                exceedance_probability=0.1 + i * 0.3,
                exceedance_flag=(i == 2),
            )
        )
    db_session.commit()
    return station_id


def test_health_endpoint(db_engine):
    from api.main import app

    client = TestClient(app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_stations_and_forecast_endpoints(db_session):
    station_id = _seed_station_and_forecast(db_session)
    from api.main import app

    client = TestClient(app)

    resp = client.get("/api/v1/stations")
    assert resp.status_code == 200
    assert any(s["station_id"] == station_id for s in resp.json())

    resp = client.get(f"/api/v1/stations/{station_id}")
    assert resp.status_code == 200
    assert resp.json()["station_id"] == station_id

    resp = client.get(f"/api/v1/forecast/{station_id}", params={"pollutant": "pm25"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["forecasts"]) == 3
    assert body["forecasts"][0]["horizon_hours"] == 24

    resp = client.get(f"/api/v1/forecast/{station_id}/exceedance", params={"pollutant": "pm25"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_recommendation"] == "no-go"  # the 72h horizon has exceedance_flag=true

    resp = client.get("/api/v1/model-health", params={"station_id": station_id})
    assert resp.status_code == 200

    resp = client.get("/api/v1/stations/does-not-exist")
    assert resp.status_code == 404


def test_subscription_create_and_delete(db_session):
    from api.main import app

    client = TestClient(app)
    resp = client.post(
        "/api/v1/subscriptions",
        json={"email": "school@example.com", "station_ids": ["openaq:api-test"], "pollutants": ["pm25"]},
    )
    assert resp.status_code == 200
    subscriber_id = resp.json()["subscriber_id"]
    assert resp.json()["status"] == "subscribed"

    resp = client.delete(f"/api/v1/subscriptions/{subscriber_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "unsubscribed"
