#!/usr/bin/env python3
"""NetCDF adapters for the bundled deterministic scientific recipe operators."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

if __package__ in {None, ""}:
    script_directory = Path(__file__).resolve().parent
    sys.path[:0] = [str(script_directory), str(script_directory.parent)]

from scientific_operators.last_dim_profile import last_dim_profile
from scientific_operators.point_timeseries import point_timeseries
from scientific_operators.region_avg_timeseries import avg_timeseries
from scientific_operators.region_max_timeseries import max_timeseries
from scientific_operators.region_median_timeseries import median_timeseries
from scientific_operators.region_min_timeseries import min_timeseries
from scientific_operators.region_stats_max_value import find_max, find_max_in_region
from scientific_operators.region_stats_median_value import find_median, find_median_in_region
from scientific_operators.region_stats_min_value import find_min, find_min_in_region


MAX_INPUT_VALUES = 20_000_000


class RecipeError(RuntimeError):
    pass


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.ndarray):
        return [_json_value(item) for item in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _result(success: bool, operation: str, **payload: Any) -> dict[str, Any]:
    return _json_value({"success": success, "operation": operation, **payload})


def _open_dataset(path: Path) -> xr.Dataset:
    errors: list[str] = []
    for engine in ("h5netcdf", "netcdf4", None):
        try:
            options: dict[str, Any] = {"decode_cf": True, "mask_and_scale": True}
            if engine:
                options["engine"] = engine
            return xr.open_dataset(path, **options)
        except Exception as exc:
            errors.append(f"{engine or 'default'}: {type(exc).__name__}: {exc}")
    raise RecipeError("unable to open NetCDF: " + "; ".join(errors))


def _choose_variable(dataset: xr.Dataset, requested: str | None) -> str:
    if requested:
        if requested not in dataset.data_vars:
            raise RecipeError(f"unknown NetCDF variable: {requested}")
        return requested
    candidates = [
        name for name, value in dataset.data_vars.items()
        if np.issubdtype(value.dtype, np.number) and value.ndim > 0
    ]
    if len(candidates) != 1:
        raise RecipeError("variable is required unless the file has exactly one numeric data variable")
    return candidates[0]


def _coordinate_role(name: str, value: xr.DataArray) -> str | None:
    lowered = name.casefold()
    standard_name = str(value.attrs.get("standard_name", "")).casefold()
    axis = str(value.attrs.get("axis", "")).upper()
    if axis == "T" or standard_name == "time" or lowered in {"time", "date", "datetime"}:
        return "time"
    if axis == "Y" or standard_name == "latitude" or lowered in {"lat", "latitude"}:
        return "latitude"
    if axis == "X" or standard_name == "longitude" or lowered in {"lon", "longitude"}:
        return "longitude"
    return None


def _coordinate_name(
    array: xr.DataArray,
    role: str,
    requested: str | None,
) -> str:
    if requested:
        if requested not in array.coords or array.coords[requested].ndim != 1:
            raise RecipeError(f"{role} coordinate must be an existing one-dimensional coordinate: {requested}")
        return requested
    matches = [
        name for name, value in array.coords.items()
        if value.ndim == 1 and _coordinate_role(name, value) == role
    ]
    if len(matches) != 1:
        raise RecipeError(f"unable to identify one unambiguous {role} coordinate; inspect the file and provide it explicitly")
    return matches[0]


def _indices(raw: str) -> dict[str, int]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecipeError(f"invalid dimension_indices JSON: {exc}") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, int)
        for key, item in value.items()
    ):
        raise RecipeError("dimension_indices must be a JSON object of integer indices")
    return value


def _optional_json_array(raw: str | None, name: str, length: int | None = None) -> list[Any] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecipeError(f"invalid {name} JSON: {exc}") from exc
    if not isinstance(value, list) or (length is not None and len(value) != length):
        expected = f" with {length} items" if length is not None else ""
        raise RecipeError(f"{name} must be a JSON array{expected}")
    return value


def _select_extra_dimensions(
    array: xr.DataArray,
    preserved: set[str],
    dimension_indices: dict[str, int],
) -> xr.DataArray:
    unknown = set(dimension_indices) - set(array.dims)
    if unknown:
        raise RecipeError("unknown dimensions in dimension_indices: " + ", ".join(sorted(unknown)))
    selected = array
    for dimension in list(array.dims):
        if dimension in preserved:
            continue
        if dimension not in dimension_indices:
            raise RecipeError(f"dimension_indices must select extra dimension: {dimension}")
        index = dimension_indices[dimension]
        if index < 0 or index >= array.sizes[dimension]:
            raise RecipeError(f"index for {dimension} must be between 0 and {array.sizes[dimension] - 1}")
        selected = selected.isel({dimension: index})
    return selected


def _bounded_values(array: xr.DataArray) -> np.ndarray:
    if array.size > MAX_INPUT_VALUES:
        raise RecipeError(
            f"selected array contains {array.size} values; subset it below {MAX_INPUT_VALUES} values first"
        )
    return np.asarray(array.values)


def _source(path: Path, variable: str, array: xr.DataArray) -> dict[str, Any]:
    return {
        "name": path.name,
        "variable": variable,
        "dimensions": list(array.dims),
        "shape": list(array.shape),
        "unit": str(array.attrs.get("units", "")),
    }


def point_series(args: argparse.Namespace) -> dict[str, Any]:
    with _open_dataset(args.input_path) as dataset:
        variable = _choose_variable(dataset, args.variable)
        array = dataset[variable]
        time_name = _coordinate_name(array, "time", args.time_coordinate)
        lat_name = _coordinate_name(array, "latitude", args.latitude_coordinate)
        lon_name = _coordinate_name(array, "longitude", args.longitude_coordinate)
        preserved = {array.coords[name].dims[0] for name in (time_name, lat_name, lon_name)}
        selected = _select_extra_dimensions(array, preserved, _indices(args.dimension_indices))
        dimensions = [selected.coords[name].dims[0] for name in (time_name, lat_name, lon_name)]
        selected = selected.transpose(*dimensions)
        output = point_timeseries(
            _bounded_values(selected), selected[time_name].values,
            selected[lat_name].values, selected[lon_name].values,
            lat_index=args.latitude_index, lon_index=args.longitude_index,
            lat_value=args.latitude, lon_value=args.longitude,
            max_points=args.max_points, variable=variable,
            unit=str(array.attrs.get("units", "")),
        )
        return _result(True, "point_timeseries", source=_source(args.input_path, variable, selected), result=output)


def region_series(args: argparse.Namespace) -> dict[str, Any]:
    reducers = {
        "mean": avg_timeseries,
        "max": max_timeseries,
        "min": min_timeseries,
        "median": median_timeseries,
    }
    with _open_dataset(args.input_path) as dataset:
        variable = _choose_variable(dataset, args.variable)
        array = dataset[variable]
        time_name = _coordinate_name(array, "time", args.time_coordinate)
        lat_name = _coordinate_name(array, "latitude", args.latitude_coordinate)
        lon_name = _coordinate_name(array, "longitude", args.longitude_coordinate)
        preserved = {array.coords[name].dims[0] for name in (time_name, lat_name, lon_name)}
        selected = _select_extra_dimensions(array, preserved, _indices(args.dimension_indices))
        dimensions = [selected.coords[name].dims[0] for name in (time_name, lat_name, lon_name)]
        selected = selected.transpose(*dimensions)
        output = reducers[args.method](
            _bounded_values(selected), selected[time_name].values,
            selected[lat_name].values, selected[lon_name].values,
            polygon_4326=_optional_json_array(args.polygon, "polygon"),
            bbox_4326=_optional_json_array(args.bbox, "bbox", 4),
            max_points=args.max_points, variable=variable,
            unit=str(array.attrs.get("units", "")),
        )
        return _result(True, "region_timeseries", source=_source(args.input_path, variable, selected), result=output)


def region_statistics(args: argparse.Namespace) -> dict[str, Any]:
    full_reducers = {"max": find_max, "min": find_min, "median": find_median}
    region_reducers = {
        "max": find_max_in_region,
        "min": find_min_in_region,
        "median": find_median_in_region,
    }
    with _open_dataset(args.input_path) as dataset:
        variable = _choose_variable(dataset, args.variable)
        array = dataset[variable]
        lat_name = _coordinate_name(array, "latitude", args.latitude_coordinate)
        lon_name = _coordinate_name(array, "longitude", args.longitude_coordinate)
        preserved = {array.coords[name].dims[0] for name in (lat_name, lon_name)}
        selected = _select_extra_dimensions(array, preserved, _indices(args.dimension_indices))
        dimensions = [selected.coords[name].dims[0] for name in (lat_name, lon_name)]
        selected = selected.transpose(*dimensions)
        values = _bounded_values(selected)
        lat = selected[lat_name].values
        lon = selected[lon_name].values
        polygon = _optional_json_array(args.polygon, "polygon")
        bbox = _optional_json_array(args.bbox, "bbox", 4)
        if polygon is not None or bbox is not None:
            output = region_reducers[args.method](
                values, lat, lon, polygon_4326=polygon, bbox_4326=bbox,
            )
        else:
            output = full_reducers[args.method](values, lat, lon)
            output.update(mode="full", polygon_4326=None, bbox_4326=None)
        return _result(True, "region_statistics", source=_source(args.input_path, variable, selected), result=output)


def profile(args: argparse.Namespace) -> dict[str, Any]:
    with _open_dataset(args.input_path) as dataset:
        variable = _choose_variable(dataset, args.variable)
        array = dataset[variable]
        if args.dimension not in array.dims:
            raise RecipeError(f"unknown profile dimension: {args.dimension}")
        selected = array
        indices = _indices(args.dimension_indices)
        for dimension, index in indices.items():
            if dimension == args.dimension or dimension not in selected.dims:
                raise RecipeError(f"invalid dimension index selection: {dimension}")
            selected = selected.isel({dimension: index})
        selected = selected.transpose(*[dim for dim in selected.dims if dim != args.dimension], args.dimension)
        coords = selected.coords[args.dimension].values if args.dimension in selected.coords else None
        output = last_dim_profile(
            _bounded_values(selected), last_coords=coords, max_points=args.max_points,
            variable=variable, unit=str(array.attrs.get("units", "")), last_dim=args.dimension,
        )
        return _result(True, "last_dimension_profile", source=_source(args.input_path, variable, selected), result=output)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("point-timeseries", "region-timeseries", "region-statistics", "last-dimension-profile"):
        command = subparsers.add_parser(operation)
        command.add_argument("input_path", type=Path)
        command.add_argument("--variable")
        command.add_argument("--dimension-indices", default="{}")
        command.add_argument("--max-points", type=int, default=480)
        if operation in {"point-timeseries", "region-timeseries"}:
            command.add_argument("--time-coordinate")
            command.add_argument("--latitude-coordinate")
            command.add_argument("--longitude-coordinate")
        if operation == "point-timeseries":
            command.add_argument("--latitude", type=float)
            command.add_argument("--longitude", type=float)
            command.add_argument("--latitude-index", type=int)
            command.add_argument("--longitude-index", type=int)
        if operation == "region-timeseries":
            command.add_argument("--method", choices=("mean", "max", "min", "median"), required=True)
            command.add_argument("--polygon")
            command.add_argument("--bbox")
        if operation == "region-statistics":
            command.add_argument("--method", choices=("max", "min", "median"), required=True)
            command.add_argument("--latitude-coordinate")
            command.add_argument("--longitude-coordinate")
            command.add_argument("--polygon")
            command.add_argument("--bbox")
        if operation == "last-dimension-profile":
            command.add_argument("--dimension", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not args.input_path.is_file():
            raise RecipeError("input_path must be an existing NetCDF file")
        args.max_points = max(2, min(args.max_points, 2_000))
        handlers = {
            "point-timeseries": point_series,
            "region-timeseries": region_series,
            "region-statistics": region_statistics,
            "last-dimension-profile": profile,
        }
        payload = handlers[args.operation](args)
    except Exception as exc:
        payload = _result(False, args.operation, error=f"{type(exc).__name__}: {exc}")
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
