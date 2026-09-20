from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.routers.forecasts import is_forecast_current
from api.schemas.stations import StationDetailOut, StationOut
from common.regions import region_for_point
from db.models import Forecast, RawSensorReading, Station

router = APIRouter(prefix="/stations", tags=["stations"])


def _region_id(station: Station) -> str | None:
    region = region_for_point(station.lat, station.lon)
    return region.id if region else None


@router.get("", response_model=list[StationOut])
def list_stations(region_id: str | None = Query(None), db: Session = Depends(get_db)):
    """Stations with a current forecast come first: the upstream sensor feed
    lags for most of the network, so most stations cannot be forecast right now
    and clients need to tell them apart."""
    stations = db.execute(select(Station).where(Station.is_active.is_(True))).scalars().all()
    # Time-bounded so the hypertable scans only recent chunks: stations dark
    # for 30+ days are deactivated anyway, and an older forecast is not current.
    now = datetime.now(timezone.utc)
    latest_reading = dict(
        db.execute(
            select(RawSensorReading.station_id, func.max(RawSensorReading.observed_at))
            .where(RawSensorReading.observed_at >= now - timedelta(days=60))
            .group_by(RawSensorReading.station_id)
        ).all()
    )
    latest_forecast = dict(
        db.execute(
            select(Forecast.station_id, func.max(Forecast.forecast_made_at))
            .where(Forecast.forecast_made_at >= now - timedelta(days=1))
            .group_by(Forecast.station_id)
        ).all()
    )
    out = [
        StationOut(
            station_id=s.station_id,
            name=s.name,
            lat=s.lat,
            lon=s.lon,
            city=s.city,
            is_active=s.is_active,
            region_id=_region_id(s),
            latest_observed_at=latest_reading.get(s.station_id),
            has_current_forecast=is_forecast_current(latest_forecast.get(s.station_id)),
        )
        for s in stations
    ]
    if region_id is not None:
        out = [s for s in out if s.region_id == region_id]
    out.sort(key=lambda s: (not s.has_current_forecast, s.name))
    return out


@router.get("/{station_id}", response_model=StationDetailOut)
def get_station(station_id: str, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")

    latest = db.execute(
        select(func.max(RawSensorReading.observed_at)).where(RawSensorReading.station_id == station_id)
    ).scalar_one_or_none()
    made_at = db.execute(
        select(func.max(Forecast.forecast_made_at)).where(Forecast.station_id == station_id)
    ).scalar_one_or_none()

    return StationDetailOut(
        station_id=station.station_id,
        name=station.name,
        lat=station.lat,
        lon=station.lon,
        city=station.city,
        is_active=station.is_active,
        region_id=_region_id(station),
        latest_observed_at=latest,
        has_current_forecast=is_forecast_current(made_at),
    )
