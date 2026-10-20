"""Region-aware pieces of the scheduled tasks, kept out of tasks.py so that file stays
a thin adapter. A station's region is its position (common.regions.region_for_point);
there is no region column."""

from collections import defaultdict

from common.regions import region_for_point
from models import evaluation, exceedance

NO_REGION = "outside every region"


def station_region_name(lat: float, lon: float) -> str:
    region = region_for_point(lat, lon)
    return region.name if region else NO_REGION


def group_by_region(stations) -> dict[str, list]:
    """Stations keyed by region name; a station inside no region goes under NO_REGION."""
    groups: dict[str, list] = defaultdict(list)
    for s in stations:
        groups[station_region_name(s.lat, s.lon)].append(s)
    return dict(groups)


def thresholds_for_station(station, cache: dict) -> dict:
    """exceedance.thresholds_for_point for the station, cached. `cache` maps region id
    -> thresholds so a run reads each region's file once."""
    region = region_for_point(station.lat, station.lon)
    key = region.id if region else None
    if key not in cache:
        cache[key] = exceedance.thresholds_for_point(station.lat, station.lon)
    return cache[key]


def drift_by_region(rows, min_recall: float, min_days: int) -> list[tuple[str, float, int]]:
    """(region name, pooled recall, exceedance days) for each region whose day-ahead
    verdicts drifted. `rows` are (lat, lon, n_exceedance_days_actual, recall); the
    minimum-days and recall floors apply to each region on its own, so a small region's
    few days neither trigger nor are hidden by a large region's."""
    per_region: dict[str, list[tuple[int, float | None]]] = defaultdict(list)
    for lat, lon, n_actual, recall in rows:
        per_region[station_region_name(lat, lon)].append((n_actual, recall))
    drifted = []
    for name, region_rows in sorted(per_region.items()):
        if evaluation.missed_days_drift(region_rows, min_recall, min_days):
            recall, days = evaluation.pooled_recall(region_rows)
            drifted.append((name, recall, days))
    return drifted


def count_faults_by_region(rows, window_days: int) -> list[str]:
    """What is wrong with the evaluation rows themselves, per region, before their recall
    is believed. `rows` are (lat, lon, station_id, pollutant, computed_at,
    n_exceedance_days_actual, recall). A nightly row covers one station, pollutant and day,
    so a week holds at most pairs x days station-days and one row per pair and night;
    more means rows were scored or stored more than once (2026-10: both happened)."""
    per_region: dict[str, list] = defaultdict(list)
    for lat, lon, *rest in rows:
        per_region[station_region_name(lat, lon)].append(rest)
    faults = []
    for name, region_rows in sorted(per_region.items()):
        pairs = {(sid, pol) for sid, pol, *_ in region_rows}
        nights = {(sid, pol, computed_at.date()) for sid, pol, computed_at, *_ in region_rows}
        counted = sum(n for *_, n, recall in region_rows if recall is not None)
        possible = len(pairs) * window_days
        repeated = len(region_rows) - len(nights)
        if counted > possible or repeated:
            faults.append(
                f"{name}: {counted} station-days counted where {len(pairs)} station/pollutant pairs "
                f"over {window_days} days allow at most {possible}; {repeated} rows repeat a pair's night"
            )
    return faults
