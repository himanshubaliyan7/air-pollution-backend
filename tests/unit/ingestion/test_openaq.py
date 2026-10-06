from datetime import datetime, timezone
from unittest.mock import patch

from common.constants import Pollutant
from ingestion.sources.openaq import OpenAQSource, declared_unit
from ingestion.units import CANONICAL_UNIT, to_canonical


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


def test_cpcb_no2_labelled_ppb_is_kept_as_ug_m3():
    """OpenAQ labels CPCB's NO2 "ppb" but the values are ug/m3 (CPCB's own AQI
    feed matches the raw number); converting them overstated NO2 by 1.88x."""
    responses = {
        "/locations/111": FakeResponse(
            {
                "id": 111,
                "coordinates": {"latitude": 28.6, "longitude": 77.2},  # inside Delhi NCR, where the rule was verified
                "sensors": [{"id": 9002, "parameter": {"name": "no2", "units": "ppb"}}],
            }
        ),
        "/sensors/9002/hours": FakeResponse(
            {
                "meta": {"found": 1},
                "results": [
                    {
                        "value": 28.0,
                        "parameter": {"name": "no2", "units": "ppb"},
                        "period": {"datetimeFrom": {"utc": "2026-09-28T00:30:00Z"}},
                    }
                ],
            }
        ),
    }
    source = OpenAQSource(api_key="test-key", session=FakeSession(responses))

    [r] = source.fetch_readings(
        source_location_ids=["111"],
        pollutants=[Pollutant.NO2],
        start=datetime(2026, 9, 28, tzinfo=timezone.utc),
        end=datetime(2026, 9, 29, tzinfo=timezone.utc),
    )

    assert r.unit == CANONICAL_UNIT
    assert to_canonical(r.pollutant, r.value, r.unit) == (28.0, CANONICAL_UNIT)  # what the loader stores


def test_only_the_known_mislabel_is_corrected():
    assert declared_unit(Pollutant.NO2, "ppb", 28.6, 77.2) == CANONICAL_UNIT
    assert declared_unit(Pollutant.NO2, "µg/m³") == "µg/m³"
    assert declared_unit(Pollutant.PM25, "ppb") == "ppb"  # still dropped by the loader, not relabelled


def test_mislabel_rule_has_a_margin_around_the_verified_region():
    """Rohtak/Dharuhera/Bhiwadi sit ~0.01 degree inside the Delhi bbox edge; OpenAQ's
    coordinates for them can land just outside it and must still get the rule."""
    assert declared_unit(Pollutant.NO2, "ppb", 28.19, 76.59) == CANONICAL_UNIT  # just outside the bbox corner
    assert declared_unit(Pollutant.NO2, "ppb", 28.10, 76.50) == CANONICAL_UNIT  # 0.1 degree outside
    assert declared_unit(Pollutant.NO2, "ppb", 19.06, 72.86) is None  # Mumbai: unverified, skipped
    assert declared_unit(Pollutant.NO2, "ppb", 27.0, 77.2) is None  # 1.2 degrees south of the margin
    assert declared_unit(Pollutant.NO2, "ppb") is None  # no coordinates


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


def test_location_last_data_times_parses_datetime_last_and_missing():
    responses = {
        "/locations": FakeResponse(
            {
                "meta": {"found": 3},
                "results": [
                    {"id": 1, "datetimeLast": {"utc": "2026-09-20T13:00:00Z", "local": "x"}},
                    {"id": 2, "datetimeLast": None},
                    {"id": 3},
                ],
            }
        )
    }
    source = OpenAQSource(api_key="k", session=FakeSession(responses))
    out = source.location_last_data_times(bbox=(76.6, 28.2, 77.6, 29.0), country="IN")
    assert out == {"1": datetime(2026, 9, 20, 13, tzinfo=timezone.utc), "2": None, "3": None}


def test_failed_location_lookup_is_skipped_not_fatal():
    """Regression: _get() raising RuntimeError (retries exhausted) on a location
    lookup crashed the whole fetch and lost readings already collected."""
    class Flaky(FakeSession):
        def get(self, url, params=None, timeout=None):
            if url.endswith("/locations/bad"):
                return FakeResponse({}, status_code=429)
            return super().get(url, params=params, timeout=timeout)

    responses = {
        "/locations/good": FakeResponse({"results": [{"sensors": [{"id": 9, "parameter": {"name": "pm25"}}]}]}),
        "/sensors/9/hours": FakeResponse(
            {"meta": {"found": 1}, "results": [{"value": 12.0, "period": {"datetimeFrom": {"utc": "2026-09-20T13:30:00Z"}}}]}
        ),
    }
    with patch("ingestion.sources.openaq.time.sleep"):
        source = OpenAQSource(api_key="k", session=Flaky(responses))
        readings = source.fetch_readings(
            source_location_ids=["bad", "good"], pollutants=[Pollutant.PM25],
            start=datetime(2026, 9, 20, 12, tzinfo=timezone.utc), end=datetime(2026, 9, 20, 14, tzinfo=timezone.utc),
        )
    assert [r.source_location_id for r in readings] == ["good"]


