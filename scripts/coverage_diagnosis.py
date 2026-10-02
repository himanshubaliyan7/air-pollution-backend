"""Read-only: why does a station/pollutant have no current forecast?

One line per active station and pollutant with the facts each stage needs:
readings per source, feature rows (and how many are complete enough to train
on), active models per horizon, and the newest forecast. Written for the
2026-10-01 question "15 stations have live CPCB PM2.5 but no PM2.5 model".

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/coverage_diagnosis.py
    ... python - --all < scripts/coverage_diagnosis.py     (also list the healthy pairs)
"""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from common.config import forecast_target
from common.constants import DEFAULT_HORIZONS_HOURS, LAG_HOURS, MAX_INPUT_STALENESS_HOURS, ModelType, Pollutant
from db.models import Feature, Forecast, ModelRun, RawSensorReading, Station
from db.session import get_session
from features.feature_store import get_feature_set_version
from models.train import load_model_config

def _t(value) -> str:
    return value.strftime("%m-%d %H:%M") if value else "-"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--all", action="store_true", help="also list pairs that have a full current forecast")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    holdout_start = now - timedelta(days=load_model_config()["training"]["holdout_days"])
    target = forecast_target().value  # models and forecasts of the family being served
    session = get_session()
    try:
        stations = session.execute(select(Station).where(Station.is_active.is_(True)).order_by(Station.name)).scalars().all()

        readings = defaultdict(dict)
        for sid, pol, source, n, first, last in session.execute(
            select(RawSensorReading.station_id, RawSensorReading.pollutant, RawSensorReading.source, func.count(),
                   func.min(RawSensorReading.observed_at), func.max(RawSensorReading.observed_at))
            .group_by(RawSensorReading.station_id, RawSensorReading.pollutant, RawSensorReading.source)
        ):
            readings[(sid, pol)][source.value] = (n, first, last)

        # Every lag present: the rows training keeps and the hours a forecast can be made from.
        complete = and_(*[Feature.features[f"lag_{h}h"].astext.isnot(None) for h in LAG_HOURS])
        features = {
            (sid, pol): rest
            for sid, pol, *rest in session.execute(
                select(Feature.station_id, Feature.pollutant, func.count(),
                       func.count().filter(complete),
                       func.count().filter(complete, Feature.feature_time >= holdout_start),
                       func.max(Feature.feature_time).filter(complete))
                .where(Feature.feature_set_version == get_feature_set_version())
                .group_by(Feature.station_id, Feature.pollutant)
            )
        }

        models = defaultdict(set)
        for sid, pol, horizon in session.execute(
            select(ModelRun.station_id, ModelRun.pollutant, ModelRun.horizon_hours)
            .where(ModelRun.is_active.is_(True), ModelRun.model_type == ModelType.QUANTILE_REGRESSOR,
                   ModelRun.target == target).distinct()
        ):
            models[(sid, pol)].add(horizon)

        forecasts = {
            (sid, pol): made_at
            for sid, pol, made_at in session.execute(
                select(Forecast.station_id, Forecast.pollutant, func.max(Forecast.forecast_made_at))
                .where(Forecast.target_time >= now - timedelta(days=10), Forecast.target == target)
                .group_by(Forecast.station_id, Forecast.pollutant)
            )
        }

        fresh = now - timedelta(hours=MAX_INPUT_STALENESS_HOURS)
        causes = defaultdict(int)
        print(f"as of {now:%Y-%m-%d %H:%M} UTC; target {target}; holdout starts {holdout_start:%m-%d}; times are UTC month-day")
        print("station | pollutant | cause | openaq n first..last | cpcb n first..last | "
              "features all/complete/complete-in-holdout newest-complete | model horizons | newest forecast")
        for station in stations:
            for pollutant in Pollutant:
                key = (station.station_id, pollutant)
                by_source = readings.get(key, {})
                newest = max((v[2] for v in by_source.values()), default=None)
                all_rows, complete_rows, holdout_rows, newest_complete = features.get(key, (0, 0, 0, None))
                horizons = models.get(key, set())
                made_at = forecasts.get(key)
                if not by_source:
                    cause = "no readings"
                elif newest < fresh:
                    cause = "input stale"
                elif not horizons:
                    cause = "no model: no complete features" if complete_rows == 0 else (
                        "no model: nothing in holdout" if holdout_rows == 0 else "no model: features exist")
                elif made_at is None or made_at < fresh:
                    cause = "model but no fresh forecast"
                elif horizons != set(DEFAULT_HORIZONS_HOURS):
                    cause = "some horizons lack a model"
                else:
                    cause = "ok"
                causes[(pollutant.value, cause)] += 1
                if cause == "ok" and not args.all:
                    continue
                sources = " | ".join(
                    f"{by_source[s][0]} {_t(by_source[s][1])}..{_t(by_source[s][2])}" if s in by_source else "0"
                    for s in ("openaq", "cpcb")
                )
                print(f"{station.station_id} {station.name[:34]} | {pollutant.value} | {cause} | {sources} | "
                      f"{all_rows}/{complete_rows}/{holdout_rows} {_t(newest_complete)} | "
                      f"{sorted(horizons) or '-'} | {_t(made_at)}")
        print("\nsummary:")
        for (pollutant, cause), n in sorted(causes.items()):
            print(f"  {pollutant:5s} {cause}: {n}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
