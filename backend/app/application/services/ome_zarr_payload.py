"""Strict NGFF subset result contract; identical copy is tested across builds."""
from __future__ import annotations

import json
import math
import re

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_TOTAL_BYTES = 32 * 1024**2
MAX_READS = 256
MAX_OUTPUT_BYTES = 2 * 1024**2
MAX_ROI = 128
SEMANTICS = "raw stored values; no fill-value masking or intensity normalization"
WARNING = "只读取所选层级和平面的原始像素；不按填充值掩膜、不作定量校正。缺失块拒绝读取，不补零。"
AXIS_TYPES = {"t": "time", "c": "channel", "z": "space", "y": "space", "x": "space"}
UNITS = {"angstrom", "attometer", "centimeter", "decimeter", "exameter", "femtometer", "foot", "gigameter", "hectometer", "inch", "kilometer", "megameter", "meter", "micrometer", "mile", "millimeter", "nanometer", "parsec", "petameter", "picometer", "terameter", "yard", "yoctometer", "yottameter", "zeptometer", "zettameter", "attosecond", "centisecond", "day", "decisecond", "exasecond", "femtosecond", "gigasecond", "hectosecond", "hour", "kilosecond", "megasecond", "microsecond", "millisecond", "minute", "nanosecond", "petasecond", "picosecond", "second", "terasecond", "yoctosecond", "yottasecond", "zeptosecond", "zettasecond"}


class OmeZarrError(ValueError):
    pass


def fail():
    raise OmeZarrError("OME-Zarr 结构、选择或预算不受支持；请确认 NGFF 0.4 / Zarr v2、层级和完整数据块。")


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        fail()


def integer(value, low=0, high=2**53-1):
    return type(value) is int and low <= value <= high


def finite(value):
    try:
        return type(value) in {int, float} and math.isfinite(value) and (type(value) is not int or abs(value) <= 2**53-1)
    except OverflowError:
        return False


def validate_axes(axes):
    if not isinstance(axes, list) or not 2 <= len(axes) <= 5:
        fail()
    names = []
    for axis in axes:
        exact(axis, {"name", "type", "unit"})
        name = axis["name"]
        if name not in AXIS_TYPES or axis["type"] != AXIS_TYPES[name] or (axis["unit"] is not None and axis["unit"] not in UNITS):
            fail()
        if name == "c" and axis["unit"] is not None:
            fail()
        time_units = {unit for unit in UNITS if unit.endswith("second") or unit in {"minute", "hour", "day"}}
        if axis["unit"] is not None and ((axis["type"] == "time" and axis["unit"] not in time_units) or (axis["type"] == "space" and axis["unit"] in time_units)):
            fail()
        names.append(name)
    if names[-2:] != ["y", "x"] or len(set(names)) != len(names) or sorted(names, key=list(AXIS_TYPES).index) != names:
        fail()


def validate_ome_options(kind, options):
    if kind not in {"tree", "image"} or not isinstance(options, dict):
        fail()
    if kind == "tree":
        if options:
            fail()
        return options
    exact(options, {"level", "indices", "roi"})
    if not integer(options["level"], 0, 15) or not isinstance(options["indices"], list) or len(options["indices"]) > 3 or not all(integer(x, 0, 10**9-1) for x in options["indices"]):
        fail()
    roi = options["roi"]
    if not isinstance(roi, list) or len(roi) != 4 or not all(integer(x, 0, 10**9) for x in roi) or not all(1 <= x <= MAX_ROI for x in roi[2:]):
        fail()
    return options


