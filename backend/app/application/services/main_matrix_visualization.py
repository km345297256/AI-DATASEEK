"""Pure, bounded Cordis contract for the migrated high-dimensional workbench."""
from __future__ import annotations

import json
import math
import re

ERROR = "矩阵文件、选择或结果不符合安全预览限制，请缩小区域或检查格式。"
WARNING = "预览仅显示选定分量与索引切片；等步长抽样及统计不代表整个源数组。稀疏结构模式按显示网格分箱非零项；非有限值显示为空。"
LIMITS = {"max_input_bytes": 134217728, "max_output_bytes": 8388608,
          "max_arrays": 512, "max_dimensions": 16, "max_points": 512,
          "max_values": 262144, "max_curve_points": 4096}
COMPONENTS = {"real", "imaginary", "magnitude", "phase"}
PLANE_KEYS = "values rows columns shape sampled row_step column_step row_range column_range structure nonzero finite_count count minimum maximum mean standard_deviation row_profile column_profile non_finite"
_SENSITIVE = re.compile(r"(?:/Users/|/home/|/tmp/|/private/|/var/|file:|https?://|[A-Za-z]:\\)", re.I)


def need(ok):
    if not ok:
        raise ValueError(ERROR)


def integer(v, lo=0, hi=2**53 - 1):
    return type(v) is int and lo <= v <= hi


def number(v):
    return type(v) in {int, float} and math.isfinite(v) and (type(v) is not int or abs(v) <= 2**53 - 1)


def keys(v, names):
    return isinstance(v, dict) and set(v) == set(names.split())


def label(v):
    return isinstance(v, str) and 1 <= len(v) <= 128 and not _SENSITIVE.search(v) and not re.search(r"[<>\x00-\x1f\x7f]", v)


def validate_main_matrix_options(kind, options):
    need(isinstance(kind, str) and kind in {"tree", "image", "series"} and isinstance(options, dict))
    if kind in {"tree", "series"}:
        need(not options)
        return {}
    need(keys(options, "variable axes indices component row_range column_range max_points structure"))
    need(isinstance(options["variable"], str) and re.fullmatch(r"array-(?:0|[1-9][0-9]{0,2})", options["variable"]))
    need(isinstance(options["axes"], list) and len(options["axes"]) == 2 and len(set(options["axes"])) == 2 and all(integer(a, 0, 15) for a in options["axes"]))
    need(isinstance(options["indices"], list) and len(options["indices"]) <= 16 and all(integer(i, 0, 2**31 - 1) for i in options["indices"]))
    need(isinstance(options["component"], str) and options["component"] in COMPONENTS)
    need(integer(options["max_points"], 16, 512) and type(options["structure"]) is bool)
    for key in ("row_range", "column_range"):
        r = options[key]
        need(r is None or isinstance(r, list) and len(r) == 2 and all(integer(n, 0, 2**31 - 1) for n in r) and r[0] < r[1])
    return json.loads(json.dumps(options))


def selection_plan(info, selected):
    shape, axes, indices = info["shape"], selected["axes"], selected["indices"]
    need(len(indices) == len(shape) and all(i < n for i, n in zip(indices, shape)))
    need(all(a < len(shape) for a in axes) if len(shape) >= 2 else axes == [0, 1])
    need(not info["sparse"] or axes == [0, 1])
    row_n = shape[axes[0]] if len(shape) >= 2 else 1
    col_n = shape[axes[1]] if len(shape) >= 2 else shape[0] if shape else 1
    row = selected["row_range"] or [0, row_n]
    col = selected["column_range"] or [0, col_n]
    need(row[1] <= row_n and col[1] <= col_n)
    if len(shape) < 2:
        need(row == [0, 1])
    row_step = max(1, math.ceil((row[1] - row[0]) / selected["max_points"]))
    col_step = max(1, math.ceil((col[1] - col[0]) / selected["max_points"]))
    return list(row), list(col), row_step, col_step


