"""Serial real HTTP acceptance: original fixtures, no database restoration.

Only this run's precisely bound uploads and two restored plugin preferences may
change. No work on import. Stop on active jobs; never retry an uncertain upload.
"""
import asyncio
import base64
import hashlib
import json
import uuid
from urllib.parse import quote

import docker
import httpx

from app.core.config import get_settings
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.external.sandbox.extended_visualization_worker import _remove_worker
from app.interfaces.dependencies import get_current_user
from app.application.services.physical_database_visualization import validate_physical_payload
from app.application.services.unified_visualization import VisualizationResult
from check_database_visualization_http import assert_idle, business_integrity_snapshot, cleanup_fixture
from visualization_acceptance_safety import FixtureLedger

SOURCE = "physical_database_http_regression"
SPEC = {
    "mysql-8.0.46.ibd": "ef03c988c5e6ddf489c80be3d903b783ad15cb6777bb877ff18f360b5e13f954",
    "rocksdb-6.11.4.sst": "f02d53fc206bbdc4026e7be71cfc1597bd20bfa1a466127d3acb7f06ba61b6fd",
    "rocksdb-range.sst": "623b9a851a16925f628658537f78ffdfb2fd0723f4c1d441c9b9924c01893d00",
    "leveldb-1.23.ldb": "e08b32b8bbc7fcd02d0574ef43a9d041098b5ef49810295830ead19ef646ee99",
}
PLUGINS = {"viz-mysql-sdi":"mysql-sdi", "viz-sst-records":"sst-records"}


def fixtures():
    client, container = docker.from_env(timeout=30), None
    name = "ai-dataseek-physical-fixtures-" + uuid.uuid4().hex
    code = """import os,json,base64,hashlib
from pathlib import Path
assert os.getuid()==65534
spec=json.loads(os.environ['SPEC']); result={}
for name,digest in spec.items():
 p=Path('/app/tests/fixtures/physical-database')/name
 assert p.is_file() and not p.is_symlink() and p.stat().st_size<=131072
 b=p.read_bytes();assert hashlib.sha256(b).hexdigest()==digest
 result[name]=base64.b64encode(b).decode()
print(json.dumps(result),flush=True)
"""
    try:
        container=client.containers.create(get_settings().sandbox_image, name=name, entrypoint=["/usr/bin/timeout"],
            command=["--signal=KILL","15s","/app/.venv/bin/python","-c",code], user="65534:65534",
            network_mode="none",read_only=True,cap_drop=["ALL"],security_opt=["no-new-privileges:true"],
            mem_limit="128m",memswap_limit="128m",nano_cpus=500000000,pids_limit=16,
            environment={"SPEC":json.dumps(SPEC),"PYTHONDONTWRITEBYTECODE":"1"},
            labels={"ai-dataseek.component":"visualization-http-fixture","ai-dataseek.fixture-run":name},
            log_config={"type":"json-file","config":{"max-size":"1m","max-file":"1"}})
        container.reload();assert not container.attrs.get("Mounts")
        assert container.attrs["HostConfig"]["NetworkMode"]=="none" and container.attrs["HostConfig"]["ReadonlyRootfs"]
        container.start();assert container.wait(timeout=25)["StatusCode"]==0
        raw=bytearray()
        for chunk in container.logs(stdout=True,stderr=False,stream=True,follow=False):
            assert len(raw)+len(chunk)<=1024*1024;raw.extend(chunk)
        decoded=json.loads(raw);assert type(decoded) is dict and set(decoded)==set(SPEC)
        data={n:base64.b64decode(b,validate=True) for n,b in decoded.items()}
        assert all(hashlib.sha256(data[n]).hexdigest()==h for n,h in SPEC.items())
        data["bad.sst"]=data["rocksdb-6.11.4.sst"][:12]+bytes([data["rocksdb-6.11.4.sst"][12]^1])+data["rocksdb-6.11.4.sst"][13:]
        data["bad.ibd"]=b"PRIVATE-FIXTURE-NOT-FOR-OUTPUT"
        return data
    finally:
        try:
            if container is None:
                try: container=client.containers.get(name)
                except docker.errors.NotFound: pass
            if container is not None:
                container.reload();assert container.attrs["Config"]["Labels"].get("ai-dataseek.fixture-run")==name
                _remove_worker(container)
        finally: client.close()


