"""Pure contract for the isolated FITS/TIFF scientific-image workbench."""
from __future__ import annotations
import base64
import json
import math
import re
import struct

ERROR = "科学影像格式、选择或结果超出安全预览范围。"
WARNING = "读取有界原始文件并应用文件声明的标度；显示拉伸不修改数据。源检测仅为局部峰候选，不是天体分类或测光。无公网底图。"
LIMITS = {"source_bytes": 33554432, "decoded_bytes": 134217728, "plane_values": 2097152, "datasets": 64, "render_side": 1024, "table_rows": 50, "table_columns": 32, "spectrum_points": 2000, "sources": 2000}
FORMATS = {"fit", "fits", "fts", "fz", "fits.gz", "tif", "tiff"}

def need(value):
    if not value: raise ValueError(ERROR)

def integer(value, low, high): return type(value) is int and low <= value <= high
def finite(value): return type(value) in {int, float} and math.isfinite(value) and abs(value) <= 1e100
def keys(value, names): return isinstance(value, dict) and set(value) == set(names.split())
def label(value): return isinstance(value, str) and len(value) <= 128 and not re.search(r"[<>\x00-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", value, re.I)
def number(value): return value is None or finite(value)

def validate_astronomy_workbench_options(kind, options):
    need(kind in {"tree", "image", "table", "series"} and isinstance(options, dict))
    if kind == "tree": need(not options); return {}
    need(integer(options.get("dataset"), 0, 63))
    if kind == "series": need(keys(options, "dataset"))
    elif kind == "table":
        need(keys(options, "dataset row_offset column_offset") and integer(options["row_offset"], 0, 2**31-1) and integer(options["column_offset"], 0, 255))
    else:
        action = options.get("action")
        extra = {"render": "stretch interval low high colour_map invert", "pixel": "x y", "region": "bounds", "sources": "threshold_sigma"}
        need(action in extra and keys(options, "dataset slices band action " + extra[action]))
        need(isinstance(options["slices"], list) and len(options["slices"]) <= 6 and all(integer(v, 0, 2**31-1) for v in options["slices"]) and integer(options["band"], 0, 16))
        if action == "render":
            need(options["stretch"] in {"linear", "log", "sqrt", "asinh"} and options["interval"] in {"zscale", "percentile", "manual"} and options["colour_map"] in {"gray", "viridis", "heat", "cool"} and type(options["invert"]) is bool)
            if options["interval"] == "manual": need(finite(options["low"]) and finite(options["high"]) and options["high"] > options["low"])
            else: need(options["low"] is None and options["high"] is None)
        elif action == "pixel": need(integer(options["x"], 0, 2**31-1) and integer(options["y"], 0, 2**31-1))
        elif action == "region":
            b = options["bounds"]; need(isinstance(b, list) and len(b) == 4 and all(integer(v, 0, 2**31-1) for v in b) and b[2] > b[0] and b[3] > b[1])
        else: need(finite(options["threshold_sigma"]) and 1 <= options["threshold_sigma"] <= 50)
    return dict(options)

def _world(value): need(value is None or isinstance(value, list) and len(value) == 2 and all(finite(n) for n in value))
def _statistics(value, count):
    need(keys(value, "valid_count missing_count minimum maximum mean median std sum") and integer(value["valid_count"], 0, count) and integer(value["missing_count"], 0, count) and value["valid_count"] + value["missing_count"] == count)
    need(all(number(value[k]) for k in ("minimum", "maximum", "mean", "median", "std", "sum")))
    if value["valid_count"] == 0: need(all(value[k] is None for k in ("minimum", "maximum", "mean", "median", "std", "sum")))
    else: need(all(finite(value[k]) for k in ("minimum", "maximum", "mean", "median", "std", "sum")) and value["minimum"] <= value["maximum"] and value["std"] >= 0)

