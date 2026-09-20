"""ERA5 (Copernicus Climate Data Store) ingestion via cdsapi.

Confirmed against CDS docs (cds.climate.copernicus.eu/how-to-api and the
reanalysis-era5-single-levels dataset page):
  - dataset id: "reanalysis-era5-single-levels"
  - client: cdsapi.Client() reads url/key from ~/.cdsapirc (written by the
    container entrypoint from CDS_API_URL/CDS_API_KEY env vars - see
    orchestration/Dockerfile), then client.retrieve(dataset, request, target)

Verified end-to-end against a real CDS account on 2026-09-20: the
request shape in _build_request() (year/month/day/time lists, "format":
"netcdf") is accepted as-is by the live API - no request-shape changes were
needed. The response NetCDF's actual structure (confirmed by inspection,
and notably different from what CDS's own dataset-page documentation implies)
drove _parse()'s field names - see that method's docstring for specifics
(the time dimension is "valid_time", not "time").

ERA5T handling: ERA5 has ~5 day latency (in practice can run a bit longer -
a live request for data 10 days old still came back tagged ERA5T); requests
for recent data return the preliminary ERA5T extension instead of final
ERA5. CDS marks which is which via a per-timestep "expver" coordinate in the
returned NetCDF ("0001" = final ERA5, "0005" = ERA5T) - always present in
observed responses, one value per hour, not a separate dimension to iterate.
Downstream code tags every row with its product_type so ingestion_dag's
reconcile_era5_final task can later overwrite ERA5T rows once the final
ERA5 value is available (see ingestion/loaders/weather_loader.py).

Relative humidity is not a direct ERA5 variable - it's derived from 2m
temperature and 2m dewpoint temperature via the Magnus-Tetens approximation.
"""

import logging
import math
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cdsapi
import xarray as xr

from common.constants import WeatherProductType
from ingestion.weather.grid import DELHI_NCR_AREA, grid_cell_id

logger = logging.getLogger(__name__)

DATASET = "reanalysis-era5-single-levels"
ERA5_FINAL_LATENCY = timedelta(days=6)  # conservative; reconcile task re-checks anyway


@dataclass(frozen=True)
class WeatherReading:
    grid_cell_id: str
    lat: float
    lon: float
    observed_at: datetime
    u_wind: float
    v_wind: float
    wind_speed: float
    wind_direction: float
    relative_humidity: float
    product_type: WeatherProductType


def _relative_humidity_from_dewpoint(temp_k: float, dewpoint_k: float) -> float:
    t_c = temp_k - 273.15
    td_c = dewpoint_k - 273.15
    numerator = math.exp((17.625 * td_c) / (243.04 + td_c))
    denominator = math.exp((17.625 * t_c) / (243.04 + t_c))
    return max(0.0, min(100.0, 100.0 * numerator / denominator))


def _wind_speed_direction(u: float, v: float) -> tuple[float, float]:
    speed = math.sqrt(u**2 + v**2)
    # Meteorological convention: direction wind is blowing FROM, 0=N, 90=E.
    direction = (math.degrees(math.atan2(-u, -v))) % 360
    return speed, direction


def _build_request(start: datetime, end: datetime, area: tuple[float, float, float, float]) -> dict:
    days = []
    cur = start
    while cur.date() <= end.date():
        days.append(cur)
        cur += timedelta(days=1)
    years = sorted({d.strftime("%Y") for d in days})
    months = sorted({d.strftime("%m") for d in days})
    day_nums = sorted({d.strftime("%d") for d in days})
    return {
        "product_type": ["reanalysis"],
        "variable": [
            "10m_u_component_of_wind",
            "10m_v_component_of_wind",
            "2m_temperature",
            "2m_dewpoint_temperature",
        ],
        "year": years,
        "month": months,
        "day": day_nums,
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": list(area),
        "format": "netcdf",
    }


class ERA5Client:
    def __init__(self, client: cdsapi.Client | None = None):
        self._client = client or cdsapi.Client()

    def fetch(
        self,
        start: datetime,
        end: datetime,
        area: tuple[float, float, float, float] = DELHI_NCR_AREA,
    ) -> list[WeatherReading]:
        request = _build_request(start, end, area)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "era5_delhi_ncr.nc"
            logger.info("Submitting CDS request for %s..%s", start, end)
            self._client.retrieve(DATASET, request, str(target))
            return self._parse(target, now=datetime.now(timezone.utc))

    def _parse(self, path: Path, now: datetime) -> list[WeatherReading]:
        """Verified against a live CDS response (2026-09-20): the current
        CDS-Beta/cfgrib-converted NetCDF names the time dimension
        "valid_time" (not "time"), and "expver" is a per-timestep string
        coordinate indexed by valid_time - each hour is tagged with exactly
        one expver value ("0001"=final ERA5, "0005"=ERA5T), not a separate
        dimension to select/loop over as earlier assumed. "number" (ensemble
        member) is a scalar coordinate, irrelevant for the deterministic
        reanalysis stream requested here.
        """
        readings: list[WeatherReading] = []
        with xr.open_dataset(path) as ds:
            has_expver = "expver" in ds.coords

            for i, time_val in enumerate(ds["valid_time"].values):
                observed_at = datetime.fromtimestamp(time_val.astype("datetime64[s]").astype(int), tz=timezone.utc)

                if has_expver:
                    expver_val = str(ds["expver"].values[i]).strip()
                    product_type = WeatherProductType.ERA5 if expver_val == "0001" else WeatherProductType.ERA5T
                else:
                    product_type = (
                        WeatherProductType.ERA5T
                        if (now - observed_at) < ERA5_FINAL_LATENCY
                        else WeatherProductType.ERA5
                    )

                for lat in ds["latitude"].values:
                    for lon in ds["longitude"].values:
                        point = ds.sel(valid_time=time_val, latitude=lat, longitude=lon)
                        u = float(point["u10"].values)
                        v = float(point["v10"].values)
                        t = float(point["t2m"].values)
                        td = float(point["d2m"].values)
                        if math.isnan(u) or math.isnan(v) or math.isnan(t) or math.isnan(td):
                            continue

                        speed, direction = _wind_speed_direction(u, v)
                        readings.append(
                            WeatherReading(
                                grid_cell_id=grid_cell_id(float(lat), float(lon)),
                                lat=float(lat),
                                lon=float(lon),
                                observed_at=observed_at,
                                u_wind=u,
                                v_wind=v,
                                wind_speed=speed,
                                wind_direction=direction,
                                relative_humidity=_relative_humidity_from_dewpoint(t, td),
                                product_type=product_type,
                            )
                        )
        return readings
