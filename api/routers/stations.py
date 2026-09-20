from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas.stations import StationDetailOut, StationOut
from db.models import RawSensorReading, Station

router = APIRouter(prefix="/stations", tags=["stations"])


@router.get("", response_model=list[StationOut])
def list_stations(db: Session = Depends(get_db)):
    stations = db.execute(select(Station).where(Station.is_active.is_(True))).scalars().all()
    return stations


@router.get("/{station_id}", response_model=StationDetailOut)
def get_station(station_id: str, db: Session = Depends(get_db)):
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")

    latest = db.execute(
        select(func.max(RawSensorReading.observed_at)).where(RawSensorReading.station_id == station_id)
    ).scalar_one_or_none()

    return StationDetailOut(
        station_id=station.station_id,
        name=station.name,
        lat=station.lat,
        lon=station.lon,
        city=station.city,
        is_active=station.is_active,
        latest_observed_at=latest,
    )
