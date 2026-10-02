"""Backtest of graded daily verdicts on last winter (read-only, PM2.5).

The owner chose graded verdicts on 2026-10-02: a day's verdict is the CPCB
category of its MEAN (go below Poor, caution for Poor, no-go from Very Poor).
This forecasts "the mean of the local day k days ahead" (k = 1..5) using all
history EXCEPT a test window, and scores the forecasts on that window, twice:

  onset   2025-10-15 .. 2025-11-30   the weeks the air turns bad (Diwali, stubble)
  winter  2026-01-15 .. 2026-03-22   deep winter easing into spring

Earlier runs (docs/PROJECT_HANDOFF.md section 0):
  1. Models as production built them (one per station, every feature) were far
     worse than "tomorrow is like the last 24 hours".
  2. Without the day-of-year and season-flag features the bias mostly went
     away. One model for all stations, forecasting log(day's mean / last-24h
     mean), was the best variant, but only level with the last-24h mean in
     exact grades. Its gain is a usable spread: a cautious rule grades fewer
     days too mild.

Run 3 (2026-10-03) asked what to build:

  level           no model at all: the last-24h mean times the ratio seen in the
                  training rows (10 %, 50 %, 90 % quantiles of that ratio, all
                  stations together). If this matches `pooled`, the trees add
                  nothing and the simple rule is the one to ship.
  pooled          as in run 2, for reference.
  pooled+weather  pooled, plus the target day's own mean wind speed and
                  humidity from the weather reanalysis. A real forecast would
                  have to use a weather FORECAST for that day, so this is the
                  best case: it shows whether adding weather forecasts is worth
                  building.

Result: `level` is as exact as `pooled` (0.61-0.50 at the onset, 0.58-0.52 in
winter at p >= 0.5) and its quantiles are the best calibrated (8 / 47 / 91 % of
onset days below q0.1 / q0.5 / q0.9). Perfect weather adds 2-8 points in winter
and nothing at the onset. So the model-free rule is the candidate, and what is
left to choose is the decision probability.

By default this now runs `level` only (a few minutes) for decision
probabilities 0.3, 0.4 and 0.5; --models adds the two pooled models again
(roughly 30 minutes).

For each it prints, per day ahead: the error of the expected value; whether
the 10/50/90 % quantiles hold that share of the days; and, per decision
probability, how often the grade is exact, too mild (the dangerous error) or
too harsh, with recall and precision of no-go and of caution-or-worse, and how
many real no-go days were called "go". The first line of each block is the
last-24h mean itself. Nothing is written to the DB.

Run inside the Airflow scheduler container:
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/daily_verdict_backtest.py 2> /dev/null
    nohup sudo docker exec -i docker-airflow-scheduler-1 python - --models < scripts/daily_verdict_backtest.py > ~/daily_backtest.log 2>&1 &
"""

import argparse

import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from common.constants import Pollutant
from db.models import RawWeatherReading, Station
from db.readings import hourly_readings
from db.session import get_session
from features.feature_store import read_features
from ingestion.weather.grid import nearest_grid_cell_id
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
SEASON_FEATURES = ["doy_sin", "doy_cos", "is_stubble_season", "is_diwali_window"]
TARGET_WEATHER = ["wind_speed", "relative_humidity"]
VARIANTS = ("level", "pooled", "pooled+weather")
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
            f"caution or worse {prf(actual >= 1, predicted >= 1)} | "
            f"no-go days called go {np.mean(predicted[actual >= no_go] == 0) if (actual >= no_go).any() else float('nan'):.2f} | shares "
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


