#!/usr/bin/env python3
"""Bounded geoscience operators used by the extensible Tool plugins."""
from __future__ import annotations

import argparse, json, math, os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import rasterio
from rasterio.enums import Resampling
from rasterio.mask import mask as raster_mask
from rasterio.merge import merge
from rasterio.warp import reproject
import xarray as xr
import geopandas as gpd
from rasterio.features import rasterize

MAX_VALUES = 20_000_000

class GeoScienceError(RuntimeError):
    pass

def json_value(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int)): return v
    if isinstance(v, (float, np.floating)): return None if not math.isfinite(float(v)) else float(v)
    if isinstance(v, np.integer): return int(v)
    if isinstance(v, (np.ndarray, list, tuple)): return [json_value(x) for x in v]
    if isinstance(v, dict): return {str(k): json_value(x) for k, x in v.items()}
    return str(v)

def result(operation: str, **payload: Any) -> dict[str, Any]:
    return json_value({"success": True, "operation": operation, **payload})

def output_path(raw: str) -> Path:
    root = Path(os.environ.get("AI_DATASEEK_OUTPUT_ROOT", "/home/ubuntu/output")).resolve()
    path = Path(raw).resolve()
    try: path.relative_to(root)
    except ValueError as exc: raise GeoScienceError(f"output path must be below {root}") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def open_nc(path: Path) -> xr.Dataset:
    errors = []
    for engine in ("h5netcdf", "netcdf4", None):
        try:
            kw = {"decode_cf": True, "mask_and_scale": True}
            if engine: kw["engine"] = engine
            return xr.open_dataset(path, **kw)
        except Exception as exc: errors.append(f"{engine or 'default'}: {exc}")
    raise GeoScienceError("unable to open NetCDF: " + "; ".join(errors))

def choose_var(ds: xr.Dataset, name: str | None) -> str:
    if name:
        if name not in ds.data_vars: raise GeoScienceError(f"unknown variable: {name}")
        return name
    candidates = [n for n, v in ds.data_vars.items() if np.issubdtype(v.dtype, np.number) and v.ndim]
    if len(candidates) != 1: raise GeoScienceError("variable is required when multiple numeric variables exist")
    return candidates[0]

def role(name: str, v: xr.DataArray) -> str | None:
    n, std, axis = name.lower(), str(v.attrs.get("standard_name", "")).lower(), str(v.attrs.get("axis", "")).upper()
    if axis == "T" or std == "time" or n in {"time", "date", "datetime"}: return "time"
    if axis == "X" or std == "longitude" or n in {"lon", "longitude", "x"}: return "longitude"
    if axis == "Y" or std == "latitude" or n in {"lat", "latitude", "y"}: return "latitude"
    return None

def collection_inspect(args: argparse.Namespace) -> dict[str, Any]:
    paths = sorted(Path(args.input_dir).rglob("*") if args.input_dir else [Path(p) for p in args.input_paths])
    files = []
    for p in paths:
        if not p.is_file() or p.suffix.lower() not in {".nc", ".nc4", ".cdf", ".tif", ".tiff"}: continue
        item: dict[str, Any] = {"name": p.name, "suffix": p.suffix.lower(), "size_bytes": p.stat().st_size}
        if p.suffix.lower() in {".nc", ".nc4", ".cdf"}:
            with open_nc(p) as ds:
                item.update({"format": "netcdf", "dimensions": {k: int(v) for k, v in ds.sizes.items()}, "variables": list(ds.data_vars), "coordinates": list(ds.coords)})
                times = [n for n, v in ds.coords.items() if role(n, v) == "time"]
                if times: item["time_range"] = [str(ds[times[0]].values.min()), str(ds[times[0]].values.max())]
        else:
            with rasterio.open(p) as src: item.update({"format": "geotiff", "width": src.width, "height": src.height, "count": src.count, "crs": str(src.crs) if src.crs else None, "bounds": list(src.bounds), "resolution": list(src.res)})
        files.append(item)
    if not files: raise GeoScienceError("no supported NetCDF or GeoTIFF files found")
    return result("collection_inspect", file_count=len(files), files=files)

