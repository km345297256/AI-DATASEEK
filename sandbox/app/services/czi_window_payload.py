"""Inert, strict result contract for the independently bounded CZI DV reader.

No native decoder, file or network imports. Keep the backend copy equivalent.
"""
import base64
import binascii
import json
import math
import struct

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_READ_BYTES = 1024**2
MAX_TOTAL_BYTES = 32 * 1024**2
MAX_READS = 4096
MAX_DIRECTORY_BYTES = 4 * 1024**2
MAX_ENTRIES = 16384
MAX_OUTPUT_BYTES = 8 * 1024**2
ENGINE = "dataseek-czi-dv-window-v1"
VARIANT = "CZI 1.0 DV uncompressed full-resolution"
WARNINGS = [
    "仅支持未压缩 CZI 1.0 DV、显式单 scene 0 和原始分辨率；不支持压缩、金字塔、分卷或像素掩码附件。",
    "只读取目录及所选 ROI 的实际像素范围；缺失覆盖或重叠子块会拒绝，不补零、不重采样。",
    "不读取采集 XML、标注或附件；显示为像素坐标，不推测物理单位，灰度范围仅针对当前 ROI。",
]


class CziWindowError(ValueError):
    pass


def _fail(message="CZI 区域结构、选择或资源预算无效。"):
    raise CziWindowError(message)


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def validate_czi_window_options(kind, options):
    if type(kind) is not str or kind not in {"tree", "image"} or not isinstance(options, dict):
        _fail()
    if kind == "tree":
        if options:
            _fail("CZI 目录检查不接受像素选择。")
    else:
        if set(options) != {"indices", "roi"}:
            _fail("CZI 区域需要显式 C/Z/T 与 ROI。")
        indices, roi = options["indices"], options["roi"]
        if not isinstance(indices, list) or len(indices) != 3 or any(not _integer(v, 0, bound - 1) for v, bound in zip(indices, (64, 4096, 4096))):
            _fail("CZI 区域索引无效。")
        if not isinstance(roi, list) or len(roi) != 4 or any(not _integer(v, 0, 99999) for v in roi[:2]) or any(not _integer(v, 1, 1024) for v in roi[2:]):
            _fail("CZI 区域宽高须为 1 至 1024 像素。")
    return dict(options)


