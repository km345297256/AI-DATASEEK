"""ENVI Standard raw BSQ/BIL/BIP via exactly two private byte capabilities.

No GDAL filename discovery, filesystem reads or header-provided data paths.
Only contiguous spans covering selected pixels are fetched; gaps count toward
the actual IO budget. The whole cube is never mapped or allocated.
"""
from __future__ import annotations
import math
import re
import struct
from decimal import Decimal

from app.services.envi_window_payload import (
    DTYPES, ERROR, LIMITS, WARNING, expected_axes, finite, integer, require,
    validate_cube, validate_envi_resources, validate_envi_window_options,
    validate_envi_window_payload,
)


def parse_header(data, data_bytes):
    require(type(data) is bytes and 0 < len(data) <= 65536 and b"\x00" not in data)
    try:
        text = data.decode("ascii")
    except UnicodeError:
        raise ValueError(ERROR) from None
    lines = text.replace("\r\n", "\n").split("\n")
    require(lines.pop(0).strip() == "ENVI")
    fields, pending = {}, ""
    for line in lines:
        if not pending and (not line.strip() or line.lstrip().startswith(";")):
            continue
        pending += " " + line.strip()
        require(pending.count("{") <= 1 and pending.count("}") <= 1)
        if pending.count("{") > pending.count("}"):
            continue
        require("=" in pending and pending.count("{") == pending.count("}"))
        key, value = pending.split("=", 1)
        key, value = " ".join(key.lower().split()), value.strip()
        require(re.fullmatch(r"[a-z][a-z0-9 ]{0,63}", key) and key not in fields and len(fields) < 256)
        require("{" not in value or (value.startswith("{") and value.endswith("}")))
        fields[key], pending = value, ""
    require(not pending and fields.get("file type", "").lower() == "envi standard" and "data file" not in fields)

    def number(key, default=None):
        value = fields.get(key, default)
        require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value))
        return int(value)

    def real(value):
        require(isinstance(value, str) and len(value) <= 64 and re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", value))
        result = float(value)
        require(finite(result) and (result != 0 or Decimal(value) == 0))
        return result

    cube = {"samples": number("samples"), "lines": number("lines"), "bands": number("bands"),
            "data_type": number("data type"), "byte_order": number("byte order"),
            "interleave": fields.get("interleave", "").lower(), "header_offset": number("header offset", "0"),
            "wavelengths": None, "wavelength_unit": None, "ignore_value": None}
    if "wavelength" in fields:
        value = fields["wavelength"]
        require(value.startswith("{") and value.endswith("}"))
        tokens = [t.strip() for t in value[1:-1].split(",")]
        if tokens and tokens[-1] == "":
            tokens.pop()
        require(len(tokens) == cube["bands"] <= 2048)
        cube["wavelengths"] = [real(t) for t in tokens]
        cube["wavelength_unit"] = fields.get("wavelength units")
    if "data ignore value" in fields:
        ignore = real(fields["data ignore value"])
        require(cube["data_type"] in DTYPES)
        if cube["data_type"] not in {4, 5}:
            require(ignore.is_integer())
            ignore = int(ignore)
        try:
            normalized = struct.unpack("<"+DTYPES[cube["data_type"]][1], struct.pack("<"+DTYPES[cube["data_type"]][1], ignore))[0]
        except (OverflowError, struct.error):
            raise ValueError(ERROR) from None
        require(finite(normalized) and (normalized != 0 or ignore == 0))
        cube["ignore_value"] = normalized
    return validate_cube(cube, data_bytes)


def envi_window_preview(read_range, size, resources, kind="tree", options=None, limits=None):
    options = {} if options is None else options
    validate_envi_window_options(kind, options)
    validate_envi_resources(resources, size)
    maxima = {"max_read_bytes": 1024**2, "max_total_bytes": 8*1024**2, "max_reads": 256}
    limits = maxima if limits is None else limits
    require(isinstance(limits, dict) and set(limits) == set(maxima)
            and all(integer(limits[k], 1, maxima[k]) for k in maxima)
            and limits["max_read_bytes"] <= limits["max_total_bytes"])
    total, reads = 0, 0

    def fetch(offset, length):
        nonlocal total, reads
        require(integer(offset, 0, size-1) and integer(length, 1, min(size-offset, limits["max_read_bytes"]))
                and total+length <= limits["max_total_bytes"] and reads < limits["max_reads"]
                and any(r["offset"] <= offset and offset+length <= r["offset"]+r["size"] for r in resources))
        value = read_range(offset, length)
        require(type(value) is bytes and len(value) == length)
        total += length
        reads += 1
        return value

    header, data = resources
    cube = parse_header(fetch(0, header["size"]), data["size"])
    result = {"contract_version": 2, "type": "envi-window", "reader": "envi-window", "kind": kind,
              "media_type": "application/json", "choices": {"cube": cube}, "selected": dict(options),
              "warnings": [WARNING], "sampled": False}
    nulls = 0
    if kind == "tree":
        result["tree"] = [{"path": "/cube", "node_type": "array", "shape": [cube["bands"], cube["lines"], cube["samples"]], "dtype": DTYPES[cube["data_type"]][0]}]
    else:
        require(options["x"] < cube["samples"] and options["y"] < cube["lines"])
        if kind == "image":
            require(options["band"] < cube["bands"] and options["x"]+options["width"] <= cube["samples"]
                    and options["y"]+options["height"] <= cube["lines"])
            points = [(options["band"], y, x) for y in range(options["y"], options["y"]+options["height"])
                      for x in range(options["x"], options["x"]+options["width"])]
            shape, dimensions = [options["height"], options["width"]], ["row", "column"]
        else:
            require(options["band_start"]+options["band_count"] <= cube["bands"])
            points = [(band, options["y"], options["x"]) for band in range(options["band_start"], options["band_start"]+options["band_count"])]
            shape, dimensions = [options["band_count"]], ["band"]
        dtype, code, width = DTYPES[cube["data_type"]]
        unpack = struct.Struct(("<" if cube["byte_order"] == 0 else ">") + code)
        def position(point):
            band, y, x = point
            if cube["interleave"] == "bsq":
                index = (band*cube["lines"]+y)*cube["samples"]+x
            elif cube["interleave"] == "bil":
                index = (y*cube["bands"]+band)*cube["samples"]+x
            else:
                index = (y*cube["samples"]+x)*cube["bands"]+band
            return data["offset"]+cube["header_offset"]+index*width
        locations = [(position(p), i) for i, p in enumerate(points)]
        locations.sort()
        # Merge nearby samples, without reading arbitrarily large gaps between
        # bands. All skipped bytes still count as fetched bytes in metadata.
        groups = []
        for offset, index in locations:
            if groups and offset-groups[-1][-1][0] <= 4096 and offset+width-groups[-1][0][0] <= limits["max_read_bytes"]:
                groups[-1].append((offset, index))
            else:
                groups.append([(offset, index)])
        spans = [(g[0][0], g[-1][0]+width-g[0][0], g) for g in groups]
        require(total+sum(span[1] for span in spans) <= limits["max_total_bytes"] and reads+len(spans) <= limits["max_reads"])
        values = [None]*len(points)
        for offset, length, group in spans:
            chunk = fetch(offset, length)
            for location, index in group:
                value = unpack.unpack_from(chunk, location-offset)[0]
                if not math.isfinite(value) or value == cube["ignore_value"]:
                    nulls += 1
                else:
                    values[index] = value
        result["array"] = {"shape": shape, "dimensions": dimensions, "dtype": dtype, "values": values}
        result["axes"] = expected_axes(kind, options, cube)
    result["metadata"] = {"format": "hdr", "input_mode": "window", "source_bytes": size,
        "header_bytes": header["size"], "data_bytes": data["size"], "read_bytes": total, "read_requests": reads,
        "null_values": nulls, "limits": dict(LIMITS), "no_calibration": True, "no_georeferencing": True}
    return validate_envi_window_payload(result, kind=kind, options=options, fmt="hdr", source_bytes=size,
        read_bytes=total, read_requests=reads)
