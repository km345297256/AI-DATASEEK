"""Pure NXdata window schema and request binding; no HDF5/native imports.

Kept byte-identical to sandbox/app/services/nexus_window_payload.py.
"""
from __future__ import annotations

import json
import math
import re

MAX_SOURCE_BYTES = 8 * 1024**3
MAX_ELEMENTS = 16384
MAX_SIGNALS = 32
MAX_DIM = 2**31 - 1
MAX_ATTRIBUTE_BYTES = 65536
MAX_CHUNK_BYTES = 4 * 1024**2
MAX_DECODED_BYTES = 16 * 1024**2
VALUE_SEMANTICS = "raw signal and coordinates; no scaling, unit conversion or finite fill masking"
WARNING = "只显示 NXdata 声明的原始信号、坐标和标准差；不应用缩放、单位转换或有限填充值掩膜。未声明坐标的轴使用索引，非有限信号留空。"
FORMATS = {"nxs", "nx", "h5", "hdf5", "hdf"}
_ID = re.compile(r"n-[0-9a-f]{32}\Z")
_DTYPE = re.compile(r"(?:\|[iu]1|[<>][iu][248]|[<>]f[48])\Z")
_SENSITIVE = re.compile(r"[<>\x00-\x1f\x7f]|/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\", re.I)


class NexusWindowError(ValueError):
    pass


def fail():
    raise NexusWindowError("NXdata 结构、坐标、选择或读取预算无效；可改用原始数组预览。")


def integer(value, maximum=MAX_DIM, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def label(value):
    return isinstance(value, str) and len(value) <= 128 and not _SENSITIVE.search(value)


def _keys(value, expected):
    return isinstance(value, dict) and set(value) == set(expected.split())


def _dtype(value):
    return isinstance(value, str) and bool(_DTYPE.fullmatch(value))


def _numeric(value, dtype, nullable=True):
    if value is None:
        return nullable and dtype[1] == "f"
    if type(value) not in {int, float}:
        return False
    try:
        if not math.isfinite(value) or type(value) is int and abs(value) > 2**53 - 1:
            return False
    except OverflowError:
        return False
    if dtype[1] in {"i", "u"}:
        bits = int(dtype[-1]) * 8
        lower, upper = (0, 2**bits - 1) if dtype[1] == "u" else (-(2**(bits - 1)), 2**(bits - 1) - 1)
        return type(value) is int and lower <= value <= upper
    return abs(value) <= (3.4028234663852886e38 if dtype[-1] == "4" else 1.7976931348623157e308)


def _chunks(chunks, rank, dtype):
    return chunks is None or (isinstance(chunks, list) and len(chunks) == rank
        and all(integer(v, MAX_DIM, 1) for v in chunks)
        and math.prod(chunks) * int(dtype[-1]) <= MAX_CHUNK_BYTES)


def validate_nexus_window_options(kind, options):
    if not isinstance(kind, str) or kind not in {"tree", "series", "image"} or not isinstance(options, dict):
        fail()
    if kind == "tree":
        if options:
            fail()
        return {}
    if not _keys(options, "nxdata selection") or not isinstance(options["nxdata"], str) or not _ID.fullmatch(options["nxdata"]):
        fail()
    selection = options["selection"]
    if not isinstance(selection, list) or len(selection) != (1 if kind == "series" else 2):
        fail()
    total = 1
    for part in selection:
        if not _keys(part, "start stop step") or not all(integer(v) for v in part.values()) or not part["start"] < part["stop"] or part["step"] < 1:
            fail()
        total *= (part["stop"] - part["start"] + part["step"] - 1) // part["step"]
    if total > MAX_ELEMENTS:
        fail()
    return json.loads(json.dumps(options))


def _cost(chunks, dtype, selection):
    if chunks is None:
        return 0, 0
    touched = math.prod(len({v // size for v in range(p["start"], p["stop"], p["step"])}) for size, p in zip(chunks, selection))
    return touched, touched * math.prod(chunks) * int(dtype[-1])


def _validate(result, *, kind=None, options=None, fmt=None, source_bytes=None, read_bytes=None, read_requests=None):
    if not isinstance(result, dict) or not isinstance(result.get("kind"), str) or result["kind"] not in {"tree", "series", "image"}:
        fail()
    view = result["kind"]
    base = "contract_version type reader kind media_type choices selected warnings sampled metadata "
    if (not _keys(result, base + ("tree" if view == "tree" else "array axes errors"))
        or type(result["contract_version"]) is not int or result["contract_version"] != 2
        or result["type"] != "nexus-window" or result["reader"] != "nexus-window"
        or result["media_type"] != "application/json" or result["sampled"] is not False
        or result["warnings"] != [WARNING] or kind is not None and view != kind):
        fail()
    selected = validate_nexus_window_options(view, result["selected"])
    if options is not None and selected != validate_nexus_window_options(view, options):
        fail()
    if not _keys(result["choices"], "signals") or not isinstance(result["choices"]["signals"], list) or len(result["choices"]["signals"]) > MAX_SIGNALS:
        fail()
    signals = {}
    for item in result["choices"]["signals"]:
        if (not _keys(item, "id label signal shape dtype chunks unit axes errors") or not isinstance(item["id"], str)
            or not _ID.fullmatch(item["id"]) or item["id"] in signals or not label(item["label"]) or not label(item["signal"])
            or not _dtype(item["dtype"]) or item["unit"] is not None and not label(item["unit"])
            or not isinstance(item["shape"], list) or not 1 <= len(item["shape"]) <= 2
            or any(not integer(v, MAX_DIM, 1) for v in item["shape"]) or math.prod(item["shape"]) > 2**53 - 1
            or not _chunks(item["chunks"], len(item["shape"]), item["dtype"])
            or not isinstance(item["axes"], list) or len(item["axes"]) != len(item["shape"])):
            fail()
        for dimension, axis in enumerate(item["axes"]):
            if (not _keys(axis, "label unit source dtype chunks") or not label(axis["label"])
                or axis["unit"] is not None and not label(axis["unit"]) or axis["source"] not in {"index", "dataset"}):
                fail()
            if axis["source"] == "index":
                if axis != {"label": f"index_{dimension}", "unit": None, "source": "index", "dtype": None, "chunks": None}:
                    fail()
            elif not _dtype(axis["dtype"]) or not _chunks(axis["chunks"], 1, axis["dtype"]):
                fail()
        errors = item["errors"]
        if errors is not None and (not _keys(errors, "dtype chunks") or not _dtype(errors["dtype"]) or not _chunks(errors["chunks"], len(item["shape"]), errors["dtype"])):
            fail()
        signals[item["id"]] = item
    meta = result["metadata"]
    if (not _keys(meta, "format container standard input_mode value_semantics source_bytes read_bytes read_requests catalog_truncated skipped_nxdata attribute_bytes chunks_touched decoded_chunk_bytes nonfinite_values output_values")
        or not isinstance(meta["format"], str) or meta["format"] not in FORMATS or fmt is not None and meta["format"] != fmt
        or meta["container"] != "HDF5" or meta["standard"] != "NXdata" or meta["input_mode"] != "window" or meta["value_semantics"] != VALUE_SEMANTICS
        or not integer(meta["source_bytes"], MAX_SOURCE_BYTES, 256) or not integer(meta["read_bytes"], 8 * 1024**2, 1)
        or not integer(meta["read_requests"], 128, 1) or meta["read_bytes"] < meta["read_requests"]
        or type(meta["catalog_truncated"]) is not bool or not integer(meta["skipped_nxdata"], 128)
        or not integer(meta["attribute_bytes"], MAX_ATTRIBUTE_BYTES) or not integer(meta["chunks_touched"], 128)
        or not integer(meta["decoded_chunk_bytes"], MAX_DECODED_BYTES) or not integer(meta["nonfinite_values"], MAX_ELEMENTS)
        or not integer(meta["output_values"], MAX_ELEMENTS)):
        fail()
    for name, expected in {"source_bytes": source_bytes, "read_bytes": read_bytes, "read_requests": read_requests}.items():
        if expected is not None and (type(expected) is not int or meta[name] != expected):
            fail()
    if view == "tree":
        expected = [{"path": "/" + item["id"], "node_type": "array", "attributes": {"label": item["label"]}} for item in signals.values()]
        if result["tree"] != expected or any(meta[k] for k in ("chunks_touched", "decoded_chunk_bytes", "nonfinite_values", "output_values")):
            fail()
    else:
        item = signals.get(selected["nxdata"])
        parts = selected["selection"]
        if not item or len(parts) != len(item["shape"]) or any(part["stop"] > size for part, size in zip(parts, item["shape"])):
            fail()
        indices = [list(range(p["start"], p["stop"], p["step"])) for p in parts]
        shape = [len(v) for v in indices]
        array = result["array"]
        if (not _keys(array, "shape dimensions values") or array["shape"] != shape or any(type(v) is not int for v in array["shape"])
            or array["dimensions"] != [a["label"] for a in item["axes"]] or not isinstance(array["values"], list)
            or len(array["values"]) != math.prod(shape) or any(not _numeric(v, item["dtype"]) for v in array["values"])
            or not isinstance(result["axes"], list) or len(result["axes"]) != len(shape)):
            fail()
        chunks, decoded = _cost(item["chunks"], item["dtype"], parts)
        all_values = list(array["values"])
        for dimension, (axis, desc) in enumerate(zip(result["axes"], item["axes"])):
            if (not _keys(axis, "dimension indices values label unit source") or type(axis["dimension"]) is not int or axis["dimension"] != dimension
                or axis["indices"] != indices[dimension] or any(type(v) is not int for v in axis["indices"])
                or any(axis[k] != desc[k] for k in ("label", "unit", "source")) or not isinstance(axis["values"], list)
                or len(axis["values"]) != shape[dimension]):
                fail()
            if desc["source"] == "index":
                if axis["values"] != axis["indices"] or any(type(v) is not int for v in axis["values"]):
                    fail()
            else:
                if any(not _numeric(v, desc["dtype"], False) for v in axis["values"]):
                    fail()
                values = axis["values"]
                if len(values) > 1 and not (all(a < b for a, b in zip(values, values[1:])) or all(a > b for a, b in zip(values, values[1:]))):
                    fail()
                a, b = _cost(desc["chunks"], desc["dtype"], [parts[dimension]])
                chunks += a
                decoded += b
            all_values.extend(axis["values"])
        errors = result["errors"]
        if item["errors"] is None:
            if errors is not None:
                fail()
        else:
            if not isinstance(errors, list) or len(errors) != math.prod(shape) or any(not _numeric(v, item["errors"]["dtype"]) or v is not None and v < 0 for v in errors):
                fail()
            all_values.extend(errors)
            a, b = _cost(item["errors"]["chunks"], item["errors"]["dtype"], parts)
            chunks += a
            decoded += b
        if (meta["chunks_touched"] != chunks or meta["decoded_chunk_bytes"] != decoded
            or meta["output_values"] != len(all_values) or meta["nonfinite_values"] != sum(v is None for v in all_values)):
            fail()
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()) > 2 * 1024**2 - 512:
        fail()
    return result


def validate_nexus_window_payload(result, **bindings):
    try:
        return _validate(result, **bindings)
    except NexusWindowError:
        raise
    except (TypeError, ValueError, OverflowError, KeyError, AttributeError):
        fail()
