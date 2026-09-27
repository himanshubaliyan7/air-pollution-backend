"""Hourly checks on the DATA, not just on whether jobs ran.

A job can succeed and still deliver nothing (OpenAQ returned 401 for 6+ hours while
every run reported success, 2026-09-21). These invariants catch that.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

# How stale the current-conditions feed may get before we complain (it publishes ~1-1.5h
# after each hour, so 3h means at least one missed publication).
MAX_SNAPSHOT_AGE = timedelta(hours=3)
# Fewer stations than this with a fresh reading means the feed or the matching broke
# (71 stations matched on 2026-09-21).
MIN_CURRENT_AQI_STATIONS = 20
MAX_SENSOR_AGE = timedelta(hours=24)
# Fewer stations than this with a current forecast means most schools see only no-data
# (73 on 2026-09-25). The newest-reading check above misses this: during the CPCB outage
# from 2026-09-25, 7 non-CPCB stations stayed fresh while 75 went silent.
MIN_FORECAST_STATIONS = 30


@dataclass(frozen=True)
class Issue:
    key: str  # stable id, used to de-duplicate repeat alerts
    message: str


def evaluate_health(
    now: datetime,
    *,
    newest_snapshot: datetime | None,
    stations_with_fresh_snapshot: int,
    sensor_ingestion_enabled: bool,
    newest_sensor_reading: datetime | None,
    stations_with_current_forecast: int,
    stations_with_fresh_input: int,
) -> list[Issue]:
    issues: list[Issue] = []

    # Always checked: the primary current-AQI source (CPCB's own feed) needs no key.
    if newest_snapshot is None or now - newest_snapshot > MAX_SNAPSHOT_AGE:
        age = "never" if newest_snapshot is None else f"{(now - newest_snapshot).total_seconds() / 3600:.1f}h old"
        issues.append(Issue("aqi-feed-stale", f"Current-AQI feed (CPCB) is stale: newest snapshot {age}."))
    elif stations_with_fresh_snapshot < MIN_CURRENT_AQI_STATIONS:
        issues.append(Issue(
            "aqi-coverage-low",
            f"Only {stations_with_fresh_snapshot} stations have a fresh CPCB reading (expected >= {MIN_CURRENT_AQI_STATIONS}).",
        ))

    # Skipped while sensor ingestion is deliberately paused (e.g. OpenAQ suspended).
    if sensor_ingestion_enabled and (newest_sensor_reading is None or now - newest_sensor_reading > MAX_SENSOR_AGE):
        age = "none" if newest_sensor_reading is None else f"{(now - newest_sensor_reading).total_seconds() / 3600:.0f}h old"
        issues.append(Issue("sensor-data-stale", f"No new sensor readings while ingestion is enabled: newest {age}."))

    if sensor_ingestion_enabled and stations_with_current_forecast < MIN_FORECAST_STATIONS:
        cause = (
            "inputs are missing upstream" if stations_with_fresh_input < MIN_FORECAST_STATIONS
            else "inputs are fresh, so the forecast pipeline itself is failing"
        )
        issues.append(Issue(
            "forecast-coverage-low",
            f"Only {stations_with_current_forecast} stations have a current forecast (expected >= {MIN_FORECAST_STATIONS}); "
            f"{stations_with_fresh_input} have a sensor reading within 24h - {cause}.",
        ))

    return issues
