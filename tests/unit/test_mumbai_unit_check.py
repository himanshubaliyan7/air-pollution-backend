"""scripts/mumbai_unit_check.py: the pairing and the conclusion line, with canned data."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from common.constants import Pollutant
from common.regions import get_region
from common.subindex import inverter
from scripts.mumbai_unit_check import conclusion, pair_up, summarise

T0 = datetime(2026, 10, 7, 6, 30, tzinfo=timezone.utc)  # CPCB lastupdate: a whole IST hour = :30 UTC
THRESHOLDS = get_region("mumbai").thresholds()


def _data(pollutant, feed_id, label, scale, hours=14):
    """CPCB sub-indices of 80..., and OpenAQ rows whose value is CPCB's x `scale`."""
    invert, _ = inverter(THRESHOLDS["pollutants"][pollutant.value]["breakpoints"])
    snaps, rows = [], []
    for i in range(hours):
        sub = 60 + 5 * i
        cpcb, _ = invert(sub)
        at = T0 + timedelta(hours=i)
        snaps.append(SimpleNamespace(station_id="s1", pollutant_id=feed_id, sub_index_hourly=sub, source_updated_at=at))
        rows.append((at.replace(minute=0), cpcb * scale, label))
    return snaps, {("s1", pollutant): rows}


def _conclusion(pollutant, feed_id, label, scale):
    snaps, rows = _data(pollutant, feed_id, label, scale)
    lines = summarise(pollutant, pair_up(snaps, rows, THRESHOLDS)[pollutant])
    return lines[-1], lines


def test_no2_labelled_ppb_whose_values_are_ug_m3_is_called_out():
    last, lines = _conclusion(Pollutant.NO2, "NO2", "ppb", 1.0)
    assert last == "  conclusion: label says ppb but values are ug/m3"
    assert "raw / CPCB        median 1.000" in lines[1] and "converted / CPCB  median 1.882" in lines[2]


def test_a_true_ppb_label_is_right_when_the_converted_value_matches():
    last, _ = _conclusion(Pollutant.NO2, "NO2", "ppb", 1 / 1.882)
    assert last == "  conclusion: stored label is right"


def test_pm25_in_ug_m3_is_right_and_unrelated_values_match_neither():
    assert _conclusion(Pollutant.PM25, "PM2.5", "µg/m³", 1.0)[0] == "  conclusion: stored label is right"
    assert "match neither" in _conclusion(Pollutant.PM25, "PM2.5", "µg/m³", 3.0)[0]


def test_too_few_pairs_decides_nothing():
    snaps, rows = _data(Pollutant.NO2, "NO2", "ppb", 1.0, hours=4)
    assert summarise(Pollutant.NO2, pair_up(snaps, rows, THRESHOLDS)[Pollutant.NO2])[-1] == "  conclusion: not enough overlapping hours"
    assert conclusion([]) == "not enough overlapping hours"
