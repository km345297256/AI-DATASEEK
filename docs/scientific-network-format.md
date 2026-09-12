# 科学网络图：首版输入规范

插件 `viz-scientific-graph` 使用 Cytoscape.js 3.34.3 展示**已提供的静态网络拓扑**，可用于蛋白互作、基因调控或一般科学实体关系。它不查询外部数据库、不推断关系、不做富集分析，也不是分子原子结构查看器。

仅在用户选中该插件时，通过现有授权文件入口读取；关闭或停用会取消请求并销毁画布、事件监听和布局对象。布局与搜索复用已经校验的本次结果，不再次下载文件。

## 共同限制

- UTF-8 单文件，整个源文件不超过 **4 MiB**；最多 **1000 个节点、3000 条边**，派生结果不超过 **1 MiB**。超限拒绝，不截断或抽样伪装成完整网络。
- 只支持静态简单有向图或无向图。允许一般环路和孤立节点；不支持自环、同方向平行边、无向重复边、嵌套图、超边、混合方向或动态图。
- 节点 ID 必须唯一；每条边的端点必须引用本图已声明节点。ID 为 1–128 个英文字母、数字、下划线、点、冒号或短横线；不能是路径或 URL。可选边 ID 在边之间唯一。
- 标签／分组为最多 256 字符的纯文本；拒绝 HTML、控制字符、URL 和主机路径，不处理图标、脚本、样式、DOM 或可执行回调。
- 边权重可缺省或为 `null`；提供时为绝对值不超过 `10^12` 的有限数值。按原值显示，不自动解释为距离、相关性、因果强度或显著性。
- 前端使用固定圆形／网格布局；不是原文件坐标或分析结论。超过 150 节点时默认隐藏标签，搜索时显示匹配标签。

## 规范化 JSON

这是**明确的节点边格式**，不是任意 JSON，也不是 Cytoscape 完整会话 JSON。顶层仅允许 `directed`、`nodes`、`edges` 三个字段：

```json
{
  "directed": true,
  "nodes": [
    { "id": "TP53", "label": "TP53 蛋白", "group": "调控因子" },
    { "id": "MDM2", "label": "MDM2 蛋白" },
    { "id": "CDKN1A" }
  ],
  "edges": [
    { "id": "interaction-1", "source": "TP53", "target": "MDM2", "label": "相互作用", "weight": 0.75 },
    { "source": "TP53", "target": "CDKN1A", "label": "调控" }
  ]
}
```

`directed` 必须是 JSON 布尔值。节点必须有字符串 `id`，可选 `label`／`group`；缺省标签为 ID。边必须有字符串 `source`／`target`，可选 `id`／`label`／`weight`。不接受其他字段或重复 JSON 键。节点必须至少一个，边可为空。

## GraphML 子集

支持 GraphML 命名空间 `http://graphml.graphdrawing.org/xmlns` 下的单个平面 `graph`，`edgedefault` 必须为 `directed` 或 `undirected`。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<graphml xmlns="http://graphml.graphdrawing.org/xmlns">
  <key id="label" for="node" attr.name="label" attr.type="string"/>
  <key id="group" for="node" attr.name="group" attr.type="string">
    <default>protein</default>
  </key>
  <key id="weight" for="edge" attr.name="weight" attr.type="double"/>
  <graph id="network" edgedefault="undirected">
    <node id="A"><data key="label">Protein A</data></node>
    <node id="B"/>
    <edge source="A" target="B"><data key="weight">2.5</data></edge>
  </graph>
</graphml>
```

`key` 只能声明节点 `label/group`、边 `label/weight` 或 `for="all"` 的 `label`；支持相应的标量 `default`。权重类型允许 `int/long/float/double`，文本类型只能为 `string`。未知键、重复键、错误作用域和额外属性直接拒绝。

不支持 yEd 等厂商样式、图级扩展属性、`port`、`hyperedge`、单边方向覆盖、嵌套 `graph`。DTD、实体定义、处理指令、CDATA 和注释均拒绝；XML 声明可用。固定标准 `schemaLocation` 只作声明校验，**不会访问或下载 schema**。

## GEXF 子集

支持 GEXF 1.2 的 `http://www.gexf.net/1.2draft` 或 `http://gexf.net/1.2draft` 命名空间，以及 GEXF 1.3 的 `http://gexf.net/1.3`，版本属性须匹配。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<gexf xmlns="http://gexf.net/1.3" version="1.3">
  <graph mode="static" defaultedgetype="directed">
    <nodes>
      <node id="A" label="Protein A"/>
      <node id="B" label="Protein B"/>
    </nodes>
    <edges>
      <edge id="e1" source="A" target="B" label="interaction" weight="0.75"/>
    </edges>
  </graph>
</gexf>
```

首版只支持原生节点 `id/label` 和边 `id/source/target/label/weight`；`mode` 只能缺省或为 `static`。不支持 `meta`、`attributes/attvalues`、`viz` 样式、时间区间、层级和扩展元素。无法表示的内容不会被静默丢弃；请先导出为上述受限子集或规范化 JSON。

## 结果与科学解释

统一请求是 `operation: preview`、`kind: graph`、`options: {}`。返回 `kind: graph`；宿主与前端严格检查源格式、字节数、节点边计数、方向、端点和资源预算。渲染器使用服务端生成的 `n0/e0` 索引，原始 ID 仅作为安全文本展示，不当作选择器、文件路径或权限凭据。

点击节点查看 ID、标签与分组，点击边查看边 ID、标签与权重。一个网络中的边可能表示不同科学含义，应以数据集说明为准；本插件不为其自动背书。

上游依据：[Cytoscape.js API](https://js.cytoscape.org/)、[GraphML Primer](https://graphml.graphdrawing.org/primer/graphml-primer.html)、[GEXF 基本图](https://gexf.net/basic.html)、[GEXF 规范](https://gexf.net/schema.html)。实际支持范围以上述子集及项目测试为准，不等于这些标准的全部能力。
