"""Serial local acceptance for SQL/PG/BSON/RDB file plugins, never restoration.

No work on import. Run only after coordinated deployment and with no active
sessions/jobs. Inputs are fixed hash-bound original test fixtures; no source SQL
or backup restore command is executed. Uncertain uploads are recovered by their
exact FixtureLedger binding, never retried. Only our files and four preference
states may be changed, and their cleanup/restoration is verified in finally.
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

from app.application.services.database_records_visualization import validate_database_records_payload
from app.application.services.pg_dump_visualization import validate_pg_dump_payload
from app.application.services.sql_dump_visualization import validate_sql_dump_payload
from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from app.interfaces.dependencies import get_current_user
from check_database_visualization_http import assert_idle, business_integrity_snapshot, cleanup_fixture
from visualization_acceptance_safety import FixtureLedger, local_base

SOURCE = "database_records_http_regression"
PLUGINS = {"viz-sql-dump": ("sql-dump", "database-dump"), "viz-postgres-dump": ("pg-dump", "database-dump"),
           "viz-bson": ("bson", "database-records"), "viz-redis-rdb": ("redis-rdb", "database-records")}
HASH = re.compile(r"[0-9a-f]{64}\Z")
FIXTURE_BYTES = 1024 * 1024
EXPECTED_FILES = {"sample.sql", "bad.sql", "sample.dump", "sample.tar", "identifiers.dump", "bad.dump",
                  "sample.bson", "empty.bson", "bad.bson", "sample.rdb", "empty.rdb", "bad.rdb"}
VERSIONS = {"sql_fixture": "SQLite 3.51.0", "pg_fixture": "PostgreSQL 18.6",
            "bson_fixture": "PyMongo 4.17.0", "rdb_fixture": "Redis 7.2.7 native SAVE"}
FIXTURE_PATHS = {
    "sample.sql": ("database-dumps/synthetic-sqlite.sql", "4f4bc5f40bb539de48932bf1d1c5c1940f8222e44431d0cfe5e73e30ead28700"),
    "sample.dump": ("database-dumps/synthetic-postgres-gzip.dump", "453fec696badec68d8f63565989de63397c359ce03ac7a85d2811ecc34d62d8f"),
    "sample.tar": ("database-dumps/synthetic-postgres.tar", "6d519a3b1d183310211536386cbfcd83d4e37f3af4f74ec10d5095c023579b82"),
    "identifiers.dump": ("database-dumps/synthetic-postgres-identifiers.dump", "aea0db5e697a80228841ab3b79fdd6b6b819a6ebbe55a098a0e6fbd8365029f4"),
    "sample.bson": ("database-records/synthetic-pymongo-4.17.0.bson", "239cddebb3967003caf75cf5b20cd1de3c47b0a16df1f66481cee29a244db986"),
    "empty.bson": ("database-records/empty-document.bson", "49e8e3297545c15ab6a79471a7a34d43e24a8f1cb25ea3d8417c61f699267a3f"),
    "sample.rdb": ("database-records/native-redis-7.2.7.rdb", "d7853cacc4bbee20f5d632226ac309ce3b913da47a669f2bfa373426431fed69"),
    "empty.rdb": ("database-records/empty-rdb11-wire.rdb", "133b5316e3800ab13615a9d4ddc40a6a0c1c133523e5506b284cee353a2c3c9b"),
}
FORBIDDEN = ("PRIVATE-FIXTURE-SHOULD-NOT-LEAK", "/Users/", "/home/", "/private/", "/var/folders/", "file://")
NATIVE_CODE = r'''
import base64,hashlib,json,os
from pathlib import Path
assert os.getuid()==65534
spec=json.loads(os.environ["FIXTURE_SPEC"])
assert len(spec)==8
root=Path("/app/tests/fixtures")
files={}
for name,(relative,digest) in spec.items():
    assert relative.startswith(("database-dumps/","database-records/")) and ".." not in relative
    path=root/relative
    assert path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=131072
    data=path.read_bytes();assert hashlib.sha256(data).hexdigest()==digest
    files[name]=data
files["bad.sql"]=b"CREATE TABLE t(a INT); INSERT INTO t VALUES(load_extension('never'));"
files["bad.dump"]=b"PGDMP"+bytes(64)
files["bad.bson"]=b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK"
files["bad.rdb"]=files["sample.rdb"][:-1]+bytes([files["sample.rdb"][-1]^1])
output=json.dumps({"versions":json.loads(os.environ["FIXTURE_VERSIONS"]),"files":{n:base64.b64encode(v).decode() for n,v in files.items()}})
assert len(output.encode())<=1048576
print(output,flush=True)
'''


def decode_fixtures(chunks):
    raw = bytearray()
    for chunk in chunks:
        assert type(chunk) is bytes and len(raw) + len(chunk) <= FIXTURE_BYTES
        raw.extend(chunk)
    value = json.loads(raw)
    assert type(value) is dict and set(value) == {"versions", "files"} and value["versions"] == VERSIONS
    assert type(value["files"]) is dict and set(value["files"]) == EXPECTED_FILES
    files = {}
    for name, encoded in value["files"].items():
        assert type(encoded) is str and len(encoded) <= FIXTURE_BYTES
        data = base64.b64decode(encoded, validate=True); assert 0 < len(data) <= 131072
        if name in FIXTURE_PATHS: assert hashlib.sha256(data).hexdigest() == FIXTURE_PATHS[name][1]
        files[name] = data
    assert sum(map(len, files.values())) <= FIXTURE_BYTES
    assert files["bad.sql"] == b"CREATE TABLE t(a INT); INSERT INTO t VALUES(load_extension('never'));"
    assert files["bad.dump"] == b"PGDMP" + bytes(64)
    assert files["bad.bson"] == b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK"
    assert files["bad.rdb"] == files["sample.rdb"][:-1] + bytes([files["sample.rdb"][-1] ^ 1])
    return files, value["versions"]


def fixtures():
    client, container = docker.from_env(timeout=30), None
    name = "ai-dataseek-records-fixtures-" + uuid.uuid4().hex
    try:
        container = client.containers.create(image=get_settings().sandbox_image, name=name,
            entrypoint=["/usr/bin/timeout"], command=["--signal=KILL", "20s", "/app/.venv/bin/python", "-c", NATIVE_CODE],
            working_dir="/app", user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="128m", memswap_limit="128m",
            nano_cpus=500_000_000, pids_limit=32,
            environment={"PYTHONDONTWRITEBYTECODE": "1", "FIXTURE_SPEC": json.dumps(FIXTURE_PATHS), "FIXTURE_VERSIONS": json.dumps(VERSIONS)},
            log_config={"type": "json-file", "config": {"max-size": "1m", "max-file": "1"}},
            labels={"ai-dataseek.component": "visualization-http-fixture", "ai-dataseek.fixture-run": name})
        container.reload(); config = container.attrs["HostConfig"]
        assert config["NetworkMode"] == "none" and config["ReadonlyRootfs"] is True
        assert container.attrs["Config"]["User"] == "65534:65534" and not container.attrs.get("Mounts")
        container.start(); assert container.wait(timeout=30).get("StatusCode") == 0, "Hash-bound fixture transport failed"
        return decode_fixtures(container.logs(stdout=True, stderr=False, stream=True, follow=False))
    finally:
        try:
            if container is None:
                deadline = time.monotonic() + 10
                while container is None and time.monotonic() < deadline:
                    try: container = client.containers.get(name)
                    except docker.errors.NotFound: time.sleep(.25)
            if container is not None:
                container.reload(); labels = container.attrs.get("Config", {}).get("Labels", {})
                assert labels.get("ai-dataseek.component") == "visualization-http-fixture" and labels.get("ai-dataseek.fixture-run") == name
                _remove_worker(container)
        finally: client.close()


async def upload_fixture(client, ledger, name, data):
    filename = ledger.declare(name, len(data))
    response = await client.post("/api/v1/files", files={"file": (filename, data, "application/octet-stream")},
                                data={"metadata": json.dumps({"source": SOURCE, "regression_run": ledger.run})})
    response.raise_for_status(); envelope = response.json(); assert envelope["code"] == 0
    return await ledger.accept(envelope["data"])


def validate_result(raw, plugin, kind, options, source, fmt, version=None):
    result = VisualizationResult.model_validate(raw)
    assert result.plugin_id == plugin and result.contract_version == 2 and result.kind == kind
    assert HASH.fullmatch(result.version) and HASH.fullmatch(result.revision) and (version is None or result.version == version)
    assert result.payload["view_kind"] == kind
    reader = PLUGINS[plugin][0]
    private = {**result.payload, "contract_version": 2, "type": reader, "reader": reader, "kind": kind,
               "metadata": result.metadata, "warnings": result.warnings, "sampled": result.sampled}
    private.pop("view_kind")
    validator = validate_sql_dump_payload if reader == "sql-dump" else validate_pg_dump_payload if reader == "pg-dump" else validate_database_records_payload
    validator(private, kind=kind, options=options, fmt=fmt, size=len(source))
    assert len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) <= 2 * 1024**2
    return result


async def run_acceptance(database, client, data, versions, user_id, run):
    assert set(data) == EXPECTED_FILES
    await assert_idle(database)
    before = await business_integrity_snapshot(database)
    ledger = FixtureLedger(database, SOURCE, run, user_id=user_id)
    files, originals, expected_states, touched, checks, errors, deleted = {}, {}, {}, set(), [], [], set()
    failure = None

    async def request(method, path, **kwargs):
        response = await client.request(method, path, **kwargs); response.raise_for_status()
        envelope = response.json(); assert envelope["code"] == 0; return envelope["data"]

    async def state(plugin, enabled, *, restoring=False):
        assert plugin in originals and type(enabled) is bool
        if not restoring: await assert_idle(database)
        touched.add(plugin); expected_states[plugin] = enabled
        await request("PATCH", f"/api/v1/visualizations/{plugin}/state", json={"enabled": enabled})
        catalog = await request("GET", "/api/v1/visualizations")
        states = {p["id"]: p["enabled"] for p in catalog["plugins"]}
        assert all(states[p] is value for p, value in expected_states.items())

    async def preview(name, plugin, *, kind="tree", options=None, version=None, status=200, label=None, operation="preview"):
        await assert_idle(database)
        options = options or {}; body = {"plugin_id": plugin, "operation": operation, "kind": kind, "options": options}
        if version is not None: body["version"] = version
        response = await client.post("/api/v1/files/" + quote(files[name], safe="") + "/visualization", json=body)
        check = {"plugin": plugin, "file": name, "check": label or kind, "status": response.status_code, "expected_status": status}
        checks.append(check); print(json.dumps({"check": check}), flush=True)
        assert response.status_code == status, f"{plugin}:{label or kind}: HTTP {response.status_code}, expected {status}"
        assert len(response.content) <= 2 * 1024**2 + 4096
        envelope = response.json(); encoded = json.dumps(envelope, ensure_ascii=False).lower()
        assert not any(marker.lower() in encoded for marker in FORBIDDEN)
        if status == 200:
            assert envelope["code"] == 0
            return validate_result(envelope["data"], plugin, kind, options, data[name], name.rsplit(".", 1)[1], version)

    try:
        catalog = await request("GET", "/api/v1/visualizations")
        assert catalog["engine"] == "cordis" and len(catalog["plugins"]) == 83
        by_id = {p["id"]: p for p in catalog["plugins"]}; assert len(by_id) == 83
        for plugin, (reader, adapter) in PLUGINS.items():
            p = by_id[plugin]; assert p["reader"] == reader and p["adapter"] == adapter and p["contract_version"] == 2
            assert type(p["enabled"]) is bool and p["capabilities"]["operations"] == ["preview"] and p["capabilities"]["shared"] is False
            originals[plugin] = p["enabled"]; expected_states[plugin] = p["enabled"]
        for plugin, enabled in originals.items():
            if not enabled: await state(plugin, True)
        for name, source in data.items():
            await assert_idle(database); files[name] = await upload_fixture(client, ledger, name, source)

        trees, selections = {}, {}
        for name, plugin in [("sample.sql", "viz-sql-dump"), ("sample.bson", "viz-bson"), ("sample.rdb", "viz-redis-rdb")]:
            tree_options = {"dialect": "sqlite"} if plugin == "viz-sql-dump" else {}
            tree = await preview(name, plugin, options=tree_options); trees[plugin] = tree
            if plugin == "viz-sql-dump":
                target = next(t for t in tree.payload["choices"]["tables"] if t["label"] == "measurements")
                selection = {"dialect": "sqlite", "table": target["id"], "columns": list(range(6)), "row_offset": 0, "row_limit": 2}
            else:
                target = tree.payload["choices"]["groups"][0]; selection = {"group_id": target["id"], "offset": 0, "limit": 2}
            selections[plugin] = selection
            first = await preview(name, plugin, kind="table", options=selection, version=tree.version)
            second_selection = {**selection, ("row_offset" if plugin == "viz-sql-dump" else "offset"): 2}
            second = await preview(name, plugin, kind="table", options=second_selection, version=tree.version)
            if plugin == "viz-sql-dump":
                rows = first.payload["table"]["rows"]
                assert rows[0][0] == {"type": "number-literal", "value": "9223372036854775807"}
                assert rows[1][0]["value"] == "9007199254740993" and rows[0][1]["value"] == "1.230000000000000001"
                assert second.payload["table"]["row_ids"] == ["2"] and second.payload["table"]["has_more"] is False
            elif plugin == "viz-bson":
                nodes = first.payload["table"]["records"][0]["nodes"]; fields = {n["key"]: n["cell"] for n in nodes}
                assert fields["integer"]["value"] == "9223372036854775807" and fields["decimal"]["value"] == "123456789012345678901234567890.1234"
                assert fields["date"]["value"] == "-9223372036854775808" and fields["stamp"]["seconds"] == "4294967295"
                assert fields["scope"] == {"type": "unsupported", "name": "javascript-scope"}
                assert fields["empty"] == {"type": "text", "value": ""} and fields["null"] == {"type": "null", "value": None}
                assert second.payload["table"]["records"][0]["index"] == "2" and second.payload["table"]["has_more"] is False
            else:
                all_records = await preview(name, plugin, kind="table", options={**selection, "limit": 50}, version=tree.version, label="all-five-types")
                keyed = {r["key"]["value"]: r for r in all_records.payload["table"]["records"]}
                assert keyed["precise"]["expires_at_ms"] == "9223372036854775807" and keyed["precise"]["nodes"][0]["cell"]["value"] == "9007199254740993"
                assert keyed["compressed"]["nodes"][0]["cell"]["reason"] == "text-budget"
                assert set(tree.payload["choices"]["groups"][0]["counts"]) == {"string", "hash", "list", "set", "zset"}
            await preview(name, plugin, kind="table", options=selection, status=422, label="missing-version")
            await preview(name, plugin, kind="table", options={**selection, ("row_offset" if plugin == "viz-sql-dump" else "offset"): 100001}, version=tree.version, status=422, label="offset-budget")

        for name in ("sample.dump", "sample.tar", "identifiers.dump"):
            tree = await preview(name, "viz-postgres-dump")
            assert tree.metadata["data_verified"] is False and tree.metadata["archive_version"] == "1.16.0"
            assert tree.metadata["objects_total"] == (15 if name == "identifiers.dump" else 8)
            if name == "sample.dump": trees["viz-postgres-dump"] = tree
        await preview("sample.dump", "viz-postgres-dump", kind="table", status=422, label="restore-or-rows-not-supported")
        for name, plugin in (("empty.bson", "viz-bson"), ("empty.rdb", "viz-redis-rdb")):
            tree = await preview(name, plugin)
            result = await preview(name, plugin, kind="table", version=tree.version, options={**selections[plugin], "offset": 1}, label="empty-page")
            assert result.payload["table"]["records"] == []
        for name, plugin, bad, other in [("sample.sql", "viz-sql-dump", "bad.sql", "sample.bson"),
                ("sample.dump", "viz-postgres-dump", "bad.dump", "sample.bson"), ("sample.bson", "viz-bson", "bad.bson", "sample.rdb"),
                ("sample.rdb", "viz-redis-rdb", "bad.rdb", "sample.bson")]:
            tree = trees[plugin]; opts = {"dialect": "sqlite"} if plugin == "viz-sql-dump" else {}
            stale = ("0" if tree.version[0] != "0" else "1") * 64
            await preview(name, plugin, options=opts, version=stale, status=409, label="stale-version")
            for key in ("sql", "path", "restore", "connection"):
                await preview(name, plugin, options={**opts, key: "PRIVATE-FIXTURE-SHOULD-NOT-LEAK"}, status=422, label="unapproved-option:" + key)
            await preview(name, plugin, options=opts, operation="prepare", status=422, label="undeclared-operation")
            await preview(other, plugin, options=opts, status=422, label="format-mismatch")
            await preview(bad, plugin, options=opts, status=422, label="unsafe-or-corrupt-source")
            assert await ledger.assert_owned(files[name])
            downloaded = await client.get("/api/v1/files/" + quote(files[name], safe="") + "/download"); downloaded.raise_for_status()
            assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(data[name]).digest()
            checks.append({"plugin": plugin, "file": name, "check": "source-sha256-unchanged", "status": 200, "expected_status": 200})
            await state(plugin, False)
            await preview(name, plugin, options=opts, status=403, label="disabled-independent-plugin")
    except BaseException as caught: failure = caught
    finally:
        for plugin in touched:
            try: await state(plugin, originals[plugin], restoring=True)
            except Exception: errors.append("preference_restore:" + plugin)
        try: await ledger.recover()
        except Exception: errors.append("fixture_inventory")
        for identifier in tuple(ledger.records):
            try: await cleanup_fixture(client, ledger, identifier); deleted.add(identifier)
            except Exception: errors.append("fixture_delete:" + identifier)
        try: await ledger.assert_clean()
        except Exception: errors.append("fixture_readback")
        try:
            current = await request("GET", "/api/v1/visualizations"); states = {p["id"]: p["enabled"] for p in current["plugins"]}
            assert all(states[p] is value for p, value in originals.items())
        except Exception: errors.append("preference_readback")
    after = await business_integrity_snapshot(database)
    summary = {"passed": failure is None and not errors and before == after and set(ledger.records) == deleted,
               "run": run, "versions": versions, "checks": checks, "fixtures_uploaded": len(ledger.records), "fixtures_deleted": len(deleted),
               "cleanup_errors": errors, "business_state_preserved": before == after,
               "plugin_preferences_restored": not any(e.startswith("preference_") for e in errors),
               "existing_user_files_accessed": 0, "script_model_endpoint_calls": 0, "database_restore_calls": 0,
               "owner_permission_check": "unit coverage; no owner mutation", "failure_type": type(failure).__name__ if failure else None}
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if not summary["passed"]: raise RuntimeError("Database records acceptance failed; inspect checks and cleanup status") from None
    return summary


async def main():
    base = local_base(os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend"))
    mongo = get_mongodb(); await mongo.initialize()
    try:
        database = mongo.client[get_settings().mongodb_database]; await assert_idle(database)
        data, versions = await asyncio.to_thread(fixtures)
        user_id = (await get_current_user()).id; assert type(user_id) is str and user_id
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10), trust_env=False,
            follow_redirects=False, transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            await run_acceptance(database, client, data, versions, user_id, "records-viz-http-" + uuid.uuid4().hex)
    finally: await mongo.shutdown()


if __name__ == "__main__": asyncio.run(main())
