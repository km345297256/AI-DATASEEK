"""Inert DICOM window contract; no DICOM library, file, network or clinical services."""
import json
import math

MAX_SOURCE = 8 * 1024**3
MAX_TOTAL = 8 * 1024**2
MAX_SINGLE = 1024**2
MAX_READS = 128
MAX_OUTPUT = 2 * 1024**2
LIMITS = {"max_roi": 128, "max_frames": 4096, "max_header_bytes": 1048576}
ERROR = "DICOM 不符合受控未压缩灰度格式、脱敏声明、选择或读取预算。"
WARNINGS = [
    "仅用于已脱敏科学图像探索，不用于诊断；单文件不代表完整临床序列，不做配准、三维重建或空间方向推断。",
    "不返回患者、机构、日期、UID 或任意文本标签；文件声明与用户确认不保证像素真正脱敏，本工具不是脱敏工具。",
    "返回原始存储整数；浏览器仅为显示应用声明的线性 rescale、LINEAR 窗宽窗位及 MONOCHROME1 反转，不修改源数据。",
]
UNITS = ("HU", "OD", "US", "MGML", "Z_EFF", "ED", "EDW", "HU_MOD", "PCT", "unspecified")
SOPS = ("CT", "MR", "SC", "SC-multiframe-8", "SC-multiframe-16")
PRIVACY = "source-declarations-and-user-confirmation-only"

def require(value):
    if not value: raise ValueError(ERROR)
def integer(v, lo, hi): return type(v) is int and lo <= v <= hi
def finite(v, bound): return type(v) in (int, float) and -bound <= v <= bound and math.isfinite(v)
def keys(v, names): return type(v) is dict and set(v) == set(names)
def dtype(image): return ("int" if image["pixel_representation"] else "uint") + str(image["bits_allocated"])
def stored_bounds(image):
    bits, signed = image["bits_stored"], image["pixel_representation"]
    return (-(2**(bits-1)), 2**(bits-1)-1) if signed else (0, 2**bits-1)

def validate_dicom_window_options(kind, options):
    require(type(kind) is str and kind in ("tree", "image") and type(options) is dict)
    if kind == "tree": require(not options)
    else:
        require(keys(options, ("frame", "roi", "confirm_deidentified")) and options["confirm_deidentified"] is True
                and integer(options["frame"], 0, 4095) and type(options["roi"]) is list and len(options["roi"]) == 4
                and all(integer(v, 0, 65534) for v in options["roi"][:2]) and all(integer(v, 1, 128) for v in options["roi"][2:]))
    return dict(options)

def validate_image(image):
    require(keys(image, ("rows", "columns", "frames", "bits_allocated", "bits_stored", "pixel_representation", "photometric", "sop_class", "rescale", "window", "padding")))
    require(integer(image["rows"],1,65535) and integer(image["columns"],1,65535) and integer(image["frames"],1,4096)
            and type(image["bits_allocated"]) is int and image["bits_allocated"] in (8,16)
            and integer(image["bits_stored"],1,image["bits_allocated"]) and integer(image["pixel_representation"],0,1)
            and image["photometric"] in ("MONOCHROME1","MONOCHROME2") and image["sop_class"] in SOPS)
    if image["sop_class"] in ("CT","MR","SC"): require(image["frames"] == 1)
    if image["sop_class"].startswith("SC-multiframe"):
        require(image["pixel_representation"] == 0 and image["bits_allocated"] == (8 if image["sop_class"].endswith("8") else 16))
    rescale = image["rescale"]
    require(keys(rescale,("slope","intercept","unit","declared")) and finite(rescale["slope"],1e6) and rescale["slope"] != 0
            and finite(rescale["intercept"],1e9) and rescale["unit"] in UNITS and type(rescale["declared"]) is bool)
    if not rescale["declared"]: require(rescale == {"slope":1,"intercept":0,"unit":"unspecified","declared":False})
    lo, hi = stored_bounds(image)
    require(all(finite(v*rescale["slope"]+rescale["intercept"],1e12) for v in (lo,hi)))
    window = image["window"]
    require(window is None or keys(window,("center","width","function")) and finite(window["center"],1e12)
            and finite(window["width"],2e12) and window["width"] >= 1 and window["function"] == "LINEAR")
    padding = image["padding"]
    require(padding is None or keys(padding,("low","high")) and integer(padding["low"],lo,hi) and integer(padding["high"],padding["low"],hi))
    return image

