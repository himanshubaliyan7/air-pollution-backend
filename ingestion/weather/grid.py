"""Delhi NCR ERA5 grid definition.

ERA5 single-levels is on a ~0.25 degree lat/lon grid. This bounding box
covers Delhi plus the immediate NCR satellite cities (Gurugram, Noida,
Ghaziabad, Faridabad) with a small margin, giving roughly a 2x3 cell
sub-grid at 0.25 degree resolution.
"""

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
