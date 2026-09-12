"""Strict inert seismic window contract; no numerical libraries or source access."""
from __future__ import annotations
import datetime as dt
import json
import math
import re

MAX_SOURCE = 8 * 1024**3
MAX_TOTAL = 8 * 1024**2
MAX_OUTPUT = 2 * 1024**2
FORMATS = {"mseed", "miniseed", "sac"}
ENCODINGS = {"INT16", "INT32", "FLOAT32", "FLOAT64", "STEIM1", "STEIM2"}
VALUE_SEMANTICS = "raw stored samples; SAC SCALE and instrument response not applied; no filtering, resampling, merging or gap filling"
TIME_AXIS = "seconds relative to the selected record's first sample; UTC display rounded to microseconds"
WARNINGS = ["只显示单条记录的显式样本窗；不拼接、不补缺口、不重采样、不校正仪器响应。",
            "单位只来自明确文件声明；MiniSEED 不含响应单位，SAC SCALE 不应用。目录连续性仅比较本页同通道记录。"]
LIMITS = {"max_samples": 16384, "max_page_records": 16, "max_record_bytes": 1048576,
          "max_decoded_samples": 65535, "max_decoded_bytes": 524280}
RELATIONS = {"uncompared", "continuous", "gap", "overlap", "rate-change"}

class SeismicWindowError(ValueError): pass
def fail(): raise SeismicWindowError("地震波形格式、记录窗口或读取预算无效。")
def integer(value, maximum=2**31-1, minimum=0):
    return type(value) is int and minimum <= value <= maximum
def finite(value, minimum=-1e12, maximum=1e12):
    return type(value) in (int, float) and minimum <= value <= maximum and math.isfinite(value)
def keys(value, expected): return isinstance(value, dict) and set(value) == set(expected)

def validate_seismic_window_options(kind, options):
    if kind not in ("tree", "series") or not isinstance(options, dict): fail()
    if kind == "tree":
        if not options: return {}
        if not keys(options, {"record_offset", "record_limit"}) or not integer(options["record_offset"], 33554431) or not integer(options["record_limit"], 16, 1): fail()
    elif (not keys(options, {"record", "start_sample", "sample_count"}) or not integer(options["record"], 33554431)
          or not integer(options["start_sample"]) or not integer(options["sample_count"], 16384, 1)): fail()
    return dict(options)

def _date(value):
    if value is None: return True
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z", value): return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return 1900 <= parsed.year <= 2200
    except ValueError: return False

