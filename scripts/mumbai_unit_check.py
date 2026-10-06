"""Are the units of a region's stored OpenAQ readings right? (read-only, owner-run)

Why: OpenAQ labelled Delhi's CPCB NO2 "ppb" although the values were ug/m3, and
converting the label overstated stored NO2 by 1.88x for five days. For any other
region the label is unverified, so the ingestion drops "ppb" NO2 there
(ingestion.sources.openaq.MISLABEL_VERIFIED_REGIONS) until this check says which
reading is true.

What: for each station in the region, inverts CPCB's hourly AQI sub-index (from
the feed, stored in station_aqi_snapshots) back to a concentration through the
CPCB breakpoints and divides it by the stored OpenAQ concentration. Reads, per
pollutant, the median ratio feed / stored, pooled and per station:
  ~1.00  the stored value is in ug/m3 and agrees with CPCB
  ~0.53  stored is 1.88x too high (a ppb conversion of a value that was ug/m3)
  ~1.88  stored is 1.88x too low (a ppb label that is really ug/m3, not relabelled)
CPCB's hour and OpenAQ's hour are offset by an unknown amount of half hours, so
the ratio is shown for each offset (as in cpcb_subindex_study.py) and the offset
where the most readings agree within 10% is used for the per-station lines.
Wait for a day or more of data after the first hourly runs; a station with no
stored NO2 is listed as unverifiable (the ingestion drops unverified ppb NO2).
Nothing is written.

Run inside the Airflow scheduler container:
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/mumbai_unit_check.py
Optional args after "python -": --region mumbai --since 2026-10-07
"""

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select

from common.constants import Pollutant, SensorSourceName
from common.regions import get_region
from db.models import RawSensorReading, Station, StationAqiSnapshot
from db.session import get_session
from models.exceedance import load_thresholds
from scripts.cpcb_subindex_study import FEED_IDS, inverter

OFFSET_STEPS = range(-8, 5)  # half hours between CPCB's lastupdate and the start of OpenAQ's hour
MIN_PAIRS = 10
MIN_CONCENTRATION = 5.0  # ratios of near-zero values are noise


def half_hour_key(ts: datetime) -> int:
    return round(ts.timestamp() / 1800)


def ratios_by_offset(pairs_by_offset: dict[int, list[tuple[float, float]]]) -> dict[int, np.ndarray]:
    """offset -> array of feed / stored for pairs where the stored value is not near zero."""
    out = {}
    for step, pairs in pairs_by_offset.items():
        usable = [f / s for f, s in pairs if s >= MIN_CONCENTRATION]
        if len(usable) >= MIN_PAIRS:
            out[step] = np.array(usable)
    return out


def best_offset(ratios: dict[int, np.ndarray]) -> int | None:
    """The offset where the most ratios are within 10% of 1, else of 0.53 / 1.88 (a unit
    error is as informative as a match, so the offset is picked by the tightest cluster)."""
    def tight(r: np.ndarray) -> float:
        return max(np.mean(np.abs(r / c - 1) <= 0.10) for c in (1.0, 1 / 1.882, 1.882))
    return max(ratios, key=lambda k: tight(ratios[k]), default=None)


def verdict(median: float) -> str:
    for centre, text in ((1.0, "units agree"), (1 / 1.882, "stored ~1.88x TOO HIGH"), (1.882, "stored ~1.88x TOO LOW")):
        if abs(median / centre - 1) <= 0.10:
            return text
    return "no clear unit pattern"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="mumbai")
    parser.add_argument("--since", help="UTC date, default 3 days ago")
    args = parser.parse_args()
    region = get_region(args.region)
    if region is None:
        raise SystemExit(f"unknown region {args.region!r}")
    since = (datetime.fromisoformat(args.since) if args.since else datetime.now() - timedelta(days=3)).replace(tzinfo=timezone.utc)
    thresholds = load_thresholds()

    with get_session() as session:
        stations = [s for s in session.execute(select(Station).where(Station.is_active)).scalars() if region.contains(s.lat, s.lon)]
        ids = [s.station_id for s in stations]
        snaps = session.execute(
            select(StationAqiSnapshot).where(
                StationAqiSnapshot.station_id.in_(ids), StationAqiSnapshot.pollutant_id.in_(list(FEED_IDS)),
                StationAqiSnapshot.source_updated_at >= since, StationAqiSnapshot.sub_index_hourly.isnot(None),
            )
        ).scalars().all()
        readings = session.execute(
            select(RawSensorReading.station_id, RawSensorReading.pollutant, RawSensorReading.observed_at, RawSensorReading.value)
            .where(
                RawSensorReading.station_id.in_(ids), RawSensorReading.source == SensorSourceName.OPENAQ,
                RawSensorReading.observed_at >= since - timedelta(hours=6),
            )
        ).all()

    series: dict[tuple[str, Pollutant], dict[int, float]] = defaultdict(dict)
    for station_id, pollutant, observed_at, value in readings:
        series[(station_id, pollutant)][half_hour_key(observed_at)] = value
    print(f"region {region.id}: {len(stations)} active stations, {len(snaps)} CPCB snapshots since {since:%Y-%m-%d %H:%M}, "
          f"{len(readings)} stored OpenAQ readings")

    for feed_id, pollutant in FEED_IDS.items():
        invert, _ = inverter(thresholds["pollutants"][pollutant.value]["breakpoints"])
        pooled: dict[int, list] = defaultdict(list)
        per_station: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
        for s in (x for x in snaps if x.pollutant_id == feed_id):
            feed_value, capped = invert(s.sub_index_hourly)
            hours = series.get((s.station_id, pollutant))
            if capped or not hours:
                continue
            for step in OFFSET_STEPS:
                stored = hours.get(half_hour_key(s.source_updated_at) + step)
                if stored is not None:
                    pooled[step].append((feed_value, stored))
                    per_station[s.station_id][step].append((feed_value, stored))
        with_data = {sid for (sid, p) in series if p == pollutant}
        print(f"\n== {pollutant.value}: {len(with_data)} of {len(stations)} stations have stored OpenAQ readings")
        if not with_data:
            print("  nothing to compare: no stored OpenAQ readings (for NO2 that is also what an unverified ppb label looks like)")
            continue
        ratios = ratios_by_offset(pooled)
        best = best_offset(ratios)
        if best is None:
            print(f"  fewer than {MIN_PAIRS} comparable hours at every offset; wait for more data")
            continue
        for step, r in sorted(ratios.items()):
            print(f"  offset {step / 2:+.1f}h  n={len(r):4d}  ratio med={np.median(r):.3f} IQR={np.percentile(r, 25):.3f}-{np.percentile(r, 75):.3f}"
                  f"{'   <- used below' if step == best else ''}")
        print(f"  pooled at {best / 2:+.1f}h: median {np.median(ratios[best]):.3f} -> {verdict(float(np.median(ratios[best])))}")
        names = {s.station_id: s.name for s in stations}
        for sid, by_step in sorted(per_station.items()):
            one = ratios_by_offset({best: by_step.get(best, [])}).get(best)
            if one is not None:
                print(f"    {names[sid][:44]:44s} n={len(one):3d} median {np.median(one):.3f}  {verdict(float(np.median(one)))}")


if __name__ == "__main__":
    main()
