from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas.current_aqi import CurrentAqiOut, OverallAqiOut, PollutantAqiOut
from common.aqi import ATTRIBUTION, at_or_above_health_threshold, category_for_sub_index, overall_aqi
from common.freshness import is_reading_current
from common.regions import Region, region_for_point
from db.models import Station, StationAqiSnapshot

router = APIRouter(prefix="/stations", tags=["current-aqi"])

# Pollutants of one station are published together, but tolerate one lagging a little.
SAME_SNAPSHOT_WINDOW = timedelta(hours=3)


@router.get("/{station_id}/current-aqi", response_model=CurrentAqiOut)
def get_current_aqi(station_id: str, db: Session = Depends(get_db)):
    """Official CPCB AQI right now (no forecast involved), when CPCB has
    a recent reading for this station."""
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")

    region = region_for_point(station.lat, station.lon)
    base = dict(
        station_id=station_id,
        aqi_standard=region.aqi_standard if region else None,
        timezone=region.timezone if region else None,
        attribution=ATTRIBUTION,
    )

    now = datetime.now(timezone.utc)
    newest = db.execute(
        select(func.max(StationAqiSnapshot.source_updated_at)).where(StationAqiSnapshot.station_id == station_id)
    ).scalar_one_or_none()
    rows = []
    if is_reading_current(newest, now):
        rows = db.execute(
            select(StationAqiSnapshot).where(
                StationAqiSnapshot.station_id == station_id,
                StationAqiSnapshot.source_updated_at >= newest - SAME_SNAPSHOT_WINDOW,
            )
        ).scalars().all()
    return CurrentAqiOut(**current_conditions(region, newest, rows, now), **base)


def current_conditions(region: Region | None, newest: datetime | None, rows: list[StationAqiSnapshot], now: datetime) -> dict:
    """The current-conditions fields of one station, from its newest reading time
    (None if it never had one) and its snapshot rows within SAME_SNAPSHOT_WINDOW
    of it. Shared with the overview endpoint, so the two cannot disagree."""
    if not is_reading_current(newest, now):
        # Same rule as forecasts: an old reading is reported as "no current data", never served as current.
        return dict(as_of=newest, is_current=False, overall=None, at_or_above_health_threshold=None, pollutants=[])

    # Ascending order: the last write per pollutant wins.
    latest_by_pollutant = {r.pollutant_id: r for r in sorted(rows, key=lambda r: r.source_updated_at)}

    thresholds = region.thresholds() if region else None
    pollutants = [
        PollutantAqiOut(
            pollutant_id=pid,
            sub_index_avg=r.sub_index_avg,
            sub_index_min=r.sub_index_min,
            sub_index_max=r.sub_index_max,
            category=category_for_sub_index(thresholds, r.sub_index_avg) if thresholds and r.sub_index_avg is not None else None,
        )
        for pid, r in sorted(latest_by_pollutant.items())
    ]

    overall = None
    over_threshold = None
    if thresholds:
        result = overall_aqi({pid: r.sub_index_avg for pid, r in latest_by_pollutant.items()})
        if result is not None:
            aqi, driver = result
            overall = OverallAqiOut(aqi=aqi, category=category_for_sub_index(thresholds, aqi), driver=driver)
            over_threshold = at_or_above_health_threshold(thresholds, overall.category)

    return dict(
        as_of=newest, is_current=True, overall=overall, at_or_above_health_threshold=over_threshold, pollutants=pollutants
    )
