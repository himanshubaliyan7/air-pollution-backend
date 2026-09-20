from datetime import datetime, timedelta, timezone

from orchestration.plugins.common.tasks import summarize_data_age

NOW = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)


def test_buckets_stations_by_age_of_newest_reading():
    newest = {
        "a": NOW - timedelta(hours=2),
        "b": NOW - timedelta(hours=6),  # boundary is inclusive
        "c": NOW - timedelta(hours=20),
        "d": NOW - timedelta(hours=70),
        "e": NOW - timedelta(days=3, hours=1),
        "f": None,
    }
    assert summarize_data_age(newest, NOW) == {"<=6h": 2, "<=24h": 1, "<=72h": 1, ">72h": 1, "never": 1}


def test_empty_network():
    assert summarize_data_age({}, NOW) == {"<=6h": 0, "<=24h": 0, "<=72h": 0, ">72h": 0, "never": 0}
