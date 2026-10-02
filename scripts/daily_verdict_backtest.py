"""Backtest of graded daily verdicts on last winter (read-only, PM2.5).

The owner chose graded verdicts on 2026-10-02: a day's verdict is the CPCB
category of its MEAN (go below Poor, caution for Poor, no-go from Very Poor).
This trains throwaway quantile models for "the mean of the local day k days
ahead" (k = 1..5) on all history EXCEPT a test window, and scores them on that
window, twice:

  onset   2025-10-15 .. 2025-11-30   the weeks the air turns bad (Diwali, stubble)
  winter  2026-01-15 .. 2026-03-22   deep winter easing into spring

For each it prints, per day ahead: the error of the expected value against
"tomorrow is like the last 24 hours"; whether the 10/50/90 % quantiles hold
that share of the days; and, for several decision probabilities, how often the
grade is exact, too mild (the dangerous error) or too harsh, with recall and
precision of no-go and of caution-or-worse. Nothing is written to the DB.

Run inside the Airflow scheduler container (takes roughly 15 minutes):
    nohup sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/daily_verdict_backtest.py > ~/daily_backtest.log 2>&1 &
    tail -f ~/daily_backtest.log
"""

import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from common.constants import Pollutant
from db.models import Station
from db.readings import hourly_readings
from db.session import get_session
from features.feature_store import read_features
from models import exceedance
from models.daily import category_for_day, day_ahead_labels, local_day_means, local_days
from models.lightgbm_pipeline import fit_quantile_model, prepare_X
from models.lightgbm_pipeline import predict as lgbm_predict
from models.outlook import station_timezone
from models.train import load_model_config

POLLUTANT = Pollutant.PM25
HISTORY_START = datetime(2025, 9, 25, tzinfo=timezone.utc)
FOLDS = {
    "onset": (pd.Timestamp("2025-10-15"), pd.Timestamp("2025-11-30")),
    "winter": (pd.Timestamp("2026-01-15"), pd.Timestamp("2026-03-22")),
}
DAYS_AHEAD = (1, 2, 3, 4, 5)
# No training row may see the test window: its label lies up to 5 days after
# its own day, and a row after the window looks back 48 h.
BUFFER_BEFORE = pd.Timedelta(days=7)
BUFFER_AFTER = pd.Timedelta(days=3)
DECISIONS = (0.3, 0.4, 0.5)
PERSISTENCE = "rolling_mean_24h"


def probability_above(values: np.ndarray, quantiles: list[float], threshold: float) -> np.ndarray:
    """models.exceedance.probability_from_quantiles for every row of `values`
    (one column per quantile, each row sorted)."""
    p = np.array(quantiles)
    cdf = np.full(len(values), p[-1])
    for j in range(len(p) - 1):
        low, high = values[:, j], values[:, j + 1]
        inside = (low <= threshold) & (threshold < high)
        cdf[inside] = p[j] + (p[j + 1] - p[j]) * (threshold - low[inside]) / (high[inside] - low[inside])
    cdf[threshold <= values[:, 0]] = p[0]
    return 1.0 - cdf


def grades(values: np.ndarray, quantiles: list[float], bounds: list[float], decision: float) -> np.ndarray:
    """0 = below the health threshold, then one step per band reached."""
    return sum((probability_above(values, quantiles, b) >= decision).astype(int) for b in bounds)


def prf(actual: np.ndarray, predicted: np.ndarray) -> str:
    tp = int((actual & predicted).sum())
    recall = tp / actual.sum() if actual.sum() else float("nan")
    precision = tp / predicted.sum() if predicted.sum() else float("nan")
    return f"R={recall:.2f} P={precision:.2f}"


def grade_line(name: str, actual: np.ndarray, predicted: np.ndarray, no_go: int) -> str:
    return (f"    {name:18s} exact {np.mean(predicted == actual):.2f}  too mild {np.mean(predicted < actual):.2f}  "
            f"too harsh {np.mean(predicted > actual):.2f} | no-go {prf(actual >= no_go, predicted >= no_go)} | "
            f"caution or worse {prf(actual >= 1, predicted >= 1)} | shares "
            + "/".join(f"{np.mean(predicted == g):.2f}" for g in range(int(max(actual.max(), predicted.max())) + 1)))


