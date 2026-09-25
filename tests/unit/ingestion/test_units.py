import pytest

from common.constants import Pollutant
from ingestion.units import CANONICAL_UNIT, to_canonical


def test_no2_ppb_converted_to_ug_m3():
    value, unit = to_canonical(Pollutant.NO2, 100.0, "ppb")
    assert unit == CANONICAL_UNIT
    assert value == pytest.approx(188.2, abs=0.1)  # 100 ppb is above CPCB's 181 ug/m3 "poor" line


@pytest.mark.parametrize("unit", ["µg/m³", "ug/m3", "μg/m³", " µg/m³ "])
def test_mass_units_pass_through(unit):
    assert to_canonical(Pollutant.PM25, 91.0, unit) == (91.0, CANONICAL_UNIT)


def test_unconvertible_unit_is_dropped_not_guessed():
    assert to_canonical(Pollutant.PM25, 1.0, "ppb") is None  # no ppb factor for particulates
    assert to_canonical(Pollutant.NO2, 1.0, "ppm") is None
