#!/usr/bin/env python3
"""Deterministic, bounded operators for NetCDF and GeoTIFF data."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
import os
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.enums import Resampling
import xarray as xr


NETCDF_SUFFIXES = {".nc", ".nc4", ".cdf"}
RASTER_SUFFIXES = {".tif", ".tiff"}
MAX_SAMPLE_VALUES = 1_000_000
MAX_INLINE_VALUES = 256


class ScientificDataError(RuntimeError):
    pass


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else float(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (datetime, date, np.datetime64)):
        return str(value)
    if isinstance(value, np.ndarray):
        return [_json_value(item) for item in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _result(success: bool, operation: str, **payload: Any) -> dict[str, Any]:
    return _json_value({"success": success, "operation": operation, **payload})


def _format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in NETCDF_SUFFIXES:
        return "netcdf"
    if suffix in RASTER_SUFFIXES:
        return "geotiff"
    raise ScientificDataError("supported formats are NetCDF (.nc/.nc4/.cdf) and GeoTIFF (.tif/.tiff)")


def _validated_output(path: Path) -> Path:
    output_root = Path(os.environ.get("AI_DATASEEK_OUTPUT_ROOT", "/home/ubuntu/output"))
    try:
        path.resolve().relative_to(output_root.resolve())
    except ValueError as exc:
        raise ScientificDataError(f"output path must be below {output_root}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "standard_name", "long_name", "units", "calendar", "axis",
        "positive", "grid_mapping", "coordinates", "cell_methods",
        "_FillValue", "missing_value", "scale_factor", "add_offset",
    )
    return {key: _json_value(attrs[key]) for key in keys if key in attrs}


def _coord_role(name: str, variable: xr.DataArray) -> str | None:
    lowered = name.lower()
    standard_name = str(variable.attrs.get("standard_name", "")).lower()
    axis = str(variable.attrs.get("axis", "")).upper()
    if axis == "T" or standard_name == "time" or lowered in {"time", "date", "datetime"}:
        return "time"
    if axis == "X" or standard_name in {"longitude", "projection_x_coordinate"} or lowered in {"lon", "longitude", "x"}:
        return "longitude" if "longitude" in standard_name or lowered in {"lon", "longitude"} else "x"
    if axis == "Y" or standard_name in {"latitude", "projection_y_coordinate"} or lowered in {"lat", "latitude", "y"}:
        return "latitude" if "latitude" in standard_name or lowered in {"lat", "latitude"} else "y"
    if axis == "Z" or standard_name in {"height", "depth", "altitude"}:
        return "vertical"
    return None


def _range(variable: xr.DataArray) -> dict[str, Any] | None:
    if variable.size == 0:
        return None
    try:
        values = np.asarray(variable.values).reshape(-1)
        return {"first": _json_value(values[0]), "last": _json_value(values[-1])}
    except Exception:
        return None


def _coordinate_summary(variable: xr.DataArray) -> dict[str, Any] | None:
    if variable.ndim != 1 or variable.size == 0:
        return None
    try:
        values = np.asarray(variable.values).reshape(-1)
    except Exception:
        return None
    summary: dict[str, Any] = {
        "count": int(values.size),
        "first_values": _json_value(values[: min(5, values.size)]),
        "last_values": _json_value(values[max(0, values.size - 5):]),
    }
    if not np.issubdtype(values.dtype, np.number):
        return summary
    numeric = values.astype("float64", copy=False)
    finite = numeric[np.isfinite(numeric)]
    if finite.size:
        summary["minimum"] = float(np.min(finite))
        summary["maximum"] = float(np.max(finite))
    if numeric.size > 1:
        differences = np.diff(numeric)
        finite_differences = differences[np.isfinite(differences)]
        if finite_differences.size:
            step = float(np.median(finite_differences))
            summary["step"] = step
            summary["direction"] = "ascending" if step > 0 else "descending" if step < 0 else "constant"
            summary["regular"] = bool(np.allclose(finite_differences, step, rtol=1e-6, atol=1e-10))
    return summary


def _open_netcdf(path: Path) -> xr.Dataset:
    errors: list[str] = []
    for engine in ("h5netcdf", "netcdf4", None):
        try:
            kwargs = {"decode_cf": True, "mask_and_scale": True}
            if engine:
                kwargs["engine"] = engine
            return xr.open_dataset(path, **kwargs)
        except Exception as exc:
            errors.append(f"{engine or 'default'}: {type(exc).__name__}: {exc}")
    raise ScientificDataError("unable to open NetCDF: " + "; ".join(errors))


def _netcdf_inspect(path: Path) -> dict[str, Any]:
    with _open_netcdf(path) as dataset:
        coordinates = []
        for name, variable in dataset.coords.items():
            coordinates.append({
                "name": name,
                "dimensions": list(variable.dims),
                "shape": list(variable.shape),
                "dtype": str(variable.dtype),
                "role": _coord_role(name, variable),
                "range": _range(variable),
                "summary": _coordinate_summary(variable),
                "attributes": _attrs(variable.attrs),
            })
        variables = []
        candidates = []
        for name, variable in dataset.data_vars.items():
            numeric = np.issubdtype(variable.dtype, np.number)
            item = {
                "name": name,
                "dimensions": list(variable.dims),
                "shape": list(variable.shape),
                "dtype": str(variable.dtype),
                "numeric": bool(numeric),
                "attributes": _attrs(variable.attrs),
            }
            variables.append(item)
            if numeric and variable.ndim > 0:
                candidates.append(name)
        return _result(
            True,
            "inspect",
            format="netcdf",
            source={"name": path.name, "size_bytes": path.stat().st_size},
            dimensions={name: int(size) for name, size in dataset.sizes.items()},
            coordinates=coordinates,
            variables=variables,
            data_variable_candidates=candidates,
            global_attributes=_json_value(dict(dataset.attrs)),
            conventions=dataset.attrs.get("Conventions"),
            warnings=[] if candidates else ["no numeric data variable was found"],
        )


def _raster_inspect(path: Path) -> dict[str, Any]:
    with rasterio.open(path) as dataset:
        mask_flags = [[flag.name for flag in flags] for flags in dataset.mask_flag_enums]
        bands = []
        for index in range(1, dataset.count + 1):
            tags = dataset.tags(index)
            bands.append({
                "band": index,
                "description": dataset.descriptions[index - 1],
                "dtype": dataset.dtypes[index - 1],
                "nodata": _json_value(dataset.nodatavals[index - 1]),
                "unit": dataset.units[index - 1] if dataset.units else None,
                "scale": _json_value(dataset.scales[index - 1]),
                "offset": _json_value(dataset.offsets[index - 1]),
                "mask_flags": mask_flags[index - 1],
                "tags": _json_value(tags),
            })
        return _result(
            True,
            "inspect",
            format="geotiff",
            source={"name": path.name, "size_bytes": path.stat().st_size},
            shape=[dataset.height, dataset.width],
            band_count=dataset.count,
            bands=bands,
            crs=dataset.crs.to_string() if dataset.crs else None,
            transform=list(dataset.transform)[:6],
            bounds={
                "left": dataset.bounds.left,
                "bottom": dataset.bounds.bottom,
                "right": dataset.bounds.right,
                "top": dataset.bounds.top,
            },
            resolution=list(dataset.res),
            tiled=dataset.is_tiled,
            warnings=[] if dataset.crs else ["CRS is not declared"],
        )


def inspect(path: Path) -> dict[str, Any]:
    kind = _format(path)
    return _netcdf_inspect(path) if kind == "netcdf" else _raster_inspect(path)


def _finite_statistics(values: np.ndarray, total_count: int, masked_count: int) -> dict[str, Any]:
    finite = np.asarray(values, dtype="float64")
    finite = finite[np.isfinite(finite)]
    if finite.size == 0:
        raise ScientificDataError("the selected data contains no finite values")
    quantiles = np.quantile(finite, [0.05, 0.25, 0.5, 0.75, 0.95])
    return {
        "count": int(total_count),
        "valid_count": int(finite.size),
        "masked_or_nonfinite_count": int(masked_count + total_count - finite.size - masked_count),
        "valid_fraction": float(finite.size / total_count) if total_count else 0.0,
        "zero_count": int(np.count_nonzero(finite == 0)),
        "minimum": float(np.min(finite)),
        "maximum": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "standard_deviation": float(np.std(finite)),
        "quantiles": {
            "p05": float(quantiles[0]), "p25": float(quantiles[1]),
            "p50": float(quantiles[2]), "p75": float(quantiles[3]),
            "p95": float(quantiles[4]),
        },
    }


def _choose_variable(dataset: xr.Dataset, requested: str | None) -> str:
    if requested:
        if requested not in dataset.data_vars:
            raise ScientificDataError(f"unknown NetCDF variable: {requested}")
        return requested
    candidates = [
        name for name, variable in dataset.data_vars.items()
        if np.issubdtype(variable.dtype, np.number) and variable.ndim > 0
    ]
    if len(candidates) != 1:
        raise ScientificDataError("variable is required; candidates: " + ", ".join(candidates))
    return candidates[0]


def _select_indices(variable: xr.DataArray, indices: dict[str, int]) -> xr.DataArray:
    unknown = sorted(set(indices) - set(variable.dims))
    if unknown:
        raise ScientificDataError("selector dimensions not present in variable: " + ", ".join(unknown))
    for dimension, index in indices.items():
        size = variable.sizes[dimension]
        if index < -size or index >= size:
            raise ScientificDataError(f"index {index} is outside dimension {dimension} of size {size}")
    return variable.isel(indices)


def _sample_dataarray(variable: xr.DataArray) -> tuple[np.ndarray, bool]:
    sampled = False
    if variable.size > MAX_SAMPLE_VALUES:
        stride = max(1, math.ceil((variable.size / MAX_SAMPLE_VALUES) ** (1 / max(variable.ndim, 1))))
        variable = variable.isel({dimension: slice(None, None, stride) for dimension in variable.dims})
        sampled = True
    return np.asarray(variable.values), sampled


def statistics(path: Path, variable: str | None, band: int, indices: dict[str, int]) -> dict[str, Any]:
    kind = _format(path)
    if kind == "netcdf":
        with _open_netcdf(path) as dataset:
            selected_name = _choose_variable(dataset, variable)
            selected = _select_indices(dataset[selected_name], indices)
            values, sampled = _sample_dataarray(selected)
            stats = _finite_statistics(values.reshape(-1), values.size, 0)
            return _result(
                True,
                "statistics",
                format=kind,
                source={"name": path.name},
                selection={"variable": selected_name, "dimension_indices": indices},
                dimensions={name: int(size) for name, size in selected.sizes.items()},
                unit=selected.attrs.get("units"),
                statistics=stats,
                provenance={"cf_decoded": True, "mask_and_scale": True, "sampled": sampled},
                warnings=["statistics use a bounded stride sample"] if sampled else [],
            )
    with rasterio.open(path) as dataset:
        if band < 1 or band > dataset.count:
            raise ScientificDataError(f"band must be between 1 and {dataset.count}")
        scale = min(1.0, math.sqrt(MAX_SAMPLE_VALUES / (dataset.width * dataset.height)))
        height = max(1, int(dataset.height * scale))
        width = max(1, int(dataset.width * scale))
        data = dataset.read(band, out_shape=(height, width), masked=True, resampling=Resampling.nearest)
        values = np.asarray(data.compressed())
        stats = _finite_statistics(values, data.size, int(np.ma.count_masked(data)))
        return _result(
            True,
            "statistics",
            format=kind,
            source={"name": path.name},
            selection={"band": band},
            unit=dataset.units[band - 1] if dataset.units else None,
            statistics=stats,
            provenance={
                "mask_applied": True,
                "mask_flags": [flag.name for flag in dataset.mask_flag_enums[band - 1]],
                "nodata": dataset.nodatavals[band - 1],
                "sampled": height != dataset.height or width != dataset.width,
                "sample_shape": [height, width],
            },
            warnings=["statistics use a bounded raster sample"] if height != dataset.height or width != dataset.width else [],
        )


def _spatial_dimensions(variable: xr.DataArray) -> tuple[str, str] | None:
    roles = {dimension: _coord_role(dimension, variable.coords[dimension]) for dimension in variable.dims if dimension in variable.coords}
    x = next((dimension for dimension, role in roles.items() if role in {"longitude", "x"}), None)
    y = next((dimension for dimension, role in roles.items() if role in {"latitude", "y"}), None)
    if x and y:
        return y, x
    return tuple(variable.dims[-2:]) if variable.ndim == 2 else None


def visualize(path: Path, output_path: Path, variable: str | None, band: int, indices: dict[str, int]) -> dict[str, Any]:
    output_path = _validated_output(output_path)
    kind = _format(path)
    fig, ax = plt.subplots(figsize=(9, 6), constrained_layout=True)
    warnings: list[str] = []
    selection: dict[str, Any]
    if kind == "netcdf":
        with _open_netcdf(path) as dataset:
            selected_name = _choose_variable(dataset, variable)
            data = _select_indices(dataset[selected_name], indices)
            spatial = _spatial_dimensions(data)
            if not spatial or data.ndim != 2:
                raise ScientificDataError(
                    "visualization requires a two-dimensional selection; provide dimension_indices for non-spatial dimensions"
                )
            y_dimension, x_dimension = spatial
            data = data.transpose(y_dimension, x_dimension)
            values, sampled = _sample_dataarray(data)
            x = np.asarray(data.coords[x_dimension].values) if x_dimension in data.coords and data.coords[x_dimension].ndim == 1 else np.arange(values.shape[1])
            y = np.asarray(data.coords[y_dimension].values) if y_dimension in data.coords and data.coords[y_dimension].ndim == 1 else np.arange(values.shape[0])
            image = ax.pcolormesh(x, y, values, shading="auto", cmap="viridis")
            ax.set_xlabel(x_dimension)
            ax.set_ylabel(y_dimension)
            title = data.attrs.get("long_name") or selected_name
            unit = data.attrs.get("units")
            selection = {"variable": selected_name, "dimension_indices": indices}
            if sampled:
                warnings.append("visualization uses a bounded stride sample")
    else:
        with rasterio.open(path) as dataset:
            if band < 1 or band > dataset.count:
                raise ScientificDataError(f"band must be between 1 and {dataset.count}")
            scale = min(1.0, math.sqrt(MAX_SAMPLE_VALUES / (dataset.width * dataset.height)))
            height = max(1, int(dataset.height * scale))
            width = max(1, int(dataset.width * scale))
            data = dataset.read(band, out_shape=(height, width), masked=True, resampling=Resampling.nearest)
            bounds = dataset.bounds
            image = ax.imshow(data, extent=[bounds.left, bounds.right, bounds.bottom, bounds.top], origin="upper", cmap="viridis")
            ax.set_xlabel("x")
            ax.set_ylabel("y")
            title = dataset.descriptions[band - 1] or f"Band {band}"
            unit = dataset.units[band - 1] if dataset.units else None
            selection = {"band": band}
            if height != dataset.height or width != dataset.width:
                warnings.append("visualization uses a bounded raster sample")
    ax.set_title(str(title))
    colorbar = fig.colorbar(image, ax=ax)
    if unit:
        colorbar.set_label(str(unit))
    fig.savefig(output_path, dpi=150)
    plt.close(fig)
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise ScientificDataError("visualization artifact was not created")
    return _result(
        True,
        "visualize",
        format=kind,
        source={"name": path.name},
        selection=selection,
        artifacts=[{"path": str(output_path), "type": "image/png", "size_bytes": output_path.stat().st_size}],
        warnings=warnings,
    )


def visualize_netcdf_bundle(
    path: Path,
    output_dir: Path,
    variable: str | None,
    max_plots: int,
    indices: dict[str, int],
) -> dict[str, Any]:
    """Generate a small deterministic map bundle from one NetCDF file.

    The operator performs the common scientific decisions in one process: it
    identifies the data variable and spatial coordinates, selects representative
    non-spatial slices, and writes up to four bounded PNGs. It is intentionally
    limited to a single file and a 2-D spatial grid.
    """
    if _format(path) != "netcdf":
        raise ScientificDataError("NetCDF visualization bundle requires a NetCDF input")
    output_dir = _validated_output(output_dir / ".output-boundary").parent
    limit = max(1, min(int(max_plots), 4))
    with _open_netcdf(path) as dataset:
        selected_name = _choose_variable(dataset, variable)
        selected = _select_indices(dataset[selected_name], indices)
        spatial = _spatial_dimensions(selected)
        if not spatial:
            raise ScientificDataError("visualization requires recognizable latitude and longitude dimensions")
        y_dimension, x_dimension = spatial
        if selected.ndim < 2:
            raise ScientificDataError("visualization requires a two-dimensional spatial variable")
        other_dimensions = [dimension for dimension in selected.dims if dimension not in {y_dimension, x_dimension}]
        if len(other_dimensions) > 1:
            raise ScientificDataError(
                "visualization has multiple non-spatial dimensions; provide dimension_indices for all but one"
            )
        time_dimension = other_dimensions[0] if other_dimensions else None
        coordinate_x = np.asarray(selected.coords[x_dimension].values) if x_dimension in selected.coords else np.arange(selected.sizes[x_dimension])
        coordinate_y = np.asarray(selected.coords[y_dimension].values) if y_dimension in selected.coords else np.arange(selected.sizes[y_dimension])
        artifacts: list[dict[str, Any]] = []
        selections: list[dict[str, Any]] = []

        def render(data: xr.DataArray, stem: str, label: str, selection: dict[str, Any]) -> None:
            data = data.transpose(y_dimension, x_dimension)
            values, sampled = _sample_dataarray(data)
            output_path = output_dir / f"{stem}.png"
            fig, ax = plt.subplots(figsize=(9, 6), constrained_layout=True)
            image = ax.pcolormesh(coordinate_x, coordinate_y, values, shading="auto", cmap="viridis")
            ax.set_xlabel(x_dimension)
            ax.set_ylabel(y_dimension)
            ax.set_title(label)
            unit = data.attrs.get("units")
            if unit:
                fig.colorbar(image, ax=ax, label=str(unit))
            else:
                fig.colorbar(image, ax=ax)
            fig.savefig(output_path, dpi=150)
            plt.close(fig)
            if output_path.is_file() and output_path.stat().st_size:
                artifacts.append({"path": str(output_path), "type": "image/png", "size_bytes": output_path.stat().st_size})
                selections.append({**selection, "sampled": sampled})

        title = str(selected.attrs.get("long_name") or selected_name)
        if not time_dimension:
            render(selected, "spatial", title, {"variable": selected_name, "dimension_indices": indices})
        else:
            time_size = int(selected.sizes[time_dimension])
            mean_data = selected.mean(time_dimension, skipna=True)
            render(mean_data, "01_time_mean", f"{title} ({time_dimension} mean)", {"variable": selected_name, "reduction": {"dimension": time_dimension, "method": "mean"}})

            if len(artifacts) < limit:
                middle_index = time_size // 2
                middle = selected.isel({time_dimension: middle_index})
                coordinate_value = _json_value(selected.coords[time_dimension].values[middle_index])
                render(middle, "02_middle_slice", f"{title} ({time_dimension}={coordinate_value})", {"variable": selected_name, "dimension_indices": {time_dimension: middle_index}})

            latitude = selected.coords.get(y_dimension)
            if latitude is not None and _coord_role(y_dimension, latitude) == "latitude":
                weights = np.cos(np.deg2rad(latitude.astype("float64"))).clip(min=0)
                spatial_series = selected.weighted(weights).mean((y_dimension, x_dimension), skipna=True)
                weighting = "cosine_latitude"
            else:
                spatial_series = selected.mean((y_dimension, x_dimension), skipna=True)
                weighting = "unweighted"

            if len(artifacts) < limit:
                series_values = np.asarray(spatial_series.values, dtype="float64")
                series_x = np.asarray(spatial_series.coords[time_dimension].values)
                output_path = output_dir / "03_spatial_mean_series.png"
                fig, ax = plt.subplots(figsize=(10, 4.5), constrained_layout=True)
                ax.plot(series_x, series_values, linewidth=0.9, color="#256f91")
                ax.set_xlabel(time_dimension)
                ax.set_ylabel(str(selected.attrs.get("units") or selected_name))
                ax.set_title(f"{title} (spatial mean time series)")
                ax.grid(alpha=0.25)
                fig.savefig(output_path, dpi=150)
                plt.close(fig)
                artifacts.append({"path": str(output_path), "type": "image/png", "size_bytes": output_path.stat().st_size})
                selections.append({"variable": selected_name, "reduction": {"dimensions": [y_dimension, x_dimension], "method": "mean", "weighting": weighting}, "sampled": False})

            if len(artifacts) < limit:
                time_coordinate = spatial_series.coords[time_dimension]
                if np.issubdtype(time_coordinate.dtype, np.datetime64):
                    grouped = spatial_series.groupby(f"{time_dimension}.month").mean(time_dimension, skipna=True)
                    group_dimension = "month"
                else:
                    group_count = min(12, time_size)
                    edges = np.linspace(0, time_size, group_count + 1, dtype=int)
                    grouped = xr.DataArray(
                        [float(spatial_series.isel({time_dimension: slice(edges[index], edges[index + 1])}).mean(skipna=True)) for index in range(group_count)],
                        dims=("group",),
                        coords={"group": np.arange(1, group_count + 1)},
                    )
                    group_dimension = "group"
                output_path = output_dir / "04_grouped_time_mean.png"
                fig, ax = plt.subplots(figsize=(8.5, 4.5), constrained_layout=True)
                ax.bar(np.asarray(grouped.coords[group_dimension].values), np.asarray(grouped.values), color="#2a8068")
                ax.set_xlabel(group_dimension)
                ax.set_ylabel(str(selected.attrs.get("units") or selected_name))
                ax.set_title(f"{title} ({group_dimension} spatial mean)")
                fig.savefig(output_path, dpi=150)
                plt.close(fig)
                artifacts.append({"path": str(output_path), "type": "image/png", "size_bytes": output_path.stat().st_size})
                selections.append({"variable": selected_name, "reduction": {"dimensions": [time_dimension, y_dimension, x_dimension], "method": "grouped_mean", "group": group_dimension, "weighting": weighting}, "sampled": False})

        if not artifacts:
            raise ScientificDataError("visualization artifacts were not created")
        return _result(
            True,
            "visualize_bundle",
            format="netcdf",
            source={"name": path.name},
            variable=selected_name,
            spatial_dimensions={"latitude": y_dimension, "longitude": x_dimension},
            non_spatial_dimensions=other_dimensions,
            selections=selections,
            artifacts=artifacts,
            warnings=["representative slices were selected automatically"] if time_dimension else [],
        )


def aggregate(
    path: Path,
    variable: str | None,
    method: str,
    dimension: str,
    start: str | None,
    end: str | None,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Perform an explicit, labelled NetCDF reduction with no guessed dimension."""
    if _format(path) != "netcdf":
        raise ScientificDataError("aggregate currently requires a NetCDF input")
    methods = {"mean": "mean", "sum": "sum", "min": "min", "max": "max", "median": "median"}
    if method not in methods:
        raise ScientificDataError("method must be one of: " + ", ".join(methods))
    with _open_netcdf(path) as dataset:
        selected_name = _choose_variable(dataset, variable)
        selected = dataset[selected_name]
        if dimension not in selected.dims:
            raise ScientificDataError(f"dimension {dimension!r} is not present; available: {', '.join(selected.dims)}")
        if start or end:
            if dimension not in selected.coords:
                raise ScientificDataError(f"dimension {dimension!r} has no coordinate for range selection")
            coordinate = selected.coords[dimension]
            try:
                selected = selected.sel({dimension: slice(start, end)})
            except Exception as exc:
                raise ScientificDataError(f"unable to select {dimension} range: {exc}") from exc
        reduced = getattr(selected, methods[method])(dimension, skipna=True)
        values = np.asarray(reduced.values)
        if values.size > 1_000_000:
            values, sampled = _sample_dataarray(reduced)
        else:
            sampled = False
        artifacts = []
        if output_path is not None:
            output_path = _validated_output(output_path)
            reduced.to_dataset(name=selected_name).to_netcdf(output_path, engine="h5netcdf")
            artifacts.append({"path": str(output_path), "type": "application/x-netcdf", "size_bytes": output_path.stat().st_size})
        inline_values = _json_value(values) if values.size <= MAX_INLINE_VALUES else None
        return _result(
            True,
            "aggregate",
            format="netcdf",
            source={"name": path.name},
            selection={"variable": selected_name, "dimension": dimension, "start": start, "end": end},
            output_dimensions={name: int(size) for name, size in reduced.sizes.items()},
            unit=selected.attrs.get("units"),
            method=method,
            values=inline_values,
            coordinates={name: _json_value(coord.values) for name, coord in reduced.coords.items() if coord.ndim <= 1 and coord.size <= MAX_INLINE_VALUES},
            statistics=_finite_statistics(values.reshape(-1), values.size, 0),
            artifacts=artifacts,
            provenance={"cf_decoded": True, "mask_and_scale": True, "skipna": True, "sampled": sampled},
            warnings=["values omitted from the tool response; use the output artifact"] if values.size > MAX_INLINE_VALUES else [],
        )


