"""HTTP acceptance using disposable public/synthetic files, never an Agent.

Run against the existing Compose frontend. The only writes are four uniquely
named fixture uploads/deletions and the current owner's NetCDF map preference,
which is restored in ``finally``. No original files, sessions or tool settings
are modified. A failed/uncertain upload is never retried automatically.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote
import uuid

import httpx

from app.application.services.scientific_visualization import ScientificVisualizationResult
from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb


async def business_snapshot(database):
    """Read-only integrity gate; return counts and a digest, never user content."""
    counts = {}
    for name in ("stored_files", "sessions", "session_events", "model_traces", "token_usage", "data_center_datasets"):
        counts[name] = await database[name].count_documents({})
    sessions = await database.sessions.find({}, {"_id": 0, "session_id": 1, "status": 1,
        "updated_at": 1, "dataset_ids": 1, "files": 1}).sort("session_id", 1).to_list()
    counts["session_snapshot_sha256"] = hashlib.sha256(json.dumps(sessions, sort_keys=True, default=str).encode()).hexdigest()
    return counts


async def main():
    prefix = "visualization-regression-" + uuid.uuid4().hex
    base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
    resources = Path(__file__).resolve().parents[1] / "app/resources/datasets"
    uploaded: list[str] = []
    deleted: list[str] = []
    cleanup_errors: list[str] = []
    map_id = None
    map_original = None
    restore_map = False
    preference_restored = False
    checks = []
    negative_checks = {}
    error = None
    mongo = get_mongodb()
    await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]
        before = await business_snapshot(database)
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10)) as client:
            async def request(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                payload = response.json()
                assert payload["code"] == 0, "API returned a non-success envelope"
                return payload["data"]

            async def catalog():
                value = await request("GET", "/api/v1/visualizations")
                assert value["engine"] == "cordis"
                return value

            async def upload(name, payload, content_type):
                value = await request("POST", "/api/v1/files", files={"file": (prefix + "-" + name, payload, content_type)},
                                      data={"metadata": json.dumps({"source": "visualization_http_regression", "regression_run": prefix})})
                file_id = value.get("file_id")
                assert isinstance(file_id, str) and file_id, "Upload did not return an opaque file ID"
                # Register immediately, before checking anything else, so any
                # later assertion still deletes exactly this returned object.
                uploaded.append(file_id)
                print(json.dumps({"fixture_uploaded": file_id, "run": prefix}), file=sys.stderr, flush=True)
                assert re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", file_id), "Unexpected opaque file ID syntax"
                assert value["filename"].startswith(prefix + "-")
                return file_id

            async def preview(file_id, plugin, reader, kind):
                value = await request("POST", f"/api/v1/files/{quote(file_id, safe='')}/visualization", json={"plugin_id": plugin})
                result = ScientificVisualizationResult.model_validate(value)
                assert result.reader == reader and result.kind == kind and result.plugin_id == plugin
                assert re.fullmatch(r"[0-9a-f]{64}", result.version)
                assert re.fullmatch(r"[0-9a-f]{64}", result.revision)
                checks.append({"reader": reader, "view": kind, "points": len(result.x),
                               "pixels": len(result.values), "sampled": result.sampled})
                return value

            async def expect_status(path, body, status, name):
                response = await client.post(path, json=body)
                assert response.status_code == status, f"{name}: expected HTTP {status}, received {response.status_code}"
                negative_checks[name] = status

            try:
                initial = await catalog()
                def plugin(reader, adapter):
                    matches = [item for item in initial["plugins"] if item["reader"] == reader and item["adapter"] == adapter]
                    assert len(matches) == 1, "Expected exactly one bundled reader/adapter"
                    return matches[0]
                map_plugin = plugin("netcdf", "scientific-map")
                map_id, map_original = map_plugin["id"], map_plugin["enabled"]
                netcdf_series = plugin("netcdf", "scientific-series")
                fits_image = plugin("fits", "scientific-image")
                fits_series = plugin("fits", "scientific-series")
                fastq_quality = plugin("fastq", "scientific-quality")
                assert all(item["enabled"] for item in (netcdf_series, fits_image, fits_series, fastq_quality)), "Required scientific reader is disabled; no unrelated preference will be changed"
                if not map_original:
                    restore_map = True
                    await request("PATCH", f"/api/v1/visualizations/{map_id}/state", json={"enabled": True})

                nc = await upload("noaa.nc", (resources / "open-noaa-air-climatology/air.sig995.mon.ltm.1991-2020.nc").read_bytes(), "application/x-netcdf")
                noaa_map = await preview(nc, map_id, "netcdf", "map")
                noaa_series = await preview(nc, netcdf_series["id"], "netcdf", "series")
                assert noaa_map["selected_variable"] == noaa_series["selected_variable"] == "air"
                assert len(noaa_series["y"]) == 12
                assert noaa_series["metadata"]["indices"] == {"lat": 0, "lon": 0}
                assert noaa_map["metadata"]["indices"] == {"time": 0}
                for name, path in [("nasa-fos.fits", "nasa-hst-fos/FOSy19g0309t_c2f.fits"),
                                   ("nasa-wfpc2.fits", "nasa-hst-wfpc2/WFPC2ASSNu5780205bx.fits")]:
                    file_id = await upload(name, (resources / path).read_bytes(), "application/fits")
                    await preview(file_id, fits_image["id"], "fits", "image")
                    await preview(file_id, fits_series["id"], "fits", "series")
                fq = await upload("quality.fastq", b"@read1\nACGT\n+\n!+5?\n@read2\nGC\n+\n5I\n", "text/plain")
                quality = await preview(fq, fastq_quality["id"], "fastq", "quality")
                assert quality["y"] == [10, 25, 20, 30]
                assert quality["metadata"]["position_counts"] == [2, 2, 1, 1]

                endpoint = f"/api/v1/files/{quote(nc, safe='')}/visualization"
                await expect_status(endpoint, {"plugin_id": "visualization-regression-unknown"}, 404, "missing_plugin")
                await expect_status(f"/api/v1/files/{prefix}-missing/visualization", {"plugin_id": map_id}, 404, "missing_file")
                await expect_status(endpoint, {"plugin_id": fits_image["id"]}, 422, "format_mismatch")
                await expect_status(endpoint, {"plugin_id": map_id, "version": "0" * 64}, 409, "stale_version")
                restore_map = True  # Also restore if this PATCH has an uncertain response.
                await request("PATCH", f"/api/v1/visualizations/{map_id}/state", json={"enabled": False})
                stopped = await catalog()
                assert next(item for item in stopped["plugins"] if item["id"] == map_id)["enabled"] is False
                await expect_status(endpoint, {"plugin_id": map_id}, 403, "disabled_plugin")
            except BaseException as caught:
                error = caught
            finally:
                if restore_map and map_id is not None:
                    try:
                        await request("PATCH", f"/api/v1/visualizations/{map_id}/state", json={"enabled": map_original})
                        restored = await catalog()
                        preference_restored = next(item for item in restored["plugins"] if item["id"] == map_id)["enabled"] == map_original
                        assert preference_restored
                    except Exception:
                        cleanup_errors.append("map_preference_restore_failed")
                else:
                    preference_restored = True
                for file_id in reversed(uploaded):
                    try:
                        path = f"/api/v1/files/{quote(file_id, safe='')}"
                        response = await client.delete(path)
                        assert response.status_code in (200, 404)
                        absent = await client.get(path + "/info")
                        assert absent.status_code == 404, "Deleted fixture remains readable"
                        deleted.append(file_id)
                    except Exception:
                        cleanup_errors.append("fixture_delete_failed:" + file_id)
            after = await business_snapshot(database)
            summary = {"run": prefix, "successful_previews": checks, "view_types": len({(c["reader"], c["view"]) for c in checks}),
                       "negative_http_checks": negative_checks, "fixtures_uploaded": len(uploaded), "fixtures_deleted": len(deleted),
                       "map_preference_restored": preference_restored, "business_state_preserved": before == after,
                       "business_counts_before": before, "business_counts_after": after,
                       "sessions_or_model_requests_created": 0, "cleanup_errors": cleanup_errors,
                       "passed": error is None and not cleanup_errors and before == after}
            print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
            if error is not None:
                raise RuntimeError("HTTP visualization acceptance failed; inspect the summary and cleanup status") from error
            if cleanup_errors or before != after:
                raise RuntimeError("HTTP visualization cleanup or business-state preservation check failed")
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
