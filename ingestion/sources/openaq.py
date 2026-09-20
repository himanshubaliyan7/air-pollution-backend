"""OpenAQ v3 implementation of SensorSource.

Confirmed against the live OpenAQ v3 OpenAPI spec (api.openaq.org/openapi.json):
  - auth: header "X-API-Key"
  - GET /v3/locations?bbox=minLon,minLat,maxLon,maxLat&iso={country}&limit&page
    -> {"meta": {..., "found": N}, "results": [{id, name, locality, coordinates: {latitude, longitude}, sensors: [...], ...}]}
  - GET /v3/locations/{id} -> same Location shape, used to resolve each
    location's sensors (id + parameter.name) before fetching measurements.
  - GET /v3/sensors/{sensors_id}/hours?datetime_from&datetime_to&limit&page
    -> {"meta": {..., "found": N}, "results": [{value, parameter: {units, name}, period: {datetimeFrom: {utc, local}, ...}}]}

Station/location IDs never need to be re-discovered on every ingestion run -
list_stations() is called only at bootstrap / periodic re-sync
(scripts/seed_stations.py), and fetch_readings() resolves each location's
sensor ids once per call and reuses them for every pollutant requested, to
stay within OpenAQ's per-key rate limits.
"""

import logging
import time
from datetime import datetime, timezone

import requests

from common.constants import Pollutant, SensorSourceName
from ingestion.sources.base import SensorReading, SensorSource, StationMetadata

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openaq.org/v3"
PAGE_LIMIT = 1000
MAX_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2.0

# OpenAQ parameter.name values line up directly with our Pollutant enum values.
_PARAMETER_NAME_TO_POLLUTANT = {p.value: p for p in Pollutant}


