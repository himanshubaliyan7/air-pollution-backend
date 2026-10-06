"""What does CPCB's Hourly_sub_index measure? (P0 step 2, 2026-09-30)

The CAAQMS feed publishes, per station and pollutant, AQI sub-indices
Min / Max / Avg / Hourly_sub_index (stored in station_aqi_snapshots since
2026-09-27). If one of them is the sub-index of the latest HOURLY
concentration, inverting it through the CPCB breakpoints gives an hourly
ug/m3 value we could use as a model input when OpenAQ's CPCB relay stalls.

This script compares each inverted sub-index with the OpenAQ hourly
concentrations stored for the same station, for several hypotheses:
  - hourly sub-index vs the hour ending at lastupdate (and neighbouring hours,
    in case of an alignment offset)
  - hourly sub-index vs the trailing 24 h mean
  - Avg / Max / Min vs the trailing 24 h mean / max / min
Only aggregate statistics are printed. Nothing is written.

Run inside the Airflow scheduler container:
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/cpcb_subindex_study.py
Optional args after "python -": --since 2026-09-27
"""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select

from common.constants import Pollutant
from common.subindex import FEED_IDS, inverter
from db.models import RawSensorReading, StationAqiSnapshot
from db.session import get_session
from models.exceedance import load_thresholds

MIN_HOURS_FOR_24H = 18


def stats(pairs: list[tuple[float, float]], threshold: float | None) -> str:
    if len(pairs) < 10:
        return f"n={len(pairs):5d}  (too few)"
    est, act = np.array(pairs).T
    err = est - act
    rel = np.abs(err) / np.maximum(act, 1.0)
    r = np.corrcoef(est, act)[0, 1] if est.std() > 0 and act.std() > 0 else float("nan")
    out = (
        f"n={len(pairs):5d}  bias={err.mean():+7.1f}  MAE={np.abs(err).mean():6.1f}  "
        f"medAE={np.median(np.abs(err)):6.1f}  r={r:5.2f}  within10%={np.mean(rel <= 0.10):4.0%}"
    )
    positive = act >= 5.0  # ratios of near-zero values are noise
    if positive.sum() >= 10:
        ratio = est[positive] / act[positive]
        out += f"  ratio med={np.median(ratio):.3f} IQR={np.percentile(ratio, 25):.3f}-{np.percentile(ratio, 75):.3f}"
    if threshold is not None:
        agree = np.mean((est > threshold) == (act > threshold))
        out += f"  >{threshold:g} agree={agree:4.0%} (actual>{threshold:g}: {np.mean(act > threshold):4.0%})"
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-09-27")
    args = parser.parse_args()
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    thresholds = load_thresholds()

    with get_session() as session:
        snaps = session.execute(
            select(StationAqiSnapshot).where(
                StationAqiSnapshot.pollutant_id.in_(list(FEED_IDS)),
                StationAqiSnapshot.source_updated_at >= since,
                StationAqiSnapshot.sub_index_hourly.isnot(None),
            )
        ).scalars().all()
        stations = {s.station_id for s in snaps}
        readings = session.execute(
            select(RawSensorReading.station_id, RawSensorReading.pollutant,
                   RawSensorReading.observed_at, RawSensorReading.value).where(
                RawSensorReading.station_id.in_(stations),
                RawSensorReading.pollutant.in_(list(FEED_IDS.values())),
                RawSensorReading.observed_at >= since - timedelta(hours=30),
            )
        ).all()

    # Hourly series keyed to the half hour (CPCB hours start at :00 IST = :30 UTC).
    def key(ts: datetime) -> int:
        return round(ts.timestamp() / 1800)

    series: dict[tuple[str, Pollutant], dict[int, float]] = defaultdict(dict)
    for station_id, pollutant, observed_at, value in readings:
        series[(station_id, pollutant)][key(observed_at)] = value

    print(f"snapshots since {since:%Y-%m-%d}: {len(snaps)} at {len(stations)} stations; "
          f"sensor readings loaded: {len(readings)}")
    first_obs = min((o for _, _, o, _ in readings), default=None)
    last_obs = max((o for _, _, o, _ in readings), default=None)
    print(f"sensor readings span: {first_obs} .. {last_obs}")

    for feed_id, pollutant in FEED_IDS.items():
        breakpoints = thresholds["pollutants"][pollutant.value]["breakpoints"]
        invert, top = inverter(breakpoints)
        threshold = 91.0 if pollutant is Pollutant.PM25 else None
        tests: dict[str, list[tuple[float, float]]] = defaultdict(list)
        capped = seen = 0
        for s in (x for x in snaps if x.pollutant_id == feed_id):
            hours = series.get((s.station_id, pollutant))
            if not hours:
                continue
            seen += 1
            # lastupdate is a whole IST hour (= :30 UTC) while OpenAQ hours start on the
            # whole UTC hour, so offsets are tested in half-hour steps; a label is the
            # start of the OpenAQ hour relative to lastupdate.
            base = key(s.source_updated_at)
            hourly, is_capped = invert(s.sub_index_hourly)
            capped += is_capped
            for step in range(-8, 5):
                actual = hours.get(base + step)
                if actual is not None and not is_capped:
                    tests[f"hourly vs hour starting at lastupdate {step / 2:+.1f}h"].append((hourly, actual))
            for step in (-3, -1):  # the last full UTC hour before / straddling lastupdate
                window = [hours.get(base + step - 2 * i) for i in range(24)]
                window = [v for v in window if v is not None]
                if len(window) < MIN_HOURS_FOR_24H:
                    continue
                mean24, max24, min24 = float(np.mean(window)), max(window), min(window)
                tag = f"24h ending {step / 2 + 1:+.1f}h"
                if not is_capped:
                    tests[f"hourly vs mean of {tag}"].append((hourly, mean24))
                for label, sub, actual in (("avg", s.sub_index_avg, mean24), ("max", s.sub_index_max, max24),
                                           ("min", s.sub_index_min, min24)):
                    if sub is not None:
                        value, c = invert(sub)
                        if not c:
                            tests[f"{label} vs {label} of {tag}"].append((value, actual))
        print(f"\n== {feed_id} ({pollutant.value}): snapshots with sensor history {seen}, hourly sub-index at cap {capped}")
        for name in sorted(tests):
            print(f"  {name:52s} {stats(tests[name], threshold)}")


if __name__ == "__main__":
    main()
