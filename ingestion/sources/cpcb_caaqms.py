"""CPCB's own public CAAQMS feed: https://airquality.cpcb.gov.in/caaqms/rss_feed

The same real-time AQI dataset that data.gov.in republishes (ingestion/sources/
data_gov_in.py), read at the source. Live-checked 2026-09-27 from the Mumbai
server: HTTP 200 in 0.3 s, ~360 KB XML, 481 stations, no key needed. Adopted as
the primary current-AQI source after both relays failed for days while CPCB kept
publishing (data.gov.in 502s from 2026-09-25; OpenAQ's CPCB relay from 2026-09-24).

- Shape: AqIndex > Country > State[id] > City[id] > Station[id, lastupdate,
  latitude, longitude] > Pollutant_Index[id, Min, Max, Avg, Hourly_sub_index],
  plus one Air_Quality_Index per station (ignored: we compute the overall AQI
  ourselves, common.aqi.overall_aqi).
- Station names, coordinates and Min/Max/Avg are identical to the data.gov.in
  rows (e.g. Anand Vihar 28.647622, 77.315809), so the same loader and station
  matching apply. Min/Max/Avg are AQI SUB-INDICES, not concentrations.
- Hourly_sub_index is extra (not on data.gov.in). Stored for later study; what
  window it covers is not documented, so nothing uses it yet.
- "NA" marks a missing value; lastupdate is "DD-MM-YYYY HH:MM:SS" in IST.
- Another project reports the feed timing out from outside India; our server
  is in India. Re-check reachability if the backend ever moves abroad.
- Licence: CPCB publishes this dataset under GODL-India on data.gov.in; the
  owner decided (2026-09-27) to rely on that licence for the direct feed too.
  See docs/api_compliance.md.
"""

import logging
import time
import xml.etree.ElementTree as ET

import requests

from ingestion.sources.data_gov_in import USER_AGENT, AqiRecord, _number, _parse_last_update

logger = logging.getLogger(__name__)

FEED_URL = "https://airquality.cpcb.gov.in/caaqms/rss_feed"
MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 30


def parse_feed(xml_text: str | bytes) -> list[AqiRecord]:
    """Every usable (station, pollutant) row. Stations without coordinates or a
    timestamp are dropped, as in data_gov_in.parse_record."""
    root = ET.fromstring(xml_text)
    records: list[AqiRecord] = []
    for state in root.iter("State"):
        for city in state.iter("City"):
            for station in city.iter("Station"):
                try:
                    lat, lon = float(station.get("latitude")), float(station.get("longitude"))
                    observed_at = _parse_last_update(station.get("lastupdate"))
                except (TypeError, ValueError):
                    continue
                for pol in station.iter("Pollutant_Index"):
                    records.append(AqiRecord(
                        station_name=(station.get("id") or "").strip(),
                        city=(city.get("id") or "").strip(),
                        state=(state.get("id") or "").strip(),
                        lat=lat,
                        lon=lon,
                        pollutant_id=(pol.get("id") or "").strip(),
                        sub_index_min=_number(pol.get("Min")),
                        sub_index_max=_number(pol.get("Max")),
                        sub_index_avg=_number(pol.get("Avg")),
                        observed_at=observed_at,
                        sub_index_hourly=_number(pol.get("Hourly_sub_index")),
                    ))
    return records


class CpcbCaaqmsClient:
    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})

    def fetch(self) -> list[AqiRecord]:
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session.get(FEED_URL, timeout=TIMEOUT_SECONDS)
                resp.raise_for_status()
                return parse_feed(resp.content)
            except (requests.RequestException, ET.ParseError) as exc:
                last_error = exc
                logger.warning("CPCB CAAQMS feed request failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2.0 * attempt)
        raise RuntimeError(f"CPCB CAAQMS feed failed after {MAX_ATTEMPTS} attempts: {last_error}")
