"""Local batch-four HTTP acceptance, using only disposable synthetic fixtures.

Run AFTER deployment, serially with other acceptance runs. The only writes are
13 exact-tagged uploads/deletions, the five plugin preferences (restored), and
temporary owner CAS changes on those same synthetic files (restored before
deletion). No original dataset, session, Agent or model endpoint is used.
Native fixtures are generated in the existing sandbox image without mounts,
network, package installation or privileges. This module does nothing on import.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from urllib.parse import quote, urlparse

import docker
import httpx

from app.application.services.dicom_window_visualization import validate_dicom_window_payload
from app.application.services.gro_trajectory_visualization import validate_gro_trajectory_payload
from app.application.services.pointcloud_window_visualization import validate_pointcloud_window_payload
from app.application.services.simulation_mesh_visualization import validate_simulation_mesh_payload
from app.application.services.spatial_window_visualization import validate_spatial_window_payload
from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from check_batch_one_visualization_http import _integrity_snapshot

PLUGINS = ("viz-dicom-window", "viz-spatial-window", "viz-pointcloud-window",
           "viz-gro-trajectory", "viz-simulation-mesh")
VALIDATORS = dict(zip(PLUGINS, (validate_dicom_window_payload, validate_spatial_window_payload,
    validate_pointcloud_window_payload, validate_gro_trajectory_payload, validate_simulation_mesh_payload)))
SOURCE = "domain_batch_four_visualization_http_regression"
ID = re.compile(r"[A-Za-z0-9_:-]{1,256}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
EXPECTED_FILES = {"explicit.dcm", "implicit.dicom", "dense.h5ad", "csr.h5ad", "csc.h5ad",
                  "points.las", "trajectory.gro", "mesh.vtu", "bad.dcm", "bad.h5ad",
                  "bad.las", "bad.gro", "bad.vtu"}
FORBIDDEN = ("SYNTHETIC-NAME-MUST-NOT-LEAK", "SYNTHETIC-ID-MUST-NOT-LEAK",
             "identity-", "PRIVATE-FIXTURE-SHOULD-NOT-LEAK", "/Users/", "/home/",
             "/private/", "/var/folders/", "file://")
NATIVE_CODE = r'''
import base64,json,os,sys
sys.path.insert(0,"/app/tests")
from dicom_window_fixtures import fixture
from spatial_window_fixtures import spatial_bytes
from pointcloud_window_fixtures import las_bytes
from geometry_fixtures import gro_bytes,mesh_bytes
import h5py,numpy,scipy
assert os.getuid()==65534
las=bytearray(las_bytes());las[104]|=128  # LASzip/compressed point-format bit.
files={"explicit.dcm":fixture()[0],"implicit.dicom":fixture(explicit=False)[0],
 "dense.h5ad":spatial_bytes("dense"),"csr.h5ad":spatial_bytes("csr"),"csc.h5ad":spatial_bytes("csc"),
 "points.las":las_bytes(),"trajectory.gro":gro_bytes(),"mesh.vtu":mesh_bytes(),
 "bad.dcm":fixture(overrides={0x00280301:(b"CS","YES")})[0],
 "bad.h5ad":b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK", "bad.las":bytes(las),
 "bad.gro":b"Synthetic invalid trajectory\n1\ninvalid fixed columns\n1 1 1\n",
 "bad.vtu":b'<!DOCTYPE VTKFile [<!ENTITY x SYSTEM "file:///private/fixture">]><VTKFile>&x;</VTKFile>'}
print(json.dumps({"versions":{"h5py":h5py.__version__,"numpy":numpy.__version__,"scipy":scipy.__version__,
 "generator":"original synthetic, non-clinical fixtures"},
 "files":{name:base64.b64encode(data).decode() for name,data in files.items()}}),flush=True)
'''


def local_base(value):
    """Allow only the existing Compose frontend or explicitly local port 7001."""
    try:
        address = urlparse(value)
        valid = (address.scheme == "http" and not address.username and not address.password
                 and not address.query and not address.fragment and address.path in {"", "/"}
                 and ((address.hostname == "frontend" and address.port in {None, 80})
                      or (address.hostname in {"localhost", "127.0.0.1", "::1"} and address.port == 7001)))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise RuntimeError("Only the existing local frontend or loopback port 7001 may be tested")
    return value.rstrip("/")


def fixtures():
    client, container = docker.from_env(timeout=5), None
    name = "ai-dataseek-domain-four-fixtures-" + uuid.uuid4().hex
    try:
        container = client.containers.create(image=get_settings().sandbox_image, name=name,
            entrypoint=["/usr/bin/timeout"], command=["--signal=KILL", "55s", "/app/.venv/bin/python", "-c", NATIVE_CODE],
            working_dir="/app", user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="1g", memswap_limit="1g",
            nano_cpus=1_000_000_000, pids_limit=96, tmpfs={"/tmp": "rw,noexec,nosuid,size=32m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            labels={"ai-dataseek.component": "visualization-http-fixture"})
        container.reload()
        config = container.attrs["HostConfig"]
        assert config["NetworkMode"] == "none" and config["ReadonlyRootfs"] is True
        assert container.attrs["Config"]["User"] == "65534:65534" and not container.attrs.get("Mounts")
        container.start()
        assert container.wait(timeout=60).get("StatusCode") == 0, "Synthetic fixture generation failed"
        raw = container.logs(stdout=True, stderr=False)
        assert 0 < len(raw) < 1024**2
        result = json.loads(raw)
        assert set(result) == {"versions", "files"} and set(result["files"]) == EXPECTED_FILES
        files = {key: base64.b64decode(value, validate=True) for key, value in result["files"].items()}
        assert all(0 < len(data) < 256 * 1024 for data in files.values())
        assert set(result["versions"]) == {"h5py", "numpy", "scipy", "generator"}
        assert all(type(v) is str and 0 < len(v) <= 64 for v in result["versions"].values())
        return files, result["versions"]
    finally:
        try:
            if container is None:
                try: container = client.containers.get(name)
                except docker.errors.NotFound: pass
            if container is not None: _remove_worker(container)
        finally:
            client.close()


def validate_result(raw, plugin, kind, options, size, version=None):
    result = VisualizationResult.model_validate(raw)
    assert result.plugin_id == plugin and result.contract_version == 2
    assert HASH.fullmatch(result.version) and HASH.fullmatch(result.revision)
    assert version is None or result.version == version
    assert result.kind == ("array" if kind == "image" else kind)
    assert result.payload["view_kind"] == kind and result.metadata["source_bytes"] == size
    private = {**result.payload, "contract_version": 2, "type": plugin[4:], "reader": plugin[4:],
               "kind": kind, "metadata": result.metadata, "warnings": result.warnings, "sampled": result.sampled}
    private.pop("view_kind")
    binding = {"kind": kind, "options": options, "fmt": result.metadata["format"]}
    binding["source_bytes" if plugin in PLUGINS[:3] else "size"] = size
    VALIDATORS[plugin](private, **binding)
    assert len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) <= 2 * 1024**2
    return result


async def main():
    base = local_base(os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend"))
    data, versions = await asyncio.to_thread(fixtures)  # All generation before application writes.
    tag = "domain-viz-four-regression-" + uuid.uuid4().hex
    foreign = "synthetic-foreign-owner-" + uuid.uuid4().hex
    files, uploaded, deleted, originals, touched, cleanup, checks = {}, set(), set(), {}, set(), [], []
    cleanup_targets, owner_records, owner_pending = {}, {}, set()
    names = [tag + "-" + name for name in data]
    mongo = get_mongodb()
    await mongo.initialize()
    failure = None
    try:
        database = mongo.client[get_settings().mongodb_database]
        before = await _integrity_snapshot(database)
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10), trust_env=False,
                follow_redirects=False, transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            def binding(identifier, filename):
                assert ID.fullmatch(identifier) and filename in names
                return {"file_id": identifier, "filename": filename,
                        "metadata.source": SOURCE, "metadata.regression_run": tag}

            async def request(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                value = response.json()
                assert value["code"] == 0
                return value["data"]

            async def state(plugin, enabled):
                assert plugin in originals and type(enabled) is bool
                touched.add(plugin)  # Includes uncertain PATCH responses.
                await request("PATCH", f"/api/v1/visualizations/{plugin}/state", json={"enabled": enabled})
                catalog = await request("GET", "/api/v1/visualizations")
                assert next(p["enabled"] for p in catalog["plugins"] if p["id"] == plugin) is enabled

            async def preview(name, plugin, *, kind="tree", options=None, version=None,
                              operation="preview", status=200, label=None):
                body = {"plugin_id": plugin, "operation": operation, "kind": kind, "options": options or {}}
                if version is not None: body["version"] = version
                response = await client.post(f"/api/v1/files/{quote(files[name], safe='')}/visualization", json=body)
                assert response.status_code == status, f"{plugin}:{name}:{label or kind} expected {status}, got {response.status_code}"
                assert len(response.content) <= 2 * 1024**2 + 4096, "Response exceeded acceptance budget"
                encoded = json.dumps(response.json(), ensure_ascii=False)
                assert not any(marker.lower() in encoded.lower() for marker in FORBIDDEN), "Preview leaked hidden fixture metadata"
                checks.append({"plugin": plugin, "file": name, "check": label or kind, "status": status})
                print(json.dumps({"check": checks[-1]}), flush=True)
                if status == 200:
                    value = response.json()
                    assert value["code"] == 0
                    result = validate_result(value["data"], plugin, kind, options or {}, len(data[name]), version)
                    assert result.metadata["format"] == {PLUGINS[0]: "dicom", PLUGINS[1]: "h5ad",
                        PLUGINS[2]: "las", PLUGINS[3]: "gro", PLUGINS[4]: "vtu"}[plugin]
                    return result

            async def restore_owner(identifier):
                record = owner_records[identifier]
                exact = {**binding(identifier, record["filename"]), "_id": record["_id"]}
                current = await database.stored_files.find_one(exact, {"user_id": 1})
                assert current is not None, "Synthetic owner binding changed; refusing update"
                if current.get("user_id") == foreign:
                    changed = await database.stored_files.update_one({**exact, "user_id": foreign},
                        {"$set": {"user_id": record["user_id"]}})
                    assert changed.matched_count == 1
                else:
                    assert current.get("user_id") == record["user_id"], "Unexpected owner; refusing overwrite"
                restored = await database.stored_files.find_one({**exact, "user_id": record["user_id"]}, {"_id": 1})
                assert restored is not None
                owner_pending.discard(identifier)

            async def other_owner(name, plugin):
                identifier = files[name]
                record = owner_records[identifier]
                owner_pending.add(identifier)  # Register before CAS, including uncertain DB outcomes.
                try:
                    changed = await database.stored_files.update_one({**binding(identifier, record["filename"]),
                        "_id": record["_id"], "user_id": record["user_id"]}, {"$set": {"user_id": foreign}})
                    assert changed.matched_count == changed.modified_count == 1
                    await preview(name, plugin, status=404, label="foreign-owner")
                finally:
                    await restore_owner(identifier)
                assert (await client.get(f"/api/v1/files/{quote(identifier, safe='')}/info")).status_code == 200

            try:
                catalog = await request("GET", "/api/v1/visualizations")
                assert catalog["engine"] == "cordis"
                by_id = {item["id"]: item for item in catalog["plugins"]}
                for plugin in PLUGINS:
                    item = by_id[plugin]
                    assert item["reader"] == item["adapter"] == plugin[4:] and type(item["enabled"]) is bool
                    assert item["capabilities"]["operations"] == ["preview"] and item["capabilities"]["shared"] is False
                    originals[plugin] = item["enabled"]
                    if not originals[plugin]: await state(plugin, True)
                for name, value in data.items():
                    filename = tag + "-" + name
                    info = await request("POST", "/api/v1/files", files={"file": (filename, value, "application/octet-stream")},
                        data={"metadata": json.dumps({"source": SOURCE, "regression_run": tag})})
                    identifier = info.get("file_id")
                    assert isinstance(identifier, str) and ID.fullmatch(identifier) and identifier not in uploaded
                    assert info["filename"] == filename and info["size"] == len(value)
                    record = await database.stored_files.find_one(binding(identifier, filename),
                        {"_id": 1, "filename": 1, "user_id": 1, "provider": 1})
                    assert record and record["provider"] == "minio" and type(record["user_id"]) is str and record["user_id"]
                    assert record["user_id"] != foreign
                    files[name] = identifier; uploaded.add(identifier)
                    cleanup_targets[identifier] = filename; owner_records[identifier] = record

                trees = {}
                image_options = {"frame": 1, "roi": [1, 1, 2, 2], "confirm_deidentified": True}
                for name, syntax in (("explicit.dcm", "explicit-little"), ("implicit.dicom", "implicit-little")):
                    tree = await preview(name, PLUGINS[0]); trees[name] = tree
                    assert tree.metadata["transfer_syntax"] == syntax and tree.payload["tree"][0]["shape"] == [2, 3, 4]
                    assert "array" not in tree.payload and tree.metadata["read_bytes"] <= tree.metadata["header_bytes"]
                    image = await preview(name, PLUGINS[0], kind="image", version=tree.version, options=image_options)
                    assert image.payload["array"]["shape"] == [2, 2] and image.payload["array"]["values"] == [1110, 1120, 1210, 1220]
                    assert image.payload["choices"]["image"]["rescale"] == {"slope": 2, "intercept": -1000, "unit": "HU", "declared": True}
                    assert image.metadata["read_bytes"] < len(data[name])
                for confirmation in (False, 1):
                    await preview("explicit.dcm", PLUGINS[0], kind="image", version=trees["explicit.dcm"].version,
                        options={**image_options, "confirm_deidentified": confirmation}, status=422, label="privacy-confirmation")
                await preview("explicit.dcm", PLUGINS[0], kind="image", version=trees["explicit.dcm"].version,
                    options={**image_options, "roi": [0, 0, 129, 1]}, status=422, label="roi-budget")

                spatial_options = {"feature": 1, "observation_start": 1, "observation_count": 2, "decode": "raw"}
                for storage in ("dense", "csr", "csc"):
                    name = storage + ".h5ad"
                    tree = await preview(name, PLUGINS[1]); trees[name] = tree
                    assert tree.metadata["matrix"]["storage"] == storage and tree.metadata["numeric_bytes_read"] == 0
                    assert "spatial" not in tree.payload and tree.payload["choices"]["feature_labels"] == "zero-based ordinal"
                    geometry = await preview(name, PLUGINS[1], kind="geometry", version=tree.version, options=spatial_options)
                    assert geometry.payload["spatial"] == {"x": [2, 3], "y": [20, 30], "values": [0, 5], "observations": [1, 2]}
                    assert geometry.sampled is True and geometry.metadata["coordinates"]["unit"] is None
                await preview("dense.h5ad", PLUGINS[1], kind="geometry", version=trees["dense.h5ad"].version,
                    options={**spatial_options, "feature": 3}, status=422, label="feature-bounds")

                point_options = {"point_offset": 8, "point_count": 4}
                tree = await preview("points.las", PLUGINS[2]); trees["points.las"] = tree
                assert tree.metadata["total_points"] == 64 and tree.metadata["point_bytes"] == 0 and "array" not in tree.payload
                geometry = await preview("points.las", PLUGINS[2], kind="geometry", version=tree.version, options=point_options)
                assert geometry.payload["array"]["values"] == [0, 10, 2, 10, 10, 4, 20, 10, 6, 30, 10, 8]
                assert geometry.payload["point_attributes"]["classification"] == [72, 73, 74, 75]
                assert geometry.payload["point_attributes"]["intensity"] == [i * 997 for i in range(8, 12)]
                assert geometry.metadata["window_bounds"] == [[600000, 600007.5], [4500005, 4500005], [100.25, 101]]
                assert geometry.metadata["point_bytes"] == 4 * 36 and geometry.metadata["read_bytes"] == 375 + 4 * 36
                await preview("points.las", PLUGINS[2], kind="geometry", version=tree.version,
                    options={"point_offset": 63, "point_count": 2}, status=422, label="point-bounds")

                tree = await preview("trajectory.gro", PLUGINS[3]); trees["trajectory.gro"] = tree
                assert tree.metadata["frame_count"] == 2 and "trajectory" not in tree.payload
                geometry = await preview("trajectory.gro", PLUGINS[3], kind="geometry", version=tree.version, options={"frame": 1})
                assert geometry.payload["trajectory"]["positions"] == [[.01, 0, 0], [.11, .2, .05], [.21, 0, .1], [.01, .2, .15]]
                assert geometry.payload["trajectory"]["velocities"] == [[-.001, .002, 0]] * 4
                assert geometry.payload["choices"]["frames"][1]["time_ps"] == .25 and geometry.metadata["coordinate_unit"] == "nm"
                await preview("trajectory.gro", PLUGINS[3], kind="geometry", version=tree.version,
                    options={"frame": 2}, status=422, label="frame-bounds")

                tree = await preview("mesh.vtu", PLUGINS[4]); trees["mesh.vtu"] = tree
                assert tree.metadata["point_count"] == 5 and tree.metadata["cell_count"] == 2 and "mesh" not in tree.payload
                for field, component, expected, association in (("p-0", 1, [2, 5, 8, 11, 14], "point"),
                        ("c-0", 0, [-2, 8], "cell"), ("p-1", 0, [10, 20, None, 40, 50], "point")):
                    geometry = await preview("mesh.vtu", PLUGINS[4], kind="geometry", version=tree.version,
                        options={"field": field, "component": component})
                    assert geometry.payload["mesh"]["field"] == {"id": field, "component": component, "association": association, "values": expected}
                    assert geometry.payload["mesh"]["cells"] == [{"type": 10, "points": [0, 1, 2, 3]}, {"type": 10, "points": [1, 2, 3, 4]}]
                await preview("mesh.vtu", PLUGINS[4], kind="geometry", version=tree.version,
                    options={"field": "p-0", "component": 3}, status=422, label="component-bounds")

                cases = [(PLUGINS[0], "explicit.dcm", "image", image_options, "bad.dcm"),
                         (PLUGINS[1], "dense.h5ad", "geometry", spatial_options, "bad.h5ad"),
                         (PLUGINS[2], "points.las", "geometry", point_options, "bad.las"),
                         (PLUGINS[3], "trajectory.gro", "geometry", {"frame": 1}, "bad.gro"),
                         (PLUGINS[4], "mesh.vtu", "geometry", {"field": "p-0", "component": 1}, "bad.vtu")]
                for plugin, name, kind, selection, bad in cases:
                    await preview(name, plugin, kind=kind, options=selection, status=422, label="missing-version")
                    stale = ("0" if trees[name].version[0] != "0" else "1") * 64
                    await preview(name, plugin, version=stale, status=409, label="stale-version")
                    await preview(name, plugin, options={"unexpected": True}, status=422, label="undeclared-option")
                    for operation in ("bytes", "page", "prepare"):
                        await preview(name, plugin, operation=operation, status=422, label="undeclared-operation:" + operation)
                    other = "points.las" if plugin == PLUGINS[0] else "explicit.dcm"
                    await preview(other, plugin, status=422, label="format-mismatch")
                    await preview(bad, plugin, status=422, label="unsupported-or-malicious-source")
                    await other_owner(name, plugin)
                    await state(plugin, False)
                    await preview(name, plugin, status=403, label="disabled-plugin")
            except BaseException as caught:
                failure = caught
            finally:
                for identifier in tuple(owner_pending):
                    try: await restore_owner(identifier)
                    except Exception: cleanup.append("owner_restore:" + identifier)
                for plugin in touched:
                    try: await state(plugin, originals[plugin])
                    except Exception: cleanup.append("preference_restore:" + plugin)
                # Recover a successful upload whose response was lost. Never retry
                # POST, never discover or delete targets by prefix alone.
                try:
                    records = await database.stored_files.find({"filename": {"$in": names}, "metadata.source": SOURCE,
                        "metadata.regression_run": tag}, {"_id": 0, "file_id": 1, "filename": 1}).limit(len(names) + 1).to_list()
                    assert len(records) <= len(names)
                    for record in records:
                        identifier, filename = record.get("file_id"), record.get("filename")
                        assert type(identifier) is str and ID.fullmatch(identifier) and filename in names
                        uploaded.add(identifier); cleanup_targets[identifier] = filename
                except Exception: cleanup.append("fixture_inventory")
                for identifier, filename in cleanup_targets.items():
                    try:
                        assert identifier not in owner_pending, "Owner restore failed; refusing deletion"
                        exact = binding(identifier, filename)
                        if identifier in owner_records:
                            exact.update({"_id": owner_records[identifier]["_id"], "user_id": owner_records[identifier]["user_id"]})
                        path = "/api/v1/files/" + quote(identifier, safe="")
                        record = await database.stored_files.find_one(exact, {"_id": 1})
                        if record is None:
                            assert await database.stored_files.count_documents({"file_id": identifier}) == 0, "Binding changed; refusing deletion"
                            assert (await client.get(path + "/info")).status_code == 404
                        else:
                            response = await client.delete(path)
                            assert response.status_code in {200, 404}
                            if response.status_code == 200: assert response.json()["code"] == 0
                            assert (await client.get(path + "/info")).status_code == 404
                            assert await database.stored_files.count_documents({"file_id": identifier}) == 0
                        deleted.add(identifier)
                    except Exception: cleanup.append("fixture_delete:" + identifier)
                try:
                    catalog = await request("GET", "/api/v1/visualizations")
                    states = {p["id"]: p["enabled"] for p in catalog["plugins"]}
                    assert all(states[p] is v for p, v in originals.items())
                except Exception: cleanup.append("preference_readback")
                try:
                    assert not await database.stored_files.count_documents({"filename": {"$in": names},
                        "metadata.source": SOURCE, "metadata.regression_run": tag})
                except Exception: cleanup.append("fixture_readback")
        after = await _integrity_snapshot(database)
        passed = failure is None and not cleanup and before == after and uploaded == deleted and not owner_pending
        print(json.dumps({"passed": passed, "run": tag, "versions": versions, "checks": checks,
            "positive": sum(c["status"] == 200 for c in checks), "negative": sum(c["status"] != 200 for c in checks),
            "fixtures_uploaded": len(uploaded), "fixtures_deleted": len(deleted), "cleanup_errors": cleanup,
            "business_state_preserved": before == after, "sessions_or_model_calls_created": 0,
            "failure_type": type(failure).__name__ if failure else None}, ensure_ascii=False, indent=2), flush=True)
        if not passed: raise RuntimeError("Domain batch four acceptance failed; inspect checks and cleanup status") from None
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
