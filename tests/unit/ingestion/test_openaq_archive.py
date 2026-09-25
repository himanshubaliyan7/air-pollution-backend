from datetime import date, datetime, timezone

from common.constants import Pollutant
from ingestion.sources.openaq_archive import day_url, parse_day

# Real rows from the archive for location 8118 on 2026-04-15 (plus a second
# PM2.5 sensor and a pollutant we do not model).
CSV = '''"location_id","sensors_id","location","datetime","lat","lon","parameter","units","value"
8118,23534,"New Delhi-8118","2026-04-15T01:00:00+05:30","28.63576","77.22445","pm25","µg/m³","71.0"
8118,23534,"New Delhi-8118","2026-04-15T02:00:00+05:30","28.63576","77.22445","pm25","µg/m³","72.0"
8118,99999,"New Delhi-8118","2026-04-15T01:00:00+05:30","28.63576","77.22445","pm25","µg/m³","500.0"
8118,23535,"New Delhi-8118","2026-04-15T01:00:00+05:30","28.63576","77.22445","no2","ppb","21.5"
8118,23536,"New Delhi-8118","2026-04-15T01:00:00+05:30","28.63576","77.22445","pm10","µg/m³","140.0"
'''


def test_period_end_maps_to_the_api_period_start_hour():
    # Verified against API-ingested rows: archive 01:00+05:30 == API datetimeFrom 18:30Z.
    first = parse_day(CSV, "8118", {Pollutant.PM25: 23534})[0]
    assert first.observed_at == datetime(2026, 4, 14, 18, tzinfo=timezone.utc)
    assert first.value == 71.0
    assert first.source_record_id == "23534:2026-04-14T18:30:00Z"


def test_keeps_only_the_live_pipelines_sensor_and_modelled_pollutants():
    rows = parse_day(CSV, "8118", {Pollutant.PM25: 23534, Pollutant.NO2: 23535})
    assert [(r.pollutant, r.value) for r in rows] == [
        (Pollutant.PM25, 71.0), (Pollutant.PM25, 72.0), (Pollutant.NO2, 21.5),
    ]
    assert rows[-1].unit == "ppb"  # passed through as published


def test_without_sensor_map_all_modelled_rows_are_kept():
    assert len(parse_day(CSV, "8118")) == 4


def test_day_url_layout():
    assert day_url("8118", date(2025, 11, 1)) == (
        "https://openaq-data-archive.s3.amazonaws.com/records/csv.gz/"
        "locationid=8118/year=2025/month=11/location-8118-20251101.csv.gz"
    )
