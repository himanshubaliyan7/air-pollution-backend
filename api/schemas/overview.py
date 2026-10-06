from datetime import datetime

from pydantic import BaseModel

from api.schemas.current_aqi import OverallAqiOut, PollutantAqiOut
from api.schemas.forecasts import ExceedanceSummaryOut


class OverviewCurrentAqiOut(BaseModel):
    """GET /stations/{id}/current-aqi without the per-station constants (see OverviewOut)."""

    # When the newest reading was published (UTC); null if there was none in the past day.
    as_of: datetime | None
    # False when the newest reading is too old to act on; `pollutants` is then empty and `overall` null.
    is_current: bool
    overall: OverallAqiOut | None
    at_or_above_health_threshold: bool | None = None
    pollutants: list[PollutantAqiOut]


class OverviewStationOut(BaseModel):
    station_id: str
    name: str
    lat: float
    lon: float
    city: str
    region_id: str | None = None
    current_aqi: OverviewCurrentAqiOut
    # One per forecast pollutant, the same object GET /forecast/{id}/exceedance returns, except that
    # `forecast_made_at` is null when the station has no current forecast (that endpoint reports
    # how old the last one is).
    outlooks: list[ExceedanceSummaryOut]


class OverviewHistoryStationOut(BaseModel):
    station_id: str
    # One entry per hour of OverviewHistoryOut.hours; null where nothing was measured.
    values: list[float | None]
    # The category each value falls in by the region's thresholds; null with the value,
    # and for a station outside every region.
    categories: list[str | None]


class OverviewHistoryOut(BaseModel):
    """The last hours of one pollutant at every active station, for a view that
    shows the stations side by side. A missing hour is null, never a guess."""

    generated_at: datetime
    pollutant: str
    hours: list[datetime]  # the start of each hour (UTC), oldest first
    stations: list[OverviewHistoryStationOut]


class OverviewOut(BaseModel):
    """Everything a map needs for every active station, in one response."""

    generated_at: datetime
    attribution: str  # for the current-AQI data; must be shown wherever it is displayed
    stations: list[OverviewStationOut]
