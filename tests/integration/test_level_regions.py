"""The daily-mean ratios are fitted per region: one region's stations never
move another region's ratios, a region without enough history gets no forecast
(and loses any old one), and thresholds are read through the station's region."""

import dataclasses
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import yaml

from common import regions
from common.config import get_settings
from common.constants import DEFAULT_HORIZONS_HOURS, ForecastTarget, ModelType, Pollutant, SensorSourceName
from db.models import Feature, ModelRun, RawSensorReading, Station
from features.feature_store import get_feature_set_version
from models import exceedance, level, predict, registry
from models import train as train_module
from models.outlook import build_outlook
from models.train import train_station_pollutant_horizon
from tests.integration.forecast_helpers import add_station
from tests.integration.test_daily_verdicts import NEWEST, _daily_run, _train_all

UTC = timezone.utc
START = datetime(2026, 1, 1, tzinfo=UTC)
DAYS = 40
END = START + timedelta(days=DAYS)
DELHI_POINT = (28.6, 77.2)
MUMBAI_POINT = (19.07, 72.87)
QUANTILES = [0.1, 0.5, 0.9]
FIT_ARGS = (Pollutant.PM25, START, END, QUANTILES)


@pytest.fixture()
def two_regions(monkeypatch):
    """Delhi NCR from config/regions.yaml plus Mumbai (same zone and standard), whether or not
    the checked-in file already lists it."""
    delhi = regions.get_region("delhi-ncr")
    mumbai = dataclasses.replace(delhi, id="mumbai", name="Mumbai", bbox=(72.70, 18.80, 73.35, 19.55))
    monkeypatch.setattr(regions, "load_regions", lambda: (delhi, mumbai))
    return delhi, mumbai


def _station(db, name, point):
    return add_station(db, name, lat=point[0], lon=point[1])