def coordinate_normalize(args: argparse.Namespace) -> dict[str, Any]:
    path, out = Path(args.input_path), output_path(args.output_path)
    with open_nc(path) as ds:
        rename = {}
        for n, v in ds.coords.items():
            r = role(n, v)
            if r and r not in ds.coords: rename[n] = r
        if rename: ds = ds.rename(rename)
        if "longitude" in ds.coords:
            lon = ds.longitude
            if float(lon.max()) > 180: ds = ds.assign_coords(longitude=((lon + 180) % 360) - 180).sortby("longitude")
        for r in ("latitude", "longitude"):
            if r in ds.coords and ds[r].ndim == 1 and ds[r].size > 1 and float(ds[r][1] - ds[r][0]) < 0: ds = ds.sortby(r)
        ds.to_netcdf(out)
        return result("coordinate_normalize", source=path.name, artifacts=[{"path": str(out), "type": "application/x-netcdf", "size_bytes": out.stat().st_size}], coordinates=list(ds.coords))

def grid_signature(path: Path) -> dict[str, Any]:
    if path.suffix.lower() in {".tif", ".tiff"}:
        with rasterio.open(path) as s: return {"format": "geotiff", "shape": [s.height, s.width], "crs": str(s.crs), "transform": list(s.transform), "bounds": list(s.bounds)}
    with open_nc(path) as ds:
        dims = {k: int(v) for k, v in ds.sizes.items()}
        coords = {}
        for n, v in ds.coords.items():
            if v.ndim == 1 and role(n, v) in {"latitude", "longitude"}: coords[role(n, v)] = {"name": n, "size": int(v.size), "first": json_value(v.values[0]), "last": json_value(v.values[-1])}
        return {"format": "netcdf", "dimensions": dims, "coordinates": coords}

def grid_compare(args: argparse.Namespace) -> dict[str, Any]:
    sigs = [grid_signature(Path(p)) for p in args.input_paths]
    compatible = all(s == sigs[0] for s in sigs[1:])
    return result("grid_compare", compatible=compatible, signatures=sigs, differences=[] if compatible else ["grid signatures differ"])

def quality_check(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.input_path); checks: dict[str, Any] = {}
    if path.suffix.lower() in {".tif", ".tiff"}:
        with rasterio.open(path) as s:
            arr = s.read(1, masked=True); vals = arr.compressed(); checks.update({"shape": [s.height, s.width], "nodata_count": int(np.ma.getmaskarray(arr).sum()), "valid_count": int(vals.size), "minimum": json_value(np.min(vals)) if vals.size else None, "maximum": json_value(np.max(vals)) if vals.size else None})
    else:
        with open_nc(path) as ds:
            for n in ds.data_vars:
                v = ds[n]
                if not np.issubdtype(v.dtype, np.number): continue
                if v.size > MAX_VALUES: checks[n] = {"size": int(v.size), "sampled": True}; continue
                a = np.asarray(v.values); finite = a[np.isfinite(a)]
                checks[n] = {"size": int(v.size), "missing_count": int(np.isnan(a).sum()), "minimum": json_value(np.min(finite)) if finite.size else None, "maximum": json_value(np.max(finite)) if finite.size else None, "units": v.attrs.get("units")}
    return result("quality_check", source=path.name, checks=checks)

def unit_convert(args: argparse.Namespace) -> dict[str, Any]:
    path, out = Path(args.input_path), output_path(args.output_path)
    factors = {("kelvin", "celsius"): (1.0, -273.15), ("k", "c"): (1.0, -273.15), ("m", "mm"): (1000.0, 0.0), ("mm", "m"): (0.001, 0.0)}
    key = (args.from_unit.lower(), args.to_unit.lower())
    if key not in factors: raise GeoScienceError(f"unsupported conversion: {args.from_unit} to {args.to_unit}")
    factor, offset = factors[key]
    with open_nc(path) as ds:
        var = choose_var(ds, args.variable); ds[var] = ds[var] * factor + offset; ds[var].attrs["units"] = args.to_unit; ds.to_netcdf(out)
    return result("unit_convert", variable=var, from_unit=args.from_unit, to_unit=args.to_unit, artifacts=[{"path": str(out), "type": "application/x-netcdf", "size_bytes": out.stat().st_size}])

