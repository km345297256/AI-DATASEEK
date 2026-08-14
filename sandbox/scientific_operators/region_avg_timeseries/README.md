# 区域平均时间序列（region_avg_timeseries）

给其他 Agent / 脚本调用的**独立工具**：Portal「区域平均时间序列分析」

**`{变量} · 选区/全场平均时间序列`**

对每个时间步，在选区（或全场）有效像元上做**空间平均值**（`nanmean`），得到 `times[]` + `values[]`。

对齐：

- 前端工具市场「区域平均时间序列分析」
- 后端 `recipes.timeseries.apply_recipe(..., "spatial_mean")`

| 本工具 | 易混淆的其它工具 |
|--------|------------------|
| **时间序列**：每时刻一个空间平均 | `point_timeseries`：单格点序列 |
| 同族（pangeo2） | `region_max_timeseries` / `region_min_timeseries`（最大/最小） |

---

## 何时使用

- 需要选区随时间变化的**空间平均**曲线
- Agent 已有 `(time, lat, lon)` 立方体，要复现 `spatial_mean`
- 对接一键分析 `plot_type=spatial_mean` 的离线等价计算

**不要**用本工具提取单格点——请用 `tools/point_timeseries`。

---

## 依赖

| 包 | 用途 |
|----|------|
| `numpy` | 必需 |
| `matplotlib` | 仅多边形/bbox 掩膜时需要 |

---

## 目录

```
tools/region_avg_timeseries/
├── README.md      ← 本说明（给 Agent）
├── __init__.py
└── compute.py
```

```python
from tools.region_avg_timeseries import avg_timeseries, avg_timeseries_from_npy
```

仓库根目录需在 `PYTHONPATH` 中。

---

## Agent 调用契约

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `cube` | `(n_time, n_lat, n_lon)` | 时间立方体；NaN/Inf 无效 |
| `times` | 长度 `n_time` | datetime64 或可格式化标签 |
| `lat` / `lon` | 1D | EPSG:4326 坐标轴 |
| `polygon_4326` | `[[lon, lat], ...]` 可选 | ≥3 点；优先于 bbox |
| `bbox_4326` | `[west, south, east, north]` 可选 | 轴对齐框 |
| `max_points` | int，默认 480 | 超过则等距抽稀 |
| `variable` / `unit` / `year_start` / `year_end` | 可选元数据 | 用于 `title` 与回显 |

皆不提供选区 → `mode="full"`（全场平均时间序列）。

### 输出 JSON（稳定字段）

```json
{
  "variable": "temp",
  "unit": "K",
  "reduce": "mean",
  "recipe_id": "spatial_mean",
  "mode": "polygon",
  "point_count": 120,
  "stride": 1,
  "times": ["1951-01-16 00:00:00", "1951-02-15 00:00:00"],
  "values": [273.1, 274.2],
  "title": "temp · 选区平均时间序列",
  "polygon_4326": [[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
  "bbox_4326": [120.0, 45.0, 122.0, 48.0]
}
```

算法（每时间步）：

```text
values[t] = nanmean( cube[t, lat, lon] 在选区内的有效像元 )
```

---

## API 一览

### 1. `avg_timeseries(...)` — 主入口

```python
from tools.region_avg_timeseries import avg_timeseries

out = avg_timeseries(
    cube, times, lat, lon,
    polygon_4326=[[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
    variable="temp",
    unit="K",
)
# out["times"], out["values"], out["title"]
```

### 2. `avg_timeseries_from_npy(...)` — 读 `.npy`

```python
from tools.region_avg_timeseries import avg_timeseries_from_npy

out = avg_timeseries_from_npy(
    "cube.npy", "times.npy", "lat.npy", "lon.npy",
    bbox_4326=[100.0, 30.0, 130.0, 50.0],
    variable="temp",
)
```

### 3. 底层辅助

| 函数 | 作用 |
|------|------|
| `spatial_mean_series(cube, mask_outside)` | `(T,Y,X) → (T,)` |
| `build_outside_mask(lat, lon, ...)` | 选区 → 区外掩膜 |
| `chart_title(variable=..., mode=...)` | UI 标题字符串 |

---

## 与现有系统的关系

```
Portal 工具市场「区域平均时间序列分析」
        │
        ▼
POST /api/v1/analyze/...  (plot_type=spatial_mean)
        │
        ▼
recipes.timeseries.apply_recipe(..., "spatial_mean")
        │
        └── 本工具封装「立方体 → 平均序列」核心；不拉 NetCDF、不依赖 job_id
```

---

## 最小自检示例

```python
import numpy as np
from tools.region_avg_timeseries import avg_timeseries

times = np.array(["1951-01-16", "1951-02-15", "1951-03-16"], dtype="datetime64[D]")
lat = np.array([45.0, 46.0, 47.0])
lon = np.array([118.0, 119.0, 120.0])
cube = np.stack([
    np.ones((3, 3), dtype=float),
    np.ones((3, 3), dtype=float) * 2,
    np.ones((3, 3), dtype=float) * 3,
]).astype(np.float32)

out = avg_timeseries(
    cube, times, lat, lon,
    bbox_4326=[117.5, 44.5, 120.5, 47.5],
    variable="temp",
)
assert out["reduce"] == "mean"
assert out["values"] == [1.0, 2.0, 3.0]
assert "平均时间序列" in out["title"]
print(out["title"], out["point_count"])
```

---

## 版本

- `__version__` = `1.0.0`
- 稳定符号见 `__all__`；`REDUCE` 恒为 `"mean"`；`RECIPE_ID` = `"spatial_mean"`。
