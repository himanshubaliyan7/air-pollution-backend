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
    # Hourly rule (owner decision 2026-09-25: hourly values, not CPCB's 24h average). Each day
    # is represented by ONE forecast hour - the run's anchor hour + N x 24 h - so this is true
    # when that hour is likely above the health threshold; worst_case_value/aqi_category are its
    # upper quantile. Other hours of the day are not forecast (see docs/PROJECT_HANDOFF.md).
    exceedance_flag: bool
    exceedance_probability: float
    worst_case_value: float
    aqi_category: str


class ExceedanceSummaryOut(BaseModel):
    station_id: str
    pollutant: str
    # IANA zone in which each day's `date` is a calendar day (the station's region's zone).
    timezone: str | None = None
    # The newest observed sensor hour the forecast was made from - i.e. the age of the data behind
    # `days` (up to MAX_INPUT_STALENESS_HOURS old; CPCB stations typically lag ~12 h). Null if none
    # ever existed. is_current says whether it is fresh enough to act on. When is_current is false, `days` is empty and the
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