def _coordinate_by_role(dataset: xr.Dataset, role: str) -> str | None:
    return next((name for name, variable in dataset.coords.items() if _coord_role(name, variable) == role), None)


def subset_netcdf(
    path: Path,
    output_path: Path,
    variable: str | None,
    bbox: list[float] | None,
    time_start: str | None,
    time_end: str | None,
    indices: dict[str, int],
) -> dict[str, Any]:
    if _format(path) != "netcdf":
        raise ScientificDataError("NetCDF subset requires a NetCDF input")
    output_path = _validated_output(output_path)
    with _open_netcdf(path) as dataset:
        selected_name = _choose_variable(dataset, variable)
        data = _select_indices(dataset[selected_name], indices)
        selections: dict[str, Any] = {"dimension_indices": indices}
        if time_start or time_end:
            time_name = _coordinate_by_role(dataset, "time")
            if not time_name or time_name not in data.dims:
                raise ScientificDataError("the selected variable has no recognized time coordinate")
            data = data.sel({time_name: slice(time_start, time_end)})
            selections["time"] = [time_start, time_end]
        if bbox:
            if len(bbox) != 4:
                raise ScientificDataError("bbox must contain west, south, east, north")
            lon_name = _coordinate_by_role(dataset, "longitude")
            lat_name = _coordinate_by_role(dataset, "latitude")
            if not lon_name or not lat_name or lon_name not in data.dims or lat_name not in data.dims:
                raise ScientificDataError("bbox subsetting requires one-dimensional latitude and longitude coordinates")
            west, south, east, north = bbox
            longitude = data.coords[lon_name]
            if float(longitude.min()) >= 0 and west < 0:
                west, east = west % 360, east % 360
            if west > east:
                raise ScientificDataError("dateline-crossing bbox is ambiguous; split it into two explicit subsets")
            lat_values = data.coords[lat_name]
            lat_slice = slice(south, north) if float(lat_values[0]) <= float(lat_values[-1]) else slice(north, south)
            lon_slice = slice(west, east) if float(longitude[0]) <= float(longitude[-1]) else slice(east, west)
            data = data.sel({lat_name: lat_slice, lon_name: lon_slice})
            selections["bbox"] = bbox
        if data.size == 0:
            raise ScientificDataError("the requested subset is empty")
        data.to_dataset(name=selected_name).to_netcdf(output_path, engine="h5netcdf")
        return _result(
            True,
            "subset",
            format="netcdf",
            source={"name": path.name},
            selection={"variable": selected_name, **selections},
            output_dimensions={name: int(size) for name, size in data.sizes.items()},
            artifacts=[{"path": str(output_path), "type": "application/x-netcdf", "size_bytes": output_path.stat().st_size}],
            provenance={"cf_decoded": True, "mask_and_scale": True},
            warnings=[],
        )


