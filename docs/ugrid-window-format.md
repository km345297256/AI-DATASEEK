# UGRID 二维非结构网格只读试点

`viz-ugrid-window` 使用统一 v2、Cordis 独立开关及 `ugrid-window` 适配器/读取器。它是受限格式实现，不是完整海洋模式后处理工具；不会修改 AgentLoop、SSE、文件或分析会话。

## 适用文件与明确限制

- 后缀 `nc / nc4 / netcdf / h5 / hdf5 / hdf` 只是候选；文件必须以 HDF5 签名起始，根组显式声明 `Conventions` 包含 `UGRID-1.0`，不接受冲突的 UGRID 版本。
- 首版限根组 NetCDF4 风格数据集，最多 128 个对象、8 个网格，全目录最多 32 个场。不支持 NetCDF3、子组、HDF5 user block、软/外部链接或同对象硬链接别名。
- 每个网格必须是标量整数拓扑变量，`cf_role=mesh_topology`、`topology_dimension=2`，明确两个 `node_coordinates` 及 `face_node_connectivity`。连接变量必须声明 `cf_role=face_node_connectivity`。
- 节点坐标使用声明的顺序作为横轴/纵轴，不从变量名猜测坐标类型。必须为安全的同文件数值数组、相同节点维。坐标非有限、缺失、非恒等 `scale_factor / add_offset` 均拒绝；不先绘制错误的未解码坐标。
- 维度身份只依据同文件固定大小的 `_Netcdf4Dimid` / `_Netcdf4Coordinates` 数字元数据及 `CLASS=DIMENSION_SCALE`，并核对长度与形状。不会为兼容性加载 `DIMENSION_LIST / REFERENCE_LIST` 的不受限引用堆。因此没有这些数字元数据的其他合法 UGRID/HDF5 编码可能不适用；错误会提示使用原始 HDF5 查看器，绝不自动切换。
- 面连接接受 `start_index=0/1`，缺省按 UGRID 规定使用 0；可明确 `face_dimension` 指示转置存储。支持每面 3–8 个节点，`_FillValue` 必须在节点索引范围外且只能出现在行末；归一化输出为 0 起点，保留原面顺序，不修复网格。拒绝越界、重复节点/坐标、零面积与自交多边形。
- 场必须明确 `mesh` 和 `location=node|face`，维度身份必须与网格对应。所有非空间维按原始存储顺序显式选择整数索引；不因长度相同就推断节点/面关联，不自动选时刻或层。暂不支持边场、位置索引集、3D 拓扑或体网格。
- 固定长度字符串属性每项最多 512 字节、累计 64 KiB；数值元数据限有界标量/维度向量。VLEN、对象/复合/复数类型不用于解析。显示标签清洗，主机路径、未知自由属性和链接目标不进入公有响应。

## 科学显示语义

只显示原生数值坐标平面，不叠加底图、不计算距离/面积、不重投影、不进行球面或周期拼接。明确声明 `standard_name=longitude` 且跨度超过 180° 的网格拒绝；明确 latitude 时要求 −90° 至 90°。未知坐标系统保持未知。

面场在原始多边形内平涂；节点场只在节点绘制彩色点与连接线框，不向面插值。缺失值为灰色。图例/颜色范围仅来自所选场切片，不代表整个多时次数据集。

字段返回原始存储值：显式 `_FillValue / missing_value` 以及 NaN/Inf 显示为空，不默认额外填充值；安全整数保留精度。`scale_factor / add_offset` 仅显示声明，不应用标定。前端显著写明“原始存储值，未应用 CF 标定”，单位显示为“源声明的物理单位（当前未应用）”，不直接给存储代码附上物理量含义。时间、垂向层和其他非空间坐标只按索引选择，不解码海洋 sigma 坐标等公式。

## 读取协议和预算

先 `kind:tree, options:{}` 获取目录，不解码坐标、连接或场数组；窗口底层读取可能包含同一有界缓存页内的其他原始字节，不宣称零数据字节读取。

后续必须带文件 `version` 并提交：

```json
{"kind":"geometry","options":{"mesh":"u-<32位摘要>","field":"f-<32位摘要>","indices":[1,2]}}
```

`field:null, indices:[]` 只看拓扑。ID 是内部名称的稳定摘要，不是路径、URL 或权限令牌；宿主仍逐次检查身份、文件、版本和插件状态。

源 ≤8 GiB，每次累计范围读取 ≤8 MiB、单次 ≤1 MiB、≤128 次，公有输出 ≤2 MiB。每个网格最多 4096 节点、2048 面、每面 8 节点。显式选择后**完整读取该有界拓扑**，仅场变量按所选索引读取；不是任意大网格空间抽样或瓦片读取。HDF5 单块解码 ≤4 MiB，命中块合计 ≤16 MiB /128 块，先验证标准压缩流再允许 native 解码。预算不足时明确拒绝，不降级为全文件下载。

复用已审查的 `RangeFile / _describe / _slice_plan / _verify_chunks`；隔离 worker 无网络、无源文件挂载，只接授权范围回调。前端绑定目录与响应、所有选择及版本；文件/插件/索引变化取消旧请求，同标识目录轮询不重载。Canvas 关闭时清空尺寸、移除元素和断开 ResizeObserver，无背景渲染循环。

## 独立参考与验收

测试使用标准 netCDF4 写出的合成文件，覆盖 0/1 起点、两种连接维度顺序、标准压缩、多个非空间维、节点/面场、填充值、稀疏 8 GiB 逻辑源、元数据/链接/解码预算和取消。`Xugrid==0.15.3` 的真实 `Ugrid2d.from_dataset` 独立比对节点/面，`to_dataset().to_netcdf(engine='netcdf4')` 写出的文件再由本读取器读取，形成双向参考。Xugrid 仅临时测试依赖，不加入生产；NumPy1.26.4、Xarray2024.11.0、Zarr2.18.3 保持不变。无 Numba 时上游使用其官方 Python 路径，不伪造科学函数。

前端真实 fixture：`frontend/tests/browser/ugrid-window-data.json`，场景 `domain-expansion-ugrid-node / domain-expansion-ugrid-face`。fixture 由实际读取器生成，不手写结果值；所有浏览器请求拦截为合成数据，不访问业务 API。

## 官方依据与许可

- [UGRID 1.0 约定](https://ugrid-conventions.github.io/ugrid-conventions/)：二维拓扑、连接索引、`mesh/location`、非空间维度顺序及填充值规则。
- [Xugrid 官方仓库](https://github.com/Deltares/xugrid) 与 [Ugrid2d.from_dataset](https://deltares.github.io/xugrid/api/xugrid.Ugrid2d.from_dataset.html)：独立科学参考，MIT 许可；生产没有嵌入 Xugrid。
- [h5py file-like VFD](https://docs.h5py.org/en/stable/high/file.html#python-file-like-objects)：经受控 Python 文件对象执行范围读取。

本模块没有复制第三方 SDK 或新增运行时依赖，不宣称支持任意 UGRID、CF、NetCDF 或海洋模式方言。
