"""Cold-start historical backfill: pulls as much OpenAQ + ERA5 history as
available for all seeded stations, so retraining_dag has something to train
on before the hourly ingestion_dag has had months to accumulate data
on its own.

Fetches in day-sized chunks (both to keep individual API requests small -
OpenAQ pagination and CDS request size limits - and so a failure partway
through a multi-month backfill doesn't lose already-fetched days).

Usage:
    python -m scripts.backfill_history --days 180
    python -m scripts.backfill_history --days 180 --skip-weather   # OpenAQ only
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone

from common.config import get_settings
from common.constants import Pollutant
from common.logging_conf import configure_logging
from db.session import get_session
from ingestion.config import ACTIVE_SOURCE, SENSOR_SOURCE_REGISTRY
from ingestion.loaders.sensor_loader import load_sensor_readings
from ingestion.loaders.weather_loader import load_weather_readings
from ingestion.weather.era5_client import ERA5Client
from ingestion.weather.grid import DELHI_NCR_AREA
from orchestration.plugins.common.tasks import _active_stations

logger = logging.getLogger(__name__)


def backfill_sensor_readings(days: int, chunk_days: int = 7) -> int:
    session = get_session()
    try:
        settings = get_settings()
        source_cls = SENSOR_SOURCE_REGISTRY[ACTIVE_SOURCE]
        source = source_cls(api_key=settings.openaq_api_key)

        stations = _active_stations(session)
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


def backfill_weather(days: int, chunk_days: int = 5) -> int:
    session = get_session()
    try:
        client = ERA5Client()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

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
    args = parser.parse_args()

    n_sensor = backfill_sensor_readings(args.days)
    logger.info("Backfilled %d sensor readings", n_sensor)

    if not args.skip_weather:
        n_weather = backfill_weather(args.days)
        logger.info("Backfilled %d weather readings", n_weather)


if __name__ == "__main__":
    main()
