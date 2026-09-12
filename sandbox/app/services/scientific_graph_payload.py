"""Pure, inert scientific graph contract; keep the backend copy identical."""
import json
import math
import re
import unicodedata

MAX_INPUT_BYTES = 4 * 1024**2
MAX_OUTPUT_BYTES = 1024**2
MAX_NODES = 1000
MAX_EDGES = 3000
FORMATS = {"json": {"dataseek-graph-json-v1"}, "graphml": {"graphml-flat-static"}, "gexf": {"gexf-1.2-static", "gexf-1.3-static"}}
WARNINGS = [
    "仅支持有界静态简单图；不支持自环、平行边、层级、动态图、外部资源或可执行样式。",
    "布局仅用于展示；不推断生物学关系，不把边权重解释为距离或因果强度。",
]
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_RESOURCE = re.compile(r"[<>]|\b[a-z][a-z0-9+.-]{1,20}://|\b(?:data|javascript|file):|(?:^|\s)[/\\]|[A-Za-z]:[/\\]", re.I)


class ScientificGraphError(ValueError):
    pass


def require(condition, message="科学网络图格式、类型或资源预算无效。"):
    if not condition:
        raise ScientificGraphError(message)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def safe_text(value, *, identifier=False, empty=False):
    require(type(value) is str and (0 if empty else 1) <= len(value) <= (128 if identifier else 256))
    require(not any(unicodedata.category(c).startswith("C") for c in value) and not _RESOURCE.search(value), "图标 URL、HTML、控制字符与主机路径不属于可展示的图标签。")
    if identifier:
        require(_ID.fullmatch(value) is not None, "图标识须为 1–128 个英文字母、数字、下划线、点、冒号或短横线。")
    return value


def weight(value):
    require(value is None or type(value) in {int, float} and abs(value) <= 10**12 and math.isfinite(value), "边权重须为有限数值，绝对值不超过 10^12。")
    return value


def validate_scientific_graph_options(kind, options):
    require(type(kind) is str and kind == "graph" and type(options) is dict and not options, "静态网络图只接受 graph 视图和空 options；布局在本地执行。")
    return {}


def validate_scientific_graph_payload(value, *, size=None, limit=MAX_OUTPUT_BYTES):
    require(type(value) is dict and set(value) == {"contract_version", "type", "reader", "kind", "media_type", "graph", "metadata", "warnings", "sampled"})
    require(integer(value["contract_version"], 2, 2) and value["type"] == value["reader"] == "scientific-graph" and value["kind"] == "graph" and value["media_type"] == "application/json")
    require(value["warnings"] == WARNINGS and value["sampled"] is False)
    meta = value["metadata"]
    require(type(meta) is dict and set(meta) == {"format", "dialect", "input_mode", "source_bytes", "node_count", "edge_count", "simple"})
    require(type(meta["format"]) is str and meta["format"] in FORMATS and type(meta["dialect"]) is str and meta["dialect"] in FORMATS[meta["format"]] and meta["input_mode"] == "whole" and meta["simple"] is True)
    require(integer(meta["source_bytes"], 1, MAX_INPUT_BYTES) and (size is None or integer(size, 1, MAX_INPUT_BYTES) and size == meta["source_bytes"]))
    graph = value["graph"]
    require(type(graph) is dict and set(graph) == {"directed", "nodes", "edges"} and type(graph["directed"]) is bool)
    nodes, edges = graph["nodes"], graph["edges"]
    require(type(nodes) is list and 1 <= len(nodes) <= MAX_NODES and type(edges) is list and len(edges) <= MAX_EDGES)
    require(integer(meta["node_count"], 1, MAX_NODES) and meta["node_count"] == len(nodes) and integer(meta["edge_count"], 0, MAX_EDGES) and meta["edge_count"] == len(edges))
    keys = set()
    ids = set()
    for index, node in enumerate(nodes):
        require(type(node) is dict and set(node) == {"id", "key", "label", "group"} and node["id"] == f"n{index}")
        safe_text(node["key"], identifier=True)
        safe_text(node["label"])
        if node["group"] is not None:
            safe_text(node["group"])
        require(node["key"] not in keys)
        keys.add(node["key"])
        ids.add(node["id"])
    edge_keys, pairs = set(), set()
    for index, edge in enumerate(edges):
        require(type(edge) is dict and set(edge) == {"id", "key", "source", "target", "label", "weight"} and edge["id"] == f"e{index}")
        if edge["key"] is not None:
            safe_text(edge["key"], identifier=True)
            require(edge["key"] not in edge_keys)
            edge_keys.add(edge["key"])
        require(type(edge["source"]) is str and type(edge["target"]) is str and edge["source"] in ids and edge["target"] in ids and edge["source"] != edge["target"])
        safe_text(edge["label"], empty=True)
        weight(edge["weight"])
        pair = (edge["source"], edge["target"]) if graph["directed"] else tuple(sorted((edge["source"], edge["target"])))
        require(pair not in pairs, "不支持同一方向的平行边或无向重复边。")
        pairs.add(pair)
    require(integer(limit, 1, MAX_OUTPUT_BYTES) and len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) <= limit)
    return value
