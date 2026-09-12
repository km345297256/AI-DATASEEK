"""Strict, standard-library-only instrument window response validation.

No instrument parser executes on the API host. PNG validation accepts only
the bounded grayscale subset emitted by the isolated range reader.
"""
from __future__ import annotations

import base64
import json
import math
import struct
import zlib

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_READ_BYTES = 1024**2
MAX_TOTAL_BYTES = 32 * 1024**2
MAX_HEADER_BYTES = 1024**2
MAX_READS = 2048
MAX_OUTPUT_BYTES = 2 * 1024**2
CALIBRATION = "none; raw detector values"
NORMALIZATION = "ROI min-max to uint8 grayscale; original data unchanged"
WARNINGS = [
    "仅显示显式选择的帧和像素窗口；不执行校准、拟合或重采样。",
    "灰度图按当前 ROI 有限原值范围映射；非有限值显示为黑色，原始数据不变。",
]
_DTYPES = {"int8": 1, "uint8": 1, "int16": 2, "uint16": 2, "int32": 4, "uint32": 4, "float32": 4, "float64": 8}
_TREE = [{"path": "/0", "node_type": "object", "attributes": {"label": "Instrument image dataset", "children_count": 0}}]


class InstrumentPreviewError(ValueError):
    pass


def _require(condition):
    if not condition:
        raise InstrumentPreviewError("仪器图像响应的格式、选择、版本绑定或资源预算无效。")


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _validate_options(kind, options):
    _require(kind in {"tree", "image"} and isinstance(options, dict))
    if kind == "tree":
        _require(not options)
        return options
    _require(set(options) == {"frame", "roi"} and _integer(options["frame"], 0, 999999))
    roi = options["roi"]
    _require(isinstance(roi, list) and len(roi) == 4
             and all(_integer(v, 0, 99999) for v in roi[:2])
             and all(_integer(v, 1, 1024) for v in roi[2:]))
    return options


def validate_instrument_options(kind, options):
    try:
        return _validate_options(kind, options)
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        _require(False)


def _validate_png(encoded, width, height):
    _require(isinstance(encoded, str) and 0 < len(encoded) <= 2 * 1024**2)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        _require(False)
    _require(base64.b64encode(data).decode("ascii") == encoded and len(data) <= 2 * 1024**2)
    _require(data[:8] == b"\x89PNG\r\n\x1a\n")
    offset, chunks = 8, []
    for expected in (b"IHDR", b"IDAT", b"IEND"):
        _require(offset + 12 <= len(data))
        length = struct.unpack_from(">I", data, offset)[0]
        _require(length <= len(data) - offset - 12 and data[offset + 4:offset + 8] == expected)
        content = data[offset + 8:offset + 8 + length]
        _require(struct.unpack_from(">I", data, offset + 8 + length)[0] == zlib.crc32(expected + content))
        chunks.append(content)
        offset += length + 12
    _require(offset == len(data) and chunks[0] == struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
             and 0 < len(chunks[1]) <= 2 * 1024**2 and not chunks[2])
    expected_bytes = (width + 1) * height
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(chunks[1], expected_bytes + 1)
    except zlib.error:
        _require(False)
    _require(len(pixels) == expected_bytes and decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data)
    _require(all(pixels[row * (width + 1)] == 0 for row in range(height)))


