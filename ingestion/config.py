from common.constants import SensorSourceName
from ingestion.sources.base import SensorSource
from ingestion.sources.openaq import OpenAQSource

# Only OpenAQ is active for Delhi NCR - see ingestion/sources/airnow_stub.py
# for why AirNow isn't (and can't be) wired up for this region.
SENSOR_SOURCE_REGISTRY: dict[SensorSourceName, type[SensorSource]] = {
    SensorSourceName.OPENAQ: OpenAQSource,
}

ACTIVE_SOURCE = SensorSourceName.OPENAQ

# (min_lon, min_lat, max_lon, max_lat) - matches ingestion/weather/grid.py's
# DELHI_NCR_AREA but expressed as a bbox tuple, which is what OpenAQ's
# /v3/locations?bbox= parameter expects.
DELHI_NCR_BBOX = (76.6, 28.2, 77.6, 29.0)
DELHI_NCR_COUNTRY_ISO = "IN"


def make_station_id(source: SensorSourceName, source_location_id: str) -> str:
    """Deterministic internal station_id, so the loader never needs a DB
    round-trip to resolve a reading's station before inserting it."""
    return f"{source.value}:{source_location_id}"
