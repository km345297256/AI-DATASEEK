"""Strict inert diffraction/scattering contract. No source or scientific imports."""
from __future__ import annotations
import json
import math

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
LIMITS = {"max_scans": 32, "max_points": 16384, "max_total_points": 65536}
FORMATS = {"xrdml", "xml"}
DIALECTS = {"xrdml-1d", "cansas1d-1.0", "cansas1d-1.1"}
UNCERTAINTY = "declared uncertainty; distribution unspecified"
SEMANTICS = "declared axes and stored intensity; no unit conversion, counting-time normalization, background subtraction or uncertainty inference"
WARNINGS = ["仅显示声明坐标与原始存储强度；不换算单位、不按计数时间归一化、不扣背景。",
            "误差线仅来自文件显式声明，不推断标准差；不进行结构求解、Rietveld 精修或二维方位积分。",
            "本插件有界读取整个 XML；目录解析全部受限扫描，不是范围读取，选择后才绘制单扫描。"]
ERROR = "衍射或散射数据不符合受控格式、扫描选择或读取预算。"

class DiffractionError(ValueError): pass
def require(value):
    if not value: raise DiffractionError(ERROR)
def integer(value, low, high): return type(value) is int and low <= value <= high
def number(value): return type(value) in (int, float) and -1e150 <= value <= 1e150 and math.isfinite(value)
def keys(value, fields): return isinstance(value, dict) and set(value) == set(fields)

def validate_diffraction_options(kind, options):
    require(kind in ("tree", "series") and isinstance(options, dict))
    require(not options if kind == "tree" else keys(options, {"scan"}) and integer(options["scan"], 0, 31))
    return dict(options)

def validate_scan(scan, index, dialect):
    require(keys(scan, {"id", "label", "points", "x_quantity", "x_unit", "y_quantity", "y_unit", "x_error", "y_error", "axis_mode"}))
    require(integer(scan["id"], 0, 31) and scan["id"] == index and scan["label"] == "Scan " + str(index + 1)
            and integer(scan["points"], 2, 16384) and scan["axis_mode"] in ("linear-declared", "explicit")
            and scan["x_error"] in (None, UNCERTAINTY) and scan["y_error"] in (None, UNCERTAINTY))
    if dialect == "xrdml-1d":
        require(scan["x_quantity"] in ("2Theta", "Omega") and scan["x_unit"] == "deg"
                and scan["y_quantity"] in ("counts", "intensities") and scan["y_unit"] == "counts"
                and scan["x_error"] is None and scan["y_error"] is None)
    else:
        require(scan["x_quantity"] == "Q" and scan["x_unit"] in ("1/A", "1/nm", "1/cm", "1/m")
                and scan["y_quantity"] == "I" and scan["y_unit"] in ("1/cm", "1/mm", "1/m", "a.u.", "none", "counts", "cps", "counts/s", "counts/sec")
                and scan["axis_mode"] == "explicit")

def validate_diffraction_payload(result, *, kind=None, options=None, fmt=None, size=None, limit=MAX_OUTPUT):
    require(isinstance(result, dict) and result.get("kind") in ("tree", "series"))
    view = result["kind"]
    require(keys(result, {"contract_version", "type", "reader", "kind", "media_type", "choices", "selected", "metadata", "warnings", "sampled", view})
            and type(result["contract_version"]) is int and result["contract_version"] == 2
            and result["type"] == result["reader"] == "diffraction" and result["media_type"] == "application/json"
            and result["warnings"] == WARNINGS and result["sampled"] is False and (kind is None or kind == view))
    selected = validate_diffraction_options(view, result["selected"])
    require(options is None or selected == validate_diffraction_options(view, options))
    m = result["metadata"]
    require(keys(m, {"format", "dialect", "input_mode", "source_bytes", "scan_count", "total_points", "returned_points", "limits", "value_semantics"})
            and isinstance(m["format"], str) and m["format"] in FORMATS
            and isinstance(m["dialect"], str) and m["dialect"] in DIALECTS
            and (m["format"] == "xrdml") == (m["dialect"] == "xrdml-1d")
            and m["input_mode"] == "whole" and integer(m["source_bytes"], 1, MAX_INPUT)
            and integer(m["scan_count"], 1, 32) and integer(m["total_points"], 2, 65536)
            and integer(m["returned_points"], 0, 16384) and m["value_semantics"] == SEMANTICS
            and keys(m["limits"], LIMITS) and all(type(v) is int and v == LIMITS[k] for k, v in m["limits"].items()))
    require(fmt is None or m["format"] == fmt)
    require(size is None or type(size) is int and m["source_bytes"] == size)
    require(keys(result["choices"], {"scans"}) and isinstance(result["choices"]["scans"], list))
    scans = result["choices"]["scans"]
    require(len(scans) == m["scan_count"])
    for index, scan in enumerate(scans): validate_scan(scan, index, m["dialect"])
    require(sum(s["points"] for s in scans) == m["total_points"])
    if view == "tree":
        require(m["returned_points"] == 0 and result["tree"] == [{"path": "/scan-" + str(s["id"]), "node_type": "scan", "attributes": {"label": s["label"]}} for s in scans])
    else:
        require(selected["scan"] < len(scans))
        scan = scans[selected["scan"]]
        require(m["returned_points"] == scan["points"] and isinstance(result["series"], list) and len(result["series"]) == 1)
        trace = result["series"][0]
        require(keys(trace, {"scan", "x", "y", "x_error", "y_error"}) and integer(trace["scan"], 0, 31) and trace["scan"] == scan["id"])
        for axis in ("x", "y"):
            values = trace[axis]
            require(isinstance(values, list) and len(values) == scan["points"] and all(number(v) for v in values))
            error = trace[axis + "_error"]
            if scan[axis + "_error"] is None: require(error is None)
            else: require(isinstance(error, list) and len(error) == len(values) and all(number(v) and v >= 0 for v in error))
        x = trace["x"]
        require(all(x[i] > x[i - 1] for i in range(1, len(x))) or all(x[i] < x[i - 1] for i in range(1, len(x))))
        if m["dialect"].startswith("cansas"): require(all(v >= 0 for v in x))
    require(integer(limit, 1, MAX_OUTPUT))
    try: require(len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) <= limit)
    except (ValueError, TypeError, UnicodeError): raise DiffractionError(ERROR) from None
    return result
