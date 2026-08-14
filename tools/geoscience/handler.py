"""Adapter for the geoscience plugin command line."""
from __future__ import annotations
import sys
from pathlib import Path
from typing import Any

OPS = {
    "geoscience_collection_inspect": "collection-inspect",
    "geoscience_coordinate_normalize": "coordinate-normalize",
    "geoscience_grid_compare": "grid-compare",
    "geoscience_quality_check": "quality-check",
    "geoscience_unit_convert": "unit-convert",
    "geoscience_raster_stack": "raster-stack",
    "geoscience_raster_mosaic": "raster-mosaic",
    "geoscience_sample_raster": "sample-raster",
    "geoscience_qa_mask": "qa-mask",
    "geoscience_scene_composite": "scene-composite",
    "geoscience_climatology": "climatology",
    "geoscience_anomaly": "anomaly",
    "geoscience_trend": "trend",
    "geoscience_artifact_validate": "artifact-validate",
    "geoscience_vector_inspect": "vector-inspect",
    "geoscience_vector_transform": "vector-transform",
    "geoscience_zonal_statistics": "zonal-statistics",
    "geoscience_rasterize_vector": "rasterize-vector",
    "geoscience_grid_align": "grid-align",
    "geoscience_remote_product_inspect": "remote-product-inspect",
    "geoscience_change_detection": "change-detection",
    "geoscience_spatial_join": "spatial-join",
    "geoscience_transect_profile": "transect-profile",
}

def build_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    if tool_name not in OPS: raise ValueError(f"Unsupported geoscience tool: {tool_name}")
    script = Path("/usr/local/lib/ai-dataseek/geoscience_ops.py")
    command = [sys.executable, str(script), OPS[tool_name]]
    if tool_name == "geoscience_collection_inspect":
        if arguments.get("input_dir"): command += ["--input-dir", str(arguments["input_dir"])]
        if arguments.get("input_paths"): command += ["--input-paths", *map(str, arguments["input_paths"])]
        return command
    if tool_name in {"geoscience_grid_compare", "geoscience_raster_stack", "geoscience_raster_mosaic", "geoscience_scene_composite", "geoscience_climatology", "geoscience_anomaly", "geoscience_trend"}:
        command += [*map(str, arguments.get("input_paths", [])), str(arguments["output_path"])] if tool_name != "geoscience_grid_compare" else [*map(str, arguments.get("input_paths", []))]
        if arguments.get("variable"): command += ["--variable", str(arguments["variable"])]
        return command
    if tool_name == "geoscience_unit_convert":
        command += [str(arguments["input_path"]), str(arguments["output_path"]), str(arguments["from_unit"]), str(arguments["to_unit"])]
        if arguments.get("variable"): command += ["--variable", str(arguments["variable"])]
        return command
    if tool_name == "geoscience_sample_raster":
        import json
        return command + [str(arguments["input_path"]), json.dumps(arguments["points"], separators=(",", ":"))]
    if tool_name == "geoscience_qa_mask":
        return command + [str(arguments["input_path"]), str(arguments["output_path"]), str(arguments["bit"])]
    if tool_name == "geoscience_artifact_validate":
        return command + [str(arguments["input_path"])]
    if tool_name == "geoscience_vector_inspect":
        return command + [str(arguments["input_path"])]
    if tool_name == "geoscience_vector_transform":
        command += [str(arguments["input_path"]), str(arguments["output_path"])]
        if arguments.get("target_crs"): command += ["--target-crs", str(arguments["target_crs"])]
        return command
    if tool_name == "geoscience_zonal_statistics":
        return command + [str(arguments["raster_path"]), str(arguments["vector_path"])]
    if tool_name == "geoscience_rasterize_vector":
        return command + [str(arguments["reference_raster"]), str(arguments["vector_path"]), str(arguments["output_path"])]
    if tool_name == "geoscience_grid_align":
        return command + [str(arguments["input_path"]), str(arguments["reference_path"]), str(arguments["output_path"]), "--resampling", str(arguments.get("resampling", "nearest"))]
    if tool_name == "geoscience_remote_product_inspect":
        return command + [str(arguments["input_path"])]
    if tool_name == "geoscience_change_detection":
        return command + [str(arguments["before_path"]), str(arguments["after_path"]), str(arguments["output_path"]), "--band", str(arguments.get("band", 1))]
    if tool_name == "geoscience_spatial_join":
        return command + [str(arguments["left_path"]), str(arguments["right_path"]), str(arguments["output_path"]), "--predicate", str(arguments.get("predicate", "intersects")), "--how", str(arguments.get("how", "left"))]
    if tool_name == "geoscience_transect_profile":
        import json
        return command + [str(arguments["input_path"]), json.dumps(arguments["points"], separators=(",", ":")), "--samples", str(arguments.get("samples", 100)), "--band", str(arguments.get("band", 1))]
    for name, value in arguments.items():
        if value is None or name == "timeout_seconds": continue
        flag = "--" + name.replace("_", "-") if name.startswith("input_") or name in {"variable", "from_unit", "to_unit", "bit", "input_dir", "input_paths"} else None
        if name == "input_paths":
            if tool_name == "geoscience_collection_inspect": command.extend(["--input-paths", *[str(x) for x in value]])
            else: command.extend([str(x) for x in value])
        elif name == "input_path": command.append(str(value))
        elif name == "output_path": command.append(str(value))
        elif flag: command.extend([flag, str(value)])
    return command
