"""Matches CPCB AQI records (direct CAAQMS feed or data.gov.in) to our stations and
stores each hourly snapshot (idempotently), so history accumulates.

Matching is by coordinates first, then by name (match_station): the station list
comes from OpenAQ, whose entries for a CPCB site can be duplicated or misplaced."""

import logging
import math
import re
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from common.constants import SensorSourceName
from db.models import RawSensorReading, Station, StationAqiSnapshot
from ingestion.sources.data_gov_in import AqiRecord

logger = logging.getLogger(__name__)

# Live check 2026-09-21: 42 of 44 Delhi feed stations were within 100 m of a
# station we already track; the rest were 2+ km away (a genuinely different
# site). 250 m leaves headroom for coordinate rounding without ever merging two
# distinct sites.
MATCH_RADIUS_METERS = 250.0

# OpenAQ places some CPCB sites kilometres from CPCB's own coordinates (live check
# 2026-10-01: Sector-1 Noida 1.2 km, Aya Nagar 2.2 km, Pusa DPCC and Pusa IMD on
# one shared point 2.7 km off, North Campus 6.5 km). Those match nothing by
# distance, so the same site name and operator within this radius is accepted
# instead. MD University, Rohtak stays unmatched: the two lists disagree by 46 km.
NAME_MATCH_RADIUS_METERS = 10000.0

# CPCB now lists the IMD-run Delhi stations under IITM; OpenAQ kept "IMD".
OPERATOR_ALIASES = {"iitm": "imd"}


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def site_and_operator(name: str) -> tuple[str, str] | None:
    """("pusa", "dpcc") from "Pusa, Delhi - DPCC". The city in the middle is
    ignored: the same site is "Delhi" in one list and "New Delhi" in the other.
    None for a name without both parts (e.g. a private sensor)."""
    head, sep, operator = name.rpartition(" - ")
    site = re.sub(r"[^a-z0-9]+", " ", head.split(",")[0].lower()).strip()
    operator = operator.strip().lower()
    if not sep or not site or not operator:
        return None
    return site, OPERATOR_ALIASES.get(operator, operator)


def match_station(
    stations: list[Station], name: str, lat: float, lon: float, newest_openaq: dict[str, datetime] | None = None
) -> Station | None:
    """The station of ours a CPCB feed station belongs to, or None.

    1. Stations within MATCH_RADIUS_METERS. OpenAQ can list one site twice (a dead
       entry next to the live one: "Anand Vihar, Delhi" and "Anand Vihar, New
       Delhi", 90 m apart), so among several the one with the newest OpenAQ
       reading wins: that is where the history and the models are. Before
       2026-10-01 the nearest won, which sent CPCB data to the dead entry.
    2. Otherwise the same site name and operator within NAME_MATCH_RADIUS_METERS.

    `newest_openaq` is station_id -> newest OpenAQ reading; ties fall back to distance."""
    newest_openaq = newest_openaq or {}
    oldest = datetime.min.replace(tzinfo=timezone.utc)

    def best(candidates: list[tuple[float, Station]]) -> Station | None:
        if not candidates:
            return None
        return max(candidates, key=lambda c: (newest_openaq.get(c[1].station_id) or oldest, -c[0]))[1]

    distances = [(haversine_m(lat, lon, s.lat, s.lon), s) for s in stations]
    near = [c for c in distances if c[0] <= MATCH_RADIUS_METERS]
    if near:
        return best(near)
    key = site_and_operator(name)
    if key is None:
        return None
    return best([c for c in distances if c[0] <= NAME_MATCH_RADIUS_METERS and site_and_operator(c[1].name) == key])


def newest_openaq_readings(session: Session) -> dict[str, datetime]:
    """station_id -> its newest OpenAQ reading (match_station's tie-break)."""
    return dict(
        session.execute(
            select(RawSensorReading.station_id, func.max(RawSensorReading.observed_at))
            .where(RawSensorReading.source == SensorSourceName.OPENAQ)
            .group_by(RawSensorReading.station_id)
        ).all()
    )


def load_aqi_snapshots(session: Session, records: list[AqiRecord], source: str | None = None) -> dict[str, int]:
    """Returns {"stored": rows written, "matched_stations": n, "unmatched_stations": n}.

    Matches against ALL stations, active or not: a station OpenAQ lists as dark
    can still have a current CPCB reading here."""
    stations = list(session.execute(select(Station)).scalars().all())
    newest_openaq = newest_openaq_readings(session)
    cache: dict[tuple[str, float, float], Station | None] = {}
    matched: set[str] = set()
    unmatched: set[str] = set()
    now = datetime.now(timezone.utc)
    stored = 0

    for rec in records:
        key = (rec.station_name, rec.lat, rec.lon)
        if key not in cache:
            cache[key] = match_station(stations, rec.station_name, rec.lat, rec.lon, newest_openaq)
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
            "sub_index_hourly": rec.sub_index_hourly,
            "fetched_at": now,
            "source": source,
        }
        stmt = insert(StationAqiSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["station_id", "pollutant_id", "source_updated_at"],
            set_={k: stmt.excluded[k] for k in ("sub_index_min", "sub_index_max", "sub_index_avg", "sub_index_hourly", "fetched_at", "source")},
        )
        session.execute(stmt)
        stored += 1

    session.commit()
    if unmatched:
        logger.info("CPCB stations matching no station of ours: %s", sorted(unmatched))
    return {"stored": stored, "matched_stations": len(matched), "unmatched_stations": len(unmatched)}
