#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# datetime.UTC was added in Python 3.11, while the analysis sandbox runs 3.10.
UTC = timezone.utc


def output_path(raw: str, *, directory: bool = False) -> Path:
    root = Path(os.getenv("AI_DATASEEK_OUTPUT_ROOT", "/home/ubuntu/output")).resolve()
    path = Path(raw).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"output path must be below {root}")
    (path if directory else path.parent).mkdir(parents=True, exist_ok=True)
    return path


def artifact(path: Path, content_type: str) -> dict[str, Any]:
    return {"path": str(path), "type": content_type, "size_bytes": path.stat().st_size}


def result(operation: str, summary: dict[str, Any], artifacts: list[dict[str, Any]] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    return {"success": True, "answer_ready": True, "operation": operation, "summary": summary, "artifacts": artifacts or [], "warnings": warnings or [], "provenance": {"tool": operation, "version": "1.0.0"}, "recommended_next_tools": []}


def describe(path: Path) -> dict[str, Any]:
    item: dict[str, Any] = {"name": path.name, "size_bytes": path.stat().st_size, "suffix": path.suffix.lower()}
    try:
        if path.suffix.lower() in {".tif", ".tiff"}:
            import rasterio
            with rasterio.open(path) as src:
                item.update(format="GeoTIFF", crs=str(src.crs) if src.crs else None, bounds=list(src.bounds), resolution=list(src.res), dimensions=[src.height, src.width], bands=src.count, nodata=src.nodata)
        elif path.suffix.lower() in {".nc", ".nc4", ".cdf"}:
            import xarray as xr
            with xr.open_dataset(path, decode_cf=False) as ds:
                item.update(format="NetCDF", dimensions=dict(ds.sizes), variables=list(ds.data_vars), coordinates=list(ds.coords), attributes={str(k): str(v) for k, v in ds.attrs.items()})
        elif path.suffix.lower() in {".shp", ".geojson", ".gpkg", ".parquet"}:
            import geopandas as gpd
            gdf = gpd.read_file(path)
            item.update(format="vector", crs=str(gdf.crs) if gdf.crs else None, bounds=list(gdf.total_bounds), feature_count=len(gdf), geometry_types=sorted(set(gdf.geometry.geom_type.dropna())))
    except Exception as exc:
        item["inspection_warning"] = str(exc)
    return item


def metadata_generate(args: dict[str, Any]) -> dict[str, Any]:
    paths = [Path(value) for value in args["input_paths"]]
    missing = [path.name for path in paths if not path.is_file()]
    if missing: raise ValueError(f"input files do not exist: {', '.join(missing)}")
    target = output_path(args["output_path"])
    payload = {"product_name": args["product_name"], "description": args.get("description", ""), "created_at": datetime.now(UTC).isoformat(), "assets": [describe(path) for path in paths]}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result("geodata_metadata_generate", {"asset_count": len(paths), "product_name": args["product_name"]}, [artifact(target, "application/json")])


def provenance_manifest(args: dict[str, Any]) -> dict[str, Any]:
    target = output_path(args["output_path"])
    payload = {"generated_at": datetime.now(UTC).isoformat(), "source_files": [Path(p).name for p in args["source_paths"]], "tools": args.get("tools", []), "parameters": args.get("parameters", {}), "outputs": [Path(p).name for p in args.get("output_paths", [])]}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result("geodata_provenance_manifest", {"source_count": len(payload["source_files"]), "tool_count": len(payload["tools"])}, [artifact(target, "application/json")])


def checksum_manifest(args: dict[str, Any]) -> dict[str, Any]:
    target = output_path(args["output_path"]); rows = []
    for raw in args["input_paths"]:
        path = Path(raw)
        if not path.is_file(): raise ValueError(f"input file does not exist: {path.name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""): digest.update(block)
        rows.append({"file": path.name, "size_bytes": path.stat().st_size, "sha256": digest.hexdigest()})
    target.write_text(json.dumps({"algorithm": "SHA-256", "files": rows}, indent=2), encoding="utf-8")
    return result("geodata_checksum_manifest", {"file_count": len(rows)}, [artifact(target, "application/json")])


def batch_validate(args: dict[str, Any]) -> dict[str, Any]:
    items=[]; valid=True
    for raw in args["input_paths"]:
        path=Path(raw); item={"name":path.name,"exists":path.is_file(),"non_empty":path.is_file() and path.stat().st_size>0}
        if item["exists"] and item["non_empty"]: item.update(describe(path))
        else: valid=False
        items.append(item)
    target=output_path(args["output_path"]); target.write_text(json.dumps({"valid":valid,"items":items},ensure_ascii=False,indent=2),encoding="utf-8")
    return result("geodata_batch_validate", {"valid":valid,"file_count":len(items),"invalid_count":sum(not i["exists"] or not i["non_empty"] for i in items)}, [artifact(target,"application/json")])


def preview_generate(args: dict[str, Any]) -> dict[str, Any]:
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    path=Path(args["input_path"]); target=output_path(args["output_path"])
    fig,ax=plt.subplots(figsize=(8,6),constrained_layout=True); suffix=path.suffix.lower()
    if suffix in {".tif",".tiff"}:
        import rasterio
        with rasterio.open(path) as src: data=src.read(args.get("band",1),masked=True); extent=[src.bounds.left,src.bounds.right,src.bounds.bottom,src.bounds.top]
        image=ax.imshow(data,extent=extent,cmap=args.get("cmap","viridis")); fig.colorbar(image,ax=ax,shrink=.75)
    elif suffix in {".shp",".geojson",".gpkg",".parquet"}:
        import geopandas as gpd
        gdf=gpd.read_file(path); gdf.plot(ax=ax,facecolor="#60a5fa",edgecolor="#1e3a8a",linewidth=.5)
    elif suffix in {".nc",".nc4",".cdf"}:
        import xarray as xr
        with xr.open_dataset(path) as ds:
            variable=args.get("variable") or next(iter(ds.data_vars)); data=ds[variable]
            while data.ndim>2: data=data.isel({data.dims[0]:0})
            image=ax.imshow(data.values,cmap=args.get("cmap","viridis")); fig.colorbar(image,ax=ax,shrink=.75)
    else: raise ValueError("preview supports GeoTIFF, vector and NetCDF files")
    ax.set_title(args.get("title") or path.stem); ax.grid(True,alpha=.15); fig.savefig(target,dpi=160); plt.close(fig)
    return result("geodata_preview_generate", {"source":path.name}, [artifact(target,"image/png")])


def product_package(args: dict[str, Any]) -> dict[str, Any]:
    target=output_path(args["output_path"]); files=[Path(p) for p in args["input_paths"]]
    if any(not p.is_file() for p in files): raise ValueError("all package inputs must be existing files")
    with zipfile.ZipFile(target,"w",compression=zipfile.ZIP_DEFLATED) as bundle:
        seen=set()
        for path in files:
            name=path.name
            if name in seen: name=f"{path.stem}-{hashlib.sha256(str(path).encode()).hexdigest()[:8]}{path.suffix}"
            seen.add(name); bundle.write(path,arcname=name)
        bundle.writestr("product.json",json.dumps({"name":args["product_name"],"description":args.get("description", ""),"file_count":len(files),"created_at":datetime.now(UTC).isoformat()},ensure_ascii=False,indent=2))
    return result("geodata_product_package", {"product_name":args["product_name"],"file_count":len(files)}, [artifact(target,"application/zip")])


OPERATIONS={"geodata_metadata_generate":metadata_generate,"geodata_provenance_manifest":provenance_manifest,"geodata_checksum_manifest":checksum_manifest,"geodata_batch_validate":batch_validate,"geodata_preview_generate":preview_generate,"geodata_product_package":product_package}


def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("operation",choices=sorted(OPERATIONS)); parser.add_argument("--arguments-json",required=True); ns=parser.parse_args()
    try: print(json.dumps(OPERATIONS[ns.operation](json.loads(ns.arguments_json)),ensure_ascii=False)); return 0
    except Exception as exc: print(json.dumps({"success":False,"error":str(exc)},ensure_ascii=False)); return 1


if __name__ == "__main__": raise SystemExit(main())
