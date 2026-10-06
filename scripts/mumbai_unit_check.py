"""Are a region's OpenAQ unit labels right? (read-only, owner-run, makes OpenAQ requests)

Why: OpenAQ labelled Delhi's CPCB NO2 "ppb" although the values were ug/m3, and
converting the label overstated stored NO2 by 1.88x for five days. For any other
region the label is unverified, so ingestion SKIPS "ppb" NO2 there
(ingestion.sources.openaq.MISLABEL_VERIFIED_REGIONS) and nothing is stored to
compare. This check therefore fetches the readings itself, straight from OpenAQ.

What: for a sample of the region's active stations (default 8, those with a CPCB
hourly sub-index stored in the last day) it fetches the last 24 hours of NO2 and
PM2.5 with the unit label OpenAQ gives (at most 3 requests per station, paced by
the OpenAQ source class), and compares, per pollutant, the raw value and the
value converted with that label (ppb -> ug/m3 x 1.88 for NO2) against the
concentration derived from CPCB's hourly sub-index in station_aqi_snapshots (the
comparison of scripts/cpcb_subindex_study.py). CPCB's hour and OpenAQ's hour are
offset by an unknown number of half hours, so the offset with the tightest
agreement over all pairs is used. Prints, per pollutant, the pairs compared, the
median ratio raw/CPCB and converted/CPCB with interquartile range, and a
conclusion. Writes nothing. OpenAQ's CPCB relay trails by hours, so right after
seeding there may be few overlapping hours: re-run the next day.

How to act on the conclusion (NO2 matters; PM2.5 is the control and should say
"stored label is right"):
  "label says ppb but values are ug/m3"  OpenAQ's NO2 label is wrong here as in
      Delhi: add the region id to MISLABEL_VERIFIED_REGIONS in
      ingestion/sources/openaq.py (the value is then stored as is).
  "stored label is right"  the label is true (a genuine ppb sensor). Do not add the
      region to MISLABEL_VERIFIED_REGIONS. Note that ingestion today SKIPS ppb NO2
      outside the verified list rather than converting it, so a genuine-ppb region
      needs a small code change first (declared_unit returning the label, so the
      ppb conversion applies): report the result instead of guessing.
  "not enough overlapping hours"  decide nothing; run again later or raise --limit.
  "values match neither"  neither reading matches CPCB: do not touch anything.

Run inside the Airflow scheduler container (the scripts package is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/mumbai_unit_check.py
Optional args after "python -": --region mumbai --limit 8
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import select

from common.config import get_settings
from common.constants import Pollutant
from common.regions import get_region
from common.subindex import FEED_IDS, inverter
from db.models import Station, StationAqiSnapshot
from db.session import get_session
from ingestion.config import ACTIVE_SOURCE, SENSOR_SOURCE_REGISTRY
from ingestion.units import to_canonical

OFFSET_STEPS = range(-8, 5)  # half hours between CPCB's lastupdate and the start of OpenAQ's hour
MIN_PAIRS = 10
MIN_CONCENTRATION = 5.0  # ratios against near-zero concentrations are noise
TOLERANCE = 0.10
PPB_FACTOR = 1.882  # NO2 ug/m3 per ppb, as ingestion.units

Pair = tuple[float, float, float, str]  # cpcb ug/m3, raw value, converted ug/m3, unit label


def half_hour_key(ts: datetime) -> int:
    return round(ts.timestamp() / 1800)


def _centred(ratios: np.ndarray, centre: float) -> bool:
    return abs(float(np.median(ratios)) / centre - 1) <= TOLERANCE


def best_offset(pairs_by_offset: dict[int, list[Pair]]) -> int | None:
    """The offset where most raw/CPCB ratios sit within 10% of 1 or of a ppb/ug/m3 mix-up
    (a unit error is as informative as a match, so the tightest cluster picks the offset)."""
    def tight(pairs: list[Pair]) -> float:
        r = np.array([raw / c for c, raw, _, _ in pairs])
        return max(np.mean(np.abs(r / k - 1) <= TOLERANCE) for k in (1.0, 1 / PPB_FACTOR, PPB_FACTOR))
    usable = {k: v for k, v in pairs_by_offset.items() if len(v) >= MIN_PAIRS}
    return max(usable, key=lambda k: tight(usable[k]), default=None)


def conclusion(pairs: list[Pair]) -> str:
    if len(pairs) < MIN_PAIRS:
        return "not enough overlapping hours"
    raw = np.array([r / c for c, r, _, _ in pairs])
    conv = np.array([v / c for c, _, v, _ in pairs])
    ppb = Counter(label for *_, label in pairs).most_common(1)[0][0].strip() == "ppb"
    if ppb and _centred(raw, 1.0):
        return "label says ppb but values are ug/m3"
    if _centred(conv, 1.0):
        return "stored label is right"
    return "values match neither (do not act on this)"


def quartiles(r: np.ndarray) -> str:
    return f"median {np.median(r):.3f} IQR {np.percentile(r, 25):.3f}-{np.percentile(r, 75):.3f}"


def summarise(pollutant: Pollutant, pairs_by_offset: dict[int, list[Pair]]) -> list[str]:
    best = best_offset(pairs_by_offset)
    pairs = pairs_by_offset.get(best, []) if best is not None else []
    head = f"== {pollutant.value}: {len(pairs)} pairs compared"
    if len(pairs) < MIN_PAIRS:
        return [head, f"  conclusion: {conclusion(pairs)}"]
    raw = np.array([r / c for c, r, _, _ in pairs])
    conv = np.array([v / c for c, _, v, _ in pairs])
    labels = dict(Counter(label for *_, label in pairs))
    return [
        f"{head} (offset {best / 2:+.1f}h, labels {labels})",
        f"  raw / CPCB        {quartiles(raw)}",
        f"  converted / CPCB  {quartiles(conv)}",
        f"  conclusion: {conclusion(pairs)}",
    ]


def pair_up(snaps, rows: dict[tuple[str, Pollutant], list], thresholds: dict) -> dict[Pollutant, dict[int, list[Pair]]]:
    """snaps: CPCB snapshots; rows: (station_id, pollutant) -> [(hour, value, label)]."""
    out: dict[Pollutant, dict[int, list[Pair]]] = {p: defaultdict(list) for p in FEED_IDS.values()}
    for feed_id, pollutant in FEED_IDS.items():
        invert, _ = inverter(thresholds["pollutants"][pollutant.value]["breakpoints"])
        for s in (x for x in snaps if x.pollutant_id == feed_id):
            hours = {half_hour_key(h): (v, lab) for h, v, lab in rows.get((s.station_id, pollutant), [])}
            cpcb, capped = invert(s.sub_index_hourly)
            if capped or cpcb < MIN_CONCENTRATION:
                continue
            for step in OFFSET_STEPS:
                hit = hours.get(half_hour_key(s.source_updated_at) + step)
                converted = to_canonical(pollutant, *hit) if hit else None
                if hit and converted:
                    out[pollutant][step].append((cpcb, hit[0], converted[0], hit[1]))
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="mumbai")
    parser.add_argument("--limit", type=int, default=8, help="stations to sample")
    args = parser.parse_args()
    region = get_region(args.region)
    if region is None:
        raise SystemExit(f"unknown region {args.region!r}")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)

    with get_session() as session:
        snaps = session.execute(
            select(StationAqiSnapshot).where(
                StationAqiSnapshot.pollutant_id.in_(list(FEED_IDS)),
                StationAqiSnapshot.source_updated_at >= start - timedelta(hours=2),
                StationAqiSnapshot.sub_index_hourly.isnot(None),
            )
        ).scalars().all()
        with_cpcb = {s.station_id for s in snaps}
        stations = [
            s for s in session.execute(select(Station).where(Station.is_active).order_by(Station.station_id)).scalars()
            if region.contains(s.lat, s.lon) and s.station_id in with_cpcb
        ][: args.limit]
    snaps = [s for s in snaps if s.station_id in {x.station_id for x in stations}]
    print(f"region {region.id}: {len(stations)} sampled stations with a CPCB hourly sub-index in the last day")
    if not stations:
        print("nothing to compare: no active station of this region has a CPCB snapshot yet (wait for ingestion runs)")
        return

    source = SENSOR_SOURCE_REGISTRY[ACTIVE_SOURCE](api_key=get_settings().openaq_api_key)
    rows: dict[tuple[str, Pollutant], list] = {}
    for st in stations:
        for pollutant, hours in source.fetch_raw_hours(st.source_location_id, list(FEED_IDS.values()), start, end).items():
            rows[(st.station_id, pollutant)] = hours
    pairs = pair_up(snaps, rows, region.thresholds())
    for pollutant in FEED_IDS.values():
        print("\n".join(summarise(pollutant, pairs[pollutant])))


if __name__ == "__main__":
    main()
