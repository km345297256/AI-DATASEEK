# 选区最大时间序列（region_max_timeseries）

给其他 Agent / 脚本调用的**独立工具**：Portal 折线图

**`{变量} · 选区最大时间序列`**

（与「选区中位时间序列」同属区域时间序列能力，本包固定 `reduce=max`）。

对每个时间步，在选区（或全场）有效像元上做**空间最大值**（`nanmax`），得到 `times[]` + `values[]`。

对齐：

- 前端 `RegionTimeChart.vue`（`reduce=max` → 标题含「最大时间序列」）
- 后端 `tpdc_batch.region_timeseries(..., reduce="max")` → `_reduce_timeseries_cube`

| 本工具 | 易混淆的其它工具 |
|--------|------------------|
| **时间序列**：每时刻一个空间最大 | `region_stats_max_value`：单层 2D 场的 max + 经纬度 |
| 同族 | `region_median_timeseries`（中位）、后续若有 min 系列 |

---

## 何时使用

- 需要选区随时间变化的**空间最大**曲线
- Agent 已有 `(time, lat, lon)` 立方体，要复现 `reduce=max` 的序列
- 对接 `POST /api/v1/tpdc/jobs/{id}/region-timeseries` 且 `reduce=max` 的离线等价计算

**不要**用本工具求单层栅格最大值位置——请用 `tools/region_stats_max_value`。

---

## 依赖

| 包 | 用途 |
|----|------|
| `numpy` | 必需 |
| `matplotlib` | 仅多边形/bbox 掩膜时需要 |

---

## 目录

```
tools/region_max_timeseries/
├── README.md      ← 本说明（给 Agent）
├── __init__.py
└── compute.py
```

```python
from tools.region_max_timeseries import max_timeseries, max_timeseries_from_npy
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
| `max_points` | int，默认 480 | 超过则等距抽稀（与 API 一致，下限 50） |
| `variable` / `unit` / `year_start` / `year_end` | 可选元数据 | 用于 `title` 与回显 |

皆不提供选区 → `mode="full"`（全场最大时间序列）。

### 输出 JSON（稳定字段）

```json
{
  "variable": "snow",
  "unit": "kg m-2 s-1",
  "reduce": "max",
  "mode": "polygon",
  "year_start": 1951,
  "year_end": 2025,
  "point_count": 444,
  "stride": 1,
  "times": ["1951-01-16 10:30:00", "1951-02-15 10:30:00"],
  "values": [3.1e-6, 4.2e-6],
  "title": "snow · 选区最大时间序列",
  "polygon_4326": [[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
  "bbox_4326": [120.0, 45.0, 122.0, 48.0]
}
```

| 字段 | 说明 |
|------|------|
| `reduce` | 固定 `"max"` |
| `mode` | `full` / `polygon` / `bbox` |
| `times` / `values` | 等长；无效步 `values[i]=null` |
| `title` | 如 `snow · 选区最大时间序列` |

算法（每时间步）：

```text
values[t] = nanmax( cube[t, lat, lon] 在选区内的有效像元 )
```

---

## API 一览

### 1. `max_timeseries(...)` — 主入口

```python
from tools.region_max_timeseries import max_timeseries

out = max_timeseries(
    cube, times, lat, lon,
    polygon_4326=[[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
    variable="snow",
    unit="kg m-2 s-1",
    year_start=1951,
    year_end=2025,
)
# out["times"], out["values"], out["title"]
```

### 2. `max_timeseries_from_npy(...)` — 读 `.npy`

```python
from tools.region_max_timeseries import max_timeseries_from_npy

out = max_timeseries_from_npy(
    "cube.npy", "times.npy", "lat.npy", "lon.npy",
    bbox_4326=[100.0, 30.0, 130.0, 50.0],
    variable="snow",
)
```

### 3. 底层辅助

| 函数 | 作用 |
|------|------|
| `spatial_max_series(cube, mask_outside)` | `(T,Y,X) → (T,)` |
| `build_outside_mask(lat, lon, ...)` | 选区 → 区外掩膜 |
| `chart_title(variable=..., mode=...)` | UI 标题字符串 |
| `format_time_label(v)` / `downsample_series(...)` | 时间格式与抽稀 |

---

## 与现有系统的关系

```
Portal RegionTimeChart（reduce=max）
  「snow · 选区最大时间序列」
        │
        ▼
POST .../region-timeseries  (reduce=max)
        │
        ▼
tpdc_batch._reduce_timeseries_cube(..., "max")
        │
        └── 本工具封装「立方体 → 最大序列」核心；不拉 NetCDF、不依赖 job_id
```

---

## 错误行为

| 情况 | 行为 |
|------|------|
| cube 非 3D / 与 lat·lon 不匹配 | `ValueError` |
| times 长度 ≠ 时间维 | `ValueError` |
| 多边形 &lt; 3 点 / bbox 非法 | `ValueError` |
| 选区内无网格 | `ValueError` |
| 某时间步全 NaN | 该步 `values[i] = null` |

---

## 最小自检示例

```python
import numpy as np
from tools.region_max_timeseries import max_timeseries

times = np.array(["1951-01-16", "1951-02-15", "1951-03-16"], dtype="datetime64[D]")
lat = np.array([45.0, 46.0, 47.0])
lon = np.array([118.0, 119.0, 120.0])
cube = np.stack([
    np.arange(9, dtype=float).reshape(3, 3),           # max=8
    np.arange(9, dtype=float).reshape(3, 3) + 10,      # max=18
    np.arange(9, dtype=float).reshape(3, 3) + 20,      # max=28
]).astype(np.float32)

out = max_timeseries(
    cube, times, lat, lon,
    bbox_4326=[117.5, 44.5, 120.5, 47.5],
    variable="snow",
)
assert out["reduce"] == "max"
assert out["values"] == [8.0, 18.0, 28.0]
assert "选区最大时间序列" in out["title"]
print(out["title"], out["point_count"])
```

---

## 版本

- `__version__` = `1.0.0`
- 稳定符号见 `__all__`；`REDUCE` 恒为 `"max"`。
