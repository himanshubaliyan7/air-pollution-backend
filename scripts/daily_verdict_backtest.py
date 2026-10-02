"""Backtest of graded daily verdicts on last winter (read-only, PM2.5).

The owner chose graded verdicts on 2026-10-02: a day's verdict is the CPCB
category of its MEAN (go below Poor, caution for Poor, no-go from Very Poor).
This trains throwaway quantile models for "the mean of the local day k days
ahead" (k = 1..5) on all history EXCEPT a test window, and scores them on that
window, twice:

  onset   2025-10-15 .. 2025-11-30   the weeks the air turns bad (Diwali, stubble)
  winter  2026-01-15 .. 2026-03-22   deep winter easing into spring

First run (2026-10-02, models as production builds them: one per station, all
features, the mean itself as the label): WORSE than "tomorrow is like the last
24 hours" at every horizon in both windows - 43 % exact grades on day +1 at
the onset against 61 %, with a bias of -65 to -90 ug/m3 at the onset and +31
to +61 in winter. Reading: the history holds one year, so the day-of-year and
stubble-season features identify last year's dates rather than a season, and a
tree cannot forecast a level it never saw at those dates. This run tests the
remedies, each without those features:

  no-season   one model per station, the day's mean as the label
  ratio       one model per station; the label is log(day's mean / last-24h
              mean), so the model only forecasts the change from today's level
  pooled      the ratio label, one model for all stations together

For each it prints, per day ahead: the error of the expected value; whether
the 10/50/90 % quantiles hold that share of the days; and, for two decision
probabilities, how often the grade is exact, too mild (the dangerous error) or
too harsh, with recall and precision of no-go and of caution-or-worse. The
last line of each block is the last-24h mean itself. Nothing is written to the
DB.

Run inside the Airflow scheduler container (takes roughly 45 minutes):
    nohup sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/daily_verdict_backtest.py > ~/daily_backtest.log 2>&1 &
    tail -f ~/daily_backtest.log
"""

import sys
from datetime import datetime, timezone

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
DECISIONS = (0.3, 0.5)
PERSISTENCE = "rolling_mean_24h"
SEASON_FEATURES = ["doy_sin", "doy_cos", "is_stubble_season", "is_diwali_window"]
VARIANTS = ("no-season", "ratio", "pooled")
POOLED_THREADS = 4


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
    return (f"      {name:14s} exact {np.mean(predicted == actual):.2f}  too mild {np.mean(predicted < actual):.2f}  "
            f"too harsh {np.mean(predicted > actual):.2f} | no-go {prf(actual >= no_go, predicted >= no_go)} | "
            f"caution or worse {prf(actual >= 1, predicted >= 1)} | shares "
            + "/".join(f"{np.mean(predicted == g):.2f}" for g in range(int(max(actual.max(), predicted.max())) + 1)))


def fit_predict(X_train, y_train, X_test, quantiles, params) -> np.ndarray:
    """One column per quantile, each row sorted."""
    columns = [lgbm_predict(fit_quantile_model(X_train, pd.Series(y_train), q, params), X_test) for q in quantiles]
    return np.sort(np.column_stack(columns), axis=1)


def log_ratio(y: np.ndarray, level: np.ndarray) -> np.ndarray:
    return np.log(np.maximum(y, 1.0) / np.maximum(level, 1.0))


def from_log_ratio(predicted: np.ndarray, level: np.ndarray) -> np.ndarray:
    return np.maximum(level, 1.0)[:, None] * np.exp(predicted)


def split(as_of_days, target_days, labelled, start, end):
    test = labelled & (target_days >= start) & (target_days <= end)
    train = labelled & ((as_of_days < start - BUFFER_BEFORE) | (as_of_days > end + BUFFER_AFTER))
    return train, test


def frame(variant, fold, k, station_ids, target_days, actual, level, values) -> pd.DataFrame:
    return pd.DataFrame({
        "variant": variant, "fold": fold, "k": k, "station": station_ids, "day": target_days,
        "actual": actual, "persist": level, **{f"q{i}": column for i, column in enumerate(values.T)},
    })


