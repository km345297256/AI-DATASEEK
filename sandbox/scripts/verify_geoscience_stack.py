#!/usr/bin/env python3
"""Exercise the preinstalled, offline geoscience analysis stack.

This is deliberately a functional smoke test rather than a list of imports. It
guards the interoperability boundaries that most often break in scientific
Python images: NumPy/GDAL, NetCDF/HDF5, Zarr, Dask, CRS transforms and vector IO.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import bottleneck
import cftime
import dask
import geopandas as gpd
import h5py
import h5netcdf
import netCDF4
import numpy as np
import pyogrio
import pyproj
import rioxarray  # noqa: F401 - registers the xarray ``rio`` accessor
import scipy
import shapely
import xarray as xr
import zarr
from shapely.geometry import Point


def _sample_dataset() -> xr.Dataset:
    values = np.arange(24, dtype="float32").reshape(2, 3, 4)
    return xr.Dataset(
        data_vars={"temperature": (("time", "lat", "lon"), values)},
        coords={
            "time": [cftime.DatetimeNoLeap(2000, 1, 1), cftime.DatetimeNoLeap(2000, 1, 2)],
            "lat": [30.0, 31.0, 32.0],
            "lon": [100.0, 101.0, 102.0, 103.0],
        },
    )


def verify() -> None:
    assert int(np.__version__.split(".", 1)[0]) < 2
    assert int(zarr.__version__.split(".", 1)[0]) == 2

    expected = _sample_dataset()
    with tempfile.TemporaryDirectory(prefix="ai-dataseek-geoscience-") as temp_dir:
        root = Path(temp_dir)

        netcdf_path = root / "sample.nc"
        expected.to_netcdf(netcdf_path, engine="netcdf4")
        with xr.open_dataset(netcdf_path, engine="h5netcdf", chunks={"time": 1}) as actual:
            assert actual.temperature.data.__class__.__module__.startswith("dask.")
            np.testing.assert_allclose(actual.temperature.mean().compute(), 11.5)

        zarr_path = root / "sample.zarr"
        expected.to_zarr(zarr_path, mode="w", consolidated=True)
        with xr.open_zarr(zarr_path, consolidated=True) as actual:
            np.testing.assert_array_equal(actual.temperature.compute(), expected.temperature)

        vector_path = root / "points.gpkg"
        points = gpd.GeoDataFrame(
            {"station": ["A", "B"]},
            geometry=[Point(116.4, 39.9), Point(121.5, 31.2)],
            crs="EPSG:4326",
        )
        points.to_file(vector_path, driver="GPKG", engine="pyogrio")
        projected = gpd.read_file(vector_path, engine="pyogrio").to_crs("EPSG:3857")
        assert projected.crs is not None and projected.crs.to_epsg() == 3857
        assert projected.geometry.is_valid.all()

        raster = xr.DataArray(
            np.arange(12, dtype="float32").reshape(3, 4),
            dims=("y", "x"),
            coords={"y": [32.0, 31.0, 30.0], "x": [100.0, 101.0, 102.0, 103.0]},
        ).rio.write_crs("EPSG:4326")
        assert raster.rio.reproject("EPSG:3857").rio.crs.to_epsg() == 3857

    transformer = pyproj.Transformer.from_crs(4326, 3857, always_xy=True)
    x, y = transformer.transform(116.4, 39.9)
    assert x > 0 and y > 0

    print(
        "geoscience stack ready",
        f"xarray={xr.__version__}",
        f"netCDF4={netCDF4.__version__}",
        f"h5py={h5py.__version__}",
        f"h5netcdf={h5netcdf.__version__}",
        f"zarr={zarr.__version__}",
        f"dask={dask.__version__}",
        f"geopandas={gpd.__version__}",
        f"pyogrio={pyogrio.__version__}",
        f"pyproj={pyproj.__version__}",
        f"shapely={shapely.__version__}",
        f"scipy={scipy.__version__}",
        f"bottleneck={bottleneck.__version__}",
    )


if __name__ == "__main__":
    verify()
