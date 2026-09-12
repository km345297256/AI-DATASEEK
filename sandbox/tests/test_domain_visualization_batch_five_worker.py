"""Actual private JSONL dispatch for each fifth-batch range reader."""
import base64
import io
import json
import pytest
from app.services.window_visualization_worker import PROFILES,PROTOCOL,run
from radar_window_fixtures import radar_bytes,SELECTION
from ugrid_window_fixtures import ugrid_bytes
from app.services.ugrid_window_reader import _id
CASES=[
 ("radar-window","h5","image",SELECTION),
 ("ugrid-window","nc","geometry",{"mesh":_id("mesh","u"),"field":None,"indices":[]}),
]
def content(reader):
    return radar_bytes() if reader=="radar-window" else ugrid_bytes()
def init(data,reader,fmt,kind,options):
    return {"protocol":PROTOCOL,"type":"init","size":len(data),"reader":reader,"format":fmt,"kind":kind,"options":options,
            "limits":{"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":128}}
class Host:
    def __init__(self,data,request,output,mutate=None):
        self.data,self.request,self.output,self.mutate=data,request,output,mutate;self.first=True
    def readline(self,maximum):
        if self.first:self.first=False;response=self.request
        else:
            read=json.loads(self.output.getvalue().splitlines()[-1]);assert set(read)=={"type","id","offset","length"} and read["type"]=="read"
            assert 0<=read["offset"]<len(self.data) and 0<read["length"]<=min(1048576,len(self.data)-read["offset"])
            response={"type":"bytes","id":read["id"],"data_base64":base64.b64encode(self.data[read["offset"]:read["offset"]+read["length"]]).decode()}
            if self.mutate:response=self.mutate(response)
        return (json.dumps(response).encode()+b"\n")[:maximum]
def invoke(data,request,mutate=None):
    output=io.BytesIO();run(Host(data,request,output,mutate),output)
    return [json.loads(line) for line in output.getvalue().splitlines()],output.getvalue()

@pytest.mark.parametrize("reader,fmt,kind,options",CASES)
@pytest.mark.parametrize("metadata_only",[True,False])
def test_real_jsonl_dispatch_counts_and_exact_single_terminal(reader,fmt,kind,options,metadata_only):
    data=content(reader);kind,options=("tree",{}) if metadata_only else (kind,options)
    messages,raw=invoke(data,init(data,reader,fmt,kind,options))
    terminal=messages[-1];assert terminal["type"]=="result" and terminal["ok"] is True
    assert sum(row["type"]=="result" for row in messages)==1 and all(row["type"]=="read" for row in messages[:-1])
    result=terminal["data"];assert result["reader"]==reader and result["kind"]==kind and result["selected"]==options
    assert result["metadata"]["read_bytes"]==sum(row["length"] for row in messages[:-1])
    assert result["metadata"]["read_requests"]==len(messages)-1<=128
    assert [row["id"] for row in messages[:-1]]==list(range(1,len(messages)))
    assert b"SYNTHETIC-NAME" not in raw and b"SYNTHETIC-ID" not in raw and b"/private/fixture" not in raw

@pytest.mark.parametrize("reader,fmt,kind,options",CASES)
@pytest.mark.parametrize("change",["extra-resource","path","format","kind","source","count-budget","byte-budget","extra-options"])
def test_init_rejects_unapproved_scope_and_budgets_before_read(reader,fmt,kind,options,change):
    data=content(reader);request=init(data,reader,fmt,kind,options)
    if change=="extra-resource":request["resources"]=[{"key":"foreign","offset":0,"size":len(data)}]
    elif change=="path":request["path"]="/private/target"
    elif change=="format":request["format"]="raw"
    elif change=="kind":request["kind"]="series"
    elif change=="source":request["size"]=True
    elif change=="count-budget":request["limits"]["max_reads"]=129
    elif change=="byte-budget":request["limits"]["max_total_bytes"]=8388609
    else:request["options"]={**options,"path":"/private/target"}
    messages,_=invoke(data,request)
    assert messages==[{"type":"result","ok":False,"error":"rejected"}]

@pytest.mark.parametrize("reader,fmt,kind,options",CASES)
@pytest.mark.parametrize("change",["id","extra","base64","empty","type"])
def test_corrupted_range_replies_are_scrubbed(reader,fmt,kind,options,change):
    data=content(reader)
    def corrupt(r):
        if change=="id":r["id"]=True
        elif change=="extra":r["filename"]="/private/secret"
        elif change=="base64":r["data_base64"]="?"
        elif change=="empty":r["data_base64"]=""
        else:r["type"]="result"
        return r
    messages,raw=invoke(data,init(data,reader,fmt,kind,options),corrupt)
    assert len(messages)==2 and messages[-1]=={"type":"result","ok":False,"error":"rejected"}
    assert b"/private" not in raw and b"Traceback" not in raw

@pytest.mark.parametrize("reader,fmt,kind,options",CASES)
def test_existing_worker_limits_stay_independent(reader,fmt,kind,options):
    profile=PROFILES[reader]
    assert profile[2:]==(8388608,128,2097152) and PROFILES["edf"][2:]==(8388608,128,2097152)
    assert "geometry" not in PROFILES["edf"][1]
