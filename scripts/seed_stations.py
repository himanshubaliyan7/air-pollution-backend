"""Bootstrap the `stations` table from OpenAQ for one region's bounding box
(config/regions.yaml; default delhi-ncr).

Run once initially, and re-run periodically (not hourly - this is a station
*discovery* call, not a readings pull) to pick up new/retired CPCB stations.
Requires OPENAQ_API_KEY. Also writes a human-reviewable copy to
config/stations_<region>.yaml (stations_delhi_ncr.yaml for delhi-ncr) so an
operator can deactivate a noisy/bad station without touching code (set
is_active: false there and re-run, or edit the DB row directly). Each region
has its own file, so seeding one never overwrites another's.

A station that already exists keeps its city, state and coordinates (only the name is refreshed): the
CPCB feed corrects city/state (aqi_snapshot_loader) and a re-run must not put
OpenAQ's values back.

Usage:
    python -m scripts.seed_stations
    python -m scripts.seed_stations --region mumbai --dry-run   # list, write nothing
    python -m scripts.seed_stations --region mumbai
"""

import argparse
import logging
from datetime import datetime, timezone

import yaml
from sqlalchemy.dialects.postgresql import insert

from common.config import REPO_ROOT, get_settings
from common.logging_conf import configure_logging
from db.models import Station
from db.session import get_session
from ingestion.config import ACTIVE_SOURCE, SENSOR_SOURCE_REGISTRY, make_station_id, region_search_area

logger = logging.getLogger(__name__)

DEFAULT_REGION = "delhi-ncr"
# (city when OpenAQ gives no locality, state): what discovery stored for every Delhi NCR
# station before regions existed. Another region gets "" and the CPCB feed fills both in.
_LEGACY_DEFAULTS = {"delhi-ncr": ("Delhi", "Delhi")}


def review_path(region_id: str):
    return REPO_ROOT / "config" / f"stations_{region_id.replace('-', '_')}.yaml"


def station_rows(source, region_id: str, now: datetime) -> list[dict]:
    bbox, country = region_search_area(region_id)
    default_city, state = _LEGACY_DEFAULTS.get(region_id, ("", ""))
    logger.info("Discovering stations for %s bbox=%s country=%s", region_id, bbox, country)
    stations = source.list_stations(bbox=bbox, country=country, default_city=default_city, state=state)
    return [
        {
            "station_id": make_station_id(ACTIVE_SOURCE, s.source_location_id),
            "name": s.name,
            "lat": s.lat,
            "lon": s.lon,
            "city": s.city,
            "state": s.state,
            "source": ACTIVE_SOURCE,
            "source_location_id": s.source_location_id,
            "is_active": True,
            "created_at": now,
        }
        for s in stations
    ]


def upsert_stations(session, rows: list[dict]) -> None:
    stmt = insert(Station).values(rows)
    # Only the name is refreshed on conflict. lat/lon: OpenAQ misplaces some
    # CPCB sites by kilometres and scripts/fix_station_coordinates.py corrects them from
    # CPCB's own feed. city/state: the feed's values are the right ones.
    stmt = stmt.on_conflict_do_update(index_elements=["station_id"], set_={"name": stmt.excluded.name})
    session.execute(stmt)
    session.commit()


def write_review_file(region_id: str, rows: list[dict], now: datetime) -> None:
    path = review_path(region_id)
    path.write_text(
        yaml.safe_dump(
            {
                "generated_at": now.isoformat(),
                "region": region_id,
                "source": ACTIVE_SOURCE.value,
                "bbox": list(region_search_area(region_id)[0]),
                "stations": [
                    {k: r[k] for k in ("station_id", "name", "lat", "lon", "city", "source_location_id")} | {"is_active": True}
                    for r in rows
                ],
            },
            sort_keys=False,
        )
    )
    logger.info("Wrote %d stations to %s", len(rows), path)


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default=DEFAULT_REGION, help="region id from config/regions.yaml")
    parser.add_argument("--dry-run", action="store_true", help="list what would be created, write nothing")
    args = parser.parse_args()

    source = SENSOR_SOURCE_REGISTRY[ACTIVE_SOURCE](api_key=get_settings().openaq_api_key)
    now = datetime.now(timezone.utc)
    rows = station_rows(source, args.region, now)
    logger.info("Discovered %d stations", len(rows))

    if args.dry_run:
        for r in rows:
            print(f"{r['station_id']}\t{r['name']}\t{r['lat']:.4f},{r['lon']:.4f}\t{r['city'] or '-'}")
        print(f"DRY RUN - {len(rows)} stations, nothing written")
        return

    session = get_session()
    try:
        upsert_stations(session, rows)
    finally:
        session.close()
    write_review_file(args.region, rows, now)


if __name__ == "__main__":
    main()
