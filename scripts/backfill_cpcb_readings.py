"""Derive CPCB readings from every CAAQMS snapshot stored since a date.

current_aqi_dag does this for the last few hours on every run; this fills the
history (snapshots with an hourly sub-index exist from 2026-09-27). Idempotent.

Run inside the Airflow scheduler container:
    sudo docker exec -i docker-airflow-scheduler-1 python - --since 2026-09-27 < scripts/backfill_cpcb_readings.py
"""

import argparse
from datetime import datetime, timezone

from db.session import get_session
from ingestion.loaders.cpcb_reading_loader import readings_from_snapshots
from models.exceedance import load_thresholds


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", required=True, help="UTC date, e.g. 2026-09-27")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    session = get_session()
    try:
        written = readings_from_snapshots(session, load_thresholds(), since)
        print(f"CPCB readings written since {since:%Y-%m-%d}: {written}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
