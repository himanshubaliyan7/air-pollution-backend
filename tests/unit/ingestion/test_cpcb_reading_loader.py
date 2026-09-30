from datetime import datetime, timezone

from common.constants import Pollutant, SensorSourceName
from common.regions import get_region
from ingestion.loaders.cpcb_reading_loader import reading_rows
from ingestion.units import CANONICAL_UNIT

THRESHOLDS = get_region("delhi-ncr").thresholds()
LAST_UPDATE = datetime(2026, 9, 30, 17, 30, tzinfo=timezone.utc)  # 23:00 IST


def test_hourly_sub_index_becomes_a_reading_on_openaqs_hour_stamp():
    """lastupdate - 1.5 h is where OpenAQ stamps the same measurement (study, 2026-10-01)."""
    [row] = reading_rows([("openaq:235", "PM2.5", LAST_UPDATE, 150)], THRESHOLDS)
    assert row["observed_at"] == datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
    assert (row["pollutant"], round(row["value"], 2), row["unit"]) == (Pollutant.PM25, 75.35, CANONICAL_UNIT)
    assert row["source"] == SensorSourceName.CPCB
    assert row["source_record_id"] == "cpcb:2026-09-30T17:30Z:150"


def test_other_pollutants_and_missing_values_are_skipped():
    items = [
        ("openaq:235", "PM10", LAST_UPDATE, 120),
        ("openaq:235", "NO2", LAST_UPDATE, None),
        ("openaq:235", "NO2", LAST_UPDATE, 100),
    ]
    [row] = reading_rows(items, THRESHOLDS)
    assert (row["pollutant"], row["value"]) == (Pollutant.NO2, 80.0)


def test_capped_sub_index_is_stored_as_a_marked_floor():
    [row] = reading_rows([("openaq:235", "PM2.5", LAST_UPDATE, 500)], THRESHOLDS)
    assert row["value"] == 380.0
    assert row["source_record_id"].endswith(":capped")


def test_one_row_per_station_pollutant_hour():
    items = [("openaq:235", "PM2.5", LAST_UPDATE, 150), ("openaq:235", "PM2.5", LAST_UPDATE, 151)]
    assert len(reading_rows(items, THRESHOLDS)) == 1
