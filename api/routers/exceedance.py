"""The primary school-facing endpoint: collapses the latest forecast run into
one go/caution/no-go decision per upcoming day. The decision itself lives in
models.outlook, shared with the daily alert digest.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas.forecasts import ExceedanceDayOut, ExceedanceSummaryOut
from common.constants import Pollutant
from db.models import Station
from models.outlook import build_outlook

router = APIRouter(tags=["exceedance"])


@router.get("/forecast/{station_id}/exceedance", response_model=ExceedanceSummaryOut)
def get_exceedance_summary(
    station_id: str,
    pollutant: Pollutant = Query(Pollutant.PM25),
    days_ahead: int = Query(5, ge=1, le=14),
    db: Session = Depends(get_db),
):
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")
    outlook = build_outlook(db, station, pollutant, days_ahead)
    return ExceedanceSummaryOut(
        station_id=outlook.station_id,
        pollutant=outlook.pollutant,
        timezone=outlook.timezone,
        forecast_made_at=outlook.forecast_made_at,
        is_current=outlook.is_current,
        days=[
            ExceedanceDayOut(
                date=d.date,
                exceedance_flag=d.exceedance_flag,
                exceedance_probability=d.exceedance_probability,
                worst_case_value=d.worst_case_value,
                aqi_category=d.aqi_category,
            )
            for d in outlook.days
        ],
        overall_recommendation=outlook.overall_recommendation,
    )
