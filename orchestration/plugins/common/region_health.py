"""Reads the watchdog's per-region inputs from the database (the rules themselves,
and why they are per region, are in watchdog.py)."""

from datetime import datetime, timedelta

from sqlalchemy import func, select

from common.config import forecast_target
from common.constants import MAX_INPUT_STALENESS_HOURS, SensorSourceName
from common.freshness import floor_hour
from common.regions import region_for_point
from db.models import Forecast, ModelRun, RawSensorReading, Station, StationAqiSnapshot
from orchestration.plugins.common.watchdog import RegionHealth

SNAPSHOT_FRESH_WITHIN = timedelta(hours=3)


def _newest(session, station_col, time_col, *where) -> dict[str, datetime]:
    return dict(session.execute(select(station_col, func.max(time_col)).where(*where).group_by(station_col)).all())


def _station_ids(session, station_col, *where) -> set[str]:
    return set(session.execute(select(func.distinct(station_col)).where(*where)).scalars())


def collect_region_health(session, now: datetime, regions) -> list[RegionHealth]:
    """One RegionHealth per region. Station-level answers are grouped into regions in
    Python (there is no region column; a station's region is its position)."""
    region_of = {}
    active: dict[str, int] = {r.id: 0 for r in regions}
    for sid, lat, lon, is_active in session.execute(
        select(Station.station_id, Station.lat, Station.lon, Station.is_active)
    ):
        region = region_for_point(lat, lon)
        if region is not None:
            region_of[sid] = region.id
            active[region.id] += 1 if is_active else 0

    # Same cutoff as common.freshness.is_input_fresh, so "current" matches what the API serves.
    cutoff = floor_hour(now) - timedelta(hours=MAX_INPUT_STALENESS_HOURS)
    snapshots = _newest(session, StationAqiSnapshot.station_id, StationAqiSnapshot.source_updated_at)
    # OpenAQ only: ingestion_dag's own feed. CPCB readings (from current_aqi_dag) would
    # otherwise hide a broken ingestion run or a stalled OpenAQ relay.
    sensors = _newest(
        session, RawSensorReading.station_id, RawSensorReading.observed_at,
        RawSensorReading.source == SensorSourceName.OPENAQ,
    )
    forecast = _station_ids(
        session, Forecast.station_id,
        Forecast.forecast_made_at >= cutoff, Forecast.target_time >= cutoff,  # target_time prunes hypertable chunks
    )
    fitted = _station_ids(session, ModelRun.station_id, ModelRun.target == forecast_target().value)
    fresh_input = _station_ids(session, RawSensorReading.station_id, RawSensorReading.observed_at >= cutoff)

    out = []
    for r in regions:
        mine = lambda sid, r=r: region_of.get(sid) == r.id  # noqa: E731
        snap = {s: t for s, t in snapshots.items() if mine(s)}
        sens = [t for s, t in sensors.items() if mine(s)]
        out.append(RegionHealth(
            region_id=r.id, name=r.name, active_stations=active[r.id],
            newest_snapshot=max(snap.values(), default=None),
            stations_with_fresh_snapshot=sum(1 for t in snap.values() if t >= now - SNAPSHOT_FRESH_WITHIN),
            newest_sensor_reading=max(sens, default=None),
            stations_with_current_forecast=sum(1 for s in forecast if mine(s)),
            stations_with_fresh_input=sum(1 for s in fresh_input if mine(s)),
            has_forecast_models=any(mine(s) for s in fitted),
        ))
    return out