class OpenAQSource(SensorSource):
    def __init__(self, api_key: str, session: requests.Session | None = None):
        if not api_key:
            raise ValueError("OpenAQSource requires a non-empty OpenAQ API key")
        self._session = session or requests.Session()
        self._session.headers.update({"X-API-Key": api_key})
        # Sensor ids for a location never change within a source instance's
        # lifetime - caching avoids re-resolving them on every fetch_readings
        # call. Matters a lot for a multi-chunk backfill (scripts/backfill_history.py
        # calls fetch_readings once per date chunk for the same station list) -
        # without this, a 180-day backfill in weekly chunks re-issues a
        # /locations/{id} GET per station per chunk, which multiplies real
        # request volume ~25x and reliably burns through OpenAQ's rate limit.
        self._sensor_cache: dict[str, dict[Pollutant, int]] = {}

    @property
    def source_name(self) -> SensorSourceName:
        return SensorSourceName.OPENAQ

    def _get(self, path: str, params: dict) -> dict:
        """Retries on 429 (rate limit) and on 5xx (transient upstream
        errors) - a real backfill run hit a genuine 500 from OpenAQ on one
        sensor's /hours endpoint and, before this fix, that crashed the
        entire multi-station fetch instead of retrying/skipping just that
        one sensor (see fetch_readings's per-sensor try/except)."""
        url = f"{BASE_URL}{path}"
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(1, MAX_RETRIES + 1):
            resp = self._session.get(url, params=params, timeout=30)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", backoff))
                logger.warning(
                    "OpenAQ rate limited on %s (attempt %d/%d), backing off %.1fs",
                    path, attempt, MAX_RETRIES, retry_after,
                )
                time.sleep(retry_after)
                backoff *= 2
                continue
            if resp.status_code >= 500:
                logger.warning(
                    "OpenAQ server error %d on %s (attempt %d/%d), backing off %.1fs",
                    resp.status_code, path, attempt, MAX_RETRIES, backoff,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"OpenAQ request to {path} failed after {MAX_RETRIES} retries (rate limited or server error)")

    def _paginate(self, path: str, params: dict):
        page = 1
        while True:
            page_params = {**params, "limit": PAGE_LIMIT, "page": page}
            data = self._get(path, page_params)
            results = data.get("results", [])
            yield from results
            found = data.get("meta", {}).get("found", 0)
            if isinstance(found, str):  # OpenAQ returns ">1000" style strings for large counts
                fetched_so_far = page * PAGE_LIMIT
                if len(results) < PAGE_LIMIT:
                    break
            elif page * PAGE_LIMIT >= found:
                break
            if len(results) == 0:
                break
            page += 1

    def list_stations(self, *, bbox: tuple[float, float, float, float], country: str) -> list[StationMetadata]:
        min_lon, min_lat, max_lon, max_lat = bbox
        params = {
            "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
            "iso": country,
        }
        stations = []
        for loc in self._paginate("/locations", params):
            coords = loc.get("coordinates") or {}
            if coords.get("latitude") is None or coords.get("longitude") is None:
                continue
            stations.append(
                StationMetadata(
                    source_location_id=str(loc["id"]),
                    name=loc.get("name") or f"location-{loc['id']}",
                    lat=coords["latitude"],
                    lon=coords["longitude"],
                    city=loc.get("locality") or "Delhi",
                    state="Delhi",
                )
            )
        return stations

    def _resolve_sensor_ids(self, location_id: str, pollutants: list[Pollutant]) -> dict[Pollutant, int]:
        cached = self._sensor_cache.get(location_id)
        if cached is not None:
            return {p: sid for p, sid in cached.items() if p in pollutants}

        data = self._get(f"/locations/{location_id}", {})
        location = data.get("results", [data])[0] if "results" in data else data
        sensor_ids: dict[Pollutant, int] = {}
        for sensor in location.get("sensors", []):
            param_name = (sensor.get("parameter") or {}).get("name")
            pollutant = _PARAMETER_NAME_TO_POLLUTANT.get(param_name)
            if pollutant is not None:
                sensor_ids[pollutant] = sensor["id"]
        self._sensor_cache[location_id] = sensor_ids
        return {p: sid for p, sid in sensor_ids.items() if p in pollutants}

    def fetch_readings(
        self,
        *,
        source_location_ids: list[str],
        pollutants: list[Pollutant],
        start: datetime,
        end: datetime,
    ) -> list[SensorReading]:
        readings: list[SensorReading] = []
        for location_id in source_location_ids:
            try:
                sensor_ids = self._resolve_sensor_ids(location_id, pollutants)
            except requests.HTTPError as exc:
                logger.warning("Could not resolve sensors for location %s: %s", location_id, exc)
                continue

            for pollutant, sensor_id in sensor_ids.items():
                params = {
                    "datetime_from": start.astimezone(timezone.utc).isoformat(),
                    "datetime_to": end.astimezone(timezone.utc).isoformat(),
                }
                try:
                    for row in self._paginate(f"/sensors/{sensor_id}/hours", params):
                        value = row.get("value")
                        if value is None:
                            continue
                        period = row.get("period") or {}
                        datetime_from = (period.get("datetimeFrom") or {}).get("utc")
                        if not datetime_from:
                            continue
                        observed_at = datetime.fromisoformat(datetime_from.replace("Z", "+00:00"))
                        # Confirmed against real Delhi NCR CPCB data: OpenAQ's
                        # period.datetimeFrom for this feed is consistently
                        # stamped at :30 past the hour (438k/438k readings in
                        # a live backfill), not :00 - unlike ERA5, which is
                        # cleanly on the hour. Every downstream hourly
                        # bucket (lag/rolling features, the daily exceedance
                        # aggregation, join with weather) assumes a clean
                        # :00 grid, so floor here at the ingestion boundary
                        # rather than letting the offset propagate and
                        # silently break every hour-alignment downstream (as
                        # it did in practice: this caused a real backfill's
                        # entire training run to see zero usable rows).
                        observed_at = observed_at.replace(minute=0, second=0, microsecond=0)
                        unit = (row.get("parameter") or {}).get("units", "ug/m3")
                        readings.append(
                            SensorReading(
                                source_location_id=location_id,
                                pollutant=pollutant,
                                value=float(value),
                                unit=unit,
                                observed_at=observed_at,
                                source_record_id=f"{sensor_id}:{datetime_from}",
                            )
                        )
                except (requests.HTTPError, RuntimeError) as exc:
                    # A persistently-failing sensor (repeated 5xx, etc.) must
                    # not take down the whole multi-station fetch - this
                    # crashed a real backfill run before this fix. Whatever
                    # hours this sensor contributed are simply missing for
                    # this window; a later re-run (idempotent upsert) picks
                    # them up if the upstream issue clears.
                    logger.warning(
                        "Could not fetch hours for sensor %s (location %s, %s): %s",
                        sensor_id, location_id, pollutant.value, exc,
                    )
                    continue
        return readings
