"""Two regions for tests: config/regions.yaml's own, plus Mumbai when the file lacks it
(it is added by the ingestion branch; the values mirror it)."""

import pytest

from common import regions as regions_module
from common.regions import Region

MUMBAI = Region(
    id="mumbai", name="Mumbai", country="IN", timezone="Asia/Kolkata", bbox=(72.70, 18.80, 73.35, 19.55),
    aqi_standard="CPCB National AQI", thresholds_config="thresholds_cpcb.yaml",
)
MUMBAI_LAT, MUMBAI_LON = 19.07, 72.87  # inside the bbox
DELHI_LAT, DELHI_LON = 28.6, 77.2


@pytest.fixture()
def two_regions(monkeypatch):
    real = regions_module.load_regions()
    both = real if any(r.id == "mumbai" for r in real) else real + (MUMBAI,)
    monkeypatch.setattr(regions_module, "load_regions", lambda: both)
    from orchestration.plugins.common import tasks

    monkeypatch.setattr(tasks, "load_regions", lambda: both)
    return both
