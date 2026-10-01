"""Train only the station/pollutant/horizon combinations that have no active model.

Why they exist (scripts/coverage_diagnosis.py, 2026-10-01): a combination is
skipped when its holdout (the last 30 days) holds no feature row with a known
label. Stations whose data resumed on 2026-09-21/22 had almost none at the
2026-09-25 retrain - a 24 h label at best, none for the longer horizons - and
the feature rows written after it were all-NaN (6 h refresh window, fixed in
c4c079b). So 15 stations had thousands of training rows and no PM2.5 model.
After scripts/rebuild_features.py their holdout is filled.

Nothing active is replaced: a combination with an active model is left to the
weekly retrain. Without --apply it reports the rows each missing combination
would train on; with --apply it trains them (train.py activates a first model
unconditionally).

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - --apply < scripts/train_missing_models.py
"""

import argparse
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from common.constants import DEFAULT_HORIZONS_HOURS, ModelType, Pollutant
from common.logging_conf import configure_logging
from db.models import ModelRun
from db.session import get_session
from models.train import build_training_matrix, load_model_config, train_station_pollutant_horizon
from orchestration.plugins.common.tasks import _active_stations

logger = logging.getLogger(__name__)


def missing_combinations(session) -> list[tuple]:
    """(station, pollutant, horizon) for active stations with no active quantile model."""
    have = set(
        session.execute(
            select(ModelRun.station_id, ModelRun.pollutant, ModelRun.horizon_hours)
            .where(ModelRun.is_active.is_(True), ModelRun.model_type == ModelType.QUANTILE_REGRESSOR).distinct()
        ).all()
    )
    return [
        (station, pollutant, horizon)
        for station in _active_stations(session)
        for pollutant in Pollutant
        for horizon in DEFAULT_HORIZONS_HOURS
        if (station.station_id, pollutant, horizon) not in have
    ]


def train_missing(session, apply: bool, now: datetime | None = None) -> Counter:
    """Prints one line per missing combination; returns counts by outcome."""
    config = load_model_config()["training"]
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=config["default_training_window_days"])
    holdout_start = now - timedelta(days=config["holdout_days"])
    outcomes: Counter = Counter()
    for station, pollutant, horizon in missing_combinations(session):
        X, _ = build_training_matrix(session, station.station_id, pollutant, horizon, window_start, now)
        train_rows = int((X.index < holdout_start).sum()) if len(X) else 0
        holdout_rows = len(X) - train_rows
        if len(X) < config["min_training_rows"]:
            outcome = "too few rows"
        elif train_rows == 0 or holdout_rows == 0:
            outcome = "empty train or holdout"
        elif not apply:
            outcome = "trainable"
        else:
            ids = train_station_pollutant_horizon(
                session, station.station_id, pollutant, horizon, window_start, now, config["holdout_days"]
            )
            outcome = "trained" if ids else "skipped by train"
        outcomes[(pollutant.value, outcome)] += 1
        print(f"{station.station_id} {station.name[:34]} | {pollutant.value} | {horizon}h | "
              f"train {train_rows} holdout {holdout_rows} | {outcome}")
    return outcomes


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="train the missing models (default: report only)")
    args = parser.parse_args()
    session = get_session()
    try:
        outcomes = train_missing(session, args.apply)
        print("\nsummary:")
        for (pollutant, outcome), n in sorted(outcomes.items()):
            print(f"  {pollutant:5s} {outcome}: {n}")
        if not args.apply:
            print("report only; rerun with --apply to train")
    finally:
        session.close()


if __name__ == "__main__":
    main()
