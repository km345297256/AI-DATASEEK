"""Pure bounded Newick display contract; mirrored verbatim in the API service."""
import json
import re
import unicodedata
from decimal import Decimal

MAX_INPUT = 4 * 1024**2
MAX_OUTPUT = 1024**2
MAX_NODES = 1000
MAX_DEPTH = 64
FORMATS = {"nwk", "newick", "tree", "tre"}
WARNINGS = [
    "仅显示单棵 Newick 树；显示起点不表示已确认的生物学根，不进行重定根或系统发育推断。",
    "内部节点标签（包括数字）仍是标签，不自动解释为支持度；枝长单位未知，根的入枝长不参与坐标。",
    "缺失枝长不补零；只有全部非根枝长明确且并非全零时才提供枝长模式，数值坐标用于展示。",
]
NUMBER = re.compile(r"\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?\Z")
RESOURCE = re.compile(r"[<>{}]|\b[a-z][a-z0-9+.-]{1,20}://|\b(?:data|javascript|file):|(?:^|\s)[/\\]|[A-Za-z]:[/\\]", re.I)


class PhylogenyError(ValueError):
    pass


def require(condition, message="系统树格式、类型或资源预算无效。"):
    if not condition:
        raise PhylogenyError(message)


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def label(value):
    require(value is None or type(value) is str and 1 <= len(value) <= 256)
    if value is not None:
        require(not any(unicodedata.category(c).startswith("C") for c in value) and not RESOURCE.search(value),
                "系统树标签不允许 HTML、外部资源、控制字符或路径。")
    return value


def length(value):
    require(value is None or type(value) is str and 1 <= len(value) <= 32)
    if value is not None:
        require(NUMBER.fullmatch(value) is not None, "枝长须为有界非负实数；不接受负数、NaN 或无限值。")
        number = Decimal(value)
        require(number.is_finite() and 0 <= number <= Decimal("1e9") and (number == 0 or number >= Decimal("1e-12")),
                "枝长须为 0 或 10^-12 至 10^9 之间的非负数。")
        return number
    return None


def validate_phylogeny_options(kind, options):
    require(type(kind) is str and kind == "tree" and type(options) is dict and not options,
            "单树预览只接受 tree 和空 options；视图切换不重新读取文件。")
    return {}


def tree_statistics(nodes):
    require(type(nodes) is list and 2 <= len(nodes) <= MAX_NODES)
    depths, children, stack = [], [0] * len(nodes), []
    missing, has_positive = 0, False
    for index, node in enumerate(nodes):
        require(type(node) is dict and set(node) == {"id", "parent", "label", "length"} and node["id"] == f"n{index}")
        label(node["label"])
        number = length(node["length"])
        if index == 0:
            require(node["parent"] is None)
            depth = 0
        else:
            require(type(node["parent"]) is str and re.fullmatch(r"n(?:0|[1-9][0-9]{0,2})", node["parent"]) is not None)
            parent = int(node["parent"][1:])
            require(parent < index and parent in stack)
            while stack[-1] != parent:
                stack.pop()
            depth = depths[parent] + 1
            children[parent] += 1
            missing += number is None
            has_positive = has_positive or number is not None and number > 0
        require(depth <= MAX_DEPTH)
        depths.append(depth)
        stack.append(index)
    require(children[0] > 0)
    return {"node_count": len(nodes), "leaf_count": children.count(0), "max_depth": max(depths),
            "missing_lengths": missing, "root_length_present": nodes[0]["length"] is not None,
            "branch_length_mode": missing == 0 and has_positive}


def validate_phylogeny_payload(value, *, size=None, fmt=None, limit=MAX_OUTPUT):
    require(type(value) is dict and set(value) == {"contract_version", "type", "reader", "kind", "media_type", "phylogeny", "metadata", "warnings", "sampled"})
    require(integer(value["contract_version"], 2, 2) and value["type"] == value["reader"] == "phylogeny" and value["kind"] == "tree" and value["media_type"] == "application/json")
    require(value["warnings"] == WARNINGS and value["sampled"] is False)
    tree = value["phylogeny"]
    require(type(tree) is dict and set(tree) == {"root", "rootedness", "nodes"} and tree["root"] == "n0" and tree["rootedness"] == "unspecified")
    statistics = tree_statistics(tree["nodes"])
    meta = value["metadata"]
    require(type(meta) is dict and set(meta) == {"format", "dialect", "input_mode", "source_bytes", *statistics})
    require(type(meta["format"]) is str and meta["format"] in FORMATS and meta["dialect"] == "newick-single-v1" and meta["input_mode"] == "whole")
    require(integer(meta["source_bytes"], 1, MAX_INPUT) and (size is None or integer(size, 1, MAX_INPUT) and size == meta["source_bytes"]))
    require(fmt is None or type(fmt) is str and fmt in FORMATS and meta["format"] == fmt)
    for key, expected in statistics.items():
        require(type(meta[key]) is type(expected) and meta[key] == expected)
    require(integer(limit, 1, MAX_OUTPUT))
    try:
        require(len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= limit)
    except (ValueError, TypeError, UnicodeError):
        raise PhylogenyError("系统树输出编码或大小无效。") from None
    return value

