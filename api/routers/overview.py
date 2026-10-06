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
from api.schemas.overview import (
    OverviewCurrentAqiOut,
    OverviewHistoryOut,
    OverviewHistoryStationOut,
    OverviewOut,
    OverviewStationOut,
)
from common.aqi import ATTRIBUTION
from common.constants import MAX_CURRENT_READING_AGE_HOURS, Pollutant
from common.regions import region_for_point
from db.models import Station, StationAqiSnapshot
from db.readings import hourly_readings_by_station
from models.exceedance import get_aqi_category
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


@router.get("/overview/history", response_model=OverviewHistoryOut)
def get_overview_history(
    response: Response,
    region_id: str | None = Query(None),
    pollutant: Pollutant = Query(Pollutant.PM25),
    hours: int = Query(48, ge=1, le=168),
    db: Session = Depends(get_db),
):
    """The last `hours` measured hours of one pollutant at every active station,
    each with its category: one request for a view of all stations side by side."""
    now = datetime.now(timezone.utc)
    newest_hour = now.replace(minute=0, second=0, microsecond=0)
    hour_starts = [newest_hour - timedelta(hours=back) for back in range(hours - 1, -1, -1)]

    stations = db.execute(select(Station).where(Station.is_active.is_(True)).order_by(Station.name)).scalars().all()
    regions = {s.station_id: region_for_point(s.lat, s.lon) for s in stations}
    if region_id is not None:
        stations = [s for s in stations if regions[s.station_id] is not None and regions[s.station_id].id == region_id]

    readings = hourly_readings_by_station(db, pollutant, hour_starts[0])

    out = []
    for s in stations:
        by_hour = dict(readings.get(s.station_id, []))
        thresholds = regions[s.station_id].thresholds() if regions[s.station_id] else None
        values = [by_hour.get(hour) for hour in hour_starts]
        out.append(
            OverviewHistoryStationOut(
                station_id=s.station_id,
                values=values,
                categories=[
                    get_aqi_category(thresholds, pollutant, v) if thresholds and v is not None else None for v in values
                ],
            )
        )

    response.headers["Cache-Control"] = CACHE_CONTROL
    return OverviewHistoryOut(generated_at=now, pollutant=pollutant.value, hours=hour_starts, stations=out)
