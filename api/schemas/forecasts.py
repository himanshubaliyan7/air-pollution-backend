from datetime import date, datetime

from pydantic import BaseModel


class ForecastPointOut(BaseModel):
    target_time: datetime
    horizon_hours: int
    point_forecast: float
    quantile_low: float
    quantile_high: float
    exceedance_probability: float
    exceedance_flag: bool


class ForecastSeriesOut(BaseModel):
    station_id: str
    pollutant: str
    forecast_made_at: datetime | None
    # False when a forecast exists but is too old to act on; `forecasts` is then empty.
    is_current: bool = False
    forecasts: list[ForecastPointOut]


class ExceedanceDayOut(BaseModel):
    date: date
    exceedance_flag: bool
    exceedance_probability: float
    worst_case_value: float
    aqi_category: str


class ExceedanceSummaryOut(BaseModel):
    station_id: str
    pollutant: str
    days: list[ExceedanceDayOut]
    overall_recommendation: str  # "go" | "caution" | "no-go" | "no-data" ("no-data" may still carry partial `days`)


class HistoryPointOut(BaseModel):
    time: datetime
    actual: float | None
    forecast_value: float | None


class HistoryOut(BaseModel):
    station_id: str
    pollutant: str
    points: list[HistoryPointOut]