def main() -> None:
    config = load_model_config()
    quantiles, params = sorted(config["quantiles"]), config["lightgbm"]["quantile"]
    min_rows = config["training"]["min_training_rows"]
    thresholds = exceedance.load_thresholds()
    order = [bp["category"] for bp in thresholds["pollutants"][POLLUTANT.value]["breakpoints"]]
    first = order.index(thresholds["health_threshold_category"])
    bands = order[first:]
    bounds = [exceedance.get_category_lower_bound(thresholds, POLLUTANT, c) for c in bands]
    no_go = bands.index(thresholds["no_go_category"]) + 1
    now = datetime.now(timezone.utc)

    session = get_session()
    stations = session.execute(select(Station).where(Station.is_active.is_(True)).order_by(Station.station_id)).scalars().all()
    parts = []
    for station in stations:
        tz_name = station_timezone(station)
        features = read_features(session, station.station_id, POLLUTANT, HISTORY_START, now)
        readings = hourly_readings(session, station.station_id, POLLUTANT, HISTORY_START, now)
        if features.empty or not readings:
            continue
        series = pd.Series([v for _, v in readings], index=pd.DatetimeIndex([t for t, _ in readings], tz="UTC"))
        day_means = local_day_means(series.groupby(level=0).first(), tz_name)
        X_all = prepare_X(features).dropna()
        if X_all.empty:
            continue
        as_of_days = local_days(X_all.index, tz_name)
        for k in DAYS_AHEAD:
            y_all = day_ahead_labels(X_all.index, day_means, tz_name, k)
            labelled = ~np.isnan(y_all)
            target_days = as_of_days + pd.Timedelta(days=k)
            for fold, (start, end) in FOLDS.items():
                test = labelled & (target_days >= start) & (target_days <= end)
                train = labelled & ((as_of_days < start - BUFFER_BEFORE) | (as_of_days > end + BUFFER_AFTER))
                if test.sum() < 24 * 7 or train.sum() < min_rows:
                    continue
                predictions = np.column_stack([
                    lgbm_predict(fit_quantile_model(X_all[train], pd.Series(y_all[train]), q, params), X_all[test])
                    for q in quantiles
                ])
                parts.append(pd.DataFrame({
                    "fold": fold, "k": k, "station": station.station_id, "day": target_days[test],
                    "actual": y_all[test], "persist": X_all[PERSISTENCE].to_numpy()[test],
                    **{f"q{i}": column for i, column in enumerate(np.sort(predictions, axis=1).T)},
                }))
        print(f"{station.station_id} done", file=sys.stderr, flush=True)
    session.close()

    df = pd.concat(parts, ignore_index=True)
    qcols = [f"q{i}" for i in range(len(quantiles))]
    median = qcols[quantiles.index(0.5)]

    # The vectorised rule must be the production rule.
    thresholds_at = lambda d: {**thresholds, "exceedance_probability_decision_threshold": d}  # noqa: E731
    sample = df.sample(min(300, len(df)), random_state=0)
    for decision in DECISIONS:
        fast = grades(sample[qcols].to_numpy(), quantiles, bounds, decision)
        slow = [max(order.index(category_for_day(dict(zip(quantiles, row)), thresholds_at(decision), POLLUTANT)) - first + 1, 0)
                for row in sample[qcols].to_numpy()]
        assert list(fast) == slow, "vectorised grades differ from models.daily.category_for_day"

    print(f"PM2.5 daily-mean backtest; grades: 0 = below {bounds[0]:.0f}, " +
          ", ".join(f"{i + 1} = {c} (from {b:.0f})" for i, (c, b) in enumerate(zip(bands, bounds))) +
          f"; no-go from grade {no_go}; one row per station, target day and hour the forecast was made")
    for fold, (start, end) in FOLDS.items():
        f = df[df.fold == fold]
        if f.empty:
            print(f"\n== {fold}: no data")
            continue
        print(f"\n== {fold}: target days {start:%Y-%m-%d} .. {end:%Y-%m-%d}; stations {f.station.nunique()}")
        for k, g in f.groupby("k"):
            actual = np.digitize(g.actual.to_numpy(), bounds)
            values = g[qcols].to_numpy()
            print(f"  day +{k}: rows {len(g)}, station-days {len(g.drop_duplicates(['station', 'day']))}, actual grade shares "
                  + "/".join(f"{np.mean(actual == i):.2f}" for i in range(len(bounds) + 1))
                  + f" | MAE expected {np.mean(np.abs(g[median] - g.actual)):.1f}, last-24h {np.mean(np.abs(g.persist - g.actual)):.1f}"
                  + f", bias {np.mean(g[median] - g.actual):+.1f} | actual below "
                  + " ".join(f"q{q}: {np.mean(g.actual.to_numpy() < values[:, i]):.2f}" for i, q in enumerate(quantiles)))
            for decision in DECISIONS:
                print(grade_line(f"rule p>={decision}", actual, grades(values, quantiles, bounds, decision), no_go))
            print(grade_line("last-24h mean", actual, np.digitize(g.persist.to_numpy(), bounds), no_go))


if __name__ == "__main__":
    main()
