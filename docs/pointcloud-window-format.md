# LAS 点云窗口试点

`viz-pointcloud-window` 是独立 Cordis 可视化插件，reader / adapter 为 `pointcloud-window`。沿用统一 v2 授权入口、显式文件版本和无网络范围 worker；不进入 Agent 工具目录，也不修改 AgentLoop、SSE 或分析任务。此视图不是 LAZ/COPC 浏览器或全文件空间索引。

## 支持的明确子集

| 文件 | 支持的点格式 | 内容 |
| --- | --- | --- |
| 未压缩 LAS 1.2 | 0、1、2、3 | 原始 int32 XYZ、uint16 强度、低 5 位分类、独立分类标志；格式 2/3 的 RGB |
| 未压缩 LAS 1.4 | 0、1、2、3、6、7、8 | 旧点格式按旧语义；6–8 按完整 uint8 分类与独立低 4 位分类标志；7/8 的 RGB |

LAS 使用固定长度点记录，并通过每轴 scale / offset 解释整数坐标。字节位置、点格式布局及旧/新分类标志以 [ASPRS 1.4 头部规范](https://github.com/ASPRSorg/LAS/blob/main-1.4/source/02.04_header.sub)、[ASPRS 点记录规范](https://github.com/ASPRSorg/LAS/blob/main-1.4/source/02.06_point.sub)及 [laspy 点格式定义](https://github.com/laspy/laspy/blob/master/laspy/point/dims.py)交叉核对。ASPRS 已发布更新版本；本试点不把限定的 1.2/1.4 支持写成完整或最新 LAS 标准支持。

不支持 LAS 1.0/1.1/1.3/1.5、LAZ、COPC、压缩标记、波形点格式 4/5/9/10、内外置波形包。即使后缀伪装为 `.las`，压缩位或 LASzip/COPC VLR/EVLR 标记也会拒绝。保留标准记录后的额外维度跨度，但不解析 Extra Bytes 定义、GPS time、NIR、回波序号、扫描角、用户数据或点源标识；界面不能据此声称这些维度已可分析。

目录只读取固定文件头和最多 96 条 VLR/EVLR 的固定记录头，检查声明区间不越界、不侵入点区；不读取记录体、额外文件头、填充或未知扩展 blob。CRS 仅识别已知 `LASF_Projection` 中 GeoTIFF / WKT 记录的存在，不解读字符串或坐标定义、不采用 WKT 标志猜测坐标系、不返回原文。`units` 始终 `unknown`。因此投影坐标不会被当成经纬度，不做重投影或在线底图请求。目录通过不等于全文件内容/CRC/点分布完整性已核验。

## 调用与结果

第一次调用 `kind:tree, options:{}`，仅获得目录，不自动读取点。

随后必须显式提交：

```json
{
  "plugin_id": "viz-pointcloud-window",
  "operation": "preview",
  "version": "首次目录返回的文件版本",
  "kind": "geometry",
  "options": { "point_offset": 8, "point_count": 32 }
}
```

`point_offset` 是从 0 开始的记录索引，不是主机字节偏移；`point_count` 必须为 1–16,384。所选连续记录数必须完整落入文件声明点数，超出时拒绝而不是自动截断或补齐。该窗口不是空间均匀抽样；文件按什么顺序储存，就按什么顺序返回。保留重复点以及 synthetic / key-point / withheld / overlap 标志，不自动筛选 withheld 点。

公有结果 `kind:geometry` 中：

- `array.shape:[N,3]`、`dimensions:["X_raw","Y_raw","Z_raw"]`；`values` 为原始 int32，不是已缩放经纬度。
- `point_attributes` 含等长强度、分类、分类标志数组；有 RGB 时为 3N 个原始 uint16，否则为 null。
- `metadata.scales/offsets` 定义 `XYZ = raw * scale + offset`，不再施加未知标定。
- `declared_bounds` 只代表头部全文件声明，`window_bounds` 则从所选原始坐标计算；二者不混为一谈。目录的 window_bounds 为 null。
- `source_bytes/read_bytes/read_requests` 与后端实际范围 broker 精确绑定；点格式、选择、源版本、作用域必须一致。
- `sampled` 在窗口小于全文件点数时为 true，仅表示部分记录，不宣称随机/均匀抽样。

这是该读取器的独立几何预算，不扩大旧数组插件的 16,384 标量上限。

## 预算、精度与清理

源最大 8 GiB，每请求累计取回最多 8 MiB、单次最多 1 MiB、128 次。单点完整记录跨度可到 65,535 字节；额外维度过多时需减少点数，不能假设任意 16,384 点都能读完。元信息实际读取不超过 1 MiB，派生完整 JSON 为 2 MiB；私有载荷为公有封装预留固定余量。原始点窗口按相邻完整记录合并读取，不为每坐标单独请求，也不遍历未选点。零点文件允许目录展示，但不能申请非空窗口。

生产读取器是标准库受限 range parser，不安装 laspy、LASzip、PDAL 或新增 native 解压依赖。单次解析无文件路径、网络、写盘或任意偏移开放给浏览器；取消异常不返回部分结果，宿主沿用既有取消、隔离容器回收及版本/权限最终栅栏。

浏览器使用已安装的 Three.js 本地依赖。先计算 `(raw - origin_raw) * scale`，再用一个公共比例缩放进入 Float32，以保留轴间比例并避免大地偏置直接转换 Float32 导致局部细节消失。报告 XYZ 仍用 double 按原公式计算，同时展示原始整数、scale、offset；double 最终加上极大偏置仍有浮点舍入，所以不把屏幕数字声称为无限精度。渲染缩放不是科学数据归一化，文件数值不变。

强度/RGB 可按固定 65535 范围显示，不自动拉伸、调增益、标定色彩或把强度称为物理单位。分类着色只是按代码的固定颜色，悬停或本窗点索引可查看原始属性。

切换、停用、关闭或源版本变化会取消请求；过期异步库载入不得创建 WebGL 上下文。清理包括 Three geometry/material、OrbitControls、ResizeObserver、指针监听、renderer、WebGL context 和 DOM。相同文件/插件描述对象的轮询克隆不会重读或重建场景；本地着色不重新请求数据。

## 可重复的验证入口

- `sandbox/tests/test_pointcloud_window_reader.py`：独立固定字节夹具、畸形头/压缩标记、范围预算、8 GiB 稀疏末点、最大窗口、回调取消、真实输出副本绑定。
- 测试临时安装 `laspy==2.6.1`，与受限 parser 双向交叉核验：原始字节由 laspy 读取，另由 laspy 官方 writer 生成文件交给生产 parser。laspy 是 [BSD-2-Clause](https://github.com/laspy/laspy/blob/master/LICENSE.txt)，仅 oracle 测试依赖，不进入生产镜像。
- `backend/tests/test_pointcloud_window_visualization.py` 与 `frontend/tests/pointcloudWindow.test.mjs`：严格 schema、科学字段与请求绑定、精度、取消与资源释放。
- `frontend/tests/browser/pointcloud-window-fixtures.mjs`：modern、legacy、precision 三种真实 reader 载荷；实际 Three/WebGL 点像素、文件版本和卸载回收。
- `sandbox/tests/pointcloud_window_fixtures.py::las_bytes()` 是无网络、标准库合成 HTTP 验收样例入口。默认文件 LAS 1.4 / 格式 7 / 64 点，窗口 `{point_offset:8,point_count:32}`；首点原始 `[0,10,2]`、XYZ `[600000,4500005,100.25]`、强度 7976、分类 72、标志 8、RGB `[8192,61439,2048]`。

上述测试仅使用合成数据，不读取用户真实点云、创建分析会话或调用模型。
