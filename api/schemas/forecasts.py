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
    # IANA zone of the station's region; format target_time / forecast_made_at in it.
    timezone: str | None = None
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
    # IANA zone in which each day's `date` is a calendar day (the station's region's zone).
    timezone: str | None = None
    # When the forecast run behind `days` was generated (null if none ever existed), and whether
    # it is fresh enough to act on. When is_current is false, `days` is empty and the
    # recommendation is no-data.
    forecast_made_at: datetime | None = None
    is_current: bool = False
    days: list[ExceedanceDayOut]
    overall_recommendation: str  # "go" | "caution" | "no-go" | "no-data" ("no-data" may still carry partial `days`)


class HistoryPointOut(BaseModel):
    time: datetime
    actual: float | None
    forecast_value: float | None


class HistoryOut(BaseModel):
    station_id: str
    pollutant: str
    # IANA zone of the station's region; format `time` in it.
    timezone: str | None = None
    points: list[HistoryPointOut]
