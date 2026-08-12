import json
from pathlib import Path

import numpy as np
import pytest

from scripts.scientific_data_ops import (
    aggregate,
    convert_netcdf_to_geotiff,
    inspect,
    raster_index,
    statistics,
    subset_netcdf,
    transform_raster,
    terrain,
    visualize,
)


def _netcdf(tmp_path: Path) -> Path:
    xr = pytest.importorskip("xarray")
    path = tmp_path / "sample.nc"
    dataset = xr.Dataset(
        {"rain": (("time", "lat", "lon"), np.arange(24, dtype="float32").reshape(2, 3, 4))},
        coords={"time": ["2000-01-01", "2000-02-01"], "lat": [10.0, 20.0, 30.0], "lon": [100.0, 110.0, 120.0, 130.0]},
    )
    dataset["rain"].attrs.update(units="mm", standard_name="precipitation_flux")
    dataset.to_netcdf(path, engine="h5netcdf")
    return path


def test_netcdf_inspect_exposes_variable_coordinate_roles_and_units(tmp_path):
    result = inspect(_netcdf(tmp_path))
    assert result["success"] is True
    assert result["format"] == "netcdf"
    assert result["data_variable_candidates"] == ["rain"]
    assert next(item for item in result["coordinates"] if item["name"] == "lat")["role"] == "latitude"
    assert result["variables"][0]["attributes"]["units"] == "mm"


def test_netcdf_statistics_requires_explicit_variable_when_ambiguous(tmp_path):
    xr = pytest.importorskip("xarray")
    path = _netcdf(tmp_path)
    with xr.open_dataset(path, engine="h5netcdf") as dataset:
        expanded = dataset.load()
    expanded["temperature"] = expanded["rain"] + 100
    expanded.to_netcdf(path, mode="w", engine="h5netcdf")
    with pytest.raises(RuntimeError, match="variable is required"):
        statistics(path, None, 1, {})
    result = statistics(path, "rain", 1, {"time": 0})
    assert result["statistics"]["valid_count"] == 12
    assert result["unit"] == "mm"


def test_netcdf_aggregate_is_labelled_and_explicit(tmp_path):
    result = aggregate(_netcdf(tmp_path), "rain", "mean", "time", "2000-01-01", "2000-02-01")
    assert result["success"] is True
    assert result["output_dimensions"] == {"lat": 3, "lon": 4}
    assert result["values"][0][0] == pytest.approx(6.0)


def test_geotiff_statistics_and_visualize_apply_mask_and_write_artifact(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    source = tmp_path / "sample.tif"
    with rasterio.open(source, "w", driver="GTiff", height=2, width=3, count=1, dtype="float32", crs="EPSG:4326", transform=from_origin(100, 20, 1, 1), nodata=-9999) as dataset:
        dataset.write(np.array([[1, 2, -9999], [4, 5, 6]], dtype="float32"), 1)
        dataset.set_band_description(1, "rain")
        dataset.set_band_unit(1, "mm")
    result = statistics(source, None, 1, {})
    assert result["statistics"]["valid_count"] == 5
    assert result["provenance"]["mask_applied"] is True
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path / "output"))
    output = tmp_path / "output" / "scientific-test" / "map.png"
    image = visualize(source, output, None, 1, {})
    assert image["success"] is True
    assert output.stat().st_size > 0


def test_netcdf_subset_and_conversion_preserve_coordinates(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path / "output"))
    source = _netcdf(tmp_path)
    subset_path = tmp_path / "output" / "subset.nc"
    subset = subset_netcdf(source, subset_path, "rain", [105, 15, 125, 30], None, None, {"time": 0})
    assert subset["output_dimensions"] == {"lat": 2, "lon": 2}
    tif_path = tmp_path / "output" / "rain.tif"
    converted = convert_netcdf_to_geotiff(source, tif_path, "rain", {"time": 0})
    assert converted["success"] is True
    rasterio = pytest.importorskip("rasterio")
    with rasterio.open(tif_path) as dataset:
        assert dataset.crs.to_string() == "EPSG:4326"
        assert dataset.shape == (3, 4)


def test_raster_transform_reprojects_and_resamples(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path / "output"))
    source = tmp_path / "source.tif"
    with rasterio.open(source, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32", crs="EPSG:4326", transform=from_origin(100, 20, 1, 1)) as dataset:
        dataset.write(np.arange(16, dtype="float32").reshape(4, 4), 1)
    output = tmp_path / "output" / "projected.tif"
    result = transform_raster(source, output, "EPSG:3857", 100000, None, "bilinear")
    assert result["success"] is True
    with rasterio.open(output) as dataset:
        assert dataset.crs.to_string() == "EPSG:3857"


def test_raster_index_uses_explicit_band_semantics(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path / "output"))
    source = tmp_path / "multiband.tif"
    with rasterio.open(source, "w", driver="GTiff", height=2, width=2, count=2, dtype="float32", crs="EPSG:3857", transform=from_origin(0, 200, 100, 100)) as dataset:
        dataset.write(np.full((2, 2), 3, dtype="float32"), 1)
        dataset.write(np.full((2, 2), 1, dtype="float32"), 2)
    output = tmp_path / "output" / "ndvi.tif"
    result = raster_index(source, output, "ndvi", {"nir": 1, "red": 2})
    assert result["formula"] == "(nir-red)/(nir+red)"
    with rasterio.open(output) as dataset:
        assert np.allclose(dataset.read(1), 0.5)


def test_terrain_rejects_geographic_dem_and_computes_projected_slope(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path / "output"))
    source = tmp_path / "dem.tif"
    with rasterio.open(source, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32", crs="EPSG:3857", transform=from_origin(0, 30, 10, 10)) as dataset:
        dataset.write(np.tile(np.arange(3, dtype="float32"), (3, 1)) * 10, 1)
    output = tmp_path / "output" / "slope.tif"
    result = terrain(source, output, "slope", 1)
    assert result["success"] is True
    with rasterio.open(output) as dataset:
        assert np.allclose(dataset.read(1), 45.0)
