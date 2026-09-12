"""NGFF 0.4 / Zarr v2 image windows over an exact private object capability.

References: ngff.openmicroscopy.org/0.4 and zarr-specs.readthedocs.io/en/latest/v2/v2.0.html
No filesystem, network, codec registry, arbitrary Store, recursive discovery or
missing-chunk fill. The host pins every member; this decoder only loads an
explicit plane ROI and returns original finite numeric values, never markup.
"""
from __future__ import annotations

import itertools
import json
import math
import re
import struct
import zlib

from app.services.ome_zarr_payload import (
    MAX_SOURCE_BYTES, MAX_TOTAL_BYTES, MAX_READS, MAX_ROI, WARNING, SEMANTICS,
    fail, exact, finite, integer, validate_axes, validate_ome_options, validate_ome_payload,
)

KEY = re.compile(r"(?:\.zattrs|\.zgroup|(?:0|[1-9][0-9]{0,2})/(?:\.zarray|(?:0|[1-9][0-9]{0,8})(?:[./](?:0|[1-9][0-9]{0,8})){1,4}))\Z")


def _pairs(items):
    value = {}
    for key, item in items:
        if key in value:
            fail()
        value[key] = item
    return value


def _constant(_):
    fail()


class Objects:
    def __init__(self, read_range, size, resources, limits):
        caps = {"max_read_bytes": 1024**2, "max_total_bytes": MAX_TOTAL_BYTES, "max_reads": MAX_READS}
        if limits is None:
            limits = caps
        exact(limits, caps)
        if any(not integer(v, 1, caps[k]) for k, v in limits.items()) or limits["max_read_bytes"] > limits["max_total_bytes"] or not integer(size, 1, MAX_SOURCE_BYTES):
            fail()
        if not isinstance(resources, list) or not 2 <= len(resources) <= 2048:
            fail()
        self.resources, end = {}, 0
        for row in resources:
            exact(row, {"key", "offset", "size"})
            if not isinstance(row["key"], str) or len(row["key"]) > 128 or not KEY.fullmatch(row["key"]) or row["key"] in self.resources or not integer(row["offset"], end, end) or not integer(row["size"], 1, 4 * 1024**2):
                fail()
            self.resources[row["key"]] = row
            end += row["size"]
        if end != size or not {".zattrs", ".zgroup"} <= self.resources.keys():
            fail()
        self.size, self.limits, self.read_range = size, limits, read_range
        self.read_bytes = self.read_requests = 0

    def get(self, key, maximum):
        row = self.resources.get(key)
        if row is None or row["size"] > maximum:
            fail()  # A missing or unregistered chunk is NOT a zero-valued image.
        output = bytearray()
        for start in range(0, row["size"], self.limits["max_read_bytes"]):
            count = min(self.limits["max_read_bytes"], row["size"] - start)
            if self.read_requests >= self.limits["max_reads"] or self.read_bytes + count > self.limits["max_total_bytes"]:
                fail()
            self.read_requests += 1
            self.read_bytes += count
            data = self.read_range(row["offset"] + start, count)
            if type(data) is not bytes or len(data) != count:
                fail()
            output.extend(data)
        return bytes(output)

    def metadata(self, key):
        data = self.get(key, 64 * 1024)
        try:
            value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        except (ValueError, UnicodeError, RecursionError):
            fail()
        if not isinstance(value, dict):
            fail()
        return value


def _transform(value, rank):
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        fail()
    scale, translation = None, [0] * rank
    for index, transform in enumerate(value):
        expected = "scale" if index == 0 else "translation"
        exact(transform, {"type", expected})
        if transform["type"] != expected or not isinstance(transform[expected], list) or len(transform[expected]) != rank or not all(finite(v) and (expected != "scale" or v > 0) for v in transform[expected]):
            fail()
        if index == 0:
            scale = transform[expected]
        else:
            translation = transform[expected]
    return scale, translation


