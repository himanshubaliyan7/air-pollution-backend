from datetime import datetime, timezone

from common.constants import SensorSourceName
from db.readings import one_value_per_hour

T1 = datetime(2026, 9, 30, 16, tzinfo=timezone.utc)
T2 = datetime(2026, 9, 30, 17, tzinfo=timezone.utc)


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
