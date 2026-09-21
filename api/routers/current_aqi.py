from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.routers.forecasts import is_forecast_current
from api.schemas.current_aqi import CurrentAqiOut, OverallAqiOut, PollutantAqiOut
from common.aqi import category_for_sub_index, overall_aqi
from common.regions import region_for_point
from db.models import Station, StationAqiSnapshot
from ingestion.sources.data_gov_in import ATTRIBUTION

router = APIRouter(prefix="/stations", tags=["current-aqi"])

# Pollutants of one station are published together, but tolerate one lagging a little.
_SAME_SNAPSHOT_WINDOW = timedelta(hours=3)


@router.get("/{station_id}/current-aqi", response_model=CurrentAqiOut)
def get_current_aqi(station_id: str, db: Session = Depends(get_db)):
    """Official CPCB AQI right now (no forecast involved), when data.gov.in has
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

    newest = db.execute(
        select(func.max(StationAqiSnapshot.source_updated_at)).where(StationAqiSnapshot.station_id == station_id)
    ).scalar_one_or_none()
    if newest is None:
        return CurrentAqiOut(as_of=None, is_current=False, overall=None, pollutants=[], **base)
    if not is_forecast_current(newest):
        # Same rule as forecasts: an old reading is reported as "no current data", never served as current.
        return CurrentAqiOut(as_of=newest, is_current=False, overall=None, pollutants=[], **base)

    rows = db.execute(
        select(StationAqiSnapshot)
        .where(
            StationAqiSnapshot.station_id == station_id,
            StationAqiSnapshot.source_updated_at >= newest - _SAME_SNAPSHOT_WINDOW,
        )
        .order_by(StationAqiSnapshot.source_updated_at)
    ).scalars().all()
    latest_by_pollutant = {r.pollutant_id: r for r in rows}  # ascending order: the last write per pollutant wins

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
    if thresholds:
        result = overall_aqi({pid: r.sub_index_avg for pid, r in latest_by_pollutant.items()})
        if result is not None:
            aqi, driver = result
            overall = OverallAqiOut(aqi=aqi, category=category_for_sub_index(thresholds, aqi), driver=driver)

    return CurrentAqiOut(as_of=newest, is_current=True, overall=overall, pollutants=pollutants, **base)
