"""Local, serial HTTP acceptance for the three independent database plugins.

Only original synthetic fixtures are uploaded. Their exact declaration and
persisted owner/document binding, never an HTTP-returned ID alone, authorize
cleanup. Lost POST responses are recovered from that binding, never reposted.
The three plugin preferences are restored in finally. No owner mutation, model
call, session creation, business-file read, schema migration or job is made.
This module performs no work on import; run only after deployment is finished.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import uuid
from urllib.parse import quote

import docker
import httpx

from app.application.services.database_table_visualization import validate_database_table_payload
from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from app.interfaces.dependencies import get_current_user
from check_batch_one_visualization_http import _integrity_snapshot
from visualization_acceptance_safety import FixtureLedger, local_base

PLUGINS = ("viz-duckdb-table", "viz-dbf-table", "viz-access-table")
SOURCE = "database_visualization_http_regression"
HASH = re.compile(r"[0-9a-f]{64}\Z")
FIXTURE_BYTES = 4 * 1024**2
EXPECTED_FILES = {"sample.duckdb", "bad.ddb", "sample.dbf", "empty.dbf", "bad.dbf",
                  "sample.mdb", "sample.accdb", "linked.mdb", "complex.accdb"}
FORBIDDEN = ("PRIVATE-FIXTURE-SHOULD-NOT-LEAK", "/Users/", "/home/", "/private/", "/var/folders/", "file://", "intentionally-nonexistent")
NATIVE_CODE = r'''
import ast,base64,ctypes,json,os,struct,sys
from pathlib import Path
sys.path.insert(0,"/app/tests")
from database_table_fixtures import duckdb_bytes
import duckdb,dbfread
assert os.getuid()==65534
# Select only the trusted fixture builder, without importing pytest or running
# tests in the production image. No model/user-provided source is evaluated.
tree=ast.parse(Path("/app/tests/test_dbf_table_reader.py").read_text())
functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="dbf_bytes"]
assert len(functions)==1
exec(compile(ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[])),"synthetic-dbf-fixture","exec"))
dbf=dbf_bytes()
bad=bytearray(dbf);bad[0]=0x83
root=Path("/app/tests/fixtures/database")
access={"sample.mdb":"synthetic-v2000.mdb","sample.accdb":"synthetic-v2010.accdb",
 "linked.mdb":"synthetic-linked-v2000.mdb","complex.accdb":"synthetic-memo-ole-v2010.accdb"}
files={"sample.duckdb":duckdb_bytes(),"bad.ddb":b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK",
 "sample.dbf":dbf,"empty.dbf":dbf_bytes([]),"bad.dbf":bytes(bad),
 **{name:(root/source).read_bytes() for name,source in access.items()}}
assert all(type(v) is bytes and 0<len(v)<=2*1024**2 for v in files.values())
lib=ctypes.CDLL("/opt/dataseek-database/lib/libmdb.so.3");lib.mdb_get_version.restype=ctypes.c_char_p
versions={"duckdb":duckdb.__version__,"dbfread":dbfread.__version__,"libmdb":lib.mdb_get_version().decode("ascii"),
 "access_writer":"Jackcess 4.0.11 original synthetic fixtures"}
assert versions["duckdb"]=="1.5.5" and versions["dbfread"]=="2.0.7" and versions["libmdb"]=="1.0.1"
encoded=json.dumps({"versions":versions,"files":{n:base64.b64encode(d).decode() for n,d in files.items()}})
assert len(encoded.encode())<=4*1024**2
print(encoded,flush=True)
'''


def decode_fixtures(chunks):
    raw = bytearray()
    for chunk in chunks:
        assert type(chunk) is bytes and len(raw) + len(chunk) <= FIXTURE_BYTES, "Fixture output budget exceeded"
        raw.extend(chunk)
    assert raw, "Empty fixture output"
    value = json.loads(raw)
    assert type(value) is dict and set(value) == {"versions", "files"}
    assert type(value["files"]) is dict and set(value["files"]) == EXPECTED_FILES
    files = {}
    for name, encoded in value["files"].items():
        assert type(encoded) is str and len(encoded) <= FIXTURE_BYTES
        decoded = base64.b64decode(encoded, validate=True)
        assert 0 < len(decoded) <= 2 * 1024**2
        files[name] = decoded
    assert sum(map(len, files.values())) <= FIXTURE_BYTES
    assert value["versions"] == {"duckdb": "1.5.5", "dbfread": "2.0.7", "libmdb": "1.0.1",
        "access_writer": "Jackcess 4.0.11 original synthetic fixtures"}
    return files, value["versions"]


def fixtures():
    client, container = docker.from_env(timeout=30), None
    name = "ai-dataseek-database-fixtures-" + uuid.uuid4().hex
    try:
        container = client.containers.create(image=get_settings().sandbox_image, name=name,
            entrypoint=["/usr/bin/timeout"], command=["--signal=KILL", "55s", "/app/.venv/bin/python", "-c", NATIVE_CODE],
            working_dir="/app", user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="1g", memswap_limit="1g",
            nano_cpus=1_000_000_000, pids_limit=96, tmpfs={"/tmp": "rw,noexec,nosuid,size=32m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            log_config={"type": "json-file", "config": {"max-size": "4m", "max-file": "1"}},
            labels={"ai-dataseek.component": "visualization-http-fixture", "ai-dataseek.fixture-run": name})
        container.reload()
        config = container.attrs["HostConfig"]
        assert config["NetworkMode"] == "none" and config["ReadonlyRootfs"] is True
        assert container.attrs["Config"]["User"] == "65534:65534" and not container.attrs.get("Mounts")
        container.start()
        assert container.wait(timeout=60).get("StatusCode") == 0, "Synthetic fixture generation failed"
        return decode_fixtures(container.logs(stdout=True, stderr=False, stream=True, follow=False))
    finally:
        try:
            if container is None:
                deadline = time.monotonic() + 10
                while container is None and time.monotonic() < deadline:
                    try: container = client.containers.get(name)
                    except docker.errors.NotFound: time.sleep(.25)
            if container is not None:
                # A recovered name after an uncertain create is not deletion
                # authority by itself (for example, a pre-existing name clash).
                container.reload()
                labels = container.attrs.get("Config", {}).get("Labels", {})
                assert labels.get("ai-dataseek.component") == "visualization-http-fixture"
                assert labels.get("ai-dataseek.fixture-run") == name, "Fixture container ownership changed; refusing removal"
                _remove_worker(container)
        finally: client.close()


async def assert_idle(database):
    # Waiting/pending sessions may resume; do not interfere with any of them.
    sessions = await database.sessions.count_documents({"status": {"$in": ["pending", "running", "waiting"]}})
    jobs = await database.analysis_jobs.count_documents({"status": {"$in": ["queued", "running", "cancelling"]}})
    if sessions or jobs:
        raise RuntimeError("Database preview acceptance requires zero active sessions and jobs; no application writes started")


async def business_integrity_snapshot(database):
    result = await _integrity_snapshot(database)
    for name in ("stored_files", "analysis_jobs", "spill_artifacts"):
        digest, count = hashlib.sha256(), 0
        projection = {"_id": 1, "file_id": 1, "filename": 1, "user_id": 1, "size": 1, "provider": 1,
            "job_id": 1, "artifact_id": 1, "status": 1, "created_at": 1, "updated_at": 1, "finished_at": 1,
            "owner_user_id": 1, "owner_session_id": 1, "session_id": 1, "metadata": 1}
        async for record in database[name].find({}, projection).sort("_id", 1):
            count += 1
            assert count <= 100000, "Business metadata scan exceeded budget"
            digest.update(json.dumps(record, sort_keys=True, default=str, separators=(",", ":")).encode())
            digest.update(b"\n")
        result[name + "_count"] = count
        result[name + "_metadata_sha256"] = digest.hexdigest()
    return result


async def upload_fixture(client, ledger, name, data):
    filename = ledger.declare(name, len(data))
    # Exactly one POST. Declaration precedes even a lost/invalid response.
    response = await client.post("/api/v1/files", files={"file": (filename, data, "application/octet-stream")},
        data={"metadata": json.dumps({"source": SOURCE, "regression_run": ledger.run})})
    response.raise_for_status()
    value = response.json()
    assert value["code"] == 0
    return await ledger.accept(value["data"])


async def cleanup_fixture(client, ledger, identifier):
    owned = await ledger.assert_owned(identifier)
    path = "/api/v1/files/" + quote(identifier, safe="")
    if owned:
        response = await client.delete(path)
        assert response.status_code in {200, 404}
        if response.status_code == 200: assert response.json()["code"] == 0
    assert (await client.get(path + "/info")).status_code == 404
    await ledger.assert_absent(identifier)


def validate_result(raw, plugin, kind, options, source, fmt, version=None):
    result = VisualizationResult.model_validate(raw)
    assert result.plugin_id == plugin and result.contract_version == 2 and result.kind == kind
    assert HASH.fullmatch(result.version) and HASH.fullmatch(result.revision)
    assert version is None or result.version == version
    assert result.payload["view_kind"] == kind
    private = {**result.payload, "contract_version": 2, "type": "database-table", "reader": "database-table",
        "kind": kind, "metadata": result.metadata, "warnings": result.warnings, "sampled": result.sampled}
    private.pop("view_kind")
    validate_database_table_payload(private, kind=kind, options=options, fmt=fmt, size=len(source))
    assert len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) <= 2 * 1024**2
    return result


async def run_acceptance(database, client, data, versions, user_id, run):
    assert set(data) == EXPECTED_FILES
    await assert_idle(database)
    before = await business_integrity_snapshot(database)
    ledger = FixtureLedger(database, SOURCE, run, user_id=user_id)
    files, originals, touched, checks, errors, deleted = {}, {}, set(), [], [], set()
    failure = None

    async def request(method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        response.raise_for_status(); envelope = response.json(); assert envelope["code"] == 0
        return envelope["data"]

    async def state(plugin, enabled, *, restoring=False):
        assert plugin in originals and type(enabled) is bool
        if not restoring: await assert_idle(database)
        touched.add(plugin)  # Also restore uncertain PATCH outcomes.
        await request("PATCH", f"/api/v1/visualizations/{plugin}/state", json={"enabled": enabled})
        catalog = await request("GET", "/api/v1/visualizations")
        assert next(p["enabled"] for p in catalog["plugins"] if p["id"] == plugin) is enabled

    async def preview(name, plugin, *, kind="tree", options=None, version=None, status=200, label=None, operation="preview"):
        await assert_idle(database)
        body = {"plugin_id": plugin, "operation": operation, "kind": kind, "options": options or {}}
        if version is not None: body["version"] = version
        response = await client.post("/api/v1/files/" + quote(files[name], safe="") + "/visualization", json=body)
        check = {"plugin": plugin, "file": name, "check": label or kind, "status": response.status_code, "expected_status": status}
        checks.append(check); print(json.dumps({"check": check}), flush=True)
        assert response.status_code == status, f"{plugin}:{name}:{label or kind}: HTTP {response.status_code}, expected {status}"
        assert len(response.content) <= 2 * 1024**2 + 4096
        value = response.json(); encoded = json.dumps(value, ensure_ascii=False).lower()
        assert not any(marker.lower() in encoded for marker in FORBIDDEN), "Preview disclosed a hidden path or raw error"
        if status == 200:
            assert value["code"] == 0
            return validate_result(value["data"], plugin, kind, options or {}, data[name], name.rsplit(".", 1)[1], version)

    try:
        catalog = await request("GET", "/api/v1/visualizations")
        assert catalog["engine"] == "cordis" and len(catalog["plugins"]) == 83
        by_id = {p["id"]: p for p in catalog["plugins"]}; assert len(by_id) == 83
        for plugin in PLUGINS:
            item = by_id[plugin]
            assert item["reader"] == item["adapter"] == "database-table" and item["contract_version"] == 2
            assert type(item["enabled"]) is bool and item["capabilities"]["operations"] == ["preview"] and item["capabilities"]["shared"] is False
            originals[plugin] = item["enabled"]
        await assert_idle(database)
        for plugin in PLUGINS:
            if not originals[plugin]: await state(plugin, True)
        for name, source in data.items():
            await assert_idle(database)
            files[name] = await upload_fixture(client, ledger, name, source)

        scenarios = [("sample.duckdb", PLUGINS[0], "measurements", list(range(8))),
                     ("sample.dbf", PLUGINS[1], "DBF table", list(range(4))),
                     ("sample.mdb", PLUGINS[2], "Samples", list(range(8))),
                     ("sample.accdb", PLUGINS[2], "Samples", list(range(8)))]
        first_by_name, trees, selections = {}, {}, {}
        for name, plugin, label, columns in scenarios:
            tree = await preview(name, plugin); trees[name] = tree
            target = next(t for t in tree.payload["choices"]["tables"] if t["label"] == label)
            assert all(target["columns"][i]["previewable"] for i in columns)
            selection = {"table": target["id"], "columns": columns, "row_offset": 0, "row_limit": 2}
            selections[name] = selection
            first = await preview(name, plugin, kind="table", options=selection, version=tree.version); first_by_name[name] = first
            assert first.payload["table"]["row_ids"] == ["0", "1"] and first.payload["table"]["has_more"] is True
            second = await preview(name, plugin, kind="table", options={**selection, "row_offset": 2}, version=tree.version)
            assert second.payload["table"]["has_more"] is False and second.payload["table"]["row_ids"][0] == "2"
            subset = await preview(name, plugin, kind="table", options={**selection, "columns": [0]}, version=tree.version, label="selected-column")
            assert subset.payload["table"]["rows"] == [[row[0]] for row in first.payload["table"]["rows"]]
            if name != "sample.dbf":
                empty = next(t for t in tree.payload["choices"]["tables"] if t["label"].casefold() == "empty")
                empty_result = await preview(name, plugin, kind="table", options={**selection, "table": empty["id"], "columns": [0]}, version=tree.version, label="empty-table")
                assert empty_result.payload["table"]["rows"] == [] and empty_result.payload["table"]["has_more"] is False
            # No file read is authorized until it is bound to this exact run.
            assert await ledger.assert_owned(files[name])
            downloaded = await client.get("/api/v1/files/" + quote(files[name], safe="") + "/download")
            downloaded.raise_for_status()
            assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(data[name]).digest()
            checks.append({"plugin": plugin, "file": name, "check": "source-sha256-unchanged", "status": 200, "expected_status": 200})
            if name == "sample.duckdb":
                rows = first.payload["table"]["rows"]
                assert rows[0][0]["value"] == "170141183460469231731687303715884105727"
                assert rows[0][1] == {"type":"decimal", "value":"1.230000000000000001"}
                assert rows[0][6]["value"] == "2026-09-11 12:00:00.123456789"
                assert rows[0][4] == {"type":"blob", "bytes":3}
                assert second.payload["table"]["rows"][1][3]["reason"] == "unsafe-text"
            elif name == "sample.dbf":
                rows = first.payload["table"]["rows"]
                assert rows[0][1] == {"type":"decimal", "value":"9007199254740993.0001"}
                assert rows[1][0] == {"type":"text", "value":""} and rows[1][1] == {"type":"null", "value":None}
                assert second.payload["table"]["rows"][0][1]["value"] == "-0.0001"
            else:
                rows = first.payload["table"]["rows"]
                assert rows[0][1]["value"] == "科学数据" and rows[0][3]["value"] == "123456789012345678901234.5678"
                assert rows[0][2]["value"] == "1234.5678" and rows[0][7] == {"type":"blob", "bytes":4}
                assert rows[0][4] == {"type":"boolean", "value":True}
                assert rows[0][5] == {"type":"timestamp", "value":"2024-02-29T12:34:56"}
                assert rows[1][1] == {"type":"text", "value":""}
                assert second.payload["table"]["rows"][0][1] == {"type":"null", "value":None}
                assert second.payload["table"]["rows"][1][2] == {"type":"decimal", "value":"922337203685477.5807"}
                assert second.payload["table"]["rows"][1][1]["value"] == "<script>inert</script>"

        empty_tree = await preview("empty.dbf", PLUGINS[1])
        empty = await preview("empty.dbf", PLUGINS[1], kind="table", version=empty_tree.version, options=selections["sample.dbf"])
        assert empty.payload["table"]["rows"] == [] and empty.payload["table"]["has_more"] is False
        complex_tree = await preview("complex.accdb", PLUGINS[2]); complex_table = complex_tree.payload["choices"]["tables"][0]
        assert [c["previewable"] for c in complex_table["columns"]] == [True, False, False]
        complex_selection = {"table":complex_table["id"], "columns":[0], "row_offset":0, "row_limit":2}
        allowed = await preview("complex.accdb", PLUGINS[2], kind="table", version=complex_tree.version, options=complex_selection)
        assert allowed.payload["table"]["rows"] == [[{"type":"integer", "value":"1"}]]
        await preview("complex.accdb", PLUGINS[2], kind="table", version=complex_tree.version, options={**complex_selection,"columns":[1]}, status=422, label="complex-column-rejected")
        for name, plugin, bad, other in [("sample.duckdb", PLUGINS[0], "bad.ddb", "sample.dbf"),
            ("sample.dbf", PLUGINS[1], "bad.dbf", "sample.duckdb"), ("sample.mdb", PLUGINS[2], "linked.mdb", "sample.dbf")]:
            selection, tree = selections[name], trees[name]
            await preview(name, plugin, kind="table", options=selection, status=422, label="missing-version")
            stale = ("0" if tree.version[0] != "0" else "1") * 64
            await preview(name, plugin, version=stale, status=409, label="stale-version")
            for key, value in (("sql", "SELECT * FROM private"), ("path", "/tmp/private"), ("attach", "https://example.invalid")):
                await preview(name, plugin, options={key:value}, status=422, label="unapproved-option:"+key)
            for change, label in [({"row_limit":201},"page-budget"), ({"columns":[True]},"boolean-column"), ({"table":"t-"+"0"*24},"unknown-table")]:
                await preview(name, plugin, kind="table", version=tree.version, options={**selection,**change}, status=422, label=label)
            for operation in ("bytes", "page", "prepare"):
                await preview(name, plugin, operation=operation, status=422, label="undeclared-operation:"+operation)
            await preview(other, plugin, status=422, label="format-mismatch")
            await preview(bad, plugin, status=422, label="unsafe-source")
            await assert_idle(database); await state(plugin, False)
            await preview(name, plugin, status=403, label="disabled-plugin")
    except BaseException as caught:
        failure = caught
    finally:
        for plugin in touched:
            try: await state(plugin, originals[plugin], restoring=True)
            except Exception: errors.append("preference_restore:" + plugin)
        try: await ledger.recover()
        except Exception: errors.append("fixture_inventory")
        for identifier in tuple(ledger.records):
            try:
                await cleanup_fixture(client, ledger, identifier); deleted.add(identifier)
            except Exception: errors.append("fixture_delete:" + identifier)
        try: await ledger.assert_clean()
        except Exception: errors.append("fixture_readback")
        try:
            catalog = await request("GET", "/api/v1/visualizations")
            states = {p["id"]:p["enabled"] for p in catalog["plugins"]}
            assert all(states[p] is enabled for p, enabled in originals.items())
        except Exception: errors.append("preference_readback")
    after = await business_integrity_snapshot(database)
    summary = {"passed": failure is None and not errors and before == after and set(ledger.records) == deleted,
        "run": run, "versions": versions, "checks": checks, "fixtures_uploaded": len(ledger.records), "fixtures_deleted": len(deleted),
        "cleanup_errors": errors, "business_state_preserved": before == after, "plugin_preferences_restored": not any(e.startswith("preference_") for e in errors),
        "existing_user_files_accessed": 0, "script_model_endpoint_calls": 0, "owner_permission_check": "unit coverage; no owner mutation",
        "failure_type": type(failure).__name__ if failure else None}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not summary["passed"]: raise RuntimeError("Database visualization acceptance failed; inspect checks and cleanup status") from None
    return summary


async def main():
    base = local_base(os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend"))
    mongo = get_mongodb(); await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]
        await assert_idle(database)  # Before any fixture container or application write.
        data, versions = await asyncio.to_thread(fixtures)
        user_id = (await get_current_user()).id
        assert type(user_id) is str and user_id
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10), trust_env=False,
            follow_redirects=False, transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            await run_acceptance(database, client, data, versions, user_id, "database-viz-http-" + uuid.uuid4().hex)
    finally: await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
