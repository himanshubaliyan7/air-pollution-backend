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
    assert sorted((r.pollutant.value, r.value) for r in rows) == [("no2", 21.5), ("pm25", 71.0), ("pm25", 72.0)]
    assert next(r for r in rows if r.pollutant == Pollutant.NO2).unit == "ppb"  # converted later, by the loader


def test_without_sensor_map_all_modelled_rows_are_kept():
    assert len(parse_day(CSV, "8118")) == 4


def test_day_url_layout():
    assert day_url("8118", date(2025, 11, 1)) == (
        "https://openaq-data-archive.s3.amazonaws.com/records/csv.gz/"
        "locationid=8118/year=2025/month=11/location-8118-20251101.csv.gz"
    )


def test_quarter_hour_periods_are_averaged_into_the_hour_containing_their_end():
    # Real layout from DPCC/IMD station 5627: 15-minute periods, end-stamped.
    header = '"location_id","sensors_id","location","datetime","lat","lon","parameter","units","value"\n'
    rows = [("00:15", 100.0), ("00:30", 110.0), ("00:45", 120.0), ("01:00", 130.0), ("01:15", 999.0)]
    csv_text = header + "".join(
        f'5627,12234678,"New Delhi - IMD","2025-12-10T{t}:00+05:30","28.6","77.2","pm25","µg/m³","{v}"\n' for t, v in rows
    )
    out = parse_day(csv_text, "5627", {Pollutant.PM25: 12234678})
    # 00:15..01:00 IST -> the hour 00:00-01:00 IST = 18:30Z the previous day -> floored 18:00Z
    assert (out[0].observed_at, out[0].value) == (datetime(2025, 12, 9, 18, tzinfo=timezone.utc), 115.0)
    assert out[0].source_record_id == "12234678:2025-12-09T18:30:00Z"
    assert (out[1].observed_at, out[1].value) == (datetime(2025, 12, 9, 19, tzinfo=timezone.utc), 999.0)
