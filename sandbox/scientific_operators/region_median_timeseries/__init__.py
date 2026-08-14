"""选区中位时间序列（region_median_timeseries）

每个时间步对选区做空间中位数，得到与 Portal 折线图一致的时间序列。
对应 UI：``{变量} · 选区中位时间序列``（如 snow · 选区中位时间序列）。
"""

from .compute import (
    REDUCE,
    build_outside_mask,
    chart_title,
    downsample_series,
    format_time_label,
    median_timeseries,
    median_timeseries_from_npy,
    normalize_polygon_4326,
    polygon_from_bbox,
    spatial_median_series,
)

__all__ = [
    "REDUCE",
    "build_outside_mask",
    "chart_title",
    "downsample_series",
    "format_time_label",
    "median_timeseries",
    "median_timeseries_from_npy",
    "normalize_polygon_4326",
    "polygon_from_bbox",
    "spatial_median_series",
]

__version__ = "1.0.0"
