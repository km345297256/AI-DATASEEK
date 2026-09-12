"""Strict flat/static scientific graph readers, never execute source content.

Normative references: graphml.graphdrawing.org/primer/graphml-primer.html,
gexf.net/basic.html and gexf.net/schema.html. This is deliberately a subset:
GraphML label/group/weight keys, GEXF native label/weight, no extension styles.
"""
import io
import json
import re
import xml.etree.ElementTree as ET

from .scientific_graph_payload import (
    MAX_EDGES, MAX_INPUT_BYTES, MAX_NODES, WARNINGS,
    ScientificGraphError, require, safe_text, weight,
    validate_scientific_graph_options, validate_scientific_graph_payload,
)

GRAPHML = "http://graphml.graphdrawing.org/xmlns"
GEXF = {"http://www.gexf.net/1.2draft": "1.2", "http://gexf.net/1.2draft": "1.2", "http://gexf.net/1.3": "1.3"}
XSI = "{http://www.w3.org/2001/XMLSchema-instance}schemaLocation"


def _keys(obj, required, optional=()):
    require(type(obj) is dict and set(required) <= set(obj) <= set(required) | set(optional), "图包含未支持的字段、样式或关联资源。")


def _pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "图 JSON 含重复字段。")
        result[key] = value
    return result


def _json(text):
    # Bound nesting before invoking json's recursive decoder; braces in strings
    # do not count. Unknown fields are subsequently rejected, not silently lost.
    depth, quoted, escaped = 0, False, False
    for c in text:
        if quoted:
            if escaped: escaped = False
            elif c == "\\": escaped = True
            elif c == '"': quoted = False
        elif c == '"': quoted = True
        elif c in "[{":
            depth += 1
            require(depth <= 8, "图 JSON 嵌套过深。")
        elif c in "]}": depth -= 1
    def number(token):
        require(len(token) <= 64, "图 JSON 数值文本过长。")
        return weight(float(token))
    try:
        result = json.loads(text, object_pairs_hook=_pairs, parse_constant=lambda _: require(False), parse_int=number, parse_float=number)
    except (ValueError, RecursionError, OverflowError) as exc:
        raise ScientificGraphError("图 JSON 无效或包含非有限数值。") from exc
    _keys(result, {"directed", "nodes", "edges"})
    require(type(result["directed"]) is bool)
    return result, "dataseek-graph-json-v1"


def _xml(text):
    # XML declaration is allowed only at the beginning; no PI/DTD/CDATA/comments.
    remaining = re.sub(r'^\s*<\?xml\s+version=["\']1\.0["\'](?:\s+encoding=["\']UTF-8["\'])?(?:\s+standalone=["\'](?:yes|no)["\'])?\s*\?>', '', text, count=1, flags=re.I)
    require("<!" not in remaining and "<?" not in remaining, "图 XML 不接受 DTD、实体定义、处理指令、CDATA 或注释。")
    depth, count = 0, 0
    try:
        parser = ET.iterparse(io.StringIO(text), events=("start", "end"))
        for event, item in parser:
            if event == "start":
                depth += 1
                count += 1
                require(depth <= 6 and count <= 16016, "图 XML 节点数或深度超限。")
                require(len(item.attrib) <= 6)
            else:
                depth -= 1
                require(not item.tail or not item.tail.strip())
        return parser.root
    except ET.ParseError as exc:
        raise ScientificGraphError("图 XML 结构或 UTF-8 编码无效。") from exc


def _element(item, tag, required=(), optional=(), text=False):
    require(item.tag == tag, "仅支持单个静态平面图；嵌套、动态或可视化扩展未支持。")
    _keys(item.attrib, required, optional)
    if not text:
        require(not item.text or not item.text.strip())


def _schema(root, namespace):
    if XSI in root.attrib:
        # Schema locations are inert declaration metadata, never fetched.
        pair = root.attrib[XSI].split()
        require(len(pair) == 2 and pair[0] == namespace and pair[1] in {
            GRAPHML + "/1.0/graphml.xsd", GRAPHML + "/1.1/graphml.xsd",
            namespace + "/gexf.xsd", namespace + "/gexf.xsd/",
        }, "不支持自定义 XML schema 资源。")


def _scalar(text, name, datatype):
    if name == "weight":
        require(datatype in {"int", "long", "float", "double"})
        require(type(text) is str and len(text) <= 64 and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text) is not None)
        if datatype in {"int", "long"}:
            require(re.fullmatch(r"[+-]?\d+", text) is not None)
        return weight(float(text))
    require(datatype == "string")
    return safe_text(text or "", empty=name == "label")


