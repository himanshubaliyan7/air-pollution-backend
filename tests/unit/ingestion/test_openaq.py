from datetime import datetime, timezone
from unittest.mock import patch

from common.constants import Pollutant
from ingestion.sources.openaq import OpenAQSource


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, responses_by_path):
        self._responses_by_path = responses_by_path
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for path, response in self._responses_by_path.items():
            if url.endswith(path):
                return response
        raise AssertionError(f"No fake response configured for {url}")


def test_list_stations_parses_locations_response():
    responses = {
        "/locations": FakeResponse(
            {
                "meta": {"found": 2},
                "results": [
                    {
                        "id": 111,
                        "name": "Anand Vihar",
                        "locality": "Delhi",
                        "coordinates": {"latitude": 28.6469, "longitude": 77.3152},
                    },
                    {
                        "id": 222,
                        "name": "RK Puram",
                        "locality": "Delhi",
                        "coordinates": {"latitude": 28.5644, "longitude": 77.1770},
                    },
                ],
            }
        )
    }
    source = OpenAQSource(api_key="test-key", session=FakeSession(responses))

    stations = source.list_stations(bbox=(76.6, 28.2, 77.6, 29.0), country="IN")

    assert len(stations) == 2
    assert stations[0].source_location_id == "111"
    assert stations[0].name == "Anand Vihar"
    assert stations[0].lat == 28.6469


def test_fetch_readings_resolves_sensors_and_parses_hours():
    responses = {
        "/locations/111": FakeResponse(
            {
                "id": 111,
                "sensors": [
                    {"id": 9001, "parameter": {"name": "pm25", "units": "ug/m3"}},
                    {"id": 9002, "parameter": {"name": "no2", "units": "ug/m3"}},
                    {"id": 9003, "parameter": {"name": "o3", "units": "ug/m3"}},
                ],
            }
        ),
        "/sensors/9001/hours": FakeResponse(
            {
                "meta": {"found": 1},
                "results": [
                    {
                        "value": 145.2,
                        "parameter": {"name": "pm25", "units": "ug/m3"},
                        "period": {"datetimeFrom": {"utc": "2026-09-19T00:00:00Z", "local": "2026-09-19T05:30:00+05:30"}},
                    }
                ],
            }
        ),
        "/sensors/9002/hours": FakeResponse({"meta": {"found": 0}, "results": []}),
    }
    source = OpenAQSource(api_key="test-key", session=FakeSession(responses))

    readings = source.fetch_readings(
        source_location_ids=["111"],
        pollutants=[Pollutant.PM25, Pollutant.NO2],
        start=datetime(2026, 9, 19, tzinfo=timezone.utc),
        end=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )

    assert len(readings) == 1
    r = readings[0]
    assert r.pollutant == Pollutant.PM25
    assert r.value == 145.2
    assert r.observed_at == datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc)
    assert r.source_location_id == "111"


def test_fetch_readings_skips_a_sensor_that_persistently_5xxs_without_crashing():
    """A real backfill run hit a genuine 500 from OpenAQ on one sensor's
    /hours endpoint, which crashed the entire multi-station fetch before
    this fix - it must instead skip just that sensor and keep going."""
    responses = {
        "/locations/111": FakeResponse(
            {
                "id": 111,
                "sensors": [
                    {"id": 9001, "parameter": {"name": "pm25", "units": "ug/m3"}},
                ],
            }
        ),
        "/sensors/9001/hours": FakeResponse({}, status_code=500),
        "/locations/222": FakeResponse(
            {
                "id": 222,
                "sensors": [
                    {"id": 9002, "parameter": {"name": "pm25", "units": "ug/m3"}},
                ],
            }
        ),
        "/sensors/9002/hours": FakeResponse(
            {
                "meta": {"found": 1},
                "results": [
                    {
                        "value": 88.0,
                        "parameter": {"name": "pm25", "units": "ug/m3"},
                        "period": {"datetimeFrom": {"utc": "2026-09-19T00:00:00Z"}},
                    }
                ],
            }
        ),
    }
    source = OpenAQSource(api_key="test-key", session=FakeSession(responses))

    with patch("ingestion.sources.openaq.time.sleep"):  # don't actually wait through the real backoff
        readings = source.fetch_readings(
            source_location_ids=["111", "222"],
            pollutants=[Pollutant.PM25],
            start=datetime(2026, 9, 19, tzinfo=timezone.utc),
            end=datetime(2026, 9, 20, tzinfo=timezone.utc),
        )

    # Station 111's sensor 5xx'd persistently and was skipped; station 222's
    # reading still comes through.
    assert len(readings) == 1
    assert readings[0].source_location_id == "222"
    assert readings[0].value == 88.0


