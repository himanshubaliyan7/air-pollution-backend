"""Recompute feature rows over a past window (all active stations).

The hourly feature_engineering_dag rewrites only the last 72 h (6 h until
2026-10-01). Run this after anything that adds or changes older readings - a
backfill, a station merge, a unit repair - or those hours never reach training,
which reads only the `features` table. Idempotent (rows are upserted).

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - --days 14 < scripts/rebuild_features.py
"""

import argparse
import logging
from datetime import datetime, timedelta, timezone

from common.constants import Pollutant
from common.logging_conf import configure_logging
from db.session import get_session
from features.build_features import build_feature_frame
from features.feature_store import write_features
from orchestration.plugins.common.tasks import _active_stations

logger = logging.getLogger(__name__)

CHUNK_DAYS = 30  # rows per upsert stay well under Postgres's bind-parameter limit


def rebuild(session, start: datetime, end: datetime, pollutants: list[Pollutant], station_ids: list[str] | None) -> int:
    """Feature rows written for every hour from `start` to `end` inclusive."""
    stations = [s for s in _active_stations(session) if not station_ids or s.station_id in station_ids]
    total = 0
    for station in stations:
        written = 0
        for pollutant in pollutants:
            chunk_start = start
            while chunk_start <= end:
                chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS), end + timedelta(hours=1))
                hours = int((chunk_end - chunk_start) / timedelta(hours=1))
                as_of = [chunk_start + timedelta(hours=h) for h in range(hours)]
                frame = build_feature_frame(session, station.station_id, pollutant, as_of,
                                            station_lat=station.lat, station_lon=station.lon)
                written += write_features(session, station.station_id, pollutant, frame)
                chunk_start = chunk_end
        total += written
        logger.info("%s: %d feature rows", station.station_id, written)
    return total


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, required=True, help="how far back to recompute, e.g. 14")
    parser.add_argument("--pollutant", choices=[p.value for p in Pollutant], help="default: all")
    parser.add_argument("--station", action="append", help="station id; repeatable (default: all active)")
    args = parser.parse_args()

    pollutants = [Pollutant(args.pollutant)] if args.pollutant else list(Pollutant)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    session = get_session()
    try:
        total = rebuild(session, start, end, pollutants, args.station)
        print(f"feature rows written since {start:%Y-%m-%d %H:%M} UTC: {total}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