async def acceptance(database, client, data, user_id):
    await assert_idle(database)
    before=await business_integrity_snapshot(database)
    ledger=FixtureLedger(database,SOURCE,"physical-http-"+uuid.uuid4().hex,user_id=user_id)
    files, originals, touched, checks, errors, deleted = {}, {}, set(), [], [], set()
    failure=None
    async def api(method,path,**kwargs):
        r=await client.request(method,path,**kwargs);r.raise_for_status();v=r.json();assert v["code"]==0;return v["data"]
    async def state(plugin,enabled):
        assert plugin in originals and type(enabled) is bool;touched.add(plugin)
        await api("PATCH",f"/api/v1/visualizations/{plugin}/state",json={"enabled":enabled})
    async def preview(name,plugin,status=200,**extra):
        await assert_idle(database)
        request={"plugin_id":plugin,"operation":"preview","kind":"tree","options":{},**extra}
        r=await client.post("/api/v1/files/"+quote(files[name],safe="")+"/visualization",json=request)
        checks.append({"file":name,"plugin":plugin,"expected":status,"status":r.status_code});assert r.status_code==status
        assert len(r.content)<=2097152+4096
        assert not any(marker in r.text for marker in ("/Users/","/home/","/tmp/","/private/","PRIVATE-FIXTURE-NOT-FOR-OUTPUT"))
        if status==200:
            v=VisualizationResult.model_validate(r.json()["data"])
            assert v.kind=="table" and v.plugin_id==plugin and len(v.version)==64
            p=dict(v.payload);p.pop("view_kind")
            validate_physical_payload({**p,"type":PLUGINS[plugin],"reader":PLUGINS[plugin],"kind":"tree","contract_version":2,
                "metadata":v.metadata,"warnings":v.warnings,"sampled":v.sampled},reader=PLUGINS[plugin],kind="tree",options={},
                fmt=name.rsplit(".",1)[-1],size=len(data[name]))
            return v
    try:
        catalog=await api("GET","/api/v1/visualizations")
        assert catalog["engine"]=="cordis" and len(catalog["plugins"])==83
        by_id={p["id"]:p for p in catalog["plugins"]}
        for plugin,reader in PLUGINS.items():
            p=by_id[plugin];assert p["reader"]==reader and p["adapter"]=="physical-database" and p["contract_version"]==2
            originals[plugin]=p["enabled"]
        for plugin,enabled in originals.items():
            if not enabled: await state(plugin,True)
        for name,body in data.items():
            await assert_idle(database)
            filename=ledger.declare(name,len(body))
            value=await api("POST","/api/v1/files",files={"file":(filename,body,"application/octet-stream")},
                data={"metadata":json.dumps({"source":SOURCE,"regression_run":ledger.run})})
            files[name]=await ledger.accept(value)
        for name,plugin in [("mysql-8.0.46.ibd","viz-mysql-sdi"),("rocksdb-6.11.4.sst","viz-sst-records"),("leveldb-1.23.ldb","viz-sst-records")]:
            result=await preview(name,plugin)
            rows=result.payload["table"]["rows"]
            if plugin=="viz-mysql-sdi": assert len(rows)==8 and rows[1][2]=="id"
            else: assert len(rows)==3 and rows[1][3]=="deletion" and rows[2][4]=="610062"
            await preview(name,plugin,409,version=("0" if result.version[0]!="0" else "1")*64)
            for key in ("sql","path","connection","restore"):
                await preview(name,plugin,422,options={key:"PRIVATE-FIXTURE-NOT-FOR-OUTPUT"})
            await preview(name,plugin,422,kind="table")
            await preview(name,plugin,422,operation="prepare")
            assert await ledger.assert_owned(files[name])
            r=await client.get("/api/v1/files/"+quote(files[name],safe="")+"/download");r.raise_for_status();assert r.content==data[name]
        for name,plugin in [("bad.ibd","viz-mysql-sdi"),("bad.sst","viz-sst-records"),("rocksdb-range.sst","viz-sst-records")]: await preview(name,plugin,422)
        for name,plugin,other in [("mysql-8.0.46.ibd","viz-mysql-sdi","rocksdb-6.11.4.sst"),("rocksdb-6.11.4.sst","viz-sst-records","mysql-8.0.46.ibd")]:
            await preview(other,plugin,422)
            await state(plugin,False);await preview(name,plugin,403)
    except BaseException as exc: failure=exc
    finally:
        for plugin in touched:
            try: await state(plugin,originals[plugin])
            except Exception: errors.append("preference_restore:"+plugin)
        try: await ledger.recover()
        except Exception: errors.append("fixture_inventory")
        for identifier in tuple(ledger.records):
            try: await cleanup_fixture(client,ledger,identifier);deleted.add(identifier)
            except Exception: errors.append("fixture_delete:"+identifier)
        try: await ledger.assert_clean()
        except Exception: errors.append("fixture_readback")
        try:
            states={p["id"]:p["enabled"] for p in (await api("GET","/api/v1/visualizations"))["plugins"]}
            assert all(states[p] is enabled for p,enabled in originals.items())
        except Exception: errors.append("preference_readback")
    after=await business_integrity_snapshot(database)
    summary={"passed":failure is None and not errors and before==after and set(ledger.records)==deleted,
        "checks":checks,"fixtures_uploaded":len(ledger.records),"fixtures_deleted":len(deleted),"cleanup_errors":errors,
        "business_state_preserved":before==after,"failure_type":type(failure).__name__ if failure else None,
        "existing_user_files_accessed":0,"model_calls":0,"database_restore_calls":0}
    print(json.dumps(summary,indent=2),flush=True)
    if not summary["passed"]: raise RuntimeError("Physical file acceptance failed; inspect cleanup status") from None


async def main():
    mongo=get_mongodb();await mongo.initialize()
    try:
        db=mongo.client[get_settings().mongodb_database];await assert_idle(db)
        data=await asyncio.to_thread(fixtures);user_id=(await get_current_user()).id
        async with httpx.AsyncClient(base_url="http://frontend",timeout=120,trust_env=False,follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0)) as client:
            await acceptance(db,client,data,user_id)
    finally: await mongo.shutdown()


if __name__=="__main__": asyncio.run(main())
