"""栅格最小值工具：在全场或选区内求 min，并返回极值经纬度。

逻辑对齐 launch-api `tpdc_batch._field_stats` / `region_layer_stats`
中「最小」「min_location」字段（对应 Portal 选区统计 KPI「最小」）。
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
# Core: min + location
# ---------------------------------------------------------------------------


def find_min(
    field: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
) -> dict[str, Any]:
    """
    在 2D 栅格上求最小值及位置。

    约定：field 形状为 (n_lat, n_lon)，lat / lon 为对应 1D 坐标轴。
    无效像元（NaN / Inf）忽略。无有效像元时 min / min_location 为 None。

    Returns:
        {
          "min": float | None,
          "min_location": {"lat": float, "lon": float} | None,
          "valid_count": int,
          "index": {"j": int, "i": int} | None,  # (lat_idx, lon_idx)
        }
    """
    flat = np.asarray(field, dtype=float)
    lat1 = np.asarray(lat, dtype=float).ravel()
    lon1 = np.asarray(lon, dtype=float).ravel()

    valid = flat[np.isfinite(flat)]
    if valid.size == 0:
        return {
            "min": None,
            "min_location": None,
            "valid_count": 0,
            "index": None,
        }

    amin = float(np.nanmin(flat))
    min_loc = None
    index = None
    try:
        if flat.ndim == 2 and lat1.ndim == 1 and lon1.ndim == 1:
            if lat1.size == flat.shape[0] and lon1.size == flat.shape[1]:
                jj, ii = np.unravel_index(np.nanargmin(flat), flat.shape)
                min_loc = {"lat": float(lat1[jj]), "lon": float(lon1[ii])}
                index = {"j": int(jj), "i": int(ii)}
    except Exception:
        pass

    return {
        "min": amin,
        "min_location": min_loc,
        "valid_count": int(valid.size),
        "index": index,
    }


def find_min_in_region(
    field: np.ndarray,
    lat: np.ndarray,
    lon: np.ndarray,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
) -> dict[str, Any]:
    """
    在多边形或 bbox 选区内求最小值（对应 UI「选区统计 → 最小」）。

    Returns（与 region-stats 相关字段对齐，仅保留最小值语义）:
        {
          "mode": "polygon" | "bbox",
          "polygon_4326": [[lon, lat], ...],  # 不含闭合重复点
          "bbox_4326": [west, south, east, north],
          "min": float | None,
          "min_location": {"lat": float, "lon": float} | None,
          "valid_count": int,
          "index": {"j": int, "i": int} | None,
        }
    """
    masked, ring, mode = mask_region(
        field, lat, lon, polygon_4326=polygon_4326, bbox_4326=bbox_4326
    )
    result = find_min(masked, lat, lon)
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


def find_min_from_npy(
    field_path: str | Path,
    lat_path: str | Path,
    lon_path: str | Path,
    *,
    polygon_4326: list[Any] | None = None,
    bbox_4326: list[float] | None = None,
) -> dict[str, Any]:
    """
    从 .npy 文件加载栅格后求最小值。

    典型用途：TPDC 任务目录下的 ``min.npy`` / ``lat.npy`` / ``lon.npy``
    （或 max.npy / median.npy 图层上再求空间最小）。
    若提供 polygon/bbox，则只在选区内求最小；否则全场。
    """
    field = np.asarray(np.load(Path(field_path)), dtype=float)
    lat = np.asarray(np.load(Path(lat_path)), dtype=float)
    lon = np.asarray(np.load(Path(lon_path)), dtype=float)
    if polygon_4326 or bbox_4326:
        return find_min_in_region(
            field, lat, lon, polygon_4326=polygon_4326, bbox_4326=bbox_4326
        )
    out = find_min(field, lat, lon)
    out["mode"] = "full"
    out["polygon_4326"] = None
    out["bbox_4326"] = None
    return out


def format_min_location(loc: dict[str, float] | None, *, digits: int = 2) -> str:
    """格式化为 UI 同款文案，如 ``47.15°N, 120.05°E``。无位置时返回 ``—``。"""
    if not loc:
        return "—"
    return f"{float(loc['lat']):.{digits}f}°N, {float(loc['lon']):.{digits}f}°E"


def format_min_value(value: float | None, *, precision: int = 2) -> str:
    """科学计数或短小数，对齐 Portal ``formatStatValue``。无值时返回 ``—``。"""
    if value is None or not np.isfinite(value):
        return "—"
    n = float(value)
    if abs(n) >= 1000 or (abs(n) > 0 and abs(n) < 0.01):
        return f"{n:.{precision}e}"
    return f"{float(f'{n:.4g}')}"