def convert_netcdf_to_geotiff(
    path: Path,
    output_path: Path,
    variable: str | None,
    indices: dict[str, int],
) -> dict[str, Any]:
    if _format(path) != "netcdf":
        raise ScientificDataError("NetCDF to GeoTIFF conversion requires a NetCDF input")
    output_path = _validated_output(output_path)
    with _open_netcdf(path) as dataset:
        selected_name = _choose_variable(dataset, variable)
        data = _select_indices(dataset[selected_name], indices)
        lon_name = _coordinate_by_role(dataset, "longitude")
        lat_name = _coordinate_by_role(dataset, "latitude")
        if not lon_name or not lat_name or data.ndim != 2 or set(data.dims) != {lat_name, lon_name}:
            raise ScientificDataError("conversion requires a two-dimensional selection on one-dimensional latitude/longitude coordinates")
        data = data.transpose(lat_name, lon_name)
        lon = np.asarray(data.coords[lon_name].values, dtype="float64")
        lat = np.asarray(data.coords[lat_name].values, dtype="float64")
        if lon.size < 2 or lat.size < 2 or not np.allclose(np.diff(lon), np.diff(lon)[0]) or not np.allclose(np.diff(lat), np.diff(lat)[0]):
            raise ScientificDataError("conversion requires regularly spaced latitude and longitude coordinates")
        values = np.asarray(data.values, dtype="float32")
        if lat[0] < lat[-1]:
            lat = lat[::-1]
            values = values[::-1]
        xres = abs(float(lon[1] - lon[0]))
        yres = abs(float(lat[1] - lat[0]))
        transform = rasterio.transform.from_origin(float(lon.min()) - xres / 2, float(lat.max()) + yres / 2, xres, yres)
        nodata = np.float32(-3.4028235e38)
        encoded = np.where(np.isfinite(values), values, nodata)
        with rasterio.open(output_path, "w", driver="GTiff", width=encoded.shape[1], height=encoded.shape[0], count=1, dtype="float32", crs="EPSG:4326", transform=transform, nodata=float(nodata), compress="deflate") as target:
            target.write(encoded, 1)
            target.set_band_description(1, selected_name)
            if data.attrs.get("units"):
                target.set_band_unit(1, str(data.attrs["units"]))
        return _result(
            True,
            "convert",
            format="geotiff",
            source={"name": path.name},
            selection={"variable": selected_name, "dimension_indices": indices},
            artifacts=[{"path": str(output_path), "type": "image/tiff", "size_bytes": output_path.stat().st_size}],
            provenance={"source_crs": "EPSG:4326", "mask_and_scale": True, "regular_grid_verified": True},
            warnings=[],
        )


