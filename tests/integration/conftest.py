import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from common.config import get_settings


@pytest.fixture(scope="session")
def db_engine():
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine, expire_on_commit=False)
    session = Session()
    yield session
    session.rollback()
    session.close()


_APP_TABLES = [
    "alert_log",
    "alert_subscriptions",
    "exceedance_evaluations",
    "forecasts",
    "model_runs",
    "features",
    "raw_weather_readings",
    "raw_sensor_readings",
    "stations",
]


@pytest.fixture(autouse=True)
def _clean_tables(db_engine):
    """This DB is dedicated dev/test infra with no real data yet, so a full
    truncate between tests is simplest and safest - avoids per-table column
    assumptions (e.g. raw_weather_readings has no station_id)."""
    yield
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {', '.join(_APP_TABLES)} CASCADE"))
