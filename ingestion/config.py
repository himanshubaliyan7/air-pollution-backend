from common.constants import SensorSourceName
from common.regions import get_region, load_regions
from ingestion.sources.base import SensorSource
from ingestion.sources.openaq import OpenAQSource

# Only OpenAQ is active for Delhi NCR - see ingestion/sources/airnow_stub.py
# for why AirNow isn't (and can't be) wired up for this region.
SENSOR_SOURCE_REGISTRY: dict[SensorSourceName, type[SensorSource]] = {
    SensorSourceName.OPENAQ: OpenAQSource,
}

ACTIVE_SOURCE = SensorSourceName.OPENAQ

def region_search_area(region_id: str) -> tuple[tuple[float, float, float, float], str]:
    """(bbox, country ISO) for station discovery in a region, from config/regions.yaml.
    bbox is (min_lon, min_lat, max_lon, max_lat), what OpenAQ's /v3/locations?bbox=
    parameter expects."""
    region = get_region(region_id)
    if region is None:
        known = ", ".join(r.id for r in load_regions())
        raise ValueError(f"unknown region {region_id!r} (known: {known})")
    return region.bbox, region.country


# Aliases kept for existing callers; the values come from config/regions.yaml
# (matches ingestion/weather/grid.py's DELHI_NCR_AREA).
DELHI_NCR_BBOX, DELHI_NCR_COUNTRY_ISO = region_search_area("delhi-ncr")


def make_station_id(source: SensorSourceName, source_location_id: str) -> str:
    """Deterministic internal station_id, so the loader never needs a DB
    round-trip to resolve a reading's station before inserting it."""
    return f"{source.value}:{source_location_id}"
