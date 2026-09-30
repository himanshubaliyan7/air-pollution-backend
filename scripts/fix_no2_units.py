"""One-off repair: undo the ppb -> ug/m3 conversion of CPCB NO2 (2026-10-01).

OpenAQ labels the NO2 it relays from India's CPCB network "ppb", but the values
are already ug/m3 (scripts/cpcb_subindex_study.py: CPCB's own AQI feed matches
the raw number; the converted one is 1.88x too high). From 2026-09-25 (6693c29)
every stored NO2 row from those sensors was multiplied by 46.0055/24.45 exactly
once, at load time or by that day's one-off UPDATE. The ingestion code no
longer converts them (85ed394); this script repairs what is already stored.

Steps, run in order, each on its own (--step):
  check     read-only: affected sensors and rows, and a probe comparing stored
            values with OpenAQ's raw numbers (median ratio ~1.88 = still
            inflated, ~1.00 = already repaired).
  data      divides the affected rows by the factor, in one transaction.
            Refuses unless the probe says "still inflated", so a second run
            cannot divide twice.
  features  recomputes NO2 features over the training window.
  retrain   trains NO2 models for every station/horizon and activates them
            unconditionally: the old ones were fitted and scored on inflated
            data, so the usual holdout-F1 comparison means nothing here. An
            active NO2 model that could not be replaced is deactivated (the
            station then shows no-data, never an inflated verdict). Previously
            active model ids are printed first, for rollback.

Pause ingestion_dag from before the code deploy until after --step data:
hourly ingestion re-upserts the last 72 h, so with the old code running it
would re-inflate repaired rows, and with the new code it would write correct
rows that --step data would then divide a second time.

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - --step check < scripts/fix_no2_units.py
"""

import argparse
import logging
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from common.config import get_settings
from common.constants import DEFAULT_HORIZONS_HOURS, Pollutant
from common.logging_conf import configure_logging
from db.models import ModelRun, RawSensorReading
from db.session import get_session
from features.build_features import build_feature_frame
from features.feature_store import write_features
from ingestion.config import DELHI_NCR_BBOX, DELHI_NCR_COUNTRY_ISO
from ingestion.sources.openaq import OpenAQSource
from ingestion.units import PPB_TO_UG_M3
from models import registry
from models.train import train_station_pollutant_horizon
from orchestration.plugins.common.tasks import _active_stations

logger = logging.getLogger(__name__)

FACTOR = PPB_TO_UG_M3[Pollutant.NO2]
TRAINING_WINDOW_DAYS = 365  # as retrain_all
HOLDOUT_DAYS = 30
FEATURE_WINDOW_DAYS = TRAINING_WINDOW_DAYS + 7
FEATURE_CHUNK_DAYS = 30
PROBE_SENSORS = 5


def ppb_no2_sensor_ids(client: OpenAQSource) -> set[str]:
    """NO2 sensors OpenAQ labels "ppb" in our bbox (one paginated call)."""
    min_lon, min_lat, max_lon, max_lat = DELHI_NCR_BBOX
    params = {"bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}", "iso": DELHI_NCR_COUNTRY_ISO}
    ids = set()
    for loc in client._paginate("/locations", params):
        for sensor in loc.get("sensors", []):
            parameter = sensor.get("parameter") or {}
            if parameter.get("name") == "no2" and (parameter.get("units") or "").strip() == "ppb":
                ids.add(str(sensor["id"]))
    return ids


def _sensor_of(record_id):
    return func.split_part(record_id, ":", 1)


def affected_counts(session, sensor_ids: set[str]) -> list[tuple[str, str, int, datetime, datetime]]:
    sensor = _sensor_of(RawSensorReading.source_record_id)
    return session.execute(
        select(sensor, RawSensorReading.station_id, func.count(), func.min(RawSensorReading.observed_at),
               func.max(RawSensorReading.observed_at))
        .where(RawSensorReading.pollutant == Pollutant.NO2, sensor.in_(sensor_ids))
        .group_by(sensor, RawSensorReading.station_id)
        .order_by(RawSensorReading.station_id)
    ).all()


def probe_ratio(session, client: OpenAQSource, counts) -> float | None:
    """Median stored/raw ratio over a few sensors' newest stored day."""
    ratios = []
    for sensor_id, station_id, _, _, newest in sorted(counts, key=lambda c: c[4], reverse=True)[:PROBE_SENSORS]:
        stored = dict(session.execute(
            select(RawSensorReading.source_record_id, RawSensorReading.value).where(
                RawSensorReading.station_id == station_id,
                RawSensorReading.pollutant == Pollutant.NO2,
                RawSensorReading.observed_at > newest - timedelta(days=1),
                _sensor_of(RawSensorReading.source_record_id) == sensor_id,
            )
        ).all())
        params = {"datetime_from": (newest - timedelta(days=1)).isoformat(),
                  "datetime_to": (newest + timedelta(hours=1)).isoformat()}
        for row in client._paginate(f"/sensors/{sensor_id}/hours", params):
            key = f"{sensor_id}:{((row.get('period') or {}).get('datetimeFrom') or {}).get('utc')}"
            raw = row.get("value")
            if key in stored and raw and raw >= 5.0:
                ratios.append(stored[key] / raw)
    return statistics.median(ratios) if ratios else None


