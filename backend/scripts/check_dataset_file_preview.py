"""Exercise registered-file preview without uploads, new sessions or model calls.

Uses existing bundled public datasets and creates only the preview endpoint's
reusable, expiring file references. Original files and plugin preferences are
never changed. Run inside the existing backend environment.
"""
from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb
from app.interfaces.dependencies import get_current_user
from check_visualization_http import business_snapshot


async def main():
    mongo = get_mongodb()
    await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]
        before = await business_snapshot(database)
        checks = []
        base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
        async with httpx.AsyncClient(base_url=base, timeout=120) as client:
            async def api(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                envelope = response.json()
                assert envelope["code"] == 0
                return envelope["data"]

            catalog = await api("GET", "/api/v1/visualizations")
            assert catalog["engine"] == "cordis"
            enabled = {item["id"] for item in catalog["plugins"] if item["enabled"]}
            examples = [
                ("bbbc007-drosophila-cells", ".tif", "tiff", "binary"),
                ("open-uci-iris", ".csv", "csv", "csv"),
                ("open-noaa-air-climatology", ".nc", "netcdf-map", "map"),
                ("nasa-hst-fos", ".fits", "fits-image", "image"),
                ("open-natural-earth-countries", ".shp", "shapefile", "sidecars"),
                ("open-uci-concrete", ".xls", "viz-excel", "workbook"),
                ("ncbi-lambda-reference", ".fasta", "viz-sequence-browser", "sequence"),
                ("pdb-crambin", ".pdb", "molecular", "molecule"),
                ("mendeley-calcium-carbonate", ".xrdml", "viz-diffraction", "diffraction"),
                ("plos-reproducible-research", ".pdf", "viz-pdfjs", "pdf"),
            ]
            for dataset_id, extension, plugin_id, mode in examples:
                assert plugin_id in enabled, "An acceptance capability is disabled; no preference is changed"
                dataset = await api("GET", f"/api/v1/datasets/{dataset_id}")
                source = next(item for item in dataset["files"] if item["path"].lower().endswith(extension))
                endpoint = f"/api/v1/datasets/{dataset_id}/files/preview"
                body = {"path": source["path"], "plugin_id": plugin_id}
                prepared = await api("POST", endpoint, json=body)
                repeated = await api("POST", endpoint, json=body)
                file_id = prepared["file"]["file_id"]
                assert file_id == repeated["file"]["file_id"], "Repeated clicks should reuse the reference"
                assert prepared["file"]["filename"] == source["name"]
                assert prepared["file"]["size"] == source["size"]
                assert all(not str(value).startswith(("/Users/", "/data/", "/app/")) for value in prepared["file"].values())
                file_endpoint = f"/api/v1/files/{quote(file_id, safe='')}"
                info = await api("GET", file_endpoint + "/info")
                assert info["file_id"] == file_id
                if mode == "csv":
                    page = (await api("POST", file_endpoint + "/visualization", json={"plugin_id": plugin_id, "operation": "page"}))["payload"]
                    assert page["rows"] and page["bytes_read"] <= 128 * 1024
                elif mode in {"map", "image"}:
                    result = await api("POST", file_endpoint + "/visualization", json={"plugin_id": plugin_id, "operation": "preview"})
                    assert result["kind"] == "raster" and result["payload"]["view_kind"] == mode and result["payload"]["values"]
                    if mode == "map":
                        assert "netcdf-series" in enabled
                        curve = await api("POST", file_endpoint + "/visualization", json={"plugin_id": "netcdf-series", "operation": "preview"})
                        assert curve["kind"] == "series" and len(curve["payload"]["y"]) == 12
                elif mode == "sidecars":
                    names = {item["filename"].lower().rsplit(".", 1)[-1] for item in prepared["related_files"]}
                    assert {"shp", "shx", "dbf"} <= names
                    for resource in prepared["related_files"]:
                        response = await client.post(file_endpoint + "/visualization", json={
                            "plugin_id": plugin_id, "operation": "bytes", "options": {"resource_id": resource["file_id"]}})
                        response.raise_for_status()
                        assert len(response.content) == resource["size"]
                        assert response.headers["x-visualization-plugin"] == plugin_id
                elif mode in {"workbook", "sequence", "diffraction"}:
                    result = await api("POST", file_endpoint + "/visualization", json={
                        "plugin_id": plugin_id, "operation": "preview"})
                    payload = result["payload"]
                    if mode == "workbook":
                        assert payload["choices"]["sheets"] and payload["table"]["rows"]
                    elif mode == "sequence":
                        assert payload["choices"]["records"][0]["length"] == 48502
                        window = await api("POST", file_endpoint + "/visualization", json={
                            "plugin_id": plugin_id, "operation": "preview", "kind": "table",
                            "version": result["version"], "options": {
                                "record": 0, "start": 1, "count": 50, "motif": "", "quality_encoding": None}})
                        local_source = Path(__file__).resolve().parents[1] / "app/resources/datasets" / dataset_id / source["path"]
                        bases = "".join(line.strip() for line in local_source.read_text().splitlines()
                                        if not line.startswith(">"))
                        assert window["payload"]["sequence"]["bases"] == bases[:50]
                    else:
                        assert payload["choices"]["scans"]
                        result = await api("POST", file_endpoint + "/visualization", json={
                            "plugin_id": plugin_id, "operation": "preview", "kind": "series",
                            "version": result["version"], "options": {"scan": 0}})
                        payload = result["payload"]
                        assert payload["series"] and payload["series"][0]["x"]
                        assert len(payload["series"][0]["x"]) == len(payload["series"][0]["y"])
                elif mode in {"molecule", "pdf"}:
                    if mode == "molecule":
                        result = await api("POST", file_endpoint + "/visualization", json={
                            "plugin_id": plugin_id, "operation": "prepare"})
                        assert result["kind"] == "molecule" and result["payload"]["source_format"] == "pdb"
                    response = await client.post(file_endpoint + "/visualization", json={
                        "plugin_id": plugin_id, "operation": "bytes"})
                    response.raise_for_status()
                    assert len(response.content) == source["size"]
                    assert response.headers["x-visualization-plugin"] == plugin_id
                    # Compare actual delivered bytes with the bundled, audited source.
                    local_source = Path(__file__).resolve().parents[1] / "app/resources/datasets" / dataset_id / source["path"]
                    assert hashlib.sha256(response.content).digest() == hashlib.sha256(local_source.read_bytes()).digest()
                    if mode == "pdf":
                        from pypdf import PdfReader
                        assert len(PdfReader(BytesIO(response.content)).pages) == 4
                    else:
                        assert any(line.startswith(b"ATOM  ") for line in response.content.splitlines())
                else:
                    response = await client.post(file_endpoint + "/visualization", json={"plugin_id": plugin_id, "operation": "bytes"})
                    response.raise_for_status()
                    assert len(response.content) == source["size"]
                    assert response.content[:4] in (b"II*\x00", b"MM\x00*")
                checks.append({"plugin": plugin_id, "mode": mode, "reference_reused": True})

            # The deployed backend cannot directly see registered host paths:
            # prove the full HTTP/provider/helper chain on an existing source.
            managed = await api("GET", "/api/v1/datasets/manage", params={"limit": 100})
            # Public HTTP metadata deliberately omits storage locations. Use
            # only DB-projected IDs to select an existing authorized HOST_PATH
            # sample, then send the browser-visible path back to the endpoint.
            actor = await get_current_user()
            host_rows = await database.data_center_datasets.find({
                "enabled": True, "locations.storage_type": "host_path",
                "$or": [{"is_submission": {"$ne": True}}, {"created_by": actor.id}],
            }, {"_id": 0, "dataset_id": 1}).to_list()
            host_ids = {row["dataset_id"] for row in host_rows}
            host_sample = next(
                ((item, source) for item in managed["datasets"]
                 if item["enabled"] and item["dataset_id"] in host_ids
                 for source in item["files"]
                 if source["path"].lower().endswith((".md", ".txt", ".csv")) and 0 < source["size"] <= 1024 * 1024),
                None,
            )
            assert host_sample is not None, "No existing bounded host sample is available; do not create one automatically"
            host_dataset, host_source = host_sample
            plugin_id = "csv" if host_source["path"].lower().endswith(".csv") else "markdown" if host_source["path"].lower().endswith(".md") else "text"
            assert plugin_id in enabled
            prepared = await api("POST", f"/api/v1/datasets/{quote(host_dataset['dataset_id'], safe='')}/files/preview",
                                 json={"path": host_source["path"], "plugin_id": plugin_id})
            file_id = prepared["file"]["file_id"]
            file_endpoint = f"/api/v1/files/{quote(file_id, safe='')}/visualization"
            if plugin_id == "markdown":
                response = await client.post(file_endpoint, json={"plugin_id": plugin_id, "operation": "bytes"})
                response.raise_for_status()
                assert len(response.content) == prepared["file"]["size"]
            else:
                page = (await api("POST", file_endpoint, json={"plugin_id": plugin_id, "operation": "page"}))["payload"]
                assert page["bytes_read"] > 0 and page["bytes_read"] <= 128 * 1024
                assert page["total_bytes"] == prepared["file"]["size"]
            checks.append({"plugin": plugin_id, "mode": "host-path-bounded-read"})

            endpoint = "/api/v1/datasets/open-noaa-air-climatology/files/preview"
            for path in ("../outside.nc", "/etc/passwd", "unregistered.nc"):
                response = await client.post(endpoint, json={"path": path, "plugin_id": "netcdf-map"})
                assert response.status_code in (400, 404, 422), "Unregistered or unsafe paths must be rejected"
        after = await business_snapshot(database)
        assert before == after, "Dataset preview must not create analysis or stored-file records"
        print(json.dumps({"passed": True, "checks": checks, "unsafe_or_unregistered_paths_rejected": 3,
                          "business_state_preserved": True, "business_counts": after,
                          "uploads_sessions_or_model_calls": 0}, ensure_ascii=False, indent=2))
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
