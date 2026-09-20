"""The primary school-facing endpoint: collapses the latest forecast run
into one go/caution/no-go decision per upcoming day.

Each configured horizon (see config/settings.yaml forecast.horizons_hours)
already targets a single future timestamp about a day apart from its
neighbors (24h, 48h, ...), so - unlike models/exceedance.daily_aggregate,
which collapses many REALIZED hourly readings into one actual-vs-forecast
comparison for evaluation - here each horizon's forecast row already stands
in for its calendar day; no further aggregation of forecast points is
needed, only bucketing each row's target_time to its station-local calendar day.
"""

from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_db
from api.routers.forecasts import _latest_forecast_made_at, is_forecast_current
from api.schemas.forecasts import ExceedanceDayOut, ExceedanceSummaryOut
from common.regions import region_for_point
from common.constants import DEFAULT_HORIZONS_HOURS, Pollutant
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
    station = db.get(Station, station_id)
    if station is None:
        raise HTTPException(status_code=404, detail="station not found")
    region = region_for_point(station.lat, station.lon)
    tz_name = region.timezone if region else "UTC"

    made_at = _latest_forecast_made_at(db, station_id, pollutant)
    # No forecast, or one anchored on input older than generate_forecasts is
    # willing to use (the upstream feed lags; most stations are days behind),
    # must never read as "go" - a school would treat silence as clearance.
    if not is_forecast_current(made_at):
        return ExceedanceSummaryOut(
            station_id=station_id, pollutant=pollutant.value, timezone=tz_name, days=[], overall_recommendation="no-data"
        )

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
        local_date = r.target_time.astimezone(ZoneInfo(tz_name)).date()
        existing = by_day.get(local_date)
        # If more than one horizon lands on the same local day, keep the
        # worst-case (highest exceedance probability) one.
        if existing is None or r.exceedance_probability > existing.exceedance_probability:
            by_day[local_date] = r

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

    # predict.forecast can skip individual horizons (missing model, gap in lag
    # history), so the newest run may cover only some days. Known bad days still
    # mean no-go, but an incomplete run with nothing flagged must not read as go.
    expected = {h for h in DEFAULT_HORIZONS_HOURS if h <= days_ahead * 24}
    incomplete = not expected <= {r.horizon_hours for r in rows}

    if any(d.exceedance_flag for d in days):
        recommendation = "no-go"
    elif incomplete:
        recommendation = "no-data"
    elif any(d.exceedance_probability >= _CAUTION_PROBABILITY_FLOOR for d in days):
        recommendation = "caution"
    else:
        recommendation = "go"

    return ExceedanceSummaryOut(
        station_id=station_id, pollutant=pollutant.value, timezone=tz_name, days=days, overall_recommendation=recommendation
    )
