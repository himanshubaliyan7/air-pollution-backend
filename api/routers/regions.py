from fastapi import APIRouter, HTTPException

from api.schemas.regions import AqiCategoryOut, RegionOut
from common.constants import Pollutant
from common.regions import Region, get_region, load_regions

router = APIRouter(prefix="/regions", tags=["regions"])


def _describe(region: Region) -> RegionOut:
    thresholds = region.thresholds()
    breakpoints = thresholds["pollutants"][Pollutant.PM25.value]["breakpoints"]
    return RegionOut(
        id=region.id,
        name=region.name,
        country=region.country,
        timezone=region.timezone,
        bbox=list(region.bbox),
        aqi_standard=region.aqi_standard,
        pollutants=[p.value for p in Pollutant],
        # Ordered best -> worst. `aqi_category` values in forecast responses are these ids.
        aqi_categories=[
            AqiCategoryOut(id=bp["category"], label=bp["category"].replace("_", " ").capitalize()) for bp in breakpoints
        ],
        health_threshold_category=thresholds["health_threshold_category"],
    )


@router.get("", response_model=list[RegionOut])
def list_regions():
    return [_describe(r) for r in load_regions()]


@router.get("/{region_id}", response_model=RegionOut)
def get_region_detail(region_id: str):
    region = get_region(region_id)
    if region is None:
        raise HTTPException(status_code=404, detail="region not found")
    return _describe(region)
