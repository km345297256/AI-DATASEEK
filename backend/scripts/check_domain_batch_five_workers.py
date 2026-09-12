"""No business API writes: actual networkless one-shot workers and host validators.

Uses the same nine synthetic source files as the HTTP acceptance. No user files,
dataset mount, model, catalog preference, or session is touched. The public HTTP
version/ownership/catalog tests remain separate and must run after deployment.
"""
import json
import threading
from check_domain_batch_five_http import fixtures, VALIDATORS, PLUGINS, FORBIDDEN
from app.core.config import get_settings
from app.application.services.extended_visualization import validate_payload, validate_requested_selection
from app.infrastructure.external.sandbox.extended_visualization_worker import run_extended_visualization_worker
from app.infrastructure.external.sandbox.window_visualization_worker import run_window_visualization_worker
from app.infrastructure.external.sandbox.visualization_worker import VisualizationWorkerError


def main():
    data, versions = fixtures()
    checks = []
    image = get_settings().sandbox_image

    def invoke(name, plugin, kind="tree", options=None):
        reader, options = plugin[4:], options or {}
        blob, fmt = data[name], name.rsplit(".", 1)[-1]
        if reader == "sqlite-table":
            result = run_extended_visualization_worker(image, blob, reader=reader, kind=kind,
                format=fmt, options=options, truncated=False, cancelled=threading.Event())
            if result.get("ok") is not True: raise VisualizationWorkerError("Rejected synthetic source")
            result = result["data"]
            validate_payload(result, reader, kind, 2097152)
            validate_requested_selection(result, reader, kind, options, fmt, len(blob))
        else:
            calls = []
            def read(offset, length):
                calls.append((offset,length))
                return blob[offset:offset+length]
            result = run_window_visualization_worker(image,size=len(blob),reader=reader,kind=kind,
                format=fmt,options=options,limits={"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":128},
                read_range=read,cancelled=threading.Event(),max_output_bytes=2097152)
            if result.get("ok") is not True: raise VisualizationWorkerError("Rejected synthetic source")
            result = result["data"]
            VALIDATORS[plugin](result,kind=kind,options=options,fmt=fmt,source_bytes=len(blob),
                read_bytes=sum(n for _,n in calls),read_requests=len(calls))
        encoded=json.dumps(result,ensure_ascii=False,allow_nan=False)
        assert not any(marker.lower() in encoded.lower() for marker in FORBIDDEN)
        assert len(encoded.encode())<=2097152
        checks.append({"file":name,"reader":reader,"kind":kind,"passed":True})
        return result

    tree=invoke("sample.sqlite",PLUGINS[0])
    table=next(t for t in tree["choices"]["tables"] if t["label"]=="measurements")
    selection={"table":table["id"],"columns":[0,1,2,3,4,5],"row_offset":0,"row_limit":2}
    first=invoke("sample.sqlite",PLUGINS[0],"table",selection)
    assert first["table"]["rows"][0][1]=={"type":"integer","value":"-9223372036854775808"}
    assert first["table"]["rows"][1][1]=={"type":"integer","value":"9223372036854775807"}
    second=invoke("sample.sqlite",PLUGINS[0],"table",{**selection,"row_offset":2})
    assert second["table"]["rows"][0][1]=={"type":"integer","value":"9007199254740993"}
    for name in ("positive.h5","negative.hdf5"):
        invoke(name,PLUGINS[1])
        selected={"sweep":1,"quantity":"DBZH","ray_start":1,"ray_count":2,"gate_start":1,"gate_count":4,"decode":"raw"}
        result=invoke(name,PLUGINS[1],"image",selected)
        assert result["array"]["values"]==[255,0,18,19,26,27,28,29]
        assert result["radar"]["range_m"]==[1375,1625,1875,2125]
    for name,location in (("node.nc","node"),("face.nc4","face")):
        tree=invoke(name,PLUGINS[2]);mesh=tree["choices"]["meshes"][0]
        field=next(f for f in mesh["fields"] if f["location"]==location)
        result=invoke(name,PLUGINS[2],"geometry",{"mesh":mesh["id"],"field":field["id"],"indices":[1,2] if location=="node" else [1]})
        assert result["ugrid"]["faces"]==[[0,1,2,3],[1,4,2]]
        assert result["ugrid"]["values"]==([17,20,None,26,29] if location=="node" else [2,4])
    for name,plugin in (("view.db",PLUGINS[0]),("wal.sqlite3",PLUGINS[0]),("old.h5",PLUGINS[1]),("bad.nc",PLUGINS[2])):
        try:invoke(name,plugin)
        except VisualizationWorkerError:checks.append({"file":name,"reader":plugin[4:],"rejected":True})
        else:raise AssertionError("Unsupported source was accepted")
    print(json.dumps({"passed":True,"versions":versions,"checks":checks,"business_api_writes":0},ensure_ascii=False,indent=2))


if __name__=="__main__":main()
