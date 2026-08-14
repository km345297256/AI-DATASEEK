"""栅格中位值工具：在全场或选区内求 median（p50）。

逻辑对齐 launch-api `tpdc_batch._field_stats` 中的 ``p50`` 字段
（``np.nanpercentile(valid, 50)``），对应选区统计语境下的中位值。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Geometry helpers（与 tpdc_batch 多边形约定一致：EPSG:4326，[lon, lat]）
# ---------------------------------------------------------------------------


def normalize_polygon_4326(polygon: list[Any]) -> list[tuple[float, float]]:
    """将 [[lon, lat], ...] 规范为闭合环（首尾相同），至少 3 个有效顶点。"""
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


def mask_region(
    field: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
) -> tuple[np.ndarray, list[tuple[float, float]], str]:
    """
    用多边形或 bbox 掩膜 2D 场（区外为 NaN）。

    Returns:
        masked_field, closed_ring, mode ('polygon' | 'bbox')
    """
    from matplotlib.path import Path as MplPath

    flat = np.asarray(field, dtype=float)
    lat1 = np.asarray(lat, dtype=float).ravel()
    lon1 = np.asarray(lon, dtype=float).ravel()
    if flat.ndim != 2 or lat1.size != flat.shape[0] or lon1.size != flat.shape[1]:
        raise ValueError(
            f"栅格与坐标维度不匹配：field={flat.shape} lat={lat1.size} lon={lon1.size}"
        )

    if polygon_4326:
        ring = normalize_polygon_4326(polygon_4326)
        mode = "polygon"
    elif bbox_4326:
        ring = polygon_from_bbox(bbox_4326)
        mode = "bbox"
    else:
        raise ValueError("请提供 polygon_4326 或 bbox_4326")

    lon2d, lat2d = np.meshgrid(lon1, lat1)
    pts = np.column_stack([lon2d.ravel(), lat2d.ravel()])
    inside = MplPath(ring, closed=True).contains_points(pts).reshape(flat.shape)
    masked = np.where(inside, flat, np.nan)
    return masked, ring, mode


# ---------------------------------------------------------------------------
# Core: median (p50)
# ---------------------------------------------------------------------------


def find_median(
    field: np.ndarray,
    lat: np.ndarray | None = None,
    lon: np.ndarray | None = None,
) -> dict[str, Any]:
    """
    在 2D 栅格有效像元上求中位值（50 分位）。

    ``lat`` / ``lon`` 可选：全场统计不依赖坐标；保留参数以便与
    ``find_min`` / ``find_max`` 调用签名一致。若传入则校验尺寸。

    算法与 ``_field_stats`` 一致：``np.nanpercentile(valid, 50)``。

    Returns:
        {
          "median": float | None,   # 主字段（Agent 友好名）
          "p50": float | None,      # 与后端 layer_stats / region-stats 同名
          "valid_count": int,
        }
    """
    flat = np.asarray(field, dtype=float)
    if lat is not None and lon is not None:
        lat1 = np.asarray(lat, dtype=float).ravel()
        lon1 = np.asarray(lon, dtype=float).ravel()
        if flat.ndim == 2 and (lat1.size != flat.shape[0] or lon1.size != flat.shape[1]):
            raise ValueError(
                f"栅格与坐标维度不匹配：field={flat.shape} lat={lat1.size} lon={lon1.size}"
            )

    valid = flat[np.isfinite(flat)]
    if valid.size == 0:
        return {
            "median": None,
            "p50": None,
            "valid_count": 0,
        }

    p50 = float(np.nanpercentile(valid, 50))
    return {
        "median": p50,
        "p50": p50,
        "valid_count": int(valid.size),
    }


def find_median_in_region(
    field: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
) -> dict[str, Any]:
    """
    在多边形或 bbox 选区内求中位值。

    Returns:
        {
          "mode": "polygon" | "bbox",
          "polygon_4326": [[lon, lat], ...],
          "bbox_4326": [west, south, east, north],
          "median": float | None,
          "p50": float | None,
          "valid_count": int,
        }
    """
    masked, ring, mode = mask_region(
        field, lat, lon, polygon_4326=polygon_4326, bbox_4326=bbox_4326
    )
    result = find_median(masked, lat, lon)
    west = min(p[0] for p in ring)
    east = max(p[0] for p in ring)
    south = min(p[1] for p in ring)
    north = max(p[1] for p in ring)
    return {
        "mode": mode,
        "polygon_4326": [[p[0], p[1]] for p in ring[:-1]],
        "bbox_4326": [west, south, east, north],
        **result,
    }


def find_median_from_npy(
    field_path: str | Path,
    lat_path: str | Path,
    lon_path: str | Path,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
) -> dict[str, Any]:
    """
    从 .npy 文件加载栅格后求中位值。

    典型用途：TPDC 任务目录下的 ``median.npy`` / ``lat.npy`` / ``lon.npy``
    （或在 max.npy / min.npy 图层上再求空间中位）。
    若提供 polygon/bbox，则只在选区内求；否则全场。
    """
    field = np.asarray(np.load(Path(field_path)), dtype=float)
    lat = np.asarray(np.load(Path(lat_path)), dtype=float)
    lon = np.asarray(np.load(Path(lon_path)), dtype=float)
    if polygon_4326 or bbox_4326:
        return find_median_in_region(
            field, lat, lon, polygon_4326=polygon_4326, bbox_4326=bbox_4326
        )
    out = find_median(field, lat, lon)
    out["mode"] = "full"
    out["polygon_4326"] = None
    out["bbox_4326"] = None
    return out


def format_median_value(value: float | None, *, precision: int = 2) -> str:
    """科学计数或短小数，对齐 Portal ``formatStatValue``。无值时返回 ``—``。"""
    if value is None or not np.isfinite(value):
        return "—"
    n = float(value)
    if abs(n) >= 1000 or (abs(n) > 0 and abs(n) < 0.01):
        return f"{n:.{precision}e}"
    return f"{float(f'{n:.4g}')}"
