"""Strict, data-only MCA preview validation; no scientific parser on API host."""
from __future__ import annotations

import json
import math

from app.application.services.scientific_visualization import ScientificPreviewRejected

MAX_CHANNELS = 8192
MAX_INPUT_BYTES = 4 * 1024**2
MAX_OUTPUT_BYTES = 2 * 1024**2
MAX_COUNT = 2**53 - 1
WARNINGS = [
    "仅显示原始通道计数；不作峰拟合、扣背景或计数率归一化。",
    "能量轴仅按文件内两个明确校准点作线性换算；未拟合、未验证仪器校准。",
    "缺少恰好两个校准点及明确能量单位，保留通道轴；不猜测或拟合校准。",
]


def validate_mca_options(options):
    if not isinstance(options, dict) or options:
        raise ScientificPreviewRejected("MCA 预览只显示有界原始单谱，不接受拟合、文件路径或外部参数。")


def _number(value, maximum=10**100):
    return type(value) in (int, float) and -maximum <= value <= maximum and math.isfinite(value)


def _integer(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _close(a, b):
    return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)


def validate_mca_payload(data):
    required = {"contract_version", "type", "reader", "kind", "media_type", "sampled", "warnings", "array", "metadata"}
    if (not isinstance(data, dict) or set(data) != required or type(data["contract_version"]) is not int
        or data["contract_version"] != 2 or data["type"] != "mca" or data["reader"] != "mca"
        or data["kind"] != "series" or data["media_type"] != "application/json" or data["sampled"] is not False):
        raise ValueError("Invalid MCA envelope")
    meta, array = data["metadata"], data["array"]
    metadata_fields = {"format", "dialect", "input_bytes", "channels", "axis", "energy_unit", "calibration_applied",
        "calibration_coefficients", "calibration_points", "live_time_seconds", "real_time_seconds", "raw_counts",
        "fitting", "header_text_hidden", "limits"}
    if (not isinstance(meta, dict) or set(meta) != metadata_fields or meta["format"] != "mca"
        or meta["dialect"] != "amptek-pmca-ascii" or not _integer(meta["input_bytes"], MAX_INPUT_BYTES)
        or not meta["input_bytes"] or not _integer(meta["channels"], MAX_CHANNELS) or not meta["channels"]
        or meta["raw_counts"] is not True or meta["fitting"] is not False or meta["header_text_hidden"] is not True
        or type(meta["calibration_applied"]) is not bool
        or not isinstance(meta["limits"], dict) or set(meta["limits"]) != {"max_channels", "max_input_bytes"}
        or type(meta["limits"]["max_channels"]) is not int or meta["limits"]["max_channels"] != MAX_CHANNELS
        or type(meta["limits"]["max_input_bytes"]) is not int or meta["limits"]["max_input_bytes"] != MAX_INPUT_BYTES):
        raise ValueError("Invalid MCA metadata")
    for key in ("live_time_seconds", "real_time_seconds"):
        if meta[key] is not None and (not _number(meta[key], 10**12) or meta[key] < 0):
            raise ValueError("Invalid MCA acquisition duration")
    count = meta["channels"]
    if (not isinstance(array, dict) or set(array) != {"shape", "dimensions", "values"}
        or not isinstance(array["shape"], list) or len(array["shape"]) != 2
        or any(type(value) is not int for value in array["shape"]) or array["shape"] != [count, 2]
        or not isinstance(array["values"], list) or len(array["values"]) != count * 2):
        raise ValueError("Invalid MCA array dimensions")
    values = array["values"]
    if any(not _integer(value, MAX_COUNT) for value in values[1::2]):
        raise ValueError("MCA counts must remain exact nonnegative safe integers")
    points = meta["calibration_points"]
    if (not isinstance(points, list) or len(points) > 64
        or any(not isinstance(pair, list) or len(pair) != 2 or any(not _number(v, 10**12) for v in pair) for pair in points)):
        raise ValueError("Invalid MCA calibration point list")
    if meta["calibration_applied"]:
        unit, coefficients = meta["energy_unit"], meta["calibration_coefficients"]
        if (not isinstance(unit, str) or unit not in {"eV", "keV", "MeV"} or meta["axis"] != "energy"
            or array["dimensions"] != ["energy (" + unit + ")", "counts"] or len(points) != 2
            or not isinstance(coefficients, list) or len(coefficients) != 3
            or any(not _number(v) for v in coefficients) or coefficients[1] <= 0 or coefficients[2] != 0):
            raise ValueError("Energy requires explicit two-point calibration and units")
        (x0, y0), (x1, y1) = points
        if not (0 <= x0 < count and 0 <= x1 < count) or x0 == x1:
            raise ValueError("Invalid MCA calibration channel range")
        gain = (y1 - y0) / (x1 - x0)
        zero = y0 - gain * x0
        if not _number(gain) or not _number(zero) or gain <= 0 or not _close(gain, coefficients[1]) or not _close(zero, coefficients[0]):
            raise ValueError("MCA calibration coefficients do not match declared points")
        coordinates = values[::2]
        if (any(not _number(value) or not _close(value, coefficients[0] + coefficients[1] * channel)
                for channel, value in enumerate(coordinates))
            or any(a >= b for a, b in zip(coordinates, coordinates[1:]))):
            raise ValueError("MCA energy coordinates do not match calibration")
        expected_warnings = [WARNINGS[0], WARNINGS[1]]
    else:
        if (meta["axis"] != "channel" or meta["energy_unit"] is not None
            or meta["calibration_coefficients"] is not None or array["dimensions"] != ["channel", "counts"]
            or any(type(value) is not int or value != channel for channel, value in enumerate(values[::2]))):
            raise ValueError("Uncalibrated MCA must retain original channel coordinates")
        expected_warnings = [WARNINGS[0], WARNINGS[2]]
    if data["warnings"] != expected_warnings:
        raise ValueError("Invalid MCA safety notices")
    if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
        raise ValueError("MCA output budget exceeded")
    return data
