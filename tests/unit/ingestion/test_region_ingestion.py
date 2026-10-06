"""Ingestion per region: bbox lookup, station defaults, the backtest block, and the
NO2 "ppb" unit rule that is only verified for Delhi NCR."""

import logging
from datetime import datetime, timezone

import pytest

from common.constants import Pollutant
from common.regions import get_region, load_regions
from ingestion.config import DELHI_NCR_BBOX, DELHI_NCR_COUNTRY_ISO, region_search_area
from ingestion.sources.openaq import MISLABEL_VERIFIED_REGIONS, OpenAQSource, declared_unit
from ingestion.sources.openaq_archive import parse_day
from ingestion.units import CANONICAL_UNIT
from tests.unit.ingestion.test_openaq import FakeResponse, FakeSession

DELHI = (28.6, 77.2)
MUMBAI = (19.07, 72.88)


def test_region_search_area_reads_the_yaml_and_delhi_aliases_are_unchanged():
    assert region_search_area("mumbai") == ((72.70, 18.80, 73.35, 19.55), "IN")
    assert region_search_area("delhi-ncr") == ((76.6, 28.2, 77.6, 29.0), "IN")
    assert (DELHI_NCR_BBOX, DELHI_NCR_COUNTRY_ISO) == ((76.6, 28.2, 77.6, 29.0), "IN")


def test_unknown_region_names_the_known_ones():
    with pytest.raises(ValueError, match="delhi-ncr, mumbai"):
        region_search_area("atlantis")


def test_backtest_block_is_delhis_and_mumbai_has_none():
    delhi, mumbai = get_region("delhi-ncr"), get_region("mumbai")
    assert delhi.backtest is not None and mumbai.backtest is None
    assert (delhi.backtest.period, delhi.backtest.exact_grade_tomorrow, delhi.backtest.exact_grade_day_5) == (
        "2025-10-15 to 2025-11-30", 0.61, 0.52,
    )
    assert (delhi.backtest.no_go_called_go_low, delhi.backtest.no_go_called_go_high) == (0.02, 0.05)
    assert len(load_regions()) == 2


def test_list_stations_leaves_city_and_state_empty_unless_told():
    responses = {
        "/locations": FakeResponse(
            {
                "meta": {"found": 2},
                "results": [
                    {"id": 1, "name": "A", "locality": "Thane", "coordinates": {"latitude": 19.2, "longitude": 72.97}},
                    {"id": 2, "name": "B", "coordinates": {"latitude": 19.0, "longitude": 72.8}},
                ],
            }
        )
    }
    source = OpenAQSource(api_key="k", session=FakeSession(responses))
    a, b = source.list_stations(bbox=(72.7, 18.8, 73.35, 19.55), country="IN")
    assert (a.city, a.state) == ("Thane", "")  # OpenAQ's locality is real data; no invented state
    assert (b.city, b.state) == ("", "")
    d = source.list_stations(bbox=(72.7, 18.8, 73.35, 19.55), country="IN", default_city="Delhi", state="Delhi")[1]
    assert (d.city, d.state) == ("Delhi", "Delhi")  # what Delhi NCR discovery has always stored


def test_ppb_no2_rule_applies_only_in_verified_regions():
    assert MISLABEL_VERIFIED_REGIONS == {"delhi-ncr"}
    assert declared_unit(Pollutant.NO2, "ppb", *DELHI) == CANONICAL_UNIT
    assert declared_unit(Pollutant.NO2, "ppb", *MUMBAI) is None  # unverified: skipped, not converted
    assert declared_unit(Pollutant.NO2, "ppb") is None  # no coordinates: unverified
    assert declared_unit(Pollutant.NO2, "ppb", 12.97, 77.59) is None  # outside every region
    assert declared_unit(Pollutant.NO2, "ug/m3", *MUMBAI) == "ug/m3"  # a correct label is untouched
    assert declared_unit(Pollutant.PM25, "µg/m³", *MUMBAI) == "µg/m³"


def _fetch(lat_lon, caplog):
    responses = {
        "/locations/7": FakeResponse(
            {
                "id": 7,
                "coordinates": {"latitude": lat_lon[0], "longitude": lat_lon[1]},
                "sensors": [
                    {"id": 71, "parameter": {"name": "no2", "units": "ppb"}},
                    {"id": 72, "parameter": {"name": "pm25", "units": "µg/m³"}},
                ],
            }
        ),
        "/sensors/71/hours": FakeResponse(
            {"meta": {"found": 2}, "results": [
                {"value": 20.0, "parameter": {"units": "ppb"}, "period": {"datetimeFrom": {"utc": "2026-10-01T00:30:00Z"}}},
                {"value": 22.0, "parameter": {"units": "ppb"}, "period": {"datetimeFrom": {"utc": "2026-10-01T01:30:00Z"}}},
            ]}
        ),
        "/sensors/72/hours": FakeResponse(
            {"meta": {"found": 1}, "results": [
                {"value": 60.0, "parameter": {"units": "µg/m³"}, "period": {"datetimeFrom": {"utc": "2026-10-01T00:30:00Z"}}},
            ]}
        ),
    }
    source = OpenAQSource(api_key="k", session=FakeSession(responses))
    with caplog.at_level(logging.WARNING, logger="ingestion.sources.openaq"):
        return source.fetch_readings(
            source_location_ids=["7"], pollutants=[Pollutant.NO2, Pollutant.PM25],
            start=datetime(2026, 10, 1, tzinfo=timezone.utc), end=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )


def test_mumbai_ppb_no2_is_skipped_with_a_counted_warning_and_pm25_still_flows(caplog):
    readings = _fetch(MUMBAI, caplog)
    assert [(r.pollutant, r.value) for r in readings] == [(Pollutant.PM25, 60.0)]  # NO2 reads as missing, not as 20/22
    assert any("Skipped 2 no2 hours at location 7" in m for m in caplog.messages)


def test_delhi_ppb_no2_is_still_relabelled_and_not_counted_as_skipped(caplog):
    readings = _fetch(DELHI, caplog)
    assert sorted((r.pollutant.value, r.value, r.unit) for r in readings) == [
        ("no2", 20.0, CANONICAL_UNIT), ("no2", 22.0, CANONICAL_UNIT), ("pm25", 60.0, CANONICAL_UNIT),
    ]
    assert not any("Skipped" in m for m in caplog.messages)


def _archive_csv(lat, lon):
    return (
        '"location_id","sensors_id","location","datetime","lat","lon","parameter","units","value"\n'
        f'9,91,"X","2026-04-15T01:00:00+05:30","{lat}","{lon}","no2","ppb","21.5"\n'
        f'9,92,"X","2026-04-15T01:00:00+05:30","{lat}","{lon}","pm25","µg/m³","70.0"\n'
    )


def test_archive_applies_the_same_rule_from_the_rows_coordinates(caplog):
    with caplog.at_level(logging.WARNING, logger="ingestion.sources.openaq_archive"):
        mumbai = parse_day(_archive_csv(*MUMBAI), "9")
    assert [r.pollutant for r in mumbai] == [Pollutant.PM25]
    assert any("skipped 1 no2 hours" in m for m in caplog.messages)
    delhi = parse_day(_archive_csv(*DELHI), "9")
    assert sorted(r.pollutant.value for r in delhi) == ["no2", "pm25"]
    assert next(r for r in delhi if r.pollutant is Pollutant.NO2).unit == CANONICAL_UNIT
