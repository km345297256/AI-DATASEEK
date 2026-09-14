"""Offline fault injection; never run Docker or the actual business API."""
import ast
import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import uuid
from urllib.parse import quote
import pytest
from test_database_visualization_http_safety import namespace as previous_namespace, fake_database, Response, CleanupClient
from test_visualization_acceptance_safety import SAFETY, OWNER, OTHER_ID

ROOT=Path(__file__).resolve().parents[2]
SCRIPT=ROOT/"backend/scripts/check_physical_database_http.py"


def namespace(**extra):
    tree=ast.parse(SCRIPT.read_text())
    nodes=[copy.deepcopy(n) for n in tree.body if isinstance(n,(ast.Assign,ast.FunctionDef,ast.AsyncFunctionDef))]
    previous=previous_namespace()
    result={"asyncio":asyncio,"base64":base64,"hashlib":hashlib,"json":json,"uuid":uuid,"quote":quote,
        "FixtureLedger":SAFETY.FixtureLedger,"assert_idle":previous["assert_idle"],"cleanup_fixture":previous["cleanup_fixture"],
        "print":lambda *a,**k:None,**extra}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),str(SCRIPT),"exec"),result)
    return result


class Client(CleanupClient):
    def __init__(self,db,plugins,lost=None):
        super().__init__(db);self.plugins=plugins;self.states={p:False for p in plugins};self.patches=[];self.posts=0;self.lost=lost
    async def request(self,method,path,**kw):
        if method=="GET":
            return Response({"engine":"cordis","plugins":[*[{"id":p,"reader":r,"adapter":"physical-database","contract_version":2,"enabled":self.states[p]} for p,r in self.plugins.items()],*[{"id":f"other-{i}","enabled":True} for i in range(81)]]})
        if method=="PATCH":
            p=path.split("/")[-2];assert p in self.states;self.states[p]=kw["json"]["enabled"];self.patches.append(p)
            if self.lost=="patch" and len(self.patches)==1: raise TimeoutError("lost response")
            return Response({})
        assert method=="POST" and path=="/api/v1/files"
        name,data,_=kw["files"]["file"];self.posts+=1
        row={"_id":f"test-{self.posts}","file_id":f"fixture:{self.posts}","filename":name,"size":len(data),"provider":"minio","user_id":OWNER,"metadata":json.loads(kw["data"]["metadata"])}
        self.database.stored_files.rows.append(row)
        if self.lost=="upload": raise TimeoutError("lost upload response")
        return Response(row)
    async def post(self,path,**kwargs): return Response(status=500)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost",[None,"patch","upload"])
async def test_uncertain_writes_restore_only_exact_test_objects(lost):
    async def snapshot(db): return copy.deepcopy(db.stored_files.rows)
    code=namespace(business_integrity_snapshot=snapshot);db=fake_database()
    original={"file_id":OTHER_ID,"filename":"original-business-file","user_id":OWNER};db.stored_files.rows.append(original)
    client=Client(db,code["PLUGINS"],lost)
    with pytest.raises(RuntimeError): await code["acceptance"](db,client,{"mysql-8.0.46.ibd":b"sample"},OWNER)
    assert db.stored_files.rows==[original] and OTHER_ID not in client.deleted
    assert all(v is False for v in client.states.values())
    assert client.posts==(0 if lost=="patch" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("collection,status",[("sessions","running"),("sessions","waiting"),("analysis_jobs","queued"),("analysis_jobs","running")])
async def test_busy_gate_precedes_all_mutations(collection,status):
    code=namespace();db=fake_database();getattr(db,collection).rows.append({"status":status})
    with pytest.raises(RuntimeError): await code["acceptance"](db,object(),{},OWNER)
    assert db.stored_files.rows==[]


def test_pinned_fixtures_and_real_manifests():
    code=namespace()
    for name,digest in code["SPEC"].items(): assert hashlib.sha256((ROOT/"sandbox/tests/fixtures/physical-database"/name).read_bytes()).hexdigest()==digest
    for plugin,reader in code["PLUGINS"].items():
        m=json.loads((ROOT/"plugin-host/visualizations"/(reader+".json")).read_text())
        assert m["id"]==plugin and m["adapter"]=="physical-database"
    source=SCRIPT.read_text()
    assert 'network_mode="none",read_only=True' in source and 'user="65534:65534"' in source
    assert 'not container.attrs.get("Mounts")' in source and 'retries=0' in source
    assert 'update_one(' not in source and 'drop_database(' not in source