def _array_metadata(raw, rank):
    import numpy as np
    required = {"zarr_format", "shape", "chunks", "dtype", "compressor", "fill_value", "order", "filters"}
    if not required <= raw.keys() or set(raw) - required - {"dimension_separator"} or type(raw["zarr_format"]) is not int or raw["zarr_format"] != 2 or raw["order"] != "C" or raw["filters"] not in (None, []):
        fail()
    for key in ("shape", "chunks"):
        if not isinstance(raw[key], list) or len(raw[key]) != rank or not all(integer(n, 1, 10**9) for n in raw[key]):
            fail()
    if not isinstance(raw["dtype"], str) or not re.fullmatch(r"[<>|][iuf][1248]", raw["dtype"]):
        fail()
    try:
        dtype = np.dtype(raw["dtype"])
    except (ValueError, TypeError):
        fail()
    if dtype.kind not in "iuf" or dtype.itemsize not in {1, 2, 4, 8} or math.prod(raw["chunks"]) * dtype.itemsize > 4 * 1024**2 or math.prod(raw["shape"]) > 2**63 - 1:
        fail()
    separator = raw.get("dimension_separator", ".")
    if separator not in {".", "/"}:
        fail()
    codec = raw["compressor"]
    if codec is None:
        compression = "none"
    else:
        if not isinstance(codec, dict) or codec.get("id") not in {"zlib", "gzip", "blosc"}:
            fail()
        compression = codec["id"]
        if compression in {"zlib", "gzip"}:
            exact(codec, {"id", "level"})
            if not integer(codec["level"], -1, 9):
                fail()
        else:
            if set(codec) - {"id", "cname", "clevel", "shuffle", "blocksize"} or not {"id", "cname", "clevel", "shuffle"} <= codec.keys() or codec["cname"] not in {"blosclz", "lz4", "lz4hc", "zlib", "zstd"} or not integer(codec["clevel"], 0, 9) or not integer(codec["shuffle"], 0, 2) or not integer(codec.get("blocksize", 0), 0, 4 * 1024**2):
                fail()
    fill = raw["fill_value"]
    if fill is not None and not finite(fill) and (not isinstance(fill, str) or fill not in {"NaN", "Infinity", "-Infinity"}):
        fail()
    if dtype.kind in "iu" and fill is not None:
        if type(fill) is not int or not np.iinfo(dtype).min <= fill <= np.iinfo(dtype).max:
            fail()
    return dtype, separator, compression


def _decode(data, compression, expected, itemsize):
    if compression == "none":
        result = data
    elif compression in {"gzip", "zlib"}:
        decoder = zlib.decompressobj(31 if compression == "gzip" else 15)
        result = decoder.decompress(data, expected + 1)
        if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            fail()
    else:
        # c-blosc fixed header: validate decoded size before entering native code.
        if len(data) < 16:
            fail()
        version, _, _, typesize, nbytes, blocksize, cbytes = struct.unpack_from("<BBBBIII", data)
        if version not in {1, 2} or typesize != itemsize or nbytes != expected or cbytes != len(data) or not 0 < blocksize <= expected:
            fail()
        from numcodecs import blosc
        result = blosc.decompress(data)
    if type(result) is not bytes or len(result) != expected:
        fail()
    return result


