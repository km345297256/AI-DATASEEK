"""单格点序列 / 剖面工具。

对齐 Portal「单格点序列/剖面分析」与 ``recipes.timeseries.apply_recipe(..., "point")``：
固定若干维度索引后，沿剩余维（通常是 ``time`` 或末维）得到 1D 序列。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

RECIPE_ID = "point"


def format_time_label(v: Any) -> str:
    try:
        ts = np.datetime64(v, "s")
        return str(ts).replace("T", " ")[:19]
    except Exception:
        s = str(v)
        return s[:19] if len(s) > 19 else s


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


def nearest_index(coords: np.ndarray, value: float) -> int:
    """在 1D 坐标轴上找最接近 value 的索引。"""
    arr = np.asarray(coords, dtype=float).ravel()
    if arr.size == 0:
        raise ValueError("坐标轴为空")
    return int(np.nanargmin(np.abs(arr - float(value))))


def chart_title(
    *,
    variable: str = "",
    lat_index: int | None = None,
    lon_index: int | None = None,
    indexers: Mapping[str, int] | None = None,
) -> str:
    v = variable or "变量"
    if indexers:
        desc = ", ".join(f"{k}={v}" for k, v in indexers.items())
        return f"{v}（{desc}）"
    parts: list[str] = []
    if lat_index is not None:
        parts.append(f"lat={lat_index}")
    if lon_index is not None:
        parts.append(f"lon={lon_index}")
    if parts:
        return f"{v}（{', '.join(parts)}）"
    return f"{v} · 单格点序列"


def point_timeseries(
    cube: np.ndarray,
    times: Sequence[Any],
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    lat_index: int | None = None,
    lon_index: int | None = None,
    lat_value: float | None = None,
    lon_value: float | None = None,
    max_points: int = 480,
    variable: str = "",
    unit: str = "",
) -> dict[str, Any]:
    """
    从 ``(time, lat, lon)`` 立方体提取单格点时间序列。

    索引优先于数值：若给 ``lat_value`` / ``lon_value`` 则取最近格点。
    """
    arr = np.asarray(cube, dtype=np.float32)
    lat1 = np.asarray(lat, dtype=float).ravel()
    lon1 = np.asarray(lon, dtype=float).ravel()
    if arr.ndim != 3:
        raise ValueError(f"cube 需为 (time, lat, lon)，得到 shape={arr.shape}")
    if arr.shape[1] != lat1.size or arr.shape[2] != lon1.size:
        if arr.shape[1] == lon1.size and arr.shape[2] == lat1.size:
            arr = np.transpose(arr, (0, 2, 1))
        else:
            raise ValueError(
                f"cube 与坐标不匹配：cube={arr.shape} lat={lat1.size} lon={lon1.size}"
            )
    if len(times) != arr.shape[0]:
        raise ValueError(f"times 长度 {len(times)} 与时间维 {arr.shape[0]} 不一致")

    if lat_index is None:
        if lat_value is None:
            raise ValueError("请提供 lat_index 或 lat_value")
        lat_index = nearest_index(lat1, lat_value)
    if lon_index is None:
        if lon_value is None:
            raise ValueError("请提供 lon_index 或 lon_value")
        lon_index = nearest_index(lon1, lon_value)

    lat_index = int(lat_index)
    lon_index = int(lon_index)
    if not (0 <= lat_index < lat1.size):
        raise ValueError(f"lat_index 超出范围：0-{lat1.size - 1}")
    if not (0 <= lon_index < lon1.size):
        raise ValueError(f"lon_index 超出范围：0-{lon1.size - 1}")

    series = np.asarray(arr[:, lat_index, lon_index], dtype=np.float64)
    labels = [format_time_label(t) for t in times]
    labels, series, stride = downsample_series(labels, series, max_points=max_points)
    values = [None if not np.isfinite(x) else float(x) for x in series]
    indexers = {"lat": lat_index, "lon": lon_index}

    return {
        "variable": variable or "",
        "unit": unit or "",
        "recipe_id": RECIPE_ID,
        "mode": "point",
        "lat_index": lat_index,
        "lon_index": lon_index,
        "lat": float(lat1[lat_index]),
        "lon": float(lon1[lon_index]),
        "indexers": indexers,
        "point_count": len(labels),
        "stride": stride,
        "times": labels,
        "values": values,
        "title": chart_title(variable=variable, indexers=indexers),
    }


def point_profile(
    data: np.ndarray,
    *,
    indexers: Mapping[str, int],
    dim_names: Sequence[str] | None = None,
    axis_coords: Sequence[Any] | None = None,
    max_points: int = 480,
    variable: str = "",
    unit: str = "",
    remaining_dim: str = "",
) -> dict[str, Any]:
    """
    通用 N 维单点剖面：按 ``indexers`` 固定若干维，保留**最后一维**作为剖面轴。

    ``data`` 维度顺序与 ``dim_names`` 一致；``indexers`` 的 key 为维度名。
    未出现在 indexers 中的维必须恰好只剩最后一维（剖面轴）。
    """
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim < 1:
        raise ValueError("data 至少需要 1 维")
    names = list(dim_names) if dim_names else [f"dim_{i}" for i in range(arr.ndim)]
    if len(names) != arr.ndim:
        raise ValueError("dim_names 长度须与 data.ndim 一致")

    remain = [n for n in names if n not in indexers]
    if len(remain) != 1:
        raise ValueError(
            f"固定索引后应恰好保留 1 个剖面维，当前剩余：{remain or '无'}"
        )
    profile_dim = remain[0]
    profile_axis = names.index(profile_dim)

    # Move profile axis to the end, then take indices on other axes.
    arr_m = np.moveaxis(arr, profile_axis, -1)
    names_m = names[:profile_axis] + names[profile_axis + 1 :] + [profile_dim]
    slices: list[Any] = []
    used: dict[str, int] = {}
    for i, name in enumerate(names_m[:-1]):
        if name not in indexers:
            raise ValueError(f"缺少维度索引：{name}")
        idx = int(indexers[name])
        size = arr_m.shape[i]
        if not (0 <= idx < size):
            raise ValueError(f"{name} 索引超出范围：0-{size - 1}")
        slices.append(idx)
        used[name] = idx
    series = np.asarray(arr_m[tuple(slices)], dtype=np.float64)
    if series.ndim != 1:
        raise ValueError("剖面提取后不是 1D，请检查 indexers")

    if axis_coords is None:
        axis_labels: list[Any] = list(range(series.size))
    else:
        axis_labels = list(axis_coords)
        if len(axis_labels) != series.size:
            raise ValueError("axis_coords 长度与剖面维不一致")

    axis_labels, series, stride = downsample_series(
        axis_labels, series, max_points=max_points
    )
    values = [None if not np.isfinite(x) else float(x) for x in series]
    dim_label = remaining_dim or profile_dim

    return {
        "variable": variable or "",
        "unit": unit or "",
        "recipe_id": RECIPE_ID,
        "mode": "profile",
        "indexers": used,
        "remaining_dim": dim_label,
        "point_count": len(axis_labels),
        "stride": stride,
        "axis": [
            format_time_label(x) if dim_label == "time" else (
                float(x) if isinstance(x, (int, float, np.floating, np.integer)) and np.isfinite(x) else x
            )
            for x in axis_labels
        ],
        "values": values,
        "title": chart_title(variable=variable, indexers=used),
    }


def point_timeseries_from_npy(
    cube_path: str | Path,
    times_path: str | Path,
    lat_path: str | Path,
    lon_path: str | Path,
    *,
    lat_index: int | None = None,
    lon_index: int | None = None,
    lat_value: float | None = None,
    lon_value: float | None = None,
    max_points: int = 480,
    variable: str = "",
    unit: str = "",
) -> dict[str, Any]:
    """从 .npy 加载后提取单格点时间序列。"""
    cube = np.load(Path(cube_path), allow_pickle=True)
    times = np.load(Path(times_path), allow_pickle=True)
    lat = np.load(Path(lat_path))
    lon = np.load(Path(lon_path))
    return point_timeseries(
        cube,
        list(times),
        lat,
        lon,
        lat_index=lat_index,
        lon_index=lon_index,
        lat_value=lat_value,
        lon_value=lon_value,
        max_points=max_points,
        variable=variable,
        unit=unit,
    )
