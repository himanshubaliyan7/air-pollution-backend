"""Hourly concentrations recovered from CPCB's CAAQMS feed, stored as readings.

The feed's Hourly_sub_index is the AQI sub-index of the latest hourly
concentration: inverted through the CPCB breakpoints it matches OpenAQ's hour
starting 1.5 h before `lastupdate` (PM2.5 median error 1.2 ug/m3, ratio 1.00;
scripts/cpcb_subindex_study.py, 2026-10-01). `lastupdate` is a whole IST hour
(:30 UTC) and our readings sit on the whole UTC hour, so lastupdate - 1.5 h
lands exactly on OpenAQ's stamp for the same measurement.

Stored with source CPCB; db/readings.py lets OpenAQ win any hour both hold,
so this only fills hours OpenAQ lacks (e.g. while its CPCB relay stalls).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from common.aqi import concentration_from_sub_index
from common.constants import Pollutant, SensorSourceName
from db.models import RawSensorReading, StationAqiSnapshot
from ingestion.units import CANONICAL_UNIT

logger = logging.getLogger(__name__)

FEED_POLLUTANTS = {"PM2.5": Pollutant.PM25, "NO2": Pollutant.NO2}
HOUR_START_OFFSET = timedelta(hours=1, minutes=30)
UPSERT_BATCH = 5000  # rows x 8 columns stays under Postgres's 65,535 bind parameters


def reading_rows(items, thresholds: dict, now: datetime | None = None) -> list[dict]:
    """raw_sensor_readings rows from (station_id, feed_pollutant_id, lastupdate,
    hourly_sub_index) tuples. Other pollutants and missing values are skipped.
    At the top of the scale (sub-index 500) the value is the band's upper
    bound, a floor: still above every no-go line, and marked in
    source_record_id."""
    now = now or datetime.now(timezone.utc)
    rows: dict[tuple, dict] = {}
    for station_id, pollutant_id, last_update, sub_index in items:
        pollutant = FEED_POLLUTANTS.get(pollutant_id)
        if pollutant is None or sub_index is None:
            continue
        value, capped = concentration_from_sub_index(thresholds, pollutant.value, sub_index)
        observed_at = (last_update - HOUR_START_OFFSET).replace(minute=0, second=0, microsecond=0)
        rows[(station_id, pollutant, observed_at)] = {
            "station_id": station_id,
            "pollutant": pollutant,
            "observed_at": observed_at,
            "source": SensorSourceName.CPCB,
            "value": value,
            "unit": CANONICAL_UNIT,
            "source_record_id": f"cpcb:{last_update:%Y-%m-%dT%H:%M}Z:{sub_index:g}" + (":capped" if capped else ""),
            "ingested_at": now,
        }
    return list(rows.values())


def load_cpcb_readings(session: Session, rows: list[dict]) -> int:
    """Idempotent upsert, like load_sensor_readings."""
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), UPSERT_BATCH):
        batch = rows[i:i + UPSERT_BATCH]
        stmt = insert(RawSensorReading).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["station_id", "pollutant", "observed_at", "source"],
            set_={k: stmt.excluded[k] for k in ("value", "unit", "source_record_id", "ingested_at")},
        )
        session.execute(stmt)
        written += len(batch)
    session.commit()
    return written


def readings_from_snapshots(session: Session, thresholds: dict, since: datetime) -> int:
    """Derives readings from the CAAQMS snapshots stored since `since` (already
    matched to our stations by load_aqi_snapshots). The hourly task passes a
    few hours back; a backfill passes an earlier date. Idempotent."""
    items = session.execute(
        select(StationAqiSnapshot.station_id, StationAqiSnapshot.pollutant_id,
               StationAqiSnapshot.source_updated_at, StationAqiSnapshot.sub_index_hourly).where(
            StationAqiSnapshot.pollutant_id.in_(list(FEED_POLLUTANTS)),
            StationAqiSnapshot.sub_index_hourly.isnot(None),  # only CPCB's direct feed sets it
            StationAqiSnapshot.source_updated_at >= since,
        )
    ).all()
    return load_cpcb_readings(session, reading_rows(items, thresholds))
