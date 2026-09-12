"""Synthetic NIST Ripple vector cube, independent NumPy memory layout."""
import json
import numpy as np
from app.services.ripple_window_reader import ripple_window_preview

def fixture(dtype="<f4", *, calibrated=True):
    values=np.fromfunction(lambda y,x,c: y*100+x*10+c,(3,4,8)).astype(dtype)
    kind={"i":"signed","u":"unsigned","f":"float"}[values.dtype.kind]
    order="dont-care" if values.dtype.itemsize==1 else "big-endian" if dtype.startswith(">") else "little-endian"
    fields={"width":4,"height":3,"depth":8,"offset":8,"data-length":values.dtype.itemsize,"data-type":kind,"byte-order":order,"record-by":"vector","signal":"EDS_TEM"}
    if calibrated:
        fields.update({"depth-scale":5,"depth-origin":100,"depth-units":"eV","width-scale":2,"width-origin":10,"width-units":"nm","height-scale":-3,"height-origin":20,"height-units":"nm"})
    header=("key\tvalue\n"+"".join(str(k)+"\t"+str(v)+"\n" for k,v in fields.items())).encode("ascii")
    return header,b"padding!"+values.tobytes(),values

def preview(header,raw,kind="tree",options=None,limits=None):
    calls=[]
    def read(o,n):
        calls.append((o,n)); return (header+raw)[o:o+n]
    resources=[{"key":"header","offset":0,"size":len(header)},{"key":"data","offset":len(header),"size":len(raw)}]
    result=ripple_window_preview(read,len(header)+len(raw),resources,kind,options,limits)
    return result,calls

def browser_payloads():
    header,raw,_=fixture()
    return {"provenance":"Independent NumPy Ripple vector raw EDS_TEM fixture; no real data",
            "tree":preview(header,raw)[0],
            "image":preview(header,raw,"image",{"channel":3,"x":1,"y":1,"width":3,"height":2})[0],
            "series":preview(header,raw,"series",{"x":2,"y":1,"channel_start":1,"channel_count":4})[0]}

if __name__=="__main__": print(json.dumps(browser_payloads(),ensure_ascii=False,allow_nan=False))