def _validate_payload(data, *, size=None, read_bytes=None, read_requests=None,
                      kind=None, options=None, format=None):
    _require(isinstance(data, dict) and data.get("kind") in {"tree", "image"})
    image = data["kind"] == "image"
    root = {"contract_version", "type", "reader", "kind", "media_type", "metadata", "warnings", "sampled", "choices", "selected"}
    _require(set(data) == root | ({"data_base64"} if image else {"tree"}))
    _require(type(data["contract_version"]) is int and data["contract_version"] == 2
             and data["type"] == "instrument" and data["reader"] == "instrument-window"
             and data["media_type"] == ("image/png" if image else "application/json")
             and data["warnings"] == WARNINGS and type(data["sampled"]) is bool)
    meta = data["metadata"]
    common = {"format", "dialect", "format_version", "frame_count", "frame_shape", "dtype", "byte_order", "input_mode",
              "source_bytes", "read_bytes", "read_requests", "header_bytes", "header_text_hidden", "calibration", "storage_layout", "selection_mode"}
    extra = {"output_shape", "display_range", "normalization", "invalid_pixels", "roi_origin"} if image else set()
    _require(isinstance(meta, dict) and set(meta) == common | extra)
    _require(meta["format"] in {"esrf-edf", "princeton-spe"} and meta["input_mode"] == "window"
             and meta["header_text_hidden"] is True and meta["calibration"] == CALIBRATION
             and meta["storage_layout"] == "row-major; x fastest" and meta["selection_mode"] == "explicit frame and pixel ROI"
             and isinstance(meta["dtype"], str) and meta["dtype"] in _DTYPES and meta["byte_order"] in {"little", "big"})
    edf = meta["format"] == "esrf-edf"
    shape, frames = meta["frame_shape"], meta["frame_count"]
    _require(isinstance(shape, list) and len(shape) == 2 and all(_integer(v, 1, 100000 if edf else 65535) for v in shape))
    _require(_integer(frames, 1, 256 if edf else 1_000_000) and _integer(meta["source_bytes"], 1, MAX_SOURCE_BYTES)
             and _integer(meta["header_bytes"], 1, MAX_HEADER_BYTES)
             and _integer(meta["read_bytes"], meta["header_bytes"], MAX_TOTAL_BYTES)
             and _integer(meta["read_requests"], 1, MAX_READS))
    if edf:
        _require(meta["dialect"] == "edf-inline-2d-uncompressed" and meta["format_version"] is None
                 and meta["header_bytes"] % 512 == 0 and frames * 512 <= meta["header_bytes"] <= frames * 65536)
        header_reads = meta["header_bytes"] // 512
    else:
        _require(meta["dialect"] == "spe-2.x-fixed-4100" and _finite(meta["format_version"])
                 and 2 <= meta["format_version"] < 3 and meta["header_bytes"] == 4100
                 and meta["byte_order"] == "little" and meta["dtype"] in {"float32", "int32", "int16", "uint16"})
        header_reads = 1
    _require(meta["source_bytes"] == meta["header_bytes"] + frames * shape[0] * shape[1] * _DTYPES[meta["dtype"]])
    choices = data["choices"]
    _require(isinstance(choices, dict) and set(choices) == {"frame_count", "max_roi_size"}
             and _integer(choices["frame_count"], frames, frames) and _integer(choices["max_roi_size"], 1024, 1024))
    selected = data["selected"]
    validate_instrument_options("image", selected)
    frame, roi = selected["frame"], selected["roi"]
    _require(frame < frames and roi[0] + roi[2] <= shape[1] and roi[1] + roi[3] <= shape[0])
    _require(data["sampled"] is (not image or frames != 1 or roi != [0, 0, shape[1], shape[0]]))
    if image:
        pixels = roi[2] * roi[3]
        _require(meta["read_bytes"] == meta["header_bytes"] + pixels * _DTYPES[meta["dtype"]]
                 and header_reads < meta["read_requests"] <= header_reads + roi[3])
        _require(meta["output_shape"] == [roi[3], roi[2]] and isinstance(meta["output_shape"], list)
                 and all(type(v) is int for v in meta["output_shape"])
                 and meta["normalization"] == NORMALIZATION and meta["roi_origin"] == "frame top-left; x right, y down"
                 and _integer(meta["invalid_pixels"], 0, pixels))
        bounds = meta["display_range"]
        _require(isinstance(bounds, list) and len(bounds) == 2 and all(_finite(v) for v in bounds) and bounds[0] <= bounds[1])
        _require(meta["invalid_pixels"] != pixels or bounds == [0, 0])
        if meta["dtype"] in {"int8", "uint8", "int16", "uint16", "int32", "uint32"}:
            bits = 8 * _DTYPES[meta["dtype"]]
            low, high = (0, 2**bits - 1) if meta["dtype"].startswith("u") else (-2**(bits - 1), 2**(bits - 1) - 1)
            _require(meta["invalid_pixels"] == 0 and all(low <= v <= high and v == int(v) for v in bounds))
        _validate_png(data["data_base64"], roi[2], roi[3])
    else:
        _require(meta["read_bytes"] == meta["header_bytes"] and meta["read_requests"] == header_reads
                 and selected == {"frame": 0, "roi": [0, 0, min(256, shape[1]), min(256, shape[0])]}
                 and data["tree"] == _TREE)
        # Python structural equality otherwise accepts bool in place of zero.
        node = data["tree"][0]
        _require(type(node["attributes"]["children_count"]) is int)
    for expected, actual in ((size, meta["source_bytes"]), (read_bytes, meta["read_bytes"]), (read_requests, meta["read_requests"])):
        _require(expected is None or (type(expected) is int and expected == actual))
    _require(kind is None or kind == data["kind"])
    _require(format is None or (format in {"edf", "spe"} and meta["format"] == {"edf": "esrf-edf", "spe": "princeton-spe"}[format]))
    if options is not None:
        validate_instrument_options(data["kind"], options)
        _require(not image or selected == options)
    try:
        _require(len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) + 512 <= MAX_OUTPUT_BYTES)
    except (TypeError, ValueError, OverflowError):
        _require(False)
    return data


def validate_instrument_payload(data, *, size=None, read_bytes=None, read_requests=None,
                                kind=None, options=None, format=None):
    try:
        return _validate_payload(data, size=size, read_bytes=read_bytes, read_requests=read_requests,
                                 kind=kind, options=options, format=format)
    except (KeyError, TypeError, ValueError, OverflowError, IndexError, struct.error, zlib.error):
        _require(False)
