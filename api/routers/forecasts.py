from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db import get_db
from api.schemas.forecasts import ForecastPointOut, ForecastSeriesOut, HistoryOut, HistoryPointOut
from api.schemas.model_health import ModelHealthOut
from common.constants import MAX_INPUT_STALENESS_HOURS, Pollutant
from db.models import ExceedanceEvaluation, Forecast, RawSensorReading, Station

router = APIRouter(tags=["forecasts"])


def _latest_forecast_made_at(db: Session, station_id: str, pollutant: Pollutant) -> datetime | None:
    return db.execute(
        select(func.max(Forecast.forecast_made_at)).where(
            Forecast.station_id == station_id, Forecast.pollutant == pollutant
        )
    ).scalar_one_or_none()


def is_forecast_current(made_at: datetime | None) -> bool:
    """A forecast is only actionable while it is as fresh as generate_forecasts
    would accept its input (MAX_INPUT_STALENESS_HOURS); older, it must be
    treated as no forecast at all, never as an implicit "go"."""
    return made_at is not None and datetime.now(timezone.utc) - made_at <= timedelta(hours=MAX_INPUT_STALENESS_HOURS)


@router.get("/forecast/{station_id}", response_model=ForecastSeriesOut)
def get_forecast(
    station_id: str,
    pollutant: Pollutant = Query(Pollutant.PM25),
    horizon_days: int | None = Query(None, ge=1, le=30),
    db: Session = Depends(get_db),
):
    if db.get(Station, station_id) is None:
        raise HTTPException(status_code=404, detail="station not found")

    made_at = _latest_forecast_made_at(db, station_id, pollutant)
    if made_at is None:
        return ForecastSeriesOut(station_id=station_id, pollutant=pollutant.value, forecast_made_at=None, forecasts=[])

    stmt = select(Forecast).where(
        Forecast.station_id == station_id, Forecast.pollutant == pollutant, Forecast.forecast_made_at == made_at
    )
    if horizon_days is not None:
        stmt = stmt.where(Forecast.horizon_hours <= horizon_days * 24)
    stmt = stmt.order_by(Forecast.target_time)

    rows = db.execute(stmt).scalars().all()
    return ForecastSeriesOut(
        station_id=station_id,
        pollutant=pollutant.value,
        forecast_made_at=made_at,
        forecasts=[
            ForecastPointOut(
                target_time=r.target_time,
                horizon_hours=r.horizon_hours,
                point_forecast=r.point_forecast,
                quantile_low=r.quantile_low,
                quantile_high=r.quantile_high,
                exceedance_probability=r.exceedance_probability,
                exceedance_flag=r.exceedance_flag,
            )
            for r in rows
        ],
    )


@router.get("/forecast/{station_id}/history", response_model=HistoryOut)
def get_forecast_history(
    station_id: str,
    pollutant: Pollutant = Query(Pollutant.PM25),
    lookback_days: int = Query(14, ge=1, le=90),
    db: Session = Depends(get_db),
):
    if db.get(Station, station_id) is None:
        raise HTTPException(status_code=404, detail="station not found")

    window_start = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    actual_rows = db.execute(
        select(RawSensorReading.observed_at, RawSensorReading.value).where(
            RawSensorReading.station_id == station_id,
            RawSensorReading.pollutant == pollutant,
            RawSensorReading.observed_at >= window_start,
        )
    ).all()
    actual_by_time = {r.observed_at: r.value for r in actual_rows}

    # For each past target_time, use the forecast that was made closest to
    # (but not after) that target_time minus its own horizon - i.e. the
    # forecast run that actually predicted it, not a later revision.
    forecast_rows = db.execute(
        select(Forecast.target_time, Forecast.point_forecast, Forecast.forecast_made_at)
        .where(
            Forecast.station_id == station_id,
            Forecast.pollutant == pollutant,
            Forecast.target_time >= window_start,
            Forecast.target_time <= datetime.now(timezone.utc),
        )
        .order_by(Forecast.target_time, Forecast.forecast_made_at)
    ).all()
    forecast_by_time: dict[datetime, float] = {}
    for r in forecast_rows:
        forecast_by_time.setdefault(r.target_time, r.point_forecast)  # earliest made_at wins

    all_times = sorted(set(actual_by_time) | set(forecast_by_time))
    points = [
        HistoryPointOut(time=t, actual=actual_by_time.get(t), forecast_value=forecast_by_time.get(t))
        for t in all_times
    ]
    return HistoryOut(station_id=station_id, pollutant=pollutant.value, points=points)


@router.get("/model-health", response_model=list[ModelHealthOut])
def get_model_health(
    station_id: str | None = Query(None),
    pollutant: Pollutant | None = Query(None),
    db: Session = Depends(get_db),
):
    stmt = select(ExceedanceEvaluation).order_by(ExceedanceEvaluation.evaluation_window_end.desc()).limit(200)
    if station_id is not None:
        stmt = stmt.where(ExceedanceEvaluation.station_id == station_id)
    if pollutant is not None:
        stmt = stmt.where(ExceedanceEvaluation.pollutant == pollutant)

    rows = db.execute(stmt).scalars().all()
    return [
        ModelHealthOut(
            station_id=r.station_id,
            pollutant=r.pollutant.value,
            horizon_hours=r.horizon_hours,
            precision=r.precision,
            recall=r.recall,
            f1=r.f1,
            mae=r.mae,
            rmse=r.rmse,
            evaluation_window_end=r.evaluation_window_end,
        )
        for r in rows
    ]
