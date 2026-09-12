"""NIST Ripple vector cubes over an exact header/raw pair of byte capabilities.

No filesystem, NumPy memmap, filename discovery or metadata-selected resources.
"""
from __future__ import annotations
import math
import re
import struct
from decimal import Decimal
from .ripple_window_payload import (
    DTYPES, ERROR, LIMITS, WARNING, expected_axes, finite, integer, require,
    validate_cube, validate_ripple_resources, validate_ripple_window_options,
    validate_ripple_window_payload,
)

def parse_ripple_header(data, data_bytes):
    require(type(data) is bytes and 0<len(data)<=65536 and b"\x00" not in data)
    text=data.decode("latin-1")
    require(not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]",text))
    lines=[line.split(";",1)[0].strip(" \r") for line in text.split("\n")]
    lines=[line for line in lines if line]
    require(lines and len(lines)<=129)
    heading=lines.pop(0).split("\t")
    required={"width","height","depth","offset","data-length","data-type","byte-order","record-by"}
    require(len(heading)==2 and all(v.strip() for v in heading) and heading[0].strip().lower() not in required)
    fields={}
    for line in lines:
        require(len(line)<=2048 and "\t" in line)
        parts=line.split("\t"); key,value=parts[0].strip().lower(),parts[1].strip()
        require(re.fullmatch(r"[a-z][a-z0-9-]{0,63}",key) and key not in fields)
        require(key not in {"data-file","raw-file","filename","path","file","data-path"})
        fields[key]=value
    require(required<=set(fields) and fields["record-by"].lower()=="vector")
    def number(key):
        value=fields[key]; require(re.fullmatch(r"[0-9]{1,10}",value))
        return int(value)
    def real(key,default):
        if key not in fields: return default
        value=fields[key]
        require(len(value)<=64 and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?",value))
        result=float(value); require(finite(result) and (result!=0 or Decimal(value)==0))
        return result
    family,length=fields["data-type"].lower(),number("data-length")
    dtype=next((name for name,(f,n,_) in DTYPES.items() if f==family and n==length),None)
    require(dtype is not None)
    signal=fields.get("signal","").upper()
    aliases={"EELS":"EELS","TEM EELS":"EELS","EDS_SEM":"EDS_SEM","EDS_TEM":"EDS_TEM","":"unspecified"}
    require(signal in aliases)
    axes=[]
    for label in ("height","width","depth"):
        is_ev=label=="depth" and "ev-per-chan" in fields
        scale_key=label+"-scale"; unit=fields.get(label+"-units") or None
        origin_key=label+"-origin"
        declared=scale_key in fields
        require(declared or is_ev or not (origin_key in fields or unit is not None))
        scale=real(scale_key,real("ev-per-chan",1) if is_ev else 1)
        if is_ev:
            ev=real("ev-per-chan",1)
            require(ev>0 and scale==ev and unit in {None,"eV","ev"})
            unit="eV"
        calibration="ev-per-chan" if is_ev and not declared else "declared" if declared else "index"
        origin=real(origin_key,0)
        if not declared and not is_ev:
            unit=None
        axes.append({"label":label,"unit":unit,"origin":origin,"scale":scale,"calibration":calibration,"origin_defaulted":origin_key not in fields})
    cube={"width":number("width"),"height":number("height"),"depth":number("depth"),"dtype":dtype,
          "byte_order":fields["byte-order"].lower(),"data_offset":number("offset"),"signal_type":aliases[signal],"axes":axes}
    return validate_cube(cube,data_bytes)

def _preview(read_range,size,resources,kind,options,limits):
    options=validate_ripple_window_options(kind,{} if options is None else options)
    validate_ripple_resources(resources,size)
    maxima={"max_read_bytes":1048576,"max_total_bytes":8388608,"max_reads":256}
    limits=maxima if limits is None else limits
    require(callable(read_range) and isinstance(limits,dict) and set(limits)==set(maxima) and all(integer(limits[k],1,maxima[k]) for k in maxima))
    total=reads=0
    def fetch(offset,length):
        nonlocal total,reads
        require(integer(offset,0,size-1) and integer(length,1,min(size-offset,limits["max_read_bytes"]))
                and total+length<=limits["max_total_bytes"] and reads<limits["max_reads"]
                and any(r["offset"]<=offset and offset+length<=r["offset"]+r["size"] for r in resources))
        total+=length; reads+=1
        value=read_range(offset,length)
        require(type(value) is bytes and len(value)==length)
        return value
    header,data=resources
    cube=parse_ripple_header(fetch(0,header["size"]),data["size"])
    result={"contract_version":2,"type":"ripple-window","reader":"ripple-window","kind":kind,"media_type":"application/json",
            "choices":{"cube":cube},"selected":options,"warnings":[WARNING],"sampled":False}
    nulls=0
    if kind=="tree":
        result["tree"]=[{"path":"/cube","node_type":"array","shape":[cube["height"],cube["width"],cube["depth"]],"dtype":cube["dtype"]}]
    else:
        require(options["x"]<cube["width"] and options["y"]<cube["height"])
        if kind=="image":
            require(options["channel"]<cube["depth"] and options["x"]+options["width"]<=cube["width"] and options["y"]+options["height"]<=cube["height"])
            points=[(y,x,options["channel"]) for y in range(options["y"],options["y"]+options["height"]) for x in range(options["x"],options["x"]+options["width"])]
            shape,dimensions=[options["height"],options["width"]],["height","width"]
        else:
            require(options["channel_start"]+options["channel_count"]<=cube["depth"])
            points=[(options["y"],options["x"],c) for c in range(options["channel_start"],options["channel_start"]+options["channel_count"])]
            shape,dimensions=[options["channel_count"]],["depth"]
        _,width,code=DTYPES[cube["dtype"]]
        unpack=struct.Struct((">" if cube["byte_order"]=="big-endian" else "<")+code)
        locations=[(data["offset"]+cube["data_offset"]+((y*cube["width"]+x)*cube["depth"]+c)*width,i) for i,(y,x,c) in enumerate(points)]
        # Vector layout retains order; coalesced gaps count as actual input.
        groups=[]
        for offset,index in locations:
            if groups and offset-groups[-1][-1][0]<=4096 and offset+width-groups[-1][0][0]<=limits["max_read_bytes"]:
                groups[-1].append((offset,index))
            else: groups.append([(offset,index)])
        spans=[(g[0][0],g[-1][0]+width-g[0][0],g) for g in groups]
        require(total+sum(n for _,n,_ in spans)<=limits["max_total_bytes"] and reads+len(spans)<=limits["max_reads"])
        values=[None]*len(points)
        for offset,length,group in spans:
            chunk=fetch(offset,length)
            for location,index in group:
                value=unpack.unpack_from(chunk,location-offset)[0]
                if math.isfinite(value): values[index]=value
                else: nulls+=1
        result["array"]={"shape":shape,"dimensions":dimensions,"dtype":cube["dtype"],"values":values}
        result["axes"]=expected_axes(kind,options,cube)
    result["metadata"]={"format":"rpl","input_mode":"window","source_bytes":size,"header_bytes":header["size"],"data_bytes":data["size"],
                        "read_bytes":total,"read_requests":reads,"null_values":nulls,"limits":dict(LIMITS),"record_by":"vector","value_semantics":"raw-storage"}
    return validate_ripple_window_payload(result,kind=kind,options=options,fmt="rpl",source_bytes=size,read_bytes=total,read_requests=reads)

def ripple_window_preview(read_range,size,resources,kind="tree",options=None,limits=None):
    try: return _preview(read_range,size,resources,kind,options,limits)
    except Exception:
        raise ValueError(ERROR) from None

