import base64
import json
from pathlib import Path

import pytest

from scripts.scientific_data_ops import build_parser as build_scientific_parser
from scripts.scientific_recipe_ops import build_parser as build_recipe_parser
from scripts.tool_plugin_runner import load_handler, load_registry, run_tool


ROOT = Path(__file__).resolve().parents[2]


def test_runner_discovers_builtin_scientific_tools():
    registry = load_registry(ROOT / "tools")

    assert len(registry) == 37
    assert "scientific_inspect" in registry
    assert "scientific_region_timeseries" in registry
    assert "geoscience_collection_inspect" in registry


def test_runner_rejects_duplicate_names(tmp_path):
    for plugin in ("one", "two"):
        directory = tmp_path / plugin
        directory.mkdir()
        (directory / "handler.py").write_text("def build_command(name, arguments): return ['true']\n")
        (directory / "manifest.json").write_text(json.dumps({
            "plugin": plugin,
            "tools": [{"name": "same"}],
        }))

    with pytest.raises(RuntimeError, match="Duplicate plugin tool name"):
        load_registry(tmp_path)


def test_runner_executes_only_registered_handler_command(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "echo"
    directory.mkdir()
    (directory / "handler.py").write_text(
        "def build_command(name, arguments):\n"
        "    return ['printf', '%s', arguments['value']]\n"
    )
    (directory / "manifest.json").write_text(json.dumps({
        "plugin": "echo",
        "handler": "handler.py",
        "tools": [{"name": "echo_value"}],
    }))
    monkeypatch.setenv("AI_DATASEEK_TOOLS_DIR", str(tmp_path))
    payload = base64.urlsafe_b64encode(json.dumps({"value": "hello"}).encode()).decode()

    assert run_tool("echo_value", payload) == 0
    assert capsys.readouterr().out == "hello"


@pytest.mark.parametrize(("name", "arguments"), [
    ("scientific_inspect", {}),
    ("scientific_statistics", {"variable": "rain", "band": 1, "dimension_indices": {"time": 0}}),
    ("scientific_aggregate", {"dimension": "time", "method": "mean", "output_path": "/home/ubuntu/output/a.nc"}),
    ("scientific_subset", {"output_path": "/home/ubuntu/output/a.nc", "bbox": [0, 1, 2, 3]}),
    ("scientific_convert_netcdf_to_geotiff", {"output_path": "/home/ubuntu/output/a.tif"}),
    ("scientific_transform_raster", {"output_path": "/home/ubuntu/output/a.tif", "target_crs": "EPSG:4326"}),
    ("scientific_raster_index", {"output_path": "/home/ubuntu/output/a.tif", "index_name": "ndvi", "bands": {"nir": 4, "red": 3}}),
    ("scientific_terrain", {"output_path": "/home/ubuntu/output/a.tif", "operation": "slope"}),
    ("scientific_visualize", {"output_path": "/home/ubuntu/output/a.png"}),
    ("scientific_netcdf_visualize", {"output_dir": "/home/ubuntu/output/plots", "max_plots": 4}),
    ("scientific_point_timeseries", {"latitude": 30.0, "longitude": 110.0}),
    ("scientific_region_timeseries", {"method": "mean", "bbox": [100, 20, 110, 30]}),
    ("scientific_region_statistics", {"method": "max", "bbox": [100, 20, 110, 30]}),
    ("scientific_last_dimension_profile", {"dimension": "level"}),
])
def test_scientific_plugin_commands_match_existing_cli_contract(name, arguments):
    registry = load_registry(ROOT / "tools")
    _, handler_path = registry[name]
    command = load_handler(handler_path)(name, {
        "input_path": "/home/ubuntu/datasets/example.nc",
        **arguments,
    })

    parser = build_recipe_parser() if name in {
        "scientific_point_timeseries",
        "scientific_region_timeseries",
        "scientific_region_statistics",
        "scientific_last_dimension_profile",
    } else build_scientific_parser()
    parsed = parser.parse_args(command[1:])
    assert parsed.input_path == Path("/home/ubuntu/datasets/example.nc")
