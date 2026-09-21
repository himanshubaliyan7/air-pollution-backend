"""CPCB "Real time Air Quality Index" feed from data.gov.in.

Resource 3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69, published by the Central
Pollution Control Board under the Government Open Data License - India
(commercial use allowed with attribution). Live-tested 2026-09-21:

- One row per station and pollutant, refreshed hourly, roughly 30-90 minutes
  after the hour. `last_update` is "DD-MM-YYYY HH:MM:SS" in IST, on the hour.
- It is a CURRENT SNAPSHOT ONLY: no history. History accumulates only because
  we store every hourly snapshot (db.models.StationAqiSnapshot).
- The values (`min_value`/`max_value`/`avg_value`) are CPCB AQI SUB-INDICES over
  a rolling window, NOT hourly ug/m3 concentrations. Evidence: at 15 of 33 Delhi
  stations the PM10 "avg" was LOWER than the PM2.5 "avg" (e.g. 168 vs 198),
  impossible for concentrations (PM10 includes PM2.5) but expected on the
  sub-index scale, plus CO values ~80-90 and PM2.5 maxima capped at 500. The API
  itself only calls the fields pollutant_min/max/avg, so this is inferred, not
  documented; confirm against one station on CPCB's CCR portal when possible.
  Never feed these values to the hourly-ug/m3 forecasting models.
- Latitude/longitude are strings; "NA" marks a missing value; result ordering
  is not stable between calls, so a paged crawl must dedupe on
  station+pollutant.
- Sending an explicit User-Agent matters: a bare urllib client stalled.
- A personal API key is free (data.gov.in -> My Account). The publicly
  documented sample key is capped at 10 rows per request and is for
  experimentation only.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

RESOURCE_URL = "https://api.data.gov.in/resource/3b01bcb8-0b14-4abf-b6f2-c1bfd384ba69"
USER_AGENT = "airpollution-forecast/0.1 (school air-quality service)"
FEED_TIMEZONE = ZoneInfo("Asia/Kolkata")
DEFAULT_PAGE_LIMIT = 500
MAX_PAGES = 100  # safety stop against a misbehaving `total`
MAX_ATTEMPTS = 3

ATTRIBUTION = (
    "Source: Central Pollution Control Board (CPCB), Ministry of Environment, Forest and Climate Change, "
    "Government of India, via data.gov.in, published under the Government Open Data License - India (GODL-India)."
)


@dataclass(frozen=True)
class AqiRecord:
    station_name: str
    city: str
    state: str
    lat: float
    lon: float
    pollutant_id: str  # as published: PM2.5, PM10, NO2, SO2, CO, OZONE, NH3
    sub_index_min: float | None
    sub_index_max: float | None
    sub_index_avg: float | None
    observed_at: datetime  # timezone-aware UTC


def _number(raw) -> float | None:
    if raw is None:
        return None
    raw = str(raw).strip()
    if raw == "" or raw.upper() == "NA":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_last_update(raw: str) -> datetime:
    local = datetime.strptime(raw.strip(), "%d-%m-%Y %H:%M:%S").replace(tzinfo=FEED_TIMEZONE)
    return local.astimezone(timezone.utc)


def parse_record(row: dict) -> AqiRecord | None:
    """None for rows we cannot use (no coordinates or timestamp)."""
    try:
        lat, lon = float(row["latitude"]), float(row["longitude"])
        observed_at = _parse_last_update(row["last_update"])
    except (KeyError, TypeError, ValueError):
        return None
    return AqiRecord(
        station_name=(row.get("station") or "").strip(),
        city=(row.get("city") or "").strip(),
        state=(row.get("state") or "").strip(),
        lat=lat,
        lon=lon,
        pollutant_id=(row.get("pollutant_id") or "").strip(),
        sub_index_min=_number(row.get("min_value")),
        sub_index_max=_number(row.get("max_value")),
        sub_index_avg=_number(row.get("avg_value")),
        observed_at=observed_at,
    )


class DataGovInClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, page_limit: int = DEFAULT_PAGE_LIMIT):
        if not api_key:
            raise ValueError("DataGovInClient requires a non-empty data.gov.in API key")
        self._api_key = api_key
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT})
        self._page_limit = page_limit

    def _get_page(self, offset: int, filters: dict[str, str] | None) -> dict:
        params = {"api-key": self._api_key, "format": "json", "limit": self._page_limit, "offset": offset}
        for key, value in (filters or {}).items():
            params[f"filters[{key}]"] = value
        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session.get(RESOURCE_URL, params=params, timeout=30)
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning("data.gov.in request failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, exc)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2.0 * attempt)
        raise RuntimeError(f"data.gov.in request failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def fetch(self, filters: dict[str, str] | None = None) -> list[AqiRecord]:
        """All usable records, deduplicated on (station, pollutant). Pages until
        the API's `total` is covered or a page comes back empty."""
        seen: dict[tuple[str, str], AqiRecord] = {}
        offset = 0
        for _ in range(MAX_PAGES):
            page = self._get_page(offset, filters)
            rows = page.get("records") or []
            for row in rows:
                record = parse_record(row)
                if record is not None:
                    seen[(record.station_name, record.pollutant_id)] = record
            offset += len(rows)
            try:
                total = int(page.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if not rows or offset >= total:
                break
        return list(seen.values())