def validate_seismic_window_payload(result, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None):
    if not isinstance(result, dict) or result.get("kind") not in ("tree", "series"): fail()
    view = result["kind"]
    if (not keys(result, {"contract_version", "type", "reader", "kind", "media_type", "choices", "selected", "metadata", "warnings", "sampled", view})
        or type(result["contract_version"]) is not int or result["contract_version"] != 2
        or result["type"] != "seismic-window" or result["reader"] != "seismic-window" or result["media_type"] != "application/json"
        or result["warnings"] != WARNINGS or type(result["sampled"]) is not bool or kind is not None and kind != view): fail()
    selected = validate_seismic_window_options(view, result["selected"])
    if options is not None and selected != validate_seismic_window_options(view, options): fail()
    m = result["metadata"]
    if (not keys(m, {"format", "input_mode", "source_bytes", "read_bytes", "read_requests", "record_bytes", "record_slots", "catalog_offset", "catalog_count", "catalog_complete", "decoded_samples", "decoded_bytes", "nonfinite_values", "value_semantics", "time_axis", "limits"})
        or not isinstance(m["format"], str) or m["format"] not in FORMATS or m["input_mode"] != "window"
        or not integer(m["source_bytes"], MAX_SOURCE, 256) or not integer(m["read_bytes"], MAX_TOTAL, 1) or not integer(m["read_requests"], 128, 1)
        or m["read_bytes"] < m["read_requests"] or not integer(m["record_bytes"], MAX_SOURCE, 256) or not integer(m["record_slots"], 33554432, 1)
        or m["record_bytes"] * m["record_slots"] != m["source_bytes"]
        or not integer(m["catalog_offset"], m["record_slots"]-1) or not integer(m["catalog_count"], min(16, m["record_slots"]-m["catalog_offset"]), 1)
        or type(m["catalog_complete"]) is not bool or m["catalog_complete"] != (m["catalog_offset"] == 0 and m["catalog_count"] == m["record_slots"])
        or not integer(m["decoded_samples"], 65535) or not integer(m["decoded_bytes"], 524280) or not integer(m["nonfinite_values"], 16384)
        or m["value_semantics"] != VALUE_SEMANTICS or m["time_axis"] != TIME_AXIS
        or not keys(m["limits"], LIMITS) or any(type(v) is not int or v != LIMITS[k] for k, v in m["limits"].items())): fail()
    if m["format"] != "sac" and (m["record_bytes"] > 1048576 or m["record_bytes"] & (m["record_bytes"]-1)): fail()
    if m["format"] == "sac" and m["record_slots"] != 1: fail()
    for key, expected in {"source_bytes": source_bytes, "read_bytes": read_bytes, "read_requests": read_requests}.items():
        if expected is not None and (type(expected) is not int or m[key] != expected): fail()
    if fmt is not None and m["format"] != fmt: fail()
    if not keys(result["choices"], {"records"}) or not isinstance(result["choices"]["records"], list) or len(result["choices"]["records"]) != m["catalog_count"]: fail()
    records = result["choices"]["records"]
    for index, r in enumerate(records):
        if (not keys(r, {"id", "label", "variant", "encoding", "byte_order", "samples", "sample_interval", "unit", "unit_source", "declared_scale", "start_time", "begin_seconds", "time_adjustment_seconds", "timing_quality", "quality_flags", "relation", "gap_seconds"})
            or not integer(r["id"], m["record_slots"]-1) or r["id"] != m["catalog_offset"]+index
            or not isinstance(r["label"], str) or not re.fullmatch(r"[A-Za-z0-9_.?-]{1,40}", r["label"])
            or r["variant"] not in ("MiniSEED2", "SAC6", "SAC7") or not isinstance(r["encoding"], str) or r["encoding"] not in ENCODINGS
            or r["byte_order"] not in ("big", "little") or not integer(r["samples"], 2**31-1 if m["format"] == "sac" else 65535, 1)
            or not finite(r["sample_interval"], 1e-7, 1e6) or not isinstance(r["unit"], str) or r["unit"] not in {"unknown", "nm", "nm/s", "nm/s^2", "V"}
            or r["unit_source"] not in ("not supplied", "SAC IDEP") or r["declared_scale"] is not None and not finite(r["declared_scale"])
            or not _date(r["start_time"]) or not finite(r["begin_seconds"]) or not finite(r["time_adjustment_seconds"], -214749, 214749)
            or r["timing_quality"] is not None and not integer(r["timing_quality"], 100) or not integer(r["quality_flags"], 255)
            or not isinstance(r["relation"], str) or r["relation"] not in RELATIONS or r["gap_seconds"] is not None and not finite(r["gap_seconds"])
            or (r["relation"] == "uncompared") != (r["gap_seconds"] is None)): fail()
        if m["format"] == "sac":
            if r["variant"] not in ("SAC6", "SAC7") or r["encoding"] != "FLOAT32" or r["time_adjustment_seconds"] != 0 or r["timing_quality"] is not None or r["quality_flags"] != 0 or r["unit_source"] != "SAC IDEP": fail()
            if 632 + r["samples"]*4 + (176 if r["variant"] == "SAC7" else 0) != m["source_bytes"]: fail()
        elif r["variant"] != "MiniSEED2" or r["unit"] != "unknown" or r["unit_source"] != "not supplied" or r["declared_scale"] is not None or r["start_time"] is None or r["begin_seconds"] != 0: fail()
    if view == "tree":
        offset, limit = selected.get("record_offset", 0), selected.get("record_limit", 16)
        if m["catalog_offset"] != offset or m["catalog_count"] != min(limit, m["record_slots"]-offset): fail()
        expected = [{"path": "/record-"+str(r["id"]), "node_type": "record", "attributes": {"label": r["label"], "encoding": r["encoding"]}} for r in records]
        if result["tree"] != expected or result["sampled"] or any(m[k] for k in ("decoded_samples", "decoded_bytes", "nonfinite_values")): fail()
    else:
        if m["catalog_count"] != 1 or m["catalog_offset"] != selected["record"]: fail()
        r = records[0]; start, count = selected["start_sample"], selected["sample_count"]
        if start + count > r["samples"] or r["relation"] != "uncompared" or r["gap_seconds"] is not None: fail()
        if not isinstance(result["series"], list) or len(result["series"]) != 1: fail()
        trace = result["series"][0]
        if (not keys(trace, {"record", "label", "unit", "x", "y"}) or not integer(trace["record"]) or trace["record"] != r["id"] or trace["label"] != r["label"] or trace["unit"] != r["unit"]
            or not isinstance(trace["x"], list) or not isinstance(trace["y"], list) or len(trace["x"]) != count or len(trace["y"]) != count): fail()
        for i, x in enumerate(trace["x"]):
            if not finite(x, 0, 2**31 * 1e6) or x != (start+i)*r["sample_interval"]: fail()
        nonfinite = 0
        for y in trace["y"]:
            if y is None:
                if r["encoding"] not in ("FLOAT32", "FLOAT64"): fail()
                nonfinite += 1
            elif r["encoding"] in ("INT16", "INT32", "STEIM1", "STEIM2"):
                bits = 16 if r["encoding"] == "INT16" else 32
                if not integer(y, 2**(bits-1)-1, -(2**(bits-1))): fail()
            elif not finite(y, -1.7976931348623157e308, 1.7976931348623157e308): fail()
        decoded = r["samples"] if r["encoding"].startswith("STEIM") else count
        width = {"INT16": 2, "INT32": 4, "FLOAT32": 4, "FLOAT64": 8, "STEIM1": 4, "STEIM2": 4}[r["encoding"]]
        if m["decoded_samples"] != decoded or m["decoded_bytes"] != decoded*width or m["nonfinite_values"] != nonfinite or result["sampled"] != (count != r["samples"] or m["record_slots"] > 1): fail()
    try:
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT: fail()
    except (ValueError, TypeError, UnicodeError): fail()
    return result
