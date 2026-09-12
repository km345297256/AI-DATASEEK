"""Bounded ASCII Amptek/PMCA single-spectrum preview, without Qt or fitting.

This independently implemented subset uses the documented ASCII spectrum/data
sections supported by PyMca's specfilewrapper. It is not a generic .mca reader:
binary MCA, SPEC, QXAS, multi-spectra and unidentified dialects are rejected.
Only an explicit two-point calibration with an energy unit enables energy.
No arbitrary instrument header/footer text, paths or serial numbers escape.

References:
https://github.com/silx-kit/pymca/blob/master/src/PyMca5/PyMcaIO/specfilewrapper.py
https://www.amptek.com/software/dp5-digital-pulse-processor-software/dppmca-display-acquisition-software
"""
from __future__ import annotations

import json
import math
import re
from fractions import Fraction

MAX_INPUT_BYTES = 4 * 1024**2
MAX_CHANNELS = 8192
MAX_LINES = 20000
MAX_LINE_BYTES = 512
MAX_HEADER_BYTES = 65536
MAX_OUTPUT_BYTES = 2 * 1024**2
MAX_COUNT = 2**53 - 1
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?\Z")


class MCAError(ValueError):
    """Only fixed safe messages are suitable for the worker boundary."""


def _number(text):
    if (len(text) > 32 or not _NUMBER.fullmatch(text)
        or ("e" in text.lower() and abs(int(text.lower().split("e")[1])) > 12)):
        raise MCAError("MCA 校准数值超出安全预算。")
    value = Fraction(text)
    if abs(value) > 10**12:
        raise MCAError("MCA 校准数值超出安全预算。")
    return value


