# 单格点序列 / 剖面（point_timeseries）

给其他 Agent / 脚本调用的**独立工具**：Portal「单格点序列/剖面分析」

提取单个经纬度格点的时间序列，或固定前 N−1 维后沿末维做剖面。

对齐：

- 前端工具市场「单格点序列/剖面分析」
- 后端 `recipes.timeseries.apply_recipe(..., "point")`

| 本工具 | 易混淆的其它工具 |
|--------|------------------|
| **单格点**：固定 lat/lon 索引 | `region_avg_timeseries`：选区空间平均 |
| 剖面 | `last_dim_profile`：对空间维求平均后沿末维剖面 |

---

## 何时使用

- 定点对比：给定格点索引或经纬度，看变量随时间变化
- 无时间维时：固定空间索引，沿光谱/深度等末维剖面
- 对接一键分析 `plot_type=point` 的离线等价计算

---

## 依赖

| 包 | 用途 |
|----|------|
| `numpy` | 必需 |

---

## 目录

```
tools/point_timeseries/
├── README.md
├── __init__.py
└── compute.py
```

```python
from tools.point_timeseries import point_timeseries, point_profile, point_timeseries_from_npy
```

仓库根目录需在 `PYTHONPATH` 中。

---

## Agent 调用契约

### A. 时间立方体单格点（主场景）

| 字段 | 类型 | 说明 |
|------|------|------|
| `cube` | `(n_time, n_lat, n_lon)` | 时间立方体 |
| `times` | 长度 `n_time` | 时间标签 |
| `lat` / `lon` | 1D | 坐标轴 |
| `lat_index` / `lon_index` | int 可选 | 格点索引（优先） |
| `lat_value` / `lon_value` | float 可选 | 经纬度 → 最近格点 |
| `variable` / `unit` | 可选 | 元数据 |

### 输出

```json
{
  "variable": "temp",
  "unit": "K",
  "recipe_id": "point",
  "mode": "point",
  "lat_index": 10,
  "lon_index": 20,
  "lat": 45.0,
  "lon": 120.0,
  "indexers": {"lat": 10, "lon": 20},
  "times": ["1951-01-16 00:00:00"],
  "values": [273.1],
  "title": "temp（lat=10, lon=20）"
}
```

### B. 通用剖面 `point_profile`

固定 `indexers`，保留唯一剩余维作为剖面轴。

---

## API 一览

```python
from tools.point_timeseries import point_timeseries

out = point_timeseries(
    cube, times, lat, lon,
    lat_value=45.0,
    lon_value=120.0,
    variable="temp",
)
# out["times"], out["values"], out["lat"], out["lon"]
```

```python
from tools.point_timeseries import point_profile

out = point_profile(
    data,  # (band, y, x) 或任意 N 维
    indexers={"y": 3, "x": 5},
    dim_names=["band", "y", "x"],
    axis_coords=np.arange(data.shape[0]),
    variable="reflectance",
    remaining_dim="band",
)
```

---

## 与现有系统的关系

```
Portal「单格点序列/剖面分析」
        │
        ▼
plot_type=point  +  dim_lat / dim_lon ...
        │
        ▼
recipes.timeseries.apply_recipe(..., "point")
        │
        └── 本工具：numpy 立方体/数组 → 序列；不打开 NetCDF
```

---

## 最小自检示例

```python
import numpy as np
from tools.point_timeseries import point_timeseries

times = np.array(["1951-01-16", "1951-02-15"], dtype="datetime64[D]")
lat = np.array([45.0, 46.0])
lon = np.array([118.0, 119.0])
cube = np.arange(8, dtype=float).reshape(2, 2, 2)

out = point_timeseries(cube, times, lat, lon, lat_index=1, lon_index=0, variable="v")
assert out["values"] == [2.0, 6.0]
assert out["recipe_id"] == "point"
print(out["title"], out["values"])
```

---

## 版本

- `__version__` = `1.0.0`
- `RECIPE_ID` = `"point"`
