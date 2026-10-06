"""Region metadata (config/regions.yaml): name, time zone, AQI standard and
which stations fall inside each region. Lets the API describe a region to a
frontend instead of the frontend hardcoding one."""

from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from common.config import get_settings, load_yaml_config


@dataclass(frozen=True)
class RegionBacktest:
    """Forecast accuracy measured for a region (optional `backtest` block in regions.yaml)."""

    period: str
    exact_grade_tomorrow: float  # share of days graded exactly right, 0-1
    exact_grade_day_5: float
    no_go_called_go_low: float  # share of No-go days wrongly called Go: low and high ends
    no_go_called_go_high: float


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    country: str
    timezone: str
    bbox: tuple[float, float, float, float]  # min_lon, min_lat, max_lon, max_lat
    aqi_standard: str
    thresholds_config: str
    backtest: RegionBacktest | None = None  # None until the region has been backtested

    def contains(self, lat: float, lon: float) -> bool:
        min_lon, min_lat, max_lon, max_lat = self.bbox
        return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat

    def local_date(self, dt: datetime) -> date:
        """Calendar day of an aware datetime in this region's time zone."""
        if dt.tzinfo is None:
            raise ValueError("local_date() requires a timezone-aware datetime")
        return dt.astimezone(ZoneInfo(self.timezone)).date()

    def thresholds(self) -> dict:
        return load_yaml_config(get_settings().thresholds_config_path.parent / self.thresholds_config)


def _parse_backtest(region_id: str, raw: dict | None) -> RegionBacktest | None:
    if not raw:
        return None
    backtest = RegionBacktest(
        period=str(raw["period"]),
        exact_grade_tomorrow=float(raw["exact_grade_tomorrow"]),
        exact_grade_day_5=float(raw["exact_grade_day_5"]),
        no_go_called_go_low=float(raw["no_go_called_go_low"]),
        no_go_called_go_high=float(raw["no_go_called_go_high"]),
    )
    for name in ("exact_grade_tomorrow", "exact_grade_day_5"):
        if not 0.0 <= getattr(backtest, name) <= 1.0:
            raise ValueError(f"region {region_id}: backtest.{name} must be between 0 and 1")
    return backtest


@lru_cache
def load_regions() -> tuple[Region, ...]:
    path = get_settings().thresholds_config_path.parent / "regions.yaml"
    regions = tuple(
        Region(
            id=r["id"], name=r["name"], country=r["country"], timezone=r["timezone"],
            bbox=tuple(float(x) for x in r["bbox"]), aqi_standard=r["aqi_standard"],
            thresholds_config=r["thresholds_config"], backtest=_parse_backtest(r["id"], r.get("backtest")),
        )
        for r in load_yaml_config(path)["regions"]
    )
    for r in regions:
        ZoneInfo(r.timezone)  # fail at load, not at first request, on a bad zone name
    return regions


def region_for_point(lat: float, lon: float) -> Region | None:
    return next((r for r in load_regions() if r.contains(lat, lon)), None)


def get_region(region_id: str) -> Region | None:
    return next((r for r in load_regions() if r.id == region_id), None)
