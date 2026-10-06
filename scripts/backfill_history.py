"""Cold-start historical backfill: pulls as much OpenAQ + ERA5 history as
available for all seeded stations, so retraining_dag has something to train
on before the hourly ingestion_dag has had months to accumulate data
on its own.

Fetches in day-sized chunks (both to keep individual API requests small -
OpenAQ pagination and CDS request size limits - and so a failure partway
through a multi-month backfill doesn't lose already-fetched days).

Usage (in the container pipe the file instead: python - --days 180 < scripts/backfill_history.py):
    python -m scripts.backfill_history --days 180
    python -m scripts.backfill_history --days 180 --skip-weather   # OpenAQ only
    python -m scripts.backfill_history --days 30 --region mumbai   # OpenAQ API, that region's stations; no weather
    python -m scripts.backfill_history --weather-only --start 2025-09-25 --end 2026-03-01
        # ERA5 for a fixed window (sensor history for old windows: scripts.backfill_archive)
"""

import argparse
import logging
from datetime import date, datetime, timedelta, timezone

from common.config import get_settings
from common.constants import Pollutant
from common.logging_conf import configure_logging
from common.regions import select_stations
from db.session import get_session
from ingestion.config import ACTIVE_SOURCE, SENSOR_SOURCE_REGISTRY
from ingestion.loaders.sensor_loader import load_sensor_readings
from ingestion.loaders.weather_loader import load_weather_readings
from ingestion.weather.era5_client import ERA5Client
from ingestion.weather.grid import DELHI_NCR_AREA
from orchestration.plugins.common.tasks import _active_stations

logger = logging.getLogger(__name__)


def backfill_sensor_readings(days: int, chunk_days: int = 30, region_id: str | None = None) -> int:
    """chunk_days is a resilience checkpoint (a failure partway through only
    loses one chunk's progress), not a pagination necessity - OpenAQSource's
    own _paginate already handles arbitrarily long date ranges via the
    /sensors/{id}/hours endpoint's page/limit params. Smaller chunks
    multiply total HTTP requests for no benefit (each chunk re-issues one
    request per station/pollutant regardless of how many hours it covers),
    which is what actually drove OpenAQ rate limiting during a real backfill
    - 30 days keeps that multiplier low while still checkpointing progress
    a few times over a long backfill."""
    session = get_session()
    try:
        settings = get_settings()
        source_cls = SENSOR_SOURCE_REGISTRY[ACTIVE_SOURCE]
        source = source_cls(api_key=settings.openaq_api_key)

        stations = select_stations(_active_stations(session), region_id)
        if not stations:
            logger.warning("No active stations - run scripts/seed_stations.py first")
            return 0

        location_ids = [s.source_location_id for s in stations]
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

        total = 0
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            logger.info("Backfilling sensor readings %s .. %s", chunk_start, chunk_end)
            readings = source.fetch_readings(
                source_location_ids=location_ids, pollutants=list(Pollutant), start=chunk_start, end=chunk_end
            )
            total += load_sensor_readings(session, readings, ACTIVE_SOURCE)
            chunk_start = chunk_end
        return total
    finally:
        session.close()


def backfill_weather(days: int, chunk_days: int = 5, start: datetime | None = None, end: datetime | None = None) -> int:
    session = get_session()
    try:
        client = ERA5Client()
        end = end or datetime.now(timezone.utc)
        start = start or end - timedelta(days=days)

        total = 0
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            logger.info("Backfilling ERA5 %s .. %s", chunk_start, chunk_end)
            readings = client.fetch(chunk_start, chunk_end, area=DELHI_NCR_AREA)
            total += load_weather_readings(session, readings)
            chunk_start = chunk_end
        return total
    finally:
        session.close()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=180, help="How many days of history to backfill")
    parser.add_argument("--skip-weather", action="store_true", help="Skip the (slower) ERA5 backfill")
    parser.add_argument("--weather-only", action="store_true", help="Skip the OpenAQ API backfill")
    parser.add_argument("--start", type=date.fromisoformat, help="weather window start (with --end), UTC date")
    parser.add_argument("--end", type=date.fromisoformat, help="weather window end, exclusive")
    parser.add_argument("--region", help="only stations inside this region (config/regions.yaml id)")
    args = parser.parse_args()

    if not args.weather_only:
        n_sensor = backfill_sensor_readings(args.days, region_id=args.region)
        logger.info("Backfilled %d sensor readings", n_sensor)

    if args.region not in (None, "delhi-ncr") and not args.skip_weather:
        # ERA5 is fetched for the Delhi NCR area only (DELHI_NCR_AREA); another region has no weather.
        logger.warning("No weather backfill for region %s (Delhi NCR area only); skipping ERA5", args.region)
    elif not args.skip_weather:
        as_utc = lambda d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None
        n_weather = backfill_weather(args.days, start=as_utc(args.start), end=as_utc(args.end))
        logger.info("Backfilled %d weather readings", n_weather)


if __name__ == "__main__":
    main()
