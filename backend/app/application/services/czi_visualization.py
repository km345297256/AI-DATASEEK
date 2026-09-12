"""Strict inert CZI result schema. This module never imports a CZI decoder."""
import base64
import math
import struct


class CziPreviewError(ValueError):
    pass


def _fail(message="CZI 预览结构、选择或资源预算无效。"):
    raise CziPreviewError(message)


def validate_czi_options(kind, options):
    if kind not in {"tree", "image"} or not isinstance(options, dict):
        _fail()
    if kind == "tree":
        if options:
            _fail("CZI 结构检查不接受解码参数。")
        return options
    if set(options) != {"indices", "roi"}:
        _fail("CZI 图像需要显式 C/Z/T 与 ROI。")
    indices, roi = options["indices"], options["roi"]
    if not isinstance(indices, list) or len(indices) != 3 or any(type(v) is not int or not 0 <= v < maximum for v, maximum in zip(indices, [64, 4096, 4096])):
        _fail("CZI 索引不符合预算。")
    if not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi) or any(not 0 <= v < 100000 for v in roi[:2]) or any(not 1 <= v <= 1024 for v in roi[2:]):
        _fail("CZI ROI 必须为场景内的有界像素窗口。")
    return options


def validate_czi_payload(result):
    if not isinstance(result, dict) or result.get("kind") not in {"tree", "image"}:
        _fail()
    image = result["kind"] == "image"
    fields = {"contract_version", "type", "reader", "kind", "media_type", "metadata", "warnings", "sampled", "choices", "selected"}
    if set(result) != fields | ({"data_base64"} if image else {"tree"}) or type(result["contract_version"]) is not int or result["contract_version"] != 2 or result["type"] != "czi" or result["reader"] != "czi" or result["media_type"] != ("image/png" if image else "application/json"):
        _fail()
    if type(result["sampled"]) is not bool or not isinstance(result["warnings"], list) or len(result["warnings"]) > 8 or any(not isinstance(w, str) or len(w) > 512 for w in result["warnings"]):
        _fail()
    meta = result["metadata"]
    common = {"format", "engine", "scene", "scene_shape", "dimension_sizes", "pixel_types", "input_mode", "input_bytes", "subblocks", "decoded_block_budget", "selection_mode"}
    if not isinstance(meta, dict) or set(meta) != common | ({"display_range", "normalization", "output_shape", "roi_origin", "invalid_pixels"} if image else set()):
        _fail()
    if meta["format"] != "CZI" or meta["engine"] != "pylibCZIrw 6.1.0" or type(meta["scene"]) is not int or meta["scene"] != 0 or meta["input_mode"] != "whole" or meta["selection_mode"] != "explicit C/Z/T and pixel ROI" or type(meta["input_bytes"]) is not int or not 1 <= meta["input_bytes"] <= 64 * 1024 * 1024 or type(meta["subblocks"]) is not int or not 1 <= meta["subblocks"] <= 4096 or type(meta["decoded_block_budget"]) is not int or meta["decoded_block_budget"] != 16 * 1024 * 1024:
        _fail()
    shape = meta["scene_shape"]
    if not isinstance(shape, list) or len(shape) != 2 or any(type(v) is not int or not 1 <= v <= 100000 for v in shape):
        _fail()
    dims = meta["dimension_sizes"]
    if not isinstance(dims, dict) or set(dims) != {"C", "Z", "T"} or any(type(v) is not int or not 1 <= v <= (64 if k == "C" else 4096) for k, v in dims.items()):
        _fail()
    types = meta["pixel_types"]
    if not isinstance(types, list) or len(types) != dims["C"] or any(v not in {"Gray8", "Gray16", "Gray32Float", "Bgr24"} for v in types):
        _fail()
    choices = result["choices"]
    if not isinstance(choices, dict) or choices != {"channels": list(range(dims["C"])), "z_count": dims["Z"], "time_count": dims["T"], "max_roi_size": 1024} or not isinstance(choices["channels"], list) or any(type(v) is not int for v in choices["channels"]) or any(type(choices[k]) is not int for k in ["z_count", "time_count", "max_roi_size"]):
        _fail()
    selected = result["selected"]
    if not isinstance(selected, dict) or set(selected) != {"indices", "roi"}:
        _fail()
    indices, roi = selected["indices"], selected["roi"]
    if not isinstance(indices, list) or len(indices) != 3 or any(type(v) is not int or not 0 <= v < dims[k] for v, k in zip(indices, ["C", "Z", "T"])):
        _fail()
    if not isinstance(roi, list) or len(roi) != 4 or any(type(v) is not int for v in roi) or min(roi[:2]) < 0 or not 1 <= roi[2] <= 1024 or not 1 <= roi[3] <= 1024 or roi[0] + roi[2] > shape[1] or roi[1] + roi[3] > shape[0]:
        _fail()
    if image:
        if not isinstance(meta["output_shape"], list) or any(type(v) is not int for v in meta["output_shape"]) or meta["output_shape"] != [roi[3], roi[2]] or meta["roi_origin"] != "scene top-left; x right, y down" or meta["normalization"] not in {"ROI min-max to uint8 grayscale; original data unchanged", "native uint8 BGR to RGB; no scaling"} or type(meta["invalid_pixels"]) is not int or not 0 <= meta["invalid_pixels"] <= roi[2] * roi[3]:
            _fail()
        limits = meta["display_range"]
        if not isinstance(limits, list) or len(limits) != 2 or any(type(v) not in {int, float} or not math.isfinite(v) for v in limits) or limits[0] > limits[1]:
            _fail()
        encoded = result["data_base64"]
        if not isinstance(encoded, str) or not 0 < len(encoded) <= 7 * 1024 * 1024:
            _fail()
        try:
            png = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            _fail()
        if len(png) > 5 * 1024 * 1024 or len(png) < 33 or png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" or struct.unpack(">II", png[16:24]) != (roi[2], roi[3]) or png[24] != 8 or png[25] not in {2, 6}:
            _fail()
    elif result["tree"] != [{"path": "/0", "node_type": "object", "attributes": {"label": "Single-scene CZI", "children_count": 0}}]:
        _fail()
    return result
