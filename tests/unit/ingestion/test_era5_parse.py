"""Regression test for era5_client.ERA5Client._parse against the real CDS
response structure (confirmed live on 2026-09-20): time dimension is named
"valid_time" (not "time"), and "expver" is a per-timestep string coordinate
indexed by valid_time, not a separate dimension to select/loop over. An
earlier version of this code assumed the latter and silently produced zero
readings against the real API.
"""

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xarray as xr

from common.constants import WeatherProductType
from ingestion.weather.era5_client import ERA5Client


def _write_sample_era5_netcdf(path: Path, expver_values: list[str]) -> None:
    n = len(expver_values)
    valid_time = np.array(
        [np.datetime64("2026-09-10T00:00:00") + np.timedelta64(h, "h") for h in range(n)]
    )
    lat = np.array([29.0, 28.75])
    lon = np.array([76.75, 77.0])

    shape = (n, len(lat), len(lon))
    ds = xr.Dataset(
        {
            "u10": (("valid_time", "latitude", "longitude"), np.full(shape, 2.0, dtype="float32")),
            "v10": (("valid_time", "latitude", "longitude"), np.full(shape, 1.0, dtype="float32")),
            "t2m": (("valid_time", "latitude", "longitude"), np.full(shape, 295.0, dtype="float32")),
            "d2m": (("valid_time", "latitude", "longitude"), np.full(shape, 290.0, dtype="float32")),
        },
        coords={
            "valid_time": valid_time,
            "latitude": lat,
            "longitude": lon,
            "expver": ("valid_time", np.array(expver_values, dtype="<U4")),
            "number": 0,
        },
    )
    ds.to_netcdf(path)


def test_parse_reads_valid_time_dimension_and_per_timestep_expver(tmp_path):
    path = tmp_path / "sample.nc"
    _write_sample_era5_netcdf(path, expver_values=["0005", "0005", "0001"])

    client = ERA5Client(client=object())  # never calls .retrieve() in this test
    readings = client._parse(path, now=datetime(2026, 9, 20, tzinfo=timezone.utc))

    # 3 timestamps x 2 lat x 2 lon = 12 readings.
    assert len(readings) == 12

    by_time = {}
    for r in readings:
        by_time.setdefault(r.observed_at, []).append(r)

    times_sorted = sorted(by_time)
    assert len(times_sorted) == 3
    assert times_sorted[0] == datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)

    # First two hours tagged ERA5T (expver 0005), last hour final ERA5 (0001).
    assert all(r.product_type == WeatherProductType.ERA5T for r in by_time[times_sorted[0]])
    assert all(r.product_type == WeatherProductType.ERA5T for r in by_time[times_sorted[1]])
    assert all(r.product_type == WeatherProductType.ERA5 for r in by_time[times_sorted[2]])

    sample = readings[0]
    assert sample.u_wind == 2.0
    assert sample.v_wind == 1.0
    assert sample.wind_speed > 0
    assert 0 <= sample.relative_humidity <= 100