def _graphml(root):
    tag = lambda name: f"{{{GRAPHML}}}{name}"
    _element(root, tag("graphml"), optional={XSI})
    _schema(root, GRAPHML)
    declarations, graph = {}, None
    for item in root:
        if item.tag == tag("key") and graph is None:
            _element(item, tag("key"), {"id", "for", "attr.name", "attr.type"})
            key = safe_text(item.attrib["id"], identifier=True)
            scope, name, dtype = (item.attrib[k] for k in ("for", "attr.name", "attr.type"))
            require(len(declarations) < 8 and key not in declarations and scope in {"node", "edge", "all"})
            require(name in ({"label", "group"} if scope == "node" else {"label", "weight"} if scope == "edge" else {"label"}), "GraphML 仅支持 label/group/weight 属性。")
            require(not any(d[0] == scope and d[1] == name or d[1] == name and (scope == "all" or d[0] == "all") for d in declarations.values()))
            require(dtype in ({"int", "long", "float", "double"} if name == "weight" else {"string"}) and len(item) <= 1)
            default = None
            if len(item):
                _element(item[0], tag("default"), text=True)
                require(not len(item[0]))
                default = _scalar(item[0].text, name, dtype)
            declarations[key] = (scope, name, dtype, default)
        else:
            require(graph is None)
            _element(item, tag("graph"), {"edgedefault"}, {"id"})
            if "id" in item.attrib: safe_text(item.attrib["id"], identifier=True)
            graph = item
    require(graph is not None and graph.attrib["edgedefault"] in {"directed", "undirected"})
    nodes, edges = [], []
    for item in graph:
        node = item.tag == tag("node")
        _element(item, tag("node") if node else tag("edge"), {"id"} if node else {"source", "target"}, () if node else {"id"})
        attrs = dict(item.attrib)
        scope = "node" if node else "edge"
        seen = set()
        for data in item:
            _element(data, tag("data"), {"key"}, text=True)
            key = data.attrib["key"]
            require(key in declarations and key not in seen and not len(data))
            target, name, dtype, _ = declarations[key]
            require(target in {scope, "all"})
            attrs[name] = _scalar(data.text, name, dtype)
            seen.add(key)
        for target, name, _, default in declarations.values():
            if target in {scope, "all"} and name not in attrs and default is not None: attrs[name] = default
        (nodes if node else edges).append(attrs)
        require(len(nodes) <= MAX_NODES and len(edges) <= MAX_EDGES)
    return {"directed": graph.attrib["edgedefault"] == "directed", "nodes": nodes, "edges": edges}, "graphml-flat-static"


def _gexf(root):
    namespace = root.tag[1:].split("}")[0] if type(root.tag) is str and root.tag.startswith("{") else ""
    require(namespace in GEXF)
    tag = lambda name: f"{{{namespace}}}{name}"
    _element(root, tag("gexf"), {"version"}, {XSI})
    _schema(root, namespace)
    require(root.attrib["version"] == GEXF[namespace] and len(root) == 1)
    graph = root[0]
    _element(graph, tag("graph"), {"defaultedgetype"}, {"mode"})
    require(graph.attrib.get("mode", "static") == "static" and graph.attrib["defaultedgetype"] in {"directed", "undirected"})
    require(len(graph) in {1, 2})
    nodes, edges, seen = [], [], set()
    for collection in graph:
        name = "nodes" if collection.tag == tag("nodes") else "edges"
        _element(collection, tag(name))
        require(name not in seen)
        seen.add(name)
        for item in collection:
            node = name == "nodes"
            _element(item, tag("node") if node else tag("edge"), {"id"} if node else {"source", "target"}, {"label"} if node else {"id", "label", "weight"})
            require(not len(item))
            attrs = dict(item.attrib)
            if "weight" in attrs: attrs["weight"] = _scalar(attrs["weight"], "weight", "double")
            (nodes if node else edges).append(attrs)
            require(len(nodes) <= MAX_NODES and len(edges) <= MAX_EDGES)
    require("nodes" in seen)
    return {"directed": graph.attrib["defaultedgetype"] == "directed", "nodes": nodes, "edges": edges}, f"gexf-{GEXF[namespace]}-static"


def scientific_graph_preview(data, fmt, options=None):
    validate_scientific_graph_options("graph", {} if options is None else options)
    require(type(data) is bytes and 0 < len(data) <= MAX_INPUT_BYTES, "静态网络图整文件预算为 4 MiB。")
    require(type(fmt) is str and fmt.lstrip(".") in {"json", "graphml", "gexf"})
    fmt = fmt.lstrip(".")
    try:
        text = data.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise ScientificGraphError("图文件必须为 UTF-8 编码。") from exc
    raw, dialect = _json(text) if fmt == "json" else _graphml(_xml(text)) if fmt == "graphml" else _gexf(_xml(text))
    require(type(raw["nodes"]) is list and 1 <= len(raw["nodes"]) <= MAX_NODES and type(raw["edges"]) is list and len(raw["edges"]) <= MAX_EDGES, "网络图最多 1000 个节点、3000 条边，不自动截断。")
    nodes, edges, ids = [], [], {}
    for item in raw["nodes"]:
        _keys(item, {"id"}, {"label", "group"})
        key = safe_text(item["id"], identifier=True)
        require(key not in ids, "网络图节点 ID 重复。")
        identifier = f"n{len(nodes)}"
        ids[key] = identifier
        nodes.append({"id": identifier, "key": key, "label": item.get("label", key), "group": item.get("group")})
    for item in raw["edges"]:
        _keys(item, {"source", "target"}, {"id", "label", "weight"})
        source, target = safe_text(item["source"], identifier=True), safe_text(item["target"], identifier=True)
        require(source in ids and target in ids, "边的端点必须引用当前图的已声明节点。")
        edges.append({"id": f"e{len(edges)}", "key": item.get("id"), "source": ids[source], "target": ids[target], "label": item.get("label", ""), "weight": item.get("weight")})
    value = {"contract_version": 2, "type": "scientific-graph", "reader": "scientific-graph", "kind": "graph", "media_type": "application/json", "graph": {"directed": raw["directed"], "nodes": nodes, "edges": edges}, "metadata": {"format": fmt, "dialect": dialect, "input_mode": "whole", "source_bytes": len(data), "node_count": len(nodes), "edge_count": len(edges), "simple": True}, "warnings": list(WARNINGS), "sampled": False}
    return validate_scientific_graph_payload(value, size=len(data))
