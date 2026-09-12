"""Pure, strict AnnData spatial numeric-window contract (no HDF5 imports)."""
from __future__ import annotations
import json
import math
import re
MAX_SOURCE=8*1024**3
MAX_TOTAL=8*1024**2
MAX_OUTPUT=2*1024**2
LIMITS={"max_observations":8192,"max_scalars":24576,"max_sparse_entries":65536,
        "max_chunk_bytes":4194304,"max_decoded_chunk_bytes":16777216,
        "max_numeric_bytes":4194304,"max_attribute_buffer_bytes":4096}
VALUE_SEMANTICS="X storage values; upstream processing unknown; no normalization, registration or unit conversion"
SPARSE_SEMANTICS="absent entries are zero; sorted unique indices required in inspected segments"
SCOPE="selected numeric windows; no whole-file validation"
LABELS="ordinal only; obs and var labels not read"
AXES="stored columns 0,1; no inversion"
WARNINGS=["只显示 X 存储原值和 obsm/spatial 中同序号坐标；上游是否归一化未知，不做配准、单位换算或图像叠加。",
          "观测与特征仅显示从 0 开始的序号，不读取 obs/var 身份或名称；窗口不是随机抽样，也不代表全文件校验。"]
READ_STRATEGIES={
    "dense":"selected dense feature column and observation rows; HDF5 chunks may include other values",
    "csr":"all sparse entries in selected observation rows, then select one feature",
    "csc":"all sparse entries in selected feature column, then select observation rows"}
DTYPE=re.compile(r"(?:[|][iu]1|[<>][iu][248]|[<>]f[48])\Z")
ERROR="该 AnnData 空间结构、数值窗口或读取预算不在当前安全支持范围。"
def fail():raise ValueError(ERROR)
def require(value):
    if not value:fail()
def integer(value,low=0,high=2**31-1):return type(value) is int and low<=value<=high
def finite(value):return type(value) in {int,float} and -(2**53-1 if type(value) is int else 1.7976931348623157e308)<=value<=(2**53-1 if type(value) is int else 1.7976931348623157e308) and math.isfinite(value)
def keys(value,names):return type(value) is dict and set(value)==set(names.split())
def dtype(value):return isinstance(value,str) and DTYPE.fullmatch(value) is not None
def raw(value,kind):
    if value is None:return kind[-2]=="f"
    if not finite(value):return False
    family,width=kind[-2],int(kind[-1])
    if family=="f":return abs(value)<=(3.4028234663852886e38 if width==4 else 1.7976931348623157e308)
    return integer(value,0 if family=="u" else -2**(width*8-1),min(2**53-1,2**(width*8-(family=="i"))-1))
def validate_spatial_window_options(kind,options):
    require(isinstance(kind,str) and kind in {"tree","geometry"} and type(options) is dict)
    if kind=="tree":require(not options);return {}
    require(keys(options,"feature observation_start observation_count decode") and options["decode"]=="raw"
            and integer(options["feature"]) and integer(options["observation_start"])
            and integer(options["observation_count"],1,LIMITS["max_observations"]))
    return dict(options)
