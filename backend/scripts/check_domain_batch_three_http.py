"""Local-only scientific batch three acceptance with exactly tagged synthetic uploads.
Plugin preferences restored and exact source/tag/name/ID bindings rechecked
before cleanup. Ripple full pair acceptance is isolated in a separate script.
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

PLUGINS = ("viz-mass-spectrum", "viz-fcs-window", "viz-diffraction", "viz-ripple-window")
SOURCE = "domain_batch_three_visualization_http_regression"
ID = re.compile(r"[A-Za-z0-9_:-]{1,256}\Z")
NATIVE_CODE = r'''
import base64,json,os,sys
sys.path.insert(0,"/app/tests")
from mass_spectrum_fixtures import mgf_bytes,mzml_bytes
from fcs_window_fixtures import fcs_bytes
from diffraction_fixtures import xrdml,cansas
assert os.getuid()==65534
fixtures={"sample.mgf":mgf_bytes(),"sample.mzml":mzml_bytes(width=4,compressed=True),
 "sample.fcs":fcs_bytes(),"scan.xrdml":xrdml(),"scatter.xml":cansas(),
 "bad.fcs":fcs_bytes(metadata={"$BYTEORD":"2,1"})}
print(json.dumps({"versions":{"generator":"original synthetic fixture readers"},
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
        assert set(files) == {"sample.mgf","sample.mzml","sample.fcs","scan.xrdml","scatter.xml","bad.fcs"}
        assert all(0 < len(data) < 256 * 1024 for data in files.values())
    finally:
        try:
            if container is None:
                try: container = client.containers.get(name)
                except docker.errors.NotFound: pass
            if container is not None: _remove_worker(container)
        finally:
            client.close()
    files["bad.mgf"]=b"BEGIN IONS\n10 20 30 40\nEND IONS\n"
    files["bad.xrdml"]=b'<!DOCTYPE x [<!ENTITY x SYSTEM "file:///private/x">]><x>&x;</x>'
    files["standalone.rpl"]=b"key\tvalue\n"
    return files, result["versions"]


async def main():
    base = os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend")
    address = urlparse(base)
    if (address.scheme not in {"http", "https"} or address.hostname not in {"frontend", "localhost", "127.0.0.1", "::1"}
            or address.username or address.password or address.query or address.fragment or address.path not in {"", "/"}):
        raise RuntimeError("Only the existing local frontend may be tested")
    data, versions = await asyncio.to_thread(fixtures)
    tag = "domain-viz-three-regression-" + uuid.uuid4().hex
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
                for name, expected in (("sample.mgf",[150.5,10,100.25,25,200.75,5]),("sample.mzml",[100,-1,100.25,3,100.5,7,100.75,2])):
                    tree=await preview(name,PLUGINS[0],kind="tree")
                    assert tree.kind=="tree" and tree.payload["choices"]["spectra"]
                    options={"spectrum":"s-000000"}
                    spectrum=await preview(name,PLUGINS[0],kind="series",version=tree.version,options=options)
                    assert spectrum.kind=="array" and spectrum.payload["array"]["values"]==expected
                    await preview(name,PLUGINS[0],status=422,kind="series",options=options)
                    await preview(name,PLUGINS[0],status=409,kind="tree",version="0"*64)
                    await preview(name,PLUGINS[0],status=422,kind="tree",options={"offset":64})
                    await preview(name,PLUGINS[0],status=422,kind="series",version=tree.version,options={"spectrum":"s-999999"})
                await preview("bad.mgf",PLUGINS[0],status=422,kind="tree")
                tree=await preview("sample.fcs",PLUGINS[1],kind="tree")
                assert tree.kind=="tree" and len(tree.payload["choices"]["channels"])==2
                options={"view":"scatter","channels":[0,1],"event_offset":1,"event_count":2}
                events=await preview("sample.fcs",PLUGINS[1],kind="series",version=tree.version,options=options)
                assert events.kind=="series" and [row["y"] for row in events.payload["series"]]==[[-2.5,0],[20,30]]
                assert all(row["x"]==[1,2] for row in events.payload["series"])
                histogram=await preview("sample.fcs",PLUGINS[1],kind="series",version=tree.version,
                    options={"view":"histogram","channels":[0],"event_offset":0,"event_count":4,"bins":4})
                assert histogram.payload["series"][0]["y"]==[1.25,-2.5,0,4.75]
                await preview("sample.fcs",PLUGINS[1],status=422,kind="series",options=options)
                await preview("sample.fcs",PLUGINS[1],status=409,kind="tree",version="0"*64)
                await preview("sample.fcs",PLUGINS[1],status=422,kind="series",version=tree.version,options={**options,"event_count":8193})
                await preview("bad.fcs",PLUGINS[1],status=422,kind="tree")
                for name, xs, ys in (("scan.xrdml",[10,11,12,13],[10,40,20,5]),("scatter.xml",[.01,.021,.045,.1],[10,-2,5,1])):
                    tree=await preview(name,PLUGINS[2],kind="tree")
                    assert tree.kind=="tree" and len(tree.payload["choices"]["scans"])==2
                    curve=await preview(name,PLUGINS[2],kind="series",version=tree.version,options={"scan":1})
                    assert curve.kind=="series" and curve.payload["series"][0]["x"]==xs and curve.payload["series"][0]["y"]==ys
                    if name.endswith(".xml"):
                        assert curve.payload["series"][0]["y_error"]==[.5,.2,.3,.1]
                    await preview(name,PLUGINS[2],status=422,kind="series",options={"scan":1})
                    await preview(name,PLUGINS[2],status=409,kind="tree",version="0"*64)
                    await preview(name,PLUGINS[2],status=422,kind="series",version=tree.version,options={"scan":True})
                await preview("bad.xrdml",PLUGINS[2],status=422,kind="tree")
                await preview("standalone.rpl",PLUGINS[3],status=422,kind="tree")
                for plugin,name in zip(PLUGINS,("sample.mgf","sample.fcs","scan.xrdml","standalone.rpl")):
                    await state(plugin,False)
                    await preview(name,plugin,status=403)
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
