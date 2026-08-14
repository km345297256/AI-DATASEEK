"""选区中位时间序列工具。

对每个时间步，在多边形/bbox/全场有效像元上做空间 ``nanmedian``，得到 1D 时间序列。
对齐 Portal「{变量} · 选区中位时间序列」与 ``tpdc_batch.region_timeseries(..., reduce="median")``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np

REDUCE = "median"

# ---------------------------------------------------------------------------
# Geometry（EPSG:4326，[lon, lat]）
# ---------------------------------------------------------------------------


def normalize_polygon_4326(polygon: list[Any]) -> list[tuple[float, float]]:
    """将 [[lon, lat], ...] 规范为闭合环，至少 3 个有效顶点。"""
    pts: list[tuple[float, float]] = []
    for p in polygon or []:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        lon, lat = float(p[0]), float(p[1])
        if np.isfinite(lon) and np.isfinite(lat):
            pts.append((lon, lat))
    if len(pts) < 3:
        raise ValueError("多边形至少需要 3 个顶点 [lon, lat]")
    if pts[0] != pts[-1]:
        pts = pts + [pts[0]]
    return pts


def polygon_from_bbox(bbox: list[float]) -> list[tuple[float, float]]:
    """[west, south, east, north] → 闭合经纬多边形。"""
    if not bbox or len(bbox) < 4:
        raise ValueError("bbox 需为 [west, south, east, north]")
    west, south, east, north = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south
    return [(west, south), (east, south), (east, north), (west, north), (west, south)]


def build_outside_mask(
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
) -> tuple[np.ndarray | None, list[tuple[float, float]] | None, str]:
    """
    构造「区外」掩膜（True = 排除），供时间立方体空间归约使用。

    Returns:
        mask_outside, closed_ring_or_None, mode ('full' | 'polygon' | 'bbox')
    """
    lat1 = np.asarray(lat, dtype=float).ravel()
    lon1 = np.asarray(lon, dtype=float).ravel()

    if not polygon_4326 and not bbox_4326:
        return None, None, "full"

    from matplotlib.path import Path as MplPath

    if polygon_4326:
        ring = normalize_polygon_4326(polygon_4326)
        mode = "polygon"
    else:
        ring = polygon_from_bbox(list(bbox_4326 or []))
        mode = "bbox"

    lon2d, lat2d = np.meshgrid(lon1, lat1)
    pts = np.column_stack([lon2d.ravel(), lat2d.ravel()])
    inside = MplPath(ring, closed=True).contains_points(pts).reshape(lat1.size, lon1.size)
    if not np.any(inside):
        raise ValueError("选区内无有效网格")
    return ~inside, ring, mode


# ---------------------------------------------------------------------------
# Core reduce（对齐 _reduce_timeseries_cube(..., reduce="median")）
# ---------------------------------------------------------------------------


def spatial_median_series(
    cube: np.ndarray,
    mask_outside: np.ndarray | None = None,
) -> np.ndarray:
    """
    ``cube``: (time, lat, lon) → (time,) 每步空间中位数。

    ``mask_outside`` 为 True 的像元排除（置 NaN）。全 NaN 时间步结果为 NaN。
    """
    data = np.asarray(cube, dtype=np.float32)
    if data.ndim != 3:
        raise ValueError(f"cube 需为 (time, lat, lon)，实际 ndim={data.ndim}")
    if mask_outside is not None:
        mo = np.asarray(mask_outside, dtype=bool)
        if mo.shape != data.shape[1:]:
            raise ValueError(
                f"mask_outside 形状 {mo.shape} 与空间维 {data.shape[1:]} 不匹配"
            )
        data = np.where(mo[np.newaxis, ...], np.nan, data)
    flat = data.reshape(data.shape[0], -1)
    with np.errstate(all="ignore"):
        out = np.nanmedian(flat, axis=1)
    return np.asarray(out, dtype=np.float64)


def format_time_label(v: Any) -> str:
    """对齐 ``tpdc_batch._format_time_label``：``YYYY-MM-DD HH:MM:SS``。"""
    try:
        ts = np.datetime64(v, "s")
        return str(ts).replace("T", " ")[:19]
    except Exception:
        s = str(v)
        return s[:19] if len(s) > 19 else s


def downsample_series(
    times: list[str],
    values: np.ndarray,
    *,
    max_points: int = 480,
) -> tuple[list[str], np.ndarray, int]:
    """点数超过 max_points 时等距抽稀；返回 (times, values, stride)。"""
    max_points = max(50, int(max_points or 480))
    labels = list(times)
    series = np.asarray(values, dtype=np.float64)
    stride = 1
    if len(labels) > max_points:
        stride = int(np.ceil(len(labels) / max_points))
        labels = labels[::stride]
        series = series[::stride]
    return labels, series, stride


def chart_title(*, variable: str = "", mode: str = "polygon") -> str:
    """对齐 RegionTimeChart：``snow · 选区中位时间序列``。"""
    v = variable or "变量"
    scope = "全场" if mode == "full" else "选区"
    return f"{v} · {scope}中位时间序列"


def median_timeseries(
    cube: np.ndarray,
    times: Sequence[Any],
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
    max_points: int = 480,
    variable: str = "",
    unit: str = "",
    year_start: int | str | None = None,
    year_end: int | str | None = None,
) -> dict[str, Any]:
    """
    计算选区（或全场）空间中位时间序列。

    Args:
        cube: (n_time, n_lat, n_lon)
        times: 长度 n_time 的时间标签或 datetime64
        lat / lon: 1D 坐标轴
        polygon_4326 / bbox_4326: 可选选区；皆空则为全场
        max_points: 输出抽稀上限（默认 480，与 API 一致）

    Returns:
        与 ``POST .../region-timeseries``（reduce=median）核心字段对齐的 dict：
        times / values / reduce / mode / point_count / stride / title / ...
    """
    arr = np.asarray(cube, dtype=np.float32)
    lat1 = np.asarray(lat, dtype=float).ravel()
    lon1 = np.asarray(lon, dtype=float).ravel()
    if arr.ndim != 3:
        raise ValueError(f"cube 需为 (time, lat, lon)，得到 shape={arr.shape}")
    if arr.shape[1] != lat1.size or arr.shape[2] != lon1.size:
        # 容错：偶发 (time, lon, lat)
        if arr.shape[1] == lon1.size and arr.shape[2] == lat1.size:
            arr = np.transpose(arr, (0, 2, 1))
        else:
            raise ValueError(
                f"cube 与坐标不匹配：cube={arr.shape} lat={lat1.size} lon={lon1.size}"
            )
    if len(times) != arr.shape[0]:
        raise ValueError(f"times 长度 {len(times)} 与时间维 {arr.shape[0]} 不一致")

    mask_outside, ring, mode = build_outside_mask(
        lat1, lon1, polygon_4326=polygon_4326, bbox_4326=bbox_4326
    )
    series = spatial_median_series(arr, mask_outside)
    labels = [format_time_label(t) for t in times]
    labels, series, stride = downsample_series(labels, series, max_points=max_points)
    values = [None if not np.isfinite(x) else float(x) for x in series]

    out: dict[str, Any] = {
        "variable": variable or "",
        "unit": unit or "",
        "reduce": REDUCE,
        "mode": mode,
        "year_start": year_start,
        "year_end": year_end,
        "point_count": len(labels),
        "stride": stride,
        "times": labels,
        "values": values,
        "title": chart_title(variable=variable, mode=mode),
    }
    if ring is not None:
        west = min(p[0] for p in ring)
        east = max(p[0] for p in ring)
        south = min(p[1] for p in ring)
        north = max(p[1] for p in ring)
        out["polygon_4326"] = [[p[0], p[1]] for p in ring[:-1]]
        out["bbox_4326"] = [west, south, east, north]
    else:
        out["polygon_4326"] = None
        out["bbox_4326"] = None
    return out


def median_timeseries_from_npy(
    cube_path: str | Path,
    times_path: str | Path,
    lat_path: str | Path,
    lon_path: str | Path,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
    max_points: int = 480,
    variable: str = "",
    unit: str = "",
    year_start: int | str | None = None,
    year_end: int | str | None = None,
) -> dict[str, Any]:
    """
    从 .npy 加载立方体与坐标后计算中位时间序列。

    - ``cube``: (time, lat, lon) float
    - ``times``: 1D，可为 datetime64 或已格式化的字符串 object 数组
    - ``lat`` / ``lon``: 1D
    """
    cube = np.load(Path(cube_path), allow_pickle=True)
    times = np.load(Path(times_path), allow_pickle=True)
    lat = np.load(Path(lat_path))
    lon = np.load(Path(lon_path))
    return median_timeseries(
        cube,
        list(times),
        lat,
        lon,
        polygon_4326=polygon_4326,
        bbox_4326=bbox_4326,
        max_points=max_points,
        variable=variable,
        unit=unit,
        year_start=year_start,
        year_end=year_end,
    )
