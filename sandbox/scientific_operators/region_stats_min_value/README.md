# 选区统计 · 最小值（region_stats_min_value）

给其他 Agent / 脚本调用的**独立工具**：Portal「**选区统计**」中的**最小值**（含 `min_location`）。

在 2D 地理栅格（全场或多边形/bbox 选区）上求 **min**，并返回极值经纬度。

对应产品 UI「选区统计」中的：

| UI 字段 | 工具输出 |
|---------|----------|
| 最小 | `min` |
| （后端 `min_location`，面板可选用） | `min_location` → `{lat, lon}` |

实现逻辑与 `launch-api/app/tpdc_batch.py` 中 `_field_stats` / `region_layer_stats` 的最小值分支一致（`nanargmin` + 1D lat/lon 索引）。

与同目录 `tools/region_stats_max_value` 对称；需要最大值时用那个包，不要混用。

---

## 何时使用

- 需要「这场数据里最小的点在哪、值是多少」
- 多边形 / bbox 选区内的空间最小值（与 Portal 框选一致）
- 已有 TPDC 任务产物 `*.npy`（`min.npy` / `lat.npy` / `lon.npy`）时快速取极值

**不要**用本工具算均值、标准差、最大值——那些属于其它统计工具。

---

## 依赖

| 包 | 用途 |
|----|------|
| `numpy` | 必需 |
| `matplotlib` | 仅在选区掩膜（`find_min_in_region` / `mask_region`）时需要 |

---

## 目录

```
tools/region_stats_min_value/
├── README.md      ← 本说明（给 Agent）
├── __init__.py
└── compute.py     ← 全部计算逻辑
```

仓库根目录需在 `PYTHONPATH` 中（或 `cd` 到仓库根后再 `import`）。

```python
from tools.region_stats_min_value import find_min, find_min_in_region, find_min_from_npy
```

---

## Agent 调用契约

### 输入约定

| 字段 | 类型 | 说明 |
|------|------|------|
| `field` | `np.ndarray`，形状 `(n_lat, n_lon)` | 2D 数值场；NaN/Inf 视为无效 |
| `lat` | 1D，长度 `n_lat` | 纬度（度，EPSG:4326） |
| `lon` | 1D，长度 `n_lon` | 经度（度，EPSG:4326） |
| `polygon_4326` | `[[lon, lat], ...]` 可选 | 至少 3 点；与 bbox 二选一优先用多边形 |
| `bbox_4326` | `[west, south, east, north]` 可选 | 轴对齐框 |

坐标顺序与项目其它 API 一致：**先 lon 后 lat**。

### 输出 JSON 形状（稳定字段）

```json
{
  "min": 2.31e-7,
  "min_location": { "lat": 46.20, "lon": 119.80 },
  "valid_count": 1280,
  "index": { "j": 12, "i": 55 },
  "mode": "polygon",
  "polygon_4326": [[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
  "bbox_4326": [120.0, 45.0, 122.0, 48.0]
}
```

| 字段 | 说明 |
|------|------|
| `min` | 最小值；无有效像元时为 `null` |
| `min_location` | `{"lat","lon"}`；无法定位时为 `null` |
| `valid_count` | 参与统计的有效像元数 |
| `index` | 栅格索引 `j`=纬度维、`i`=经度维；可选诊断字段 |
| `mode` | `full` / `polygon` / `bbox`（`find_min` 本身不含 mode；npy/region API 会带） |
| `polygon_4326` / `bbox_4326` | 选区回显（仅 region / 带选区的 npy 调用） |

无有效数据时的最小失败形态：

```json
{
  "min": null,
  "min_location": null,
  "valid_count": 0,
  "index": null
}
```

---

## API 一览

### 1. `find_min(field, lat, lon)` — 全场最小

```python
from tools.region_stats_min_value import find_min

out = find_min(field, lat, lon)
# out["min"], out["min_location"]["lat"], out["min_location"]["lon"]
```

### 2. `find_min_in_region(...)` — 选区内最小（对齐 UI）

```python
from tools.region_stats_min_value import find_min_in_region

out = find_min_in_region(
    field, lat, lon,
    polygon_4326=[[120.0, 45.0], [122.0, 45.0], [122.0, 48.0], [120.0, 48.0]],
)
# 或
out = find_min_in_region(field, lat, lon, bbox_4326=[120.0, 45.0, 122.0, 48.0])
```

缺省既无 polygon 也无 bbox → 抛 `ValueError`。

### 3. `find_min_from_npy(...)` — 读任务 `.npy`

```python
from tools.region_stats_min_value import find_min_from_npy

out = find_min_from_npy(
    "data/tpdc_jobs/<job_id>/min.npy",
    "data/tpdc_jobs/<job_id>/lat.npy",
    "data/tpdc_jobs/<job_id>/lon.npy",
    polygon_4326=[...],   # 可选
)
```

无选区时 `mode="full"`。

### 4. 展示辅助（可选）

```python
from tools.region_stats_min_value import format_min_value, format_min_location

format_min_value(2.31e-7)           # → "2.31e-07" 类科学计数
format_min_location({"lat": 46.20, "lon": 119.80})  # → "46.20°N, 119.80°E"
```

---

## 与现有系统的关系

```
Portal「选区统计」KPI「最小」
        │
        ▼
POST /api/tpdc/jobs/{id}/region-stats
        │
        ▼
tpdc_batch.region_layer_stats → _field_stats
        │
        ├── mean / std / min / max / ...
        └── min + min_location  ← 本工具单独封装这一支
```

本工具**不**发起 HTTP，**不**依赖 FastAPI / job_id；Agent 传入数组或 `.npy` 路径即可离线复现同一极值语义。

若 Agent 只有 `job_id`、没有数组：先读任务目录下的图层 `*.npy`（常用 `min.npy`，或当前展示的 `max.npy` / `median.npy`）+ `lat.npy` + `lon.npy`，再调用 `find_min_from_npy`。

对称工具：最大值见 `tools/region_stats_max_value/README.md`。

---

## 错误行为

| 情况 | 行为 |
|------|------|
| 多边形顶点 &lt; 3 | `ValueError` |
| bbox 长度 &lt; 4 | `ValueError` |
| field 与 lat/lon 尺寸不一致 | `ValueError` |
| 未提供选区却调用 `find_min_in_region` | `ValueError` |
| 区内全 NaN | 返回 `min=null`，不抛异常 |

---

## 最小自检示例

```python
import numpy as np
from tools.region_stats_min_value import find_min, find_min_in_region, format_min_location

lat = np.array([45.0, 46.0, 47.0, 48.0])
lon = np.array([118.0, 119.0, 120.0, 121.0])
field = np.array(
    [
        [1.0, 2.0, 3.0, 4.0],
        [5.0, 6.0, 9.0, 7.0],
        [8.0, 1.0, 2.0, 3.0],
        [0.5, 1.0, 2.0, 3.0],
    ],
    dtype=float,
)

full = find_min(field, lat, lon)
assert full["min"] == 0.5
assert full["min_location"] == {"lat": 48.0, "lon": 118.0}

region = find_min_in_region(
    field, lat, lon,
    bbox_4326=[119.5, 46.5, 121.5, 48.5],
)
print(region["min"], format_min_location(region["min_location"]))
```

---

## 版本

- `__version__` = `1.0.0`
- 稳定对外符号见 `__all__`：`find_min`、`find_min_in_region`、`find_min_from_npy`、`format_min_value`、`format_min_location`，以及几何辅助 `normalize_polygon_4326` / `polygon_from_bbox` / `mask_region`。
