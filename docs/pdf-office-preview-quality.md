# PDF / Office 清晰阅读改造

日期：2026-09-09。保持 Cordis 可视化协议 `contract_version: 2`、统一调用入口和 `VisualizationResult` 不变，不修改 AgentLoop、PlanActFlow、SSE 或只读数据集挂载。

## 使用方式

现有 PDF、Word、PowerPoint 预览直接获得高清重绘、50%–400% 缩放、适应宽度/整页和全屏。

在「插件 → 可视化」可以独立启停新增的两个插件；打开文件后，用预览方式选择器切换：

| 插件 | 适用场景 | 统一能力 | 默认选择策略 |
| --- | --- | --- | --- |
| DOCX 清晰阅读 `viz-docx` | 可选择文字的 HTML 阅读、缩放；普通文字/表格/PNG/JPEG | `bytes`，`binary` reader | 默认可用，优先级低于原 Word 预览 |
| ONLYOFFICE 本机只读阅读 `viz-onlyoffice` | Word/Excel/PPT 的独立办公阅读工作台 | `prepare`，`office-viewer` reader，`resources` 结果 | 仓库默认关闭；部署本机服务后手动启用，优先级低于原预览 |

插件发布版本和协议版本是两回事。PDF/Word/PPT 插件发布版本提升到 `2.1.0`，新增插件发布版本为 `1.0.0`，四种阅读方式仍然使用同一协议。

本机本轮已部署并启用 ONLYOFFICE；访问 `http://localhost:7001`，刷新后打开办公文件，在预览方式中选择「ONLYOFFICE 本机只读阅读」。仓库默认关闭策略不变，其他部署不会自动启动该服务。本机只发布 `127.0.0.1:7001`，没有公网端口。

## 高清显示与转换

PDF.js 按 PDF 点值换算 CSS 尺寸，绘制缓冲区独立乘以屏幕像素密度。缩放、调整容器尺寸或改变显示器密度会真正重绘，而非仅把原有位图拉大。当前页单画布最多 8M 像素、最长边 8192；临时画布复制完成即释放。达到预算时显示清晰度限制说明，不偷偷改变用户选择的缩放比例。

LibreOffice 7.3.7 不支持现有 CLI 中的新版 JSON 过滤选项。真实回归发现原配置把 1200×800 嵌入图片降成 300×200，且未限制为 100 页。现在同时使用独立 profile 和现代 CLI 参数，明确无损、不降低图像分辨率、请求嵌入标准字体；生成后用已有 pypdf 无损保留前 100 页。

安全预算不因清晰度提升而取消：生成 PDF 在读取/解析前检查 5 MiB，最多解析 1000 页，截页后再次检查输出预算；超限明确报错，不降低画质。旧版 LibreOffice 仍先转换完整文档，因此大文件仍受原运行时间/内存限制。保留已有 Noto CJK/Liberation 字体，不下载商业字体。缺失字体会替换；不承诺与 Microsoft Office 分页百分之百一致。

## DOCX 隔离与预算

锁定 `docx-preview@0.4.0`、`jszip@3.10.1`。宿主只读取固定、无重定向、有界的本地脚本资源，再通过 nonce 在 opaque-origin iframe 执行；不为该 iframe 开放同源访问。文件内容不拼接到页面源码。

- 最多 16 MiB ZIP 输入、512 条 ZIP 记录、32 MiB 解包数据；XML 单项 2 MiB、合计 8 MiB，深度 128。
- 流式解压检查实际长度，不只相信 ZIP 声明。拒绝路径穿越、宏、嵌入对象、HTML altChunk、外部内容与危险样式资源；普通超链接仅保留文字。
- 图片仅允许经过头信息验证的 PNG/JPEG，合计 16M 像素；同时核验实际图片引用目标与规范化 MIME，防止伪装成 XML 的 SVG 绕过预算。
- 嵌入字体不加载，图片采用受控 Blob URL；CSP 阻止网络、表单、脚本注入和文档自带 data 图片。离开视图时销毁 iframe 并释放资源。
- XML 最多 40,000 元素；排版时在分配前限制 40,000 节点、8M 字符，避免页眉/页脚重复造成放大。

HTML 阅读不能重算 Word 分页、目录域和复杂 Office 图表；不支持的内容应切换原 Word 分页预览或 ONLYOFFICE，不假装完整保真。

## ONLYOFFICE

