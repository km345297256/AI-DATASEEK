# 系统发育树插件：单棵 Newick

`viz-phylogeny` 是独立的系统树视图，不是通用目录树，也不是 NeXus 科学容器读取器。新增视图不改变已有通用结构树的默认优先级。读取继续通过现有统一插件入口、文件身份校验和无网络隔离 worker；本地布局不创建分析任务，不改变源树。

## 如何准备文件

文件使用 UTF-8（允许开头 BOM），后缀为 `.nwk`、`.newick`、`.tree` 或 `.tre`。内容必须是一棵以括号开始、分号结束的 Newick，例如：

```text
((Human:0.1,Chimp:0.2)95:0.3,Mouse:0.8)Root:0.01;
```

该示例中 `95` 是内部节点标签，**不是自动认定的支持度**；`0.3` 是这个节点相连的入枝长。`Root` 的 `0.01` 保留在节点详情，但没有父节点，因此不加到绘图横坐标。

支持多分叉、单分叉、空标签和重复标签；不同节点由不透明顺序 ID 区分。空白可出现在标签和数值之间，不能出现在未引用标签或数值中间。含空格、逗号或单引号的名称使用单引号；名称中的单引号写为两个单引号：

```text
('O''Brien':0.1,Homo_sapiens:0.2,'literal_underscore':0.3);
```

按 Newick 规则，未引用的 `Homo_sapiens` 显示为 `Homo sapiens`；引号内的下划线不替换。标签最多 256 个 Unicode 字符，拒绝控制字符、HTML、外部资源地址及路径，不将标签当作 HTML 或 URL。

## 两种视图及交互

- **拓扑模式**：横坐标表示层级，不表示距离或时间，默认打开此模式。
- **枝长比例模式**：只有所有非根枝长都有明确值、且并非全零时可选。横坐标为从显示起点累计的原始枝长，单位未知；不归一化、不变换，也不补齐缺失值。绘图使用有限浮点坐标，详情始终保留枝长的源文本。
- **标签搜索**：匹配物种或内部标签；“定位下一个”会展开通往匹配节点的祖先分支。
- **折叠分支**：选中内部节点后可折叠／展开，只改变展示，不删除后代或重新读取文件。

显示起点只是源文件括号结构的起点，不据此宣称源树在生物学上有根；不进行重定根、支持度计算、树推断、时间标定或树间比较。

## 首版明确边界

| 项目 | 限制 |
| --- | --- |
| 输入／输出 | 整文件 ≤4 MiB；派生结果 ≤1 MiB，仍服从更小插件预算 |
| 拓扑 | ≤1,000 节点；根深度为 0，最深 ≤64 层；完整单树，不采样截断 |
| 数值 | 原文 ≤32 字符；指数最多 3 位；枝长为 0，或 10^-12 至 10^9 的非负有限值 |
| 不支持 | 负枝长（包括 `-0`）、NaN／Infinity、注释、NHX／HyPhy 注解、NEXUS、phyloXML、多个树及分号后的其他内容 |
| 外部资源 | 不解析 XML，拒绝该类内容；不载入实体、DTD、图片、脚本、在线资源或外部文件 |

超过边界或方言不支持会明确报错，不丢弃内容后伪称完整展示。多树文件需要先在分析流程中明确选择并导出其中一棵树。本版不把 NEXUS 文本当作 NeXus HDF5 容器，二者是不同格式。

## 插件协议与实现

`reader=adapter=phylogeny`，`view_kind=tree`，`preview/whole/shared:false`，权限仅 `file:read`。

```json
{"plugin_id":"viz-phylogeny","operation":"preview","kind":"tree","options":{}}
```

公有 `kind:tree` 中的 `payload.phylogeny` 是专用、严格校验的拓扑对象，不伪装成普通文件树节点。节点为预序排列的 `{id,parent,label,length}`；根 ID 为 `n0`，`rootedness` 固定为 `unspecified`，`length` 是原始数值字符串或 `null`。元信息独立复核节点／叶节点／深度／缺失枝长数量，格式与源文件大小绑定。数据不含用户路径、布局脚本或任意样式。

worker 与 API 使用内容一致的纯标准库验证模块，测试同时比对语法树及实际结果；API 不导入科学解析库。前端采用项目自有的有界矩形 SVG 布局和 Vue 文本渲染，无新增运行时依赖。关闭、切换或停用同步取消请求并清理结果与 SVG；相同目录轮询不会重新下载，视图切换／搜索／折叠复用已经授权的同一结果。

## 官方依据与绘图选择

格式依据 [PHYLIP Newick 描述](https://phylipweb.github.io/phylip/newicktree.html)和 [Gary Olsen 的 Newick 语法说明](https://phylipweb.github.io/phylip/newick_doc.html)。上游允许的完整语法比本插件宽；本文列出的预算和限制是本系统批准子集。

已核验的可嵌入绘图方案 [phylotree.js](https://github.com/veg/phylotree.js) 使用 D3／SVG，提供枝长、折叠等交互，并采用 [MIT 许可证](https://github.com/veg/phylotree.js/blob/master/LICENSE)。本版为维持已验证数据语义和较小集成面，采用自有 SVG，不安装或复制 phylotree.js；这不等于已经集成其全部解析、推断或重定根能力。

专用测试入口：`sandbox/tests/test_phylogeny_reader.py`、`frontend/tests/phylogenyVisualizations.test.mjs`。真实浏览器案例前缀为 `domain-expansion-phylogeny`，使用原创合成树及真实读取器产物，不读取用户数据或生产 API。
