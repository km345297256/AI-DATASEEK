"""沿末维剖面（last_dim_profile）

对除最后一维（及可选 time）外求平均，得到沿末维的剖面曲线。
对应 UI：``沿末维剖面分析``；recipe_id = ``reduce_last_dim``。
"""

from .compute import (
    RECIPE_ID,
    REDUCE,
    chart_title,
    downsample_series,
    format_label,
    last_dim_profile,
    last_dim_profile_from_npy,
    reduce_to_last_dim,
)

__all__ = [
    "RECIPE_ID",
    "REDUCE",
    "chart_title",
    "downsample_series",
    "format_label",
    "last_dim_profile",
    "last_dim_profile_from_npy",
    "reduce_to_last_dim",
]

__version__ = "1.0.0"
