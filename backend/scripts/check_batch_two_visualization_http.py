"""Local HTTP acceptance for five batch-two plugins, after deployment only.

Uploads ten uniquely tagged synthetic fixtures, verifies positive/negative
paths, then deletes only this run's exact IDs and restores its five plugin
preferences. No user source, session, model, job or event is created/read.
Each HTTP request has a 10 s connect / 120 s total deadline and no retries.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import struct
import sys
import uuid
import zipfile
from urllib.parse import quote, urlparse

import docker
import httpx

from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from check_batch_one_visualization_http import _integrity_snapshot

PLUGIN_IDS = ("viz-edf-signals", "viz-archive-members", "viz-mca-spectrum", "viz-geoformats", "viz-czi")
SOURCE = "batch_two_visualization_http_regression"
ID = re.compile(r"[A-Za-z0-9_:-]{1,256}")


def signal_fixture(fmt):
    """Small fixed-record synthetic EDF/BDF, with no library or filesystem I/O."""
    width = 2 if fmt == "edf" else 3
    low, high = -(2**(width * 8 - 1)), 2**(width * 8 - 1) - 1
    labels, samples = ["EEG C3", "Status" if fmt == "bdf" else "ECG I"], [4, 2]
    def field(value, length):
        result = str(value).encode("ascii")
        assert len(result) <= length
        return result.ljust(length, b" ")
    result = b"0       " if fmt == "edf" else b"\xffBIOSEMI"
    result += field("PRIVATE SYNTHETIC PATIENT", 80) + field("PRIVATE SYNTHETIC RECORD", 80)
    result += b"01.01.24" + b"12.34.56" + field(768, 8) + field("", 44) + field(3, 8) + field(1, 8) + field(2, 4)
    for values, length in ((labels, 16), (["SYNTHETIC SENSOR"] * 2, 80), (["uV"] * 2, 8),
            ([low] * 2, 8), ([high] * 2, 8), ([low] * 2, 8), ([high] * 2, 8),
            ([""] * 2, 80), (samples, 8), ([""] * 2, 32)):
        result += b"".join(field(value, length) for value in values)
    for record in range(3):
        for count in samples:
            result += b"".join(int(record * count + i).to_bytes(width, "little", signed=True) for i in range(count))
    return result


CZI_FIXTURE_CODE = r'''
import base64,json,os,tempfile
from pathlib import Path
import numpy as np
from pylibCZIrw import czi
assert os.getuid()==65534
with tempfile.TemporaryDirectory(prefix="synthetic-czi-http-") as folder:
    path=Path(folder)/"synthetic.czi"
    with czi.create_czi(str(path)) as writer:
        for channel in range(2):
            for z in range(2):
                pixels=np.arange(48,dtype=np.uint16).reshape(6,8,1)+channel*100+z*1000
                writer.write(pixels,plane={"C":channel,"Z":z,"T":0},scene=0)
    data=path.read_bytes()
    assert 0<len(data)<256*1024
    print(json.dumps({"uid":os.getuid(),"data_base64":base64.b64encode(data).decode("ascii")}),flush=True)
'''


def synthetic_czi():
    """Use the deployed official library to create pixels; no real CZI input."""
    image = get_settings().sandbox_image
    if not image:
        raise RuntimeError("Configure the deployed sandbox image first")
    client, container = docker.from_env(timeout=5), None
    name = "ai-dataseek-czi-http-fixture-" + uuid.uuid4().hex
    try:
        container = client.containers.create(image=image, name=name,
            entrypoint=["/usr/bin/timeout"], command=["--signal=KILL", "55s", "/app/.venv/bin/python", "-c", CZI_FIXTURE_CODE],
            working_dir="/app", user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="1g", memswap_limit="1g",
            nano_cpus=1000000000, pids_limit=96, tmpfs={"/tmp": "rw,noexec,nosuid,size=32m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            labels={"ai-dataseek.component": "visualization-http-fixture"})
        container.reload()
        assert container.attrs["HostConfig"]["NetworkMode"] == "none" and not container.attrs.get("Mounts")
        assert container.attrs["HostConfig"]["ReadonlyRootfs"] and container.attrs["Config"]["User"] == "65534:65534"
        container.start()
        assert container.wait(timeout=60).get("StatusCode") == 0, "Synthetic CZI generation failed in deployed image"
        output = container.logs(stdout=True, stderr=False)
        assert len(output) < 512 * 1024
        value = json.loads(output)
        assert set(value) == {"uid", "data_base64"} and value["uid"] == 65534
        data = base64.b64decode(value["data_base64"], validate=True)
        assert 0 < len(data) < 256 * 1024 and data.startswith(b"ZISRAWFILE")
        return data
    finally:
        try:
            if container is None:
                try: container = client.containers.get(name)
                except docker.errors.NotFound: pass
            if container is not None: _remove_worker(container)
        finally:
            client.close()


def synthetic_fixtures(czi):
    def archive(marker):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as result:
            result.writestr("measurements.csv", b"time,value\n0,1.5\n1,2.5\n")
            result.writestr("notes.txt", marker.encode())
            result.writestr("opaque.bin", b"\0\1\2")
        return stream.getvalue()
    raw = b"<<PMCA SPECTRUM>>\nLIVE_TIME - 2\nREAL_TIME - 3\n"
    counts = b"<<DATA>>\n1\n4\n9\n16\n<<END>>\n"
    calibration = b"<<CALIBRATION>>\nLABEL - keV\n0 1\n3 7\n"
    asc = b"ncols 4 nrows 3 xllcorner 10 yllcorner 20 cellsize 1 nodata_value -9999\n0 1 2 3 4 -9999 6 7 8 9 10 11"
    grd = b"DSAA\n4 3\n10 13\n20 22\n0 11\n0 1 2 3 4 1.70141e38 6 7 8 9 10 11"
    kml = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>Synthetic point</name><Point><coordinates>10,20</coordinates></Point></Placemark></Document></kml>'
    return [("signals.edf", signal_fixture("edf"), "application/octet-stream"),
        ("signals.bdf", signal_fixture("bdf"), "application/octet-stream"),
        ("members.zip", archive("archive one"), "application/zip"), ("other.zip", archive("archive two"), "application/zip"),
        ("raw.mca", raw + counts, "application/octet-stream"), ("calibrated.mca", raw + calibration + counts, "application/octet-stream"),
        ("grid.asc", asc, "text/plain"), ("grid.grd", grd, "text/plain"), ("geometry.kml", kml, "application/xml"),
        ("microscopy.czi", czi, "application/octet-stream")]


async def main():
    prefix = "batch-two-visualization-regression-" + uuid.uuid4().hex
    base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
    parsed = urlparse(base)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"frontend", "localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise RuntimeError("Acceptance is restricted to the existing local frontend")
    uploaded, deleted, cleanup_errors, checks, attempts = [], [], [], [], []
    negatives, originals, touched, fixture_names = {}, {}, set(), []
    preferences_restored, error = False, None
    mongo = get_mongodb()
    await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]
        before = await _integrity_snapshot(database)
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10),
                transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False) as client:
            async def raw(method, path, *, label, **kwargs):
                record = {"case": label, "method": method}
                attempts.append(record)
                try:
                    async with asyncio.timeout(120):
                        response = await client.request(method, path, **kwargs)
                except Exception as failure:
                    record["error_type"] = type(failure).__name__
                    raise
                record["status"] = response.status_code
                return response

            async def request(method, path, *, label, **kwargs):
                response = await raw(method, path, label=label, **kwargs)
                response.raise_for_status()
                result = response.json()
                assert result.get("code") == 0, "API returned a non-success envelope"
                return result["data"]

            async def catalog(label):
                result = await request("GET", "/api/v1/visualizations", label=label)
                assert result["engine"] == "cordis"
                return {item["id"]: item for item in result["plugins"]}

            async def state(plugin, enabled, label):
                touched.add(plugin)  # Restore even if the PATCH result is uncertain.
                await request("PATCH", f"/api/v1/visualizations/{plugin}/state", label=label, json={"enabled": enabled})

            async def upload(name, data, media_type):
                response = await raw("POST", "/api/v1/files", label="upload:" + name,
                    files={"file": (prefix + "-" + name, data, media_type)},
                    data={"metadata": json.dumps({"source": SOURCE, "regression_run": prefix})})
                envelope = response.json()
                info = envelope.get("data")
                identifier = info.get("file_id") if isinstance(info, dict) else None
                if isinstance(identifier, str) and ID.fullmatch(identifier):
                    uploaded.append(identifier)
                    print(json.dumps({"fixture_uploaded": identifier, "run": prefix}), file=sys.stderr, flush=True)
                else:
                    cleanup_errors.append("untrackable_upload_response:" + name)
                response.raise_for_status()
                assert envelope.get("code") == 0 and isinstance(identifier, str) and ID.fullmatch(identifier)
                assert info["filename"] == prefix + "-" + name
                return identifier

            def endpoint(file_id):
                return "/api/v1/files/" + quote(file_id, safe="") + "/visualization"

            async def preview(file_id, plugin, label, *, version=None, kind=None, options=None):
                body = {"plugin_id": plugin, "operation": "preview", "options": options or {}}
                if version is not None: body["version"] = version
                if kind is not None: body["kind"] = kind
                value = await request("POST", endpoint(file_id), label=label, json=body)
                result = VisualizationResult.model_validate(value)
                assert result.plugin_id == plugin and re.fullmatch(r"[0-9a-f]{64}", result.version) and re.fullmatch(r"[0-9a-f]{64}", result.revision)
                if version is not None: assert result.version == version
                encoded = json.dumps(value, ensure_ascii=False).encode()
                assert len(encoded) <= 2 * 1024**2 and not any(v in encoded for v in (b"PRIVATE SYNTHETIC", b"01.01.24", b"12.34.56"))
                checks.append({"case": label, "plugin": plugin, "kind": result.kind, "output_bytes": len(encoded)})
                return result

            async def negative(label, file_id, plugin, status, **body):
                response = await raw("POST", endpoint(file_id), label=label,
                    json={"operation": "preview", "plugin_id": plugin, **body})
                negatives[label] = {"expected": status, "actual": response.status_code}
                assert response.status_code == status, f"{label}: expected {status}, received {response.status_code}"

            try:
                initial = await catalog("initial-catalog")
                assert all(plugin in initial for plugin in PLUGIN_IDS), "Batch-two plugins are not deployed"
                originals = {plugin: initial[plugin]["enabled"] for plugin in PLUGIN_IDS}
                assert all(type(value) is bool for value in originals.values())
                for plugin, enabled in originals.items():
                    if not enabled: await state(plugin, True, "enable:" + plugin)
                czi = await asyncio.to_thread(synthetic_czi)
                fixtures = synthetic_fixtures(czi)
                fixture_names = [prefix + "-" + name for name, _, _ in fixtures]
                files = {name: await upload(name, data, media_type) for name, data, media_type in fixtures}
                edf_id, member_id, mca_id, geo_id, czi_id = PLUGIN_IDS

                edf = await preview(files["signals.edf"], edf_id, "edf-window", options={"channels": [0, 1], "start_seconds": .5, "duration_seconds": 1.5})
                assert edf.kind == "series" and [v["sample_rate"] for v in edf.payload["series"]] == [4, 2]
                assert edf.payload["series"][0]["x"] == [.5, .75, 1, 1.25, 1.5, 1.75]
                assert edf.payload["series"][0]["y"] == [2, 3, 4, 5, 6, 7]
                assert edf.payload["series"][1]["x"] == [.5, 1, 1.5]
                assert edf.metadata["no_resampling"] and edf.metadata["identity_fields_hidden"]
                bdf = await preview(files["signals.bdf"], edf_id, "bdf-window", options={"channels": [0], "duration_seconds": 1})
                assert bdf.metadata["format"] == "bdf" and bdf.payload["series"][0]["y"] == [0, 1, 2, 3]
                assert bdf.payload["choices"]["channels"][1]["selectable"] is False

                directory = await preview(files["members.zip"], member_id, "archive-directory")
                other = await preview(files["other.zip"], member_id, "other-archive-directory")
                rows = directory.payload["table"]["rows"]
                token = next(row[4] for row in rows if row[0] == "measurements.csv")
                assert re.fullmatch(r"member-[0-9a-f]{64}", token) and next(row for row in rows if row[0] == "opaque.bin")[3:] == [False, None]
                member = await preview(files["members.zip"], member_id, "archive-member-text", version=directory.version, options={"member_id": token})
                assert member.metadata["mode"] == "text" and member.metadata["checksum_verified"] is True
                assert member.payload["table"]["rows"] == [[1, "time,value"], [2, "0,1.5"], [3, "1,2.5"]]

                raw_mca = await preview(files["raw.mca"], mca_id, "mca-raw")
                calibrated = await preview(files["calibrated.mca"], mca_id, "mca-calibrated")
                assert raw_mca.kind == calibrated.kind == "array"
                assert raw_mca.metadata["axis"] == "channel" and raw_mca.metadata["calibration_applied"] is False
                assert raw_mca.payload["array"]["values"] == [0, 1, 1, 4, 2, 9, 3, 16]
                assert calibrated.metadata["energy_unit"] == "keV" and calibrated.metadata["calibration_coefficients"] == [1, 2, 0]
                assert calibrated.payload["array"]["values"] == [1, 1, 3, 4, 5, 9, 7, 16]

                asc = await preview(files["grid.asc"], geo_id, "asc-unknown-crs", kind="map")
                assert asc.kind == "array" and asc.metadata["crs"] == "unknown" and asc.payload["array"]["values"][5] is None
                known = await preview(files["grid.asc"], geo_id, "asc-explicit-wgs84", kind="map", version=asc.version, options={"crs": "EPSG:4326"})
                assert known.metadata["crs"] == "EPSG:4326" and known.metadata["crs_source"] == "user" and known.metadata["extent"] == [10, 20, 14, 23]
                grd = await preview(files["grid.grd"], geo_id, "grd-dsaa", kind="map")
                assert grd.metadata["format"] == "Surfer DSAA" and grd.metadata["registration"] == "node"
                assert grd.payload["array"]["values"][:4] == [8, 9, 10, 11]
                kml = await preview(files["geometry.kml"], geo_id, "kml-inert-geometry", kind="map")
                assert kml.kind == "features" and kml.metadata["crs_source"] == "format" and kml.metadata["feature_count"] == 1
                assert kml.payload["geojson"]["features"][0]["geometry"] == {"type": "Point", "coordinates": [10, 20]}

                tree = await preview(files["microscopy.czi"], czi_id, "czi-tree", kind="tree")
                assert tree.kind == "tree" and tree.metadata["scene_shape"] == [6, 8] and tree.payload["choices"]["channels"] == [0, 1]
                roi = {"indices": [1, 1, 0], "roi": [2, 1, 3, 2]}
                image = await preview(files["microscopy.czi"], czi_id, "czi-selected-roi", kind="image", version=tree.version, options=roi)
                png = base64.b64decode(image.payload["data_base64"], validate=True)
                assert image.kind == "media" and png.startswith(b"\x89PNG\r\n\x1a\n") and struct.unpack(">II", png[16:24]) == (3, 2)
                assert image.metadata["display_range"] == [1110, 1120] and image.metadata["output_shape"] == [2, 3]

                await negative("edf-stale-version", files["signals.edf"], edf_id, 409, version="0" * 64)
                await negative("edf-excessive-window", files["signals.edf"], edf_id, 422, options={"duration_seconds": 61})
                await negative("edf-nonexistent-channel", files["signals.edf"], edf_id, 422, options={"channels": [255]})
                await negative("bdf-status-channel", files["signals.bdf"], edf_id, 422, options={"channels": [1]})
                await negative("archive-member-without-version", files["members.zip"], member_id, 422, options={"member_id": token})
                await negative("archive-member-stale-version", files["members.zip"], member_id, 409, version="0" * 64, options={"member_id": token})
                await negative("archive-forged-token", files["members.zip"], member_id, 422, version=directory.version, options={"member_id": "member-" + "0" * 64})
                await negative("archive-cross-resource-token", files["other.zip"], member_id, 422, version=other.version, options={"member_id": token})
                await negative("archive-path-not-token", files["members.zip"], member_id, 422, version=directory.version, options={"member_id": "../secret.txt"})
                await negative("unknown-file-unreadable", "batch-two-missing-" + uuid.uuid4().hex, member_id, 404)
                await negative("mca-fit-not-allowed", files["raw.mca"], mca_id, 422, options={"fit": True})
                await negative("mca-wrong-format", files["members.zip"], mca_id, 422)
                await negative("geo-unsupported-crs", files["grid.asc"], geo_id, 422, options={"crs": "EPSG:32650"})
                await negative("kml-crs-override", files["geometry.kml"], geo_id, 422, kind="map", options={"crs": "EPSG:3857"})
                await negative("czi-image-without-version", files["microscopy.czi"], czi_id, 422, kind="image", options=roi)
                await negative("czi-stale-version", files["microscopy.czi"], czi_id, 409, kind="image", version="0" * 64, options=roi)
                await negative("czi-wrong-channel", files["microscopy.czi"], czi_id, 422, kind="image", version=tree.version, options={"indices": [2, 0, 0], "roi": [0, 0, 1, 1]})
                await negative("czi-unapproved-scene", files["microscopy.czi"], czi_id, 422, kind="image", version=tree.version, options={**roi, "scene": 1})
                await negative("czi-excessive-roi", files["microscopy.czi"], czi_id, 422, kind="image", version=tree.version, options={"indices": [0, 0, 0], "roi": [0, 0, 1025, 1]})
                for plugin, name in zip(PLUGIN_IDS, ("signals.edf", "members.zip", "raw.mca", "grid.asc", "microscopy.czi")):
                    await state(plugin, False, "disable:" + plugin)
                    assert (await catalog("confirm-disabled:" + plugin))[plugin]["enabled"] is False
                    await negative("disabled:" + plugin, files[name], plugin, 403)
            except BaseException as caught:
                error = caught
            finally:
                for plugin in reversed(PLUGIN_IDS):
                    if plugin in touched:
                        try:
                            await request("PATCH", f"/api/v1/visualizations/{plugin}/state", label="restore:" + plugin, json={"enabled": originals[plugin]})
                        except Exception:
                            cleanup_errors.append("preference_restore_failed:" + plugin)
                try:
                    restored = await catalog("restored-catalog")
                    preferences_restored = all(restored[plugin]["enabled"] == enabled for plugin, enabled in originals.items())
                    if not preferences_restored: cleanup_errors.append("preference_readback_mismatch")
                except Exception:
                    cleanup_errors.append("preference_readback_failed")
                # Resolve only this run's exact declared names and random tag.
                # This can recover an accepted upload whose response was lost;
                # it never retries the upload or scans other users' file bodies.
                if fixture_names:
                    try:
                        records = await database.stored_files.find({"filename": {"$in": fixture_names},
                            "metadata.source": SOURCE, "metadata.regression_run": prefix}, {"_id": 0, "file_id": 1}).limit(11).to_list()
                        if len(records) > 10: cleanup_errors.append("unexpected_fixture_inventory")
                        for record in records:
                            identifier = record.get("file_id")
                            if isinstance(identifier, str) and ID.fullmatch(identifier) and identifier not in uploaded:
                                uploaded.append(identifier)
                    except Exception:
                        cleanup_errors.append("fixture_inventory_check_failed")
                for file_id in reversed(uploaded):
                    try:
                        path = "/api/v1/files/" + quote(file_id, safe="")
                        response = await raw("DELETE", path, label="delete-fixture")
                        assert response.status_code in {200, 404}
                        assert (await raw("GET", path + "/info", label="confirm-fixture-deleted")).status_code == 404
                        deleted.append(file_id)
                    except Exception:
                        cleanup_errors.append("fixture_delete_failed:" + file_id)
            after = await _integrity_snapshot(database)
            passed = error is None and not cleanup_errors and preferences_restored and before == after
            print(json.dumps({"run": prefix, "passed": passed, "successful_checks": checks, "negative_http_checks": negatives,
                "http_attempts": attempts, "fixtures_uploaded": len(uploaded), "fixtures_deleted": len(deleted),
                "preferences_restored": preferences_restored, "business_state_preserved": before == after,
                "business_counts_before": before, "business_counts_after": after,
                "digest_scope": "counts and session/event/model/token metadata only; no user content",
                "cross_resource_scope": "archive token substitution and unknown-file HTTP rejection; cross-owner isolation remains covered by backend tests",
                "sessions_or_model_requests_created": 0, "cleanup_errors": cleanup_errors,
                "failure_type": type(error).__name__ if error else None}, ensure_ascii=False, indent=2), flush=True)
            if not passed:
                raise RuntimeError("Batch-two HTTP acceptance failed; inspect summary and cleanup status") from error
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
