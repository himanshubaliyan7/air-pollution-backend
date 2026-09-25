"""OpenAQ's public AWS Open Data archive (s3://openaq-data-archive), for bulk
history. OpenAQ's terms direct bulk/historical pulls here rather than at the
API, and it costs no API quota, so a months-long backfill cannot trip the rate
limits that got the account suspended once (see docs/api_compliance.md).

Layout (verified 2026-09-25): one gzipped CSV per location per local day,
    records/csv.gz/locationid=<id>/year=<YYYY>/month=<MM>/location-<id>-<YYYYMMDD>.csv.gz
with columns location_id, sensors_id, location, datetime, lat, lon, parameter,
units, value. A missing day is an S3 error document (not gzip), not an empty file.

`datetime` is the END of each measurement period in local time. Hourly
stations: the archive's "2026-04-15T01:00:00+05:30" (19:30 UTC) is the value
the API returns for period.datetimeFrom 18:30 UTC (verified value-for-value,
station 8118). Some stations (DPCC/IMD) publish 15-minute periods instead,
while the API serves hourly averages; each reading belongs to the local hour
that contains its period end (00:45 -> the 00:00-01:00 hour), and the hour's
readings are averaged. observed_at = floor_hour(UTC start of that local hour),
matching OpenAQSource; source_record_id reuses the API's "<sensor>:<from UTC>".

Units are passed through as published (NO2 arrives in ppb, like the API).
"""

import csv
import gzip
import io
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

from common.constants import Pollutant
from ingestion.sources.base import SensorReading

logger = logging.getLogger(__name__)

ARCHIVE_URL = "https://openaq-data-archive.s3.amazonaws.com/records/csv.gz"
_PARAMETER_TO_POLLUTANT = {p.value: p for p in Pollutant}
MAX_ATTEMPTS = 3


def day_url(location_id: str, day: date) -> str:
    return (
        f"{ARCHIVE_URL}/locationid={location_id}/year={day:%Y}/month={day:%m}/"
        f"location-{location_id}-{day:%Y%m%d}.csv.gz"
    )


def _hour_start_utc(period_end: datetime) -> datetime:
    """UTC start of the local hour containing a period that ends at `period_end`."""
    hour_end = period_end.replace(minute=0, second=0, microsecond=0)
    if hour_end != period_end:
        hour_end += timedelta(hours=1)
    return (hour_end - timedelta(hours=1)).astimezone(timezone.utc)


def parse_day(raw_csv: str, location_id: str, sensor_ids: dict[Pollutant, int] | None = None) -> list[SensorReading]:
    """Hourly readings for our pollutants (sub-hourly periods averaged). With
    `sensor_ids`, keeps only the sensor the live pipeline uses for each
    pollutant, so history and live data share a sensor."""
    buckets: dict[tuple, list[float]] = defaultdict(list)
    units: dict[tuple, str] = {}
    for row in csv.DictReader(io.StringIO(raw_csv)):
        pollutant = _PARAMETER_TO_POLLUTANT.get((row.get("parameter") or "").strip())
        if pollutant is None:
            continue
        try:
            sensor_id = int(row["sensors_id"])
            value = float(row["value"])
            period_end = datetime.fromisoformat(row["datetime"])
        except (KeyError, TypeError, ValueError):
            continue
        if sensor_ids is not None and sensor_ids.get(pollutant) != sensor_id:
            continue
        key = (pollutant, sensor_id, _hour_start_utc(period_end))
        buckets[key].append(value)
        units[key] = (row.get("units") or "").strip()
    return [
        SensorReading(
            source_location_id=str(location_id),
            pollutant=pollutant,
            value=sum(values) / len(values),
            unit=units[(pollutant, sensor_id, start)],
            observed_at=start.replace(minute=0, second=0, microsecond=0),
            source_record_id=f"{sensor_id}:{start:%Y-%m-%dT%H:%M:%SZ}",
        )
        for (pollutant, sensor_id, start), values in sorted(buckets.items(), key=lambda kv: (kv[0][0].value, kv[0][1], kv[0][2]))
    ]


class OpenAQArchiveClient:
    def __init__(self, session: requests.Session | None = None):
        self._session = session or requests.Session()

    def fetch_day_csv(self, location_id: str, day: date) -> str | None:
        """Decompressed CSV text, or None when the archive has no file for that day."""
        url = day_url(location_id, day)
        last_error: Exception | None = None
        for _ in range(MAX_ATTEMPTS):
            try:
                resp = self._session.get(url, timeout=30)
                if resp.status_code in (403, 404):
                    return None  # S3 answers a missing public key with 403/404
                resp.raise_for_status()
                return gzip.decompress(resp.content).decode("utf-8")
            except (requests.RequestException, OSError) as exc:
                last_error = exc
        raise RuntimeError(f"OpenAQ archive fetch failed for {url}: {last_error}")