def validate_astronomy_workbench_payload(value, *, kind=None, options=None, fmt=None, size=None, limit=4194304):
    try:
        need(isinstance(value, dict)); k = value.get("kind"); s = validate_astronomy_workbench_options(k, value.get("selected"))
        need(kind is None or kind == k); need(options is None or s == validate_astronomy_workbench_options(k, options))
        need(keys(value, "contract_version type reader kind media_type choices selected metadata warnings sampled workbench" + (" tree" if k == "tree" else "")))
        need(type(value["contract_version"]) is int and value["contract_version"] == 2 and value["reader"] == value["type"] == "astronomy-workbench" and value["media_type"] == "application/json" and value["warnings"] == [WARNING] and type(value["sampled"]) is bool)
        m = value["metadata"]; need(keys(m, "format input_mode source_bytes unpacked_bytes limits value_semantics"))
        need(m["format"] in {"fits", "tiff"} and (fmt is None or fmt in FORMATS and m["format"] == ("tiff" if fmt in {"tif", "tiff"} else "fits")) and m["input_mode"] == "whole" and integer(m["source_bytes"], 1, LIMITS["source_bytes"]) and (size is None or type(size) is int and size == m["source_bytes"]) and integer(m["unpacked_bytes"], 1, LIMITS["source_bytes"]) and m["limits"] == LIMITS and all(type(n) is int for n in m["limits"].values()) and m["value_semantics"] == "file-declared scaling; zero-based indices; upper-exclusive regions")
        c = value["choices"]; need(keys(c, "datasets geospatial") and isinstance(c["datasets"], list) and 1 <= len(c["datasets"]) <= 64)
        total_columns = 0
        for i, d in enumerate(c["datasets"]):
            need(keys(d, "index name kind shape plane_shape dtype channels columns row_count wcs") and integer(d["index"], i, i) and label(d["name"]) and d["kind"] in {"empty", "image", "table", "spectrum"} and isinstance(d["shape"], list) and len(d["shape"]) <= 8 and all(integer(n, 0, 2**31-1) for n in d["shape"]) and label(d["dtype"]) and integer(d["channels"], 1, 16) and integer(d["row_count"], 0, 2**31-1) and isinstance(d["columns"], list) and len(d["columns"]) <= 256)
            need(isinstance(d["plane_shape"],list) and ((len(d["plane_shape"]) == 2 and all(integer(n,1,2**31-1) for n in d["plane_shape"])) if d["kind"] == "image" else not d["plane_shape"]))
            total_columns += len(d["columns"])
            for ci, col in enumerate(d["columns"]): need(keys(col, "index name format unit") and integer(col["index"], ci, ci) and all(label(col[key]) for key in ("name", "format", "unit")))
            w = d["wcs"]; need(keys(w, "celestial axis_types units") and type(w["celestial"]) is bool and isinstance(w["axis_types"], list) and isinstance(w["units"], list) and len(w["axis_types"]) <= 8 and len(w["units"]) <= 8 and all(label(v) for v in w["axis_types"] + w["units"]))
        need(total_columns <= 256)
        geo = c["geospatial"]
        if geo is not None:
            need(keys(geo, "crs bounds bounds_wgs84 resolution bands nodata") and label(geo["crs"]) and integer(geo["bands"], 1, 16) and number(geo["nodata"]))
            for key, n in (("bounds",4),("resolution",2)) : need(isinstance(geo[key], list) and len(geo[key]) == n and all(finite(x) for x in geo[key]))
            need(geo["bounds_wgs84"] is None or isinstance(geo["bounds_wgs84"], list) and len(geo["bounds_wgs84"]) == 4 and all(finite(x) for x in geo["bounds_wgs84"]))
        w = value["workbench"]
        if k == "tree":
            need(w == {"action": "inspect"} and value["sampled"] is False and value["tree"] == [{"path": "/datasets/"+str(d["index"]), "node_type": "array", "attributes": {"label": d["name"]}} for d in c["datasets"]]); return _budget(value,limit)
        need(s["dataset"] < len(c["datasets"])); d = c["datasets"][s["dataset"]]
        if k == "table":
            need(d["kind"] == "table" and keys(w, "action columns rows row_offset column_offset total_rows total_columns") and w["action"] == "table" and w["row_offset"] == s["row_offset"] and w["column_offset"] == s["column_offset"] and w["total_rows"] == d["row_count"] and w["total_columns"] == len(d["columns"]))
            need(s["row_offset"] <= d["row_count"] and s["column_offset"] <= len(d["columns"]) and w["columns"] == d["columns"][s["column_offset"]:s["column_offset"]+32] and isinstance(w["rows"], list) and len(w["rows"]) == min(50, d["row_count"]-s["row_offset"]))
            for row in w["rows"]:
                need(isinstance(row, list) and len(row) == len(w["columns"]))
                for cell in row: need(_cell(cell))
            need(value["sampled"] == (len(w["rows"]) < d["row_count"] or len(w["columns"]) < len(d["columns"])))
        elif k == "series":
            need(d["kind"] == "spectrum" and len(d["shape"]) == 1 and keys(w, "action indices values stride total_points") and w["action"] == "spectrum" and w["total_points"] == d["shape"][0] and integer(w["stride"], 1, 2**31-1) and w["stride"] == max(1, math.ceil(w["total_points"]/2000)))
            need(w["indices"] == list(range(0,w["total_points"],w["stride"])) and isinstance(w["values"], list) and len(w["values"]) == len(w["indices"]) and all(number(x) for x in w["values"]) and value["sampled"] == (w["stride"] > 1))
        else:
            need(d["kind"] == "image" and w.get("action") == s["action"] and integer(w.get("width"), 1, 2**31-1) and integer(w.get("height"), 1, 2**31-1) and integer(w.get("channels"), 1, 3) and w["channels"] in {1,3} and w["width"]*w["height"]*w["channels"] <= LIMITS["plane_values"])
            width,height,channels = w["width"],w["height"],w["channels"]; action = s["action"]
            need([height,width] == d["plane_shape"])
            if m["format"] == "fits": need(s["band"] == 1 and len(s["slices"]) == len(d["shape"])-2 and all(i < n for i,n in zip(s["slices"],d["shape"][:-2])) and [height,width] == d["shape"][-2:] and channels == 1)
            else: need(not s["slices"] and s["band"] <= d["channels"] and (s["band"] != 0 or d["channels"] >= 3) and channels == (3 if s["band"] == 0 else 1))
            if action == "render":
                need(keys(w, "action width height channels image_base64 render_width render_height display_limits statistics histogram wcs") and integer(w["render_width"], 1, 1024) and integer(w["render_height"], 1, 1024) and w["render_width"] <= width and w["render_height"] <= height and isinstance(w["image_base64"], str) and len(w["image_base64"]) <= 4000000)
                raw = base64.b64decode(w["image_base64"], validate=True); need(raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 33 and struct.unpack(">II",raw[16:24]) == (w["render_width"],w["render_height"]))
                need(isinstance(w["display_limits"],list) and len(w["display_limits"]) == channels and all(isinstance(p,list) and len(p) == 2 and all(finite(x) for x in p) and p[1] > p[0] for p in w["display_limits"]))
                _statistics(w["statistics"],width*height*channels); h = w["histogram"]; need(keys(h,"counts edges sample_count") and isinstance(h["counts"],list) and len(h["counts"]) == 96 and all(integer(x,0,500000) for x in h["counts"]) and isinstance(h["edges"],list) and len(h["edges"]) == 97 and all(finite(x) for x in h["edges"]) and all(a < b for a,b in zip(h["edges"],h["edges"][1:])) and integer(h["sample_count"],0,500000) and sum(h["counts"]) <= h["sample_count"])
                cw = w["wcs"]; need(keys(cw,"corners") and (cw["corners"] is None or isinstance(cw["corners"],list) and len(cw["corners"]) == 4)); [_world(p) for p in (cw["corners"] or [])]
                need(value["sampled"] == (w["render_width"] < width or w["render_height"] < height or h["sample_count"] < w["statistics"]["valid_count"]))
            elif action == "pixel":
                need(keys(w,"action width height channels x y value world") and integer(w["x"],s["x"],s["x"]) and integer(w["y"],s["y"],s["y"]) and s["x"] < width and s["y"] < height)
                need(number(w["value"]) if channels == 1 else isinstance(w["value"],list) and len(w["value"]) == 3 and all(number(v) for v in w["value"])); _world(w["world"]); need(value["sampled"] is False)
            elif action == "region":
                need(keys(w,"action width height channels bounds pixel_count statistics") and w["bounds"] == s["bounds"] and s["bounds"][2] <= width and s["bounds"][3] <= height)
                count = (s["bounds"][2]-s["bounds"][0])*(s["bounds"][3]-s["bounds"][1])*channels; need(integer(w["pixel_count"],count,count)); _statistics(w["statistics"],count); need(value["sampled"] is False)
            else:
                need(keys(w,"action width height channels background noise threshold sources truncated") and all(finite(w[n]) for n in ("background","noise","threshold")) and w["noise"] >= 0 and w["threshold"] == w["background"]+s["threshold_sigma"]*w["noise"] and type(w["truncated"]) is bool and isinstance(w["sources"],list) and len(w["sources"]) <= 2000)
                seen=set()
                for i,p in enumerate(w["sources"]):
                    need(keys(p,"id x y peak snr world") and integer(p["id"],i+1,i+1) and integer(p["x"],0,width-1) and integer(p["y"],0,height-1) and finite(p["peak"]) and finite(p["snr"]) and (p["x"],p["y"]) not in seen); seen.add((p["x"],p["y"])); _world(p["world"])
                need(value["sampled"] == w["truncated"])
        return _budget(value,limit)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, struct.error): raise ValueError(ERROR) from None

def _cell(value): return number(value) or type(value) is bool or label(value) or isinstance(value,list) and len(value) <= 100 and all(number(v) or type(v) is bool or label(v) for v in value)
def _budget(value,limit):
    need(integer(limit,1,4194304) and len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode()) <= limit); return value

def validate_options(kind, options): return validate_astronomy_workbench_options(kind, options)
def validate_payload(payload, *, kind=None, options=None, format=None, source_bytes=None):
    return validate_astronomy_workbench_payload(payload, kind=kind, options=options, fmt=format, size=source_bytes)
