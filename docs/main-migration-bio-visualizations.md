# main 生物数据可视化迁移到 Cordis

来源：`origin/main` 固定提交 `6f6b32e`，对照本次迁移前的 v2 工作树；没有 checkout、merge 或覆盖既有插件。本文说明三个生物数据组件的实际能力与安全边界，不代表全套迁移已发布。

## 前端清点结果

main 的 `frontend/src/renderers/registry.ts` 有 12 个 builtin 注册项，FITS/TIFF 工作台重复注册于天文与 TIFF 两个入口，因此是 11 组实际实现。

| main 实现 | 迁移处理 |
| --- | --- |
| `MatrixFilePreview.vue` | 新增独立 `matrix-workbench` Cordis 工作台，由矩阵迁移模块负责 |
| `AstronomyImagePreview.vue` | 新增独立 `astronomy-workbench`，保留 FITS/TIFF 科学影像工作流 |
| `AlignmentFilePreview.vue` | 新增独立 `alignment-browser`，另由比对模块负责 |
| `FastaSequencePreview.vue` | 本文的 `sequence-browser` |
| `GenomeBrowserPreview.vue` | 本文的 `genome-tracks` |
| `BlastAlignmentPreview.vue` | 本文的 `blast-hits` |
| 分子、PNG、OBJ、Shapefile、HTML | 当前工作树已有 Cordis 适配，保留原实现及其授权、取消、相关文件边界，不退回签名 URL 直读 |

未另建重复插件的部分：`BioTextPreview.vue` 未接入 main 的注册列表，仅是旧的表格/原文预览；main 的自定义 API/component 注册项实际一律映射到 ImageFilePreview，不能当作已经实现的任意远程渲染功能迁回。CSV、Markdown、Code、Unknown 仍由当前系统的相应插件或文件预览承接。

## 三个独立插件

| 插件 | 真实迁移能力 | 读取边界 |
| --- | --- | --- |
| `viz-sequence-browser` | 多记录选择、50/100/250/500/1000 字符窗口、位置标尺、前后翻页、GC 概览点击跳转、字面模体搜索及命中导航、碱基着色、逐碱基质量条、整条质量摘要、复制窗口 | 未压缩 FASTA/FASTQ，16 MiB 完整有界源文件，最多 256 条记录、800 万个序列字符，单次窗口最多 1000 字符；保留前 10000 个搜索位置并报告确切总数及截断 |
| `viz-genome-tracks` | 染色体选择、定位、平移、缩放、轨道启停、链方向、点击详情、键盘可选列表、VCF/GFF/GTF/BED 区间；WIG/BedGraph 正负信号条 | 16 MiB 完整有界文件，最多 100000 记录、256 染色体；视窗最多 3000 元素，超过则拒绝并要求缩窄，不静默丢失后半数据 |
| `viz-blast-hits` | Query 选择、一致性/已知覆盖度筛选、分页、Query 坐标命中图、正反方向、详情、精确 E-value 原文、结果复制 | 16 MiB 完整有界文件，最多 20000 命中、256 Query，单页最多 500 条；标准 12 列或明确 Fields 声明的 13 列（末列 qlen） |

这三个插件共用无额外第三方依赖的隔离解析器及纯结果验证模块，仍是独立插件、独立启停、独立描述符，并不共享启用状态。输出各自不超过 2 MiB。`whole` 明确表示每次解析完整但有上限的文件，不宣称使用了 BGZF/Tabix/FAI 等索引或大文件随机读取。

## 不是直接搬运旧实现

- 源解析从浏览器签名 URL 全量下载，改为现有 networkless worker 内完成；浏览器只收到经过严格验证的惰性数据，不得到主机路径。
- 先请求 `tree` 目录，用户点击后才读取所选窗口或区域；所有非目录请求携带该目录的文件版本。用户切换文件、插件版本或停用时会取消请求，迟到结果不会重新出现。
- backend 与 sandbox 的纯结果契约保持逐字一致。前端再核对格式、源大小、选择、目录身份、结果形状、版本、科学数值范围及预算。通用请求层校验插件身份，组件还传递所选插件 ID 作重复绑定。
- 不恢复 main 的静态注册表、任意 API URL 配置、旧文件预览路由或模型通信协议。AgentLoop、SSE、PlanActFlow 与数据集只读挂载边界未改动。
- 基因组轨道结果可由 `alignment-browser` 通过另一个已启用的 `genome-tracks` 插件加载，但不能绕过该插件的启用、版本或文件权限；字面染色体名称相同不代表参考组装相同，不进行重映射。

## 科学语义纠正与补齐