def transform_raster(
    path: Path,
    output_path: Path,
    target_crs: str | None,
    resolution: float | None,
    bbox: list[float] | None,
    resampling: str,
) -> dict[str, Any]:
    if _format(path) != "geotiff":
        raise ScientificDataError("raster transform requires a GeoTIFF input")
    output_path = _validated_output(output_path)
    methods = {"nearest": Resampling.nearest, "bilinear": Resampling.bilinear, "cubic": Resampling.cubic, "average": Resampling.average}
    if resampling not in methods:
        raise ScientificDataError("resampling must be nearest, bilinear, cubic, or average")
    if resolution is not None and resolution <= 0:
        raise ScientificDataError("resolution must be positive")
    from rasterio.warp import calculate_default_transform, reproject
    from rasterio.windows import from_bounds
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ScientificDataError("raster transform requires a declared source CRS")
        window = None
        source_transform = source.transform
        source_width, source_height = source.width, source.height
        if bbox:
            if len(bbox) != 4:
                raise ScientificDataError("bbox must contain left, bottom, right, top in the source CRS")
            window = from_bounds(*bbox, transform=source.transform).round_offsets().round_lengths()
            window = window.intersection(rasterio.windows.Window(0, 0, source.width, source.height))
            source_transform = source.window_transform(window)
            source_width, source_height = int(window.width), int(window.height)
        destination_crs = target_crs or source.crs
        left, bottom, right, top = rasterio.transform.array_bounds(source_height, source_width, source_transform)
        destination_transform, width, height = calculate_default_transform(
            source.crs, destination_crs, source_width, source_height, left, bottom, right, top,
            resolution=resolution,
        )
        profile = source.profile.copy()
        profile.update(driver="GTiff", crs=destination_crs, transform=destination_transform, width=width, height=height, compress="deflate")
        with rasterio.open(output_path, "w", **profile) as target:
            for band in range(1, source.count + 1):
                reproject(
                    source=rasterio.band(source, band) if window is None else source.read(band, window=window),
                    destination=rasterio.band(target, band),
                    src_transform=source.transform if window is None else source_transform,
                    src_crs=source.crs,
                    src_nodata=source.nodatavals[band - 1],
                    dst_transform=destination_transform,
                    dst_crs=destination_crs,
                    dst_nodata=source.nodatavals[band - 1],
                    resampling=methods[resampling],
                )
                if source.descriptions[band - 1]:
                    target.set_band_description(band, source.descriptions[band - 1])
                if source.units and source.units[band - 1]:
                    target.set_band_unit(band, source.units[band - 1])
        return _result(
            True,
            "transform",
            format="geotiff",
            source={"name": path.name},
            selection={"bbox": bbox},
            output={"crs": str(destination_crs), "shape": [height, width], "resolution": list(destination_transform)[0:5:4]},
            artifacts=[{"path": str(output_path), "type": "image/tiff", "size_bytes": output_path.stat().st_size}],
            provenance={"source_crs": str(source.crs), "resampling": resampling},
            warnings=[],
        )


