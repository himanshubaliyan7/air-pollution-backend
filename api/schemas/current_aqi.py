from datetime import datetime

from pydantic import BaseModel


class PollutantAqiOut(BaseModel):
    pollutant_id: str  # as published by CPCB: PM2.5, PM10, NO2, SO2, CO, OZONE, NH3
    sub_index_avg: float | None
    sub_index_min: float | None
    sub_index_max: float | None
    category: str | None  # id from the region's aqi_categories, derived from sub_index_avg


class OverallAqiOut(BaseModel):
    aqi: int  # the worst sub-index (CPCB rule)
    category: str
    driver: str  # the pollutant that sets it


class CurrentAqiOut(BaseModel):
    station_id: str
    # When the newest reading was published (UTC); null if the station never had one.
    as_of: datetime | None
    # False when the newest reading is too old to act on; `pollutants` is then empty and
    # `overall` null on purpose, exactly as for forecasts.
    is_current: bool
    aqi_standard: str | None
    timezone: str | None
    # null unless CPCB's rule is met (at least 3 pollutants, one of them PM2.5 or PM10).
    overall: OverallAqiOut | None
    # CPCB AQI SUB-INDICES over a rolling window, not ug/m3 concentrations.
    pollutants: list[PollutantAqiOut]
    attribution: str  # must be shown wherever this data is displayed
