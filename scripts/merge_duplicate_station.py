"""Move the CPCB data stored on a dead duplicate station entry to the live one.

OpenAQ lists some CPCB sites twice. Until 2026-10-01 the CPCB feed was matched
to the nearest entry, which for Anand Vihar (openaq:5509, live: openaq:235) and
ITO (openaq:10489, live: openaq:5613) is the dead one: current AQI and the
CPCB fallback readings went there, while the history and the models are on the
live entry. The matcher now picks the live entry; this moves what is already
stored, so the live station can forecast at once instead of after 48 h of new
readings.

Without --apply it only reports. With --apply, in one transaction:
  1. snapshots of --dead move to --live (an hour --live already holds is kept);
  2. CPCB readings are derived again from the live station's snapshots (idempotent);
  3. --dead is marked inactive (the daily activity refresh keeps it so: no
     recent OpenAQ data, no recent snapshot);
  4. subscriptions that list --dead list --live instead.
Then rebuild the live station's features:
    ... python - --days 7 --station openaq:235 < scripts/rebuild_features.py

Deploy the new matcher first, or the next hourly run sends data to --dead again.
Run inside the Airflow scheduler container, between DAG runs (:50-:58 or :20-:28):
    sudo docker exec -i docker-airflow-scheduler-1 python - --dead openaq:5509 --live openaq:235 < scripts/merge_duplicate_station.py
"""

import argparse

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import aliased

from common.constants import SensorSourceName
from db.models import AlertSubscription, ModelRun, RawSensorReading, Station, StationAqiSnapshot
from db.session import get_session
from ingestion.loaders.aqi_snapshot_loader import MATCH_RADIUS_METERS, haversine_m
from ingestion.loaders.cpcb_reading_loader import FEED_POLLUTANTS, UPSERT_BATCH, reading_rows
from models.exceedance import load_thresholds


def newest_openaq(session, station_id: str):
    return session.execute(
        select(func.max(RawSensorReading.observed_at)).where(
            RawSensorReading.station_id == station_id, RawSensorReading.source == SensorSourceName.OPENAQ)
    ).scalar_one()


def active_models(session, station_id: str) -> int:
    return session.execute(
        select(func.count()).select_from(ModelRun).where(ModelRun.station_id == station_id, ModelRun.is_active.is_(True))
    ).scalar_one()


def merge(session, dead_id: str, live_id: str, apply: bool) -> dict | None:
    """Prints the report; with `apply` makes the changes and returns their counts.
    Raises ValueError when the pair is not a duplicate the matcher would merge."""
    dead, live = session.get(Station, dead_id), session.get(Station, live_id)
    if dead is None or live is None or dead_id == live_id:
        raise ValueError("both stations must exist and differ")
    distance = haversine_m(dead.lat, dead.lon, live.lat, live.lon)
    dead_newest, live_newest = newest_openaq(session, dead_id), newest_openaq(session, live_id)
    count, first, last = session.execute(
        select(func.count(), func.min(StationAqiSnapshot.source_updated_at), func.max(StationAqiSnapshot.source_updated_at))
        .where(StationAqiSnapshot.station_id == dead_id)
    ).one()
    subscriptions = session.execute(
        select(func.count()).select_from(AlertSubscription).where(AlertSubscription.station_ids.any(dead_id))
    ).scalar_one()
    print(f"dead {dead_id} {dead.name!r}: newest OpenAQ reading {dead_newest}, "
          f"active models {active_models(session, dead_id)}, active={dead.is_active}")
    print(f"live {live_id} {live.name!r}: newest OpenAQ reading {live_newest}, "
          f"active models {active_models(session, live_id)}, active={live.is_active}")
    print(f"distance {distance:.0f} m; snapshots on dead: {count} ({first} .. {last}); "
          f"subscriptions listing dead: {subscriptions}")

    # The matcher's own test: refuse anything it would not decide the same way.
    if distance > MATCH_RADIUS_METERS:
        raise ValueError(f"the stations are more than {MATCH_RADIUS_METERS:.0f} m apart")
    if live_newest is None or (dead_newest is not None and dead_newest >= live_newest):
        raise ValueError("--live does not have the newer OpenAQ history; are the arguments swapped?")
    if not apply:
        print("report only; rerun with --apply to make the changes")
        return None

    kept = aliased(StationAqiSnapshot)
    already_on_live = (
        select(kept.station_id).where(
            kept.station_id == live_id,
            kept.pollutant_id == StationAqiSnapshot.pollutant_id,
            kept.source_updated_at == StationAqiSnapshot.source_updated_at,
        ).exists()
    )
    moved = session.execute(
        update(StationAqiSnapshot)
        .where(StationAqiSnapshot.station_id == dead_id, ~already_on_live)
        .values(station_id=live_id)
        .execution_options(synchronize_session=False)
    ).rowcount
    dropped = session.execute(
        delete(StationAqiSnapshot).where(StationAqiSnapshot.station_id == dead_id)
        .execution_options(synchronize_session=False)
    ).rowcount

    items = session.execute(
        select(StationAqiSnapshot.station_id, StationAqiSnapshot.pollutant_id,
               StationAqiSnapshot.source_updated_at, StationAqiSnapshot.sub_index_hourly).where(
            StationAqiSnapshot.station_id == live_id,
            StationAqiSnapshot.pollutant_id.in_(list(FEED_POLLUTANTS)),
            StationAqiSnapshot.sub_index_hourly.isnot(None),
        )
    ).all()
    rows = reading_rows(items, load_thresholds())
    for i in range(0, len(rows), UPSERT_BATCH):
        stmt = pg_insert(RawSensorReading).values(rows[i:i + UPSERT_BATCH])
        session.execute(stmt.on_conflict_do_update(
            index_elements=["station_id", "pollutant", "observed_at", "source"],
            set_={k: stmt.excluded[k] for k in ("value", "unit", "source_record_id", "ingested_at")},
        ))

    dead.is_active = False
    resubscribed = session.execute(
        text("UPDATE alert_subscriptions SET station_ids = array_replace(station_ids, :dead, :live) "
             "WHERE :dead = ANY(station_ids)"),
        {"dead": dead_id, "live": live_id},
    ).rowcount
    session.commit()
    result = {"snapshots_moved": moved, "snapshots_dropped": dropped, "cpcb_readings": len(rows),
              "subscriptions_updated": resubscribed}
    print(f"{result}; {dead_id} is now inactive")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dead", required=True, help="the duplicate entry, e.g. openaq:5509")
    parser.add_argument("--live", required=True, help="the entry with the history and models, e.g. openaq:235")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    args = parser.parse_args()
    session = get_session()
    try:
        merge(session, args.dead, args.live, args.apply)
    except ValueError as exc:
        session.rollback()
        raise SystemExit(f"refusing: {exc}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
