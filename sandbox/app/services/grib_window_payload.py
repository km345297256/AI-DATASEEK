"""Pure GRIB2 result contract; byte-identical worker copy, no native imports."""
from __future__ import annotations

import datetime
import json
import math
import re

MAX_SOURCE = 8 * 1024**3
MAX_READ = 1024**2
MAX_TOTAL = 8 * 1024**2
MAX_READS = 128
MAX_POINTS = 16384
MAX_MESSAGES = 8
FORMATS = {"grib", "grib2", "grb", "grb2"}
WARNING = "按单条 GRIB2 消息解码原始气象场及缺失位图；不合并变量、层或时刻，不额外换算单位、插值、排序经度或请求在线底图。"
SEMANTICS = "ecCodes decoded values; explicit bitmap; longitude [0,360); no extra unit conversion or interpolation"
_ID = re.compile(r"g-[0-9a-f]{16}\Z")
_UNSAFE = re.compile(r"[<>\x00-\x1f\x7f]|/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\", re.I)


class GribWindowError(ValueError):
    pass


def fail():
    raise GribWindowError("GRIB2 消息、网格、编码或读取预算不受支持；请缩小范围或选择其他数据。")


def integer(v, maximum=MAX_SOURCE, minimum=0):
    return type(v) is int and minimum <= v <= maximum


def finite(v, maximum=1.7976931348623157e308):
    return type(v) in {int, float} and abs(v) <= maximum and math.isfinite(v)


def label(v):
    return isinstance(v, str) and 0 < len(v) <= 128 and not _UNSAFE.search(v)


def keys(v, expected):
    return isinstance(v, dict) and set(v) == set(expected.split())


def message_offset(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or int(value[2:], 16) >= MAX_SOURCE:
        fail()
    return int(value[2:], 16)


def validate_grib_window_options(kind, options):
    if not isinstance(kind, str) or kind not in {"tree", "image"} or not isinstance(options, dict): fail()
    if kind == "tree":
        if set(options) - {"offset"} or not integer(options.get("offset", 0), MAX_SOURCE - 1): fail()
        return {"offset": options.get("offset", 0)}
    if not keys(options, "message roi"): fail()
    message_offset(options["message"])
    roi = options["roi"]
    if (not isinstance(roi, list) or len(roi) != 4 or any(not integer(v, MAX_POINTS) for v in roi)
        or not roi[2] or not roi[3] or roi[2] * roi[3] > MAX_POINTS): fail()
    return {"message": options["message"], "roi": list(roi)}


def _validate(result, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None):
    if not isinstance(result, dict) or result.get("kind") not in {"tree", "image"}: fail()
    view = result["kind"]
    if (not keys(result, "contract_version type reader kind media_type choices selected metadata warnings sampled " + ("tree" if view == "tree" else "array axes"))
        or type(result["contract_version"]) is not int or result["contract_version"] != 2
        or result["type"] != "grib-window" or result["reader"] != "grib-window" or result["media_type"] != "application/json"
        or result["warnings"] != [WARNING] or result["sampled"] is not False or kind is not None and kind != view): fail()
    selected = validate_grib_window_options(view, result["selected"])
    if options is not None and selected != validate_grib_window_options(view, options): fail()
    meta = result["metadata"]
    if (not keys(meta, "format edition grid_type packing decoder input_mode value_semantics source_bytes read_bytes read_requests page_offset next_offset scanned_messages skipped_messages decoded_points output_values missing_values")
        or meta["format"] not in FORMATS or fmt is not None and meta["format"] != fmt
        or type(meta["edition"]) is not int or meta["edition"] != 2 or meta["grid_type"] != "regular_ll" or meta["packing"] != "grid_simple"
        or meta["decoder"] != "ecCodes" or meta["input_mode"] != "window" or meta["value_semantics"] != SEMANTICS
        or not integer(meta["source_bytes"], MAX_SOURCE, 20) or not integer(meta["read_bytes"], MAX_TOTAL, 1)
        or not integer(meta["read_requests"], MAX_READS, 1) or meta["read_bytes"] < meta["read_requests"]
        or not integer(meta["page_offset"], meta["source_bytes"] - 1)
        or meta["next_offset"] is not None and not integer(meta["next_offset"], meta["source_bytes"] - 1, meta["page_offset"] + 1)
        or not integer(meta["scanned_messages"], MAX_MESSAGES, 1) or not integer(meta["skipped_messages"], meta["scanned_messages"])
        or any(not integer(meta[k], MAX_POINTS) for k in ("decoded_points", "output_values", "missing_values"))): fail()
    for field, expected in (("source_bytes", source_bytes), ("read_bytes", read_bytes), ("read_requests", read_requests)):
        if expected is not None and (type(expected) is not int or meta[field] != expected): fail()
    if not keys(result["choices"], "messages") or not isinstance(result["choices"]["messages"], list): fail()
    messages = result["choices"]["messages"]
    if len(messages) != meta["scanned_messages"] - meta["skipped_messages"]: fail()
    previous_end = meta["page_offset"]
    for item in messages:
        if not keys(item, "id byte_length label short_name unit discipline parameter_category parameter_number reference_time forecast_time forecast_unit surface_type surface_scale surface_value shape first step scanning_mode earth_shape bitmap missing_count packing_bits"): fail()
        offset = message_offset(item["id"])
        if (offset < previous_end or not integer(item["byte_length"], MAX_READ, 179) or offset + item["byte_length"] > meta["source_bytes"]
            or any(not label(item[k]) for k in ("label", "short_name", "unit"))
            or any(not integer(item[k], 255) for k in ("discipline", "parameter_category", "parameter_number", "forecast_unit", "surface_type"))
            or not integer(item["forecast_time"], 2**31 - 1) or item["forecast_unit"] not in {0, 1, 2, 10, 11, 12, 13}
            or item["surface_scale"] is not None and not integer(item["surface_scale"], 127, -127)
            or item["surface_value"] is not None and not integer(item["surface_value"], 2**32 - 2)
            or not isinstance(item["reference_time"], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", item["reference_time"])
            or not isinstance(item["shape"], list) or len(item["shape"]) != 2 or any(not integer(n, MAX_POINTS, 1) for n in item["shape"]) or math.prod(item["shape"]) > MAX_POINTS
            or not isinstance(item["first"], list) or len(item["first"]) != 2 or any(not finite(n, 360) for n in item["first"]) or not 0 <= item["first"][0] < 360 or abs(item["first"][1]) > 90
            or not isinstance(item["step"], list) or len(item["step"]) != 2 or any(not finite(n, 360) or not n for n in item["step"])
            or not integer(item["scanning_mode"], 255) or item["scanning_mode"] & 31
            or not integer(item["earth_shape"], 9) or type(item["bitmap"]) is not bool
            or not integer(item["missing_count"], math.prod(item["shape"])) or not item["bitmap"] and item["missing_count"]
            or not integer(item["packing_bits"], 32)): fail()
        datetime.datetime.fromisoformat(item["reference_time"].replace("Z", "+00:00"))
        ny, nx = item["shape"]
        if ((item["step"][0] < 0) != bool(item["scanning_mode"] & 128) or (item["step"][1] > 0) != bool(item["scanning_mode"] & 64)
            or abs(item["first"][1] + (ny - 1) * item["step"][1]) > 90 + 1e-6 or abs((nx - 1) * item["step"][0]) >= 360): fail()
        previous_end = offset + item["byte_length"]
    if meta["next_offset"] is not None and previous_end > meta["next_offset"]: fail()
    if view == "tree":
        if (meta["page_offset"] != selected["offset"] or any(meta[k] for k in ("decoded_points", "output_values", "missing_values"))
            or result["tree"] != [{"path": "/" + item["id"], "node_type": "array", "attributes": {"label": item["short_name"]}} for item in messages]): fail()
    else:
        if len(messages) != 1 or messages[0]["id"] != selected["message"] or meta["page_offset"] != message_offset(selected["message"]): fail()
        item, (x, y, width, height) = messages[0], selected["roi"]
        end = meta["page_offset"] + item["byte_length"]
        if meta["next_offset"] != (end if end < meta["source_bytes"] else None): fail()
        if x + width > item["shape"][1] or y + height > item["shape"][0]: fail()
        arr = result["array"]
        if (not keys(arr, "shape dimensions values") or arr["shape"] != [height, width] or any(type(n) is not int for n in arr["shape"])
            or arr["dimensions"] != ["latitude", "longitude"] or not isinstance(arr["values"], list) or len(arr["values"]) != width * height
            or any(v is not None and not finite(v) for v in arr["values"])): fail()
        axes = result["axes"]
        if not isinstance(axes, list) or len(axes) != 2: fail()
        for index, (name, unit, start, count, slot) in enumerate((("latitude", "degrees_north", y, height, 1), ("longitude", "degrees_east", x, width, 0))):
            axis = axes[index]
            if not keys(axis, "name unit values") or axis["name"] != name or axis["unit"] != unit or not isinstance(axis["values"], list) or len(axis["values"]) != count: fail()
            expected = [item["first"][slot] + (start + n) * item["step"][slot] for n in range(count)]
            if slot == 0: expected = [n % 360 for n in expected]
            if any(not finite(v, 360) or not math.isclose(v, e, rel_tol=0, abs_tol=1e-6) for v, e in zip(axis["values"], expected)): fail()
            if count > 1 and any((b - a) * item["step"][slot] <= 0 or not math.isclose(b - a, item["step"][slot], rel_tol=0, abs_tol=1e-6) for a, b in zip(axis["values"], axis["values"][1:])): fail()
        if (meta["scanned_messages"] != 1 or meta["skipped_messages"] or meta["decoded_points"] != math.prod(item["shape"])
            or meta["output_values"] != width * height or meta["missing_values"] != sum(v is None for v in arr["values"])
            or meta["missing_values"] > item["missing_count"] or not item["bitmap"] and meta["missing_values"]): fail()
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > 2 * 1024**2 - 512: fail()
    return result


def validate_grib_window_payload(result, **bindings):
    try:
        return _validate(result, **bindings)
    except GribWindowError:
        raise
    except (TypeError, ValueError, OverflowError, KeyError, AttributeError):
        fail()
