"""Integration-test fixtures.

SAFETY: the autouse fixture below TRUNCATEs every application table after each
test. The application database now holds real production data (hundreds of
thousands of readings, trained models), so these tests run ONLY against a
dedicated database named "*_test", supplied via TEST_DATABASE_URL, and are
skipped otherwise. They never read DATABASE_URL. Set one up with:
    python -m scripts.setup_test_db
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


def _test_database_url() -> str | None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        return None
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise RuntimeError(
            f"Refusing to run integration tests: TEST_DATABASE_URL database {name!r} does not end in '_test'. "
            "These tests TRUNCATE all tables."
        )
    return url


_TEST_URL = _test_database_url()
if _TEST_URL:
    # Everything under test resolves its DB via get_settings()/get_engine();
    # point them at the test DB before any of that is cached.
    os.environ["DATABASE_URL"] = _TEST_URL
    from common.config import get_settings
    from db.session import get_engine, get_sessionmaker

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


@pytest.fixture(scope="session")
def db_engine():
    if not _TEST_URL:
        pytest.skip("TEST_DATABASE_URL not set (needs a dedicated '*_test' database; see tests/integration/conftest.py)")
    engine = create_engine(_TEST_URL)
    assert engine.url.database.endswith("_test")  # belt and braces before any TRUNCATE
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
    """Dedicated '*_test' DB only (enforced above), so a full truncate between
    tests is simplest and safest - avoids per-table column assumptions (e.g.
    raw_weather_readings has no station_id)."""
    yield
    with db_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {', '.join(_APP_TABLES)} CASCADE"))
