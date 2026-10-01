"""Correct station coordinates that OpenAQ has wrong, from CPCB's own feed.

The station list comes from OpenAQ, which places some CPCB sites kilometres
from where CPCB says they are (2026-10-01: Sector-1 Noida 1.2 km, Aya Nagar
2.2 km, Pusa DPCC and Pusa IMD on one shared point 2.7 km off, North Campus
6.5 km). The CPCB feed is matched to those stations by name
(ingestion/loaders/aqi_snapshot_loader.match_station); this gives each one the
feed's coordinates, so a map shows it in the right place.

A station's coordinates also pick its weather grid cell (0.25 degrees). When a
correction moves a station into another cell, its weather features change and
no longer match what its models were trained on. Such a station is skipped
unless --allow-grid-change is given; afterwards rebuild its features over the
training window and let the next retrain pick them up:
    ... python - --days 372 --station <id> < scripts/rebuild_features.py

Without --apply it only reports. Fetches the live CPCB feed.

Run inside the Airflow scheduler container (scripts/ is not in the image):
    sudo docker exec -i docker-airflow-scheduler-1 python - < scripts/fix_station_coordinates.py
"""

import argparse
from dataclasses import dataclass

from sqlalchemy import select

from db.models import Station
from db.session import get_session
from ingestion.loaders.aqi_snapshot_loader import (
    MATCH_RADIUS_METERS,
    haversine_m,
    match_station,
    newest_openaq_readings,
)
from ingestion.sources.cpcb_caaqms import CpcbCaaqmsClient
from ingestion.weather.grid import nearest_grid_cell_id


@dataclass(frozen=True)
class Correction:
    station: Station
    feed_name: str
    lat: float
    lon: float
    distance_m: float
    old_cell: str
    new_cell: str

    @property
    def changes_grid_cell(self) -> bool:
        return self.old_cell != self.new_cell


def plan_corrections(stations, feed_stations, newest_openaq) -> tuple[list[Correction], list[str]]:
    """(corrections, problems) for feed stations given as (name, lat, lon).

    A correction is a station matched by name, not by distance: one already
    within MATCH_RADIUS_METERS of its feed station is left alone. A station
    that two feed stations claim is reported as a problem and not corrected."""
    claims: dict[str, list[Correction]] = {}
    for name, lat, lon in sorted(set(feed_stations)):
        station = match_station(stations, name, lat, lon, newest_openaq)
        if station is None:
            continue
        distance = haversine_m(lat, lon, station.lat, station.lon)
        if distance <= MATCH_RADIUS_METERS:
            continue
        claims.setdefault(station.station_id, []).append(Correction(
            station, name, lat, lon, distance,
            nearest_grid_cell_id(station.lat, station.lon), nearest_grid_cell_id(lat, lon),
        ))
    corrections, problems = [], []
    for station_id, claimed in sorted(claims.items()):
        if len(claimed) > 1:
            problems.append(f"{station_id}: claimed by {[c.feed_name for c in claimed]}, left unchanged")
        else:
            corrections.append(claimed[0])
    return corrections, problems


def fix(session, feed_stations, apply: bool, allow_grid_change: bool) -> dict:
    stations = list(session.execute(select(Station)).scalars().all())
    corrections, problems = plan_corrections(stations, feed_stations, newest_openaq_readings(session))
    moved, skipped = [], []
    for c in corrections:
        blocked = c.changes_grid_cell and not allow_grid_change
        note = (f"weather cell {c.old_cell} -> {c.new_cell}"
                + (" (SKIPPED: needs --allow-grid-change)" if blocked else "")) if c.changes_grid_cell else "same weather cell"
        print(f"{c.station.station_id} {c.station.name!r} <- {c.feed_name!r}: "
              f"({c.station.lat:.6f}, {c.station.lon:.6f}) -> ({c.lat:.6f}, {c.lon:.6f}), {c.distance_m:.0f} m, {note}")
        if blocked:
            skipped.append(c.station.station_id)
        else:
            moved.append(c.station.station_id)
            if apply:
                c.station.lat, c.station.lon = c.lat, c.lon
    for problem in problems:
        print("PROBLEM", problem)
    if apply:
        session.commit()
    print(f"{'corrected' if apply else 'would correct'}: {len(moved)}; skipped for a weather-cell change: {len(skipped)}; "
          f"problems: {len(problems)}" + ("" if apply else "; report only, rerun with --apply"))
    return {"moved": moved, "skipped": skipped, "problems": problems, "applied": apply}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="make the changes (default: report only)")
    parser.add_argument("--allow-grid-change", action="store_true",
                        help="also correct stations whose weather grid cell changes (rebuild their features afterwards)")
    args = parser.parse_args()
    feed_stations = {(r.station_name, r.lat, r.lon) for r in CpcbCaaqmsClient().fetch()}
    session = get_session()
    try:
        fix(session, feed_stations, args.apply, args.allow_grid_change)
    finally:
        session.close()


if __name__ == "__main__":
    main()
