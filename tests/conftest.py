import pytest

from common.config import get_settings
from common.constants import ForecastTarget


@pytest.fixture(autouse=True)
def fresh_level_ratios():
    """models.level caches its pooled fit per training window; tests reuse windows."""
    from models import level

    level.clear_cache()
    yield
    level.clear_cache()


@pytest.fixture(autouse=True)
def hourly_target(monkeypatch):
    """Most tests were written for the hourly model family and seed its rows.
    Pin it, so they do not depend on what config/settings.yaml serves today."""
    monkeypatch.setattr(get_settings(), "forecast_target", ForecastTarget.HOURLY.value)


@pytest.fixture()
def daily_target(hourly_target, monkeypatch):
    """Serve the daily-mean family (graded verdicts) for this test."""
    monkeypatch.setattr(get_settings(), "forecast_target", ForecastTarget.DAILY_MEAN.value)
