from dataclasses import asdict

from fastapi import APIRouter, HTTPException

from api.schemas.regions import AqiCategoryOut, PollutantInfoOut, RegionBacktestOut, RegionOut
from common.constants import Pollutant
from common.regions import Region, get_region, load_regions
from models.exceedance import get_category_lower_bound

router = APIRouter(prefix="/regions", tags=["regions"])


_UNIT_DISPLAY = {"ug/m3": "\u00b5g/m\u00b3"}


def _pollutant_details(thresholds: dict) -> list[PollutantInfoOut]:
    out = []
    for pollutant in Pollutant:
        entry = thresholds["pollutants"].get(pollutant.value)
        if entry is None:
            continue
        unit = entry.get("unit", "")
        try:
            concentration = get_category_lower_bound(thresholds, pollutant, thresholds["health_threshold_category"])
        except ValueError:
            concentration = None
        out.append(
            PollutantInfoOut(
                id=pollutant.value,
                unit=_UNIT_DISPLAY.get(unit, unit),
                health_threshold_concentration=concentration,
                threshold_averaging=entry.get("averaging"),
            )
        )
    return out


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
        pollutant_details=_pollutant_details(thresholds),
        # Ordered best -> worst. `aqi_category` values in forecast responses are these ids.
        aqi_categories=[
            AqiCategoryOut(id=bp["category"], label=bp["category"].replace("_", " ").capitalize()) for bp in breakpoints
        ],
        health_threshold_category=thresholds["health_threshold_category"],
        backtest=RegionBacktestOut(**asdict(region.backtest)) if region.backtest else None,
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
