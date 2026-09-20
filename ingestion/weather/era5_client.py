"""ERA5 (Copernicus Climate Data Store) ingestion via cdsapi.

Confirmed against CDS docs (cds.climate.copernicus.eu/how-to-api and the
reanalysis-era5-single-levels dataset page):
  - dataset id: "reanalysis-era5-single-levels"
  - client: cdsapi.Client() reads url/key from ~/.cdsapirc (written by the
    container entrypoint from CDS_API_URL/CDS_API_KEY env vars - see
    orchestration/Dockerfile), then client.retrieve(dataset, request, target)

IMPORTANT: the exact request dict keys for the CDS-Beta API (e.g. whether
date filtering uses year/month/day/time lists vs a "date" range string, and
"format" vs "data_format" for the output format key) should be verified
against the live "Show API request" panel on the dataset's CDS page before
first real use against a real API key - this could not be end-to-end tested
in this environment since no CDS credentials are configured. The request
shape below follows the long-standing documented pattern; if CDS rejects it,
compare the rejected request against the dataset page's generated example
and adjust the keys in _build_request() only - nothing downstream changes.

ERA5T handling: ERA5 has ~5 day latency; requests for the last ~5 days
return the preliminary ERA5T extension instead of final ERA5. CDS marks
which is which via an "expver" dimension in the returned NetCDF (expver
"0001" = final ERA5, "0005" = ERA5T) when a request spans the boundary.
When the response has no expver dimension (a request entirely inside or
entirely outside the ~5 day window), product type is inferred from how old
the timestamp is relative to now. Either way, downstream code tags every row
with its product_type so ingestion_dag's reconcile_era5_final task can later
overwrite ERA5T rows once the final ERA5 value is available (see
ingestion/loaders/weather_loader.py).

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
        readings: list[WeatherReading] = []
        with xr.open_dataset(path) as ds:
            has_expver = "expver" in ds.dims

            for time_val in ds["time"].values:
                observed_at = datetime.fromtimestamp(time_val.astype("datetime64[s]").astype(int), tz=timezone.utc)
                default_product = (
                    WeatherProductType.ERA5T
                    if (now - observed_at) < ERA5_FINAL_LATENCY
                    else WeatherProductType.ERA5
                )

                for lat in ds["latitude"].values:
                    for lon in ds["longitude"].values:
                        sel_kwargs = {"time": time_val, "latitude": lat, "longitude": lon}

                        expver_candidates = ds["expver"].values if has_expver else [None]
                        for expver in expver_candidates:
                            point = ds.sel(**sel_kwargs, expver=expver) if expver is not None else ds.sel(**sel_kwargs)
                            u = float(point["u10"].values)
                            v = float(point["v10"].values)
                            t = float(point["t2m"].values)
                            td = float(point["d2m"].values)
                            if math.isnan(u) or math.isnan(v) or math.isnan(t) or math.isnan(td):
                                continue  # this expver slot has no data for this time (the other one does)

                            product_type = default_product
                            if expver is not None:
                                product_type = (
                                    WeatherProductType.ERA5 if str(expver) == "0001" else WeatherProductType.ERA5T
                                )

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