def main() -> None:
    config = load_model_config()
    quantiles, params = sorted(config["quantiles"]), config["lightgbm"]["quantile"]
    pooled_params = {**params, "num_threads": POOLED_THREADS}
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
    loaded = []  # per station: (station_id, X, as_of_days, {k: labels})
    for station in stations:
        tz_name = station_timezone(station)
        features = read_features(session, station.station_id, POLLUTANT, HISTORY_START, now)
        readings = hourly_readings(session, station.station_id, POLLUTANT, HISTORY_START, now)
        if features.empty or not readings:
            continue
        series = pd.Series([v for _, v in readings], index=pd.DatetimeIndex([t for t, _ in readings], tz="UTC"))
        day_means = local_day_means(series.groupby(level=0).first(), tz_name)
        X = prepare_X(features).dropna().drop(columns=SEASON_FEATURES, errors="ignore")
        if X.empty:
            continue
        as_of_days = local_days(X.index, tz_name)
        level = X[PERSISTENCE].to_numpy()
        labels = {k: day_ahead_labels(X.index, day_means, tz_name, k) for k in DAYS_AHEAD}
        loaded.append((station.station_id, X, as_of_days, labels))
        for k in DAYS_AHEAD:
            y = labels[k]
            target_days = as_of_days + pd.Timedelta(days=k)
            for fold, (start, end) in FOLDS.items():
                train, test = split(as_of_days, target_days, ~np.isnan(y), start, end)
                if test.sum() < 24 * 7 or train.sum() < min_rows:
                    continue
                args = (fold, k, station.station_id, target_days[test], y[test], level[test])
                parts.append(frame("no-season", *args, fit_predict(X[train], y[train], X[test], quantiles, params)))
                ratio = fit_predict(X[train], log_ratio(y[train], level[train]), X[test], quantiles, params)
                parts.append(frame("ratio", *args, from_log_ratio(ratio, level[test])))
        print(f"{station.station_id} done", file=sys.stderr, flush=True)
    session.close()

    X_all = pd.concat([X for _, X, _, _ in loaded], ignore_index=True)
    as_of_all = np.concatenate([days.to_numpy() for _, _, days, _ in loaded])
    station_all = np.concatenate([np.full(len(X), sid) for sid, X, _, _ in loaded])
    level_all = X_all[PERSISTENCE].to_numpy()
    for k in DAYS_AHEAD:
        y = np.concatenate([labels[k] for _, _, _, labels in loaded])
        target_days = as_of_all + np.timedelta64(k, "D")
        for fold, (start, end) in FOLDS.items():
            train, test = split(as_of_all, target_days, ~np.isnan(y), start.to_datetime64(), end.to_datetime64())
            if not test.any() or train.sum() < min_rows:
                continue
            ratio = fit_predict(X_all[train], log_ratio(y[train], level_all[train]), X_all[test], quantiles, pooled_params)
            parts.append(frame("pooled", fold, k, station_all[test], target_days[test], y[test], level_all[test],
                               from_log_ratio(ratio, level_all[test])))
            print(f"pooled day +{k} {fold} done", file=sys.stderr, flush=True)

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
          f"; no-go from grade {no_go}; one row per station, target day and hour the forecast was made; "
          f"features left out: {', '.join(SEASON_FEATURES)}")
    for fold, (start, end) in FOLDS.items():
        f = df[df.fold == fold]
        if f.empty:
            print(f"\n== {fold}: no data")
            continue
        print(f"\n== {fold}: target days {start:%Y-%m-%d} .. {end:%Y-%m-%d}")
        for k, at_k in f.groupby("k"):
            print(f"  day +{k}")
            for variant in VARIANTS:
                g = at_k[at_k.variant == variant]
                if g.empty:
                    continue
                actual = np.digitize(g.actual.to_numpy(), bounds)
                values = g[qcols].to_numpy()
                print(f"    {variant}: stations {g.station.nunique()}, rows {len(g)}, actual grade shares "
                      + "/".join(f"{np.mean(actual == i):.2f}" for i in range(len(bounds) + 1))
                      + f" | MAE {np.mean(np.abs(g[median] - g.actual)):.1f} (last-24h {np.mean(np.abs(g.persist - g.actual)):.1f})"
                      + f", bias {np.mean(g[median] - g.actual):+.1f} | actual below "
                      + " ".join(f"q{q}: {np.mean(g.actual.to_numpy() < values[:, i]):.2f}" for i, q in enumerate(quantiles)))
                for decision in DECISIONS:
                    print(grade_line(f"rule p>={decision}", actual, grades(values, quantiles, bounds, decision), no_go))
                print(grade_line("last-24h mean", actual, np.digitize(g.persist.to_numpy(), bounds), no_go))


if __name__ == "__main__":
    main()
