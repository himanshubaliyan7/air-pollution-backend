"""The daily-mean model family (graded verdicts, owner decision 2026-10-02):
training on local-day means, forecasts keyed by the target day, the graded
outlook, and the two families staying out of each other's way."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from common.config import get_settings
from common.constants import DEFAULT_HORIZONS_HOURS, ForecastTarget, ModelType, Pollutant
from db.models import ExceedanceEvaluation, Forecast, ModelRun, Station
from models import predict, registry
from models.outlook import build_outlook
from models.train import train_station_pollutant_horizon
from orchestration.plugins.common import tasks
from tests.clock import fixed_datetime
from tests.integration.forecast_helpers import add_model_run, add_reading, add_station, seed_forecast_run
from tests.integration.test_train_predict import _seed_full_dataset

UTC = timezone.utc
DAILY = ForecastTarget.DAILY_MEAN.value
# 20:30 on 17 January in Delhi: the newest hour of the seeded history.
NEWEST = datetime(2026, 1, 17, 15, tzinfo=UTC)
MIDNIGHT_18TH = datetime(2026, 1, 17, 18, 30, tzinfo=UTC)  # 00:00 on 18 January in Delhi


def _train_all(db, monkeypatch, tmp_path, level):
    monkeypatch.setattr(get_settings(), "model_artifacts_dir", tmp_path)  # keep artifacts out of the repo
    station_id, lat, lon, base, as_of_times = _seed_full_dataset(db, level=level)
    for horizon in DEFAULT_HORIZONS_HOURS:
        assert train_station_pollutant_horizon(
            db, station_id, Pollutant.PM25, horizon, as_of_times[0], as_of_times[-1], holdout_days=7
        ), horizon  # 7: the seeded history is 16 days and a 5-day-ahead label needs 6 of them
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(NEWEST + timedelta(minutes=45)))
    return station_id, lat, lon


def test_models_learn_the_days_mean_and_stay_apart_from_the_hourly_family(db_session, monkeypatch, tmp_path, daily_target):
    station_id, lat, lon = _train_all(db_session, monkeypatch, tmp_path, level=80.0)

    assert {r.target for r in db_session.query(ModelRun).all()} == {DAILY}
    assert registry.get_active_models(db_session, station_id, Pollutant.PM25, 24, ModelType.QUANTILE_REGRESSOR)
    assert registry.get_active_models(
        db_session, station_id, Pollutant.PM25, 24, ModelType.QUANTILE_REGRESSOR, ForecastTarget.HOURLY) == []

    # The seeded hours swing between 40 and 120 around a mean of 80. At 20:30 local the
    # value 24 h ahead is near 50; the next day's mean is near 80.
    result = predict.forecast(db_session, station_id, Pollutant.PM25, 24, NEWEST, station_lat=lat, station_lon=lon)
    assert 70 < result.point_forecast < 90
    assert result.quantile_low <= result.point_forecast <= result.quantile_high


@pytest.mark.parametrize("level, category, verdict", [(200.0, "very_poor", "no-go"), (45.0, "satisfactory", "go")])
def test_forecasts_are_keyed_by_the_target_day_and_graded(db_session, monkeypatch, tmp_path, daily_target, level, category, verdict):
    station_id, _, _ = _train_all(db_session, monkeypatch, tmp_path, level)

    result = tasks.generate_forecasts()
    assert result["forecasts_written"] == len(DEFAULT_HORIZONS_HOURS)
    rows = db_session.query(Forecast).order_by(Forecast.horizon_hours).all()
    assert {r.target for r in rows} == {DAILY} and {r.forecast_made_at for r in rows} == {NEWEST}
    assert [r.target_time for r in rows] == [MIDNIGHT_18TH + timedelta(days=k) for k in range(5)]

    outlook = build_outlook(db_session, db_session.get(Station, station_id), Pollutant.PM25, now=NEWEST + timedelta(minutes=45))
    assert [d.date.isoformat() for d in outlook.days] == [f"2026-01-{day}" for day in (18, 19, 20, 21, 22)]
    assert {d.aqi_category for d in outlook.days} == {category}
    assert {d.verdict for d in outlook.days} == {verdict} and outlook.overall_recommendation == verdict

    # A second run at the same anchor replaces the rows; it does not add any.
    assert tasks.generate_forecasts()["forecasts_written"] == len(DEFAULT_HORIZONS_HOURS)
    assert db_session.query(Forecast).count() == len(DEFAULT_HORIZONS_HOURS)


def _daily_run(db, station_id, made_at, days):
    """One daily-mean run: `days` is a list of (low, median, high), one per day ahead."""
    model_id = add_model_run(db, station_id)
    for k, (low, median, high) in enumerate(days, start=1):
        db.add(Forecast(
            station_id=station_id, pollutant=Pollutant.PM25, model_id=model_id, forecast_made_at=made_at,
            target_time=MIDNIGHT_18TH + timedelta(days=k - 1), horizon_hours=24 * k, point_forecast=median,
            quantile_low=low, quantile_high=high, exceedance_probability=0.5, exceedance_flag=median >= 91, target=DAILY,
        ))
    db.commit()


def test_each_day_gets_the_category_of_its_mean_and_the_verdict_that_follows(db_session, daily_target):
    station_id = add_station(db_session, "graded")
    _daily_run(db_session, station_id, NEWEST, [
        (20, 40, 55),     # satisfactory
        (70, 80, 88),     # moderate
        (80, 100, 115),   # poor: likely above 91, unlikely above 121
        (130, 180, 240),  # very poor
        (260, 300, 400),  # severe
    ])
    outlook = build_outlook(db_session, db_session.get(Station, station_id), Pollutant.PM25, now=NEWEST + timedelta(hours=1))
    assert [(d.aqi_category, d.verdict) for d in outlook.days] == [
        ("satisfactory", "go"), ("moderate", "go"), ("poor", "caution"), ("very_poor", "no-go"), ("severe", "no-go")]
    assert [d.expected_value for d in outlook.days] == [40, 80, 100, 180, 300]
    assert outlook.overall_recommendation == "no-go"


def test_a_poor_day_makes_the_outlook_caution_and_a_missing_day_makes_it_no_data(db_session, daily_target):
    now = NEWEST + timedelta(hours=1)
    poor = add_station(db_session, "poor")
    _daily_run(db_session, poor, NEWEST, [(20, 40, 55), (80, 100, 115), (20, 40, 55), (20, 40, 55), (20, 40, 55)])
    assert build_outlook(db_session, db_session.get(Station, poor), Pollutant.PM25, now=now).overall_recommendation == "caution"

    partial = add_station(db_session, "partial")
    _daily_run(db_session, partial, NEWEST, [(20, 40, 55), (20, 40, 55)])
    outlook = build_outlook(db_session, db_session.get(Station, partial), Pollutant.PM25, now=now)
    assert outlook.overall_recommendation == "no-data" and len(outlook.days) == 2  # silence is never "go"


def test_the_family_not_being_served_is_invisible(db_session, monkeypatch):
    now = NEWEST + timedelta(hours=1)
    station_id = add_station(db_session, "both")
    station = db_session.get(Station, station_id)
    seed_forecast_run(db_session, station_id, NEWEST - timedelta(hours=1), flagged_horizons=(24,))  # hourly: no-go
    _daily_run(db_session, station_id, NEWEST, [(20, 40, 55)] * 5)  # daily: go

    hourly = build_outlook(db_session, station, Pollutant.PM25, now=now)
    assert hourly.overall_recommendation == "no-go" and hourly.forecast_made_at == NEWEST - timedelta(hours=1)

    monkeypatch.setattr(get_settings(), "forecast_target", DAILY)
    daily = build_outlook(db_session, station, Pollutant.PM25, now=now)
    assert daily.overall_recommendation == "go" and daily.forecast_made_at == NEWEST


def test_api_reports_the_target_the_verdict_and_the_expected_value(db_session, daily_target):
    station_id = add_station(db_session, "api-daily")
    made_at = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=2)
    model_id = add_model_run(db_session, station_id)
    start = tasks.target_day_start(made_at, "Asia/Kolkata", 1)
    for k in range(5):
        db_session.add(Forecast(
            station_id=station_id, pollutant=Pollutant.PM25, model_id=model_id, forecast_made_at=made_at,
            target_time=start + timedelta(days=k), horizon_hours=24 * (k + 1), point_forecast=100.0,
            quantile_low=80.0, quantile_high=115.0, exceedance_probability=0.68, exceedance_flag=True, target=DAILY,
        ))
    db_session.commit()
    from api.main import app

    client = TestClient(app)
    body = client.get(f"/api/v1/forecast/{station_id}/exceedance").json()
    assert body["target"] == DAILY and body["overall_recommendation"] == "caution"
    assert {(d["aqi_category"], d["verdict"], d["expected_value"]) for d in body["days"]} == {("poor", "caution", 100.0)}

    series = client.get(f"/api/v1/forecast/{station_id}").json()
    assert series["target"] == DAILY and len(series["forecasts"]) == 5
    overview = client.get("/api/v1/overview").json()
    station = next(s for s in overview["stations"] if s["station_id"] == station_id)
    assert station["outlooks"][0]["days"][0]["verdict"] == "caution"
    assert next(s for s in client.get("/api/v1/stations").json() if s["station_id"] == station_id)["has_current_forecast"]


def test_history_shows_the_last_forecast_made_before_each_day_against_its_hours(db_session, monkeypatch, daily_target):
    from api.routers import forecasts as forecasts_router

    station_id = add_station(db_session, "history")
    day_start = MIDNIGHT_18TH  # 18 January in Delhi
    for h in range(24):
        add_reading(db_session, station_id, day_start + timedelta(minutes=30, hours=h), value=60.0 + h)
    add_reading(db_session, station_id, day_start - timedelta(minutes=30), value=50.0)  # 23:30 on the 17th
    model_id = add_model_run(db_session, station_id)
    for made_at, horizon, point in [
        (day_start - timedelta(hours=30), 48, 150.0),  # two days ahead
        (day_start - timedelta(hours=3), 24, 90.0),    # the evening before: the one shown
        (day_start + timedelta(hours=2), 24, 300.0),   # about the 19th
    ]:
        target_time = day_start if made_at < day_start else day_start + timedelta(days=1)
        db_session.add(Forecast(
            station_id=station_id, pollutant=Pollutant.PM25, model_id=model_id, forecast_made_at=made_at,
            target_time=target_time, horizon_hours=horizon, point_forecast=point, quantile_low=point - 10,
            quantile_high=point + 10, exceedance_probability=0.5, exceedance_flag=False, target=DAILY,
        ))
    db_session.commit()
    monkeypatch.setattr(forecasts_router, "datetime", fixed_datetime(day_start + timedelta(days=2)))
    from api.main import app

    body = TestClient(app).get(f"/api/v1/forecast/{station_id}/history", params={"lookback_days": 5}).json()
    assert body["target"] == DAILY and len(body["points"]) == 25
    assert body["points"][0]["forecast_value"] is None  # the 17th had no forecast
    assert {p["forecast_value"] for p in body["points"][1:]} == {90.0}
    assert body["points"][1]["actual"] == 60.0


def test_evaluation_scores_a_daily_forecast_against_the_days_mean(db_session, monkeypatch, daily_target):
    station_id = add_station(db_session, "eval")
    day_start = MIDNIGHT_18TH
    for h in range(24):
        add_reading(db_session, station_id, day_start + timedelta(minutes=30, hours=h), value=100.0 + (h % 2) * 40)  # mean 120
    model_id = add_model_run(db_session, station_id)
    db_session.add(Forecast(
        station_id=station_id, pollutant=Pollutant.PM25, model_id=model_id, forecast_made_at=day_start - timedelta(hours=3),
        target_time=day_start, horizon_hours=24, point_forecast=105.0, quantile_low=90.0, quantile_high=130.0,
        exceedance_probability=0.9, exceedance_flag=True, target=DAILY,
    ))
    # An hourly-family row for the same instant must not be scored.
    seed_forecast_run(db_session, station_id, day_start - timedelta(hours=24), horizons=(24,), model_id=model_id)
    db_session.commit()
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(day_start + timedelta(hours=25)))

    assert tasks.evaluate_recent_forecasts() == 1
    row = db_session.query(ExceedanceEvaluation).one()
    assert (row.horizon_hours, row.mae, row.recall, row.precision) == (24, pytest.approx(15.0), 1.0, 1.0)

    # The day is not over yet: nothing to score.
    db_session.query(ExceedanceEvaluation).delete()
    db_session.commit()
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(day_start + timedelta(hours=20)))
    assert tasks.evaluate_recent_forecasts() == 0


def test_digest_legend_explains_the_verdicts_of_the_family_being_served(daily_target, monkeypatch):
    from alerting.digest import _legend

    assert _legend() == {
        "go": "the day's average is expected to stay below Poor.",
        "caution": "the day's average is expected to be Poor.",
        "no-go": "the day's average is expected to be Very Poor or worse; outdoor practice not recommended.",
    }
    monkeypatch.setattr(get_settings(), "forecast_target", ForecastTarget.HOURLY.value)
    assert _legend()["go"] == "no health-threshold exceedance expected."


def test_switch_rehearsal_train_and_forecast_the_daily_family_while_the_hourly_one_is_served(db_session, monkeypatch, tmp_path):
    """The cutover: scripts/retrain_models.py --target daily_mean, then
    scripts/generate_forecasts.py --target daily_mean, then the config switch."""
    from models.train import load_model_config
    from scripts import retrain_models

    monkeypatch.setattr(get_settings(), "model_artifacts_dir", tmp_path)
    station_id, _, _, base, _ = _seed_full_dataset(db_session, level=200.0)
    config = load_model_config()
    config["training"] = {**config["training"], "holdout_days": 7}  # the seeded history is 16 days long
    monkeypatch.setattr(retrain_models, "load_model_config", lambda: config)
    now = NEWEST + timedelta(minutes=45)
    monkeypatch.setattr(tasks, "datetime", fixed_datetime(now))
    station = db_session.get(Station, station_id)

    hourly = retrain_models.retrain(db_session, True, [Pollutant.PM25], None, now=now)
    daily = retrain_models.retrain(db_session, True, [Pollutant.PM25], None, now=now, target=ForecastTarget.DAILY_MEAN)
    n = len(DEFAULT_HORIZONS_HOURS)
    assert hourly["activated"] == daily["activated"] == 4 * n  # neither run deactivated the other family's models
    assert db_session.query(ModelRun).filter_by(is_active=True).count() == 8 * n

    assert tasks.generate_forecasts()["forecasts_written"] == n
    assert tasks.generate_forecasts(ForecastTarget.DAILY_MEAN)["forecasts_written"] == n
    assert build_outlook(db_session, station, Pollutant.PM25, now=now).days[0].date.isoformat() == "2026-01-18"

    monkeypatch.setattr(get_settings(), "forecast_target", DAILY)  # the switch
    outlook = build_outlook(db_session, station, Pollutant.PM25, now=now)
    assert outlook.is_current and len(outlook.days) == n
    assert {d.aqi_category for d in outlook.days} == {"very_poor"} and outlook.overall_recommendation == "no-go"
