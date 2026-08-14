"""Command adapter for the built-in scientific data plugin."""

from __future__ import annotations

import json
from typing import Any


DATA_OPERATIONS = {
    "scientific_inspect": "inspect",
    "scientific_statistics": "statistics",
    "scientific_aggregate": "aggregate",
    "scientific_subset": "subset",
    "scientific_convert_netcdf_to_geotiff": "convert",
    "scientific_transform_raster": "transform",
    "scientific_raster_index": "raster-index",
    "scientific_terrain": "terrain",
    "scientific_visualize": "visualize",
    "scientific_netcdf_visualize": "visualize-bundle",
}

RECIPE_OPERATIONS = {
    "scientific_point_timeseries": "point-timeseries",
    "scientific_region_timeseries": "region-timeseries",
    "scientific_region_statistics": "region-statistics",
    "scientific_last_dimension_profile": "last-dimension-profile",
}

FLAG_NAMES = {
    "output_path": "--output",
    "output_dir": "--output",
    "dimension_indices": "--dimension-indices",
    "target_crs": "--target-crs",
    "target_resolution": "--target-resolution",
    "resampling": "--resampling",
    "max_plots": "--max-plots",
    "max_points": "--max-points",
    "time_coordinate": "--time-coordinate",
    "latitude_coordinate": "--latitude-coordinate",
    "longitude_coordinate": "--longitude-coordinate",
    "latitude_index": "--latitude-index",
    "longitude_index": "--longitude-index",
    "index_name": "--index",
    "operation": "--terrain-operation",
}


def _flag(name: str) -> str:
    return FLAG_NAMES.get(name, "--" + name.replace("_", "-"))


def _render(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    if tool_name in DATA_OPERATIONS:
        command = ["ai-dataseek-scientific", DATA_OPERATIONS[tool_name]]
    elif tool_name in RECIPE_OPERATIONS:
        command = ["ai-dataseek-scientific-recipe", RECIPE_OPERATIONS[tool_name]]
    else:
        raise ValueError(f"Unsupported scientific tool: {tool_name}")
    input_path = arguments.get("input_path")
    if not isinstance(input_path, str) or not input_path:
        raise ValueError("input_path is required")
    command.append(input_path)
    for name, value in arguments.items():
        if name in {"input_path", "timeout_seconds"} or value is None:
            continue
        command.extend([_flag(name), _render(value)])
    return command
