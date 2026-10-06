"""One value per hour from raw_sensor_readings, whichever sources hold it.

OpenAQ and CPCB (hourly concentrations recovered from CPCB's CAAQMS feed) can
both hold the same station-hour. Every consumer - features, training targets,
forecast evaluation, the API's history chart - must pick the same value, so
the rule lives here: the higher-precedence source wins, CPCB only fills hours
OpenAQ lacks. The models were trained on OpenAQ data; CPCB matched it with a
median error of 1.2 ug/m3 for PM2.5 (scripts/cpcb_subindex_study.py).

The same rule drops instrument faults (common.constants.PLAUSIBLE_RANGE), so
no consumer ever sees one.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from common.constants import PLAUSIBLE_RANGE, Pollutant, SensorSourceName
from db.models import RawSensorReading

SOURCE_PRECEDENCE = {SensorSourceName.OPENAQ: 0, SensorSourceName.CPCB: 1}  # lower wins


def is_plausible(pollutant: Pollutant, value: float) -> bool:
    """False for an instrument fault (negative, or beyond PLAUSIBLE_RANGE).
    Such an hour counts as missing, so the other source may fill it."""
    low, high = PLAUSIBLE_RANGE[pollutant]
    return low <= value <= high


def one_value_per_hour(rows) -> list[tuple[datetime, float]]:
    """(observed_at, value) sorted by time, from (observed_at, value, source)
    rows: the highest-precedence source for each hour."""
    best: dict[datetime, tuple[int, float]] = {}
    for observed_at, value, source in rows:
        rank = SOURCE_PRECEDENCE.get(source, len(SOURCE_PRECEDENCE))
        if observed_at not in best or rank < best[observed_at][0]:
            best[observed_at] = (rank, value)
    return [(t, best[t][1]) for t in sorted(best)]


def hourly_readings(
    session: Session, station_id: str, pollutant: Pollutant, start: datetime, end: datetime | None = None
) -> list[tuple[datetime, float]]:
    conditions = [
        RawSensorReading.station_id == station_id,
        RawSensorReading.pollutant == pollutant,
        RawSensorReading.observed_at >= start,
    ]
    if end is not None:
        conditions.append(RawSensorReading.observed_at <= end)
    rows = session.execute(
        select(RawSensorReading.observed_at, RawSensorReading.value, RawSensorReading.source).where(*conditions)
    ).all()
    return one_value_per_hour([r for r in rows if is_plausible(pollutant, r.value)])


def hourly_readings_by_station(
    session: Session, pollutant: Pollutant, start: datetime
) -> dict[str, list[tuple[datetime, float]]]:
    """hourly_readings for every station at once, in one query."""
    rows = session.execute(
        select(
            RawSensorReading.station_id, RawSensorReading.observed_at, RawSensorReading.value, RawSensorReading.source
        ).where(RawSensorReading.pollutant == pollutant, RawSensorReading.observed_at >= start)
    ).all()
    by_station: dict[str, list] = {}
    for station_id, observed_at, value, source in rows:
        if is_plausible(pollutant, value):
            by_station.setdefault(station_id, []).append((observed_at, value, source))
    return {station_id: one_value_per_hour(station_rows) for station_id, station_rows in by_station.items()}