1. FASTA 不都代表 DNA。界面使用“字符/位置”和“GC、N 字符占比”，不将氨基酸序列误标为 bp。大写仅为显示转换；模体是字面匹配，不把 IUPAC 模糊码当正则表达式。
2. FASTQ 的质量编码不能可靠地只靠字符范围猜测。用户明确选择 Phred+33 或 Phred+64 后读取；不支持 Solexa 分数，无法满足所选编码的文件会拒绝。质量长度必须与序列长度完全一致，支持换行的 sequence/quality，不跳过坏记录。
3. Genome 内部统一使用 **0 基半开**坐标。BED/BedGraph 原样，GFF/GTF/WIG 由 1 基转换，VCF 的 REF 长度及声明 END 得到真实跨度。零长度 BED 插入边界保留，不伪造一个碱基长度。界面显示 1 基位置；插入边界特别标识。
   VCF 未提供 ID 的变异使用 `A→G`、`N→〈DEL〉` 等惰性显示标签，不因安全尖括号过滤而丢掉等位基因含义；只转换展示分隔符，不改变坐标或源序列。
4. main 注册了 WIG 但没有对应解析分支。迁移版补充受限 `fixedStep`、`variableStep` 的真实信号绘图；BedGraph 也不再仅被画成没有数值意义的平区间。空隙仍为空，不补零、连线或插值。
5. main BLAST 用命中跨度猜 Query 全长，且覆盖公式错误地先将跨度截到 100。本版没有 qlen 就显示“未知”；明确声明 qlen 才计算 `(abs(qend - qstart) + 1) / qlen * 100`。覆盖筛选设正阈值时排除未知值，并显示排除口径。
6. BLAST 反向 Query 使用较小端点定位且保留方向，单碱基命中仍有可见宽度。`1e-350` 等 E-value 保留源文本，不转浮点而变成 0。不把比对长度当 Query 坐标跨度（翻译比对可能单位不同）。

仍不支持：压缩/索引生物文件、完整 GFF 父子模型或 BED12 外显子布局、VCF breakend 跨染色体解释、任意 BLAST 自定义列顺序、远程参考序列或在线轨道。GFF 详情为有界惰性文本，不解析其链接。敏感路径/URL 标签被隐藏，长详情截取并报告次数。

## 协议接口

隔离入口：`sequence_browser_preview(data, reader, fmt, kind='tree', options=None)`。

主机纯验证：`validate_options(reader, kind, options)` 与 `validate_payload(payload, *, reader, kind=None, options=None, format=None, source_bytes=None, limit=2097152)`。

| Reader | 非目录 kind → 公开 kind | 精确选择 |
| --- | --- | --- |
| sequence-browser | table → table | record、start（1 起）、count、motif、quality_encoding |
| genome-tracks | map → features | chromosome（目录序号）、start（0 起）、end（不含） |
| blast-hits | table → table | query（目录序号或 null）、min_identity、min_coverage、offset、count |

数据分别位于 `sequence`、`tracks`、`hits`，这是明确的领域结果，不把整个序列或基因组轨道压成通用二维表格。`tracks` 为原始端点的元素数组；`choices.chromosomes` 的 ordinal 必须与请求一致。前端公开 helper `parseBioData`、`bioCatalogIdentity`、`GenomeChromosome`、`GenomeFeature` 供可信组件组合使用。

## 测试与发布状态

已完成本模块本地验证：105 项前端单元/生命周期测试通过；全项前端类型检查通过；7 类 Chrome 场景最终全部通过（FASTA、FASTQ、BED、WIG、BedGraph、BLAST12、BLAST13），没有外部请求、控制台错误或卸载残留。浏览器实际发现并修复了轨道平移 pointer capture 抢走元素点击的问题。

另外已补充 sandbox/host 解析与防火墙测试、20 个精确标记合成文件的六插件真实 HTTP 验收脚本、5 项该脚本的安全约束单元测试。5 项工具安全测试已通过；全套 pytest、真实业务 API 验收和发布由统一发布任务执行，不能把这些待执行项计为完成。

HTTP 脚本只允许已有 Compose frontend 或本机 7001；原生合成文件在无网、非 root、无 host mount 容器中生成。只对精确 tag 文件做上传、所有权 CAS、删除；插件偏好恢复并回读，前后业务摘要必须相同。脚本不访问 Agent、模型、原数据集，也没有在本模块开发期间执行过。

## 官方格式依据

- [NCBI FASTA 格式](https://www.ncbi.nlm.nih.gov/genbank/fastaformat)
- [NCBI SRA FASTQ 格式与质量编码](https://www.ncbi.nlm.nih.gov/sra/docs/submitformats/)
- [UCSC BED 格式与坐标](https://www.genome.ucsc.edu/FAQ/FAQformat)
- [UCSC WIG 格式](https://www.genome.ucsc.edu/goldenPath/help/wiggle.html)
- [UCSC BedGraph 格式](https://www.genome.ucsc.edu/goldenPath/help/bedgraph.html)
- [NCBI BLAST 输出列定义](https://www.ncbi.nlm.nih.gov/books/NBK279684/table/appendices.T.options_common_to_all_blast/)