def validate_ome_payload(value, *, kind=None, options=None, size=None, read_bytes=None, read_requests=None):
    if not isinstance(value, dict) or value.get("kind") not in {"tree", "image"}:
        fail()
    image = value["kind"] == "image"
    exact(value, {"contract_version", "reader", "type", "kind", "media_type", "metadata", "choices", "selected", "warnings", "sampled", "array" if image else "tree"})
    if type(value["contract_version"]) is not int or value["contract_version"] != 2 or value["reader"] != "ome-zarr" or value["type"] != "ome-zarr" or value["media_type"] != "application/json" or type(value["sampled"]) is not bool or value["sampled"] != image or value["warnings"] != [WARNING]:
        fail()
    exact(value["choices"], {"axes", "levels", "max_roi_size"})
    choices = value["choices"]
    validate_axes(choices["axes"])
    rank = len(choices["axes"])
    levels = choices["levels"]
    if not isinstance(levels, list) or not 1 <= len(levels) <= 16 or choices["max_roi_size"] != MAX_ROI or type(choices["max_roi_size"]) is not int:
        fail()
    for index, level in enumerate(levels):
        exact(level, {"level", "shape", "chunks", "dtype", "scale", "translation", "compression", "fill_value"})
        if not integer(level["level"], index, index) or not isinstance(level["dtype"], str) or not re.fullmatch(r"(?:\|[iu]1|[<>](?:[iu][248]|f[248]))", level["dtype"]) or level["compression"] not in {"none", "zlib", "gzip", "blosc"}:
            fail()
        for field in ("shape", "chunks", "scale", "translation"):
            vals = level[field]
            if not isinstance(vals, list) or len(vals) != rank:
                fail()
            if field in {"shape", "chunks"} and not all(integer(x, 1, 10**9) for x in vals):
                fail()
            if field in {"scale", "translation"} and not all(finite(x) and (field != "scale" or x > 0) for x in vals):
                fail()
        if math.prod(level["chunks"]) * int(level["dtype"][-1]) > 4 * 1024**2 or math.prod(level["shape"]) > 2**63-1:
            fail()
        fill = level["fill_value"]
        if fill is not None and not finite(fill) and (not isinstance(fill, str) or fill not in {"NaN", "Infinity", "-Infinity"}):
            fail()
        if index:
            for axis, entry in enumerate(choices["axes"]):
                previous = levels[index - 1]
                if entry["type"] == "space":
                    if level["shape"][axis] > previous["shape"][axis] or level["scale"][axis] < previous["scale"][axis]:
                        fail()
                elif level["shape"][axis] != levels[0]["shape"][axis] or level["scale"][axis] != levels[0]["scale"][axis]:
                    fail()
    selected = validate_ome_options("image", value["selected"])
    if selected["level"] >= len(levels):
        fail()
    level = levels[selected["level"]]
    roi, indices = selected["roi"], selected["indices"]
    if len(indices) != rank - 2 or any(i >= n for i, n in zip(indices, level["shape"][:-2])) or roi[0] + roi[2] > level["shape"][-1] or roi[1] + roi[3] > level["shape"][-2]:
        fail()
    meta = value["metadata"]
    exact(meta, {"format", "input_mode", "source_bytes", "read_bytes", "read_requests", "resource_count", "loaded_chunks", "decoded_chunk_bytes", "missing_chunks", "value_semantics", "invalid_values", "value_range", "scope"})
    if meta["format"] != "OME-NGFF 0.4 / Zarr v2" or meta["input_mode"] != "window" or meta["missing_chunks"] != "rejected" or meta["value_semantics"] != SEMANTICS or meta["scope"] != "registered local dataset image":
        fail()
    for key, low, high in (("source_bytes", 1, MAX_SOURCE_BYTES), ("read_bytes", 1, MAX_TOTAL_BYTES), ("read_requests", 1, MAX_READS), ("resource_count", 2, 2048), ("loaded_chunks", 0, 64), ("decoded_chunk_bytes", 0, MAX_TOTAL_BYTES), ("invalid_values", 0, MAX_ROI**2)):
        if not integer(meta[key], low, high):
            fail()
    for key, expected in (("source_bytes", size), ("read_bytes", read_bytes), ("read_requests", read_requests)):
        if expected is not None and meta[key] != expected:
            fail()
    if kind is not None and value["kind"] != kind:
        fail()
    if options is not None:
        validate_ome_options(value["kind"], options)
        if image and selected != options:
            fail()
    if image:
        array = value["array"]
        exact(array, {"shape", "values", "dtype"})
        if not isinstance(array["shape"], list) or not all(integer(v, 1, MAX_ROI) for v in array["shape"]):
            fail()
        if meta["value_range"] is not None and (not isinstance(meta["value_range"], list) or len(meta["value_range"]) != 2 or not all(finite(v) for v in meta["value_range"])):
            fail()
        if array["shape"] != [roi[3], roi[2]] or array["dtype"] != level["dtype"] or not isinstance(array["values"], list) or len(array["values"]) != roi[2] * roi[3] or any(v is not None and not finite(v) for v in array["values"]):
            fail()
        valid = [v for v in array["values"] if v is not None]
        dtype, itemsize = level["dtype"][1], int(level["dtype"][-1])
        if dtype in "iu":
            lower = -(2**(itemsize*8-1)) if dtype == "i" else 0
            upper = 2**(itemsize*8-(1 if dtype == "i" else 0))-1
            if len(valid) != len(array["values"]) or any(type(v) is not int or not lower <= v <= upper for v in valid):
                fail()
        else:
            maximum = {2: 65504, 4: 3.4028234663852886e38, 8: 1.7976931348623157e308}[itemsize]
            if any(abs(v) > maximum for v in valid):
                fail()
        chunk_y, chunk_x = level["chunks"][-2:]
        touched = ((roi[1]+roi[3]-1)//chunk_y-roi[1]//chunk_y+1) * ((roi[0]+roi[2]-1)//chunk_x-roi[0]//chunk_x+1)
        if meta["loaded_chunks"] != touched or meta["decoded_chunk_bytes"] != touched * math.prod(level["chunks"]) * itemsize:
            fail()
        if meta["value_range"] != ([min(valid), max(valid)] if valid else None) or meta["invalid_values"] != len(array["values"]) - len(valid) or meta["loaded_chunks"] < 1:
            fail()
    else:
        expected = [{"path": "/" + str(i), "node_type": "array", "attributes": {"label": "Level " + str(i), "shape": level["shape"], "dtype": level["dtype"]}} for i, level in enumerate(levels)]
        if json.dumps(value["tree"], sort_keys=True) != json.dumps(expected, sort_keys=True) or any(meta[k] != 0 for k in ("loaded_chunks", "decoded_chunk_bytes", "invalid_values")) or meta["value_range"] is not None:
            fail()
        if selected != {"level": 0, "indices": [0] * (rank-2), "roi": [0, 0, min(MAX_ROI, levels[0]["shape"][-1]), min(MAX_ROI, levels[0]["shape"][-2])]}:
            fail()
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
        fail()
    return value
