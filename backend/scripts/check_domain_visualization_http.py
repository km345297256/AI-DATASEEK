"""Local-only acceptance for columnar, scientific graph and NXdata plugins.

Run after deployment. Creates only uniquely tagged synthetic files, restores
plugin preferences, deletes exact fixture IDs and compares business metadata.
No analysis session, model call, user source read or public network is involved.
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

from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from check_batch_one_visualization_http import _integrity_snapshot

PLUGINS = ("viz-columnar-window", "viz-nexus-window", "viz-scientific-graph")
SOURCE = "domain_visualization_http_regression"
ID = re.compile(r"[A-Za-z0-9_:-]{1,256}\Z")
NATIVE_CODE = r'''
import base64,io,json,os
from decimal import Decimal
from importlib.metadata import version
import numpy as np,h5py,pyarrow as pa,pyarrow.parquet as pq
assert os.getuid()==65534
table=pa.table({"identifier":pa.array([9007199254740993+i for i in range(6)],type=pa.int64()),
  "value":pa.array([1.5,2.5,None,4.5,5.5,6.5],type=pa.float64()),
  "amount":pa.array([Decimal("1.25")]*6,type=pa.decimal128(10,2))})
out=pa.BufferOutputStream();pq.write_table(table,out,row_group_size=3,compression="NONE",use_dictionary=False)
fixtures={"table.parquet":out.getvalue().to_pybytes()}
out=pa.BufferOutputStream()
with pa.ipc.new_file(out,table.schema) as writer: writer.write_table(table,max_chunksize=3)
fixtures["table.arrow"]=out.getvalue().to_pybytes()
out=pa.BufferOutputStream()
with pa.ipc.new_stream(out,table.schema) as writer: writer.write_table(table)
fixtures["stream.arrow"]=out.getvalue().to_pybytes()
out=io.BytesIO()
with h5py.File(out,"w") as f:
  entry=f.create_group("entry");entry.attrs["NX_class"]=np.bytes_("NXentry")
  g=entry.create_group("scan");g.attrs["NX_class"]=np.bytes_("NXdata");g.attrs["signal"]=np.bytes_("counts")
  g.attrs["axes"]=np.array([b"energy"],dtype="S6")
  g.create_dataset("counts",data=[1.,4.,9.,16.]);g["counts"].attrs["units"]=np.bytes_("counts")
  g.create_dataset("energy",data=[10.,20.,30.,40.]);g["energy"].attrs["units"]=np.bytes_("keV")
  g.create_dataset("counts_errors",data=[.1,.2,.3,.4]);g["counts_errors"].attrs["units"]=np.bytes_("counts")
fixtures["scan.nxs"]=out.getvalue()
print(json.dumps({"versions":{"pyarrow":version("pyarrow"),"h5py":version("h5py")},
  "files":{name:base64.b64encode(data).decode() for name,data in fixtures.items()}}),flush=True)
'''


def fixtures():
    client, container = docker.from_env(timeout=5), None
    name = "ai-dataseek-domain-fixtures-" + uuid.uuid4().hex
    try:
        container = client.containers.create(image=get_settings().sandbox_image, name=name,
            entrypoint=["/usr/bin/timeout"], command=["--signal=KILL", "55s", "/app/.venv/bin/python", "-c", NATIVE_CODE],
            working_dir="/app", user="65534:65534", network_mode="none", read_only=True,
            cap_drop=["ALL"], security_opt=["no-new-privileges:true"], mem_limit="1g", memswap_limit="1g",
            nano_cpus=1_000_000_000, pids_limit=96, tmpfs={"/tmp": "rw,noexec,nosuid,size=32m,mode=1777"},
            environment={"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            labels={"ai-dataseek.component": "visualization-http-fixture"})
        container.reload()
        assert container.attrs["HostConfig"]["NetworkMode"] == "none" and not container.attrs.get("Mounts")
        container.start()
        assert container.wait(timeout=60).get("StatusCode") == 0, "Native synthetic fixture generation failed"
        raw = container.logs(stdout=True, stderr=False)
        assert len(raw) < 1024**2
        result = json.loads(raw)
        files = {key: base64.b64decode(value, validate=True) for key, value in result["files"].items()}
        assert set(files) == {"table.parquet", "table.arrow", "stream.arrow", "scan.nxs"}
        assert all(0 < len(data) < 256 * 1024 for data in files.values())
    finally:
        try:
            if container is None:
                try: container = client.containers.get(name)
                except docker.errors.NotFound: pass
            if container is not None: _remove_worker(container)
        finally:
            client.close()
    files["network.json"] = json.dumps({"directed": True, "nodes": [{"id": "A", "label": "Protein A", "group": "signal"},
        {"id": "B", "label": "Protein B", "group": "signal"}], "edges": [{"source": "A", "target": "B", "weight": 2.5}]}).encode()
    files["network.graphml"] = b'<graphml xmlns="http://graphml.graphdrawing.org/xmlns"><graph edgedefault="undirected"><node id="A"/><node id="B"/><edge source="A" target="B"/></graph></graphml>'
    files["network.gexf"] = b'<?xml version="1.0" encoding="UTF-8"?><gexf xmlns="http://gexf.net/1.3" version="1.3"><graph mode="static" defaultedgetype="directed"><nodes><node id="A" label="Protein A"/><node id="B" label="Protein B"/></nodes><edges><edge id="ab" source="A" target="B" label="interaction" weight="0.75"/></edges></graph></gexf>'
    files["unsafe.graphml"] = b'<!DOCTYPE graphml [<!ENTITY x SYSTEM "file:///etc/passwd">]><graphml>&x;</graphml>'
    return files, result["versions"]


async def main():
    base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
    address = urlparse(base)
    if (address.scheme not in {"http", "https"} or address.hostname not in {"frontend", "localhost", "127.0.0.1", "::1"}
            or address.username or address.password or address.query or address.fragment or address.path not in {"", "/"}):
        raise RuntimeError("Only the existing local frontend may be tested")
    data, versions = await asyncio.to_thread(fixtures)
    tag = "domain-viz-regression-" + uuid.uuid4().hex
    files, uploaded, deleted, originals, touched, cleanup, checks = {}, set(), set(), {}, set(), [], []
    cleanup_targets = {}
    names = [tag + "-" + name for name in data]
    mongo = get_mongodb()
    await mongo.initialize()
    failure = None
    try:
        database = mongo.client[get_settings().mongodb_database]
        before = await _integrity_snapshot(database)
        async with httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(120, connect=10), trust_env=False, follow_redirects=False,
                                     transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            async def request(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                result = response.json()
                assert result["code"] == 0
                return result["data"]

            async def state(plugin, value):
                touched.add(plugin)
                await request("PATCH", f"/api/v1/visualizations/{plugin}/state", json={"enabled": value})

            async def preview(name, plugin, *, status=200, **options):
                response = await client.post(f"/api/v1/files/{quote(files[name], safe='')}/visualization",
                    json={"plugin_id": plugin, "operation": "preview", **options})
                assert response.status_code == status, f"{plugin}:{name}:{options.get('kind')} expected {status}, got {response.status_code}"
                checks.append({"plugin": plugin, "file": name, "kind": options.get("kind"), "status": status})
                print(json.dumps({"check": checks[-1]}), flush=True)
                if status == 200:
                    result = response.json()
                    assert result["code"] == 0
                    return VisualizationResult.model_validate(result["data"])
            try:
                catalog = await request("GET", "/api/v1/visualizations")
                assert catalog["engine"] == "cordis"
                by_id = {item["id"]: item for item in catalog["plugins"]}
                for plugin in PLUGINS:
                    originals[plugin] = by_id[plugin]["enabled"]
                    if not originals[plugin]: await state(plugin, True)
                for name, value in data.items():
                    info = await request("POST", "/api/v1/files", files={"file": (tag + "-" + name, value, "application/octet-stream")},
                        data={"metadata": json.dumps({"source": SOURCE, "regression_run": tag})})
                    identifier = info.get("file_id")
                    assert isinstance(identifier, str) and ID.fullmatch(identifier)
                    assert info["filename"] == tag + "-" + name
                    binding = {"file_id": identifier, "filename": tag + "-" + name,
                               "metadata.source": SOURCE, "metadata.regression_run": tag}
                    assert await database.stored_files.find_one(binding, {"_id": 1}), "Uploaded fixture identity does not match the exact run"
                    assert identifier not in uploaded, "Upload returned a duplicate fixture ID"
                    uploaded.add(identifier); files[name] = identifier
                    cleanup_targets[identifier] = tag + "-" + name
                for name in ("table.parquet", "table.arrow"):
                    tree = await preview(name, PLUGINS[0], kind="tree")
                    assert tree.kind == "tree" and len(tree.payload["choices"]["columns"]) == 3
                    options = {"columns": [0, 1, 2], "row_offset": 0, "row_limit": 2}
                    table = await preview(name, PLUGINS[0], kind="table", version=tree.version, options=options)
                    assert table.kind == "table" and table.payload["table"]["rows"][0] == ["9007199254740993", 1.5, "1.25"]
                    later = await preview(name, PLUGINS[0], kind="table", version=tree.version, options={**options, "row_offset": 4})
                    assert later.payload["table"]["rows"][0][0] == "9007199254740997"
                    await preview(name, PLUGINS[0], status=422, kind="table", options=options)
                    await preview(name, PLUGINS[0], status=409, kind="tree", version="0" * 64)
                    await preview(name, PLUGINS[0], status=422, kind="table", version=tree.version, options={**options, "row_limit": 201})
                    await preview(name, PLUGINS[0], status=422, kind="table", version=tree.version, options={**options, "sql": "SELECT 1"})
                await preview("stream.arrow", PLUGINS[0], status=422, kind="tree")
                tree = await preview("scan.nxs", PLUGINS[1], kind="tree")
                signal = tree.payload["choices"]["signals"][0]
                options = {"nxdata": signal["id"], "selection": [{"start": 1, "stop": 4, "step": 1}]}
                scan = await preview("scan.nxs", PLUGINS[1], kind="series", version=tree.version, options=options)
                assert scan.kind == "array" and scan.payload["array"]["values"] == [4, 9, 16]
                assert scan.payload["axes"][0]["values"] == [20, 30, 40] and scan.payload["axes"][0]["unit"] == "keV"
                assert scan.payload["errors"] == [.2, .3, .4]
                await preview("scan.nxs", PLUGINS[1], status=422, kind="series", options=options)
                await preview("scan.nxs", PLUGINS[1], status=409, kind="tree", version="0" * 64)
                await preview("scan.nxs", PLUGINS[1], status=422, kind="series", version=tree.version, options={**options, "nxdata": "n-" + "0" * 32})
                for name in ("network.json", "network.graphml", "network.gexf"):
                    graph = await preview(name, PLUGINS[2], kind="graph")
                    assert graph.kind == "graph" and len(graph.payload["graph"]["nodes"]) == 2 and len(graph.payload["graph"]["edges"]) == 1
                    topology = graph.payload["graph"]
                    assert [node["key"] for node in topology["nodes"]] == ["A", "B"]
                    assert topology["directed"] is (name != "network.graphml")
                    assert topology["edges"][0]["source"] == "n0" and topology["edges"][0]["target"] == "n1"
                    assert topology["edges"][0]["weight"] == {"network.json": 2.5, "network.graphml": None, "network.gexf": 0.75}[name]
                    assert graph.metadata["format"] == name.rsplit(".", 1)[1] and graph.metadata["source_bytes"] == len(data[name])
                await preview("network.json", PLUGINS[2], status=409, kind="graph", version="0" * 64)
                await preview("unsafe.graphml", PLUGINS[2], status=422, kind="graph")
                await preview("network.json", PLUGINS[2], status=422, kind="graph", options={"url": "https://example.invalid"})
                for plugin, name in zip(PLUGINS, ("table.parquet", "scan.nxs", "network.json")):
                    await state(plugin, False)
                    await preview(name, plugin, status=403)
            except BaseException as error:
                failure = error
            finally:
                for plugin in touched:
                    try: await request("PATCH", f"/api/v1/visualizations/{plugin}/state", json={"enabled": originals[plugin]})
                    except Exception: cleanup.append("preference_restore:" + plugin)
                # Recover accepted uploads with lost responses using only the
                # exact run tag plus declared synthetic names, never broad deletes.
                try:
                    records = await database.stored_files.find({"filename": {"$in": names}, "metadata.source": SOURCE,
                        "metadata.regression_run": tag}, {"_id": 0, "file_id": 1, "filename": 1}).limit(len(names) + 1).to_list()
                    assert len(records) <= len(names)
                    for record in records:
                        identifier = record.get("file_id")
                        filename = record.get("filename")
                        assert isinstance(identifier, str) and ID.fullmatch(identifier) and filename in names
                        uploaded.add(identifier)
                        cleanup_targets[identifier] = filename
                except Exception: cleanup.append("fixture_inventory")
                for identifier, filename in cleanup_targets.items():
                    try:
                        path = "/api/v1/files/" + quote(identifier, safe="")
                        # Never trust an upload response ID by itself, and verify
                        # the exact metadata binding again immediately before deletion.
                        binding = {"file_id": identifier, "filename": filename,
                                   "metadata.source": SOURCE, "metadata.regression_run": tag}
                        if not await database.stored_files.find_one(binding, {"_id": 1}):
                            assert (await client.get(path + "/info")).status_code == 404, "Fixture binding changed; refusing deletion"
                            deleted.add(identifier)
                            continue
                        assert (await client.delete(path)).status_code in {200, 404}
                        assert (await client.get(path + "/info")).status_code == 404
                        deleted.add(identifier)
                    except Exception: cleanup.append("fixture_delete:" + identifier)
                try:
                    restored = await request("GET", "/api/v1/visualizations")
                    states = {p["id"]: p["enabled"] for p in restored["plugins"]}
                    if any(states[p] != v for p, v in originals.items()): cleanup.append("preference_readback")
                except Exception: cleanup.append("preference_readback")
                try:
                    remaining = await database.stored_files.count_documents({"filename": {"$in": names},
                        "metadata.source": SOURCE, "metadata.regression_run": tag})
                    if remaining: cleanup.append("fixtures_remaining")
                except Exception: cleanup.append("fixture_readback")
        after = await _integrity_snapshot(database)
        passed = failure is None and not cleanup and before == after and uploaded == deleted
        print(json.dumps({"passed": passed, "run": tag, "versions": versions, "checks": checks,
            "positive": sum(c["status"] == 200 for c in checks), "negative": sum(c["status"] != 200 for c in checks),
            "fixtures_uploaded": len(uploaded), "fixtures_deleted": len(deleted), "cleanup_errors": cleanup,
            "business_state_preserved": before == after, "sessions_or_model_calls_created": 0,
            "failure_type": type(failure).__name__ if failure else None}, ensure_ascii=False, indent=2), flush=True)
        if not passed: raise RuntimeError("Domain visualization acceptance failed") from failure
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
