from datetime import datetime, timezone

from collections import namedtuple
from unittest import mock

from common.constants import Pollutant, SensorSourceName
from db.readings import hourly_readings, is_plausible, one_value_per_hour

T1 = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 30, 17, tzinfo=timezone.utc)
Row = namedtuple("Row", "observed_at value source")


def test_openaq_wins_an_hour_both_sources_hold_and_cpcb_fills_the_rest():
    rows = [
        (T1, 40.0, SensorSourceName.CPCB),
        (T1, 42.0, SensorSourceName.OPENAQ),
        (T2, 55.0, SensorSourceName.CPCB),
    ]
    assert one_value_per_hour(rows) == [(T1, 42.0), (T2, 55.0)]


def test_order_of_rows_does_not_matter():
    rows = [(T1, 42.0, SensorSourceName.OPENAQ), (T1, 40.0, SensorSourceName.CPCB)]
    assert one_value_per_hour(rows) == one_value_per_hour(list(reversed(rows))) == [(T1, 42.0)]


def test_instrument_faults_are_not_plausible():
    assert is_plausible(Pollutant.PM25, 0.0) and is_plausible(Pollutant.PM25, 1894.0)  # a real Diwali-night peak
    assert not is_plausible(Pollutant.PM25, -3.0)
    assert not is_plausible(Pollutant.PM25, 10000.0)
    assert is_plausible(Pollutant.NO2, 425.0) and not is_plausible(Pollutant.NO2, 1708.0)
    assert not is_plausible(Pollutant.PM25, float("nan"))


def test_hourly_readings_drops_a_fault_and_lets_the_other_source_fill_the_hour():
    session = mock.Mock()
    session.execute.return_value.all.return_value = [
        Row(T1, 10000.0, SensorSourceName.OPENAQ),
        Row(T1, 40.0, SensorSourceName.CPCB),
        Row(T2, -1.0, SensorSourceName.OPENAQ),
    ]
    assert hourly_readings(session, "openaq:1", Pollutant.PM25, T1) == [(T1, 40.0)]
