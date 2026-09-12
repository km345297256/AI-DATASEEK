"""Strict inert FCS window contract shared as identical source with backend."""
from __future__ import annotations
import json
import math
import re
import unicodedata

MAX_SOURCE = 8 * 1024**3
MAX_TOTAL = 8 * 1024**2
MAX_OUTPUT = 2 * 1024**2
LIMITS = {"max_text_bytes": 262144, "max_channels": 128, "max_events": 8192, "max_values": 16384,
          "max_decoded_bytes": 131072, "max_histogram_bins": 128}
SEMANTICS = "stored values; no bit mask, gain, antilog, calibration, timestep conversion, compensation, gating or sampling"
READ_SEMANTICS = "event-interleaved row window read; only selected channels decoded; not whole-file validation"
WARNINGS = ["仅显示明确选择的事件窗；不补偿、不门控、不抽样、不作对数或标定变换。",
            "图中为存储原值；PnE、PnG、TIMESTEP、标定单位和补偿声明仅展示。直方图只统计当前窗口。"]
DECLARATIONS = ["$SPILLOVER", "$COMP", "SPILL", "SPILLOVER"]


class FcsWindowError(ValueError): pass
def fail(): raise FcsWindowError("该 FCS 布局、参数或窗口不在当前安全支持范围，或读取预算不足。")
def integer(v, maximum=MAX_SOURCE, minimum=0): return type(v) is int and minimum <= v <= maximum
def finite(v, minimum=-1.7976931348623157e308, maximum=1.7976931348623157e308):
    return type(v) in (int, float) and minimum <= v <= maximum and math.isfinite(v)
def keys(v, names): return isinstance(v, dict) and set(v) == set(names)
def safe_text(v, limit=128):
    return (isinstance(v, str) and 0 < len(v) <= limit and len(v.encode("utf-8", errors="replace")) <= 4*limit
        and not any(unicodedata.category(c).startswith("C") or c in "<>\\&" for c in v)
        and not v.startswith(("/", "~")) and "://" not in v and "../" not in v)


def validate_fcs_window_options(kind, options):
    if kind not in ("tree", "series") or not isinstance(options, dict): fail()
    if kind == "tree":
        if options: fail()
        return {}
    if options.get("view") not in ("scatter", "histogram"): fail()
    names = {"view", "channels", "event_offset", "event_count"} | ({"bins"} if options["view"] == "histogram" else set())
    count = 2 if options["view"] == "scatter" else 1
    if (not keys(options, names) or not isinstance(options["channels"], list) or len(options["channels"]) != count
        or any(not integer(c, 127) for c in options["channels"]) or len(set(options["channels"])) != count
        or not integer(options["event_offset"], MAX_SOURCE-1) or not integer(options["event_count"], 8192, 1)
        or options["view"] == "histogram" and not integer(options["bins"], 128, 1)): fail()
    return {**options, "channels": list(options["channels"])}


def validate_channel(c, index, datatype):
    if (not keys(c, {"id", "name", "stain", "bits", "range", "exponent", "gain", "calibration", "display"})
        or not integer(c["id"], 127) or c["id"] != index or not safe_text(c["name"]) or "," in c["name"]
        or c["stain"] is not None and not safe_text(c["stain"])
        or not integer(c["bits"], 64, 1)
        or not isinstance(c["exponent"], list) or len(c["exponent"]) != 2 or any(not finite(v, 0, 1e12) for v in c["exponent"])
        or not (c["exponent"] == [0, 0] or all(v > 0 for v in c["exponent"]))
        or c["gain"] is not None and not finite(c["gain"], 1e-300, 1e12)): fail()
    if datatype == "I":
        if c["bits"] not in (8, 16, 32) or not integer(c["range"], 2**32, 1) or c["range"] != 2**c["bits"]: fail()
    elif c["bits"] != (32 if datatype == "F" else 64) or not finite(c["range"], 1e-300) or c["exponent"] != [0, 0]: fail()
    if c["exponent"] != [0, 0] and c["gain"] not in (None, 1): fail()
    cal = c["calibration"]
    if cal is not None and (not keys(cal, {"factor", "unit"}) or not finite(cal["factor"], 1e-300, 1e12) or not safe_text(cal["unit"], 64)): fail()
    d = c["display"]
    if d is not None:
        if not keys(d, {"scale", "values"}) or d["scale"] not in ("Linear", "Logarithmic") or not isinstance(d["values"], list) or len(d["values"]) != 2 or any(not finite(v, -1e12, 1e12) for v in d["values"]): fail()
        if (d["scale"] == "Linear" and d["values"][1] <= d["values"][0]) or (d["scale"] == "Logarithmic" and min(d["values"]) <= 0): fail()


