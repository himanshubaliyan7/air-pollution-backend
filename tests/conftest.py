import pytest

from common.config import get_settings
from common.constants import ForecastTarget


@pytest.fixture(autouse=True)
def fresh_level_ratios():
    """models.level caches its per-region fit per training window; tests reuse windows."""
    from models import level

    level.clear_cache()
    yield
    level.clear_cache()


@pytest.fixture(autouse=True)
def small_level_floor(monkeypatch):
    """The seeded histories are days long and have one or a few stations; the
    production floor for a region's level ratios (180 days, 3 stations) would
    refuse every fit. Tests of the floor itself set their own values."""
    from models import train

    real = train.load_model_config

    def config() -> dict:
        loaded = real()
        return {**loaded, "training": {**loaded["training"], "min_level_days": 1, "min_level_stations": 1}}

    monkeypatch.setattr(train, "load_model_config", config)


@pytest.fixture(autouse=True)
def hourly_target(monkeypatch):
    """Most tests were written for the hourly model family and seed its rows.
    Pin it, so they do not depend on what config/settings.yaml serves today."""
    monkeypatch.setattr(get_settings(), "forecast_target", ForecastTarget.HOURLY.value)


@pytest.fixture()
def daily_target(hourly_target, monkeypatch):
    """Serve the daily-mean family (graded verdicts) for this test."""
    monkeypatch.setattr(get_settings(), "forecast_target", ForecastTarget.DAILY_MEAN.value)
