from datetime import date, datetime

from pydantic import BaseModel


class ForecastPointOut(BaseModel):
    # target "hourly": the hour the values are for. target "daily_mean": the start (local
    # midnight) of the calendar day whose mean the values are for.
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
    # What each point forecasts: "hourly" (the value at target_time) or "daily_mean" (the mean
    # of the calendar day, in `timezone`, that starts at target_time).
    target: str = "hourly"
    forecasts: list[ForecastPointOut]


class ExceedanceDayOut(BaseModel):
    """One calendar day. What the values describe depends on the summary's `target`.

    "daily_mean" (graded verdicts, owner decision 2026-10-02): everything is about the day's
    MEAN concentration. aqi_category is the worst category that mean reaches with the service's
    decision probability, and verdict follows from it: go below the region's health-threshold
    category, caution in it, no-go from the next one up.

    "hourly": the day is represented by ONE forecast hour (the run's anchor hour + N x 24 h).
    exceedance_flag is true when that hour is likely above the health threshold, and
    aqi_category is the category of its upper quantile."""

    date: date
    exceedance_flag: bool  # at or above the health threshold with the decision probability
    exceedance_probability: float
    worst_case_value: float  # upper quantile
    aqi_category: str
    # "go" | "caution" | "no-go". Null only from a service version older than this field.
    verdict: str | None = None
    expected_value: float | None = None  # median forecast
    # The AQI sub-index of expected_value for THIS pollutant, on the region's AQI scale. It is
    # not an overall AQI (the worst of several pollutants, most of which are not forecast), so
    # it can be far below the current overall index. Label it with the pollutant. It describes
    # the expected value, while aqi_category may be a worse category the day could reach.
    expected_sub_index: int | None = None


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
    # "daily_mean" or "hourly": see ExceedanceDayOut.
    target: str = "hourly"
    days: list[ExceedanceDayOut]
    # The worst verdict among `days`: "go" | "caution" | "no-go" | "no-data" ("no-data" may still carry partial `days`)
    overall_recommendation: str


class HistoryPointOut(BaseModel):
    time: datetime
    actual: float | None
    # target "hourly": the first forecast made for this hour. target "daily_mean": the
    # forecast of this hour's calendar-day mean that was made last before the day began
    # (the same value for every hour of the day).
    forecast_value: float | None
    # The category `actual` falls in by the region's thresholds, so a client can colour an hour
    # without knowing any breakpoint. Null without a measurement or outside every region.
    aqi_category: str | None = None


class HistoryOut(BaseModel):
    station_id: str
    pollutant: str
    # IANA zone of the station's region; format `time` in it.
    timezone: str | None = None
    target: str = "hourly"  # see HistoryPointOut.forecast_value
    points: list[HistoryPointOut]
