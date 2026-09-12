"""Local HTTP acceptance for all six main-to-Cordis migrated workbenches.

Run only after deployment, serially with other acceptance tests. Generate exact
synthetic fixtures before writes; upload/delete only this run's tag; temporary
owner CAS and plugin preferences are restored. Never call Agent/model endpoints
or open user datasets. The native producer has no mounts, privileges or network.
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

from app.application.services.main_migration_visualization import validate_payload, OUTPUT_LIMITS
from app.application.services.unified_visualization import VisualizationResult
from app.core.config import get_settings
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.infrastructure.storage.mongodb import get_mongodb
from check_batch_one_visualization_http import _integrity_snapshot

PLUGINS = ("viz-sequence-browser", "viz-genome-tracks", "viz-blast-hits", "viz-matrix-workbench", "viz-astronomy-workbench", "viz-alignment-browser")
SOURCE = "main_migration_visualization_http_regression"
ID = re.compile(r"[A-Za-z0-9_:-]{1,256}\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
EXPECTED_FILES = {"sequence.fa","reads.fq","annotations.bed","signal.wig","hits.m8","known.blast",
    "tensor.npy","curves.npz","cube.fits","image.tiff","reads.sam","reads.bam","reads.cram",
    "bad.fa","bad.bed","bad.m8","object.npy","bad.fits","bad.sam","external.cram"}
FORBIDDEN = ("PRIVATE-FIXTURE-SHOULD-NOT-LEAK", "/Users/", "/home/", "/private/", "/var/folders/",
             "file://", "https://private.invalid/reference.fa", "hidden sample")
NATIVE_CODE = r'''
import base64,io,json,os,sys
sys.path.insert(0,"/app/tests")
import numpy as np,astropy,pysam,tifffile
from sequence_browser_fixtures import FASTA,FASTQ,BED,WIG,BLAST12,BLAST13
from astronomy_workbench_fixtures import cube_bytes,tiff_bytes
from alignment_browser_fixtures import encoded
assert os.getuid()==65534
def npy(value):
    stream=io.BytesIO();np.save(stream,value);return stream.getvalue()
curves=io.BytesIO();np.savez_compressed(curves,S=np.array([4.,3.]),residual_history=np.array([1.,.1,.001]))
files={"sequence.fa":FASTA,"reads.fq":FASTQ,"annotations.bed":BED,"signal.wig":WIG,
 "hits.m8":BLAST12,"known.blast":BLAST13,"tensor.npy":npy(np.arange(24).reshape(2,3,4)*(1+2j)),
 "curves.npz":curves.getvalue(),"cube.fits":cube_bytes(),"image.tiff":tiff_bytes(),
 "bad.fa":b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK","bad.bed":b"chr1 9 1",
 "bad.m8":b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK","object.npy":npy(np.array([{"unsafe":"inert"}],dtype=object)),
 "bad.fits":b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK","bad.sam":b"PRIVATE-FIXTURE-SHOULD-NOT-LEAK"}
for fmt in ("sam","bam","cram"):
    with encoded(fmt) as (data,_,__):files["reads."+fmt]=data
with encoded("cram",cram_mode="external") as (data,_,__):files["external.cram"]=data
print(json.dumps({"versions":{"numpy":np.__version__,"astropy":astropy.__version__,"pysam":pysam.__version__,
 "tifffile":tifffile.__version__,"generator":"original synthetic fixtures"},
 "files":{name:base64.b64encode(data).decode() for name,data in files.items()}}),flush=True)
'''


def local_base(value):
    """Allow only the existing Compose frontend or explicitly local port 7001."""
    try:
        if not isinstance(value, str) or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError("Invalid local address")
        address = urlparse(value)
        valid = (address.scheme == "http" and address.username is None and address.password is None
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
    name = "ai-dataseek-main-migration-fixtures-" + uuid.uuid4().hex
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
        assert set(result["versions"]) == {"numpy", "astropy", "pysam", "tifffile", "generator"}
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
    reader = plugin[4:]
    assert plugin in PLUGINS and result.plugin_id == plugin and result.contract_version == 2
    assert HASH.fullmatch(result.version) and HASH.fullmatch(result.revision)
    assert version is None or result.version == version
    expected = "tree" if kind == "tree" else "array" if reader == "matrix-workbench" and kind == "image" else "raster" if reader == "astronomy-workbench" and kind == "image" else "features" if reader == "genome-tracks" else kind
    assert result.kind == expected and result.payload["view_kind"] == kind
    assert result.metadata["source_bytes"] == size
    private = {**result.payload, "contract_version": 2, "type": reader, "reader": reader, "kind": kind,
               "metadata": result.metadata, "warnings": result.warnings, "sampled": result.sampled}
    private.pop("view_kind")
    validate_payload(private, reader, kind, options=options, format=result.metadata["format"], source_bytes=size)
    assert len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) <= OUTPUT_LIMITS[reader]
    return result


async def main():
    base = local_base(os.environ.get("DATASEEK_VERIFY_BASE_URL", "http://frontend"))
    data, versions = await asyncio.to_thread(fixtures)  # All generation before application writes.
    tag = "main-migration-viz-regression-" + uuid.uuid4().hex
    foreign = "synthetic-foreign-owner-" + uuid.uuid4().hex
    files, uploaded, deleted, originals, touched, cleanup, checks = {}, set(), set(), {}, set(), [], []
    cleanup_targets, owner_records, owner_pending = {}, {}, set()
    names = [tag + "-" + name for name in data]
    mongo = get_mongodb()
    await mongo.initialize()
    failure = None
    try:
        database = mongo.client[get_settings().mongodb_database]
        assert await database.sessions.count_documents({"status":{"$in":["running","waiting"]}}) == 0, "Active session: postpone acceptance"
        assert await database.analysis_jobs.count_documents({"status":{"$in":["queued","running","cancelling"]}}) == 0, "Active job: postpone acceptance"
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
                assert len(response.content) <= OUTPUT_LIMITS[plugin[4:]] + 4096, "Response exceeded acceptance budget"
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
                for name,plugin in (("sequence.fa",PLUGINS[0]),("reads.fq",PLUGINS[0]),("annotations.bed",PLUGINS[1]),
                    ("signal.wig",PLUGINS[1]),("hits.m8",PLUGINS[2]),("known.blast",PLUGINS[2]),
                    ("tensor.npy",PLUGINS[3]),("curves.npz",PLUGINS[3]),("cube.fits",PLUGINS[4]),("image.tiff",PLUGINS[4]),
                    ("reads.sam",PLUGINS[5]),("reads.bam",PLUGINS[5]),("reads.cram",PLUGINS[5])):
                    trees[name] = await preview(name,plugin)

                sequence_options = {"record":0,"start":1,"count":50,"motif":"ACG","quality_encoding":None}
                sequence = await preview("sequence.fa",PLUGINS[0],kind="table",version=trees["sequence.fa"].version,options=sequence_options)
                assert sequence.payload["sequence"]["bases"] == ("ACGT"*13)[:50]
                assert sequence.payload["sequence"]["search"]["total"] == 300
                quality = await preview("reads.fq",PLUGINS[0],kind="table",version=trees["reads.fq"].version,
                    options={**sequence_options,"motif":"","quality_encoding":"phred33"})
                assert quality.payload["sequence"]["qualities"] == [0,20,30,40,41,42]
                await preview("reads.fq",PLUGINS[0],kind="table",version=trees["reads.fq"].version,
                    options={**sequence_options,"motif":"","quality_encoding":"phred64"},status=422,label="incompatible-quality-encoding")
                await preview("sequence.fa",PLUGINS[0],kind="table",version=trees["sequence.fa"].version,
                    options={**sequence_options,"count":1001},status=422,label="sequence-window-budget")

                track_options = {"chromosome":0,"start":0,"end":200}
                tracks = await preview("annotations.bed",PLUGINS[1],kind="map",version=trees["annotations.bed"].version,options=track_options)
                assert [(f["start"],f["end"],f["strand"]) for f in tracks.payload["tracks"]] == [(0,100,"+"),(80,180,"-"),(180,180,".")]
                wig = await preview("signal.wig",PLUGINS[1],kind="map",version=trees["signal.wig"].version,
                    options={"chromosome":0,"start":0,"end":100})
                assert [(f["start"],f["end"],f["value"]) for f in wig.payload["tracks"]] == [(0,2,4),(10,12,-2)]
                await preview("annotations.bed",PLUGINS[1],kind="map",version=trees["annotations.bed"].version,
                    options={**track_options,"end":0},status=422,label="empty-region-rejected")

                blast_options = {"query":None,"min_identity":0,"min_coverage":0,"offset":0,"count":100}
                unknown = await preview("hits.m8",PLUGINS[2],kind="table",version=trees["hits.m8"].version,options=blast_options)
                assert unknown.payload["hits"]["rows"][0]["coverage"] is None and unknown.payload["hits"]["rows"][0]["evalue"] == "1e-350"
                assert unknown.payload["hits"]["rows"][1]["qstart"] == 400 and unknown.payload["hits"]["rows"][1]["qend"] == 301
                known = await preview("known.blast",PLUGINS[2],kind="table",version=trees["known.blast"].version,
                    options={**blast_options,"min_identity":90,"min_coverage":5})
                assert known.payload["hits"]["total"] == 1 and known.payload["hits"]["rows"][0]["coverage"] == 10
                await preview("hits.m8",PLUGINS[2],kind="table",version=trees["hits.m8"].version,
                    options={**blast_options,"min_identity":101},status=422,label="invalid-identity")

                matrix_options = {"variable":"array-0","axes":[1,2],"indices":[1,0,0],"component":"imaginary",
                    "row_range":None,"column_range":None,"max_points":256,"structure":False}
                matrix = await preview("tensor.npy",PLUGINS[3],kind="image",version=trees["tensor.npy"].version,options=matrix_options)
                assert matrix.payload["matrix"]["plane"]["values"] == [[24,26,28,30],[32,34,36,38],[40,42,44,46]]
                curves = await preview("curves.npz",PLUGINS[3],kind="series",version=trees["curves.npz"].version)
                assert any(len(c["y"]) == 2 and abs(c["y"][0]-.64)<1e-12 and c["y"][1] == 1 for c in curves.payload["matrix"]["curves"])
                await preview("tensor.npy",PLUGINS[3],kind="image",version=trees["tensor.npy"].version,
                    options={**matrix_options,"axes":[1,1]},status=422,label="duplicate-axes")

                astronomy_options = {"dataset":0,"slices":[1],"band":1,"action":"render","stretch":"asinh",
                    "interval":"zscale","low":None,"high":None,"colour_map":"heat","invert":False}
                rendered = await preview("cube.fits",PLUGINS[4],kind="image",version=trees["cube.fits"].version,options=astronomy_options)
                work = rendered.payload["workbench"]
                assert work["width"] == 6 and work["height"] == 4 and work["statistics"]["valid_count"] == 23 and work["statistics"]["missing_count"] == 1
                pixel = await preview("cube.fits",PLUGINS[4],kind="image",version=trees["cube.fits"].version,
                    options={"dataset":0,"slices":[1],"band":1,"action":"pixel","x":2,"y":1})
                assert pixel.payload["workbench"]["value"] == 32 and pixel.payload["workbench"]["world"] is not None
                region = await preview("cube.fits",PLUGINS[4],kind="image",version=trees["cube.fits"].version,
                    options={"dataset":0,"slices":[1],"band":1,"action":"region","bounds":[1,1,4,3]})
                assert region.payload["workbench"]["pixel_count"] == 6 and region.payload["workbench"]["statistics"]["sum"] == 210
                await preview("cube.fits",PLUGINS[4],kind="image",version=trees["cube.fits"].version,
                    options={"dataset":0,"slices":[1],"band":1,"action":"sources","threshold_sigma":1})
                table = await preview("cube.fits",PLUGINS[4],kind="table",version=trees["cube.fits"].version,
                    options={"dataset":1,"row_offset":0,"column_offset":0})
                assert table.payload["workbench"]["rows"] == [[10,"source-a"],[None,"source-b"]]
                spectrum = await preview("cube.fits",PLUGINS[4],kind="series",version=trees["cube.fits"].version,options={"dataset":2})
                assert spectrum.payload["workbench"]["values"] == [1,2,None,5]
                colour = await preview("image.tiff",PLUGINS[4],kind="image",version=trees["image.tiff"].version,
                    options={"dataset":1,"slices":[],"band":0,"action":"pixel","x":3,"y":2})
                assert colour.payload["workbench"]["value"] == [13,23,33]
                await preview("cube.fits",PLUGINS[4],kind="image",version=trees["cube.fits"].version,
                    options={"dataset":0,"slices":[1],"band":1,"action":"pixel","x":6,"y":1},status=422,label="pixel-outside-image")

                alignment_options = {"reference":0,"start":100,"end":160,"max_reads":20,"bins":20}
                for name in ("reads.sam","reads.bam","reads.cram"):
                    alignment = await preview(name,PLUGINS[5],kind="table",version=trees[name].version,options=alignment_options)
                    a = alignment.payload["alignment"]
                    assert a["matched_reads"] == 5 and sum(b["covered_bases"] for b in a["coverage"]) == 38
                    if name != "reads.cram":
                        assert a["reads"][0]["mismatches"][0] == {"position":102,"query":"G","reference":"C"}
                await preview("reads.sam",PLUGINS[5],kind="table",version=trees["reads.sam"].version,
                    options={**alignment_options,"bins":19},status=422,label="invalid-coverage-bins")
                external = await preview("external.cram",PLUGINS[5],label="external-cram-header-only")
                await preview("external.cram",PLUGINS[5],kind="table",version=external.version,
                    options=alignment_options,status=422,label="external-reference-cram-denied")

                cases = [(PLUGINS[0],"sequence.fa","table",sequence_options,"bad.fa"),
                    (PLUGINS[1],"annotations.bed","map",track_options,"bad.bed"),
                    (PLUGINS[2],"hits.m8","table",blast_options,"bad.m8"),
                    (PLUGINS[3],"tensor.npy","image",matrix_options,"object.npy"),
                    (PLUGINS[4],"cube.fits","image",astronomy_options,"bad.fits"),
                    (PLUGINS[5],"reads.sam","table",alignment_options,"bad.sam")]
                for plugin, name, kind, selection, bad in cases:
                    await preview(name, plugin, kind=kind, options=selection, status=422, label="missing-version")
                    stale = ("0" if trees[name].version[0] != "0" else "1") * 64
                    await preview(name, plugin, version=stale, status=409, label="stale-version")
                    await preview(name, plugin, options={"unexpected": True}, status=422, label="undeclared-option")
                    for operation in ("bytes", "page", "prepare"):
                        await preview(name, plugin, operation=operation, status=422, label="undeclared-operation:" + operation)
                    other = "annotations.bed" if plugin == PLUGINS[0] else "sequence.fa"
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
        if not passed: raise RuntimeError("Main migration acceptance failed; inspect checks and cleanup status") from None
    finally:
        await mongo.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