def raster_index(path: Path, output_path: Path, index_name: str, bands: dict[str, int]) -> dict[str, Any]:
    if _format(path) != "geotiff":
        raise ScientificDataError("raster index requires a GeoTIFF input")
    output_path = _validated_output(output_path)
    formulas = {
        "ndvi": ("(nir-red)/(nir+red)", {"nir", "red"}),
        "evi": ("2.5*(nir-red)/(nir+6*red-7.5*blue+1)", {"nir", "red", "blue"}),
        "ndwi": ("(green-nir)/(green+nir)", {"green", "nir"}),
        "nbr": ("(nir-swir2)/(nir+swir2)", {"nir", "swir2"}),
    }
    normalized = index_name.lower()
    if normalized not in formulas:
        raise ScientificDataError("index_name must be ndvi, evi, ndwi, or nbr")
    expression, required = formulas[normalized]
    if set(bands) != required or any(not isinstance(value, int) or value < 1 for value in bands.values()):
        raise ScientificDataError("bands must contain exactly: " + ", ".join(sorted(required)))
    with rasterio.open(path) as source:
        if any(value > source.count for value in bands.values()):
            raise ScientificDataError(f"band index exceeds source band count {source.count}")
        data = {name: source.read(index, masked=True).astype("float64") for name, index in bands.items()}
        with np.errstate(divide="ignore", invalid="ignore"):
            if normalized == "ndvi":
                result = (data["nir"] - data["red"]) / (data["nir"] + data["red"])
            elif normalized == "evi":
                result = 2.5 * (data["nir"] - data["red"]) / (data["nir"] + 6 * data["red"] - 7.5 * data["blue"] + 1)
            elif normalized == "ndwi":
                result = (data["green"] - data["nir"]) / (data["green"] + data["nir"])
            else:
                result = (data["nir"] - data["swir2"]) / (data["nir"] + data["swir2"])
        result = np.ma.masked_invalid(result)
        profile = source.profile.copy()
        profile.update(driver="GTiff", count=1, dtype="float32", nodata=-9999.0, compress="deflate")
        with rasterio.open(output_path, "w", **profile) as target:
            target.write(result.filled(profile["nodata"]).astype("float32"), 1)
            target.set_band_description(1, normalized.upper())
            target.set_band_unit(1, "1")
        return _result(True, "raster_index", format="geotiff", source={"name": path.name}, selection={"index": normalized, "bands": bands}, formula=expression, artifacts=[{"path": str(output_path), "type": "image/tiff", "size_bytes": output_path.stat().st_size}], provenance={"source_masks_applied": True}, warnings=[])


