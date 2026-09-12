"""Ripple vector-cube contract; pure validation, no filesystem/native imports."""
from __future__ import annotations
import json
import math
import re

ERROR = "Ripple 预览不符合受控谱像格式或读取预算；请检查配对文件、坐标及选区。"
WARNING = "只显示单通道原始图像或单像元谱线；坐标仅按文件声明展开，不做能量校准、背景扣除、元素识别或定量。"
MAX_SOURCE = 8 * 1024**3
LIMITS = {"max_values": 16384, "max_depth": 1048576, "max_spectrum_channels": 16384}
DTYPES = {"int8": ("signed",1,"b"), "uint8": ("unsigned",1,"B"),
          "int16": ("signed",2,"h"), "uint16": ("unsigned",2,"H"),
          "int32": ("signed",4,"i"), "uint32": ("unsigned",4,"I"),
          "float32": ("float",4,"f"), "float64": ("float",8,"d")}

def require(value):
    if not value: raise ValueError(ERROR)

def integer(v, low, high):
    return type(v) is int and low <= v <= high

def finite(v):
    try: return type(v) in {int,float} and math.isfinite(v)
    except OverflowError: return False

def keys(v, names):
    return isinstance(v,dict) and set(v) == set(names.split())

def validate_ripple_resources(resources, size):
    require(integer(size,2,MAX_SOURCE) and isinstance(resources,list) and len(resources)==2)
    offset=0
    for row,key,limit in zip(resources,("header","data"),(65536,MAX_SOURCE)):
        require(keys(row,"key offset size") and row["key"]==key and integer(row["offset"],offset,offset) and integer(row["size"],1,limit))
        offset+=row["size"]
    require(offset==size)
    return resources

def validate_ripple_window_options(kind, options):
    require(isinstance(kind,str) and kind in {"tree","image","series"} and isinstance(options,dict))
    if kind=="tree":
        require(not options)
    elif kind=="image":
        require(keys(options,"channel x y width height"))
        require(integer(options["channel"],0,1048575) and integer(options["x"],0,2**31-1) and integer(options["y"],0,2**31-1)
                and integer(options["width"],1,128) and integer(options["height"],1,128))
    else:
        require(keys(options,"x y channel_start channel_count"))
        require(integer(options["x"],0,2**31-1) and integer(options["y"],0,2**31-1)
                and integer(options["channel_start"],0,1048575) and integer(options["channel_count"],1,16384))
    return dict(options)

def validate_cube(cube, data_bytes):
    require(keys(cube,"width height depth dtype byte_order data_offset signal_type axes"))
    require(integer(cube["width"],1,2**31-1) and integer(cube["height"],1,2**31-1) and integer(cube["depth"],2,1048576)
            and isinstance(cube["dtype"],str) and cube["dtype"] in DTYPES
            and isinstance(cube["byte_order"],str) and cube["byte_order"] in {"little-endian","big-endian","dont-care"}
            and integer(cube["data_offset"],0,1048576)
            and isinstance(cube["signal_type"],str) and cube["signal_type"] in {"EELS","EDS_SEM","EDS_TEM","unspecified"})
    _,width,_=DTYPES[cube["dtype"]]
    require((width==1)==(cube["byte_order"]=="dont-care"))
    require(cube["data_offset"]+cube["width"]*cube["height"]*cube["depth"]*width==data_bytes)
    require(isinstance(cube["axes"],list) and len(cube["axes"])==3)
    for desc,label,size in zip(cube["axes"],("height","width","depth"),(cube["height"],cube["width"],cube["depth"])):
        require(keys(desc,"label unit origin scale calibration origin_defaulted") and desc["label"]==label
                and (desc["unit"] is None or isinstance(desc["unit"],str) and 0<len(desc["unit"])<=64 and re.fullmatch(r"[A-Za-z0-9 µμÅÅ°^()./− -]+",desc["unit"]) is not None and not re.search(r"(?:^/|/(?:Users|home|tmp|private|var)/)",desc["unit"],re.I))
                and finite(desc["origin"]) and abs(desc["origin"])<=1e12 and finite(desc["scale"]) and 1e-12<=abs(desc["scale"])<=1e12
                and isinstance(desc["calibration"],str) and desc["calibration"] in {"index","declared","ev-per-chan"} and type(desc["origin_defaulted"]) is bool)
        if desc["origin_defaulted"]: require(desc["origin"]==0)
        if desc["calibration"]=="index":
            require(desc=={"label":label,"unit":None,"origin":0,"scale":1,"calibration":"index","origin_defaulted":True})
        if desc["calibration"]=="ev-per-chan": require(label=="depth" and desc["unit"]=="eV" and desc["scale"]>0)
        end=desc["origin"]+(size-1)*desc["scale"]
        require(finite(end) and abs(end)<=1e15 and (size<2 or desc["origin"]+desc["scale"]!=desc["origin"] and end-desc["scale"]!=end))
    return cube

