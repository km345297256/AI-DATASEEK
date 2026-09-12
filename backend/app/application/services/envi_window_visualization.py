"""Bounded ENVI pair contract. Pure validation; no file IO or scientific imports."""
from __future__ import annotations
import json
import math
import re
import struct

ERROR = "ENVI 预览不符合受控格式或读取预算。"
WARNING = "仅显示原始存储值；不应用增益、反射率缩放、坏波段修复或地图投影。缺失值不插值。"
DTYPES = {1: ("uint8", "B", 1), 2: ("int16", "h", 2), 3: ("int32", "i", 4),
          4: ("float32", "f", 4), 5: ("float64", "d", 8), 12: ("uint16", "H", 2), 13: ("uint32", "I", 4)}
LIMITS = {"max_values": 16384, "max_bands": 2048, "max_spectrum_bands": 128}
MAX_SOURCE = 8 * 1024**3

def require(condition):
    if not condition:
        raise ValueError(ERROR)

def integer(value, low, high):
    return type(value) is int and low <= value <= high

def finite(value):
    try:
        return type(value) in {int, float} and math.isfinite(value) and abs(value) <= 1e300
    except OverflowError:
        return False

def validate_envi_resources(resources, size):
    require(integer(size, 1, MAX_SOURCE) and isinstance(resources, list) and len(resources) == 2)
    offset = 0
    for row, key, maximum in zip(resources, ("header", "data"), (65536, MAX_SOURCE)):
        require(isinstance(row, dict) and set(row) == {"key", "offset", "size"}
                and row["key"] == key and integer(row["offset"], offset, offset)
                and integer(row["size"], 1, maximum))
        offset += row["size"]
    require(offset == size)
    return resources

def validate_envi_window_options(kind, options):
    require(kind in {"tree", "image", "series"} and isinstance(options, dict))
    if kind == "tree":
        require(not options)
    elif kind == "image":
        require(set(options) == {"band", "x", "y", "width", "height"})
        require(integer(options["band"], 0, 2047) and integer(options["x"], 0, 2**31-1)
                and integer(options["y"], 0, 2**31-1) and integer(options["width"], 1, 128)
                and integer(options["height"], 1, 128))
    else:
        require(set(options) == {"x", "y", "band_start", "band_count"})
        require(integer(options["x"], 0, 2**31-1) and integer(options["y"], 0, 2**31-1)
                and integer(options["band_start"], 0, 2047) and integer(options["band_count"], 1, 128))
    return options

def validate_cube(cube, data_bytes):
    require(isinstance(cube, dict) and set(cube) == {"samples", "lines", "bands", "data_type", "byte_order",
            "interleave", "header_offset", "wavelengths", "wavelength_unit", "ignore_value"})
    require(integer(cube["samples"], 1, 2**31-1) and integer(cube["lines"], 1, 2**31-1)
            and integer(cube["bands"], 1, 2048) and type(cube["data_type"]) is int and cube["data_type"] in DTYPES
            and integer(cube["byte_order"], 0, 1) and cube["interleave"] in {"bsq", "bil", "bip"}
            and integer(cube["header_offset"], 0, 1024**2))
    require(cube["header_offset"] + cube["samples"] * cube["lines"] * cube["bands"] * DTYPES[cube["data_type"]][2] == data_bytes)
    wavelengths, unit = cube["wavelengths"], cube["wavelength_unit"]
    require(wavelengths is None or (isinstance(wavelengths, list) and len(wavelengths) == cube["bands"]
            and all(finite(v) and 0 < v <= 1e12 for v in wavelengths)))
    require(unit is None or (isinstance(unit, str) and 0 < len(unit) <= 64
            and re.fullmatch(r"[A-Za-z0-9 µμ^(). -]+", unit) and wavelengths is not None))
    ignore = cube["ignore_value"]
    require(ignore is None or finite(ignore))
    if ignore is not None:
        dtype = cube["data_type"]
        if dtype not in {4, 5}:
            bits, signed = DTYPES[dtype][2]*8, dtype in {2, 3}
            low, high = (-2**(bits-1), 2**(bits-1)-1) if signed else (0, 2**bits-1)
            require(integer(ignore, low, high))
        else:
            try:
                require(struct.unpack("<"+DTYPES[dtype][1], struct.pack("<"+DTYPES[dtype][1], ignore))[0] == ignore)
            except (OverflowError, struct.error):
                raise ValueError(ERROR) from None
    return cube