def test_sensor_ids_are_cached_across_fetch_readings_calls():
    """A multi-chunk backfill calls fetch_readings once per date range for
    the same station list - resolving sensor ids fresh every time multiplies
    real request volume and burns through OpenAQ's rate limit (this
    regressed in practice during a real 180-day backfill)."""
    calls = {"locations": 0}

    class CountingSession(FakeSession):
        def get(self, url, params=None, timeout=None):
            if "/locations/" in url:
                calls["locations"] += 1
            return super().get(url, params=params, timeout=timeout)

    responses = {
        "/locations/111": FakeResponse(
            {"id": 111, "sensors": [{"id": 9001, "parameter": {"name": "pm25", "units": "ug/m3"}}]}
        ),
        "/sensors/9001/hours": FakeResponse({"meta": {"found": 0}, "results": []}),
    }
    source = OpenAQSource(api_key="test-key", session=CountingSession(responses))

    source.fetch_readings(
        source_location_ids=["111"],
        pollutants=[Pollutant.PM25],
        start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    source.fetch_readings(
        source_location_ids=["111"],
        pollutants=[Pollutant.PM25],
        start=datetime(2026, 9, 8, tzinfo=timezone.utc),
        end=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )

    assert calls["locations"] == 1  # second call reused the cached sensor id


def test_fetch_readings_floors_timestamps_to_the_hour():
    """Real Delhi NCR CPCB data via OpenAQ is consistently stamped at :30
    past the hour (confirmed against a live 180-day backfill: 438k/438k
    readings), unlike ERA5 which is cleanly on the hour. Every downstream
    hourly bucket (lag/rolling features, daily exceedance aggregation,
    weather join) assumes a clean :00 grid - an unfloored :30 offset
    silently broke every hour-alignment downstream in practice."""
    responses = {
        "/locations/111": FakeResponse(
            {"id": 111, "sensors": [{"id": 9001, "parameter": {"name": "pm25", "units": "ug/m3"}}]}
        ),
        "/sensors/9001/hours": FakeResponse(
            {
                "meta": {"found": 1},
                "results": [
                    {
                        "value": 100.0,
                        "parameter": {"name": "pm25", "units": "ug/m3"},
                        "period": {"datetimeFrom": {"utc": "2026-09-19T11:30:00Z"}},
                    }
                ],
            }
        ),
    }
    source = OpenAQSource(api_key="test-key", session=FakeSession(responses))

    readings = source.fetch_readings(
        source_location_ids=["111"],
        pollutants=[Pollutant.PM25],
        start=datetime(2026, 9, 19, tzinfo=timezone.utc),
        end=datetime(2026, 9, 20, tzinfo=timezone.utc),
    )

    assert len(readings) == 1
    assert readings[0].observed_at == datetime(2026, 9, 19, 11, 0, tzinfo=timezone.utc)


def test_pagination_stops_on_short_page():
    responses = {
        "/locations": FakeResponse(
            {
                "meta": {"found": 1},
                "results": [
                    {"id": 1, "name": "A", "locality": "Delhi", "coordinates": {"latitude": 1.0, "longitude": 2.0}}
                ],
            }
        )
    }
    source = OpenAQSource(api_key="test-key", session=FakeSession(responses))
    stations = source.list_stations(bbox=(0, 0, 1, 1), country="IN")
    assert len(stations) == 1
