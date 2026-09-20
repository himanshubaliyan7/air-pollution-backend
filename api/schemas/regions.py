from pydantic import BaseModel


class AqiCategoryOut(BaseModel):
    id: str
    label: str


class RegionOut(BaseModel):
    id: str
    name: str
    country: str  # ISO 3166-1 alpha-2
    timezone: str  # IANA zone; local calendar days and display times use this
    bbox: list[float]  # min_lon, min_lat, max_lon, max_lat
    aqi_standard: str
    pollutants: list[str]
    aqi_categories: list[AqiCategoryOut]  # ordered best -> worst
    health_threshold_category: str  # category at/above which outdoor practice is not recommended