def expected_axes(kind, options, cube):
    dimensions=[(0,range(options["y"],options["y"]+options["height"])),(1,range(options["x"],options["x"]+options["width"]))] if kind=="image" else [(2,range(options["channel_start"],options["channel_start"]+options["channel_count"]))]
    return [{"label":cube["axes"][dim]["label"],"unit":cube["axes"][dim]["unit"],
             "values":[cube["axes"][dim]["origin"]+i*cube["axes"][dim]["scale"] for i in indices]} for dim,indices in dimensions]

def _validate(value, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None):
    require(isinstance(value,dict) and isinstance(value.get("kind"),str))
    actual=value["kind"]
    selected=validate_ripple_window_options(actual,value.get("selected"))
    require(kind is None or kind==actual)
    fields="contract_version type reader kind media_type choices selected metadata warnings sampled "+("tree" if actual=="tree" else "array axes")
    require(keys(value,fields) and type(value["contract_version"]) is int and value["contract_version"]==2
            and value["type"]==value["reader"]=="ripple-window" and value["media_type"]=="application/json"
            and value["sampled"] is False and value["warnings"]==[WARNING])
    m=value["metadata"]
    require(keys(m,"format input_mode source_bytes header_bytes data_bytes read_bytes read_requests null_values limits record_by value_semantics"))
    require(m["format"]=="rpl" and (fmt is None or fmt=="rpl") and m["input_mode"]=="window"
            and integer(m["source_bytes"],2,MAX_SOURCE) and integer(m["header_bytes"],1,65536) and integer(m["data_bytes"],1,MAX_SOURCE)
            and m["header_bytes"]+m["data_bytes"]==m["source_bytes"] and integer(m["read_bytes"],m["header_bytes"],8*1024**2)
            and integer(m["read_requests"],1,256) and integer(m["null_values"],0,16384) and m["limits"]==LIMITS
            and all(type(v) is int for v in m["limits"].values()) and m["record_by"]=="vector" and m["value_semantics"]=="raw-storage")
    for key,expected in (("source_bytes",source_bytes),("read_bytes",read_bytes),("read_requests",read_requests)):
        require(expected is None or type(expected) is int and expected==m[key])
    require(keys(value["choices"],"cube"))
    cube=validate_cube(value["choices"]["cube"],m["data_bytes"])
    require(options is None or selected==validate_ripple_window_options(actual,options))
    if actual=="tree":
        require(isinstance(value["tree"],list) and len(value["tree"])==1 and keys(value["tree"][0],"path node_type shape dtype")
                and isinstance(value["tree"][0]["shape"],list) and all(type(v) is int for v in value["tree"][0]["shape"]))
        require(value["tree"]==[{"path":"/cube","node_type":"array","shape":[cube["height"],cube["width"],cube["depth"]],"dtype":cube["dtype"]}]
                and m["read_bytes"]==m["header_bytes"] and m["read_requests"]==1 and m["null_values"]==0)
    else:
        require(selected["x"]<cube["width"] and selected["y"]<cube["height"])
        if actual=="image":
            require(selected["channel"]<cube["depth"] and selected["x"]+selected["width"]<=cube["width"] and selected["y"]+selected["height"]<=cube["height"])
            shape,dimensions=[selected["height"],selected["width"]],["height","width"]
        else:
            require(selected["channel_start"]+selected["channel_count"]<=cube["depth"])
            shape,dimensions=[selected["channel_count"]],["depth"]
        a=value["array"]
        require(keys(a,"shape dimensions dtype values") and a["shape"]==shape and all(type(v) is int for v in a["shape"]) and a["dimensions"]==dimensions and a["dtype"]==cube["dtype"])
        values=a["values"]
        require(isinstance(values,list) and len(values)==math.prod(shape)<=16384 and sum(v is None for v in values)==m["null_values"])
        family,width,_=DTYPES[cube["dtype"]]
        require(m["read_requests"]>=2 and m["read_bytes"]>=m["header_bytes"]+len(values)*width)
        if family=="float":
            limit=3.4028234663852886e38 if width==4 else 1.7976931348623157e308
            require(all(v is None or finite(v) and abs(v)<=limit for v in values))
        else:
            low,high=(0,2**(width*8)-1) if family=="unsigned" else (-2**(width*8-1),2**(width*8-1)-1)
            require(all(integer(v,low,high) for v in values))
        require(isinstance(value["axes"],list) and all(keys(axis,"label unit values") and isinstance(axis["values"],list) and all(finite(v) for v in axis["values"]) for axis in value["axes"]))
        require(value["axes"]==expected_axes(actual,selected,cube))
        for axis in value["axes"]:
            v=axis["values"]
            require(len(v)<2 or all(a<b for a,b in zip(v,v[1:])) or all(a>b for a,b in zip(v,v[1:])))
    require(len(json.dumps(value,allow_nan=False,ensure_ascii=False).encode())<=2*1024**2)
    return value

def validate_ripple_window_payload(value, **bindings):
    try: return _validate(value,**bindings)
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):
        raise ValueError(ERROR) from None
