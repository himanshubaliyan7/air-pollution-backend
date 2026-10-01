"""Every active station's current AQI and outlook in one response, for map views
(one request instead of about two per station and pollutant). It adds no
logic: the per-station endpoints' own functions produce each part."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.routers.current_aqi import SAME_SNAPSHOT_WINDOW, current_conditions
from api.routers.exceedance import summary_out
from api.schemas.overview import OverviewCurrentAqiOut, OverviewOut, OverviewStationOut
from common.aqi import ATTRIBUTION
from common.constants import MAX_CURRENT_READING_AGE_HOURS, Pollutant
from common.regions import region_for_point
from db.models import Station, StationAqiSnapshot
from models.outlook import build_outlooks

router = APIRouter(tags=["overview"])

# The source data changes once an hour; a short shared cache absorbs a burst of map loads.
CACHE_CONTROL = "public, max-age=120"


@router.get("/overview", response_model=OverviewOut)
def get_overview(
    response: Response,
    region_id: str | None = Query(None),
    days_ahead: int = Query(5, ge=1, le=14),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    stations = db.execute(select(Station).where(Station.is_active.is_(True)).order_by(Station.name)).scalars().all()
    regions = {s.station_id: region_for_point(s.lat, s.lon) for s in stations}
    if region_id is not None:
        stations = [s for s in stations if regions[s.station_id] is not None and regions[s.station_id].id == region_id]

    newest = dict(
        db.execute(
            select(StationAqiSnapshot.station_id, func.max(StationAqiSnapshot.source_updated_at))
            .where(StationAqiSnapshot.source_updated_at >= now - timedelta(days=1))
            .group_by(StationAqiSnapshot.station_id)
        ).all()
    )
    snapshots: dict[str, list[StationAqiSnapshot]] = {}
    # Wide enough for every current reading's SAME_SNAPSHOT_WINDOW; each station's rows are cut to its own below.
    oldest_needed = now - timedelta(hours=MAX_CURRENT_READING_AGE_HOURS + 1) - SAME_SNAPSHOT_WINDOW
    for row in db.execute(
        select(StationAqiSnapshot).where(StationAqiSnapshot.source_updated_at >= oldest_needed)
    ).scalars():
        station_newest = newest.get(row.station_id)
        if station_newest is not None and row.source_updated_at >= station_newest - SAME_SNAPSHOT_WINDOW:
            snapshots.setdefault(row.station_id, []).append(row)

    outlooks = build_outlooks(db, stations, days_ahead, now)

    response.headers["Cache-Control"] = CACHE_CONTROL
    return OverviewOut(
        generated_at=now,
        attribution=ATTRIBUTION,
        stations=[
            OverviewStationOut(
                station_id=s.station_id,
                name=s.name,
                lat=s.lat,
                lon=s.lon,
                city=s.city,
                region_id=regions[s.station_id].id if regions[s.station_id] else None,
                current_aqi=OverviewCurrentAqiOut(
                    **current_conditions(regions[s.station_id], newest.get(s.station_id), snapshots.get(s.station_id, []), now)
                ),
                outlooks=[summary_out(outlooks[(s.station_id, pollutant)]) for pollutant in Pollutant],
            )
            for s in stations
        ],
    )
