"""Delhi NCR ERA5 grid definition.

ERA5 single-levels is on a ~0.25 degree lat/lon grid. This bounding box
covers Delhi plus the immediate NCR satellite cities (Gurugram, Noida,
Ghaziabad, Faridabad) with a small margin, giving roughly a 2x3 cell
sub-grid at 0.25 degree resolution.
"""

import math
from dataclasses import dataclass

# (north, west, south, east) - the order cdsapi's "area" request key expects.
DELHI_NCR_AREA = (29.0, 76.6, 28.2, 77.6)


@dataclass(frozen=True)
class GridCell:
    grid_cell_id: str
    lat: float
    lon: float


def grid_cell_id(lat: float, lon: float) -> str:
    return f"{lat:.2f}_{lon:.2f}"


ERA5_GRID_RESOLUTION_DEG = 0.25


def nearest_grid_cell_id(station_lat: float, station_lon: float) -> str:
    """ERA5 single-levels is on a regular 0.25 degree grid aligned to exact
    multiples of 0.25 (e.g. 28.00, 28.25, 28.50, ...), so rounding a
    station's coordinates to the nearest 0.25 reliably lands on the same
    grid point era5_client.py produced ids for."""
    lat = round(station_lat / ERA5_GRID_RESOLUTION_DEG) * ERA5_GRID_RESOLUTION_DEG
    lon = round(station_lon / ERA5_GRID_RESOLUTION_DEG) * ERA5_GRID_RESOLUTION_DEG
    return grid_cell_id(lat, lon)


def delhi_ncr_grid_points(area: tuple[float, float, float, float] = DELHI_NCR_AREA) -> list[tuple[float, float]]:
    """Enumerates the same 0.25-degree grid points ERA5 actually returns for
    DELHI_NCR_AREA (verified against a live response: latitudes
    [29.0, 28.75, 28.5, 28.25] x longitudes [76.75, 77.0, 77.25, 77.5] = 16
    cells - CDS snaps the requested area's bounds inward to the nearest
    interior 0.25 multiples, not outward), so open_meteo_client.py requests
    forecasts for identically-named grid cells and the two sources are
    directly join-compatible via grid_cell_id."""
    north, west, south, east = area
    res = ERA5_GRID_RESOLUTION_DEG
    lat_start = math.floor(north / res) * res  # largest multiple of res <= north
    lat_end = math.ceil(south / res) * res  # smallest multiple of res >= south
    lon_start = math.ceil(west / res) * res  # smallest multiple of res >= west
    lon_end = math.floor(east / res) * res  # largest multiple of res <= east

    lats, lat = [], lat_start
    while lat >= lat_end - 1e-9:
        lats.append(round(lat, 2))
        lat -= res

    lons, lon = [], lon_start
    while lon <= lon_end + 1e-9:
        lons.append(round(lon, 2))
        lon += res

    return [(la, lo) for la in lats for lo in lons]
