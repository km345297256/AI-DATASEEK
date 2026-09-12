"""Strict GRO trajectory result contract; no filesystem or scientific runtime."""
from __future__ import annotations
import json
import math
import re

ERROR="GRO 轨迹不符合受限格式、帧选择或输出预算。"
WARNING="GRO 坐标为 nm、速度为 nm/ps、声明时间为 ps；不推断元素或化学键，不展开周期边界，不拟合或跨帧插值。"
LIMITS={"max_frames":64,"max_atoms":8192,"max_total_atoms":131072}
def need(ok):
    if not ok: raise ValueError(ERROR)
def integer(x,lo,hi):return type(x) is int and lo<=x<=hi
def finite(x,bound=1e12):return type(x) in {int,float} and math.isfinite(x) and abs(x)<=bound
def keys(x,names):return isinstance(x,dict) and set(x)==set(names.split())
def triple(x):return isinstance(x,list) and len(x)==3 and all(finite(v,1e6) for v in x)
def atom(a):
    return keys(a,"residue_number residue_name atom_name atom_number") and integer(a["residue_number"],0,99999) and integer(a["atom_number"],0,99999) and all(isinstance(a[k],str) and re.fullmatch(r"[A-Za-z0-9_+*'-]{1,5}",a[k]) for k in ("residue_name","atom_name"))
def box(v):
    return isinstance(v,list) and len(v)==9 and all(finite(x,1e6) for x in v) and all(v[i]==0 for i in (3,4,6)) and (all(x==0 for x in v) or all(v[i]>0 for i in (0,1,2)))
def validate_gro_trajectory_options(kind,options):
    need(kind in {"tree","geometry"} and isinstance(options,dict))
    need(not options if kind=="tree" else keys(options,"frame") and integer(options["frame"],0,63))
    return dict(options)

def validate_gro_trajectory_payload(v,*,kind=None,options=None,fmt=None,size=None,limit=2097152):
    try:
        need(isinstance(v,dict)); k=v.get("kind");selected=validate_gro_trajectory_options(k,v.get("selected"))
        need(kind is None or k==kind);need(options is None or selected==validate_gro_trajectory_options(k,options))
        need(keys(v,"contract_version type reader kind media_type choices selected metadata warnings sampled "+("tree" if k=="tree" else "trajectory")))
        need(type(v["contract_version"]) is int and v["contract_version"]==2 and v["type"]==v["reader"]=="gro-trajectory" and v["media_type"]=="application/json" and v["warnings"]==[WARNING] and v["sampled"] is False)
        m=v["metadata"]; need(keys(m,"format input_mode source_bytes frame_count total_atoms coordinate_unit velocity_unit time_unit topology_consistency limits"))
        need(m["format"]=="gro" and (fmt is None or fmt=="gro") and m["input_mode"]=="whole" and integer(m["source_bytes"],1,16*1024**2) and (size is None or type(size) is int and size==m["source_bytes"]))
        need(m["coordinate_unit"]=="nm" and m["velocity_unit"]=="nm/ps" and m["time_unit"]=="ps" and m["topology_consistency"]=="ordered GRO identifiers; bonds and elements unspecified" and m["limits"]==LIMITS and all(type(x) is int for x in m["limits"].values()))
        need(keys(v["choices"],"frames") and isinstance(v["choices"]["frames"],list));frames=v["choices"]["frames"]
        need(integer(m["frame_count"],1,64) and len(frames)==m["frame_count"] and integer(m["total_atoms"],1,131072))
        for i,f in enumerate(frames):
            need(keys(f,"id atoms time_ps velocities box") and integer(f["id"],i,i) and integer(f["atoms"],1,8192) and (f["time_ps"] is None or finite(f["time_ps"])) and type(f["velocities"]) is bool and box(f["box"]))
            need(f["atoms"]==frames[0]["atoms"])
        need(m["total_atoms"]==sum(f["atoms"] for f in frames))
        if k=="tree":
            need(v["tree"]==[{"path":f"/frames/{i}","node_type":"array","attributes":{"label":f"Frame {i+1}"}} for i in range(len(frames))])
        else:
            need(selected["frame"]<len(frames));f=frames[selected["frame"]];g=v["trajectory"]
            need(keys(g,"positions atoms velocities") and isinstance(g["positions"],list) and len(g["positions"])==f["atoms"] and all(triple(x) for x in g["positions"]))
            need(isinstance(g["atoms"],list) and len(g["atoms"])==f["atoms"] and all(atom(x) for x in g["atoms"]))
            need(g["velocities"] is None if not f["velocities"] else isinstance(g["velocities"],list) and len(g["velocities"])==f["atoms"] and all(triple(x) for x in g["velocities"]))
        need(integer(limit,1,2097152) and len(json.dumps(v,ensure_ascii=False,allow_nan=False).encode())<=limit)
        return v
    except (ValueError,KeyError,TypeError,OverflowError,AttributeError):raise ValueError(ERROR) from None