def expected_axes(kind, options, cube):
    if kind == "image":
        return [{"label": "行索引", "unit": None, "values": list(range(options["y"], options["y"]+options["height"]))},
                {"label": "列索引", "unit": None, "values": list(range(options["x"], options["x"]+options["width"]))}]
    ids = list(range(options["band_start"], options["band_start"]+options["band_count"]))
    return [{"label": "光谱坐标" if cube["wavelengths"] is not None else "波段索引",
             "unit": cube["wavelength_unit"] if cube["wavelengths"] is not None else None,
             "values": [cube["wavelengths"][i] for i in ids] if cube["wavelengths"] is not None else ids}]

def validate_envi_window_payload(value, *, kind=None, options=None, fmt=None, source_bytes=None,
                                 read_bytes=None, read_requests=None):
    require(isinstance(value, dict))
    actual = value.get("kind")
    require(actual in {"tree", "image", "series"} and (kind is None or actual == kind))
    fields = {"contract_version", "type", "reader", "kind", "media_type", "choices", "selected", "metadata", "warnings", "sampled"}
    fields |= {"tree"} if actual == "tree" else {"array", "axes"}
    require(set(value) == fields and type(value["contract_version"]) is int and value["contract_version"] == 2
            and value["type"] == value["reader"] == "envi-window" and value["media_type"] == "application/json"
            and value["warnings"] == [WARNING] and value["sampled"] is False)
    m = value["metadata"]
    require(isinstance(m, dict) and set(m) == {"format", "input_mode", "source_bytes", "header_bytes", "data_bytes",
            "read_bytes", "read_requests", "null_values", "limits", "no_calibration", "no_georeferencing"})
    require(m["format"] == "hdr" and (fmt is None or fmt == "hdr") and m["input_mode"] == "window"
            and integer(m["source_bytes"], 2, MAX_SOURCE) and integer(m["header_bytes"], 1, 65536)
            and integer(m["data_bytes"], 1, MAX_SOURCE) and m["header_bytes"]+m["data_bytes"] == m["source_bytes"]
            and integer(m["read_bytes"], m["header_bytes"], 8*1024**2) and integer(m["read_requests"], 1, 256)
            and integer(m["null_values"], 0, 16384) and m["limits"] == LIMITS
            and all(type(v) is int for v in m["limits"].values())
            and m["no_calibration"] is True and m["no_georeferencing"] is True)
    for key, expected in (("source_bytes", source_bytes), ("read_bytes", read_bytes), ("read_requests", read_requests)):
        require(expected is None or m[key] == expected)
    require(isinstance(value["choices"], dict) and set(value["choices"]) == {"cube"})
    cube = validate_cube(value["choices"]["cube"], m["data_bytes"])
    selected = validate_envi_window_options(actual, value["selected"])
    require(options is None or selected == options)
    if actual == "tree":
        require(isinstance(value["tree"], list) and len(value["tree"]) == 1 and isinstance(value["tree"][0], dict)
                and isinstance(value["tree"][0].get("shape"), list) and all(type(v) is int for v in value["tree"][0]["shape"]))
        require(value["tree"] == [{"path": "/cube", "node_type": "array", "shape": [cube["bands"], cube["lines"], cube["samples"]], "dtype": DTYPES[cube["data_type"]][0]}]
                and m["read_bytes"] == m["header_bytes"] and m["read_requests"] == 1 and m["null_values"] == 0)
    else:
        require(selected["x"] < cube["samples"] and selected["y"] < cube["lines"])
        if actual == "image":
            require(selected["band"] < cube["bands"] and selected["x"]+selected["width"] <= cube["samples"]
                    and selected["y"]+selected["height"] <= cube["lines"])
            shape, dimensions = [selected["height"], selected["width"]], ["row", "column"]
        else:
            require(selected["band_start"]+selected["band_count"] <= cube["bands"])
            shape, dimensions = [selected["band_count"]], ["band"]
        a = value["array"]
        require(isinstance(a, dict) and set(a) == {"shape", "dimensions", "dtype", "values"}
                and a["shape"] == shape and all(type(v) is int for v in a["shape"])
                and a["dimensions"] == dimensions and a["dtype"] == DTYPES[cube["data_type"]][0])
        values = a["values"]
        require(isinstance(values, list) and len(values) == math.prod(shape) <= 16384
                and sum(v is None for v in values) == m["null_values"] and all(v is None or finite(v) for v in values))
        dtype = cube["data_type"]
        if dtype not in {4, 5}:
            bits, signed = DTYPES[dtype][2]*8, dtype in {2, 3}
            low, high = (-2**(bits-1), 2**(bits-1)-1) if signed else (0, 2**bits-1)
            require(all(v is None or integer(v, low, high) for v in values))
        require(isinstance(value["axes"], list) and all(isinstance(axis, dict) and isinstance(axis.get("values"), list)
                and all(finite(v) for v in axis["values"]) for axis in value["axes"]))
        require(value["axes"] == expected_axes(actual, selected, cube))
    require(len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode()) <= 2*1024**2)
    return value