def verdict(ratio: float | None) -> str:
    if ratio is None:
        return "unknown"
    if abs(ratio - FACTOR) < 0.08:
        return "inflated"
    if abs(ratio - 1.0) < 0.04:
        return "repaired"
    return "unexpected"


def step_check(session, client) -> tuple[set[str], list, str]:
    sensor_ids = ppb_no2_sensor_ids(client)
    counts = affected_counts(session, sensor_ids)
    total = sum(c[2] for c in counts)
    print(f"'ppb'-labelled NO2 sensors in bbox: {len(sensor_ids)}; with stored rows: {len(counts)}; rows: {total}")
    for sensor_id, station_id, n, first, last in counts:
        print(f"  {station_id:22s} sensor {sensor_id:>10s}  {n:6d} rows  {first:%Y-%m-%d} .. {last:%Y-%m-%d %H:%M}")
    other = session.execute(
        select(func.count()).where(RawSensorReading.pollutant == Pollutant.NO2,
                                   _sensor_of(RawSensorReading.source_record_id).not_in(sensor_ids))
    ).scalar_one()
    print(f"NO2 rows from other sensors (left untouched): {other}")
    ratio = probe_ratio(session, client, counts)
    state = verdict(ratio)
    print(f"probe: median stored/raw ratio = {ratio if ratio is None else round(ratio, 3)} -> {state}"
          f" (inflated = {FACTOR:.3f}, repaired = 1.000)")
    return sensor_ids, counts, state


def step_data(session, client) -> None:
    sensor_ids, counts, state = step_check(session, client)
    if state != "inflated":
        raise SystemExit(f"Refusing to change data: probe says {state!r}, not 'inflated'.")
    result = session.execute(
        update(RawSensorReading)
        .where(RawSensorReading.pollutant == Pollutant.NO2,
               _sensor_of(RawSensorReading.source_record_id).in_(sensor_ids))
        .values(value=RawSensorReading.value / FACTOR)
        .execution_options(synchronize_session=False)
    )
    expected = sum(c[2] for c in counts)
    if result.rowcount != expected:
        session.rollback()
        raise SystemExit(f"Rolled back: updated {result.rowcount} rows, expected {expected}.")
    session.commit()
    print(f"divided {result.rowcount} NO2 rows by {FACTOR:.4f}")
    ratio = probe_ratio(session, client, counts)
    print(f"probe after: median stored/raw ratio = {ratio if ratio is None else round(ratio, 3)} -> {verdict(ratio)}")


def step_features(session) -> None:
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=FEATURE_WINDOW_DAYS)
    total = 0
    for station in _active_stations(session):
        written = 0
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + timedelta(days=FEATURE_CHUNK_DAYS), end)
            hours = int((chunk_end - chunk_start) / timedelta(hours=1))
            as_of = [chunk_start + timedelta(hours=h) for h in range(hours)]
            frame = build_feature_frame(session, station.station_id, Pollutant.NO2, as_of,
                                        station_lat=station.lat, station_lon=station.lon)
            written += write_features(session, station.station_id, Pollutant.NO2, frame)
            chunk_start = chunk_end
        total += written
        logger.info("%s: %d NO2 feature rows", station.station_id, written)
    print(f"NO2 feature rows written: {total}")


def step_retrain(session) -> None:
    previously_active = session.execute(
        select(ModelRun.model_id, ModelRun.station_id, ModelRun.horizon_hours, ModelRun.model_type, ModelRun.quantile)
        .where(ModelRun.pollutant == Pollutant.NO2, ModelRun.is_active.is_(True))
    ).all()
    print(f"previously active NO2 models ({len(previously_active)}), for rollback:")
    for row in previously_active:
        print(f"  ROLLBACK {row.model_id} {row.station_id} {row.horizon_hours}h {row.model_type.value} q={row.quantile}")

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=TRAINING_WINDOW_DAYS)
    activated, skipped, failed = set(), 0, 0
    for station in _active_stations(session):
        for horizon in DEFAULT_HORIZONS_HOURS:
            try:
                ids = train_station_pollutant_horizon(
                    session, station.station_id, Pollutant.NO2, horizon, window_start, now, HOLDOUT_DAYS
                )
            except Exception:  # noqa: BLE001 - one failing combo must not stop the rest
                session.rollback()
                failed += 1
                logger.exception("%s/no2/%dh failed", station.station_id, horizon)
                continue
            if not ids:
                skipped += 1
                continue
            for model_id in ids:
                registry.activate_model(session, model_id)
                activated.add(model_id)
            logger.info("%s/no2/%dh: activated %d models", station.station_id, horizon, len(ids))

    stale = [row.model_id for row in previously_active
             if session.get(ModelRun, row.model_id).is_active and row.model_id not in activated]
    if stale:
        session.execute(update(ModelRun).where(ModelRun.model_id.in_(stale)).values(is_active=False))
        session.commit()
    print(f"activated {len(activated)} new NO2 models; skipped {skipped} (too little data), failed {failed}; "
          f"deactivated {len(stale)} old models that could not be replaced")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--step", choices=["check", "data", "features", "retrain"], default="check")
    args = parser.parse_args()

    session = get_session()
    try:
        if args.step in ("check", "data"):
            client = OpenAQSource(api_key=get_settings().openaq_api_key)
            step_check(session, client) if args.step == "check" else step_data(session, client)
        elif args.step == "features":
            step_features(session)
        else:
            step_retrain(session)
    finally:
        session.close()


if __name__ == "__main__":
    main()
