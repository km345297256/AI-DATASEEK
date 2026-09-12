"""Local batch-five HTTP acceptance, using only disposable synthetic fixtures.

Run AFTER deployment, serially with other acceptance runs. The only writes are
9 exact-tagged uploads/deletions, the three plugin preferences (restored), and
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
import time
import uuid
from urllib.parse import quote, urlparse

import docker
import httpx

from app.application.services.sqlite_table_visualization import validate_sqlite_table_payload
from app.application.services.radar_window_visualization import validate_radar_window_payload
from app.application.services.ugrid_window_visualization import validate_ugrid_window_payload
from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from check_batch_one_visualization_http import _integrity_snapshot

PLUGINS = ("viz-sqlite-table", "viz-radar-window", "viz-ugrid-window")
VALIDATORS = dict(zip(PLUGINS, (validate_sqlite_table_payload, validate_radar_window_payload, validate_ugrid_window_payload)))
SOURCE = "domain_batch_five_visualization_http_regression"
ID = re.compile(r"[A-Za-z0-9_:-]{1,256}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
EXPECTED_FILES = {"sample.sqlite","view.db","wal.sqlite3","positive.h5","negative.hdf5","old.h5","node.nc","face.nc4","bad.nc"}
FORBIDDEN = ("SYNTHETIC-NAME-MUST-NOT-LEAK", "SYNTHETIC-ID-MUST-NOT-LEAK",
             "identity-", "PRIVATE-FIXTURE-SHOULD-NOT-LEAK", "/Users/", "/home/",
             "/private/", "/var/folders/", "file://")
NATIVE_CODE = r'''
import base64,json,os,sys
sys.path.insert(0,"/app/tests")
from sqlite_table_fixtures import sqlite_bytes
from radar_window_fixtures import radar_bytes,mutate
from ugrid_window_fixtures import ugrid_bytes
import apsw,h5py,numpy
assert os.getuid()==65534
wal=bytearray(sqlite_bytes());wal[18:20]=bytes([2,2])
def old(h): h.attrs.modify("Conventions",numpy.bytes_("ODIM_H5/V2_3"))
files={"sample.sqlite":sqlite_bytes(),"view.db":sqlite_bytes("CREATE TABLE x(a);CREATE VIEW y AS SELECT a FROM x;"),
 "wal.sqlite3":bytes(wal),"positive.h5":radar_bytes(),"negative.hdf5":radar_bytes(gain=-.25),
 "old.h5":mutate(radar_bytes(),old),"node.nc":ugrid_bytes(start=1,transpose=True,packed=True),
 "face.nc4":ugrid_bytes(compression=True),"bad.nc":b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK"}
print(json.dumps({"versions":{"h5py":h5py.__version__,"numpy":numpy.__version__,"apsw":apsw.apswversion(),
 "generator":"original synthetic fixtures"},
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
    # A cold, large scientific image can take longer than five seconds to
    # create. This timeout is test orchestration, not a preview input budget.
    client, container = docker.from_env(timeout=30), None
    name = "ai-dataseek-domain-five-fixtures-" + uuid.uuid4().hex
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
        assert set(result["versions"]) == {"h5py", "numpy", "apsw", "generator"}
        assert all(type(v) is str and 0 < len(v) <= 64 for v in result["versions"].values())
        return files, result["versions"]
    finally:
        try:
            if container is None:
                # Recover a server-side create whose client response timed out.
                # A single early 404 is not proof that creation cannot finish.
                deadline = time.monotonic() + 10
                while container is None and time.monotonic() < deadline:
                    try: container = client.containers.get(name)
                    except docker.errors.NotFound: time.sleep(.25)
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
    binding["size" if plugin == PLUGINS[0] else "source_bytes"] = size
    VALIDATORS[plugin](private, **binding)
    assert len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) <= 2 * 1024**2
    return result


async def main():
    base = local_base(os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend"))
    data, versions = await asyncio.to_thread(fixtures)  # All generation before application writes.
    tag = "domain-viz-five-regression-" + uuid.uuid4().hex
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
                checks.append({"plugin": plugin, "file": name, "check": label or kind, "status": response.status_code, "expected_status": status})
                print(json.dumps({"check": checks[-1]}), flush=True)
                assert response.status_code == status, f"{plugin}:{name}:{label or kind} expected {status}, got {response.status_code}"
                assert len(response.content) <= 2 * 1024**2 + 4096, "Response exceeded acceptance budget"
                encoded = json.dumps(response.json(), ensure_ascii=False)
                assert not any(marker.lower() in encoded.lower() for marker in FORBIDDEN), "Preview leaked hidden fixture metadata"
                if status == 200:
                    value = response.json()
                    assert value["code"] == 0
                    result = validate_result(value["data"], plugin, kind, options or {}, len(data[name]), version)
                    assert result.metadata["format"] == name.rsplit(".",1)[-1]
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
                tree = await preview("sample.sqlite", PLUGINS[0]); trees["sample.sqlite"] = tree
                table = next(t for t in tree.payload["choices"]["tables"] if t["label"] == "measurements")
                sql_options = {"table":table["id"],"columns":[0,1,2,3,4,5],"row_offset":0,"row_limit":2}
                first = await preview("sample.sqlite", PLUGINS[0],kind="table",version=tree.version,options=sql_options)
                assert first.payload["table"]["row_ids"] == ["-5","0"] and first.payload["table"]["has_more"] is True
                assert first.payload["table"]["rows"][0][1] == {"type":"integer","value":"-9223372036854775808"}
                assert first.payload["table"]["rows"][1][1] == {"type":"integer","value":"9223372036854775807"}
                assert first.payload["table"]["rows"][0][4] == {"type":"blob","bytes":3}
                second = await preview("sample.sqlite", PLUGINS[0],kind="table",version=tree.version,options={**sql_options,"row_offset":2})
                assert second.payload["table"]["row_ids"] == ["9","25"]
                assert second.payload["table"]["rows"][0][1] == {"type":"integer","value":"9007199254740993"}
                assert second.payload["table"]["rows"][0][3] == {"type":"text-omitted","bytes":513,"reason":"cell-budget"}
                assert second.payload["table"]["rows"][1][2] == {"type":"nonfinite","value":None}
                await preview("sample.sqlite",PLUGINS[0],kind="table",version=tree.version,options={**sql_options,"row_limit":201},status=422,label="page-budget")
                await preview("wal.sqlite3",PLUGINS[0],status=422,label="wal-rejected")

                radar_options = {"sweep":1,"quantity":"DBZH","ray_start":1,"ray_count":2,"gate_start":1,"gate_count":4,"decode":"raw"}
                for name in ("positive.h5","negative.hdf5"):
                    tree = await preview(name,PLUGINS[1]); trees[name] = tree
                    assert tree.metadata["odim_version"] == "ODIM_H5/V2_4" and "array" not in tree.payload
                    image = await preview(name,PLUGINS[1],kind="image",version=tree.version,options=radar_options)
                    assert image.payload["array"]["shape"] == [2,4]
                    assert image.payload["array"]["values"] == [255,0,18,19,26,27,28,29]
                    sweep = image.payload["choices"]["sweeps"][0]
                    quantity = next(q for q in sweep["quantities"] if q["id"] == "DBZH")
                    assert sweep["a1gate"] == 3 and quantity["offset"] == -32
                    assert quantity["gain"] == (.5 if name == "positive.h5" else -.25)
                    assert image.payload["radar"]["range_m"] == [1375,1625,1875,2125]
                    assert image.payload["radar"]["nodata_count"] == image.payload["radar"]["undetect_count"] == 1
                await preview("positive.h5",PLUGINS[1],kind="image",version=trees["positive.h5"].version,
                    options={**radar_options,"ray_count":129},status=422,label="ray-budget")
                ugrid_options = None
                for name,location in (("node.nc","node"),("face.nc4","face")):
                    tree = await preview(name,PLUGINS[2]); trees[name] = tree
                    mesh = tree.payload["choices"]["meshes"][0]
                    field = next(f for f in mesh["fields"] if f["location"] == location)
                    selection = {"mesh":mesh["id"],"field":field["id"],"indices":[1,2] if location=="node" else [1]}
                    geometry = await preview(name,PLUGINS[2],kind="geometry",version=tree.version,options=selection)
                    assert geometry.payload["ugrid"]["faces"] == [[0,1,2,3],[1,4,2]]
                    assert geometry.payload["ugrid"]["values"] == ([17,20,None,26,29] if location=="node" else [2,4])
                    assert geometry.metadata["topology_complete"] is True
                    if location == "node": ugrid_options = selection
                await preview("node.nc",PLUGINS[2],kind="geometry",version=trees["node.nc"].version,
                    options={**ugrid_options,"indices":[2,0]},status=422,label="dimension-bounds")

                cases = [(PLUGINS[0],"sample.sqlite","table",sql_options,"view.db"),
                         (PLUGINS[1],"positive.h5","image",radar_options,"old.h5"),
                         (PLUGINS[2],"node.nc","geometry",ugrid_options,"bad.nc")]
                for plugin, name, kind, selection, bad in cases:
                    await preview(name, plugin, kind=kind, options=selection, status=422, label="missing-version")
                    stale = ("0" if trees[name].version[0] != "0" else "1") * 64
                    await preview(name, plugin, version=stale, status=409, label="stale-version")
                    await preview(name, plugin, options={"unexpected": True}, status=422, label="undeclared-option")
                    for operation in ("bytes", "page", "prepare"):
                        await preview(name, plugin, operation=operation, status=422, label="undeclared-operation:" + operation)
                    other = "positive.h5" if plugin == PLUGINS[0] else "sample.sqlite"
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
            "business_state_preserved": before == after, "script_model_endpoint_calls": 0,
            "failure_type": type(failure).__name__ if failure else None}, ensure_ascii=False, indent=2), flush=True)
        if not passed: raise RuntimeError("Domain batch five acceptance failed; inspect checks and cleanup status") from None
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