def daily_weather(session, grid_cell_id: str, tz_name: str, cache: dict) -> pd.DataFrame:
    """Mean wind speed and humidity per local day for one weather grid cell
    (index: the day's local midnight, naive)."""
    key = (grid_cell_id, tz_name)
    if key not in cache:
        rows = session.execute(
            select(RawWeatherReading.observed_at, RawWeatherReading.wind_speed, RawWeatherReading.relative_humidity)
            .where(RawWeatherReading.grid_cell_id == grid_cell_id, RawWeatherReading.observed_at >= HISTORY_START,
                   RawWeatherReading.is_superseded.is_(False))
        ).all()
        if rows:
            hourly = pd.DataFrame(
                {"wind_speed": [r.wind_speed for r in rows], "relative_humidity": [r.relative_humidity for r in rows]},
                index=pd.DatetimeIndex([r.observed_at for r in rows], tz="UTC"),
            ).groupby(level=0).mean()
            grouped = hourly.groupby(local_days(hourly.index, tz_name))
            cache[key] = grouped.mean()[grouped.size() >= 18]
        else:
            cache[key] = pd.DataFrame(columns=TARGET_WEATHER)
    return cache[key]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", action="store_true", help="also fit the pooled models (slow)")
    with_models = parser.parse_args().models
    variants = VARIANTS if with_models else VARIANTS[:1]
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
    loaded = []  # per station: (station_id, X, as_of_days, {k: labels}, {k: target-day weather})
    weather_cache: dict = {}
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
        weather = daily_weather(session, nearest_grid_cell_id(station.lat, station.lon), tz_name, weather_cache)
        labels = {k: day_ahead_labels(X.index, day_means, tz_name, k) for k in DAYS_AHEAD}
        target_weather = {
            k: weather.reindex(as_of_days + pd.Timedelta(days=k))[TARGET_WEATHER].to_numpy(dtype="float64")
            for k in DAYS_AHEAD
        }
        loaded.append((station.station_id, X, as_of_days, labels, target_weather))
        print(f"{station.station_id} loaded", file=sys.stderr, flush=True)
    session.close()

    parts = []
    X_all = pd.concat([item[1] for item in loaded], ignore_index=True)
    as_of_all = np.concatenate([item[2].to_numpy() for item in loaded])
    station_all = np.concatenate([np.full(len(item[1]), item[0]) for item in loaded])
    level_all = X_all[PERSISTENCE].to_numpy()
    for k in DAYS_AHEAD:
        y = np.concatenate([item[3][k] for item in loaded])
        weather = np.concatenate([item[4][k] for item in loaded])
        X_weather = X_all.assign(**{f"target_day_{name}": weather[:, i] for i, name in enumerate(TARGET_WEATHER)})
        has_weather = ~np.isnan(weather).any(axis=1)
        target_days = as_of_all + np.timedelta64(k, "D")
        for fold, (start, end) in FOLDS.items():
            # Every variant is scored on the same rows: those whose target day has weather.
            train, test = split(as_of_all, target_days, ~np.isnan(y) & has_weather, start.to_datetime64(), end.to_datetime64())
            if not test.any() or train.sum() < min_rows:
                continue
            args = (fold, k, station_all[test], target_days[test], y[test], level_all[test])
            ratios = log_ratio(y[train], level_all[train])
            constant = np.tile(np.quantile(ratios, quantiles), (int(test.sum()), 1))
            parts.append(frame("level", *args, from_log_ratio(constant, level_all[test])))
            for variant, X in (("pooled", X_all), ("pooled+weather", X_weather)) if with_models else ():
                predicted = fit_predict(X[train], ratios, X[test], quantiles, pooled_params)
                parts.append(frame(variant, *args, from_log_ratio(predicted, level_all[test])))
            print(f"day +{k} {fold} done", file=sys.stderr, flush=True)

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
            g = at_k[at_k.variant == variants[0]]
            actual = np.digitize(g.actual.to_numpy(), bounds)
            print(f"  day +{k}: stations {g.station.nunique()}, rows {len(g)}, actual grade shares "
                  + "/".join(f"{np.mean(actual == i):.2f}" for i in range(len(bounds) + 1))
                  + f"; last-24h mean: MAE {np.mean(np.abs(g.persist - g.actual)):.1f}, bias {np.mean(g.persist - g.actual):+.1f}")
            print(grade_line("last-24h mean", actual, np.digitize(g.persist.to_numpy(), bounds), no_go))
            for variant in variants:
                g = at_k[at_k.variant == variant]
                values = g[qcols].to_numpy()
                print(f"    {variant}: MAE {np.mean(np.abs(g[median] - g.actual)):.1f}, bias {np.mean(g[median] - g.actual):+.1f}"
                      + " | actual below "
                      + " ".join(f"q{q}: {np.mean(g.actual.to_numpy() < values[:, i]):.2f}" for i, q in enumerate(quantiles)))
                for decision in DECISIONS:
                    print(grade_line(f"rule p>={decision}", actual, grades(values, quantiles, bounds, decision), no_go))


if __name__ == "__main__":
    main()
