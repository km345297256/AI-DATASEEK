# 沿末维剖面（last_dim_profile）

给其他 Agent / 脚本调用的**独立工具**：Portal「沿末维剖面分析」

对除最后一维（及可选 `time`）外的维度求**平均**，得到沿末维（光谱 / 深度等）的剖面。

对齐：

- 前端工具市场「沿末维剖面分析」
- 后端 `recipes.timeseries.apply_recipe(..., "reduce_last_dim")`

| 本工具 | 易混淆的其它工具 |
|--------|------------------|
| **空间/其它维平均 → 末维剖面** | `point_timeseries`：固定单格点后沿一维 |
| 非时间序列 | `region_avg_timeseries`：保留时间、压掉空间 |

---

## 何时使用

- 数据末维是波段、深度、高度等，需要一条剖面曲线
- 对接一键分析 `plot_type=reduce_last_dim` 的离线等价计算

---

## 依赖

| 包 | 用途 |
|----|------|
| `numpy` | 必需 |

---

## 目录

```
tools/last_dim_profile/
├── README.md
├── __init__.py
└── compute.py
```

```python
from tools.last_dim_profile import last_dim_profile, last_dim_profile_from_npy
```

仓库根目录需在 `PYTHONPATH` 中。

---

## Agent 调用契约

### 输入

| 字段 | 类型 | 说明 |
|------|------|------|
| `data` | `(..., last)` 或 `(time, ..., last)` | 多维数组 |
| `last_coords` | 长度 = 末维 | 剖面轴坐标（可选） |
| `times` | 可选 | 若第一维是时间 |
| `has_time` | bool 可选 | 默认在提供 `times` 时为 True |
| `average_over_time` | bool，默认 True | 含时间时再对时间平均得到 1D `values` |
| `variable` / `unit` / `last_dim` | 可选 | 元数据 / 标题 |

### 输出

```json
{
  "variable": "reflectance",
  "unit": "",
  "recipe_id": "reduce_last_dim",
  "reduce": "mean",
  "mode": "profile",
  "last_dim": "band",
  "axis": [0.0, 1.0, 2.0],
  "values": [0.1, 0.2, 0.3],
  "title": "reflectance 沿 band 剖面"
}
```

算法：

```text
# 无 time
profile[k] = nanmean( data[..., k]  对所有非末维 )

# 有 time（has_time=True）
tmp[t, k] = nanmean( data[t, ..., k]  对中间维 )
values[k] = nanmean( tmp[:, k] )   # average_over_time=True
```

---

## API 一览

```python
from tools.last_dim_profile import last_dim_profile
import numpy as np

data = np.random.rand(4, 5, 8).astype(np.float32)  # (y, x, band)
out = last_dim_profile(
    data,
    last_coords=np.arange(8),
    variable="reflectance",
    last_dim="band",
)
# out["axis"], out["values"], out["title"]
```

含时间：

```python
data = np.random.rand(12, 4, 5, 8)  # (time, y, x, band)
out = last_dim_profile(
    data,
    times=[f"t{i}" for i in range(12)],
    last_coords=np.arange(8),
    last_dim="band",
    average_over_time=True,
)
```

---

## 与现有系统的关系

```
Portal「沿末维剖面分析」
        │
        ▼
plot_type=reduce_last_dim
        │
        ▼
recipes.timeseries.apply_recipe(..., "reduce_last_dim")
        │
        └── 本工具：numpy 数组 → 剖面；不打开 NetCDF
```

---

## 最小自检示例

```python
import numpy as np
from tools.last_dim_profile import last_dim_profile

# (y, x, band)；每层 band=k 时全场值为 k
data = np.zeros((2, 3, 4), dtype=np.float32)
for k in range(4):
    data[:, :, k] = float(k)

out = last_dim_profile(data, last_coords=[10, 20, 30, 40], variable="v", last_dim="band")
assert out["values"] == [0.0, 1.0, 2.0, 3.0]
assert out["recipe_id"] == "reduce_last_dim"
print(out["title"], out["values"])
```

---

## 版本

- `__version__` = `1.0.0`
- `RECIPE_ID` = `"reduce_last_dim"`
