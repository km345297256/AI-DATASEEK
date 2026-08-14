"""选区统计 · 最小值（region_stats_min_value）

从 2D 栅格（全场或选区）提取最小值及其经纬度位置。
对应 Portal「选区统计」面板中的「最小」（后端字段 `min` / `min_location`）。
"""

from .compute import (
    find_min,
    find_min_from_npy,
    find_min_in_region,
    format_min_location,
    format_min_value,
    mask_region,
    normalize_polygon_4326,
    polygon_from_bbox,
)

__all__ = [
    "find_min",
    "find_min_from_npy",
    "find_min_in_region",
    "format_min_location",
    "format_min_value",
    "mask_region",
    "normalize_polygon_4326",
    "polygon_from_bbox",
]

__version__ = "1.0.0"