def _validate(value,*,kind=None,options=None,fmt=None,source_bytes=None,read_bytes=None,read_requests=None):
    require(type(value) is dict and isinstance(value.get("kind"),str))
    actual=value["kind"];selected=validate_spatial_window_options(actual,value.get("selected"))
    require(keys(value,"contract_version type reader kind media_type choices selected metadata warnings sampled "+("tree" if actual=="tree" else "spatial"))
            and type(value["contract_version"]) is int and value["contract_version"]==2
            and value["type"]==value["reader"]=="spatial-window" and value["media_type"]=="application/json"
            and value["warnings"]==WARNINGS and type(value["sampled"]) is bool
            and (kind is None or actual==kind) and (options is None or selected==validate_spatial_window_options(actual,options)))
    m=value["metadata"]
    require(keys(m,"format h5ad_encoding input_mode source_bytes read_bytes read_requests matrix coordinates labels value_semantics sparse_semantics scope read_strategy attribute_buffer_bytes chunks_touched decoded_chunk_bytes numeric_values_read numeric_bytes_read sparse_entries_scanned null_coordinates null_expressions plottable_points limits"))
    require(m["format"]=="h5ad" and (fmt is None or fmt=="h5ad") and m["h5ad_encoding"]=="0.1.0" and m["input_mode"]=="window"
            and integer(m["source_bytes"],256,MAX_SOURCE) and integer(m["read_bytes"],1,MAX_TOTAL) and integer(m["read_requests"],1,128)
            and m["labels"]==LABELS and m["value_semantics"]==VALUE_SEMANTICS and m["sparse_semantics"]==SPARSE_SEMANTICS and m["scope"]==SCOPE
            and m["limits"]==LIMITS and all(type(v) is int for v in m["limits"].values())
            and integer(m["attribute_buffer_bytes"],1,4096) and integer(m["chunks_touched"],0,128)
            and integer(m["decoded_chunk_bytes"],0,LIMITS["max_decoded_chunk_bytes"])
            and integer(m["numeric_values_read"],0,200000) and integer(m["numeric_bytes_read"],0,LIMITS["max_numeric_bytes"])
            and integer(m["sparse_entries_scanned"],0,LIMITS["max_sparse_entries"])
            and integer(m["null_coordinates"],0,16384) and integer(m["null_expressions"],0,8192) and integer(m["plottable_points"],0,8192))
    for field,expected in (("source_bytes",source_bytes),("read_bytes",read_bytes),("read_requests",read_requests)):
        require(expected is None or type(expected) is int and m[field]==expected)
    matrix=m["matrix"];coordinate=m["coordinates"]
    require(keys(matrix,"storage shape dtype nnz index_dtype pointer_dtype") and matrix["storage"] in READ_STRATEGIES
            and isinstance(matrix["shape"],list) and len(matrix["shape"])==2 and all(integer(v,1) for v in matrix["shape"]) and dtype(matrix["dtype"])
            and m["read_strategy"]==READ_STRATEGIES[matrix["storage"]])
    obs,features=matrix["shape"]
    if matrix["storage"]=="dense":require(matrix["nnz"] is None and matrix["index_dtype"] is None and matrix["pointer_dtype"] is None)
    else:
        require(integer(matrix["nnz"],0,2**53-1) and all(dtype(matrix[k]) and matrix[k][-2] in "iu" for k in ("index_dtype","pointer_dtype")))
    require(keys(coordinate,"path shape dtype unit axis_order") and coordinate["path"]=="/obsm/spatial"
            and coordinate["shape"]==[obs,2] and all(type(v) is int for v in coordinate["shape"])
            and dtype(coordinate["dtype"]) and coordinate["unit"] is None and coordinate["axis_order"]==AXES)
    require(keys(value["choices"],"observation_count feature_count feature_labels") and value["choices"]=={
        "observation_count":obs,"feature_count":features,"feature_labels":"zero-based ordinal"}
        and type(value["choices"]["observation_count"]) is int and type(value["choices"]["feature_count"]) is int)
    if actual=="tree":
        require(value["tree"]==[{"path":"/spatial","node_type":"array","shape":[obs,2]},{"path":"/X","node_type":"array","shape":[obs,features]}]
                and all(type(n) is int for row in value["tree"] for n in row["shape"]) and value["sampled"] is False
                and all(m[k]==0 for k in ("chunks_touched","decoded_chunk_bytes","numeric_values_read","numeric_bytes_read","sparse_entries_scanned","null_coordinates","null_expressions","plottable_points")))
    else:
        start,count,feature=selected["observation_start"],selected["observation_count"],selected["feature"]
        require(start+count<=obs and feature<features)
        s=value["spatial"];require(keys(s,"x y values observations"))
        require(all(isinstance(s[k],list) and len(s[k])==count for k in s)
                and all(integer(v,start,start+count-1) and v==start+i for i,v in enumerate(s["observations"]))
                and all(raw(v,coordinate["dtype"]) for k in ("x","y") for v in s[k])
                and all(raw(v,matrix["dtype"]) for v in s["values"]))
        require(m["null_coordinates"]==sum(v is None for k in ("x","y") for v in s[k])
                and m["null_expressions"]==sum(v is None for v in s["values"])
                and m["plottable_points"]==sum(all(s[k][i] is not None for k in ("x","y","values")) for i in range(count))
                and value["sampled"]==(count<obs))
        # Sparse implicit zeros need no data bytes, but all requested coordinates do.
        require(m["numeric_values_read"]>=2*count and m["numeric_bytes_read"]>=2*count*int(coordinate["dtype"][-1]))
        if matrix["storage"]=="dense":
            require(m["sparse_entries_scanned"]==0 and m["numeric_values_read"]==3*count
                    and m["numeric_bytes_read"]==count*(2*int(coordinate["dtype"][-1])+int(matrix["dtype"][-1])))
        else:
            n=m["sparse_entries_scanned"];p=count+1 if matrix["storage"]=="csr" else 2
            require(n<=matrix["nnz"] and m["numeric_values_read"]==2*count+2*n+p+2
                    and m["numeric_bytes_read"]==2*count*int(coordinate["dtype"][-1])+n*(int(matrix["dtype"][-1])+int(matrix["index_dtype"][-1]))+(p+2)*int(matrix["pointer_dtype"][-1]))
    require(len(json.dumps(value,allow_nan=False,ensure_ascii=False).encode())<=MAX_OUTPUT)
    return value
def validate_spatial_window_payload(value,**bindings):
    try:return _validate(value,**bindings)
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):raise ValueError(ERROR) from None
