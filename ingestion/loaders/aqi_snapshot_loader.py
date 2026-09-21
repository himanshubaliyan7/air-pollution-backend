"""Matches data.gov.in records to our stations by coordinates and stores each
hourly snapshot (idempotently), so history accumulates from now on."""

import logging
import math
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from db.models import Station, StationAqiSnapshot
from ingestion.sources.data_gov_in import AqiRecord

logger = logging.getLogger(__name__)

# Live check 2026-09-21: 42 of 44 Delhi feed stations were within 100 m of a
# station we already track; the rest were 2+ km away (a genuinely different
# site). 250 m leaves headroom for coordinate rounding without ever merging two
# distinct sites.
MATCH_RADIUS_METERS = 250.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_station(stations: list[Station], lat: float, lon: float) -> Station | None:
    best, best_d = None, MATCH_RADIUS_METERS
    for s in stations:
        d = haversine_m(lat, lon, s.lat, s.lon)
        if d <= best_d:
            best, best_d = s, d
    return best


def load_aqi_snapshots(session: Session, records: list[AqiRecord]) -> dict[str, int]:
    """Returns {"stored": rows written, "matched_stations": n, "unmatched_stations": n}.

    Matches against ALL stations, active or not: a station OpenAQ lists as dark
    can still have a current CPCB reading here."""
    stations = list(session.execute(select(Station)).scalars().all())
    cache: dict[tuple[float, float], Station | None] = {}
    matched: set[str] = set()
    unmatched: set[str] = set()
    now = datetime.now(timezone.utc)
    stored = 0

    for rec in records:
        key = (rec.lat, rec.lon)
        if key not in cache:
            cache[key] = nearest_station(stations, rec.lat, rec.lon)
        station = cache[key]
        if station is None:
            unmatched.add(rec.station_name)
            continue
        matched.add(station.station_id)
        # OpenAQ station discovery hardcoded city/state to "Delhi" for every station (Noida,
        # Gurugram and Ghaziabad stations included, 2026-09-22). The CPCB feed carries the
        # real values, so a matched station takes them from here.
        if rec.city and station.city != rec.city:
            station.city = rec.city
        if rec.state and station.state != rec.state:
            station.state = rec.state
        values = {
            "station_id": station.station_id,
            "pollutant_id": rec.pollutant_id,
            "source_updated_at": rec.observed_at,
            "sub_index_min": rec.sub_index_min,
            "sub_index_max": rec.sub_index_max,
            "sub_index_avg": rec.sub_index_avg,
            "fetched_at": now,
        }
        stmt = insert(StationAqiSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["station_id", "pollutant_id", "source_updated_at"],
            set_={k: stmt.excluded[k] for k in ("sub_index_min", "sub_index_max", "sub_index_avg", "fetched_at")},
        )
        session.execute(stmt)
        stored += 1

    session.commit()
    if unmatched:
        logger.info("data.gov.in stations with no station of ours within %.0f m: %s", MATCH_RADIUS_METERS, sorted(unmatched))
    return {"stored": stored, "matched_stations": len(matched), "unmatched_stations": len(unmatched)}
