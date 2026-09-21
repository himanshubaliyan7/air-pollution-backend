"""Regression: a 6h ingestion window left permanent holes after a 13h scheduler
outage (2026-09-20/21). The default window must cover multi-hour outages."""

from datetime import datetime, timedelta, timezone

from common.constants import SensorSourceName
from db.models import Station


class _CapturingSource:
    captured = {}

    def __call__(self, api_key):
        return self

    def fetch_readings(self, *, source_location_ids, pollutants, start, end):
        self.captured.update(start=start, end=end, ids=list(source_location_ids))
        return []


def test_default_lookback_covers_a_long_outage(db_session, monkeypatch):
    db_session.add(Station(
        station_id="openaq:lb", name="LB", lat=28.6, lon=77.2, city="Delhi", state="Delhi",
        source=SensorSourceName.OPENAQ, source_location_id="lb", is_active=True,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    from orchestration.plugins.common import tasks

    source = _CapturingSource()
    monkeypatch.setitem(tasks.SENSOR_SOURCE_REGISTRY, tasks.ACTIVE_SOURCE, source)
    tasks.ingest_sensor_readings()

    window = source.captured["end"] - source.captured["start"]
    assert window >= timedelta(hours=48), window  # comfortably beyond an overnight gap
    assert source.captured["ids"] == ["lb"]
