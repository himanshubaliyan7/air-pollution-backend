"""Retrain every station/pollutant/horizon and activate the new models unconditionally.

For use after the training data itself was corrected - first written for the
2026-10-02 reading filter (common.constants.PLAUSIBLE_RANGE): the active models
were fitted and scored on instrument faults, so the weekly retrain's holdout-F1
comparison against them means nothing and could keep a bad model. Rebuild the
features first (scripts/rebuild_features.py --days 372), or training reads the
old rows.

Without --apply it only reports what is active. With --apply it prints the
active model ids (for rollback), trains, and activates every new model. A
combination that cannot be trained now keeps its old model; those are listed
at the end.

--target trains the other model family (hourly or daily_mean) than the one
being served: that is how a family gets its first models before
config/settings.yaml forecast.target is switched to it. Each family has its
own active set, so this never touches the models being served.

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - --apply < scripts/retrain_models.py
    ... python - --apply --pollutant pm25 --station openaq:5630 < scripts/retrain_models.py
    ... python - --apply --target daily_mean < scripts/retrain_models.py
"""

import argparse
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select

from common.config import forecast_target
from common.constants import DEFAULT_HORIZONS_HOURS, ForecastTarget, Pollutant
from common.logging_conf import configure_logging
from db.models import ModelRun
from db.session import get_session
from models import registry
from models.train import load_model_config, train_station_pollutant_horizon
from orchestration.plugins.common.tasks import _active_stations

logger = logging.getLogger(__name__)


def retrain(session, apply: bool, pollutants: list[Pollutant], station_ids: list[str] | None,
            now: datetime | None = None, target: ForecastTarget | None = None) -> dict:
    """Counts by outcome; `kept` lists the combinations left on their old model."""
    target = target or forecast_target()
    config = load_model_config()["training"]
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=config["default_training_window_days"])
    stations = [s for s in _active_stations(session) if not station_ids or s.station_id in station_ids]

    previously_active = session.execute(
        select(ModelRun.model_id, ModelRun.station_id, ModelRun.pollutant, ModelRun.horizon_hours,
               ModelRun.model_type, ModelRun.quantile)
        .where(ModelRun.is_active.is_(True), ModelRun.pollutant.in_(pollutants), ModelRun.target == target.value,
               ModelRun.station_id.in_([s.station_id for s in stations]))
    ).all()
    had_model = {(r.station_id, r.pollutant, r.horizon_hours) for r in previously_active}
    print(f"target {target.value}: {len(stations)} stations x {[p.value for p in pollutants]} x "
          f"{len(DEFAULT_HORIZONS_HOURS)} horizons; {len(previously_active)} active models on {len(had_model)} combinations")
    report = {"trained": 0, "skipped": 0, "failed": 0, "activated": 0, "kept": []}
    if not apply:
        print("report only; rerun with --apply to train")
        return report

    for row in previously_active:
        print(f"  ROLLBACK {row.model_id} {row.station_id} {row.pollutant.value} {row.horizon_hours}h "
              f"{row.model_type.value} q={row.quantile}")

    for station in stations:
        for pollutant in pollutants:
            for horizon in DEFAULT_HORIZONS_HOURS:
                key = (station.station_id, pollutant, horizon)
                try:
                    ids = train_station_pollutant_horizon(
                        session, station.station_id, pollutant, horizon, window_start, now, config["holdout_days"],
                        target,
                    )
                except Exception:  # noqa: BLE001 - one failing combo must not stop the rest
                    session.rollback()
                    report["failed"] += 1
                    logger.exception("%s/%s/%dh failed", station.station_id, pollutant.value, horizon)
                    ids = []
                else:
                    report["trained" if ids else "skipped"] += 1
                if not ids:
                    if key in had_model:
                        report["kept"].append(key)
                    continue
                for model_id in ids:
                    registry.activate_model(session, model_id)
                report["activated"] += len(ids)
                logger.info("%s/%s/%dh: activated %d models", station.station_id, pollutant.value, horizon, len(ids))
    return report


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="train and activate (default: report only)")
    parser.add_argument("--pollutant", choices=[p.value for p in Pollutant], help="default: all")
    parser.add_argument("--station", action="append", help="station id; repeatable (default: all active)")
    parser.add_argument("--target", choices=[t.value for t in ForecastTarget], help="default: the family being served")
    args = parser.parse_args()

    pollutants = [Pollutant(args.pollutant)] if args.pollutant else list(Pollutant)
    session = get_session()
    try:
        report = retrain(session, args.apply, pollutants, args.station,
                         target=ForecastTarget(args.target) if args.target else None)
        if args.apply:
            print(f"\ncombinations trained {report['trained']}, skipped (too little data) {report['skipped']}, "
                  f"failed {report['failed']}; models activated {report['activated']}")
            print(f"combinations still on their OLD model: {len(report['kept'])}")
            for station_id, pollutant, horizon in report["kept"]:
                print(f"  KEPT {station_id} {pollutant.value} {horizon}h")
    finally:
        session.close()


if __name__ == "__main__":
    main()