def validate_main_matrix_payload(value, *, kind=None, options=None, fmt=None, size=None, limit=8388608):
    try:
        need(keys(value, "contract_version type reader kind media_type matrix selected metadata warnings sampled"))
        need(type(value["contract_version"]) is int and value["contract_version"] == 2)
        need(value["type"] == value["reader"] == "matrix-workbench" and value["media_type"] == "application/json")
        k = value["kind"]
        selected = validate_main_matrix_options(k, value["selected"])
        need(kind is None or kind == k)
        need(options is None or selected == validate_main_matrix_options(k, options))
        need(value["warnings"] == [WARNING] and type(value["sampled"]) is bool)
        metadata = value["metadata"]
        need(keys(metadata, "format input_mode source_bytes limits matrix_semantics"))
        need(metadata["format"] in {"npy", "npz", "mtx", "mat"} and (fmt is None or fmt == metadata["format"]))
        need(metadata["input_mode"] == "whole" and integer(metadata["source_bytes"], 1, LIMITS["max_input_bytes"]))
        need(size is None or type(size) is int and size == metadata["source_bytes"])
        need(metadata["limits"] == LIMITS and all(type(v) is int for v in metadata["limits"].values()))
        need(metadata["matrix_semantics"] == "selected component; sampled statistics; sparse structure bins nonzero entries")
        matrix = value["matrix"]
        need(keys(matrix, "arrays plane curves") and isinstance(matrix["arrays"], list) and 1 <= len(matrix["arrays"]) <= 512)
        for i, info in enumerate(matrix["arrays"]):
            need(keys(info, "id name shape dtype sparse") and info["id"] == f"array-{i}" and label(info["name"]))
            need(isinstance(info["shape"], list) and len(info["shape"]) <= 16 and all(integer(n, 1, 2**31 - 1) for n in info["shape"]) and math.prod(info["shape"]) <= 2**53 - 1)
            need(isinstance(info["dtype"], str) and re.fullmatch(r"(?:bool|(?:u?int)(?:8|16|32|64)|float(?:16|32|64|128)|complex(?:64|128|256))", info["dtype"]))
            need(type(info["sparse"]) is bool and (not info["sparse"] or len(info["shape"]) == 2))
        need(isinstance(matrix["curves"], list) and len(matrix["curves"]) <= 6)
        if k != "series":
            need(not matrix["curves"])
        else:
            seen = set()
            for curve in matrix["curves"]:
                need(keys(curve, "variable title kind x y") and curve["variable"] in {"S", "singular_values", "residual_history", "eigenvalues"})
                name = curve["variable"]
                allowed = {"S": {"奇异值谱", "已保存奇异值的累计能量比例"}, "singular_values": {"奇异值谱", "已保存奇异值的累计能量比例"}, "residual_history": {"迭代残差"}, "eigenvalues": {"特征值分布"}}
                need(curve["title"] in allowed[name] and (name, curve["title"]) not in seen)
                seen.add((name, curve["title"]))
                need(curve["kind"] == ("scatter" if name == "eigenvalues" else "line"))
                need(isinstance(curve["x"], list) and isinstance(curve["y"], list) and 1 <= len(curve["x"]) == len(curve["y"]) <= 4096 and all(number(n) for n in curve["x"] + curve["y"]))
                need(curve["kind"] != "line" or curve["x"] == list(range(len(curve["x"]))))
        plane = matrix["plane"]
        if k != "image":
            need(plane is None and value["sampled"] is False)
        else:
            info = next((a for a in matrix["arrays"] if a["id"] == selected["variable"]), None)
            need(info is not None and keys(plane, PLANE_KEYS))
            row, col, row_step, col_step = selection_plan(info, selected)
            rows, cols = list(range(*row, row_step)), list(range(*col, col_step))
            need(plane["rows"] == rows and plane["columns"] == cols and all(type(n) is int for n in plane["rows"] + plane["columns"]))
            need(plane["shape"] == info["shape"] and all(type(n) is int for n in plane["shape"]))
            need(plane["row_range"] == row and plane["column_range"] == col and all(type(n) is int for n in plane["row_range"] + plane["column_range"]))
            need(integer(plane["row_step"], row_step, row_step) and integer(plane["column_step"], col_step, col_step))
            need(type(plane["sampled"]) is bool and plane["sampled"] == (row_step > 1 or col_step > 1) == value["sampled"])
            need(type(plane["structure"]) is bool and plane["structure"] == selected["structure"])
            values = plane["values"]
            need(isinstance(values, list) and len(values) == len(rows) and all(isinstance(r, list) and len(r) == len(cols) for r in values))
            flat = [n for r in values for n in r]
            need(1 <= len(flat) <= 262144 and all(n is None or number(n) for n in flat))
            if selected["structure"]:
                need(all(type(n) in {int, float} and n in {0, 1} for n in flat))
            finite = [n for n in flat if n is not None]
            need(integer(plane["count"], len(flat), len(flat)) and integer(plane["finite_count"], len(finite), len(finite)) and integer(plane["non_finite"], len(flat) - len(finite), len(flat) - len(finite)))
            need(integer(plane["nonzero"], 0, 2**53 - 1))
            need(plane["minimum"] == (min(finite) if finite else None) and plane["maximum"] == (max(finite) if finite else None))
            for field in ("mean", "standard_deviation"):
                need(plane[field] is None if not finite else number(plane[field]))
            need(plane["standard_deviation"] is None or plane["standard_deviation"] >= 0)
            for field, length in (("row_profile", len(rows)), ("column_profile", len(cols))):
                need(isinstance(plane[field], list) and len(plane[field]) == length and all(n is None or number(n) for n in plane[field]))
        need(integer(limit, 1, LIMITS["max_output_bytes"]) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= limit)
        return value
    except (ValueError, KeyError, TypeError, OverflowError, AttributeError):
        raise ValueError(ERROR) from None