def mca_preview(data: bytes, fmt="mca", options=None):
    if fmt != "mca" or (options is not None and options != {}):
        raise MCAError("不支持的 MCA 格式或预览参数。")
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_INPUT_BYTES:
        raise MCAError("MCA 文件超过 4 MiB 单谱预算。")
    if any(v not in (9, 10, 13) and not 32 <= v <= 126 for v in data):
        raise MCAError("仅支持可识别的 ASCII Amptek/PMCA 单谱，不支持二进制 MCA。")
    # Bound lines before splitlines creates an untrusted number of objects.
    if data.count(b"\n") + data.count(b"\r") > 2 * MAX_LINES:
        raise MCAError("MCA 文件行数超过安全预算。")
    lines = data.decode("ascii").splitlines()
    if not 1 <= len(lines) <= MAX_LINES or any(len(line) > MAX_LINE_BYTES for line in lines):
        raise MCAError("MCA 文件行数或行长度超过安全预算。")
    lines = [line.strip() for line in lines]
    first = next((i for i, line in enumerate(lines) if line), len(lines))
    lines = lines[first:]
    if not lines or lines[0] not in {"<<PMCA SPECTRUM>>", "<<DPPMCA SPECTRUM>>", "<<AMPTEK SPECTRUM>>"}:
        raise MCAError("未识别的 MCA 方言；仅支持 Amptek/PMCA ASCII 单谱。")
    counts, points, unit, sections = [], [], None, set()
    section, completed, metadata_bytes = "header", False, 0
    acquisition = {}
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("<<"):
            if not re.fullmatch(r"<<[A-Z0-9 _-]{1,40}>>", line):
                raise MCAError("MCA 分节标记无效。")
            name = line[2:-2]
            if name in {"PMCA SPECTRUM", "DPPMCA SPECTRUM", "AMPTEK SPECTRUM"}:
                raise MCAError("MCA 预览只接受单谱。")
            if name == "DATA":
                if "DATA" in sections or completed:
                    raise MCAError("MCA 预览不允许重复数据段。")
                section = "data"
            elif name == "END":
                if section != "data" or not counts:
                    raise MCAError("MCA 数据段结束标记无效。")
                completed, section = True, "footer"
            else:
                if section == "data":
                    raise MCAError("MCA 数据段缺少结束标记。")
                if name in {"CALIBRATION", "ROI"}:
                    if name in sections or completed:
                        raise MCAError("MCA 校准或 ROI 段重复或位置无效。")
                    section = name.lower()
                else:
                    # Hardware configuration footer is inert and suppressed.
                    section = "footer" if completed else "header"
            sections.add(name)
            if len(sections) > 32:
                raise MCAError("MCA 分节数量超过安全预算。")
            continue
        if section == "data":
            if not re.fullmatch(r"\d{1,16}", line):
                raise MCAError("MCA 数据段必须每行包含一个非负整数计数。")
            count = int(line)
            if count > MAX_COUNT or len(counts) >= MAX_CHANNELS:
                raise MCAError("MCA 计数精度或通道数量超过预览预算。")
            counts.append(count)
            continue
        metadata_bytes += len(line) + 1
        if metadata_bytes > MAX_HEADER_BYTES:
            raise MCAError("MCA 元信息超过安全预算。")
        if section == "calibration":
            if line.startswith("LABEL"):
                match = re.fullmatch(r"LABEL\s*-\s*([A-Za-z0-9 _()/.-]{1,32})", line)
                if match is None or unit is not None:
                    raise MCAError("MCA 校准单位字段无效或重复。")
                unit = match[1]
            else:
                pair = line.split()
                if len(pair) != 2 or len(points) >= 64:
                    raise MCAError("MCA 校准点无效或超过预算。")
                points.append([_number(value) for value in pair])
        elif section == "header":
            for key in ("LIVE_TIME", "REAL_TIME"):
                if line.startswith(key):
                    match = re.fullmatch(key + r"\s*-\s*(\S+)", line)
                    if match is None or key in acquisition:
                        raise MCAError("MCA 采集时长字段无效或重复。")
                    value = _number(match[1])
                    if value < 0:
                        raise MCAError("MCA 采集时长字段无效。")
                    acquisition[key] = float(value)
    if not completed or "DATA" not in sections:
        raise MCAError("MCA 单谱数据不完整。")
    calibrated, coefficients = False, None
    warnings = ["仅显示原始通道计数；不作峰拟合、扣背景或计数率归一化。"]
    if unit in {"eV", "keV", "MeV"} and len(points) == 2:
        (x0, y0), (x1, y1) = points
        if not (0 <= x0 < len(counts) and 0 <= x1 < len(counts)) or x0 == x1:
            raise MCAError("MCA 校准通道范围无效或重复。")
        gain = (y1 - y0) / (x1 - x0)
        zero = y0 - gain * x0
        if gain <= 0:
            raise MCAError("MCA 能量校准必须单调递增。")
        x = [float(zero + gain * channel) for channel in range(len(counts))]
        if any(not math.isfinite(v) for v in x) or any(a >= b for a, b in zip(x, x[1:])):
            raise MCAError("MCA 能量校准无法安全表示。")
        calibrated, coefficients = True, [float(zero), float(gain), 0.0]
        warnings.append("能量轴仅按文件内两个明确校准点作线性换算；未拟合、未验证仪器校准。")
    else:
        x = list(range(len(counts)))
        warnings.append("缺少恰好两个校准点及明确能量单位，保留通道轴；不猜测或拟合校准。")
    x_label = "energy (" + unit + ")" if calibrated else "channel"
    result = {"contract_version": 2, "type": "mca", "reader": "mca", "kind": "series",
        "media_type": "application/json", "sampled": False, "warnings": warnings,
        "array": {"shape": [len(counts), 2], "dimensions": [x_label, "counts"],
                  "values": [v for pair in zip(x, counts) for v in pair]},
        "metadata": {"format": "mca", "dialect": "amptek-pmca-ascii", "input_bytes": len(data),
            "channels": len(counts), "axis": "energy" if calibrated else "channel",
            "energy_unit": unit if calibrated else None, "calibration_applied": calibrated,
            "calibration_coefficients": coefficients, "calibration_points": [[float(v) for v in p] for p in points],
            "live_time_seconds": acquisition.get("LIVE_TIME"), "real_time_seconds": acquisition.get("REAL_TIME"),
            "raw_counts": True, "fitting": False, "header_text_hidden": True,
            "limits": {"max_channels": MAX_CHANNELS, "max_input_bytes": MAX_INPUT_BYTES}}}
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_OUTPUT_BYTES:
        raise MCAError("MCA 谱线输出超过预算。")
    return result
