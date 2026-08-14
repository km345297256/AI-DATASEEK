# 选区统计 · 中位值（region_stats_median_value）

给其他 Agent / 脚本调用的**独立工具**：Portal「**选区统计**」中的**中位值**（`p50`）。

在 2D 地理栅格（全场或多边形/bbox 选区）上求 **median / p50**。

| 语义 | 工具输出 |
|------|----------|
| 中位值 | `median`（主字段） |
| 与后端同名 | `p50`（与 `median` 同值） |

实现与 `launch-api/app/tpdc_batch.py` 中 `_field_stats` 一致：

```text
p50 = np.nanpercentile(valid_finite_pixels, 50)
```

对称工具：`tools/region_stats_max_value`、`tools/region_stats_min_value`。中位值是分布统计，**不**附带单一经纬度位置。

> 注意：本工具是**空间选区内像元的中位数**，不是 TPDC 批量任务里「时间维 median 图层」的生成逻辑。后者产出的 `median.npy` 可作为本工具的输入场，再在选区上求空间中位。

---

## 何时使用

- 需要选区或全场有效像元的中位值（p50）
- 已有任务产物 `*.npy`，要快速算空间中位
- Agent 需要与 `region-stats` 返回的 `stats.p50` 对齐的离线结果

**不要**用本工具算均值、标准差、最小/最大——见其它 `tools/*_value` 包。

---

## 依赖

| 包 | 用途 |
|----|------|
| `numpy` | 必需 |
| `matplotlib` | 仅选区掩膜（`find_median_in_region` / `mask_region`）时需要 |

---

## 目录

```
tools/region_stats_median_value/
├── README.md      ← 本说明（给 Agent）
├── __init__.py
└── compute.py
```

仓库根目录需在 `PYTHONPATH` 中：

```python
from tools.region_stats_median_value import find_median, find_median_in_region, find_median_from_npy
```

---

## Agent 调用契约

### 输入约定

| 字段 | 类型 | 说明 |
|------|------|------|
| `field` | `np.ndarray` `(n_lat, n_lon)` | 2D 数值场；NaN/Inf 无效 |
| `lat` | 1D `n_lat` | 纬度；选区 API 必需，全场可选 |
| `lon` | 1D `n_lon` | 经度；选区 API 必需，全场可选 |
| `polygon_4326` | `[[lon, lat], ...]` 可选 | ≥3 点；优先于 bbox |
| `bbox_4326` | `[west, south, east, north]` 可选 | 轴对齐框 |

坐标顺序：**先 lon 后 lat**（EPSG:4326）。

### 输出 JSON 形状

```json
{
  "median": 9.29e-7,
  "p50": 9.29e-7,
  "valid_count": 1280,
  "mode": "polygon",
  "polygon_4326": [[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
  "bbox_4326": [120.0, 45.0, 122.0, 48.0]
}
```

| 字段 | 说明 |
|------|------|
| `median` | 中位值；无有效像元为 `null` |
| `p50` | 与 `median` 相同，便于对接后端字段名 |
| `valid_count` | 有效像元数 |
| `mode` | `full` / `polygon` / `bbox`（`find_median` 本身可不含） |
| `polygon_4326` / `bbox_4326` | 选区回显 |

空数据：

```json
{ "median": null, "p50": null, "valid_count": 0 }
```

---

## API 一览

### 1. `find_median(field, lat=None, lon=None)` — 全场中位

```python
from tools.region_stats_median_value import find_median

out = find_median(field, lat, lon)
# out["median"] 或 out["p50"]
```

### 2. `find_median_in_region(...)` — 选区内中位

```python
from tools.region_stats_median_value import find_median_in_region

out = find_median_in_region(
    field, lat, lon,
    polygon_4326=[[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
)
# 或
out = find_median_in_region(field, lat, lon, bbox_4326=[120.0, 45.0, 122.0, 48.0])
```

### 3. `find_median_from_npy(...)` — 读任务 `.npy`

```python
from tools.region_stats_median_value import find_median_from_npy

out = find_median_from_npy(
    "data/tpdc_jobs/<job_id>/median.npy",
    "data/tpdc_jobs/<job_id>/lat.npy",
    "data/tpdc_jobs/<job_id>/lon.npy",
    bbox_4326=[...],  # 可选
)
```

### 4. 展示辅助

```python
from tools.region_stats_median_value import format_median_value

format_median_value(9.29e-7)  # → "9.29e-07" 类科学计数
```

---

## 与现有系统的关系

```
选区统计 / region-stats → stats.p50
        │
        ▼
tpdc_batch._field_stats 的 p50 分支
        │
        └── 本工具单独封装
```

不发起 HTTP、不依赖 `job_id`。若只有任务 ID：读 `median.npy`（或当前图层 `max.npy` / `min.npy`）+ `lat.npy` + `lon.npy`，再调用 `find_median_from_npy`。

---

## 错误行为

| 情况 | 行为 |
|------|------|
| 多边形顶点 &lt; 3 | `ValueError` |
| bbox 长度 &lt; 4 | `ValueError` |
| field 与 lat/lon 尺寸不一致 | `ValueError` |
| 未提供选区却调用 `find_median_in_region` | `ValueError` |
| 区内全 NaN | `median=null`，不抛异常 |

---

## 最小自检示例

```python
import numpy as np
from tools.region_stats_median_value import find_median, find_median_in_region

lat = np.array([45.0, 46.0, 47.0, 48.0])
lon = np.array([118.0, 119.0, 120.0, 121.0])
field = np.arange(16, dtype=float).reshape(4, 4)

full = find_median(field, lat, lon)
assert full["median"] == full["p50"]
assert abs(full["median"] - 7.5) < 1e-9

region = find_median_in_region(field, lat, lon, bbox_4326=[118.5, 45.5, 120.5, 47.5])
print(region["median"], region["valid_count"])
```

---

## 版本

- `__version__` = `1.0.0`
- 稳定符号见 `__all__`：`find_median`、`find_median_in_region`、`find_median_from_npy`、`format_median_value`，以及几何辅助函数。
