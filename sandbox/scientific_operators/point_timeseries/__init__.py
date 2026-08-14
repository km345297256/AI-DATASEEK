"""单格点序列 / 剖面（point_timeseries）

固定经纬度（或其它维度）索引，提取时间序列或沿末维剖面。
对应 UI：``单格点序列/剖面分析``；recipe_id = ``point``。
"""

from .compute import (
    RECIPE_ID,
    chart_title,
    downsample_series,
    format_time_label,
    nearest_index,
    point_profile,
    point_timeseries,
    point_timeseries_from_npy,
)

__all__ = [
    "RECIPE_ID",
    "chart_title",
    "downsample_series",
    "format_time_label",
    "nearest_index",
    "point_profile",
    "point_timeseries",
    "point_timeseries_from_npy",
]

__version__ = "1.0.0"
