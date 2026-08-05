import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_common_excel_and_visualization_stack_is_preinstalled():
    import matplotlib
    import numpy
    import openpyxl
    import pandas
    import rarfile
    import rasterio
    import seaborn
    import xlrd
    from PIL import Image

    assert int(numpy.__version__.split(".", 1)[0]) < 2
    assert all(
        module is not None
        for module in (matplotlib, openpyxl, pandas, rarfile, rasterio, seaborn, xlrd, Image)
    )


def test_python_and_pip_use_the_same_virtual_environment():
    venv_path = Path(os.environ.get("VIRTUAL_ENV") or sys.prefix)
    assert venv_path != Path(sys.base_prefix)
    assert Path(sys.executable).is_relative_to(venv_path)
    assert Path(shutil.which("python") or "").is_relative_to(venv_path)
    assert Path(shutil.which("python3") or "").is_relative_to(venv_path)
    assert Path(shutil.which("pip") or "").is_relative_to(venv_path)
    assert Path(shutil.which("pip3") or "").is_relative_to(venv_path)

    pip_version = subprocess.run(
        [sys.executable, "-m", "pip", "--version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(venv_path) in pip_version


def test_gdal_remains_importable_with_locked_numpy():
    gdal = pytest.importorskip("osgeo.gdal")

    assert gdal.VersionInfo()


def test_rasterio_reads_a_geotiff_alongside_system_gdal():
    import numpy
    import rasterio
    from osgeo import gdal
    from rasterio.io import MemoryFile

    pixels = numpy.arange(12, dtype="float32").reshape(1, 3, 4)
    with MemoryFile() as memory_file:
        with memory_file.open(
            driver="GTiff",
            width=4,
            height=3,
            count=1,
            dtype="float32",
        ) as dataset:
            dataset.write(pixels)
        with memory_file.open() as dataset:
            numpy.testing.assert_array_equal(dataset.read(), pixels)

    assert rasterio.__gdal_version__
    assert gdal.VersionInfo()


def test_multidimensional_and_vector_geoscience_stack(tmp_path):
    dask = pytest.importorskip("dask")
    geopandas = pytest.importorskip("geopandas")
    h5py = pytest.importorskip("h5py")
    h5netcdf = pytest.importorskip("h5netcdf")
    netcdf4 = pytest.importorskip("netCDF4")
    numpy = pytest.importorskip("numpy")
    pytest.importorskip("pyogrio")
    pyproj = pytest.importorskip("pyproj")
    pytest.importorskip("rioxarray")
    scipy = pytest.importorskip("scipy")
    shapely = pytest.importorskip("shapely")
    xarray = pytest.importorskip("xarray")
    zarr = pytest.importorskip("zarr")
    from shapely.geometry import Point

    assert int(zarr.__version__.split(".", 1)[0]) == 2
    assert all(
        module is not None
        for module in (dask, geopandas, h5py, h5netcdf, netcdf4, pyproj, scipy, shapely)
    )

    values = numpy.arange(24, dtype="float32").reshape(2, 3, 4)
    dataset = xarray.Dataset(
        {"temperature": (("time", "lat", "lon"), values)},
        coords={"time": [0, 1], "lat": [30.0, 31.0, 32.0], "lon": [100.0, 101.0, 102.0, 103.0]},
    )

    netcdf_path = tmp_path / "sample.nc"
    dataset.to_netcdf(netcdf_path, engine="netcdf4")
    with xarray.open_dataset(netcdf_path, engine="h5netcdf", chunks={"time": 1}) as actual:
        assert actual.temperature.data.__class__.__module__.startswith("dask.")
        numpy.testing.assert_allclose(actual.temperature.mean().compute(), 11.5)

    zarr_path = tmp_path / "sample.zarr"
    dataset.to_zarr(zarr_path, mode="w", consolidated=True)
    with xarray.open_zarr(zarr_path, consolidated=True) as actual:
        numpy.testing.assert_array_equal(actual.temperature, dataset.temperature)

    vector_path = tmp_path / "points.gpkg"
    points = geopandas.GeoDataFrame(
        {"station": ["A", "B"]},
        geometry=[Point(116.4, 39.9), Point(121.5, 31.2)],
        crs="EPSG:4326",
    )
    points.to_file(vector_path, driver="GPKG", engine="pyogrio")
    projected = geopandas.read_file(vector_path, engine="pyogrio").to_crs("EPSG:3857")
    assert projected.crs.to_epsg() == 3857
    assert projected.geometry.is_valid.all()


@pytest.mark.parametrize("command", ["ncdump", "h5dump", "projinfo"])
def test_geoscience_cli_is_preinstalled(command):
    if not os.environ.get("AI_DATASEEK_REQUIRE_GEOSCIENCE_STACK"):
        pytest.skip("CLI presence is enforced during the sandbox image build")
    assert shutil.which(command), f"missing preinstalled geoscience command: {command}"
