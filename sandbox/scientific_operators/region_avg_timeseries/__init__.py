"""选区平均时间序列（region_avg_timeseries）

每个时间步对选区做空间平均值，得到与 Portal「区域平均时间序列分析」一致的时间序列。
对应 UI：``{变量} · 选区/全场平均时间序列``；recipe_id = ``spatial_mean``。
"""

from .compute import (
    RECIPE_ID,
    REDUCE,
    avg_timeseries,
    avg_timeseries_from_npy,
    build_outside_mask,
    chart_title,
    downsample_series,
    format_time_label,
    normalize_polygon_4326,
    polygon_from_bbox,
    spatial_mean_series,
)

__all__ = [
    "RECIPE_ID",
    "REDUCE",
    "avg_timeseries",
    "avg_timeseries_from_npy",
    "build_outside_mask",
    "chart_title",
    "downsample_series",
    "format_time_label",
    "normalize_polygon_4326",
    "polygon_from_bbox",
    "spatial_mean_series",
]

__version__ = "1.0.0"