def test_waits_for_window_reset_when_quota_is_exhausted_instead_of_hitting_429():
    """OpenAQ allows 60 requests/min and says how many remain on every response;
    bursting into 429s made runs crawl and drop sensors."""
    class Quota(FakeResponse):
        def __init__(self, remaining, reset):
            super().__init__({"results": [], "meta": {"found": 0}})
            self.headers = {"X-Ratelimit-Remaining": str(remaining), "X-Ratelimit-Reset": str(reset)}

    class Seq(FakeSession):
        def __init__(self, resps):
            super().__init__({})
            self._resps = list(resps)

        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return self._resps.pop(0)

    sleeps = []
    with patch("ingestion.sources.openaq.time.sleep", side_effect=sleeps.append):
        source = OpenAQSource(api_key="k", session=Seq([Quota(1, 20), Quota(59, 55)]))
        source._get("/locations/1", {})   # leaves 1 request in the window, resets in 20s
        assert sleeps == []               # nothing to wait for yet
        source._get("/locations/2", {})   # must wait out the window BEFORE sending
    assert len(sleeps) == 1 and 15 < sleeps[0] <= 21


def test_429_backoff_uses_the_reported_reset_when_no_retry_after():
    class Limited(FakeResponse):
        def __init__(self):
            super().__init__({}, status_code=429)
            self.headers = {"X-Ratelimit-Reset": "7"}

    class Seq(FakeSession):
        def __init__(self, resps):
            super().__init__({})
            self._resps = list(resps)

        def get(self, url, params=None, timeout=None):
            return self._resps.pop(0)

    sleeps = []
    with patch("ingestion.sources.openaq.time.sleep", side_effect=sleeps.append):
        source = OpenAQSource(api_key="k", session=Seq([Limited(), FakeResponse({"results": []})]))
        source._get("/x", {})
    assert sleeps == [7.0]


def test_rejected_api_key_fails_loudly_instead_of_being_skipped_per_station():
    """Regression: a revoked key made every location lookup return 401; each was skipped
    like any transient failure, so ingestion wrote nothing yet reported success."""
    import pytest

    from ingestion.sources.openaq import OpenAQAuthError

    session = FakeSession({"/locations/1": FakeResponse({"detail": "Invalid credentials"}, status_code=401)})
    source = OpenAQSource(api_key="k", session=session)
    with pytest.raises(OpenAQAuthError):
        source.fetch_readings(
            source_location_ids=["1", "2"], pollutants=[Pollutant.PM25],
            start=datetime(2026, 9, 20, 12, tzinfo=timezone.utc), end=datetime(2026, 9, 20, 14, tzinfo=timezone.utc),
        )
    assert len(session.calls) == 1  # no retries, and it did not carry on to the next station


def test_fetch_raw_hours_keeps_the_label_openaq_gives_and_costs_three_requests():
    """The unit check needs the NO2 hours the ingestion would skip (Mumbai, "ppb")."""
    hour = {"value": 12.0, "parameter": {"units": "ppb"}, "period": {"datetimeFrom": {"utc": "2026-10-07T00:30:00Z"}}}
    session = FakeSession({
        "/locations/501": FakeResponse({"id": 501, "coordinates": {"latitude": 19.06, "longitude": 72.86}, "sensors": [
            {"id": 1, "parameter": {"name": "no2"}}, {"id": 2, "parameter": {"name": "pm25"}}]}),
        "/sensors/1/hours": FakeResponse({"meta": {"found": 1}, "results": [hour]}),
        "/sensors/2/hours": FakeResponse({"meta": {"found": 0}, "results": []}),
    })
    source = OpenAQSource(api_key="k", session=session)
    out = source.fetch_raw_hours("501", [Pollutant.NO2, Pollutant.PM25], datetime(2026, 10, 7, tzinfo=timezone.utc), datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert out == {Pollutant.NO2: [(datetime(2026, 10, 7, 0, 0, tzinfo=timezone.utc), 12.0, "ppb")], Pollutant.PM25: []}
    assert len(session.calls) == 3
