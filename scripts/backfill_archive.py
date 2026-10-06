"""Backfill sensor history from OpenAQ's public S3 archive (no API quota used).

For each active station, uses the same sensor per pollutant the live pipeline
ingests (read from the stored source_record_id), so history and live data
come from one instrument. Rows are upserted exactly like hourly ingestion.

Usage (inside the Airflow scheduler container):
    python -m scripts.backfill_archive --start 2025-10-01 --end 2026-03-23 --dry-run
    python -m scripts.backfill_archive --start 2025-10-01 --end 2026-03-23
    python -m scripts.backfill_archive --region mumbai --start 2026-04-01 --end 2026-09-30 --dry-run
    python -m scripts.backfill_archive --station openaq:8118 --start 2026-04-01 --end 2026-04-30

A station needs a stored OpenAQ reading first (that is where its sensor ids come
from), so a newly seeded station is skipped until one hourly ingestion has run.
"""

import argparse
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from sqlalchemy import func, select

from common.constants import Pollutant, SensorSourceName
from common.logging_conf import configure_logging
from common.regions import select_stations
from db.models import RawSensorReading
from db.session import get_session
from ingestion.config import ACTIVE_SOURCE
from ingestion.loaders.sensor_loader import load_sensor_readings
from ingestion.sources.openaq_archive import OpenAQArchiveClient, parse_day
from orchestration.plugins.common.tasks import _active_stations

logger = logging.getLogger(__name__)


def live_sensor_ids(session) -> dict[str, dict[Pollutant, int]]:
    """station_id -> {pollutant: sensor id} from the newest stored OpenAQ reading.
    Only OpenAQ rows carry a sensor id ("<sensor>:<time>"); a CPCB-sourced row's
    source_record_id starts with "cpcb:" and is not one."""
    is_openaq = RawSensorReading.source == SensorSourceName.OPENAQ
    newest = (
        select(RawSensorReading.station_id, RawSensorReading.pollutant, func.max(RawSensorReading.observed_at).label("t"))
        .where(is_openaq)
        .group_by(RawSensorReading.station_id, RawSensorReading.pollutant)
        .subquery()
    )
    rows = session.execute(
        select(RawSensorReading.station_id, RawSensorReading.pollutant, RawSensorReading.source_record_id)
        .join(
            newest,
            (RawSensorReading.station_id == newest.c.station_id)
            & (RawSensorReading.pollutant == newest.c.pollutant)
            & (RawSensorReading.observed_at == newest.c.t),
        )
        .where(is_openaq)
    ).all()
    out: dict[str, dict[Pollutant, int]] = {}
    for station_id, pollutant, record_id in rows:
        sensor_id = (record_id or "").split(":", 1)[0]
        if sensor_id.isdigit():
            out.setdefault(station_id, {})[pollutant] = int(sensor_id)
    return out


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="inclusive")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report coverage, write nothing")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--region", help="only stations inside this region (config/regions.yaml id)")
    parser.add_argument("--station", action="append", help="only this station_id (repeatable)")
    args = parser.parse_args()

    session = get_session()
    try:
        stations = select_stations(_active_stations(session), args.region, args.station)
        logger.info("%d stations selected", len(stations))
        sensors = live_sensor_ids(session)
        client = OpenAQArchiveClient()
        days = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
        coverage: Counter = Counter()
        total = 0
        for station in stations:
            sensor_ids = sensors.get(station.station_id)
            if not sensor_ids:
                logger.warning("%s: no stored sensor id, skipped", station.station_id)
                continue
            loc = station.source_location_id
            with ThreadPoolExecutor(args.workers) as pool:
                texts = list(pool.map(lambda d: client.fetch_day_csv(loc, d), days))
            readings = [r for t in texts if t for r in parse_day(t, loc, sensor_ids)]
            for r in readings:
                coverage[(station.station_id, r.pollutant.value)] += 1
            if not args.dry_run and readings:
                total += load_sensor_readings(session, readings, ACTIVE_SOURCE)
            logger.info("%s: %d readings from %d/%d day files", station.station_id, len(readings), sum(1 for t in texts if t), len(days))

        hours = len(days) * 24
        for pollutant in Pollutant:
            got = sorted((n for (s, p), n in coverage.items() if p == pollutant.value), reverse=True)
            good = sum(1 for n in got if n >= 0.5 * hours)
            print(f"{pollutant.value}: {len(got)} stations with data, {good} with >=50% of {hours} hours, total rows {sum(got)}")
        print(f"{'DRY RUN - nothing written' if args.dry_run else f'upserted {total} rows'}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
