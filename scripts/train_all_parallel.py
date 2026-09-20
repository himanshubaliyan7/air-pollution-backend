"""Parallel training across every station/pollutant/horizon combination.

Each combination is fully independent (its own train/predict/register/
promote cycle - see models/train.py), so process-level parallelism across
CPU cores is the effective speedup lever here, not GPU: this workload is
many small models (a few thousand rows, ~20 features each), not few large
ones - see models/config/model_config.yaml's num_threads note for why GPU
and even high per-model thread counts don't help at this scale.

Usage:
    python -m scripts.train_all_parallel --workers 10 --training-window-days 200 --holdout-days 21
"""

import argparse
import logging
import multiprocessing as mp
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from common.constants import DEFAULT_HORIZONS_HOURS, Pollutant
from common.logging_conf import configure_logging
from db.models import Feature
from db.session import get_session
from models.train import train_station_pollutant_horizon

logger = logging.getLogger(__name__)


def _station_pollutant_pairs() -> list[tuple[str, Pollutant]]:
    """Only (station, pollutant) pairs that actually have materialized
    features - avoids submitting jobs for a station that never reported a
    given pollutant (they'd just fail the min_training_rows check anyway,
    but this keeps the job list honest and the progress log meaningful)."""
    session = get_session()
    try:
        rows = session.execute(select(Feature.station_id, Feature.pollutant).distinct()).all()
        return [(station_id, pollutant) for station_id, pollutant in rows]
    finally:
        session.close()


def _train_one(job: tuple[str, Pollutant, int, datetime, datetime, int]) -> tuple:
    station_id, pollutant, horizon, window_start, window_end, holdout_days = job
    session = get_session()
    try:
        ids = train_station_pollutant_horizon(
            session, station_id, pollutant, horizon, window_start, window_end, holdout_days
        )
        return (station_id, pollutant.value, horizon, len(ids), None)
    except Exception as exc:  # noqa: BLE001 - one failing combo must not kill the whole parallel run
        return (station_id, pollutant.value, horizon, 0, str(exc))
    finally:
        session.close()


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 2))
    parser.add_argument("--training-window-days", type=int, default=200)
    parser.add_argument("--holdout-days", type=int, default=21)
    args = parser.parse_args()

    pairs = _station_pollutant_pairs()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=args.training_window_days)

    jobs = [
        (station_id, pollutant, horizon, window_start, now, args.holdout_days)
        for station_id, pollutant in pairs
        for horizon in DEFAULT_HORIZONS_HOURS
    ]
    logger.info(
        "Training %d combinations (%d station/pollutant pairs x %d horizons) with %d workers",
        len(jobs), len(pairs), len(DEFAULT_HORIZONS_HOURS), args.workers,
    )

    trained, skipped, failed = 0, 0, 0
    with mp.Pool(processes=args.workers) as pool:
        for i, (station_id, pollutant, horizon, n_models, error) in enumerate(
            pool.imap_unordered(_train_one, jobs), start=1
        ):
            if error:
                failed += 1
                logger.warning("[%d/%d] %s/%s/%dh FAILED: %s", i, len(jobs), station_id, pollutant, horizon, error)
            elif n_models == 0:
                skipped += 1
                logger.info("[%d/%d] %s/%s/%dh skipped (not enough data)", i, len(jobs), station_id, pollutant, horizon)
            else:
                trained += 1
                logger.info("[%d/%d] %s/%s/%dh trained %d models", i, len(jobs), station_id, pollutant, horizon, n_models)

    logger.info("Done: %d trained, %d skipped, %d failed (of %d total)", trained, skipped, failed, len(jobs))


if __name__ == "__main__":
    main()
