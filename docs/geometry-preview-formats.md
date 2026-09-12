# GRO 轨迹与 VTU 仿真网格首版

日期：2026-09-10。两种独立 Cordis 插件，共用可信本地 Three.js 视图，不执行求解器。

## 公有协议

`viz-gro-trajectory → gro-trajectory`、`viz-simulation-mesh → simulation-mesh`；契约版本 2，`view_kind: structure`，只授予 `file:read` 与 `preview`，`shared:false`。

首次 `kind:tree, options:{}` 返回帧或场目录。读取值必须显式携带该目录的文件 `version`：

- GRO：`kind:geometry, options:{frame:1}`，帧序号从 0 起。
- VTU：`kind:geometry, options:{field:"p-0",component:1}`，节点／单元场分别以 `p-N`／`c-N` 定位，分量从 0 起。仅网格须明确 `{field:null,component:0}`。

公有 `geometry` 是新增、受限的结果类型，不借用曲线或通用数组。只有四个批准的新读器可用；GRO 的 `trajectory` 与 VTU 的 `mesh` 分别按严格结构验证，未知字段、错选、错版本、跨文件及超预算均拒绝。

## GRO

支持拼接的 ASCII GRO 帧，固定五列身份字段、3–8 位坐标小数精度与可选速度，保持帧内记录顺序。要求各帧的有序 GRO 标识完全一致；不将可能回绕的原子编号当成唯一身份，也不根据原子标签猜元素或化学键。

- 坐标 nm，速度 nm/ps；只有标题明确的 `t=` 解析为 ps，未声明不猜时间步长。任意标题不返回浏览器。
- 周期盒保存 GRO 规定的九分量顺序 `v1x,v2y,v3z,v1y,v1z,v2x,v2z,v3x,v3y`；三分量盒展开其余零值。要求文档限定的 `v1y=v1z=v2z=0`，对角量为正或全部为零。
- 全零盒表示没有指定周期盒；不自动展开周期边界，不叠加轨迹、不对齐、插帧、能量计算或速度标定。
- 每文件 ≤16 MiB、≤64 帧、每帧 ≤8192 原子、全文件 ≤131072 原子记录、响应 ≤2 MiB。每次解析完整受限文件，**不是 XTC/DCD 等大型轨迹的随机帧读取器**。
- 坐标与速度绝对值 ≤1e6，时间绝对值 ≤1e12；ASCII、精度或结构不满足本子集即拒绝。

## VTU

支持单个 `UnstructuredGrid/Piece` 的 ASCII DataArray；不支持压缩、appended/binary、并行 PVTU、外部 Piece、Legacy VTK、OpenFOAM case、时间序列目录或混合求解器工程。

- 支持线、三角形、四边形、四面体、六面体、楔形和金字塔（VTK 3/5/9/10/12/13/14），顶点索引必须有效且单元内不重复。绘制原始线框，不提取外表面，不宣称几何质量、方向或体积已通过求解器检验。
- 保留 PointData / CellData 的关联和显式分量，不自动计算向量模长。节点值显示在原节点；单元值显示在顶点坐标的算术均值位置，**不是体积质心，也不插值到节点**。
- 原始 Float32 文本值按 Float32 存储精度解释；浮点场的非有限值转为空缺，灰色显示，不当作零。坐标、连接关系中的非有限值拒绝。
- 坐标／场单位未声明，不推断 SI 单位；不做变形放大、等值面、流线、拟合或求解器计算。
- 每文件 ≤16 MiB，≤4096 节点、≤2048 单元、≤32 个场、每场 ≤9 分量、全文件场值 ≤131072、响应 ≤2 MiB。每次读取整个受限文件。
- 数值绝对值 ≤1e12；整数需满足声明类型范围和 JS 安全整数边界。XML 在解析事件中检查 ≤256 节点、≤7 层、每节点 ≤8 属性，禁止 DTD、实体声明、处理指令与外部资源。

## 界面与生命周期

三维数据先在双精度中减去显示原点，再应用全轴统一比例，最后转 Float32；原值表格不受显示变换影响。旋转、缩放不再发读取请求。变更选择先清空旧画面；停用插件、文件变更、关闭页面会取消请求、断开监听、释放 WebGL 上下文，不保留旧结果作为新选择。

## 格式依据与参照

- [GROMACS GRO 格式文档](https://manual.gromacs.org/current/reference-manual/file-formats.html)：拼接帧、单位、可变精度及盒分量顺序。
- [VTK XML 格式文档](https://docs.vtk.org/en/v9.6.1/vtk_file_formats/vtkxml_file_format.html)：串行 Piece、连接数组与 PointData/CellData 的关联。
- [meshio](https://github.com/nschloe/meshio)、[MDAnalysis](https://github.com/MDAnalysis/mdanalysis) 只用作临时测试环境中的独立读取／写出参照，不加入生产依赖。
