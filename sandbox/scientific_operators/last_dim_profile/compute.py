"""沿末维剖面工具。

对齐 Portal「沿末维剖面分析」与 ``recipes.timeseries.apply_recipe(..., "reduce_last_dim")``：
对除最后一维（及可选 time）外的维度求 ``nanmean``，得到沿末维的剖面。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np

RECIPE_ID = "reduce_last_dim"
REDUCE = "mean"


def format_label(v: Any) -> str:
    try:
        if isinstance(v, (np.datetime64,)):
            return str(np.datetime64(v, "s")).replace("T", " ")[:19]
        ts = np.datetime64(v, "s")
        return str(ts).replace("T", " ")[:19]
    except Exception:
        if isinstance(v, (int, float, np.floating, np.integer)) and np.isfinite(v):
            return f"{float(v):.6g}"
        s = str(v)
        return s[:32] if len(s) > 32 else s


def downsample_series(
    axis: list[Any],
    values: np.ndarray,
    *,
    max_points: int = 480,
) -> tuple[list[Any], np.ndarray, int]:
    max_points = max(50, int(max_points or 480))
    labels = list(axis)
    series = np.asarray(values, dtype=np.float64)
    stride = 1
    if len(labels) > max_points:
        stride = int(np.ceil(len(labels) / max_points))
        labels = labels[::stride]
        series = series[::stride]
    return labels, series, stride


def chart_title(*, variable: str = "", last_dim: str = "") -> str:
    v = variable or "变量"
    dim = last_dim or "末维"
    return f"{v} 沿 {dim} 剖面"


def reduce_to_last_dim(
    data: np.ndarray,
    *,
    has_time: bool = False,
) -> np.ndarray:
    """
    对除最后一维（及可选第 0 维 time）外求 nanmean。

    - ``has_time=False``: 任意维 → 对除最后一维外求平均 → ``(last,)``
    - ``has_time=True``: ``(time, ..., last)`` → 对中间维求平均 → ``(time, last)``
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim < 2:
        raise ValueError("沿末维剖面至少需要 2 个维度")
    if has_time:
        if arr.ndim < 2:
            raise ValueError("含 time 时至少需要 2 维")
        # mean over axes 1 .. -2
        if arr.ndim == 2:
            return np.asarray(arr, dtype=np.float64)
        reduce_axes = tuple(range(1, arr.ndim - 1))
        with np.errstate(all="ignore"):
            return np.asarray(np.nanmean(arr, axis=reduce_axes), dtype=np.float64)
    reduce_axes = tuple(range(0, arr.ndim - 1))
    with np.errstate(all="ignore"):
        return np.asarray(np.nanmean(arr, axis=reduce_axes), dtype=np.float64)