def ome_zarr_preview(read_range, size, resources, kind="tree", options=None, limits=None):
    import numpy as np
    options = {} if options is None else options
    validate_ome_options(kind, options)
    source = Objects(read_range, size, resources, limits)
    group = source.metadata(".zgroup")
    if group != {"zarr_format": 2} or type(group.get("zarr_format")) is not int:
        fail()
    attrs = source.metadata(".zattrs")
    if {"plate", "well", "labels", "image-label", "bioformats2raw.layout"} & attrs.keys():
        fail()
    multiscales = attrs.get("multiscales")
    if not isinstance(multiscales, list) or len(multiscales) != 1 or not isinstance(multiscales[0], dict):
        fail()
    multi = multiscales[0]
    if multi.get("version") != "0.4" or not isinstance(multi.get("axes"), list):
        fail()
    axes = []
    for axis in multi["axes"]:
        if not isinstance(axis, dict) or not {"name", "type"} <= axis.keys() or set(axis) - {"name", "type", "unit"}:
            fail()
        axes.append({"name": axis["name"], "type": axis["type"], "unit": axis.get("unit")})
    validate_axes(axes)
    rank = len(axes)
    datasets = multi.get("datasets")
    if not isinstance(datasets, list) or not 1 <= len(datasets) <= 16:
        fail()
    global_scale, global_translation = _transform(multi["coordinateTransformations"], rank) if "coordinateTransformations" in multi else ([1] * rank, [0] * rank)
    levels, private, paths = [], [], set()
    for index, dataset in enumerate(datasets):
        exact(dataset, {"path", "coordinateTransformations"})
        path = dataset["path"]
        if not isinstance(path, str) or not re.fullmatch(r"0|[1-9][0-9]{0,2}", path) or path in paths:
            fail()
        paths.add(path)
        raw = source.metadata(path + "/.zarray")
        dtype, separator, compression = _array_metadata(raw, rank)
        scale, translation = _transform(dataset["coordinateTransformations"], rank)
        scale = [a * b for a, b in zip(scale, global_scale)]
        translation = [a * b + c for a, b, c in zip(translation, global_scale, global_translation)]
        if not all(finite(v) for v in scale + translation):
            fail()
        if levels and any(raw["shape"][i] > levels[-1]["shape"][i] or scale[i] < levels[-1]["scale"][i] for i in range(rank) if axes[i]["type"] == "space"):
            fail()
        if levels and any(raw["shape"][i] != levels[0]["shape"][i] or scale[i] != levels[0]["scale"][i] for i in range(rank) if axes[i]["type"] != "space"):
            fail()
        levels.append({"level": index, "shape": raw["shape"], "chunks": raw["chunks"], "dtype": dtype.str, "scale": scale, "translation": translation, "compression": compression, "fill_value": raw["fill_value"]})
        private.append((path, dtype, separator))
    selected = {"level": 0, "indices": [0] * (rank - 2), "roi": [0, 0, min(MAX_ROI, levels[0]["shape"][-1]), min(MAX_ROI, levels[0]["shape"][-2])]} if kind == "tree" else options
    if selected["level"] >= len(levels):
        fail()
    level = levels[selected["level"]]
    roi, indices = selected["roi"], selected["indices"]
    if len(indices) != rank-2 or any(i >= n for i, n in zip(indices, level["shape"][:-2])) or roi[0] + roi[2] > level["shape"][-1] or roi[1] + roi[3] > level["shape"][-2]:
        fail()
    meta = {"format": "OME-NGFF 0.4 / Zarr v2", "input_mode": "window", "source_bytes": size, "read_bytes": 0, "read_requests": 0, "resource_count": len(resources), "loaded_chunks": 0, "decoded_chunk_bytes": 0, "missing_chunks": "rejected", "value_semantics": SEMANTICS, "invalid_values": 0, "value_range": None, "scope": "registered local dataset image"}
    result = {"contract_version": 2, "type": "ome-zarr", "reader": "ome-zarr", "kind": kind, "media_type": "application/json", "choices": {"axes": axes, "levels": levels, "max_roi_size": MAX_ROI}, "selected": selected, "metadata": meta, "warnings": [WARNING], "sampled": kind == "image"}
    if kind == "tree":
        result["tree"] = [{"path": "/"+str(i), "node_type": "array", "attributes": {"label": "Level "+str(i), "shape": item["shape"], "dtype": item["dtype"]}} for i, item in enumerate(levels)]
    else:
        path, dtype, separator = private[selected["level"]]
        chunks = level["chunks"]
        ranges = [[i // n] for i, n in zip(indices, chunks[:-2])]
        ranges += [range(roi[1] // chunks[-2], (roi[1] + roi[3] - 1) // chunks[-2] + 1), range(roi[0] // chunks[-1], (roi[0] + roi[2] - 1) // chunks[-1] + 1)]
        count = math.prod(len(values) for values in ranges)
        decoded_size = math.prod(chunks) * dtype.itemsize
        if count > 64 or count * decoded_size > MAX_TOTAL_BYTES:
            fail()
        output = np.empty((roi[3], roi[2]), dtype=dtype)
        covered = np.zeros(output.shape, dtype=bool)
        for coordinate in itertools.product(*ranges):
            key = path + "/" + separator.join(str(i) for i in coordinate)
            encoded = source.get(key, 4 * 1024**2)
            decoded = _decode(encoded, level["compression"], decoded_size, dtype.itemsize)
            block = np.frombuffer(decoded, dtype=dtype).reshape(chunks)
            plane = tuple(i % n for i, n in zip(indices, chunks[:-2]))
            y, x = coordinate[-2] * chunks[-2], coordinate[-1] * chunks[-1]
            y0, y1 = max(y, roi[1]), min(y + chunks[-2], roi[1] + roi[3])
            x0, x1 = max(x, roi[0]), min(x + chunks[-1], roi[0] + roi[2])
            output[y0-roi[1]:y1-roi[1], x0-roi[0]:x1-roi[0]] = block[plane + (slice(y0-y, y1-y), slice(x0-x, x1-x))]
            covered[y0-roi[1]:y1-roi[1], x0-roi[0]:x1-roi[0]] = True
            meta["loaded_chunks"] += 1
            meta["decoded_chunk_bytes"] += decoded_size
        if not np.all(covered):
            fail()
        values = []
        for value in output.reshape(-1).tolist():
            if type(value) is int and abs(value) > 2**53-1:
                fail()
            values.append(value if finite(value) else None)
        valid = [v for v in values if v is not None]
        meta["invalid_values"] = len(values) - len(valid)
        meta["value_range"] = [min(valid), max(valid)] if valid else None
        result["array"] = {"shape": [roi[3], roi[2]], "values": values, "dtype": dtype.str}
    meta.update(read_bytes=source.read_bytes, read_requests=source.read_requests)
    return validate_ome_payload(result, kind=kind, options=options, size=size, read_bytes=source.read_bytes, read_requests=source.read_requests)
