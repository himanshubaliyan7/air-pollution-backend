"""CPCB's own CAAQMS feed. The XML is cut down from a live response (2026-09-27,
28-09-2026 00:00 IST): same station names and coordinates as data.gov.in."""

from datetime import datetime, timezone

import pytest
import requests

from ingestion.sources import cpcb_caaqms
from ingestion.sources.cpcb_caaqms import CpcbCaaqmsClient, parse_feed

FEED = b"""<?xml version='1.0' encoding='UTF-8'?>
<AqIndex>
<Country id="India">
<State id="Andaman and Nicobar"/>
<State id="Delhi">
<City id="Delhi">
<Station id="Anand Vihar, Delhi - DPCC" lastupdate="28-09-2026 00:00:00" latitude="28.647622" longitude="77.315809">
<Pollutant_Index id="PM2.5" Min="5" Max="74" Avg="23" Hourly_sub_index="11"/>
<Pollutant_Index id="NO2" Min="7" Max="19" Avg="14" Hourly_sub_index="15"/>
<Pollutant_Index id="OZONE" Min="NA" Max="NA" Avg="NA" Hourly_sub_index="NA"/>
<Air_Quality_Index Value="67" Predominant_Parameter="CO"/>
</Station>
<Station id="No Coordinates - DPCC" lastupdate="28-09-2026 00:00:00" latitude="NA" longitude="77.3">
<Pollutant_Index id="PM2.5" Min="5" Max="74" Avg="23" Hourly_sub_index="11"/>
</Station>
</City>
</State>
<State id="Uttar Pradesh">
<City id="Noida">
<Station id="Sector - 62, Noida - IMD" lastupdate="27-09-2026 23:00:00" latitude="28.6245479" longitude="77.3577104">
<Pollutant_Index id="PM2.5" Min="20" Max="60" Avg="40" Hourly_sub_index="35"/>
</Station>
</City>
</State>
</Country>
</AqIndex>"""


def test_parse_reads_every_pollutant_with_ist_converted_to_utc():
    recs = parse_feed(FEED)
    anand = [r for r in recs if r.station_name == "Anand Vihar, Delhi - DPCC"]
    pm = next(r for r in anand if r.pollutant_id == "PM2.5")
    assert pm.observed_at == datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)  # 00:00 IST
    assert (pm.lat, pm.lon) == (28.647622, 77.315809)
    assert (pm.sub_index_min, pm.sub_index_max, pm.sub_index_avg, pm.sub_index_hourly) == (5, 74, 23, 11)
    assert (pm.city, pm.state) == ("Delhi", "Delhi")
    assert {r.pollutant_id for r in anand} == {"PM2.5", "NO2", "OZONE"}  # the overall AQI element is not a pollutant


def test_na_becomes_none_and_stations_without_coordinates_are_dropped():
    recs = parse_feed(FEED)
    ozone = next(r for r in recs if r.pollutant_id == "OZONE")
    assert (ozone.sub_index_min, ozone.sub_index_avg, ozone.sub_index_hourly) == (None, None, None)
    assert not any(r.station_name.startswith("No Coordinates") for r in recs)
    assert next(r for r in recs if r.city == "Noida").state == "Uttar Pradesh"


class FakeResponse:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses):
        self.headers, self.responses = {}, list(responses)

    def get(self, url, timeout=None):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_fetch_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(cpcb_caaqms.time, "sleep", lambda s: None)
    session = FakeSession([requests.ConnectTimeout("t"), FakeResponse(b"", 502), FakeResponse(FEED)])
    assert len(CpcbCaaqmsClient(session=session).fetch()) == 4


def test_fetch_fails_loudly_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr(cpcb_caaqms.time, "sleep", lambda s: None)
    session = FakeSession([FakeResponse(b"", 502), FakeResponse(b"<not xml"), FakeResponse(b"", 503)])
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        CpcbCaaqmsClient(session=session).fetch()