def _seed_series(db, station_id, day_value):
    """Hourly readings for DAYS days, every hour of day d at day_value(d), and the
    features the fit reads: the mean of the trailing 24 hours."""
    hours = pd.date_range(START, periods=DAYS * 24, freq="h", tz="UTC")
    values = pd.Series([day_value(h // 24) for h in range(len(hours))], index=hours, dtype="float64")
    db.add_all(
        RawSensorReading(station_id=station_id, pollutant=Pollutant.PM25, observed_at=t.to_pydatetime(),
                         source=SensorSourceName.OPENAQ, value=float(v), unit="ug/m3", source_record_id=None,
                         ingested_at=START)
        for t, v in values.items()
    )
    rolling = values.rolling(24, min_periods=24).mean().dropna()
    db.add_all(
        Feature(station_id=station_id, pollutant=Pollutant.PM25, feature_time=t.to_pydatetime(),
                feature_set_version=get_feature_set_version(), features={level.LEVEL_FEATURE: float(v)}, computed_at=START)
        for t, v in rolling.items()
    )
    db.commit()


def _delhi_day(d):  # swings by day
    return 80.0 + 60.0 * math.sin(d * 0.9)


def _mumbai_day(d):  # a flatter, lower city
    return 30.0 + 4.0 * math.sin(d * 0.5)


@pytest.fixture()
def delhi_only(db_session, two_regions):
    for i in range(3):
        _seed_series(db_session, _station(db_session, f"d{i}", DELHI_POINT), lambda d, i=i: _delhi_day(d) + 5 * i)
    return db_session


def _quantiles(fit):
    return {h: f.quantiles for h, f in fit.items()}


def test_each_region_gets_its_own_quantiles_and_one_does_not_move_the_other(delhi_only):
    db = delhi_only
    delhi_before = _quantiles(level.fit_log_ratio_quantiles(db, *FIT_ARGS, region_id="delhi-ncr"))
    for i in range(2):
        _seed_series(db, _station(db, f"m{i}", MUMBAI_POINT), lambda d, i=i: _mumbai_day(d) + i)
    level.clear_cache()  # a fresh run, now that Mumbai stations exist

    delhi_after = level.fit_log_ratio_quantiles(db, *FIT_ARGS, region_id="delhi-ncr")
    mumbai = level.fit_log_ratio_quantiles(db, *FIT_ARGS, region_id="mumbai")
    assert _quantiles(delhi_after) == delhi_before  # Mumbai's data did not move Delhi's ratios
    assert {f.stations for f in delhi_after.values()} == {3} and {f.stations for f in mumbai.values()} == {2}
    assert set(mumbai) == set(DEFAULT_HORIZONS_HOURS)
    for horizon in DEFAULT_HORIZONS_HOURS:
        assert abs(mumbai[horizon].quantiles[0.9] - mumbai[horizon].quantiles[0.1]) < \
               abs(delhi_after[horizon].quantiles[0.9] - delhi_after[horizon].quantiles[0.1])  # Mumbai is flatter
    # The old pooled fit would have moved Delhi's numbers: that is the problem being solved.
    pooled = level.fit_log_ratio_quantiles(db, *FIT_ARGS)
    assert _quantiles(pooled) != delhi_before


def test_delhi_alone_with_the_regional_fit_equals_the_old_pooled_fit(delhi_only):
    """Nothing but Delhi stations exist: the per-region fit must reproduce the fit
    that produced today's live ratios (region_id=None is that code path, unfiltered)."""
    regional = level.fit_log_ratio_quantiles(delhi_only, *FIT_ARGS, region_id="delhi-ncr")
    level.clear_cache()
    pooled = level.fit_log_ratio_quantiles(delhi_only, *FIT_ARGS)
    assert regional == pooled and regional  # quantiles, rows, days and stations, exactly


def test_the_cache_is_keyed_by_region(delhi_only, two_regions):
    db = delhi_only
    _seed_series(db, _station(db, "m0", MUMBAI_POINT), _mumbai_day)
    delhi = level.fit_log_ratio_quantiles(db, *FIT_ARGS, region_id="delhi-ncr")
    mumbai = level.fit_log_ratio_quantiles(db, *FIT_ARGS, region_id="mumbai")
    assert delhi is not mumbai and _quantiles(delhi) != _quantiles(mumbai)
    assert {key[0] for key in level._cache} == {"delhi-ncr", "mumbai"}
    assert level.fit_log_ratio_quantiles(db, *FIT_ARGS, region_id="mumbai") is mumbai  # served from the cache
    assert level.fit_log_ratio_quantiles(db, *FIT_ARGS, region_id="delhi-ncr") is delhi


def _floors(monkeypatch, days, stations):
    real = train_module.load_model_config

    def config():
        loaded = real()
        return {**loaded, "training": {**loaded["training"], "min_level_days": days, "min_level_stations": stations}}

    monkeypatch.setattr(train_module, "load_model_config", config)


def _train_level(db, station_id, horizon=24):
    return train_station_pollutant_horizon(
        db, station_id, Pollutant.PM25, horizon, START, END, holdout_days=7, target=ForecastTarget.DAILY_MEAN)


def _active_level_rows(db, station_id):
    return db.query(ModelRun).filter_by(station_id=station_id, is_active=True, target=ForecastTarget.DAILY_MEAN.value).count()


def test_a_region_below_the_floor_gets_no_rows_and_loses_its_old_ones_and_never_borrows(delhi_only, two_regions, monkeypatch):
    db = delhi_only
    mumbai_id = _station(db, "m0", MUMBAI_POINT)
    _seed_series(db, mumbai_id, _mumbai_day)
    # An earlier fit left active rows on the Mumbai station (say, from a time the floor was lower).
    _floors(monkeypatch, days=20, stations=1)
    assert len(_train_level(db, mumbai_id)) == 3 and _active_level_rows(db, mumbai_id) == 3
    _floors(monkeypatch, days=20, stations=2)  # Delhi has 3 stations and ~35 days; Mumbai one station
    level.clear_cache()

    assert _train_level(db, mumbai_id) == []  # one Mumbai station is below the floor of two
    assert _active_level_rows(db, mumbai_id) == 0  # and the old fit is switched off, not served
    delhi_id = "openaq:d0"
    delhi_ids = _train_level(db, delhi_id)
    assert len(delhi_ids) == 3
    row = db.get(ModelRun, delhi_ids[0])
    assert row.hyperparams["region"] == "delhi-ncr" and row.metrics["stations"] == 3 and row.metrics["days"] >= 20


def test_too_few_days_and_a_station_outside_every_region_get_nothing(delhi_only, monkeypatch):
    db = delhi_only
    _floors(monkeypatch, days=DAYS + 100, stations=1)
    assert _train_level(db, "openaq:d0") == []
    _floors(monkeypatch, days=1, stations=1)
    level.clear_cache()
    nowhere = _station(db, "nowhere", (0.0, 0.0))  # in no region's bounding box
    _seed_series(db, nowhere, _delhi_day)
    assert _train_level(db, nowhere) == []  # never pooled in with a region it is not in
    assert _train_level(db, "openaq:d0")  # while the same floors let Delhi through


def test_too_little_history_deactivates_the_old_fit_and_the_outlook_reads_no_data(db_session, monkeypatch, tmp_path, daily_target):
    station_id, lat, lon = _train_all(db_session, monkeypatch, tmp_path, level=200.0)
    assert registry.get_active_models(db_session, station_id, Pollutant.PM25, 24, ModelType.QUANTILE_REGRESSOR)
    assert predict.forecast(db_session, station_id, Pollutant.PM25, 24, NEWEST, station_lat=lat, station_lon=lon)

    _floors(monkeypatch, days=365, stations=1)  # the 16 seeded days are no longer enough
    level.clear_cache()
    for horizon in DEFAULT_HORIZONS_HOURS:
        assert train_station_pollutant_horizon(
            db_session, station_id, Pollutant.PM25, horizon, START, NEWEST, holdout_days=7) == []
    assert _active_level_rows(db_session, station_id) == 0
    assert predict.forecast(db_session, station_id, Pollutant.PM25, 24, NEWEST, station_lat=lat, station_lon=lon) is None

    outlook = build_outlook(db_session, db_session.get(Station, station_id), Pollutant.PM25, now=NEWEST + timedelta(minutes=45))
    assert outlook.overall_recommendation == "no-data" and not outlook.is_current and outlook.days == []


def _mumbai_thresholds_file(tmp_path):
    """The global file, except that only Severe is no-go."""
    path = tmp_path / "thresholds_mumbai_test.yaml"
    loaded = exceedance.load_thresholds()
    path.write_text(yaml.safe_dump({**loaded, "no_go_category": "severe"}), encoding="utf-8")
    return path


def test_thresholds_come_from_the_stations_region(db_session, monkeypatch, tmp_path, two_regions, daily_target):
    delhi, mumbai = two_regions
    monkeypatch.setattr(regions, "load_regions", lambda: (delhi, dataclasses.replace(
        mumbai, thresholds_config=str(_mumbai_thresholds_file(tmp_path)))))
    global_thresholds = exceedance.load_thresholds()
    assert exceedance.thresholds_for_point(*DELHI_POINT) == global_thresholds
    assert exceedance.thresholds_for_point(*MUMBAI_POINT)["no_go_category"] == "severe"
    assert exceedance.thresholds_for_point(0.0, 0.0) == global_thresholds  # outside every region: the global file

    # The same very poor forecast: no-go in Delhi, caution where the region's file says so.
    verdicts = {}
    for name, point in (("delhi", DELHI_POINT), ("mumbai", MUMBAI_POINT)):
        station_id = add_station(db_session, f"th-{name}", lat=point[0], lon=point[1])
        _daily_run(db_session, station_id, NEWEST, [(130, 180, 240)] * 5)
        outlook = build_outlook(db_session, db_session.get(Station, station_id), Pollutant.PM25, now=NEWEST + timedelta(hours=1))
        verdicts[name] = {d.verdict for d in outlook.days}
    assert verdicts == {"delhi": {"no-go"}, "mumbai": {"caution"}}
