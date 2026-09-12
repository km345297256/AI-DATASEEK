"""Run only AFTER the approved local deployment: batch-three HTTP acceptance.

Seven uniquely tagged synthetic uploads, three temporary plugin preferences,
exact-tag/filename/ID cleanup, and before/after business metadata integrity.
Never reads user file bodies, creates sessions, calls a model, or fetches a URL
from a dataset. Official h5py / Zeiss fixtures are generated in one confined,
networkless, read-only, non-root, automatically removed sandbox container.
The script is deliberately not run during source implementation.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import struct
import sys
import uuid
from urllib.parse import quote, urlparse

import docker
import httpx

from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from check_batch_one_visualization_http import _integrity_snapshot
from check_batch_two_visualization_http import signal_fixture

PLUGIN_IDS = ("viz-array-window", "viz-czi-window", "viz-instrument-images")
SOURCE = "batch_three_visualization_http_regression"
ID = re.compile(r"[A-Za-z0-9_:-]{1,256}\Z")
NATIVE_FIXTURE_CODE = r'''
import base64,io,json,os,tempfile
from pathlib import Path
from importlib.metadata import version
import h5py,numpy as np
from pylibCZIrw import czi
assert os.getuid()==65534 and version("pylibCZIrw")=="6.1.0"
buffer=io.BytesIO()
with h5py.File(buffer,"w") as handle:
    handle.create_dataset("signal",data=np.arange(20,dtype=np.float64),chunks=(4,),compression="gzip")
    handle.create_dataset("image",data=np.arange(120,dtype=np.int16).reshape(10,12),chunks=(2,3),compression="gzip",shuffle=True,fletcher32=True)
fixtures={"h5":buffer.getvalue()}
with tempfile.TemporaryDirectory(prefix="synthetic-batch-three-") as folder:
    for name,compression in (("czi",None),("compressed_czi","zstd0:ExplicitLevel=1")):
        path=Path(folder)/(name+".czi")
        with czi.create_czi(str(path),compression_options=compression) as writer:
            for channel in range(2):
                for z in range(2):
                    values=np.arange(48,dtype=np.uint16).reshape(6,8,1)+channel*100+z*1000
                    writer.write(values,plane={"C":channel,"Z":z,"T":0},scene=0)
        fixtures[name]=path.read_bytes()
assert all(0<len(data)<256*1024 for data in fixtures.values())
print(json.dumps({"uid":os.getuid(),"h5py":version("h5py"),"pylibCZIrw":version("pylibCZIrw"),"fixtures":{key:base64.b64encode(value).decode("ascii") for key,value in fixtures.items()}}),flush=True)
'''


def native_fixtures():
    image = get_settings().sandbox_image
    if not image:
        raise RuntimeError("Configure the deployed sandbox image first")
    client, container = docker.from_env(timeout=5), None
    name = "ai-dataseek-batch-three-http-fixture-" + uuid.uuid4().hex
    try:
        container = client.containers.create(image=image, name=name, entrypoint=["/usr/bin/timeout"],
            command=["--signal=KILL", "55s", "/app/.venv/bin/python", "-c", NATIVE_FIXTURE_CODE],
            working_dir="/app", user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="1g", memswap_limit="1g",
            nano_cpus=1_000_000_000, pids_limit=96, tmpfs={"/tmp": "rw,noexec,nosuid,size=32m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            labels={"ai-dataseek.component": "visualization-http-fixture"})
        container.reload()
        assert container.attrs["HostConfig"]["NetworkMode"] == "none" and not container.attrs.get("Mounts")
        assert container.attrs["HostConfig"]["ReadonlyRootfs"] and container.attrs["Config"]["User"] == "65534:65534"
        container.start()
        assert container.wait(timeout=60).get("StatusCode") == 0, "Native fixture generation failed"
        raw = container.logs(stdout=True, stderr=False)
        assert len(raw) < 1024**2
        value = json.loads(raw)
        assert set(value) == {"uid", "h5py", "pylibCZIrw", "fixtures"} and value["uid"] == 65534 and value["pylibCZIrw"] == "6.1.0"
        assert set(value["fixtures"]) == {"h5", "czi", "compressed_czi"}
        fixtures = {key: base64.b64decode(encoded, validate=True) for key, encoded in value["fixtures"].items()}
        assert all(0 < len(data) < 256 * 1024 for data in fixtures.values())
        assert fixtures["h5"].startswith(b"\x89HDF\r\n\x1a\n") and all(fixtures[key].startswith(b"ZISRAWFILE") for key in ("czi", "compressed_czi"))
        return fixtures, {key: value[key] for key in ("uid", "h5py", "pylibCZIrw")}
    finally:
        try:
            if container is None:
                try: container = client.containers.get(name)
                except docker.errors.NotFound: pass
            if container is not None: _remove_worker(container)
        finally:
            client.close()


def instrument_fixtures():
    """Original synthetic ESRF EDF and Princeton SPE 2.6 field layouts."""
    edf = bytearray()
    for frame in range(2):
        fields = {"HeaderID": "EH:000001:000000:000000", "Image": str(frame), "ByteOrder": "LowByteFirst",
            "DataType": "UnsignedShort", "Dim_1": "4", "Dim_2": "3", "Size": "24", "EDF_HeaderSize": "512",
            "Comment": "SYNTHETIC PRIVATE /private/not-a-real-path"}
        header = ("{\n" + "".join(f"{key} = {value} ;\n" for key, value in fields.items())).encode("ascii")
        assert len(header) + 2 <= 512
        edf += header + b" " * (510 - len(header)) + b"}\n"
        edf += struct.pack("<12H", *(frame * 100 + i for i in range(12)))
    spe = bytearray(4100)
    marker = b"SYNTHETIC PRIVATE /private/not-a-real-path"
    spe[200:200 + len(marker)] = marker
    for offset, code, value in ((42, "H", 4), (656, "H", 3), (108, "h", 3), (1446, "i", 2), (1992, "f", 2.6), (678, "Q", 0), (1510, "h", 0)):
        struct.pack_into("<" + code, spe, offset, value)
    spe += struct.pack("<24H", *(frame * 100 + i for frame in range(2) for i in range(12)))
    assert len(edf) == 1072 and len(spe) == 4148
    return bytes(edf), bytes(spe)


async def main():
    base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
    parsed = urlparse(base)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"frontend", "localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise RuntimeError("Acceptance is restricted to the existing local frontend")
    prefix = "batch-three-visualization-regression-" + uuid.uuid4().hex
    uploaded, deleted, checks, attempts, cleanup_errors = [], [], [], [], []
    originals, negatives, provenance = {}, {}, {}
    touched, fixture_names, preferences_restored, error = set(), [], False, None
    mongo = get_mongodb(); await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]
        before = await _integrity_snapshot(database)
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10),
                transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False, follow_redirects=False) as client:
            async def raw(method, path, *, label, **kwargs):
                record = {"case": label, "method": method}; attempts.append(record)
                try:
                    async with asyncio.timeout(120): response = await client.request(method, path, **kwargs)
                except Exception as failure:
                    record["error_type"] = type(failure).__name__; raise
                record["status"] = response.status_code
                return response

            async def request(method, path, *, label, **kwargs):
                response = await raw(method, path, label=label, **kwargs); response.raise_for_status()
                value = response.json(); assert value.get("code") == 0, "Non-success API envelope"
                return value["data"]

            async def catalog(label):
                value = await request("GET", "/api/v1/visualizations", label=label)
                assert value["engine"] == "cordis"
                return {item["id"]: item for item in value["plugins"]}

            async def state(plugin, enabled, label):
                touched.add(plugin)
                await request("PATCH", f"/api/v1/visualizations/{plugin}/state", label=label, json={"enabled": enabled})

            async def upload(name, data):
                response = await raw("POST", "/api/v1/files", label="upload:" + name,
                    files={"file": (prefix + "-" + name, data, "application/octet-stream")},
                    data={"metadata": json.dumps({"source": SOURCE, "regression_run": prefix})})
                envelope = response.json(); info = envelope.get("data")
                identifier = info.get("file_id") if isinstance(info, dict) else None
                if isinstance(identifier, str) and ID.fullmatch(identifier):
                    uploaded.append(identifier)
                    print(json.dumps({"fixture_uploaded": identifier, "run": prefix}), file=sys.stderr, flush=True)
                response.raise_for_status()
                assert envelope.get("code") == 0 and isinstance(identifier, str) and ID.fullmatch(identifier)
                assert info["filename"] == prefix + "-" + name and info["size"] == len(data)
                return identifier

            def endpoint(file_id): return "/api/v1/files/" + quote(file_id, safe="") + "/visualization"

            async def preview(file_id, plugin, label, *, kind="tree", version=None, options=None):
                body = {"plugin_id": plugin, "operation": "preview", "kind": kind, "options": options or {}}
                if version is not None: body["version"] = version
                value = await request("POST", endpoint(file_id), label=label, json=body)
                result = VisualizationResult.model_validate(value)
                assert result.plugin_id == plugin and re.fullmatch(r"[0-9a-f]{64}", result.version) and re.fullmatch(r"[0-9a-f]{64}", result.revision)
                if version is not None: assert result.version == version
                assert result.payload["view_kind"] == kind and result.metadata["input_mode"] == "window"
                assert 0 < result.metadata["source_bytes"] <= 8 * 1024**3
                maximum = 8 * 1024**2 if plugin == PLUGIN_IDS[0] else 32 * 1024**2
                assert 0 < result.metadata["read_bytes"] <= maximum
                assert 0 < result.metadata["read_requests"] <= {PLUGIN_IDS[0]: 128, PLUGIN_IDS[1]: 4096, PLUGIN_IDS[2]: 2048}[plugin]
                encoded = json.dumps(value, ensure_ascii=False).encode()
                assert len(encoded) <= initial[plugin]["limits"]["max_output_bytes"]
                assert not any(secret in encoded for secret in (b"SYNTHETIC PRIVATE", b"/private/not-a-real-path", b"PRIVATE SYNTHETIC"))
                checks.append({"case": label, "plugin": plugin, "kind": result.kind, "output_bytes": len(encoded), "source_bytes": result.metadata["source_bytes"], "read_bytes": result.metadata["read_bytes"], "read_requests": result.metadata["read_requests"]})
                return result

            async def negative(label, file_id, plugin, status=422, **body):
                response = await raw("POST", endpoint(file_id), label=label,
                    json={"operation": "preview", "plugin_id": plugin, "kind": "tree", "options": {}, **body})
                negatives[label] = {"expected": status, "actual": response.status_code}
                assert response.status_code == status, f"{label}: expected {status}, got {response.status_code}"

            def png(result, width, height):
                value = base64.b64decode(result.payload["data_base64"], validate=True)
                assert result.kind == "media" and value[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" and struct.unpack(">II", value[16:24]) == (width, height)

            try:
                initial = await catalog("initial-catalog")
                assert all(plugin in initial for plugin in PLUGIN_IDS), "Batch-three single-file plugins not deployed"
                originals = {plugin: initial[plugin]["enabled"] for plugin in PLUGIN_IDS}
                assert all(type(value) is bool for value in originals.values())
                for plugin, enabled in originals.items():
                    assert initial[plugin]["capabilities"] == {"operations": ["preview"], "input_mode": "window", "shared": False}
                    if not enabled: await state(plugin, True, "enable:" + plugin)
                native, provenance = await asyncio.to_thread(native_fixtures)
                edf, spe = instrument_fixtures()
                fixtures = {"arrays.h5": native["h5"], "microscopy.czi": native["czi"], "detector.edf": edf, "detector.spe": spe,
                    "wrong-magic.h5": native["czi"], "physiological.edf": signal_fixture("edf"), "compressed.czi": native["compressed_czi"]}
                fixture_names = [prefix + "-" + name for name in fixtures]
                files = {name: await upload(name, data) for name, data in fixtures.items()}
                array_id, czi_id, instrument_id = PLUGIN_IDS
                tree = await preview(files["arrays.h5"], array_id, "h5-tree")
                assert tree.kind == "tree" and tree.payload["selected"] == {}
                variables = {item["label"]: item for item in tree.payload["choices"]["variables"]}
                assert all(variables[key]["selectable"] for key in ("signal", "image"))
                slice1 = {"variable": variables["signal"]["id"], "selection": [{"start": 3, "stop": 17, "step": 3}], "decode": "raw"}
                series = await preview(files["arrays.h5"], array_id, "h5-exact-series", kind="series", version=tree.version, options=slice1)
                assert series.kind == "array" and series.payload["array"]["values"] == [3, 6, 9, 12, 15] and series.payload["selected"] == slice1
                slice2 = {"variable": variables["image"]["id"], "selection": [{"start": 2, "stop": 5, "step": 1}, {"start": 3, "stop": 8, "step": 2}], "decode": "raw"}
                array = await preview(files["arrays.h5"], array_id, "h5-exact-image", kind="image", version=tree.version, options=slice2)
                assert array.kind == "array" and array.payload["array"]["shape"] == [3, 3] and array.payload["array"]["values"] == [27, 29, 31, 39, 41, 43, 51, 53, 55]
                assert array.payload["selected"] == slice2 and array.metadata["chunks_touched"] == 4
                czi_tree = await preview(files["microscopy.czi"], czi_id, "czi-range-tree")
                assert czi_tree.kind == "tree" and czi_tree.metadata["coverage"] == "not decoded" and czi_tree.metadata["dimension_sizes"] == {"C": 2, "Z": 2, "T": 1}
                roi = {"indices": [1, 1, 0], "roi": [2, 1, 3, 2]}
                czi_image = await preview(files["microscopy.czi"], czi_id, "czi-range-roi", kind="image", version=czi_tree.version, options=roi)
                png(czi_image, 3, 2)
                assert czi_image.payload["selected"] == roi and czi_image.metadata["display_range"] == [1110, 1120] and czi_image.metadata["coverage"] == "complete and non-overlapping"
                detector_roi = {"frame": 1, "roi": [1, 1, 2, 2]}
                detector_versions = {}
                for extension in ("edf", "spe"):
                    name = "detector." + extension
                    detector = await preview(files[name], instrument_id, extension + "-image-tree")
                    assert detector.kind == "tree" and detector.metadata["frame_count"] == 2 and detector.metadata["frame_shape"] == [3, 4]
                    detector_versions[extension] = detector.version
                    rendered = await preview(files[name], instrument_id, extension + "-image-roi", kind="image", version=detector.version, options=detector_roi)
                    png(rendered, 2, 2)
                    assert rendered.payload["selected"] == detector_roi and rendered.metadata["display_range"] == [105, 110]
                    assert rendered.metadata["read_bytes"] == rendered.metadata["header_bytes"] + 8
                for label, filename, plugin, selection, kind, version in (("h5", "arrays.h5", array_id, slice1, "series", tree.version), ("czi", "microscopy.czi", czi_id, roi, "image", czi_tree.version), ("edf", "detector.edf", instrument_id, detector_roi, "image", detector_versions["edf"]), ("spe", "detector.spe", instrument_id, detector_roi, "image", detector_versions["spe"])):
                    await negative(label + "-missing-version", files[filename], plugin, kind=kind, options=selection)
                    await negative(label + "-stale-version", files[filename], plugin, 409, kind=kind, version="0" * 64, options=selection)
                    await negative(label + "-unknown-option", files[filename], plugin, kind=kind, version=version, options={**selection, "url": "https://invalid.example/not-read"})
                await negative("h5-wrong-extension", files["microscopy.czi"], array_id)
                await negative("h5-wrong-magic", files["wrong-magic.h5"], array_id)
                await negative("h5-forged-variable", files["arrays.h5"], array_id, kind="series", version=tree.version, options={**slice1, "variable": "v-" + "0" * 32})
                await negative("h5-implicit-cf-decode", files["arrays.h5"], array_id, kind="series", version=tree.version, options={**slice1, "decode": "cf"})
                await negative("h5-excessive-window", files["arrays.h5"], array_id, kind="series", version=tree.version, options={**slice1, "selection": [{"start": 0, "stop": 1000000, "step": 1}]})
                await negative("czi-wrong-format", files["arrays.h5"], czi_id)
                await negative("czi-compressed-no-fallback", files["compressed.czi"], czi_id)
                await negative("czi-channel-oob", files["microscopy.czi"], czi_id, kind="image", version=czi_tree.version, options={"indices": [2, 0, 0], "roi": [0, 0, 1, 1]})
                await negative("czi-excessive-roi", files["microscopy.czi"], czi_id, kind="image", version=czi_tree.version, options={"indices": [0, 0, 0], "roi": [0, 0, 1025, 1]})
                await negative("instrument-wrong-extension", files["arrays.h5"], instrument_id)
                await negative("physiological-edf-not-diffraction", files["physiological.edf"], instrument_id)
                await negative("instrument-frame-oob", files["detector.spe"], instrument_id, kind="image", version=detector_versions["spe"], options={"frame": 2, "roi": [0, 0, 1, 1]})
                await negative("instrument-excessive-roi", files["detector.edf"], instrument_id, kind="image", version=detector_versions["edf"], options={"frame": 0, "roi": [0, 0, 1025, 1]})
                await negative("unknown-file", "batch-three-missing-" + uuid.uuid4().hex, czi_id, 404)
                for plugin, name in zip(PLUGIN_IDS, ("arrays.h5", "microscopy.czi", "detector.edf")):
                    await state(plugin, False, "disable:" + plugin)
                    assert (await catalog("confirm-disabled:" + plugin))[plugin]["enabled"] is False
                    await negative("disabled:" + plugin, files[name], plugin, 403)
            except BaseException as caught:
                error = caught
            finally:
                for plugin in reversed(PLUGIN_IDS):
                    if plugin in touched:
                        try: await request("PATCH", f"/api/v1/visualizations/{plugin}/state", label="restore:" + plugin, json={"enabled": originals[plugin]})
                        except Exception: cleanup_errors.append("preference_restore_failed:" + plugin)
                try:
                    restored = await catalog("restored-catalog")
                    preferences_restored = bool(originals) and all(restored[plugin]["enabled"] == enabled for plugin, enabled in originals.items())
                    if not preferences_restored: cleanup_errors.append("preference_readback_mismatch")
                except Exception: cleanup_errors.append("preference_readback_failed")
                # Never delete a merely returned ID. Resolve exact run tag AND
                # one of seven predeclared filenames before deleting by ID.
                selector = {"filename": {"$in": fixture_names}, "metadata.source": SOURCE, "metadata.regression_run": prefix}
                safe_ids = []
                if fixture_names:
                    try:
                        records = await database.stored_files.find(selector, {"_id": 0, "file_id": 1}).limit(8).to_list()
                        assert len(records) <= 7
                        safe_ids = [record["file_id"] for record in records]
                        assert len(safe_ids) == len(set(safe_ids)) and all(isinstance(v, str) and ID.fullmatch(v) for v in safe_ids)
                        if set(uploaded) - set(safe_ids): cleanup_errors.append("upload_id_not_bound_to_exact_run_tag")
                    except Exception: cleanup_errors.append("fixture_inventory_check_failed"); safe_ids = []
                for file_id in reversed(safe_ids):
                    try:
                        # Revalidate the binding at the actual mutation boundary.
                        assert await database.stored_files.count_documents({**selector, "file_id": file_id}) == 1
                        path = "/api/v1/files/" + quote(file_id, safe="")
                        response = await raw("DELETE", path, label="delete-fixture")
                        assert response.status_code in {200, 404}
                        assert (await raw("GET", path + "/info", label="confirm-fixture-deleted")).status_code == 404
                        deleted.append(file_id)
                    except Exception: cleanup_errors.append("fixture_delete_failed:" + file_id)
                if fixture_names:
                    try:
                        if await database.stored_files.count_documents(selector): cleanup_errors.append("tagged_fixtures_remain")
                    except Exception: cleanup_errors.append("cleanup_readback_failed")
            after = await _integrity_snapshot(database)
            passed = error is None and not cleanup_errors and preferences_restored and before == after
            print(json.dumps({"run": prefix, "passed": passed, "successful_checks": checks, "negative_http_checks": negatives,
                "http_attempts": attempts, "native_fixture_versions": provenance, "fixtures_uploaded": len(uploaded), "fixtures_deleted": len(deleted),
                "preferences_restored": preferences_restored, "business_state_preserved": before == after,
                "business_counts_before": before, "business_counts_after": after, "sessions_or_model_requests_created": 0,
                "scope": "small synthetic single-file HTTP checks; large-file efficiency, cross-owner and multi-object authorization are separately tested",
                "cleanup_errors": cleanup_errors, "failure_type": type(error).__name__ if error else None}, ensure_ascii=False, indent=2), flush=True)
            if not passed: raise RuntimeError("Batch-three HTTP acceptance failed; inspect summary and cleanup status") from error
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
