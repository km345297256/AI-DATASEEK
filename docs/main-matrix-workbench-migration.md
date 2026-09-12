# main → Cordis：高维矩阵工作台

来源固定为 `origin/main` 的 `6f6b32eebf76ae502adfe6d74e2c77ab92d1968f`，迁移对象为 `matrix_preview.py`、`MatrixFilePreview.vue` 及数值行为测试。不是将旧接口或进程内缓存一起合并。

## 保留的能力

- NPY / 数值 NPZ；SciPy CSR、CSC、COO、DIA、BSR 稀疏 NPZ；Matrix Market `.mtx`；MAT v4/v5（含压缩）及根级数值 MATLAB v7.3 HDF5。
- 标量、向量、最多 16 维；变量选择、任意两个显示轴、其余轴固定索引、原始索引区域、每轴 16–512 点。三维正交切面、点击定位、切片播放、热图框选回读保持可用。
- 实部、虚部、幅值、相位（弧度）；稀疏非零结构概览会对显示网格分箱所有非零项，不会只取恰好命中抽样点的项。
- 热图和数值表、灰度/彩色色带、对称对数、缩放、指针坐标、行列均值剖面、当前显示值统计。
- 显式读取已保存的 `S`、`singular_values`、`residual_history`、`eigenvalues` 结果曲线和奇异值累计能量比例；不运行模型、求解器或文件内代码。

## Cordis 适配

稳定 ID `viz-matrix-workbench`，reader / adapter `matrix-workbench`，协议 2，`preview` / `whole`，不共享。输入独立保留 main 的 **128 MiB** 上限；最多 512 变量、16 维、262144 显示值、每曲线 4096 点、8 MiB 结果。它不扩大原 Plotly、H5Web、通用数组或数组窗口的预算和默认优先级。

唯一入口仍是 `POST /files/{opaque_file_id}/visualization`：

| 请求 kind | options | 公共结果 kind |
| --- | --- | --- |
| tree | `{}`，仅读取目录，不自动读切片/曲线 | tree |
| image | variable 不透明 ID、axes、indices、component、row_range、column_range、max_points、structure | array，专用 `matrix` 负载 |
| series | `{}`，显式读取已保存的结果数组 | series，专用 `matrix` 负载 |

非 tree 请求必须传客户端看到的文件 version。范围采用 0 起点、左闭右开。三维辅助切片每轴 32 点，按同一取消信号顺序读取；不能承诺一次操作只读取一个切片。每次请求仍有界取回整个文件，并非大型矩阵范围读取或零拷贝服务。颜色、对数和缩放在已授权结果上本地完成，不重新读源文件。

不保留旧 `preview_id`、准备/释放专用接口、TTL 缓存或源文件临时副本。复杂解析仅在既有无网络一次性隔离 worker 内运行；FastAPI 只执行不依赖科学库的严格结果校验。文件和插件变更、停用、关闭、选择切换取消旧请求；不同文件的旧结果不能迟到重挂载。

## 正确性和隔离改进

- 不加载 pickle、对象数组、MAT 结构体/单元/引用。NPZ 检查解压量、成员重复、穿越、加密和成员类型。
- MAT 压缩流在进入 SciPy 之前限制实际展开量；MAT73 不跟随外链/软链、VDS 或外部存储，不加载未知 HDF5 动态过滤器。所选压缩块对实际展开量进行预检。
- 严格绑定格式、文件长度、变量目录、请求选择、显示索引、形状、统计计数和输出限额。内部变量名通过安全文本规则，主机路径永不返回浏览器。
- 大于 JavaScript 安全整数的数值不静默舍入；非有限显示值为 null。极大有限值的均值、标准差和累计能量采用缩放计算，避免无谓溢出；剖面不跨 null 缺口画连接线。
- 稀疏重复项先按 SciPy 语义求和；互相抵消的项不再错误地计作非零结构。稀疏概览只创建有界显示平面，不创建全形状密集矩阵或按巨大行数构建新 CSR 索引。
- MATLAB v7.3 轴次序保持 MATLAB 逻辑次序。经典 MAT 的目录 dtype 基于 `whosmat` 类别，复数按显式分量读取；不能将目录的 double/single 类别当作“此变量一定没有虚部”的证明。

## 验证入口

`sandbox/tests/test_main_matrix_reader.py` 保留 main 数值用例并补充压缩 MAT73、稀疏方言、512² 平面、恶意参数、资源/选择绑定；可在沙箱镜像通过 unittest 或 pytest 执行。`backend/tests/test_main_matrix_visualization.py` 验证宿主纯契约。`frontend/tests/mainMatrixWorkbench.test.mjs` 验证真实读取器样例、生命周期及本地操作不重读；`frontend/tests/browser/main-matrix-fixtures.mjs` 是独立真实浏览器场景。总集成测试、HTTP 和部署状态以总迁移报告为准，不把样例或静态编译等同生产验收。

参考：[NumPy load 的禁止 pickle 语义](https://numpy.org/doc/stable/reference/generated/numpy.load.html)、[SciPy Matrix Market 读取与线程控制](https://docs.scipy.org/doc/scipy/reference/generated/scipy.io.mmread.html)、[SciPy MAT 格式支持](https://docs.scipy.org/doc/scipy/reference/generated/scipy.io.loadmat.html)、[HDF5 数据集与存储特性](https://docs.h5py.org/en/stable/high/dataset.html)。
