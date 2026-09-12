# 科学领域可视化扩展实施顺序

日期：2026-09-10。基于已有 49 个 Cordis 可视化插件继续扩展。

本文记录用户已确认的实施顺序，不代表所有候选均已实现或完成格式兼容验收。

## 分批范围

| 顺序 | 本批范围 | 状态 |
| --- | --- | --- |
| 1：通用补齐 | Parquet / Arrow 分块大表、Cytoscape 科学关系网络、NeXus NXdata 科学坐标和误差 | 已完成，本机 7001 部署及接口验收通过 |
| 2：轻量学科扩展 | 系统发育树、ENVI 高光谱、GRIB 气象场、MiniSEED / SAC 波形 | 已完成，本机 7001 部署及接口验收通过 |
| 3：深入实验数据 | 质谱、流式细胞术、衍射 / 散射、电子显微谱像 | 已完成受限格式首版，本机 7001 部署及接口验收通过 |
| 4：重型专项 | DICOM、空间组学、大型点云、分子轨迹、CFD / 有限元 | 已完成受限首版：DICOM 灰度 ROI、H5AD、LAS、GRO、ASCII VTU；本机 7001 接口验收通过 |
| 5：数据库与观测网格 | SQLite 只读表格、ODIM 2.4 雷达、UGRID 1.0 海洋网格 | 已完成受限首版；协调并行任务后统一部署本机 7001，本批接口 45/45、联合九组接口回归通过；见[第五批记录](changes-2026-09-11-domain-visualization-batch-five.md)与[统一发布记录](changes-2026-09-11-unified-local-release.md) |

SQLite、天气雷达和非结构海洋网格已进入第五批受限首版。Phonopy 声子结果和天文光谱立方体作为下一批候选；现有 NetCDF CF 语义、CZI 压缩/多场景和 OME 新版本是旧插件增强项，单独验收。

## 每批的准入条件

1. 注册真实、可信的 Cordis 插件；同格式多场景独立开关，旧稳定 ID、偏好、优先级与默认视图不变。
2. 唯一批准规范同步生成 Node / Python / TypeScript 副本。新增图、坐标、单位等内容通过明确的结构约束，不借用无关结果类型，不接收任意脚本和 URL。
3. 解析在现有无网络隔离 worker 中完成。源路径继续经过 allowlist / nofollow 校验，浏览器仅持有不透明引用；保留只读数据集边界。
4. 限制实际取回、解压/解码、输出和渲染复杂度；显示少量数据不等于只扫描少量数据，明确暴露实际读取量及不支持方言。
5. 初始元数据请求不隐式运行模型或长分析。重计算使用用户明确启动的 AnalysisJob 和私有 Artifact Store。
6. 合成文件验证正常路径、格式拒绝、精度、单位、版本、撤权、取消和清理；运行全量前后端、沙箱、Cordis 回归与真实浏览器测试。
7. 保留 AgentLoop、PlanActFlow、SSE、FastAPI 主机；不直接移植桌面软件或引入第二套主消息通信。

## 第一批实现选择

- Parquet / Arrow：先使用 PyArrow 做受控单文件列/行窗口，不引入任意 SQL、DuckDB-Wasm 全量下载、Arrow Flight 服务或分区目录权限。
- 网络图：使用 Cytoscape.js 核心引擎，固定本地布局与声明式节点/边。GraphML/GEXF 按受限静态格式解析，不等于支持桌面 Cytoscape 的全部导入格式。
- NeXus：复用已有 HDF5 分块与安全检查，只解释明确的 NXdata 信号、坐标、单位、误差；不引入 Qt NeXpy，不执行标定、拟合或自动科学推断。

第一批详细支持边界与实测结果见[本批交付记录](changes-2026-09-10-domain-visualization-batch-one.md)。

第二批详细范围见[系统树、ENVI、GRIB 与地震波形](changes-2026-09-10-domain-visualization-batch-two.md)。

第三批详细支持边界和结果见[质谱、流式、衍射与 Ripple 谱像](changes-2026-09-10-domain-visualization-batch-three.md)。第四批见[重型格式受限首版](changes-2026-09-10-domain-visualization-batch-four.md)。第五批阶段目录为 68 个插件、67 个批准适配器；main 迁移另增六个独立 Cordis 插件，当前合并目录为 74 个插件、73 个批准适配器，见[main 可视化迁移记录](changes-2026-09-11-main-visualization-migration.md)。每类仍有明确格式及预算边界，不等于完整领域工具箱。