def raster_stack(args: argparse.Namespace) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]; out = output_path(args.output_path)
    with rasterio.open(paths[0]) as first:
        profile = first.profile.copy(); profile.update(count=len(paths), compress="deflate")
        with rasterio.open(out, "w", **profile) as dst:
            for i, p in enumerate(paths, 1):
                with rasterio.open(p) as src: dst.write(src.read(1), i); dst.set_band_description(i, p.stem)
    return result("raster_stack", input_count=len(paths), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def raster_mosaic(args: argparse.Namespace) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]
    with rasterio.open(paths[0]) as first:
        data, transform = merge([rasterio.open(p) for p in paths]); profile = first.profile.copy(); profile.update(height=data.shape[1], width=data.shape[2], transform=transform, count=data.shape[0], compress="deflate")
    out = output_path(args.output_path)
    with rasterio.open(out, "w", **profile) as dst: dst.write(data)
    return result("raster_mosaic", input_count=len(paths), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def sample_raster(args: argparse.Namespace) -> dict[str, Any]:
    with rasterio.open(args.input_path) as src:
        points = json.loads(args.points); values = [{"x": p[0], "y": p[1], "values": [json_value(v) for v in next(src.sample([p]))]} for p in points]
    return result("sample_raster", samples=values, crs=str(src.crs) if src.crs else None)

def qa_mask(args: argparse.Namespace) -> dict[str, Any]:
    path, out = Path(args.input_path), output_path(args.output_path); bit = int(args.bit)
    with rasterio.open(path) as src:
        data = src.read(); qa = data[0]; mask = ((qa.astype("uint64") >> bit) & 1) == 0; profile = src.profile.copy(); profile.update(count=data.shape[0], nodata=0)
        with rasterio.open(out, "w", **profile) as dst: dst.write(np.where(mask, data, 0).astype(data.dtype))
    return result("qa_mask", bit=bit, valid_fraction=float(mask.mean()), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def scene_composite(args: argparse.Namespace) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]; out = output_path(args.output_path)
    with rasterio.open(paths[0]) as first:
        arrays = []
        for p in paths:
            with rasterio.open(p) as src: arrays.append(src.read(1, masked=True))
        data = np.ma.median(np.ma.stack(arrays), axis=0).filled(first.nodata if first.nodata is not None else 0); profile = first.profile.copy(); profile.update(count=1, compress="deflate")
        with rasterio.open(out, "w", **profile) as dst: dst.write(data.astype(profile["dtype"]), 1)
    return result("scene_composite", input_count=len(paths), method="median", artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def temporal_stat(args: argparse.Namespace, mode: str) -> dict[str, Any]:
    paths = [Path(p) for p in args.input_paths]; out = output_path(args.output_path)
    with xr.open_mfdataset(paths, combine="by_coords", engine="h5netcdf") as ds:
        var = choose_var(ds, args.variable); arr = ds[var]
        if "time" not in arr.dims: raise GeoScienceError("time dimension is required")
        if mode == "climatology": value = arr.groupby("time.month").mean("time", skipna=True)
        elif mode == "anomaly": value = arr - arr.groupby("time.month").mean("time", skipna=True)
        else:
            t = np.arange(arr.sizes["time"], dtype=float); value = xr.DataArray(np.polyfit(t, arr.mean(dim=[d for d in arr.dims if d != "time"], skipna=True), 1)[0], name="trend")
        value.to_netcdf(out)
    return result(mode, variable=var, artifacts=[{"path": str(out), "type": "application/x-netcdf", "size_bytes": out.stat().st_size}])

def artifact_validate(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.input_path); checks = {"exists": path.is_file(), "size_bytes": path.stat().st_size if path.is_file() else 0}
    if not path.is_file() or path.stat().st_size == 0: raise GeoScienceError("artifact is missing or empty")
    try:
        if path.suffix.lower() in {".tif", ".tiff"}:
            with rasterio.open(path) as s: checks.update({"readable": True, "format": "geotiff", "shape": [s.height, s.width]})
        elif path.suffix.lower() in {".nc", ".nc4", ".cdf"}:
            with open_nc(path) as ds: checks.update({"readable": True, "format": "netcdf", "variables": list(ds.data_vars)})
        else: checks.update({"readable": True, "format": path.suffix.lower()})
    except Exception as exc: raise GeoScienceError(f"artifact cannot be read: {exc}") from exc
    return result("artifact_validate", artifact=str(path), checks=checks)

def vector_inspect(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.input_path)
    return result("vector_inspect", feature_count=len(gdf), crs=str(gdf.crs) if gdf.crs else None, geometry_types=sorted(set(gdf.geometry.geom_type.dropna())), bounds=list(gdf.total_bounds), columns=list(gdf.columns))

def vector_transform(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.input_path)
    if args.target_crs: gdf = gdf.to_crs(args.target_crs)
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    gdf["geometry"] = gdf.geometry.make_valid()
    out = output_path(args.output_path); gdf.to_file(out, driver="GeoJSON")
    return result("vector_transform", feature_count=len(gdf), crs=str(gdf.crs) if gdf.crs else None, artifacts=[{"path": str(out), "type": "application/geo+json", "size_bytes": out.stat().st_size}])

def zonal_statistics(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.vector_path)
    rows = []
    with rasterio.open(args.raster_path) as src:
        if gdf.crs and src.crs and gdf.crs != src.crs: gdf = gdf.to_crs(src.crs)
        for idx, geom in enumerate(gdf.geometry):
            if geom is None or geom.is_empty: rows.append({"feature": idx, "count": 0}); continue
            data, _ = raster_mask(src, [geom.__geo_interface__], crop=False, filled=False)
            values = data[0].compressed()
            rows.append({"feature": idx, "count": int(values.size), "mean": json_value(np.mean(values)) if values.size else None, "minimum": json_value(np.min(values)) if values.size else None, "maximum": json_value(np.max(values)) if values.size else None})
    return result("zonal_statistics", zones=rows, zone_count=len(rows), raster=Path(args.raster_path).name)

def rasterize_vector(args: argparse.Namespace) -> dict[str, Any]:
    gdf = gpd.read_file(args.vector_path)
    with rasterio.open(args.reference_raster) as ref:
        shapes = [(geom, 1) for geom in gdf.geometry if geom is not None and not geom.is_empty]
        arr = rasterize(shapes, out_shape=(ref.height, ref.width), transform=ref.transform, fill=0, dtype="uint8")
        profile = ref.profile.copy(); profile.update(count=1, dtype="uint8", nodata=0, compress="deflate")
    out = output_path(args.output_path)
    with rasterio.open(out, "w", **profile) as dst: dst.write(arr, 1)
    return result("rasterize_vector", feature_count=len(shapes), artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def grid_align(args: argparse.Namespace) -> dict[str, Any]:
    out = output_path(args.output_path)
    methods = {"nearest": Resampling.nearest, "bilinear": Resampling.bilinear, "cubic": Resampling.cubic, "average": Resampling.average}
    with rasterio.open(args.reference_path) as ref, rasterio.open(args.input_path) as src:
        profile = src.profile.copy(); profile.update(crs=ref.crs, transform=ref.transform, width=ref.width, height=ref.height, compress="deflate")
        with rasterio.open(out, "w", **profile) as dst:
            for band in range(1, src.count + 1):
                reproject(rasterio.band(src, band), rasterio.band(dst, band), src_transform=src.transform, src_crs=src.crs, dst_transform=ref.transform, dst_crs=ref.crs, resampling=methods[args.resampling])
    return result("grid_align", reference=Path(args.reference_path).name, resampling=args.resampling, artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def remote_sensing_product_inspect(args: argparse.Namespace) -> dict[str, Any]:
    path = Path(args.input_path)
    with rasterio.open(path) as src:
        metadata = {**src.tags()}; descriptions = list(src.descriptions); units = list(src.units)
        probe = (path.name + " " + " ".join(f"{k}={v}" for k, v in metadata.items())).lower()
        platform = next((p for p in ("sentinel-2", "sentinel-1", "landsat", "modis", "viirs") if p.replace("-", "") in probe.replace("-", "")), None)
        bands = [{"index": i, "description": descriptions[i-1], "unit": units[i-1]} for i in range(1, src.count + 1)]
    return result("remote_sensing_product_inspect", source=path.name, platform=platform, bands=bands, metadata=metadata, warnings=[] if platform else ["platform could not be identified from embedded metadata"])

def change_detection(args: argparse.Namespace) -> dict[str, Any]:
    out = output_path(args.output_path)
    with rasterio.open(args.before_path) as before, rasterio.open(args.after_path) as after:
        if (before.crs, before.transform, before.shape) != (after.crs, after.transform, after.shape): raise GeoScienceError("input rasters must be grid-aligned before change detection")
        a, b = before.read(args.band, masked=True).astype("float64"), after.read(args.band, masked=True).astype("float64")
        diff = b - a; profile = before.profile.copy(); profile.update(count=1, dtype="float32", nodata=-9999.0, compress="deflate")
        with rasterio.open(out, "w", **profile) as dst: dst.write(diff.filled(-9999.0).astype("float32"), 1)
    vals = diff.compressed()
    return result("change_detection", mean_change=json_value(np.mean(vals)) if vals.size else None, minimum_change=json_value(np.min(vals)) if vals.size else None, maximum_change=json_value(np.max(vals)) if vals.size else None, artifacts=[{"path": str(out), "type": "image/tiff", "size_bytes": out.stat().st_size}])

def spatial_join(args: argparse.Namespace) -> dict[str, Any]:
    left, right = gpd.read_file(args.left_path), gpd.read_file(args.right_path)
    if left.crs and right.crs and left.crs != right.crs: right = right.to_crs(left.crs)
    joined = gpd.sjoin(left, right, how=args.how, predicate=args.predicate)
    out = output_path(args.output_path); joined.to_file(out, driver="GeoJSON")
    return result("spatial_join", feature_count=len(joined), predicate=args.predicate, artifacts=[{"path": str(out), "type": "application/geo+json", "size_bytes": out.stat().st_size}])

def transect_profile(args: argparse.Namespace) -> dict[str, Any]:
    points = json.loads(args.points)
    if len(points) < 2: raise GeoScienceError("transect requires at least two points")
    distances = np.linspace(0.0, 1.0, args.samples); segments = np.asarray(points, dtype=float)
    lengths = np.sqrt(((segments[1:] - segments[:-1]) ** 2).sum(axis=1)); cumulative = np.concatenate([[0.0], np.cumsum(lengths)]); total = cumulative[-1]
    coords = []
    for fraction in distances:
        target = fraction * total; index = min(int(np.searchsorted(cumulative, target, side="right") - 1), len(lengths)-1); local = 0.0 if lengths[index] == 0 else (target-cumulative[index])/lengths[index]; coords.append((segments[index] + local*(segments[index+1]-segments[index])).tolist())
    with rasterio.open(args.input_path) as src: values = [json_value(v[args.band-1]) for v in src.sample(coords)]
    return result("transect_profile", distances=json_value(distances * total), coordinates=coords, values=values, band=args.band)

def main() -> int:
    p = argparse.ArgumentParser(); sub = p.add_subparsers(dest="op", required=True)
    def common(name: str, input_paths=True):
        c = sub.add_parser(name)
        if input_paths: c.add_argument("input_paths", nargs="+")
        return c
    c = sub.add_parser("collection-inspect"); c.add_argument("--input-dir"); c.add_argument("--input-paths", nargs="*", default=[])
    c = sub.add_parser("coordinate-normalize"); c.add_argument("input_path"); c.add_argument("output_path")
    common("grid-compare"); common("raster-stack").add_argument("output_path"); common("raster-mosaic").add_argument("output_path")
    c = sub.add_parser("quality-check"); c.add_argument("input_path")
    c = sub.add_parser("unit-convert"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("from_unit"); c.add_argument("to_unit"); c.add_argument("--variable")
    c = sub.add_parser("sample-raster"); c.add_argument("input_path"); c.add_argument("points")
    c = sub.add_parser("qa-mask"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("bit", type=int)
    common("scene-composite").add_argument("output_path")
    for name in ("climatology", "anomaly", "trend"):
        c = common(name); c.add_argument("output_path"); c.add_argument("--variable")
    c = sub.add_parser("artifact-validate"); c.add_argument("input_path")
    c = sub.add_parser("vector-inspect"); c.add_argument("input_path")
    c = sub.add_parser("vector-transform"); c.add_argument("input_path"); c.add_argument("output_path"); c.add_argument("--target-crs")
    c = sub.add_parser("zonal-statistics"); c.add_argument("raster_path"); c.add_argument("vector_path")
    c = sub.add_parser("rasterize-vector"); c.add_argument("reference_raster"); c.add_argument("vector_path"); c.add_argument("output_path")
    c = sub.add_parser("grid-align"); c.add_argument("input_path"); c.add_argument("reference_path"); c.add_argument("output_path"); c.add_argument("--resampling", choices=("nearest","bilinear","cubic","average"), default="nearest")
    c = sub.add_parser("remote-product-inspect"); c.add_argument("input_path")
    c = sub.add_parser("change-detection"); c.add_argument("before_path"); c.add_argument("after_path"); c.add_argument("output_path"); c.add_argument("--band", type=int, default=1)
    c = sub.add_parser("spatial-join"); c.add_argument("left_path"); c.add_argument("right_path"); c.add_argument("output_path"); c.add_argument("--predicate", choices=("intersects","within","contains","touches"), default="intersects"); c.add_argument("--how", choices=("left","inner"), default="left")
    c = sub.add_parser("transect-profile"); c.add_argument("input_path"); c.add_argument("points"); c.add_argument("--samples", type=int, default=100); c.add_argument("--band", type=int, default=1)
    a = p.parse_args()
    try:
        if a.op == "collection-inspect": r = collection_inspect(a)
        elif a.op == "coordinate-normalize": r = coordinate_normalize(a)
        elif a.op == "grid-compare": r = grid_compare(a)
        elif a.op == "quality-check": r = quality_check(a)
        elif a.op == "unit-convert": r = unit_convert(a)
        elif a.op == "raster-stack": r = raster_stack(a)
        elif a.op == "raster-mosaic": r = raster_mosaic(a)
        elif a.op == "sample-raster": r = sample_raster(a)
        elif a.op == "qa-mask": r = qa_mask(a)
        elif a.op == "scene-composite": r = scene_composite(a)
        elif a.op in {"climatology", "anomaly", "trend"}: r = temporal_stat(a, a.op)
        elif a.op == "vector-inspect": r = vector_inspect(a)
        elif a.op == "vector-transform": r = vector_transform(a)
        elif a.op == "zonal-statistics": r = zonal_statistics(a)
        elif a.op == "rasterize-vector": r = rasterize_vector(a)
        elif a.op == "grid-align": r = grid_align(a)
        elif a.op == "remote-product-inspect": r = remote_sensing_product_inspect(a)
        elif a.op == "change-detection": r = change_detection(a)
        elif a.op == "spatial-join": r = spatial_join(a)
        elif a.op == "transect-profile": r = transect_profile(a)
        else: r = artifact_validate(a)
    except Exception as exc:
        r = {"success": False, "operation": a.op, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(r, ensure_ascii=False, allow_nan=False)); return 0 if r["success"] else 1

if __name__ == "__main__": raise SystemExit(main())