def validate_dicom_window_payload(value, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None, size=None, limit=MAX_OUTPUT):
    try:
        require(type(value) is dict and value.get("kind") in ("tree","image"))
        view=value["kind"]
        require(keys(value,("contract_version","type","reader","kind","media_type","choices","selected","metadata","warnings","sampled","tree" if view=="tree" else "array"))
                and integer(value["contract_version"],2,2) and value["type"]==value["reader"]=="dicom-window"
                and value["media_type"]=="application/json" and value["sampled"] is False and value["warnings"]==WARNINGS)
        require(kind is None or view==kind)
        require(fmt is None or type(fmt) is str and fmt in ("dcm","dicom"))
        selected=validate_dicom_window_options(view,value["selected"])
        require(options is None or selected==validate_dicom_window_options(view,options))
        require(keys(value["choices"],("image",))); image=validate_image(value["choices"]["image"])
        meta=value["metadata"]
        require(keys(meta,("format","transfer_syntax","input_mode","source_bytes","read_bytes","read_requests","header_bytes","pixel_bytes","padding_pixels","metadata_hidden","privacy","value_semantics","limits")))
        require(meta["format"]=="dicom" and meta["transfer_syntax"] in ("implicit-little","explicit-little") and meta["input_mode"]=="window"
                and meta["metadata_hidden"] is True and meta["privacy"]==PRIVACY and meta["value_semantics"]=="raw-stored-integers"
                and keys(meta["limits"],LIMITS) and all(integer(v,LIMITS[k],LIMITS[k]) for k,v in meta["limits"].items()))
        for k,lo,hi,expected in (("source_bytes",152,MAX_SOURCE,source_bytes), ("read_bytes",132,MAX_TOTAL,read_bytes),("read_requests",1,MAX_READS,read_requests),("header_bytes",152,1048576,None),("pixel_bytes",1,4294967294,None),("padding_pixels",0,16384,None)):
            require(integer(meta[k],lo,hi) and (expected is None or type(expected) is int and meta[k]==expected))
        require(size is None or type(size) is int and meta["source_bytes"]==size)
        expected_pixels=image["rows"]*image["columns"]*image["frames"]*(image["bits_allocated"]//8)
        require(meta["pixel_bytes"]==expected_pixels and meta["source_bytes"]==meta["header_bytes"]+expected_pixels+(expected_pixels%2)
                and meta["read_bytes"]<=meta["read_requests"]*MAX_SINGLE)
        if view=="tree":
            require(value["tree"]==[{"path":"/pixels","node_type":"array","shape":[image["frames"],image["rows"],image["columns"]],"dtype":dtype(image)}]
                    and all(type(v) is int for v in value["tree"][0]["shape"]) and meta["padding_pixels"]==0 and meta["read_bytes"]<=meta["header_bytes"])
        else:
            x,y,w,h=selected["roi"]
            require(selected["frame"]<image["frames"] and x+w<=image["columns"] and y+h<=image["rows"])
            array=value["array"]; require(keys(array,("shape","dimensions","dtype","values")) and type(array["shape"]) is list and all(type(v) is int for v in array["shape"]) and array["shape"]==[h,w]
                and array["dimensions"]==["row","column"] and array["dtype"]==dtype(image) and type(array["values"]) is list and len(array["values"])==w*h)
            lo,hi=stored_bounds(image); require(all(integer(v,lo,hi) for v in array["values"]))
            padding=image["padding"]; expected_pad=sum(padding["low"]<=v<=padding["high"] for v in array["values"]) if padding else 0
            require(meta["padding_pixels"]==expected_pad and meta["read_bytes"]>=132+w*h*(image["bits_allocated"]//8))
        require(integer(limit,1,MAX_OUTPUT) and len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())<=limit)
        return value
    except (ValueError,TypeError,KeyError,IndexError,OverflowError): raise ValueError(ERROR) from None
