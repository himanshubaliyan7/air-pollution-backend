from pydantic import BaseModel, Field


class AqiCategoryOut(BaseModel):
    id: str
    label: str


class PollutantInfoOut(BaseModel):
    id: str  # matches `pollutant` in forecast responses
    unit: str  # unit of forecast/history concentrations for this pollutant (display form, e.g. "µg/m³")
    # Concentration at which the region's health-threshold category begins, in `unit`
    # (draw it as the reference line on forecast charts); null if the region defines none.
    health_threshold_concentration: float | None
    # Averaging period the region's standard defines that threshold over (e.g. "24h").
    # Forecast points are hourly values compared against it.
    threshold_averaging: str | None


class RegionBacktestOut(BaseModel):
    """How well the daily verdicts matched what happened, measured on past days."""

    period: str  # the days graded, e.g. "2025-10-15 to 2025-11-30"
    exact_grade_tomorrow: float = Field(ge=0, le=1)  # share of days graded exactly right, tomorrow
    exact_grade_day_5: float = Field(ge=0, le=1)  # same, five days ahead
    # Share of real No-go days the forecast called Go; the range is the low and high ends measured.
    no_go_called_go_low: float
    no_go_called_go_high: float


class RegionOut(BaseModel):
    id: str
    name: str
    country: str  # ISO 3166-1 alpha-2
    timezone: str  # IANA zone; local calendar days and display times use this
    bbox: list[float]  # min_lon, min_lat, max_lon, max_lat
    aqi_standard: str
    pollutants: list[str]
    pollutant_details: list[PollutantInfoOut]  # per pollutant: unit and health-threshold concentration
    aqi_categories: list[AqiCategoryOut]  # ordered best -> worst
    health_threshold_category: str  # category at/above which outdoor practice is not recommended
    # Forecast accuracy measured for this region; null until it has been backtested
    # (show "not yet measured", never another region's numbers).
    backtest: RegionBacktestOut | None = None