def terrain(path: Path, output_path: Path, operation: str, band: int) -> dict[str, Any]:
    if _format(path) != "geotiff":
        raise ScientificDataError("terrain operation requires a GeoTIFF DEM")
    if operation not in {"slope", "aspect"}:
        raise ScientificDataError("operation must be slope or aspect")
    output_path = _validated_output(output_path)
    with rasterio.open(path) as source:
        if band < 1 or band > source.count:
            raise ScientificDataError(f"band must be between 1 and {source.count}")
        if source.crs and source.crs.is_geographic:
            raise ScientificDataError("terrain derivatives require projected linear units; reproject the DEM before computing slope or aspect")
        values = source.read(band, masked=True).astype("float64")
        xres, yres = source.res
        if xres <= 0 or yres <= 0:
            raise ScientificDataError("DEM resolution must be positive")
        gradient_y, gradient_x = np.gradient(values.filled(np.nan), yres, xres)
        valid = ~np.isnan(gradient_x) & ~np.isnan(gradient_y) & ~np.ma.getmaskarray(values)
        if operation == "slope":
            result = np.degrees(np.arctan(np.hypot(gradient_x, gradient_y)))
            unit = "degree"
        else:
            result = (np.degrees(np.arctan2(-gradient_x, gradient_y)) + 360) % 360
            unit = "degree clockwise from north"
        result = np.ma.array(result, mask=~valid)
        profile = source.profile.copy()
        profile.update(driver="GTiff", count=1, dtype="float32", nodata=-9999.0, compress="deflate")
        with rasterio.open(output_path, "w", **profile) as target:
            target.write(result.filled(profile["nodata"]).astype("float32"), 1)
            target.set_band_description(1, operation)
            target.set_band_unit(1, unit)
        return _result(True, "terrain", format="geotiff", source={"name": path.name}, selection={"operation": operation, "band": band}, artifacts=[{"path": str(output_path), "type": "image/tiff", "size_bytes": output_path.stat().st_size}], provenance={"source_crs": str(source.crs) if source.crs else None, "cell_resolution": [xres, yres]}, warnings=[])
