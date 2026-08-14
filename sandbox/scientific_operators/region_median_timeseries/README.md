# 选区中位时间序列（region_median_timeseries）

给其他 Agent / 脚本调用的**独立工具**：Portal 折线图

**`{变量} · 选区中位时间序列`**

（例如截图中的 `snow · 选区中位时间序列 · 1951–2025`）。

对每个时间步，在选区（或全场）有效像元上做**空间中位数**（`nanmedian`），得到 `times[]` + `values[]`。

对齐：

- 前端 `RegionTimeChart.vue`（`reduce=median`）
- 后端 `tpdc_batch.region_timeseries(..., reduce="median")` → `_reduce_timeseries_cube`

| 本工具 | 易混淆的其它工具 |
|--------|------------------|
| **时间序列**：每时刻一个中位值 | `region_stats_median_value`：单层 2D 场的空间 p50（标量） |

---

## 何时使用

- 需要选区随时间变化的中位曲线（绘图 / 分析）
- Agent 已有 `(time, lat, lon)` 立方体，要复现 UI 同款序列
- 对接 `POST /api/v1/tpdc/jobs/{id}/region-timeseries` 且 `reduce=median` 的离线等价计算

**不要**用本工具算单层栅格的 p50——请用 `tools/region_stats_median_value`。

---

## 依赖

| 包 | 用途 |
|----|------|
| `numpy` | 必需 |
| `matplotlib` | 仅多边形/bbox 掩膜时需要 |

---

## 目录

```
tools/region_median_timeseries/
├── README.md      ← 本说明（给 Agent）
├── __init__.py
└── compute.py
```

```python
from tools.region_median_timeseries import median_timeseries, median_timeseries_from_npy
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

皆不提供选区 → `mode="full"`（全场中位时间序列）。

### 输出 JSON（稳定字段）

```json
{
  "variable": "snow",
  "unit": "kg m-2 s-1",
  "reduce": "median",
  "mode": "polygon",
  "year_start": 1951,
  "year_end": 2025,
  "point_count": 444,
  "stride": 1,
  "times": ["1951-01-16 10:30:00", "1951-02-15 10:30:00"],
  "values": [1.2e-6, 2.3e-6],
  "title": "snow · 选区中位时间序列",
  "polygon_4326": [[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
  "bbox_4326": [120.0, 45.0, 122.0, 48.0]
}
```

| 字段 | 说明 |
|------|------|
| `reduce` | 固定 `"median"` |
| `mode` | `full` / `polygon` / `bbox` |
| `times` | 字符串时间标签列表 |
| `values` | 与 times 等长；无效步为 `null` |
| `point_count` / `stride` | 抽稀后点数与步长 |
| `title` | 与 UI 标题同款文案 |

算法（每时间步）：

```text
values[t] = nanmedian( cube[t, lat, lon] 在选区内的有效像元 )
```

---

## API 一览

### 1. `median_timeseries(...)` — 主入口

```python
from tools.region_median_timeseries import median_timeseries

out = median_timeseries(
    cube, times, lat, lon,
    polygon_4326=[[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
    variable="snow",
    unit="kg m-2 s-1",
    year_start=1951,
    year_end=2025,
)
# out["times"], out["values"], out["title"]
```

### 2. `median_timeseries_from_npy(...)` — 读 `.npy`

```python
from tools.region_median_timeseries import median_timeseries_from_npy

out = median_timeseries_from_npy(
    "cube.npy", "times.npy", "lat.npy", "lon.npy",
    bbox_4326=[100.0, 30.0, 130.0, 50.0],
    variable="snow",
)
```

### 3. 底层辅助

| 函数 | 作用 |
|------|------|
| `spatial_median_series(cube, mask_outside)` | `(T,Y,X) → (T,)` |
| `build_outside_mask(lat, lon, ...)` | 选区 → 区外掩膜 |
| `chart_title(variable=..., mode=...)` | UI 标题字符串 |
| `format_time_label(v)` / `downsample_series(...)` | 时间格式与抽稀 |

---

## 与现有系统的关系

```
Portal RegionTimeChart
  「snow · 选区中位时间序列」
        │
        ▼
POST /api/v1/tpdc/jobs/{id}/region-timeseries  (reduce=median)
        │
        ▼
tpdc_batch.region_timeseries → _reduce_timeseries_cube(..., "median")
        │
        └── 本工具封装「立方体 → 中位序列」核心；不拉 NetCDF、不依赖 job_id
```

若 Agent 只有 `job_id`：应调上述 HTTP API，或自行打开任务源文件组成 `cube` 后再调用本工具。

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
from tools.region_median_timeseries import median_timeseries

times = np.array(["1951-01-16", "1951-02-15", "1951-03-16"], dtype="datetime64[D]")
lat = np.array([45.0, 46.0, 47.0])
lon = np.array([118.0, 119.0, 120.0])
# 每层常数场：中位 = 该常数
cube = np.stack([
    np.full((3, 3), 1.0),
    np.full((3, 3), 2.0),
    np.full((3, 3), 3.0),
]).astype(np.float32)

out = median_timeseries(
    cube, times, lat, lon,
    bbox_4326=[117.5, 44.5, 120.5, 47.5],
    variable="snow",
)
assert out["reduce"] == "median"
assert out["values"] == [1.0, 2.0, 3.0]
assert "选区中位时间序列" in out["title"]
print(out["title"], out["point_count"])
```

---

## 版本

- `__version__` = `1.0.0`
- 稳定符号见 `__all__`；`REDUCE` 恒为 `"median"`。