def validate_czi_window_payload(value, *, size=None, read_bytes=None, read_requests=None, limit=MAX_OUTPUT_BYTES):
    def require(condition):
        if not condition:
            _fail()

    require(isinstance(value, dict) and type(value.get("kind")) is str and value["kind"] in {"tree", "image"})
    image = value["kind"] == "image"
    require(set(value) == {"contract_version", "type", "reader", "kind", "media_type", "metadata", "warnings", "sampled", "choices", "selected"} | ({"data_base64"} if image else {"tree"}))
    require(_integer(value["contract_version"], 2, 2) and value["type"] == value["reader"] == "czi-window" and value["media_type"] == ("image/png" if image else "application/json"))
    require(type(value["sampled"]) is bool and value["warnings"] == WARNINGS)
    meta = value["metadata"]
    common = {"format", "variant", "engine", "scene", "scene_shape", "dimension_sizes", "pixel_types", "input_mode", "source_bytes", "read_bytes", "read_requests", "directory_bytes", "subblocks", "limits", "metadata_hidden", "coverage"}
    require(isinstance(meta, dict) and set(meta) == common | ({"display_range", "normalization", "output_shape", "invalid_pixels"} if image else set()))
    require(meta["format"] == "CZI" and meta["variant"] == VARIANT and meta["engine"] == ENGINE and _integer(meta["scene"], 0, 0) and meta["input_mode"] == "window" and meta["metadata_hidden"] is True and meta["coverage"] == ("complete and non-overlapping" if image else "not decoded"))
    for key, low, high, expected in (("source_bytes", 544, MAX_SOURCE_BYTES, size), ("read_bytes", 1, MAX_TOTAL_BYTES, read_bytes), ("read_requests", 1, MAX_READS, read_requests), ("directory_bytes", 160, MAX_DIRECTORY_BYTES + 32, None), ("subblocks", 1, MAX_ENTRIES, None)):
        require(_integer(meta[key], low, high) and (expected is None or type(expected) is int and meta[key] == expected))
    limits = meta["limits"]
    require(isinstance(limits, dict) and set(limits) == {"max_source_bytes", "max_read_bytes", "max_total_bytes", "max_reads", "max_directory_bytes", "max_entries", "max_roi_size"})
    for key, maximum in (("max_source_bytes", MAX_SOURCE_BYTES), ("max_directory_bytes", MAX_DIRECTORY_BYTES), ("max_entries", MAX_ENTRIES), ("max_roi_size", 1024)):
        require(_integer(limits[key], maximum, maximum))
    for key, maximum in (("max_read_bytes", MAX_READ_BYTES), ("max_total_bytes", MAX_TOTAL_BYTES), ("max_reads", MAX_READS)):
        require(_integer(limits[key], 1, maximum))
    require(meta["read_bytes"] <= limits["max_total_bytes"] and meta["read_requests"] <= limits["max_reads"] and meta["read_bytes"] <= meta["read_requests"] * limits["max_read_bytes"])
    shape, dims, types = meta["scene_shape"], meta["dimension_sizes"], meta["pixel_types"]
    require(isinstance(shape, list) and len(shape) == 2 and all(_integer(v, 1, 100000) for v in shape))
    require(isinstance(dims, dict) and set(dims) == {"C", "Z", "T"} and all(_integer(v, 1, 64 if k == "C" else 4096) for k, v in dims.items()))
    require(isinstance(types, list) and len(types) == dims["C"] and all(type(v) is str and v in {"Gray8", "Gray16", "Gray32Float", "Bgr24"} for v in types))
    choices = value["choices"]
    require(isinstance(choices, dict) and choices == {"channels": list(range(dims["C"])), "z_count": dims["Z"], "time_count": dims["T"], "max_roi_size": 1024} and all(type(v) is int for v in choices["channels"]) and all(type(choices[k]) is int for k in ("z_count", "time_count", "max_roi_size")))
    selected = value["selected"]
    validate_czi_window_options("image", selected)
    indices, roi = selected["indices"], selected["roi"]
    require(all(v < dims[k] for v, k in zip(indices, ("C", "Z", "T"))) and roi[0] + roi[2] <= shape[1] and roi[1] + roi[3] <= shape[0])
    require(value["sampled"] == (image and (roi != [0, 0, shape[1], shape[0]] or any(v > 1 for v in dims.values()))))
    if image:
        require(isinstance(meta["output_shape"], list) and all(type(v) is int for v in meta["output_shape"]) and meta["output_shape"] == [roi[3], roi[2]] and _integer(meta["invalid_pixels"], 0, roi[2] * roi[3] - 1))
        display_range = meta["display_range"]
        require(isinstance(display_range, list) and len(display_range) == 2 and all(type(v) in {int, float} and abs(v) <= 3.5e38 and math.isfinite(v) for v in display_range) and display_range[0] <= display_range[1])
        color = types[indices[0]] == "Bgr24"
        require(meta["normalization"] == ("native uint8 BGR to RGB; no scaling" if color else "ROI min-max to uint8 grayscale; original data unchanged"))
        if color:
            require(display_range == [0, 255] and meta["invalid_pixels"] == 0)
        encoded = value["data_base64"]
        require(isinstance(encoded, str) and 0 < len(encoded) <= 7 * 1024**2)
        try:
            png = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error):
            _fail()
        require(base64.b64encode(png).decode("ascii") == encoded and 33 <= len(png) <= 5 * 1024**2 and png[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" and struct.unpack(">II", png[16:24]) == (roi[2], roi[3]) and png[24:29] == bytes((8, 2 if color else 6, 0, 0, 0)))
    else:
        require(value["tree"] == [{"path": "/0", "node_type": "object", "attributes": {"label": "Uncompressed CZI directory", "children_count": 0}}] and type(value["tree"][0]["attributes"]["children_count"]) is int)
    require(_integer(limit, 1, MAX_OUTPUT_BYTES) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= limit)
    return value
