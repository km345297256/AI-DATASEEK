"""选区统计 · 最大值（region_stats_max_value）

从 2D 栅格（全场或选区）提取最大值及其经纬度位置。
对应 Portal「选区统计」面板中的「最大」与「最大值位置」。
"""

from .compute import (
    find_max,
    find_max_from_npy,
    find_max_in_region,
    format_max_location,
    format_max_value,
    mask_region,
    normalize_polygon_4326,
    polygon_from_bbox,
)

__all__ = [
    "find_max",
    "find_max_from_npy",
    "find_max_in_region",
    "format_max_location",
    "format_max_value",
    "mask_region",
    "normalize_polygon_4326",
    "polygon_from_bbox",
]

__version__ = "1.0.0"
