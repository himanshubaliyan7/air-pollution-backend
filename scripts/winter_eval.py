"""Winter test: do models that have seen a Delhi winter forecast one better?

Trains throwaway PM2.5 quantile models on all history EXCEPT a winter test
window (plus a buffer on each side so no training label or feature overlaps
it), then scores them on that window next to the live models (which were
trained on Mar-Sep only) and a persistence baseline, using the production
decision rule. Nothing is written to the DB or the model registry.

Run inside the Airflow scheduler container:
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/winter_eval.py
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from sqlalchemy import select

from common.constants import DEFAULT_HORIZONS_HOURS, ModelType, Pollutant
from db.models import Station
from db.session import get_session
from models import exceedance, registry
from models.lightgbm_pipeline import fit_quantile_model
from models.lightgbm_pipeline import predict as lgbm_predict
from models.train import build_training_matrix, load_model_config

TEST_START = datetime(2026, 1, 15, tzinfo=timezone.utc)
TEST_END = datetime(2026, 3, 23, tzinfo=timezone.utc)
BUFFER = timedelta(days=5)  # > longest horizon (120 h) and the 48 h lag window
HISTORY_START = datetime(2025, 9, 25, tzinfo=timezone.utc)
POLLUTANT = Pollutant.PM25

config = load_model_config()
quantiles, qparams = config["quantiles"], config["lightgbm"]["quantile"]
thresholds = exceedance.load_thresholds()
thr = exceedance.get_health_threshold_concentration(POLLUTANT, thresholds)
decision = thresholds["exceedance_probability_decision_threshold"]
now = datetime.now(timezone.utc)

session = get_session()
stations = session.execute(select(Station).where(Station.is_active)).scalars().all()
rows = []
for st in stations:
    for h in DEFAULT_HORIZONS_HOURS:
        live = registry.get_active_models(session, st.station_id, POLLUTANT, h, ModelType.QUANTILE_REGRESSOR)
        if not live or not all(os.path.exists(m.artifact_path) for m in live):
            continue
        X, y = build_training_matrix(session, st.station_id, POLLUTANT, h, HISTORY_START, now)
        if X.empty:
            continue
        label_time = X.index + pd.Timedelta(hours=h)
        test = (label_time >= TEST_START) & (label_time < TEST_END)
        train = (label_time < TEST_START - BUFFER) | (X.index >= TEST_END + BUFFER)
        if test.sum() < 24 * 7 or train.sum() < config["training"]["min_training_rows"]:
            continue
        Xtr, ytr, Xte, yte = X[train], y[train], X[test], y[test]

        def rule(preds):
            return np.array([exceedance.probability_from_quantiles({q: p[i] for q, p in preds.items()}, thr) >= decision
                             for i in range(len(Xte))])

        winter = {q: lgbm_predict(fit_quantile_model(Xtr, ytr, q, qparams), Xte) for q in quantiles}
        old = {m.quantile: lgbm_predict(registry.load_booster(m.artifact_path), Xte) for m in live}
        rows.append(pd.DataFrame({
            "station": st.station_id, "h": h, "target_time": label_time[test], "actual": yte.values,
            "winter_med": winter[0.5], "winter_rule": rule(winter),
            "live_med": old[0.5], "live_rule": rule(old),
            "persist": Xte["lag_1h"].values,
        }))
    print(f"{st.station_id} done", file=sys.stderr)
session.close()

df = pd.concat(rows, ignore_index=True)
df["date"] = df.target_time.dt.tz_convert("Asia/Kolkata").dt.date


def prf(actual, pred):
    tp = int((actual & pred).sum()); fn = int((actual & ~pred).sum()); fp = int((~actual & pred).sum())
    r = tp / (tp + fn) if tp + fn else float("nan"); p = tp / (tp + fp) if tp + fp else float("nan")
    return f"R={r:.2f} P={p:.2f}"


print(f"PM2.5 winter test {TEST_START:%Y-%m-%d}..{TEST_END:%Y-%m-%d}: rows={len(df)} stations={df.station.nunique()} "
      f"hours>thr={int((df.actual > thr).sum())} ({(df.actual > thr).mean():.0%})")
for h, g in df.groupby("h"):
    a = (g.actual > thr).values
    mae = lambda c: np.nanmean(np.abs(g[c] - g.actual))
    d = g.groupby(["station", "date"]).agg(act=("actual", "max"), n=("actual", "size"),
                                            w=("winter_rule", "any"), l=("live_rule", "any"), p=("persist", "max"))
    d = d[d.n >= 18]
    da = (d.act > thr).values
    print(f"h={h:>3} MAE winter={mae('winter_med'):5.1f} live={mae('live_med'):5.1f} persist={mae('persist'):5.1f} | "
          f"hourly rule: winter {prf(a, g.winter_rule.values)}  live {prf(a, g.live_rule.values)}  "
          f"persist {prf(a, (g.persist > thr).values)} | daily: winter {prf(da, d.w.values)}  live {prf(da, d.l.values)} "
          f"(days={len(d)}, bad={int(da.sum())})")
