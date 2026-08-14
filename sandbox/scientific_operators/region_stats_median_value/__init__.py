"""选区统计 · 中位值（region_stats_median_value）

从 2D 栅格（全场或选区）提取中位值（50 分位 / p50）。
对齐后端 ``_field_stats`` 的 ``p50`` 字段；对应 Portal「选区统计」。
"""

from .compute import (
    find_median,
    find_median_from_npy,
    find_median_in_region,
    format_median_value,
    mask_region,
    normalize_polygon_4326,
    polygon_from_bbox,
)

__all__ = [
    "find_median",
    "find_median_from_npy",
    "find_median_in_region",
    "format_median_value",
    "mask_region",
    "normalize_polygon_4326",
    "polygon_from_bbox",
]

__version__ = "1.0.0"
