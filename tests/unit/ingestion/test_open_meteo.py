from datetime import datetime, timezone

from common.constants import WeatherProductType
from ingestion.weather.grid import delhi_ncr_grid_points
from ingestion.weather.open_meteo_client import OpenMeteoClient


class FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def raise_for_status(self):
        pass

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, json_data):
        self._json_data = json_data
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_params = params
        return FakeResponse(self._json_data)


def test_fetch_parses_list_response_into_weather_readings_tagged_forecast():
    points = delhi_ncr_grid_points()
    # Two grid points' worth of hourly data, matching the real confirmed
    # Open-Meteo list-of-objects response shape.
    fake_data = [
        {
            "latitude": lat + 0.001,  # provider snaps slightly - grid_cell_id must use the REQUESTED point
            "longitude": lon + 0.001,
            "hourly": {
                "time": ["2026-09-20T00:00", "2026-09-20T01:00"],
                "wind_speed_10m": [2.5, 3.0],
                "wind_direction_10m": [180.0, 190.0],
                "relative_humidity_2m": [60.0, 62.0],
            },
        }
        for lat, lon in points
    ]
    session = FakeSession(fake_data)
    client = OpenMeteoClient(session=session)

    readings = client.fetch(forecast_days=3)

    assert len(readings) == len(points) * 2
    assert all(r.product_type == WeatherProductType.FORECAST for r in readings)

    first = readings[0]
    assert first.grid_cell_id == f"{points[0][0]:.2f}_{points[0][1]:.2f}"
    assert first.observed_at == datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)
    assert first.wind_speed == 2.5
    assert first.relative_humidity == 60.0
    # u/v must be invertible back to the original speed/direction.
    import math

    recovered_speed = math.hypot(first.u_wind, first.v_wind)
    assert abs(recovered_speed - first.wind_speed) < 1e-6


def test_fetch_requests_all_grid_points_as_comma_separated_lists():
    points = delhi_ncr_grid_points()
    fake_data = [{"hourly": {"time": [], "wind_speed_10m": [], "wind_direction_10m": [], "relative_humidity_2m": []}} for _ in points]
    session = FakeSession(fake_data)
    client = OpenMeteoClient(session=session)

    client.fetch(forecast_days=3)

    assert session.last_params["latitude"].count(",") == len(points) - 1
    assert session.last_params["longitude"].count(",") == len(points) - 1
    assert session.last_params["wind_speed_unit"] == "ms"