def validate_fcs_window_payload(result, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None):
    if not isinstance(result, dict) or result.get("kind") not in ("tree", "series"): fail()
    view = result["kind"]
    if (not keys(result, {"contract_version", "type", "reader", "kind", "media_type", "choices", "selected", "metadata", "warnings", "sampled", view})
        or type(result["contract_version"]) is not int or result["contract_version"] != 2 or result["type"] != "fcs-window"
        or result["reader"] != "fcs-window" or result["media_type"] != "application/json" or result["warnings"] != WARNINGS
        or type(result["sampled"]) is not bool or kind is not None and kind != view): fail()
    selection = validate_fcs_window_options(view, result["selected"])
    if options is not None and selection != validate_fcs_window_options(view, options): fail()
    m = result["metadata"]
    if (not keys(m, {"format", "fcs_version", "datatype", "byte_order", "input_mode", "source_bytes", "read_bytes", "read_requests",
        "text_bytes", "total_events", "channel_count", "event_bytes", "scanned_event_bytes", "decoded_values", "decoded_bytes",
        "nonfinite_values", "plottable_events", "timestep", "compensation", "value_semantics", "read_semantics", "limits"})
        or m["format"] != "fcs" or fmt is not None and fmt != "fcs" or m["fcs_version"] not in ("3.0", "3.1") or m["datatype"] not in ("I", "F", "D")
        or m["byte_order"] not in ("little", "big") or m["input_mode"] != "window" or not integer(m["source_bytes"], MAX_SOURCE, 59)
        or not integer(m["read_bytes"], MAX_TOTAL, 1) or not integer(m["read_requests"], 128, 1) or m["read_bytes"] < m["read_requests"]
        or not integer(m["text_bytes"], 262144, 1) or not integer(m["total_events"], MAX_SOURCE, 1) or not integer(m["channel_count"], 128, 1)
        or not integer(m["event_bytes"], 1024, 1) or m["total_events"]*m["event_bytes"]+58+m["text_bytes"] > m["source_bytes"]
        or not integer(m["scanned_event_bytes"], MAX_TOTAL) or not integer(m["decoded_values"], 16384)
        or not integer(m["decoded_bytes"], 131072) or not integer(m["nonfinite_values"], 16384) or not integer(m["plottable_events"], 8192)
        or m["read_bytes"] < 58+m["text_bytes"]+m["scanned_event_bytes"]
        or m["timestep"] is not None and not finite(m["timestep"], 1e-300, 1e12)
        or m["value_semantics"] != SEMANTICS or m["read_semantics"] != READ_SEMANTICS or not keys(m["limits"], LIMITS)
        or any(type(v) is not int or v != LIMITS[k] for k, v in m["limits"].items())): fail()
    for key, expected in {"source_bytes": source_bytes, "read_bytes": read_bytes, "read_requests": read_requests}.items():
        if expected is not None and (type(expected) is not int or m[key] != expected): fail()
    if not keys(result["choices"], {"channels"}) or not isinstance(result["choices"]["channels"], list) or len(result["choices"]["channels"]) != m["channel_count"]: fail()
    channels = result["choices"]["channels"]
    for index, channel in enumerate(channels): validate_channel(channel, index, m["datatype"])
    if len({c["name"] for c in channels}) != len(channels) or len({c["bits"] for c in channels}) != 1 or m["event_bytes"] != len(channels)*channels[0]["bits"]//8: fail()
    compensation = m["compensation"]
    if not keys(compensation, {"declarations", "spillover"}) or not isinstance(compensation["declarations"], list) or compensation["declarations"] != [v for v in DECLARATIONS if v in compensation["declarations"]]: fail()
    spill = compensation["spillover"]
    if (spill is not None) != ("$SPILLOVER" in compensation["declarations"]): fail()
    if spill is not None:
        if (not keys(spill, {"channels", "matrix"}) or not isinstance(spill["channels"], list) or not 2 <= len(spill["channels"]) <= len(channels)
            or any(not integer(c, len(channels)-1) for c in spill["channels"]) or len(set(spill["channels"])) != len(spill["channels"])
            or not isinstance(spill["matrix"], list) or len(spill["matrix"]) != len(spill["channels"])
            or any(not isinstance(row, list) or len(row) != len(spill["channels"]) or any(not finite(v, -1e6, 1e6) for v in row) for row in spill["matrix"])): fail()
    if view == "tree":
        expected_tree = [{"path": "/channel-"+str(c["id"]), "node_type": "channel", "attributes": {"name": c["name"], "bits": c["bits"]}} for c in channels]
        if result["tree"] != expected_tree or result["sampled"] or any(m[k] for k in ("scanned_event_bytes", "decoded_values", "decoded_bytes", "nonfinite_values", "plottable_events")): fail()
    else:
        offset, count, ids = selection["event_offset"], selection["event_count"], selection["channels"]
        if offset+count > m["total_events"] or any(c >= len(channels) for c in ids) or not isinstance(result["series"], list) or len(result["series"]) != len(ids): fail()
        nonfinite = 0
        for i, trace in enumerate(result["series"]):
            c = channels[ids[i]]
            if (not keys(trace, {"channel", "label", "x", "y"}) or not integer(trace["channel"], 127) or trace["channel"] != c["id"] or trace["label"] != c["name"]
                or not isinstance(trace["x"], list) or not isinstance(trace["y"], list) or len(trace["x"]) != count or len(trace["y"]) != count
                or any(not integer(v, MAX_SOURCE-1) or v != offset+j for j, v in enumerate(trace["x"]))): fail()
            for value in trace["y"]:
                if value is None:
                    if m["datatype"] == "I": fail()
                    nonfinite += 1
                elif m["datatype"] == "I":
                    if not integer(value, c["range"]-1): fail()
                elif not finite(value, -3.4028234663852886e38 if m["datatype"] == "F" else -1.7976931348623157e308, 3.4028234663852886e38 if m["datatype"] == "F" else 1.7976931348623157e308): fail()
        plottable = sum(all(trace["y"][i] is not None for trace in result["series"]) for i in range(count))
        if (m["scanned_event_bytes"] != count*m["event_bytes"] or m["decoded_values"] != count*len(ids)
            or m["decoded_bytes"] != count*len(ids)*channels[0]["bits"]//8 or m["nonfinite_values"] != nonfinite
            or m["plottable_events"] != plottable or result["sampled"] != (count < m["total_events"])): fail()
    try:
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT: fail()
    except (ValueError, TypeError, UnicodeError): fail()
    return result
