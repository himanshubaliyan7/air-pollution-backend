"""Open-Meteo near-term weather forecast ingestion.

Fills a real, verified gap in ERA5: ERA5 has an inherent ~5 day publication
lag even for its ERA5T preliminary extension (confirmed live - a forecast
made "now" found the latest available ERA5/ERA5T data was 5 days old, with
nothing at all more recent). Since build_feature_frame joins the exact
as-of hour's weather, that gap meant a forecast could never actually be
generated in real time using ERA5 alone. Open-Meteo (free, no API key,
global coverage, confirmed via a live call) fills exactly that gap; it is
never used for training - build_feature_frame's weather join prefers
ERA5 > ERA5T > FORECAST (see features/weather_features.py / build_features.py),
so historical/materialized features always reflect real reanalysis data,
and FORECAST only ever supplies the most recent few days at inference time.

Response shape confirmed via a live call to api.open-meteo.com/v1/forecast
with multiple comma-separated lat/lon pairs: a JSON *list* (one object per
coordinate, same order as requested), each with
{"hourly": {"time": [...], "wind_speed_10m": [...], "wind_direction_10m": [...],
"relative_humidity_2m": [...]}} - "time" is naive ISO8601 (no offset) because
timezone=UTC was requested, so it's treated as UTC directly. Response
latitude/longitude are the provider's snapped grid point, not necessarily
identical to the requested value, so grid_cell_id is derived from the
REQUESTED coordinates (matching era5_client.py's cell ids) rather than the
response's own lat/lon.
"""

import logging
import math
from datetime import datetime, timezone

import requests

from common.constants import WeatherProductType
from ingestion.weather.era5_client import WeatherReading
from ingestion.weather.grid import DELHI_NCR_AREA, delhi_ncr_grid_points, grid_cell_id

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoClient:
    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()

    def fetch(
        self,
        forecast_days: int = 7,
        area: tuple[float, float, float, float] = DELHI_NCR_AREA,
    ) -> list[WeatherReading]:
        points = delhi_ncr_grid_points(area)
        params = {
            "latitude": ",".join(str(lat) for lat, _ in points),
            "longitude": ",".join(str(lon) for _, lon in points),
            "hourly": "wind_speed_10m,wind_direction_10m,relative_humidity_2m",
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "forecast_days": forecast_days,
        }
        resp = self._session.get(BASE_URL, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = [data]  # a single-coordinate request returns a bare object, not a list

        readings: list[WeatherReading] = []
        for (lat, lon), point in zip(points, data):
            hourly = point.get("hourly", {})
            times = hourly.get("time", [])
            speeds = hourly.get("wind_speed_10m", [])
            directions = hourly.get("wind_direction_10m", [])
            humidities = hourly.get("relative_humidity_2m", [])

            cell_id = grid_cell_id(lat, lon)
            for t, speed, direction, humidity in zip(times, speeds, directions, humidities):
                if speed is None or direction is None or humidity is None:
                    continue
                observed_at = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
                # wind_speed_10m/wind_direction_10m give speed+direction
                # directly (unlike ERA5's raw u/v components) - back out u/v
                # for schema consistency (raw_weather_readings stores u/v as
                # the primary columns, speed/direction as derived).
                readings.append(
                    WeatherReading(
                        grid_cell_id=cell_id,
                        lat=lat,
                        lon=lon,
                        observed_at=observed_at,
                        u_wind=-speed * _sin_deg(direction),
                        v_wind=-speed * _cos_deg(direction),
                        wind_speed=speed,
                        wind_direction=direction,
                        relative_humidity=humidity,
                        product_type=WeatherProductType.FORECAST,
                    )
                )
        return readings


def _sin_deg(deg: float) -> float:
    return math.sin(math.radians(deg))


def _cos_deg(deg: float) -> float:
    return math.cos(math.radians(deg))
