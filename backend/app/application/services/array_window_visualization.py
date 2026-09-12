"""Pure schema/selection validation for range-backed HDF5 arrays; no HDF5 import."""
from __future__ import annotations

import json
import math
import re

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_ELEMENTS = 16384
MAX_NODES = 128
MAX_DIM = 2**31 - 1
MAX_CHUNK_BYTES = 4 * 1024**2
MAX_DECODED_BYTES = 16 * 1024**2
VALUE_SEMANTICS = "raw storage values; no CF scale/add_offset or finite fill masking"
WARNING = "仅显示显式选择的原始存储值；未应用 CF 缩放、偏移或有限填充值掩膜。非有限值显示为空。"
_REASONS = {"", "link", "alias", "depth", "type", "shape", "storage", "filter", "chunk"}
_ID = re.compile(r"v-[0-9a-f]{32}\Z")
_DTYPE = re.compile(r"[<>=|][iufb][1248]\Z")
_SENSITIVE = re.compile(r"(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)


class ArrayWindowError(ValueError):
    pass


def _fail():
    raise ArrayWindowError("数组窗口响应、选择或读取预算无效。")


def _integer(value, maximum=MAX_DIM, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def _finite(value):
    try:
        return type(value) in {int, float} and math.isfinite(value) and (type(value) is not int or abs(value) <= 2**53 - 1)
    except OverflowError:
        return False


def _label(value):
    return isinstance(value, str) and len(value) <= 128 and not re.search(r"[<>\x00-\x1f\x7f]", value) and not _SENSITIVE.search(value)


def validate_array_window_options(kind, options):
    if not isinstance(kind, str) or kind not in {"tree", "series", "image"} or not isinstance(options, dict):
        _fail()
    if kind == "tree":
        if options:
            _fail()
        return {}
    if set(options) != {"variable", "selection", "decode"} or options["decode"] != "raw" or not isinstance(options["variable"], str) or not _ID.fullmatch(options["variable"]):
        _fail()
    selection = options["selection"]
    if not isinstance(selection, list) or not 1 <= len(selection) <= 8:
        _fail()
    slices, count = 0, 1
    for value in selection:
        if _integer(value):
            continue
        if not isinstance(value, dict) or set(value) != {"start", "stop", "step"} or not all(_integer(v) for v in value.values()) or not 0 <= value["start"] < value["stop"] or value["step"] < 1:
            _fail()
        slices += 1
        count *= (value["stop"] - value["start"] + value["step"] - 1) // value["step"]
    if slices != (1 if kind == "series" else 2) or count > MAX_ELEMENTS:
        _fail()
    return json.loads(json.dumps(options))


def validate_array_window_payload(result, *, kind=None, options=None, fmt=None,
                                  source_bytes=None, read_bytes=None, read_requests=None):
    """Bind worker output to the host request and actual broker accounting.

    Host SHOULD supply all keyword bindings. The optional form also supports
    pure inert schema validation before authorization-specific checks.
    """
    if not isinstance(result, dict) or not isinstance(result.get("kind"), str) or result.get("kind") not in {"tree", "series", "image"}:
        _fail()
    view = result["kind"]
    common = {"contract_version", "type", "reader", "kind", "media_type", "metadata", "warnings", "sampled", "choices", "selected"}
    if (set(result) != common | ({"tree"} if view == "tree" else {"array", "axes"})
        or type(result["contract_version"]) is not int or result["contract_version"] != 2
        or result["type"] != "array-window" or result["reader"] != "array-window"
        or result["media_type"] != "application/json" or result["sampled"] is not False
        or result["warnings"] != [WARNING] or kind is not None and view != kind):
        _fail()
    selected = validate_array_window_options(view, result["selected"])
    if options is not None and selected != validate_array_window_options(view, options):
        _fail()
    choices = result["choices"]
    if not isinstance(choices, dict) or set(choices) != {"variables"} or not isinstance(choices["variables"], list) or len(choices["variables"]) > MAX_NODES:
        _fail()
    variables = {}
    for item in choices["variables"]:
        if (not isinstance(item, dict) or set(item) != {"id", "label", "shape", "dtype", "chunks", "selectable", "reason"}
            or not isinstance(item["id"], str) or not _ID.fullmatch(item["id"]) or item["id"] in variables
            or not _label(item["label"]) or type(item["selectable"]) is not bool
            or not isinstance(item["reason"], str) or item["reason"] not in _REASONS
            or item["selectable"] != (item["reason"] == "") or not isinstance(item["dtype"], str)
            or item["dtype"] != "unsupported" and not _DTYPE.fullmatch(item["dtype"])
            or not isinstance(item["shape"], list) or len(item["shape"]) > 8 or any(not _integer(v) for v in item["shape"])):
            _fail()
        chunks = item["chunks"]
        if chunks is not None and (not isinstance(chunks, list) or len(chunks) != len(item["shape"]) or any(not _integer(v, MAX_DIM, 1) for v in chunks)):
            _fail()
        if item["selectable"] and (not item["shape"] or not all(item["shape"]) or math.prod(item["shape"]) > 2**53 - 1
            or item["dtype"] == "unsupported" or chunks and math.prod(chunks) * int(item["dtype"][-1]) > MAX_CHUNK_BYTES):
            _fail()
        variables[item["id"]] = item
    metadata = result["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {"format", "container", "input_mode", "value_semantics", "source_bytes", "read_bytes", "read_requests", "chunks_touched", "decoded_chunk_bytes", "catalog_truncated", "attributes", "nonfinite_values", "coordinates", "limits"}:
        _fail()
    if (not isinstance(metadata["format"], str) or metadata["format"] not in {"h5", "hdf5", "hdf", "nc", "nc4", "netcdf", "mat"}
        or fmt is not None and metadata["format"] != fmt or metadata["container"] != "HDF5" or metadata["input_mode"] != "window"
        or metadata["value_semantics"] != VALUE_SEMANTICS or not _integer(metadata["source_bytes"], MAX_SOURCE_BYTES, 256)
        or not _integer(metadata["read_bytes"], 8 * 1024**2, 1) or not _integer(metadata["read_requests"], 128, 1)
        or metadata["read_bytes"] < metadata["read_requests"] or not _integer(metadata["chunks_touched"], 128)
        or not _integer(metadata["decoded_chunk_bytes"], MAX_DECODED_BYTES) or type(metadata["catalog_truncated"]) is not bool
        or not _integer(metadata["nonfinite_values"], MAX_ELEMENTS)
        or metadata["coordinates"] != "zero-based dimension indices; not geospatial coordinates"
        or metadata["limits"] != {"max_elements": MAX_ELEMENTS, "max_chunk_bytes": MAX_CHUNK_BYTES, "max_decoded_bytes": MAX_DECODED_BYTES, "max_nodes": MAX_NODES}
        or not isinstance(metadata["limits"], dict) or any(type(v) is not int for v in metadata["limits"].values())):
        _fail()
    for key, expected in {"source_bytes": source_bytes, "read_bytes": read_bytes, "read_requests": read_requests}.items():
        if expected is not None and (type(expected) is not int or metadata[key] != expected):
            _fail()
    attrs = metadata["attributes"]
    if not isinstance(attrs, dict) or set(attrs) - {"units", "scale_factor", "add_offset", "_FillValue", "missing_value"} or any(not (_label(value) if key == "units" else _finite(value)) for key, value in attrs.items()):
        _fail()
    if view == "tree":
        tree = result["tree"]
        if not isinstance(tree, list) or len(tree) > MAX_NODES or attrs or any(metadata[k] != 0 for k in ("chunks_touched", "decoded_chunk_bytes", "nonfinite_values")):
            _fail()
        seen, arrays = set(), set()
        for node in tree:
            if (not isinstance(node, dict) or set(node) != {"path", "node_type", "attributes"}
                or not isinstance(node["path"], str) or not node["path"].startswith("/") or not _ID.fullmatch(node["path"][1:])
                or node["path"] in seen or not isinstance(node["node_type"], str) or node["node_type"] not in {"object", "array"}
                or not isinstance(node["attributes"], dict) or set(node["attributes"]) != {"label", "depth", "reason"}
                or not _label(node["attributes"]["label"]) or not _integer(node["attributes"]["depth"], 8)
                or not isinstance(node["attributes"]["reason"], str) or node["attributes"]["reason"] not in _REASONS):
                _fail()
            seen.add(node["path"])
            if node["node_type"] == "array":
                ident = node["path"][1:]
                arrays.add(ident)
                if ident not in variables or any(variables[ident][k] != node["attributes"][k] for k in ("label", "reason")):
                    _fail()
        if arrays != set(variables):
            _fail()
    else:
        variable = variables.get(selected["variable"])
        if not variable or not variable["selectable"] or len(selected["selection"]) != len(variable["shape"]):
            _fail()
        axes, chunk_counts = [], []
        for axis, (size, value) in enumerate(zip(variable["shape"], selected["selection"])):
            if type(value) is int:
                if value >= size:
                    _fail()
                chunk_counts.append(1)
            else:
                if value["stop"] > size:
                    _fail()
                indices = list(range(value["start"], value["stop"], value["step"]))
                axes.append({"dimension": axis, "indices": indices})
                chunk_counts.append(len({i // variable["chunks"][axis] for i in indices}) if variable["chunks"] else 1)
        if result["axes"] != axes or not isinstance(result["axes"], list) or any(not isinstance(a, dict) or type(a.get("dimension")) is not int or not isinstance(a.get("indices"), list) or any(type(i) is not int for i in a["indices"]) for a in result["axes"]):
            _fail()
        chunks = math.prod(chunk_counts) if variable["chunks"] else 0
        decoded = chunks * math.prod(variable["chunks"]) * int(variable["dtype"][-1]) if variable["chunks"] else 0
        if metadata["chunks_touched"] != chunks or metadata["decoded_chunk_bytes"] != decoded:
            _fail()
        array = result["array"]
        expected_shape = [len(axis["indices"]) for axis in axes]
        if (not isinstance(array, dict) or set(array) != {"shape", "dimensions", "values"}
            or array["shape"] != expected_shape or not isinstance(array["shape"], list) or any(type(v) is not int for v in array["shape"])
            or array["dimensions"] != ["index_" + str(a["dimension"]) for a in axes]
            or not isinstance(array["values"], list) or len(array["values"]) != math.prod(expected_shape)
            or any(value is not None and (not _finite(value) or variable["dtype"][1] in {"i", "u", "b"} and type(value) is not int
                or variable["dtype"][1] == "u" and value < 0 or variable["dtype"][1] == "b" and value not in {0, 1}) for value in array["values"])
            or sum(value is None for value in array["values"]) != metadata["nonfinite_values"]):
            _fail()
    try:
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) > 2 * 1024**2 - 256:
            _fail()
    except (TypeError, OverflowError, ValueError):
        _fail()
    return result