def last_dim_profile(
    data: np.ndarray,
    *,
    last_coords: Sequence[Any] | None = None,
    times: Sequence[Any] | None = None,
    has_time: bool | None = None,
    average_over_time: bool = True,
    max_points: int = 480,
    variable: str = "",
    unit: str = "",
    last_dim: str = "",
) -> dict[str, Any]:
    """
    计算沿末维的剖面。

    Args:
        data: ``(..., last)`` 或 ``(time, ..., last)``
        last_coords: 末维坐标，长度 = ``data.shape[-1]``
        times: 若第一维是时间，可传入时间标签
        has_time: 默认在提供 ``times`` 时为 True
        average_over_time: 若结果含时间维，是否再对时间求平均得到 1D 剖面（默认 True，
            便于出图；同时仍返回 ``values_by_time``）

    Returns:
        axis / values / title / recipe_id / ...
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim < 2:
        raise ValueError("沿末维剖面至少需要 2 个维度")

    use_time = bool(has_time) if has_time is not None else (times is not None)
    reduced = reduce_to_last_dim(arr, has_time=use_time)

    n_last = int(arr.shape[-1])
    if last_coords is None:
        axis = list(range(n_last))
    else:
        axis = list(last_coords)
        if len(axis) != n_last:
            raise ValueError(f"last_coords 长度 {len(axis)} 与末维 {n_last} 不一致")

    out: dict[str, Any] = {
        "variable": variable or "",
        "unit": unit or "",
        "recipe_id": RECIPE_ID,
        "reduce": REDUCE,
        "last_dim": last_dim or "last",
        "title": chart_title(variable=variable, last_dim=last_dim or "last"),
        "input_shape": list(arr.shape),
    }

    if reduced.ndim == 1:
        labels, series, stride = downsample_series(axis, reduced, max_points=max_points)
        out["mode"] = "profile"
        out["point_count"] = len(labels)
        out["stride"] = stride
        out["axis"] = [format_label(x) if not isinstance(x, (int, float, np.integer, np.floating))
                       else (float(x) if np.isfinite(x) else None)
                       for x in labels]
        # Prefer numeric axis when possible
        numeric_axis: list[Any] = []
        for x in labels:
            if isinstance(x, (int, float, np.integer, np.floating)) and np.isfinite(x):
                numeric_axis.append(float(x))
            else:
                numeric_axis.append(format_label(x))
        out["axis"] = numeric_axis
        out["values"] = [None if not np.isfinite(x) else float(x) for x in series]
        out["times"] = None
        out["values_by_time"] = None
        return out

    # (time, last)
    if times is not None and len(times) != reduced.shape[0]:
        raise ValueError(f"times 长度 {len(times)} 与时间维 {reduced.shape[0]} 不一致")
    time_labels = [format_label(t) for t in (times or range(reduced.shape[0]))]
    out["times"] = time_labels
    out["values_by_time"] = [
        [None if not np.isfinite(x) else float(x) for x in row]
        for row in np.asarray(reduced, dtype=np.float64)
    ]
    out["mode"] = "profile_with_time"

    if average_over_time:
        with np.errstate(all="ignore"):
            profile = np.nanmean(reduced, axis=0)
        labels, series, stride = downsample_series(axis, profile, max_points=max_points)
        out["point_count"] = len(labels)
        out["stride"] = stride
        out["axis"] = [
            float(x) if isinstance(x, (int, float, np.integer, np.floating)) and np.isfinite(x)
            else format_label(x)
            for x in labels
        ]
        out["values"] = [None if not np.isfinite(x) else float(x) for x in series]
    else:
        # 默认取最后一个时间步作为 1D 剖面，避免前端无图
        labels, series, stride = downsample_series(
            axis, np.asarray(reduced[-1], dtype=np.float64), max_points=max_points
        )
        out["point_count"] = len(labels)
        out["stride"] = stride
        out["axis"] = [
            float(x) if isinstance(x, (int, float, np.integer, np.floating)) and np.isfinite(x)
            else format_label(x)
            for x in labels
        ]
        out["values"] = [None if not np.isfinite(x) else float(x) for x in series]
    return out


def last_dim_profile_from_npy(
    data_path: str | Path,
    *,
    last_coords_path: str | Path | None = None,
    times_path: str | Path | None = None,
    has_time: bool | None = None,
    average_over_time: bool = True,
    max_points: int = 480,
    variable: str = "",
    unit: str = "",
    last_dim: str = "",
) -> dict[str, Any]:
    """从 .npy 加载数组后计算末维剖面。"""
    data = np.load(Path(data_path), allow_pickle=True)
    last_coords = None
    if last_coords_path is not None:
        last_coords = list(np.load(Path(last_coords_path), allow_pickle=True))
    times = None
    if times_path is not None:
        times = list(np.load(Path(times_path), allow_pickle=True))
    return last_dim_profile(
        data,
        last_coords=last_coords,
        times=times,
        has_time=has_time,
        average_over_time=average_over_time,
        max_points=max_points,
        variable=variable,
        unit=unit,
        last_dim=last_dim,
    )
