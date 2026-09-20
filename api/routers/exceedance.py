"""The primary school-facing endpoint: collapses the latest forecast run
into one go/caution/no-go decision per upcoming day.

Each configured horizon (see config/settings.yaml forecast.horizons_hours)
already targets a single future timestamp about a day apart from its
neighbors (24h, 48h, ...), so - unlike models/exceedance.daily_aggregate,
which collapses many REALIZED hourly readings into one actual-vs-forecast
comparison for evaluation - here each horizon's forecast row already stands
in for its calendar day; no further aggregation of forecast points is
needed, only bucketing each row's target_time to its IST calendar day.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.routers.forecasts import _latest_forecast_made_at
from api.schemas.forecasts import ExceedanceDayOut, ExceedanceSummaryOut
from common.config import to_ist
from common.constants import Pollutant
from db.models import Forecast, Station
from models.exceedance import get_aqi_category, load_thresholds

router = APIRouter(tags=["exceedance"])

_CAUTION_PROBABILITY_FLOOR = 0.15  # below the decision threshold but worth flagging as "caution"


@router.get("/forecast/{station_id}/exceedance", response_model=ExceedanceSummaryOut)
def get_exceedance_summary(
    station_id: str,
    pollutant: Pollutant = Query(Pollutant.PM25),
    days_ahead: int = Query(5, ge=1, le=14),
    db: Session = Depends(get_db),
):
    if db.get(Station, station_id) is None:
        raise HTTPException(status_code=404, detail="station not found")

    made_at = _latest_forecast_made_at(db, station_id, pollutant)
    if made_at is None:
        return ExceedanceSummaryOut(station_id=station_id, pollutant=pollutant.value, days=[], overall_recommendation="go")

    rows = db.execute(
        select(Forecast)
        .where(
            Forecast.station_id == station_id,
            Forecast.pollutant == pollutant,
            Forecast.forecast_made_at == made_at,
            Forecast.horizon_hours <= days_ahead * 24,
        )
        .order_by(Forecast.target_time)
    ).scalars().all()

    thresholds = load_thresholds()

    by_day: dict = {}
    for r in rows:
        ist_date = to_ist(r.target_time).date()
        existing = by_day.get(ist_date)
        # If more than one horizon lands on the same IST day, keep the
        # worst-case (highest exceedance probability) one.
        if existing is None or r.exceedance_probability > existing.exceedance_probability:
            by_day[ist_date] = r

    days = [
        ExceedanceDayOut(
            date=d,
            exceedance_flag=r.exceedance_flag,
            exceedance_probability=r.exceedance_probability,
            worst_case_value=r.quantile_high,
            aqi_category=get_aqi_category(thresholds, pollutant, r.quantile_high),
        )
        for d, r in sorted(by_day.items())
    ]

    if any(d.exceedance_flag for d in days):
        recommendation = "no-go"
    elif any(d.exceedance_probability >= _CAUTION_PROBABILITY_FLOOR for d in days):
        recommendation = "caution"
    else:
        recommendation = "go"

    return ExceedanceSummaryOut(
        station_id=station_id, pollutant=pollutant.value, days=days, overall_recommendation=recommendation
    )