def _indices(raw: str) -> dict[str, int]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScientificDataError(f"invalid dimension_indices JSON: {exc}") from exc
    if not isinstance(value, dict) or any(not isinstance(key, str) or not isinstance(item, int) for key, item in value.items()):
        raise ScientificDataError("dimension_indices must be a JSON object of integer indices")
    return value


def _bbox(raw: str | None) -> list[float] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ScientificDataError(f"invalid bbox JSON: {exc}") from exc
    if not isinstance(value, list) or len(value) != 4 or any(not isinstance(item, (int, float)) for item in value):
        raise ScientificDataError("bbox must be a JSON array of four numbers")
    return [float(item) for item in value]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for operation in ("inspect", "statistics", "aggregate", "subset", "convert", "transform", "raster-index", "terrain", "visualize", "visualize-bundle"):
        command = subparsers.add_parser(operation)
        command.add_argument("input_path", type=Path)
        if operation in {"statistics", "aggregate", "subset", "convert", "visualize", "visualize-bundle"}:
            command.add_argument("--variable")
        if operation == "aggregate":
            command.add_argument("--method", required=True)
            command.add_argument("--dimension", required=True)
            command.add_argument("--start")
            command.add_argument("--end")
            command.add_argument("--output", type=Path)
        if operation in {"statistics", "subset", "convert", "visualize", "visualize-bundle"}:
            if operation in {"statistics", "visualize"}:
                command.add_argument("--band", type=int, default=1)
            command.add_argument("--dimension-indices", default="{}")
        if operation == "subset":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--bbox")
            command.add_argument("--time-start")
            command.add_argument("--time-end")
        if operation == "convert":
            command.add_argument("--output", type=Path, required=True)
        if operation == "transform":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--target-crs")
            command.add_argument("--resolution", type=float)
            command.add_argument("--bbox")
            command.add_argument("--resampling", default="nearest")
        if operation == "raster-index":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--index", required=True)
            command.add_argument("--bands", required=True)
        if operation == "terrain":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--terrain-operation", required=True)
            command.add_argument("--band", type=int, default=1)
        if operation == "visualize":
            command.add_argument("--output", type=Path, required=True)
        if operation == "visualize-bundle":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--max-plots", type=int, default=4)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not args.input_path.is_file():
            raise ScientificDataError("input_path must be an existing file")
        if args.operation == "inspect":
            payload = inspect(args.input_path)
        elif args.operation == "statistics":
            payload = statistics(args.input_path, args.variable, args.band, _indices(args.dimension_indices))
        elif args.operation == "aggregate":
            payload = aggregate(args.input_path, args.variable, args.method, args.dimension, args.start, args.end, args.output)
        elif args.operation == "subset":
            payload = subset_netcdf(args.input_path, args.output, args.variable, _bbox(args.bbox), args.time_start, args.time_end, _indices(args.dimension_indices))
        elif args.operation == "convert":
            payload = convert_netcdf_to_geotiff(args.input_path, args.output, args.variable, _indices(args.dimension_indices))
        elif args.operation == "transform":
            payload = transform_raster(args.input_path, args.output, args.target_crs, args.resolution, _bbox(args.bbox), args.resampling)
        elif args.operation == "raster-index":
            payload = raster_index(args.input_path, args.output, args.index, _indices(args.bands))
        elif args.operation == "terrain":
            payload = terrain(args.input_path, args.output, args.terrain_operation, args.band)
        elif args.operation == "visualize":
            payload = visualize(args.input_path, args.output, args.variable, args.band, _indices(args.dimension_indices))
        else:
            payload = visualize_netcdf_bundle(args.input_path, args.output, args.variable, args.max_plots, _indices(args.dimension_indices))
    except Exception as exc:
        payload = _result(False, args.operation, error=f"{type(exc).__name__}: {exc}")
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
