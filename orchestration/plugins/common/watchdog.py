"""Hourly checks on the DATA, not just on whether jobs ran.

A job can succeed and still deliver nothing (OpenAQ returned 401 for 6+ hours while
every run reported success, 2026-09-21). These invariants catch that.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

# How stale the current-conditions feed may get before we complain (it publishes ~1-1.5h
# after each hour, so 3h means at least one missed publication).
MAX_SNAPSHOT_AGE = timedelta(hours=3)
MAX_SENSOR_AGE = timedelta(hours=24)

# The region whose issue keys stay bare (un-suffixed). Pinned to this id, not to "the first
# region in regions.yaml": issues open in production use the bare keys (aqi-feed-stale ...),
# and reordering regions.yaml must not rename them and strand those open issues.
DEFAULT_REGION_ID = "delhi-ncr"

# Count rules are per region and scale with the stations the region has active, so one
# live city cannot hide a dark one. The fractions reproduce the fixed minimums used
# before regions (20 and 30) at Delhi's 82 active stations: floor(0.25 * 82) = 20,
# floor(0.37 * 82) = 30. The floor keeps a small region from alarming over one or two
# stations; a region never needs more stations than it has.
# - current AQI: fewer stations than this with a fresh CPCB reading means the feed or the
#   matching broke (71 stations matched on 2026-09-21).
# - forecast: fewer stations than this with a current forecast means most schools see only
#   no-data (73 on 2026-09-25). The newest-reading check misses this: during the CPCB
#   outage from 2026-09-25, 7 non-CPCB stations stayed fresh while 75 went silent.
MIN_CURRENT_AQI_FRACTION = 0.25
MIN_FORECAST_FRACTION = 0.37
MIN_STATIONS_FLOOR = 3

_BASE_KEYS = ("aqi-feed-stale", "aqi-coverage-low", "sensor-data-stale", "forecast-coverage-low")


def min_stations(active: int, fraction: float) -> int:
    return min(active, max(MIN_STATIONS_FLOOR, math.floor(fraction * active)))


@dataclass(frozen=True)
class Issue:
    key: str  # stable id, used to de-duplicate repeat alerts
    message: str


@dataclass(frozen=True)
class RegionHealth:
    """One region's inputs to the checks. None means "no such reading", never "fine"."""

    region_id: str
    name: str
    active_stations: int
    newest_snapshot: datetime | None
    stations_with_fresh_snapshot: int
    newest_sensor_reading: datetime | None
    stations_with_current_forecast: int
    stations_with_fresh_input: int
    # Whether any station of the region has ever been fitted for the served forecast family
    # (a model row, active or not). A region never fitted has no forecasts to be missing;
    # one whose models were all switched off still does, so it keeps alarming.
    has_forecast_models: bool = True


def issue_key(base: str, region_id: str, default_region_id: str = DEFAULT_REGION_ID) -> str:
    """The default region (Delhi NCR, the only region before 2026-10) keeps the bare
    key, so an issue already open at deploy is the same issue afterwards."""
    return base if region_id == default_region_id else f"{base}:{region_id}"


def all_issue_keys(region_ids: Sequence[str]) -> list[str]:
    """Every key evaluate_health can raise, for sending the "resolved" message."""
    return [issue_key(b, r) for r in region_ids for b in _BASE_KEYS]


def _age_hours(now: datetime, ts: datetime) -> float:
    return (now - ts).total_seconds() / 3600


def _evaluate_region(now: datetime, r: RegionHealth, key, sensor_ingestion_enabled: bool) -> list[Issue]:
    issues: list[Issue] = []
    min_aqi = min_stations(r.active_stations, MIN_CURRENT_AQI_FRACTION)
    min_forecast = min_stations(r.active_stations, MIN_FORECAST_FRACTION)

    # Always checked: the primary current-AQI source (CPCB's own feed) needs no key.
    if r.newest_snapshot is None or now - r.newest_snapshot > MAX_SNAPSHOT_AGE:
        age = "never" if r.newest_snapshot is None else f"{_age_hours(now, r.newest_snapshot):.1f}h old"
        issues.append(Issue(key("aqi-feed-stale"), f"{r.name}: current-AQI feed (CPCB) is stale: newest snapshot {age}."))
    elif r.stations_with_fresh_snapshot < min_aqi:
        issues.append(Issue(
            key("aqi-coverage-low"),
            f"{r.name}: only {r.stations_with_fresh_snapshot} of {r.active_stations} stations have a fresh CPCB reading "
            f"(expected >= {min_aqi}).",
        ))

    # Skipped while sensor ingestion is deliberately paused (e.g. OpenAQ suspended).
    if sensor_ingestion_enabled and (r.newest_sensor_reading is None or now - r.newest_sensor_reading > MAX_SENSOR_AGE):
        age = "none" if r.newest_sensor_reading is None else f"{_age_hours(now, r.newest_sensor_reading):.0f}h old"
        issues.append(Issue(
            key("sensor-data-stale"), f"{r.name}: no new sensor readings while ingestion is enabled: newest {age}."
        ))

    if sensor_ingestion_enabled and r.has_forecast_models and r.stations_with_current_forecast < min_forecast:
        cause = (
            "inputs are missing upstream" if r.stations_with_fresh_input < min_forecast
            else "inputs are fresh, so the forecast pipeline itself is failing"
        )
        issues.append(Issue(
            key("forecast-coverage-low"),
            f"{r.name}: only {r.stations_with_current_forecast} of {r.active_stations} stations have a current forecast "
            f"(expected >= {min_forecast}); {r.stations_with_fresh_input} have a sensor reading within 24h - {cause}.",
        ))
    return issues


def evaluate_health(
    now: datetime,
    *,
    regions: Sequence[RegionHealth],
    sensor_ingestion_enabled: bool,
    default_region_id: str | None = None,
) -> list[Issue]:
    """Issues across regions. DEFAULT_REGION_ID keeps the bare issue keys unless another is
    named. A region with no active stations raises nothing: there is nothing to watch."""
    default = default_region_id or DEFAULT_REGION_ID
    issues: list[Issue] = []
    for r in regions:
        if r.active_stations == 0:
            continue
        key = lambda base, r=r: issue_key(base, r.region_id, default)  # noqa: E731
        issues.extend(_evaluate_region(now, r, key, sensor_ingestion_enabled))
    return issues
