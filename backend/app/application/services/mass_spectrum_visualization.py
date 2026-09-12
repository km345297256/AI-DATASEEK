"""Pure mass-spectrum contract, mirrored in the API service without parsers."""
import json
import math
import re

MAX_INPUT = 16 * 1024**2
MAX_OUTPUT = 2 * 1024**2
MAX_SPECTRA = 1024
MAX_POINTS = 16384
PAGE = 64
FORMATS = {"mgf", "mzml"}
REASONS = {"supported", "unsupported-representation", "unsupported-encoding", "ambiguous-metadata", "point-budget", "empty-spectrum"}
WARNING = "仅显示显式选择的单条质谱；保留原始 m/z、强度和声明单位，不归一化、扣背景、寻峰、插值或静默抽样。"


class MassSpectrumError(ValueError):
    pass


def require(ok, message="质谱格式、选择或资源预算无效。"):
    if not ok:
        raise MassSpectrumError(message)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def number(value, low=-1e308, high=1e308):
    return type(value) in {int, float} and low <= value <= high and math.isfinite(value)


def spectrum_index(value):
    require(type(value) is str and re.fullmatch(r"s-[0-9]{6}", value) is not None)
    index = int(value[2:])
    require(index < MAX_SPECTRA)
    return index


def validate_mass_spectrum_options(kind, options):
    require(type(kind) is str and kind in {"tree", "series"} and type(options) is dict)
    if kind == "tree":
        require(set(options) <= {"offset"})
        offset = options.get("offset", 0)
        require(integer(offset, 0, MAX_SPECTRA - 1) and offset % PAGE == 0)
        return {"offset": offset}
    require(set(options) == {"spectrum"})
    spectrum_index(options["spectrum"])
    return dict(options)


def descriptor(value):
    require(type(value) is dict and set(value) == {"id", "index", "points", "representation", "mz_unit", "intensity_unit", "retention_time", "time_unit", "precursor_mz", "ms_level", "selectable", "reason"})
    require(integer(value["index"], 0, MAX_SPECTRA - 1) and spectrum_index(value["id"]) == value["index"])
    require(integer(value["points"], 0, 2**31 - 1))
    require(type(value["representation"]) is str and value["representation"] in {"centroid", "profile", "unknown"})
    require(value["mz_unit"] is None or value["mz_unit"] == "MS:1000040")
    unit = value["intensity_unit"]
    require(unit is None or type(unit) is str and re.fullmatch(r"[A-Z]{2,8}:[0-9]{1,12}", unit) is not None)
    require(value["retention_time"] is None or number(value["retention_time"], 0, 1e12))
    require(value["time_unit"] is None or type(value["time_unit"]) is str and value["time_unit"] in {"s", "min"})
    require((value["retention_time"] is None) == (value["time_unit"] is None))
    require(value["precursor_mz"] is None or number(value["precursor_mz"], 0))
    require(value["ms_level"] is None or integer(value["ms_level"], 1, 100))
    require(type(value["selectable"]) is bool and type(value["reason"]) is str and value["reason"] in REASONS)
    require(value["selectable"] == (value["reason"] == "supported"))
    if value["selectable"]:
        require(1 <= value["points"] <= MAX_POINTS and value["representation"] in {"centroid", "profile"})
    if value["reason"] == "empty-spectrum": require(value["points"] == 0)
    if value["reason"] == "point-budget": require(value["points"] > MAX_POINTS)
    return value


def validate_mass_spectrum_payload(value, *, kind=None, options=None, fmt=None, size=None, limit=MAX_OUTPUT):
    require(type(value) is dict)
    actual_kind = value.get("kind")
    require(type(actual_kind) is str and actual_kind in {"tree", "series"} and (kind is None or actual_kind == kind))
    require(set(value) == {"contract_version", "type", "reader", "kind", "media_type", "choices", "selected", "metadata", "warnings", "sampled", "tree" if actual_kind == "tree" else "array"})
    require(integer(value["contract_version"], 2, 2) and value["type"] == value["reader"] == "mass-spectrum" and value["media_type"] == "application/json")
    require(value["warnings"] == [WARNING] and value["sampled"] is False)
    selected = validate_mass_spectrum_options(actual_kind, value["selected"])
    require(value["selected"] == selected and (options is None or selected == validate_mass_spectrum_options(actual_kind, options)))
    meta = value["metadata"]
    require(type(meta) is dict and set(meta) == {"format", "dialect", "input_mode", "source_bytes", "total_spectra", "offset", "next_offset", "decoded_bytes", "output_points"})
    require(type(meta["format"]) is str and meta["format"] in FORMATS and (fmt is None or fmt == meta["format"]))
    require(meta["dialect"] == ("mgf-ions-2column" if meta["format"] == "mgf" else "mzml-1.1-inline-float") and meta["input_mode"] == "whole")
    require(integer(meta["source_bytes"], 1, MAX_INPUT) and (size is None or integer(size, 1, MAX_INPUT) and size == meta["source_bytes"]))
    require(integer(meta["total_spectra"], 1, MAX_SPECTRA) and integer(meta["offset"], 0, meta["total_spectra"] - 1))
    require(integer(meta["decoded_bytes"], 0, MAX_POINTS * 16) and integer(meta["output_points"], 0, MAX_POINTS))
    choices = value["choices"]
    require(type(choices) is dict and set(choices) == {"spectra"} and type(choices["spectra"]) is list)
    spectra = choices["spectra"]
    for item in spectra: descriptor(item)
    if actual_kind == "tree":
        offset = selected["offset"]
        require(meta["offset"] == offset and len(spectra) == min(PAGE, meta["total_spectra"] - offset))
        require([v["index"] for v in spectra] == list(range(offset, offset + len(spectra))))
        require(meta["next_offset"] == (offset + PAGE if offset + PAGE < meta["total_spectra"] else None))
        require(meta["decoded_bytes"] == meta["output_points"] == 0)
        require(value["tree"] == [{"path": "/" + v["id"], "node_type": "array", "attributes": {"label": "Spectrum " + str(v["index"] + 1)}} for v in spectra])
    else:
        require(len(spectra) == 1 and spectra[0]["selectable"] and spectra[0]["id"] == selected["spectrum"])
        item = spectra[0]
        require(meta["offset"] == item["index"] and meta["next_offset"] is None and meta["output_points"] == item["points"])
        require(meta["decoded_bytes"] == 0 if meta["format"] == "mgf" else meta["decoded_bytes"] in {item["points"] * 8, item["points"] * 12, item["points"] * 16})
        array = value["array"]
        require(type(array) is dict and set(array) == {"shape", "dimensions", "values"})
        require(type(array["shape"]) is list and len(array["shape"]) == 2 and integer(array["shape"][0], 1, MAX_POINTS) and type(array["shape"][1]) is int and array["shape"] == [item["points"], 2])
        require(array["dimensions"] == ["m/z", "intensity"] and type(array["values"]) is list and len(array["values"]) == item["points"] * 2)
        require(all(number(v, 0) if i % 2 == 0 else number(v) for i, v in enumerate(array["values"])))
        if item["representation"] == "profile":
            require(all(a < b for a, b in zip(array["values"][::2], array["values"][2::2])), "连续 profile 质谱的 m/z 必须严格递增；不自动排序。")
    require(integer(limit, 1, MAX_OUTPUT))
    try:
        require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= min(limit, MAX_OUTPUT - 512))
    except (ValueError, TypeError, UnicodeError):
        raise MassSpectrumError("质谱输出编码或大小无效。") from None
    return value