参见 [本机服务部署、隔离和许可说明](./onlyoffice-local-viewer.md)。文档服务在独立内部网络，无宿主端口、数据集挂载或 Docker socket。浏览器使用 `office.localhost` 独立来源但共用现有 7001 端口；主系统 API 对该来源拒绝访问。文档授权绑定文件、用户、版本与插件状态，并受限时租约保护；配置禁止编辑、保存回调、打印和查看器下载。

“只读”不代表能撤回用户已经看到、截图或由浏览器缓存的内容。关闭插件/租约到期会关闭查看器并拒绝新的受控源文件读取，但不宣称抹除已显示内容。

## 上游依据与许可

- [PDF.js 官方高清 canvas 示例](https://mozilla.github.io/pdf.js/examples/index.html)：独立 CSS 尺寸与 `devicePixelRatio` 绘图缓冲区。
- [LibreOffice PDF 参数](https://help.libreoffice.org/latest/en-US/text/shared/guide/pdf_params.html) 与 [7.3 配置定义](https://raw.githubusercontent.com/LibreOffice/core/libreoffice-7.3.7.2/officecfg/registry/schema/org/openoffice/Office/Common.xcs)：新旧导出版本分别核验，不能只按最新默认值判断本机行为。
- [docx-preview](https://github.com/VolodymyrBaydalka/docxjs)：Apache-2.0；JSZip 选择 MIT 许可，构建产物保留许可文件。ONLYOFFICE 的非商业使用仍须遵守其许可要求，不因“非商业”免除许可义务。

## 验收记录

- 前端类型检查、生产构建通过；前端单元测试 **341 passed**。
- Cordis 插件宿主 **52 passed**；生成的三端契约映射一致，当前 36 个注册仅有协议版本 2。
- 后端完整回归 **2361 passed、32 skipped**；跳过项保留原有环境条件，不把跳过计作通过。
- ONLYOFFICE 最终补充目标回归 **47/47**，覆盖统一分派、非法操作、授权和部署隔离。
- 沙箱完整回归 **552 passed、0 skipped**。真实 DOCX/PPTX 导出验证中文可提取、字体嵌入、0.25pt 矢量线保留，以及 1200×800 源图像像素逐字节一致；101 页可靠限制为 100 页。
- 科学/办公浏览器回归 **47/47**：地图、天球、谱图、分子、基因组等旧功能，以及 PDF/Word/PPT 的 DPR 1/2/3、动态密度、缩放/全屏/回收；DOCX 中文文字选择、PNG 原始尺寸、GUID 元数据、危险资源/深层 XML/分页放大拒绝。
- ONLYOFFICE 真实服务验收 **3/3**：DOCX、XLSX、PPTX 的实际内容与合成样本一致，原件摘要不变，编辑/下载/打印权限关闭、无外部请求、停用后关闭查看器。读取固定版本 SDK 模型核对文字/单元格，并逐张检查截图，不以空白画布当作成功。停用后的授权状态 404 是预期拒绝。
- 原有真实 HTTP 回归通过：扩展可视化的 14 个合成上传全部清理，2 个测试作业和 1 个测试产物清理；NetCDF/FITS/FASTQ 的 7 次预览通过，4 个上传全部清理；数据集入口 6 项通过，3 种非法或未注册路径被拒绝。已有插件偏好恢复，没有访问用户文件或新增模型调用。

同一 PDF 页在 100% 下保持 `480×320` CSS 像素，DPR 1/2/3 对应缓冲区 `480×320`、`960×640`、`1440×960`；2/3 倍屏能分开间隔仅 1 CSS 像素的双细线。以上是渲染和保真检查，不是虚构的数据分析速度提升百分比。

本轮浏览器详细证据目录：`/var/folders/n2/wz3bk3pj2ps8y5qk2zx257v40000gn/T/dataseek-visualization-browser-mzE8px/`。临时证据可能由操作系统清理。

ONLYOFFICE 原生三格式证据目录：`/var/folders/n2/wz3bk3pj2ps8y5qk2zx257v40000gn/T/dataseek-onlyoffice-native-PDMcxp/`。这组验证通过真实统一入口、网关和原生文档服务，不模拟文档服务响应。测试的三份上传已清理。

部署前确认 25 个会话均完成、0 个活动分析作业。真实 HTTP 回归前后，158 个文件、25 个会话、1763 个事件、450 条模型追踪、440 条 token 记录、51 个数据集保持一致；扩展回归还核验 194 个分析作业和 2 个已有产物的完整快照摘要一致。未提交或推送 Git。以上验证覆盖当前回归集和合成样本，不承诺任意办公文档均完全保真。
