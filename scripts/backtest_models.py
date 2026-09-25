"""Offline backtest of the live forecast models on data they never saw.

Run inside the Airflow scheduler container (it has the models and DB access):
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/backtest_models.py

Test window per (station, pollutant, horizon) = from the active model's
training_window_end (its holdout start) to now. Read-only: no DB writes.
Compares the production decision rule (P(value > threshold) >= 0.3 from the
quantile models) with naive baselines, hourly and per local day.
"""
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from common.constants import DEFAULT_HORIZONS_HOURS, ModelType, Pollutant
from db.models import Station
from db.session import get_session
from models import exceedance, registry
from models.lightgbm_pipeline import predict as lgbm_predict
from models.train import build_training_matrix
from sqlalchemy import select

now = datetime.now(timezone.utc)
session = get_session()
thresholds = exceedance.load_thresholds()
decision = thresholds["exceedance_probability_decision_threshold"]
stations = session.execute(select(Station).where(Station.is_active)).scalars().all()

rows = []
for pol in (Pollutant.PM25, Pollutant.NO2):
    thr = exceedance.get_health_threshold_concentration(pol, thresholds)
    for st in stations:
        for h in DEFAULT_HORIZONS_HOURS:
            qms = registry.get_active_models(session, st.station_id, pol, h, ModelType.QUANTILE_REGRESSOR)
            if not qms:
                continue
            test_start = max(m.training_window_end for m in qms)
            if not all(os.path.exists(m.artifact_path) for m in qms):
                print(f"skip {st.station_id}/{pol.value}/{h}h: model artifact file missing", file=sys.stderr)
                continue
            X, y = build_training_matrix(session, st.station_id, pol, h, test_start, now)
            if X.empty:
                continue
            preds = {m.quantile: lgbm_predict(registry.load_booster(m.artifact_path), X) for m in qms}
            prob = np.array([exceedance.probability_from_quantiles({q: p[i] for q, p in preds.items()}, thr) for i in range(len(X))])
            rows.append(pd.DataFrame({
                "pollutant": pol.value, "station": st.station_id, "h": h, "thr": thr,
                "target_time": X.index + pd.Timedelta(hours=h),
                "actual": y.values,
                "median": preds[0.5],
                "rule": prob >= decision,
                "persist": X["lag_1h"].values,
                "roll24": X["rolling_mean_24h"].values,
            }))
    print(f"{pol.value}: done", file=sys.stderr)
session.close()
df = pd.concat(rows, ignore_index=True)
df["date"] = df["target_time"].dt.tz_convert("Asia/Kolkata").dt.date


def prf(actual, pred):
    tp = int((actual & pred).sum()); fn = int((actual & ~pred).sum()); fp = int((~actual & pred).sum())
    r = tp / (tp + fn) if tp + fn else float("nan"); p = tp / (tp + fp) if tp + fp else float("nan")
    return f"R={r:.2f} P={p:.2f} (tp={tp} fn={fn} fp={fp})"


print(f"test window: {df.target_time.min()} .. {df.target_time.max()}  rows={len(df)}  stations={df.station.nunique()}")
for pol, g in df.groupby("pollutant"):
    print(f"\n=== {pol}  threshold {g.thr.iloc[0]} ug/m3 ===")
    print(f"{'h':>4} {'n':>6} {'MAE model':>9} {'persist':>8} {'roll24':>7} | hourly exceedance: actual>thr count")
    for h, gh in g.groupby("h"):
        mae = lambda c: np.nanmean(np.abs(gh[c] - gh.actual))
        print(f"{h:>4} {len(gh):>6} {mae('median'):>9.1f} {mae('persist'):>8.1f} {mae('roll24'):>7.1f} | {int((gh.actual > gh.thr).sum())}")
    if pol != "pm25":
        continue
    for h, gh in g.groupby("h"):
        a = (gh.actual > gh.thr).values
        print(f"\n h={h} HOURLY  rule: {prf(a, gh.rule.values)}")
        print(f"         median>thr: {prf(a, (gh['median'] > gh.thr).values)}")
        print(f"         persist>thr: {prf(a, (gh.persist > gh.thr).values)}")
        print(f"         roll24>thr: {prf(a, (gh.roll24 > gh.thr).values)}")
        d = gh.groupby(["station", "date"]).agg(
            act_max=("actual", "max"), act_mean=("actual", "mean"), n=("actual", "size"),
            rule=("rule", "any"), roll24=("roll24", "max"), med=("median", "max"))
        d = d[d.n >= 18]
        thr = gh.thr.iloc[0]
        for label, act in (("day max>thr", d.act_max > thr), ("day MEAN>thr (CPCB 24h)", d.act_mean > thr)):
            print(f"   DAILY {label:<24} days={len(d)} actual={int(act.sum())}  rule: {prf(act.values, d.rule.values)}"
                  f" | roll24: {prf(act.values, (d.roll24 > thr).values)}")
