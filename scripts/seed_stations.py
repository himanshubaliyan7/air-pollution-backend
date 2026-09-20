"""Bootstrap the `stations` table from OpenAQ for the Delhi NCR bounding box.

Run once initially, and re-run periodically (not hourly - this is a station
*discovery* call, not a readings pull) to pick up new/retired CPCB stations.
Requires OPENAQ_API_KEY. Also writes a human-reviewable copy to
config/stations_delhi_ncr.yaml so an operator can deactivate a noisy/bad
station without touching code (set is_active: false there and re-run, or
edit the DB row directly).

Usage:
    python -m scripts.seed_stations
"""

import logging
from datetime import datetime, timezone

import yaml
from sqlalchemy.dialects.postgresql import insert

from common.config import REPO_ROOT, get_settings
from common.constants import SensorSourceName
from common.logging_conf import configure_logging
from db.models import Station
from db.session import get_session
from ingestion.config import (
    ACTIVE_SOURCE,
    DELHI_NCR_BBOX,
    DELHI_NCR_COUNTRY_ISO,
    SENSOR_SOURCE_REGISTRY,
    make_station_id,
)

logger = logging.getLogger(__name__)


def main() -> None:
    configure_logging()
    settings = get_settings()

    source_cls = SENSOR_SOURCE_REGISTRY[ACTIVE_SOURCE]
    source = source_cls(api_key=settings.openaq_api_key)

    logger.info("Discovering stations in Delhi NCR bbox=%s country=%s", DELHI_NCR_BBOX, DELHI_NCR_COUNTRY_ISO)
    stations = source.list_stations(bbox=DELHI_NCR_BBOX, country=DELHI_NCR_COUNTRY_ISO)
    logger.info("Discovered %d stations", len(stations))

    now = datetime.now(timezone.utc)
    rows = [
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

    session = get_session()
    try:
        stmt = insert(Station).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["station_id"],
            set_={
                "name": stmt.excluded.name,
                "lat": stmt.excluded.lat,
                "lon": stmt.excluded.lon,
                "city": stmt.excluded.city,
                "state": stmt.excluded.state,
            },
        )
        session.execute(stmt)
        session.commit()
    finally:
        session.close()

    out_path = REPO_ROOT / "config" / "stations_delhi_ncr.yaml"
    out_path.write_text(
        yaml.safe_dump(
            {
                "generated_at": now.isoformat(),
                "source": ACTIVE_SOURCE.value,
                "bbox": list(DELHI_NCR_BBOX),
                "stations": [
                    {
                        "station_id": r["station_id"],
                        "name": r["name"],
                        "lat": r["lat"],
                        "lon": r["lon"],
                        "city": r["city"],
                        "source_location_id": r["source_location_id"],
                        "is_active": True,
                    }
                    for r in rows
                ],
            },
            sort_keys=False,
        )
    )
    logger.info("Wrote %d stations to %s", len(rows), out_path)


if __name__ == "__main__":
    main()
