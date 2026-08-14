import json

import numpy as np
import xarray as xr

from scripts.scientific_recipe_ops import main


def _dataset(path):
    times = np.array(["2000-01-01", "2000-02-01", "2000-03-01"], dtype="datetime64[D]")
    lat = np.array([10.0, 20.0])
    lon = np.array([100.0, 110.0, 120.0])
    values = np.arange(18, dtype=np.float32).reshape(3, 2, 3)
    xr.Dataset({"rain": (("time", "lat", "lon"), values)}, coords={"time": times, "lat": lat, "lon": lon}).to_netcdf(path)


def _run(monkeypatch, args, capsys):
    monkeypatch.setattr("sys.argv", ["scientific-recipe", *args])
    assert main() == 0
    return json.loads(capsys.readouterr().out)


def test_point_timeseries_uses_nearest_coordinates(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sample.nc"
    _dataset(path)
    payload = _run(monkeypatch, ["point-timeseries", str(path), "--variable", "rain", "--latitude", "19", "--longitude", "111"], capsys)
    assert payload["success"] is True
    assert payload["result"]["lat"] == 20.0
    assert payload["result"]["lon"] == 110.0
    assert payload["result"]["values"] == [4.0, 10.0, 16.0]


def test_region_mean_and_max_statistics_are_deterministic(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sample.nc"
    _dataset(path)
    mean = _run(monkeypatch, ["region-timeseries", str(path), "--variable", "rain", "--method", "mean"], capsys)
    assert mean["result"]["values"] == [2.5, 8.5, 14.5]
    maximum = _run(monkeypatch, ["region-statistics", str(path), "--variable", "rain", "--method", "max", "--dimension-indices", "{\"time\": 1}"], capsys)
    assert maximum["result"]["max"] == 11.0
    assert maximum["result"]["max_location"] == {"lat": 20.0, "lon": 120.0}


def test_last_dimension_profile_returns_profile_values(tmp_path, monkeypatch, capsys):
    path = tmp_path / "sample.nc"
    _dataset(path)
    payload = _run(monkeypatch, ["last-dimension-profile", str(path), "--variable", "rain", "--dimension", "lon", "--dimension-indices", "{\"time\": 0}"], capsys)
    assert payload["success"] is True
    assert payload["result"]["axis"] == [100.0, 110.0, 120.0]
    assert payload["result"]["values"] == [1.5, 2.5, 3.5]
