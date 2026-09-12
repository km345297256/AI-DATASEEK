"""Bounded VTU wire geometry and explicitly associated scalar component."""
from __future__ import annotations
import json
import math
import re
ERROR="VTU 网格、标量关联或所选分量不符合受限仿真预览规范。"
WARNING="仅绘制已存储网格线框与节点值或单元顶点均值位置上的值；不提取外表面、不插值、不变形、不拟合或执行求解器。坐标与场单位未声明。"
LIMITS={"max_points":4096,"max_cells":2048,"max_fields":32,"max_components":9,"max_field_values":131072}
CELL_SIZES={3:2,5:3,9:4,10:4,12:8,13:6,14:5}
CELL_EDGES={3:((0,1),),5:((0,1),(1,2),(2,0)),9:((0,1),(1,2),(2,3),(3,0)),10:((0,1),(1,2),(2,0),(0,3),(1,3),(2,3)),12:((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7)),13:((0,1),(1,2),(2,0),(3,4),(4,5),(5,3),(0,3),(1,4),(2,5)),14:((0,1),(1,2),(2,3),(3,0),(0,4),(1,4),(2,4),(3,4))}
def need(x):
    if not x:raise ValueError(ERROR)
def integer(x,lo,hi):return type(x) is int and lo<=x<=hi
def finite(x):return type(x) in {int,float} and math.isfinite(x) and abs(x)<=1e12
def keys(x,n):return isinstance(x,dict) and set(x)==set(n.split())
def label(x):return isinstance(x,str) and 0<len(x)<=128 and not re.search(r"[<>\x00-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)",x,re.I)
def validate_simulation_mesh_options(kind,options):
    need(kind in {"tree","geometry"} and isinstance(options,dict))
    if kind=="tree":need(not options)
    else:
        need(keys(options,"field component") and (options["field"] is None or isinstance(options["field"],str) and re.fullmatch(r"[pc]-[0-9]{1,2}",options["field"])) and integer(options["component"],0,8))
        if options["field"] is None:need(options["component"]==0)
    return dict(options)
def validate_simulation_mesh_payload(v,*,kind=None,options=None,fmt=None,size=None,limit=2097152):
    try:
        need(isinstance(v,dict));k=v.get("kind");s=validate_simulation_mesh_options(k,v.get("selected"))
        need(kind is None or kind==k);need(options is None or s==validate_simulation_mesh_options(k,options))
        need(keys(v,"contract_version type reader kind media_type choices selected metadata warnings sampled "+("tree" if k=="tree" else "mesh")))
        need(type(v["contract_version"]) is int and v["contract_version"]==2 and v["type"]==v["reader"]=="simulation-mesh" and v["media_type"]=="application/json" and v["warnings"]==[WARNING] and v["sampled"] is False)
        m=v["metadata"];need(keys(m,"format input_mode source_bytes point_count cell_count cell_types field_values coordinate_unit field_unit limits value_semantics"))
        need(m["format"]=="vtu" and (fmt is None or fmt=="vtu") and m["input_mode"]=="whole" and integer(m["source_bytes"],1,16*1024**2) and (size is None or type(size) is int and size==m["source_bytes"]))
        need(integer(m["point_count"],1,4096) and integer(m["cell_count"],1,2048) and integer(m["field_values"],0,131072) and m["coordinate_unit"]==m["field_unit"]=="unspecified" and m["value_semantics"]=="stored component; no interpolation or deformation" and m["limits"]==LIMITS and all(type(x) is int for x in m["limits"].values()))
        need(isinstance(m["cell_types"],list) and m["cell_types"] and all(type(t) is int and t in CELL_SIZES for t in m["cell_types"]) and m["cell_types"]==sorted(set(m["cell_types"])))
        need(keys(v["choices"],"fields") and isinstance(v["choices"]["fields"],list) and len(v["choices"]["fields"])<=32)
        fields=v["choices"]["fields"];ids=set();counts={"point":0,"cell":0};total=0
        for f in fields:
            need(keys(f,"id name association components tuples") and f["association"] in counts and label(f["name"]) and integer(f["components"],1,9) and f["tuples"]==m[f["association"]+"_count"] and type(f["tuples"]) is int)
            association=f["association"];need(f["id"]==association[0]+"-"+str(counts[association]) and f["id"] not in ids);ids.add(f["id"]);counts[association]+=1;total+=f["tuples"]*f["components"]
        need(total==m["field_values"])
        if k=="tree":need(v["tree"]==[{"path":"/mesh","node_type":"array","attributes":{"label":"VTU mesh"}}])
        else:
            g=v["mesh"];need(keys(g,"points cells field") and isinstance(g["points"],list) and len(g["points"])==m["point_count"] and all(isinstance(p,list) and len(p)==3 and all(finite(n) for n in p) for p in g["points"]))
            need(isinstance(g["cells"],list) and len(g["cells"])==m["cell_count"])
            for c in g["cells"]:
                need(keys(c,"type points") and type(c["type"]) is int and c["type"] in CELL_SIZES and isinstance(c["points"],list) and len(c["points"])==CELL_SIZES[c["type"]] and all(integer(i,0,m["point_count"]-1) for i in c["points"]) and len(set(c["points"]))==len(c["points"]))
            need(sorted({c["type"] for c in g["cells"]})==m["cell_types"])
            if s["field"] is None:need(g["field"] is None)
            else:
                f=next((f for f in fields if f["id"]==s["field"]),None);need(f is not None and s["component"]<f["components"])
                d=g["field"];need(keys(d,"id association component values") and d["id"]==f["id"] and d["association"]==f["association"] and integer(d["component"],s["component"],s["component"]) and isinstance(d["values"],list) and len(d["values"])==f["tuples"] and all(x is None or finite(x) for x in d["values"]))
        need(integer(limit,1,2097152) and len(json.dumps(v,ensure_ascii=False,allow_nan=False).encode())<=limit);return v
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):raise ValueError(ERROR) from None
