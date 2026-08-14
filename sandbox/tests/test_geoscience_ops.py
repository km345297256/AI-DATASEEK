from argparse import Namespace

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box
import xarray as xr

from scripts.geoscience_ops import (
    artifact_validate,
    change_detection,
    collection_inspect,
    grid_compare,
    grid_align,
    quality_check,
    raster_stack,
    sample_raster,
    transect_profile,
    vector_inspect,
    zonal_statistics,
)


def _netcdf(path):
    xr.Dataset(
        {"temperature": (("time", "latitude", "longitude"), np.arange(12, dtype="float32").reshape(1, 3, 4))},
        coords={"time": [np.datetime64("2020-01-01")], "latitude": [10.0, 11.0, 12.0], "longitude": [100.0, 101.0, 102.0, 103.0]},
    ).to_netcdf(path, engine="h5netcdf")


def _raster(path, offset=0):
    profile = {"driver": "GTiff", "width": 4, "height": 3, "count": 1, "dtype": "float32", "crs": "EPSG:4326", "transform": from_origin(100, 13, 1, 1), "nodata": -9999.0}
    with rasterio.open(path, "w", **profile) as target:
        target.write(np.arange(12, dtype="float32").reshape(3, 4) + offset, 1)


def test_collection_quality_and_grid_compare(tmp_path):
    first, second = tmp_path / "a.nc", tmp_path / "b.nc"
    _netcdf(first); _netcdf(second)
    inspected = collection_inspect(Namespace(input_dir=str(tmp_path), input_paths=[]))
    assert inspected["file_count"] == 2
    assert quality_check(Namespace(input_path=str(first)))["checks"]["temperature"]["maximum"] == 11.0
    assert grid_compare(Namespace(input_paths=[str(first), str(second)]))["compatible"] is True


def test_raster_stack_sample_and_validate(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path))
    first, second, stacked = tmp_path / "a.tif", tmp_path / "b.tif", tmp_path / "stack.tif"
    _raster(first); _raster(second, 100)
    assert raster_stack(Namespace(input_paths=[str(first), str(second)], output_path=str(stacked)))["success"]
    assert sample_raster(Namespace(input_path=str(first), points="[[100.5,12.5]]"))["samples"][0]["values"] == [0.0]
    assert artifact_validate(Namespace(input_path=str(stacked)))["checks"]["shape"] == [3, 4]


def test_vector_inspect_and_zonal_statistics(tmp_path):
    raster, vector = tmp_path / "a.tif", tmp_path / "zone.geojson"
    _raster(raster)
    gpd.GeoDataFrame({"name": ["zone"]}, geometry=[box(100, 11, 102, 13)], crs="EPSG:4326").to_file(vector, driver="GeoJSON")
    assert vector_inspect(Namespace(input_path=str(vector)))["feature_count"] == 1
    stats = zonal_statistics(Namespace(raster_path=str(raster), vector_path=str(vector)))
    assert stats["zones"][0]["count"] == 4
    assert stats["zones"][0]["mean"] == 2.5


def test_grid_alignment_change_detection_and_transect(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path))
    before, after = tmp_path / "before.tif", tmp_path / "after.tif"
    aligned, changed = tmp_path / "aligned.tif", tmp_path / "changed.tif"
    _raster(before); _raster(after, 2)
    assert grid_align(Namespace(input_path=str(after), reference_path=str(before), output_path=str(aligned), resampling="nearest"))["success"]
    changed_result = change_detection(Namespace(before_path=str(before), after_path=str(aligned), output_path=str(changed), band=1))
    assert changed_result["mean_change"] == 2.0
    profile = transect_profile(Namespace(input_path=str(before), points="[[100.5,12.5],[103.5,10.5]]", samples=4, band=1))
    assert len(profile["values"]) == 4
