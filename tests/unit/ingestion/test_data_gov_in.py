"""data.gov.in CPCB real-time AQI feed. Row shapes are copied from live responses
(2026-09-21): lat/lon are strings, "NA" marks missing, last_update is IST."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from ingestion.sources.data_gov_in import DataGovInClient, parse_record

ROW = {
    "country": "India", "state": "Delhi", "city": "Delhi", "station": "Anand Vihar, Delhi - DPCC ",
    "last_update": "20-09-2026 21:00:00", "latitude": "28.647622", "longitude": "77.315809",
    "pollutant_id": "PM2.5", "min_value": "45", "max_value": "370", "avg_value": "191",
}


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.headers, self.responses, self.calls = {}, list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append(params)
        return self.responses.pop(0)


def test_parse_converts_ist_to_utc_and_reads_string_numbers():
    rec = parse_record(ROW)
    assert rec.observed_at == datetime(2026, 9, 20, 15, 30, tzinfo=timezone.utc)  # 21:00 IST
    assert (rec.lat, rec.lon) == (28.647622, 77.315809)
    assert (rec.sub_index_min, rec.sub_index_max, rec.sub_index_avg) == (45.0, 370.0, 191.0)
    assert rec.station_name == "Anand Vihar, Delhi - DPCC" and rec.pollutant_id == "PM2.5"


def test_na_becomes_none_and_unusable_rows_are_dropped():
    rec = parse_record({**ROW, "pollutant_id": "PM10", "min_value": "NA", "max_value": "NA", "avg_value": "NA"})
    assert (rec.sub_index_min, rec.sub_index_max, rec.sub_index_avg) == (None, None, None)
    assert parse_record({**ROW, "latitude": "NA"}) is None
    assert parse_record({**ROW, "last_update": "not a date"}) is None
    assert parse_record({k: v for k, v in ROW.items() if k != "last_update"}) is None


def test_client_requires_a_key_and_sends_a_user_agent():
    with pytest.raises(ValueError):
        DataGovInClient(api_key="")
    session = FakeSession([FakeResponse({"records": [ROW], "total": 1})])
    DataGovInClient(api_key="k", session=session).fetch()
    assert "User-Agent" in session.headers and session.calls[0]["api-key"] == "k"


def test_pages_until_total_and_dedupes_repeats_from_unstable_ordering():
    other = {**ROW, "station": "Alipur, Delhi - DPCC", "latitude": "28.8", "longitude": "77.1"}
    session = FakeSession([
        FakeResponse({"records": [ROW, other], "total": 4}),
        FakeResponse({"records": [ROW, {**other, "avg_value": "170"}], "total": 4}),  # repeats: ordering is unstable
    ])
    records = DataGovInClient(api_key="k", session=session, page_limit=2).fetch()
    assert [c["offset"] for c in session.calls] == [0, 2]
    assert len(records) == 2  # one per station+pollutant
    assert {r.station_name: r.sub_index_avg for r in records}["Alipur, Delhi - DPCC"] == 170.0  # latest wins


def test_stops_on_an_empty_page():
    session = FakeSession([FakeResponse({"records": [ROW], "total": 99}), FakeResponse({"records": [], "total": 99})])
    assert len(DataGovInClient(api_key="k", session=session, page_limit=1).fetch()) == 1


def test_retries_server_errors_then_succeeds_and_gives_up_eventually():
    ok = FakeResponse({"records": [ROW], "total": 1})
    with patch("ingestion.sources.data_gov_in.time.sleep"):
        session = FakeSession([FakeResponse({}, 503), ok])
        assert len(DataGovInClient(api_key="k", session=session).fetch()) == 1
        session = FakeSession([FakeResponse({}, 503)] * 3)
        with pytest.raises(RuntimeError):
            DataGovInClient(api_key="k", session=session).fetch()
